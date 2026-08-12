from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    input: str
    passed: bool
    run_id: str | None = None
    route: str | None = None
    strategy: str | None = None
    tool_names: list[str] = Field(default_factory=list)
    expected_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    first_event_ms: int | None = None
    first_tool_ms: int | None = None
    total_ms: int
    terminal_event: str | None = None
    answer_preview: str = ""
    error_code: str | None = None
    attempts: int = 1


class AgentE2EReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str = "agent-e2e-v1"
    generated_at: datetime
    base_url: str
    project_id: str
    passed: bool
    pass_rate: float
    cases: list[CaseResult]


@dataclass(frozen=True, slots=True)
class _Case:
    case_id: str
    prompt: str
    expected_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    expected_route: str | None = None


CASES = (
    _Case(
        "greeting",
        "哈哈你好",
        forbidden_tools=("search_knowledge", "search_web", "read_web_page"),
        expected_route="conversation",
    ),
    _Case("calculator", "请精确计算 sqrt(81) + 2 ** 8，只给出计算结果。", ("calculate",)),
    _Case("current-time", "现在 Asia/Shanghai 的准确时间是什么？", ("current_time",)),
    _Case(
        "web-search",
        "搜索当前 OpenAI Responses API 的官方网页搜索文档，给出来源。",
        ("search_web",),
    ),
    _Case(
        "web-page",
        "读取 https://developers.openai.com/api/docs/guides/tools-web-search "
        "的正文，概括网页搜索工具。",
        ("read_web_page",),
    ),
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Run real HermesGraph chat/SSE vertical cases")
    value.add_argument("--base-url", default="http://127.0.0.1:8001")
    value.add_argument("--project-id", default="default")
    value.add_argument("--output", type=Path, required=True)
    value.add_argument("--timeout", type=float, default=180.0)
    value.add_argument("--attempts", type=int, default=3)
    return value


async def _run_case(
    client: httpx.AsyncClient,
    case: _Case,
    *,
    base_url: str,
    project_id: str,
    attempts: int,
) -> CaseResult:
    started = time.perf_counter()
    last_error = "start_failed"
    for attempt in range(1, attempts + 1):
        key = f"e2e-{case.case_id}-{uuid4().hex}"
        response = await client.post(
            f"{base_url}/v1/projects/{project_id}/runs/start",
            json={
                "input": case.prompt,
                "session_id": f"e2e-{case.case_id}-{uuid4().hex}",
                "idempotency_key": key,
            },
        )
        if response.status_code == 202:
            run_id = str(response.json()["run_id"])
            return await _collect_case(
                client,
                case,
                run_id=run_id,
                base_url=base_url,
                project_id=project_id,
                started=started,
                attempt=attempt,
            )
        last_error = _error_code(response)
        if response.status_code < 500 or attempt == attempts:
            break
        await asyncio.sleep(min(8.0, attempt * 2.0))
    return CaseResult(
        case_id=case.case_id,
        input=case.prompt,
        passed=False,
        expected_tools=list(case.expected_tools),
        forbidden_tools=list(case.forbidden_tools),
        total_ms=round((time.perf_counter() - started) * 1_000),
        error_code=last_error,
        attempts=attempts,
    )


async def _collect_case(
    client: httpx.AsyncClient,
    case: _Case,
    *,
    run_id: str,
    base_url: str,
    project_id: str,
    started: float,
    attempt: int,
) -> CaseResult:
    events: list[tuple[str, dict[str, Any]]] = []
    first_event_ms: int | None = None
    first_tool_ms: int | None = None
    async with client.stream(
        "GET",
        f"{base_url}/v1/projects/{project_id}/runs/{run_id}/events/stream",
        params={"after_cursor": 0},
        headers={"Accept": "text/event-stream"},
    ) as response:
        response.raise_for_status()
        event_name = "message"
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif not line and data_lines:
                payload = json.loads("\n".join(data_lines))
                events.append((event_name, payload))
                elapsed = round((time.perf_counter() - started) * 1_000)
                first_event_ms = first_event_ms or elapsed
                if event_name == "tool.completed":
                    first_tool_ms = first_tool_ms or elapsed
                if event_name in {"run.completed", "run.error", "run.cancelled"}:
                    break
                event_name = "message"
                data_lines = []

    route_payload = next((item for name, item in events if name == "run.route"), {})
    terminal_name, terminal = next(
        ((name, item) for name, item in reversed(events) if name.startswith("run.")),
        ("run.error", {}),
    )
    tool_names = [
        str(item.get("tool_name"))
        for name, item in events
        if name == "tool.completed" and item.get("tool_name")
    ]
    answer = terminal.get("answer", {}) if isinstance(terminal, dict) else {}
    expected_ok = all(name in tool_names for name in case.expected_tools)
    forbidden_ok = all(name not in tool_names for name in case.forbidden_tools)
    route_ok = case.expected_route is None or route_payload.get("route") == case.expected_route
    passed = terminal_name == "run.completed" and expected_ok and forbidden_ok and route_ok
    return CaseResult(
        case_id=case.case_id,
        input=case.prompt,
        passed=passed,
        run_id=run_id,
        route=route_payload.get("route"),
        strategy=route_payload.get("strategy"),
        tool_names=tool_names,
        expected_tools=list(case.expected_tools),
        forbidden_tools=list(case.forbidden_tools),
        first_event_ms=first_event_ms,
        first_tool_ms=first_tool_ms,
        total_ms=round((time.perf_counter() - started) * 1_000),
        terminal_event=terminal_name,
        answer_preview=str(answer.get("answer_markdown", ""))[:500],
        error_code=(str(terminal.get("code")) if terminal_name == "run.error" else None),
        attempts=attempt,
    )


def _error_code(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return f"http_{response.status_code}:{str(detail or 'unknown')[:200]}"


async def run(args: argparse.Namespace) -> int:
    if args.attempts < 1 or args.attempts > 5:
        raise ValueError("--attempts must be between 1 and 5")
    timeout = httpx.Timeout(args.timeout, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = [
            await _run_case(
                client,
                case,
                base_url=args.base_url.rstrip("/"),
                project_id=args.project_id,
                attempts=args.attempts,
            )
            for case in CASES
        ]
    passed_count = sum(item.passed for item in results)
    report = AgentE2EReport(
        generated_at=datetime.now(UTC),
        base_url=args.base_url,
        project_id=args.project_id,
        passed=passed_count == len(results),
        pass_rate=round(passed_count / len(results), 4),
        cases=results,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(report.model_dump_json(indent=2))
    return int(not report.passed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parser().parse_args())))
