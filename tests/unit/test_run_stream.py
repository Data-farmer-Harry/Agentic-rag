import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import app.application.run_stream as run_stream_module
from app.application.run_event_recorder import RunEventRecorder
from app.application.run_service import RunService
from app.application.run_stream import (
    RunStreamCoordinator,
    _tool_progress,
    _waiting_progress,
)
from app.config import Settings
from app.domain.enums import EvidenceLevel, RunStatus
from app.domain.models import AdaptiveRAGRoute, AnswerResponse, RunContext, ToolEvent
from app.infra.local_repositories import JsonlTrajectoryRepository


class PausedRuntime:
    def __init__(
        self,
        memory_ids: list[UUID] | None = None,
        adaptive_route: AdaptiveRAGRoute | None = None,
    ) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.memory_ids = memory_ids or []
        self.adaptive_route = adaptive_route

    async def prepare_route(
        self,
        user_input: str,
        context: RunContext,
    ) -> AdaptiveRAGRoute | None:
        del user_input, context
        return self.adaptive_route

    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        del context
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return AnswerResponse(
            answer_markdown=f"done: {user_input}",
            confidence=EvidenceLevel.INSUFFICIENT,
            memory_ids=self.memory_ids,
            adaptive_rag_route=self.adaptive_route,
        )


def test_waiting_progress_is_truthful_and_tool_aware() -> None:
    initial = _waiting_progress(500)
    planning = _waiting_progress(5_000)
    waiting = _waiting_progress(13_000)
    slow = _waiting_progress(31_000)
    retrieval = ToolEvent(
        tool_name="search_knowledge",
        input_hash="a" * 64,
        output_summary="evidence_count=3",
    )
    after_retrieval = _waiting_progress(8_000, last_tool=retrieval)
    graph = _tool_progress(
        ToolEvent(
            tool_name="retrieve_evidence_subgraph",
            input_hash="b" * 64,
        )
    )

    assert initial == {
        "phase": "understanding",
        "label": "正在判断是否需要检索或调用工具",
        "slow": False,
    }
    assert planning["label"] == "正在规划需要的知识与工具"
    assert waiting == {
        "phase": "executing",
        "label": "正在等待模型或工具返回",
        "slow": True,
    }
    assert slow["label"] == "任务仍在运行，结果会自动保留"
    assert slow["slow"] is True
    assert after_retrieval["label"] == "已完成知识库检索，正在分析证据"
    assert graph["label"] == "已完成图谱查询，正在分析关系"


@pytest.mark.asyncio
async def test_running_stream_emits_explanatory_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 0.0

    def advance_clock() -> float:
        nonlocal clock
        clock += 5.0
        return clock

    monkeypatch.setattr(run_stream_module, "perf_counter", advance_clock)
    runtime = PausedRuntime()
    recorder = RunEventRecorder(tmp_path / "events.jsonl")
    service = RunService(
        runtime=runtime,
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
        event_recorder=recorder,
    )
    coordinator = RunStreamCoordinator(service, recorder)
    started = await coordinator.start(
        "explain a complex architecture",
        idempotency_key="request-progress-1234",
    )
    await asyncio.wait_for(runtime.started.wait(), timeout=0.5)

    observer = coordinator.stream(started.run_id)
    events = []
    while True:
        event = await asyncio.wait_for(anext(observer), timeout=1.5)
        events.append(event)
        if event.event == "run.heartbeat":
            break

    heartbeat = events[-1]
    runtime.release.set()
    await observer.aclose()
    await coordinator.close()

    assert [item.event for item in events[:4]] == [
        "run.accepted",
        "run.route",
        "run.status",
        "run.status",
    ]
    assert heartbeat.payload["label"] in {
        "正在规划需要的知识与工具",
        "正在等待模型或工具返回",
    }
    assert heartbeat.payload["phase"] == "executing"


@pytest.mark.asyncio
async def test_observer_disconnect_does_not_cancel_server_run(tmp_path: Path) -> None:
    memory_id = uuid4()
    runtime = PausedRuntime(memory_ids=[memory_id])
    recorder = RunEventRecorder(tmp_path / "events.jsonl")
    service = RunService(
        runtime=runtime,
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
        event_recorder=recorder,
    )
    coordinator = RunStreamCoordinator(service, recorder)
    started = await coordinator.start(
        "long task",
        idempotency_key="request-12345678",
        session_id="session-a",
    )
    await asyncio.wait_for(runtime.started.wait(), timeout=0.5)

    observer = coordinator.stream(started.run_id)
    first = await anext(observer)
    assert first.event == "run.accepted"
    await observer.aclose()
    still_running = await service.get_run(started.run_id)

    runtime.release.set()
    for _ in range(100):
        completed = await service.get_run(started.run_id)
        if completed is not None and completed.status == RunStatus.COMPLETED:
            break
        await asyncio.sleep(0.01)
    events = await coordinator.list_events(started.run_id)
    repeated = await coordinator.start(
        "long task",
        idempotency_key="request-12345678",
        session_id="session-a",
    )

    assert still_running is not None and still_running.status == RunStatus.RUNNING
    assert completed is not None and completed.status == RunStatus.COMPLETED
    assert events[-1].event == "run.completed"
    assert events[-1].payload["answer"]["memory_ids"] == [str(memory_id)]
    assert repeated.run_id == started.run_id
    assert repeated.coalesced is True
    assert runtime.calls == 1
    await coordinator.close()


@pytest.mark.asyncio
async def test_stream_exposes_direct_conversation_lane(tmp_path: Path) -> None:
    runtime = PausedRuntime(
        adaptive_route=AdaptiveRAGRoute(
            strategy="no_retrieval",
            knowledge_route="conversation",
            self_reflection=False,
            confidence="high",
            signals=["adaptive_model"],
        )
    )
    recorder = RunEventRecorder(tmp_path / "events.jsonl")
    service = RunService(
        runtime=runtime,
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
        event_recorder=recorder,
    )
    coordinator = RunStreamCoordinator(service, recorder)

    started = await coordinator.start("你好", idempotency_key="request-conversation-1234")
    runtime.release.set()
    for _ in range(100):
        events = await coordinator.list_events(started.run_id)
        if any(item.event == "run.route" for item in events):
            break
        await asyncio.sleep(0.01)

    route = next(item for item in events if item.event == "run.route")
    assert route.payload["route"] == "conversation"
    assert route.payload["strategy"] == "no_retrieval"
    assert route.payload["requires_graph"] is False
    await coordinator.close()


@pytest.mark.asyncio
async def test_explicit_cancel_is_persisted_as_terminal_event(tmp_path: Path) -> None:
    runtime = PausedRuntime()
    recorder = RunEventRecorder(tmp_path / "events.jsonl")
    service = RunService(
        runtime=runtime,
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
        event_recorder=recorder,
    )
    coordinator = RunStreamCoordinator(service, recorder)
    started = await coordinator.start(
        "cancel me",
        idempotency_key="request-cancel-1234",
    )
    await asyncio.wait_for(runtime.started.wait(), timeout=0.5)

    cancelled = await coordinator.cancel(started.run_id)
    events = await coordinator.list_events(started.run_id)

    assert cancelled.status == RunStatus.CANCELLED
    assert events[-1].event == "run.cancelled"
    assert events[-1].payload["status"] == "cancelled"
    await coordinator.close()
