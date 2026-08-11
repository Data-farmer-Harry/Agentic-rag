import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from app.application.run_event_recorder import RunEventRecorder
from app.domain.models import ToolEvent


@pytest.mark.asyncio
async def test_tool_subscriber_receives_copy_without_consuming_trajectory_events() -> None:
    recorder = RunEventRecorder()
    run_id = uuid4()
    queue = await recorder.subscribe_tools(run_id)
    event = ToolEvent(
        tool_name="search_knowledge",
        input_hash="query-hash",
        output_summary="evidence=2",
        duration_ms=18,
    )

    await recorder.record_tool(run_id, event)

    assert await asyncio.wait_for(queue.get(), timeout=0.1) == event
    assert await recorder.drain_tools(run_id) == [event]

    await recorder.unsubscribe_tools(run_id, queue)
    await recorder.record_tool(run_id, event.model_copy(update={"input_hash": "second"}))
    assert queue.empty()


@pytest.mark.asyncio
async def test_stream_events_are_persistent_scoped_and_cursor_addressable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "run_events.jsonl"
    run_id = uuid4()
    recorder = RunEventRecorder(path)
    accepted = await recorder.record_stream(
        run_id,
        "run.accepted",
        {"run_id": str(run_id)},
        tenant_id="local",
        project_id="default",
        user_id="user-a",
        session_id="session-a",
    )
    status = await recorder.record_stream(
        run_id,
        "run.status",
        {"label": "working"},
        tenant_id="local",
        project_id="default",
        user_id="user-a",
        session_id="session-a",
    )

    backlog, queue = await recorder.subscribe_stream(
        run_id,
        after_cursor=accepted.cursor,
        user_id="user-a",
    )
    assert backlog == [status]
    heartbeat = await recorder.record_stream(
        run_id,
        "run.heartbeat",
        {"elapsed_ms": 1000},
        tenant_id="local",
        project_id="default",
        user_id="user-a",
        session_id="session-a",
    )
    assert await asyncio.wait_for(queue.get(), timeout=0.1) == heartbeat
    await recorder.unsubscribe_stream(run_id, queue)

    reloaded = RunEventRecorder(path)
    replay = await reloaded.list_stream(
        run_id,
        after_cursor=accepted.cursor,
        user_id="user-a",
    )
    hidden = await reloaded.list_stream(run_id, user_id="user-b")

    assert [item.cursor for item in replay] == [2, 3]
    assert [item.event for item in replay] == ["run.status", "run.heartbeat"]
    assert hidden == []
