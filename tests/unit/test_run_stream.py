import asyncio
from pathlib import Path

import pytest

from app.application.run_service import RunService
from app.application.run_stream import RunStreamCoordinator
from app.config import Settings
from app.domain.enums import EvidenceLevel, RunStatus
from app.domain.models import AnswerResponse, RunContext
from app.infra.local_repositories import JsonlTrajectoryRepository
from app.observability.events import RunEventRecorder


class PausedRuntime:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        del context
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return AnswerResponse(
            answer_markdown=f"done: {user_input}",
            confidence=EvidenceLevel.INSUFFICIENT,
        )


@pytest.mark.asyncio
async def test_observer_disconnect_does_not_cancel_server_run(tmp_path: Path) -> None:
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
    assert repeated.run_id == started.run_id
    assert repeated.coalesced is True
    assert runtime.calls == 1
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
