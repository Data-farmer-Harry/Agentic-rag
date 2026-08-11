from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from app.application.run_event_recorder import RunEventRecorder
from app.application.run_service import RunService, answer_from_trajectory
from app.application.workspace_service import WorkspaceService
from app.domain.enums import RunStatus
from app.domain.models import RunStreamEvent, RunTrajectory, ToolEvent
from app.retrieval.knowledge_query_router import public_adaptive_route, route_knowledge_query

TERMINAL_STREAM_EVENTS = {"run.completed", "run.error", "run.cancelled"}


def public_run_error(exc: Exception) -> dict[str, object]:
    detail = str(exc).casefold()
    if "429" in detail or "model_cooldown" in detail or "rate limit" in detail:
        return {
            "code": "provider_busy",
            "message": "模型服务当前繁忙，请稍后重试。",
            "retryable": True,
        }
    if "timeout" in detail or "timed out" in detail:
        return {
            "code": "provider_timeout",
            "message": "模型响应超时，请重新发送；复杂任务也可以稍后再试。",
            "retryable": True,
        }
    if "401" in detail or "unauthorized" in detail or "api key" in detail:
        return {
            "code": "provider_authentication_failed",
            "message": "模型服务暂时不可用，请检查连接配置后重试。",
            "retryable": False,
        }
    return {
        "code": "run_failed",
        "message": "这次任务没有完成，请重新发送后再试。",
        "retryable": True,
    }


@dataclass(frozen=True)
class RunStartResult:
    run_id: UUID
    status: RunStatus
    idempotency_key: str
    coalesced: bool


