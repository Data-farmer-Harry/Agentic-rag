from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from app.domain.enums import OutboxEventStatus
from app.domain.models import KnowledgeChunk, KnowledgeDocument, OutboxEvent, utc_now
from app.infra.outbox_dispatcher import (
    CompositeOutboxPublisher,
    KnowledgeGraphEnrichmentPublisher,
    OutboxDispatcher,
)
from app.infra.outbox_errors import OutboxLeaseLostError


class _MemoryOutboxRepository:
    def __init__(self, events: list[OutboxEvent] | None = None) -> None:
        self.events = list(events or [])
        self.published: list[UUID] = []
        self.failures: list[tuple[UUID, str, int]] = []
        self.renewals: list[UUID] = []

    async def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> OutboxEvent | None:
        event = next(
            (item for item in self.events if item.status == OutboxEventStatus.PENDING),
            None,
        )
        if event is None:
            return None
        claimed = event.model_copy(
            update={
                "status": OutboxEventStatus.PROCESSING,
                "attempt": event.attempt + 1,
                "lease_owner": worker_id,
                "lease_expires_at": utc_now() + timedelta(seconds=lease_seconds),
                "updated_at": utc_now(),
            }
        )
        self.events[self.events.index(event)] = claimed
        return claimed

    async def renew_lease(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        self.renewals.append(event_id)
        return True

    async def mark_published(
        self,
        event_id: UUID,
        *,
        worker_id: str,
    ) -> OutboxEvent:
        event = self._owned(event_id, worker_id)
        published = event.model_copy(
            update={
                "status": OutboxEventStatus.PUBLISHED,
                "lease_owner": None,
                "lease_expires_at": None,
                "published_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.events[self.events.index(event)] = published
        self.published.append(event_id)
        return published

    async def fail(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: int,
    ) -> OutboxEvent:
        event = self._owned(event_id, worker_id)
        failed = event.model_copy(
            update={
                "status": OutboxEventStatus.PENDING,
                "lease_owner": None,
                "lease_expires_at": None,
                "error": error,
                "updated_at": utc_now(),
            }
        )
        self.events[self.events.index(event)] = failed
        self.failures.append((event_id, error, retry_delay_seconds))
        return failed

    async def count_unpublished(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> int:
        return sum(
            item.status in {OutboxEventStatus.PENDING, OutboxEventStatus.PROCESSING}
            and item.payload.get("tenant_id") == tenant_id
            and item.payload.get("project_id") == project_id
            for item in self.events
        )

    def _owned(self, event_id: UUID, worker_id: str) -> OutboxEvent:
        event = next(item for item in self.events if item.event_id == event_id)
        if (
            event.status != OutboxEventStatus.PROCESSING
            or event.lease_owner != worker_id
        ):
            raise OutboxLeaseLostError("lease lost")
        return event


class _RecordingPublisher:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.events: list[OutboxEvent] = []

    async def publish(self, event: OutboxEvent) -> None:
        self.events.append(event)
        if self.error is not None:
            raise self.error


class _KnowledgeFixture:
    def __init__(self, document: KnowledgeDocument, chunk: KnowledgeChunk) -> None:
        self.document = document
        self.chunk = chunk

    async def get_document(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return self.document

    async def list_chunks(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return [self.chunk]


class _RecordingCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[KnowledgeDocument, list[KnowledgeChunk]]] = []

    async def index_document(self, document, chunks):  # type: ignore[no-untyped-def]
        self.calls.append((document, list(chunks)))


def _event(*, attempt: int = 0) -> OutboxEvent:
    return OutboxEvent(
        aggregate_type="knowledge_document",
        aggregate_id="document-1",
        event_type="knowledge.document.metadata_stored",
        payload={"tenant_id": "local", "project_id": "default"},
        attempt=attempt,
    )


@pytest.mark.asyncio
async def test_run_once_returns_false_when_outbox_is_empty() -> None:
    repository = _MemoryOutboxRepository()
    publisher = _RecordingPublisher()
    dispatcher = OutboxDispatcher(repository, publisher)

    assert await dispatcher.run_once() is False
    assert publisher.events == []


@pytest.mark.asyncio
async def test_run_once_publishes_and_acknowledges_event() -> None:
    repository = _MemoryOutboxRepository([_event()])
    publisher = _RecordingPublisher()
    dispatcher = OutboxDispatcher(repository, publisher)

    assert await dispatcher.run_once() is True
    assert [item.event_id for item in publisher.events] == repository.published
    assert repository.events[0].status == OutboxEventStatus.PUBLISHED


@pytest.mark.asyncio
async def test_publish_failure_schedules_exponential_retry() -> None:
    repository = _MemoryOutboxRepository([_event(attempt=2)])
    publisher = _RecordingPublisher(RuntimeError("broker unavailable"))
    dispatcher = OutboxDispatcher(repository, publisher, retry_base_seconds=3)

    assert await dispatcher.run_once() is True
    assert repository.published == []
    assert repository.failures == [
        (repository.events[0].event_id, "broker unavailable", 12)
    ]
    assert repository.events[0].status == OutboxEventStatus.PENDING


@pytest.mark.asyncio
async def test_lease_loss_does_not_attempt_a_second_state_transition() -> None:
    repository = _MemoryOutboxRepository([_event()])
    publisher = _RecordingPublisher(OutboxLeaseLostError("lease lost"))
    dispatcher = OutboxDispatcher(repository, publisher)

    assert await dispatcher.run_once() is True
    assert repository.published == []
    assert repository.failures == []
    assert repository.events[0].status == OutboxEventStatus.PROCESSING


@pytest.mark.asyncio
async def test_success_event_runs_durable_graph_enrichment_before_ack() -> None:
    document = KnowledgeDocument(
        filename="paper.md",
        title="Paper",
        media_type="text/markdown",
        byte_size=32,
        content_hash="a" * 64,
        storage_key="uploads/paper.md",
        chunk_count=1,
    )
    chunk = KnowledgeChunk(
        document_id=document.document_id,
        chunk_index=0,
        text="HermesGraph uses Qdrant.",
        content_hash="b" * 64,
        char_end=25,
    )
    event = OutboxEvent(
        aggregate_type="ingestion_job",
        aggregate_id="job-1",
        event_type="ingestion.job.succeeded",
        payload={
            "tenant_id": "local",
            "project_id": "default",
            "document_id": str(document.document_id),
        },
    )
    coordinator = _RecordingCoordinator()
    audit = _RecordingPublisher()
    publisher = CompositeOutboxPublisher(
        [
            KnowledgeGraphEnrichmentPublisher(  # type: ignore[arg-type]
                _KnowledgeFixture(document, chunk),
                coordinator,
            ),
            audit,
        ]
    )

    await publisher.publish(event)

    assert coordinator.calls == [(document, [chunk])]
    assert audit.events == [event]
