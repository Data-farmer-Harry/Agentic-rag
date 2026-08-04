from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from uuid import UUID

from app.domain.models import RunStreamEvent, ToolEvent


class RunEventRecorder:
    """Tool fan-out plus an optional durable, cursor-addressable run event log."""

    def __init__(self, stream_path: Path | None = None) -> None:
        self._events: dict[UUID, list[ToolEvent]] = defaultdict(list)
        self._subscribers: dict[UUID, set[asyncio.Queue[ToolEvent]]] = defaultdict(set)
        self._stream_path = stream_path
        self._stream_events: dict[UUID, list[RunStreamEvent]] = defaultdict(list)
        self._stream_cursors: dict[UUID, int] = {}
        self._stream_subscribers: dict[
            UUID, set[asyncio.Queue[RunStreamEvent]]
        ] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def record_tool(self, run_id: UUID, event: ToolEvent) -> None:
        async with self._lock:
            self._events[run_id].append(event)
            for queue in self._subscribers.get(run_id, set()):
                queue.put_nowait(event)

    async def subscribe_tools(self, run_id: UUID) -> asyncio.Queue[ToolEvent]:
        queue: asyncio.Queue[ToolEvent] = asyncio.Queue()
        async with self._lock:
            self._subscribers[run_id].add(queue)
        return queue

    async def unsubscribe_tools(
        self,
        run_id: UUID,
        queue: asyncio.Queue[ToolEvent],
    ) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(run_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(run_id, None)

    async def drain_tools(self, run_id: UUID) -> list[ToolEvent]:
        async with self._lock:
            return self._events.pop(run_id, [])

    async def record_stream(
        self,
        run_id: UUID,
        event: str,
        payload: dict[str, object],
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
        session_id: str,
    ) -> RunStreamEvent:
        async with self._lock:
            if run_id not in self._stream_cursors:
                existing = self._read_persisted(run_id)
                if existing:
                    self._stream_events[run_id] = existing
                self._stream_cursors[run_id] = max(
                    (item.cursor for item in existing),
                    default=0,
                )
            cursor = self._stream_cursors[run_id] + 1
            recorded = RunStreamEvent(
                run_id=run_id,
                cursor=cursor,
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
                session_id=session_id,
                event=event,
                payload=payload,
            )
            self._stream_cursors[run_id] = cursor
            self._stream_events[run_id].append(recorded)
            self._append_persisted(recorded)
            for queue in self._stream_subscribers.get(run_id, set()):
                queue.put_nowait(recorded)
            return recorded

    async def list_stream(
        self,
        run_id: UUID,
        *,
        after_cursor: int = 0,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> list[RunStreamEvent]:
        async with self._lock:
            events = self._stream_events.get(run_id)
            if events is None or (not events and self._stream_path is not None):
                events = self._read_persisted(run_id)
                if events:
                    self._stream_events[run_id] = events
                    self._stream_cursors[run_id] = events[-1].cursor
            return [
                item
                for item in events or []
                if item.cursor > after_cursor
                and item.tenant_id == tenant_id
                and item.project_id == project_id
                and item.user_id == user_id
            ]

    async def subscribe_stream(
        self,
        run_id: UUID,
        *,
        after_cursor: int = 0,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> tuple[list[RunStreamEvent], asyncio.Queue[RunStreamEvent]]:
        queue: asyncio.Queue[RunStreamEvent] = asyncio.Queue()
        async with self._lock:
            events = self._stream_events.get(run_id)
            if events is None or (not events and self._stream_path is not None):
                events = self._read_persisted(run_id)
                if events:
                    self._stream_events[run_id] = events
                    self._stream_cursors[run_id] = events[-1].cursor
            backlog = [
                item
                for item in events or []
                if item.cursor > after_cursor
                and item.tenant_id == tenant_id
                and item.project_id == project_id
                and item.user_id == user_id
            ]
            self._stream_subscribers[run_id].add(queue)
        return backlog, queue

    async def unsubscribe_stream(
        self,
        run_id: UUID,
        queue: asyncio.Queue[RunStreamEvent],
    ) -> None:
        async with self._lock:
            subscribers = self._stream_subscribers.get(run_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._stream_subscribers.pop(run_id, None)

    def _read_persisted(self, run_id: UUID) -> list[RunStreamEvent]:
        if self._stream_path is None or not self._stream_path.exists():
            return list(self._stream_events.get(run_id, []))
        events: list[RunStreamEvent] = []
        for line in self._stream_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = RunStreamEvent.model_validate(json.loads(line))
            if item.run_id == run_id:
                events.append(item)
        events.sort(key=lambda item: item.cursor)
        return events

    def _append_persisted(self, event: RunStreamEvent) -> None:
        if self._stream_path is None:
            return
        self._stream_path.parent.mkdir(parents=True, exist_ok=True)
        with self._stream_path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json(exclude_none=True) + "\n")