class RunStreamCoordinator:
    """Owns run execution independently from any individual HTTP connection."""

    def __init__(
        self,
        run_service: RunService,
        recorder: RunEventRecorder,
        workspace: WorkspaceService | None = None,
    ) -> None:
        self._runs = run_service
        self._recorder = recorder
        self._workspace = workspace
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._start_lock = asyncio.Lock()

    async def start(
        self,
        user_input: str,
        *,
        idempotency_key: str,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        session_id: str = "default",
        domain_pack: str | None = None,
    ) -> RunStartResult:
        async with self._start_lock:
            resolved_domain_pack = self._runs.resolve_domain_pack(
                domain_pack,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            existing = await self._runs.get_by_idempotency_key(
                idempotency_key,
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
            )
            if existing is not None:
                context = existing.context
                if (
                    existing.user_input != user_input
                    or context.session_id != session_id
                    or context.domain_pack != resolved_domain_pack
                ):
                    raise ValueError("Idempotency key is already bound to a different run request")
                return RunStartResult(
                    run_id=existing.context.run_id,
                    status=existing.status,
                    idempotency_key=idempotency_key,
                    coalesced=True,
                )

            run_id = uuid4()
            trajectory = await self._runs.prepare_run(
                user_input,
                run_id=run_id,
                idempotency_key=idempotency_key,
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
                session_id=session_id,
                domain_pack=resolved_domain_pack,
            )
            await self._emit(
                trajectory,
                "run.accepted",
                {
                    "request_id": idempotency_key,
                    "run_id": str(run_id),
                    "project_id": project_id,
                    "domain_pack": resolved_domain_pack,
                },
            )
            await self._emit(trajectory, "run.route", _public_route(trajectory))
            await self._emit(
                trajectory,
                "run.status",
                {
                    "status": "understanding",
                    "phase": "understanding",
                    "label": "正在理解请求",
                },
            )
            task = asyncio.create_task(self._execute(trajectory))
            self._tasks[run_id] = task
            task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
            return RunStartResult(
                run_id=run_id,
                status=RunStatus.RUNNING,
                idempotency_key=idempotency_key,
                coalesced=False,
            )

    async def list_events(
        self,
        run_id: UUID,
        *,
        after_cursor: int = 0,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> list[RunStreamEvent]:
        trajectory = await self._require_scoped_run(
            run_id,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        await self._ensure_terminal_event(trajectory)
        return await self._recorder.list_stream(
            run_id,
            after_cursor=after_cursor,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )

    async def stream(
        self,
        run_id: UUID,
        *,
        after_cursor: int = 0,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> AsyncGenerator[RunStreamEvent, None]:
        trajectory = await self._require_scoped_run(
            run_id,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        await self._ensure_terminal_event(trajectory)
        backlog, queue = await self._recorder.subscribe_stream(
            run_id,
            after_cursor=after_cursor,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        try:
            for event in backlog:
                yield event
                if event.event in TERMINAL_STREAM_EVENTS:
                    return
            while True:
                event = await queue.get()
                yield event
                if event.event in TERMINAL_STREAM_EVENTS:
                    return
        finally:
            await self._recorder.unsubscribe_stream(run_id, queue)

    async def cancel(
        self,
        run_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> RunTrajectory:
        trajectory = await self._require_scoped_run(
            run_id,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        if trajectory.status != RunStatus.RUNNING:
            return trajectory
        task = self._tasks.get(run_id)
        if task is None:
            interrupted = await self._mark_orphaned(trajectory)
            return interrupted
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        current = await self._runs.get_run(run_id)
        return current or trajectory

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _execute(self, trajectory: RunTrajectory) -> None:
        run_id = trajectory.context.run_id
        started = perf_counter()
        tool_queue: asyncio.Queue[ToolEvent] | None = None
        execution: asyncio.Task[RunTrajectory] | None = None
        emitted_tools: set[str] = set()

        def tool_key(event: ToolEvent) -> str:
            return f"{event.tool_name}:{event.input_hash}:{event.created_at.isoformat()}"

        try:
            tool_queue = await self._runs.subscribe_tool_events(run_id)
            await self._emit(
                trajectory,
                "run.status",
                {
                    "status": "working",
                    "phase": "executing",
                    "label": "正在选择处理路径",
                },
            )
            execution = asyncio.create_task(self._runs.execute_run(trajectory))
            heartbeat = 0
            last_tool: ToolEvent | None = None
            while not execution.done():
                event = None
                if tool_queue is None:
                    await asyncio.sleep(0.75)
                else:
                    try:
                        event = await asyncio.wait_for(tool_queue.get(), timeout=0.75)
                    except TimeoutError:
                        pass
                if event is not None:
                    last_tool = event
                    emitted_tools.add(tool_key(event))
                    await self._emit(
                        trajectory,
                        "tool.completed",
                        event.model_dump(mode="json"),
                    )
                    await self._emit(
                        trajectory,
                        "run.status",
                        _tool_progress(event),
                    )
                    continue
                heartbeat += 1
                elapsed_ms = int((perf_counter() - started) * 1000)
                await self._emit(
                    trajectory,
                    "run.heartbeat",
                    {
                        "sequence": heartbeat,
                        "elapsed_ms": elapsed_ms,
                        **_waiting_progress(elapsed_ms, last_tool=last_tool),
                    },
                )
            if tool_queue is not None:
                while True:
                    try:
                        event = tool_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    emitted_tools.add(tool_key(event))
                    await self._emit(
                        trajectory,
                        "tool.completed",
                        event.model_dump(mode="json"),
                    )
                    await self._emit(
                        trajectory,
                        "run.status",
                        _tool_progress(event),
                    )
            completed = await execution
            answer = answer_from_trajectory(completed)
            await self._emit(
                completed,
                "run.status",
                {
                    "status": "synthesizing",
                    "phase": "synthesizing",
                    "label": (
                        "正在整理回复"
                        if answer.response_mode.value == "conversational"
                        else "正在整理有依据的回答"
                    ),
                },
            )
            for event in completed.tool_events:
                if tool_key(event) not in emitted_tools:
                    await self._emit(
                        completed,
                        "tool.completed",
                        event.model_dump(mode="json"),
                    )
            for chunk in _answer_chunks(answer.answer_markdown):
                await self._emit(completed, "answer.delta", {"delta": chunk})
                await asyncio.sleep(0.012)
            for citation in answer.citations:
                await self._emit(
                    completed,
                    "evidence.added",
                    citation.model_dump(mode="json"),
                )
            run_changes: list[Any] = []
            if self._workspace is not None:
                try:
                    run_changes = [
                        item
                        for item in await self._workspace.list_change_sets(
                            tenant_id=completed.context.tenant_id,
                            project_id=completed.context.project_id,
                        )
                        if completed.context.run_id in item.source_run_ids
                    ]
                except Exception:
                    run_changes = []
                if run_changes:
                    await self._emit(
                        completed,
                        "learning.updated",
                        {
                            "count": len(run_changes),
                            "targets": [item.target_type for item in run_changes],
                        },
                    )
            await self._emit(
                completed,
                "run.completed",
                _completed_payload(
                    completed,
                    learning_change_count=len(run_changes),
                    duration_ms=int((perf_counter() - started) * 1000),
                ),
            )
        except asyncio.CancelledError:
            if execution is not None and not execution.done():
                execution.cancel()
                with suppress(asyncio.CancelledError):
                    await execution
            current = await self._runs.get_run(run_id)
            if current is not None and current.status == RunStatus.RUNNING:
                current = await self._runs.mark_cancelled(run_id)
            await self._ensure_terminal_event(current or trajectory)
        except Exception as exc:
            payload = public_run_error(exc)
            payload.update(
                {
                    "phase": "executing",
                    "duration_ms": int((perf_counter() - started) * 1000),
                }
            )
            await self._emit(trajectory, "run.error", payload)
        finally:
            await self._runs.unsubscribe_tool_events(run_id, tool_queue)

    async def _ensure_terminal_event(self, trajectory: RunTrajectory) -> None:
        context = trajectory.context
        existing = await self._recorder.list_stream(
            context.run_id,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
        )
        if any(item.event in TERMINAL_STREAM_EVENTS for item in existing):
            return
        if trajectory.status == RunStatus.RUNNING:
            if context.run_id not in self._tasks:
                await self._mark_orphaned(trajectory)
            return
        duration_ms = _trajectory_duration_ms(trajectory)
        if trajectory.status == RunStatus.COMPLETED and trajectory.answer is not None:
            await self._emit(
                trajectory,
                "run.completed",
                _completed_payload(
                    trajectory,
                    learning_change_count=0,
                    duration_ms=duration_ms,
                ),
            )
        elif trajectory.status == RunStatus.CANCELLED:
            await self._emit(
                trajectory,
                "run.cancelled",
                {
                    "run_id": str(context.run_id),
                    "status": "cancelled",
                    "message": "任务已停止。",
                    "duration_ms": duration_ms,
                },
            )
        else:
            await self._emit(
                trajectory,
                "run.error",
                {
                    "code": "run_failed",
                    "message": "这次任务没有完成，请重新发送后再试。",
                    "retryable": True,
                    "phase": "executing",
                    "duration_ms": duration_ms,
                },
            )

    async def _mark_orphaned(self, trajectory: RunTrajectory) -> RunTrajectory:
        interrupted = await self._runs.mark_interrupted(trajectory.context.run_id)
        current = interrupted or trajectory
        await self._emit(
            current,
            "run.error",
            {
                "code": "run_interrupted",
                "message": "服务重启中断了这次任务，请重新发送。",
                "retryable": True,
                "phase": "executing",
                "duration_ms": _trajectory_duration_ms(current),
            },
        )
        return current

    async def _require_scoped_run(
        self,
        run_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> RunTrajectory:
        trajectory = await self._runs.get_run(run_id)
        if trajectory is None:
            raise KeyError(f"Unknown run: {run_id}")
        context = trajectory.context
        if (
            context.tenant_id != tenant_id
            or context.project_id != project_id
            or context.user_id != user_id
        ):
            raise KeyError(f"Unknown run: {run_id}")
        return trajectory

    async def _emit(
        self,
        trajectory: RunTrajectory,
        event: str,
        payload: dict[str, object],
    ) -> RunStreamEvent:
        context = trajectory.context
        return await self._recorder.record_stream(
            context.run_id,
            event,
            payload,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            session_id=context.session_id,
        )


def _answer_chunks(text: str, size: int = 24) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]


def _waiting_progress(
    elapsed_ms: int,
    *,
    last_tool: ToolEvent | None = None,
) -> dict[str, object]:
    """Project truthful, bounded wait guidance without exposing model reasoning."""

    if elapsed_ms >= 30_000:
        return {
            "phase": "executing",
            "label": "任务仍在运行，结果会自动保留",
            "slow": True,
        }
    if last_tool is not None:
        return _tool_progress(last_tool)
    if elapsed_ms >= 12_000:
        return {
            "phase": "executing",
            "label": "正在等待模型或工具返回",
            "slow": True,
        }
    if elapsed_ms >= 4_000:
        return {
            "phase": "executing",
            "label": "正在规划需要的知识与工具",
            "slow": False,
        }
    return {
        "phase": "understanding",
        "label": "正在判断是否需要检索或调用工具",
        "slow": False,
    }


def _tool_progress(event: ToolEvent) -> dict[str, object]:
    name = event.tool_name.casefold()
    if not event.success:
        return {
            "phase": "executing",
            "label": "一个步骤未完成，正在判断是否可以继续",
        }
    if "publish_answer" in name:
        label = "回答已通过检查，正在准备展示"
    elif "graph" in name:
        label = "已完成图谱查询，正在分析关系"
    elif "web" in name or "search_online" in name:
        label = "已完成网页搜索，正在核对来源"
    elif "retriev" in name or "knowledge" in name or "rag" in name:
        label = "已完成知识库检索，正在分析证据"
    elif "memory" in name:
        label = "已读取相关记忆，正在结合当前问题"
    elif "vision" in name or "image" in name:
        label = "已完成图片分析，正在整理结果"
    elif "computer" in name or "workspace" in name or "file" in name:
        label = "已读取工作区资料，正在分析内容"
    else:
        label = "已完成一个受控步骤，正在继续处理"
    return {"phase": "executing", "label": label}


def _trajectory_duration_ms(trajectory: RunTrajectory) -> int:
    end = trajectory.completed_at or datetime.now(UTC)
    return max(0, int((end - trajectory.context.started_at).total_seconds() * 1000))


def _completed_payload(
    trajectory: RunTrajectory,
    *,
    learning_change_count: int,
    duration_ms: int,
) -> dict[str, object]:
    answer = answer_from_trajectory(trajectory)
    return {
        "run_id": str(trajectory.context.run_id),
        "status": trajectory.status.value,
        "answer": answer.model_dump(mode="json"),
        "tool_events": [item.model_dump(mode="json") for item in trajectory.tool_events],
        "learning_change_count": learning_change_count,
        "duration_ms": duration_ms,
        "retrieval_route": _public_route(trajectory),
    }


def _public_route(trajectory: RunTrajectory) -> dict[str, object]:
    """Expose the application lane and bounded retrieval policy, never model reasoning."""

    route = trajectory.context.adaptive_rag_route
    if route is None and trajectory.answer is not None:
        route = trajectory.answer.adaptive_rag_route
    if route is not None:
        return public_adaptive_route(route)
    # Offline/test runtimes have no model router. Keep their projection explicit and isolated.
    return route_knowledge_query(trajectory.user_input).model_dump(mode="json")
