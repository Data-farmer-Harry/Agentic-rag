from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Sequence
from contextlib import suppress
from typing import Protocol
from uuid import UUID, uuid4

from app.domain.contracts import KnowledgeRepository, OutboxRepository
from app.domain.enums import DocumentStatus
from app.domain.models import OutboxEvent
from app.graph.candidate_service import KnowledgeGraphIngestionCoordinator
from app.infra.outbox_errors import OutboxLeaseLostError

logger = logging.getLogger(__name__)


class OutboxPublisher(Protocol):
    async def publish(self, event: OutboxEvent) -> None: ...


class AuditLogOutboxPublisher:
    async def publish(self, event: OutboxEvent) -> None:
        logger.info(
            "Published outbox event id=%s type=%s aggregate=%s/%s",
            event.event_id,
            event.event_type,
            event.aggregate_type,
            event.aggregate_id,
        )


class CompositeOutboxPublisher:
    def __init__(self, publishers: Sequence[OutboxPublisher]) -> None:
        if not publishers:
            raise ValueError("At least one outbox publisher is required")
        self._publishers = tuple(publishers)

    async def publish(self, event: OutboxEvent) -> None:
        for publisher in self._publishers:
            await publisher.publish(event)


class KnowledgeGraphEnrichmentPublisher:
    """Durably enrich successful documents without blocking their searchable state."""

    def __init__(
        self,
        knowledge: KnowledgeRepository,
        coordinator: KnowledgeGraphIngestionCoordinator,
    ) -> None:
        self._knowledge = knowledge
        self._coordinator = coordinator

    async def publish(self, event: OutboxEvent) -> None:
        if event.event_type != "ingestion.job.succeeded":
            return
        tenant_id = _required_payload_string(event, "tenant_id")
        project_id = _required_payload_string(event, "project_id")
        document_id = UUID(_required_payload_string(event, "document_id"))
        document = await self._knowledge.get_document(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if document is None or document.status != DocumentStatus.ACTIVE:
            raise RuntimeError("Outbox enrichment document is not active")
        chunks = await self._knowledge.list_chunks(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if not chunks:
            raise RuntimeError("Outbox enrichment document has no chunks")
        await self._coordinator.index_document(document, chunks)


def _required_payload_string(event: OutboxEvent, key: str) -> str:
    value = event.payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Outbox event is missing {key}")
    return value


class OutboxDispatcher:
    def __init__(
        self,
        repository: OutboxRepository,
        publisher: OutboxPublisher,
        *,
        lease_seconds: int = 60,
        poll_seconds: float = 0.5,
        retry_base_seconds: int = 2,
    ) -> None:
        if not 10 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        if not 0.05 <= poll_seconds <= 30:
            raise ValueError("poll_seconds must be between 0.05 and 30")
        if not 1 <= retry_base_seconds <= 3_600:
            raise ValueError("retry_base_seconds must be between 1 and 3600")
        self._repository = repository
        self._publisher = publisher
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._retry_base_seconds = retry_base_seconds
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="outbox-dispatcher")

    async def close(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._wake.set()
        task, self._task = self._task, None
        try:
            await asyncio.wait_for(task, timeout=min(self._lease_seconds, 30))
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def wake(self) -> None:
        self._wake.set()

    async def run_once(self) -> bool:
        event = await self._repository.claim(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if event is None:
            return False
        await self._process(event)
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("Outbox claim loop failed")
                processed = False
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    async def _process(self, event: OutboxEvent) -> None:
        heartbeat = asyncio.create_task(
            self._heartbeat(event.event_id),
            name=f"outbox-heartbeat:{event.event_id}",
        )
        try:
            await self._publisher.publish(event)
            await self._repository.mark_published(
                event.event_id,
                worker_id=self._worker_id,
            )
        except OutboxLeaseLostError:
            logger.warning("Lease lost while publishing outbox event %s", event.event_id)
        except Exception as exc:
            logger.exception("Outbox publisher failed for event %s", event.event_id)
            retry_delay = min(
                3_600,
                self._retry_base_seconds * (2 ** max(0, event.attempt - 1)),
            )
            try:
                await self._repository.fail(
                    event.event_id,
                    worker_id=self._worker_id,
                    error=str(exc) or exc.__class__.__name__,
                    retry_delay_seconds=retry_delay,
                )
            except OutboxLeaseLostError:
                logger.warning("Lease lost while failing outbox event %s", event.event_id)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self, event_id: UUID) -> None:
        interval = max(3.0, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._repository.renew_lease(
                    event_id,
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                )
            except Exception:
                logger.exception("Unable to renew outbox lease for %s", event_id)
                return
            if not renewed:
                return
