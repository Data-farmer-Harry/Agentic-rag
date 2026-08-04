from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.enums import IngestionJobStatus
from app.domain.models import (
    IngestionJob,
    IngestionResult,
    KnowledgeDocument,
    utc_now,
)
from app.knowledge.ingestion import KnowledgeIndexError, KnowledgeIngestionError
from app.knowledge.job_errors import IngestionJobLeaseLostError
from app.knowledge.jobs import (
    IngestionJobService,
    IngestionJobTransitionError,
    IngestionStagingError,
    IngestionStagingStore,
)


class _MemoryJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[UUID, IngestionJob] = {}
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.started = False

    async def enqueue(self, job: IngestionJob) -> tuple[IngestionJob, bool]:
        existing = next(
            (
                item
                for item in self.jobs.values()
                if item.tenant_id == job.tenant_id
                and item.project_id == job.project_id
                and item.content_hash == job.content_hash
                and item.status
                in {
                    IngestionJobStatus.QUEUED,
                    IngestionJobStatus.RUNNING,
                    IngestionJobStatus.RETRY_SCHEDULED,
                }
            ),
            None,
        )
        if existing is not None:
            return existing, False
        self.jobs[job.job_id] = job
        return job, True

    async def get(self, job_id: UUID, *, tenant_id: str, project_id: str) -> IngestionJob | None:
        job = self.jobs.get(job_id)
        if job is None or job.tenant_id != tenant_id or job.project_id != project_id:
            return None
        return job

    async def list_scoped(
        self, *, tenant_id: str, project_id: str, limit: int = 100
    ) -> list[IngestionJob]:
        return [
            item
            for item in reversed(list(self.jobs.values()))
            if item.tenant_id == tenant_id and item.project_id == project_id
        ][:limit]

    async def claim(self, *, worker_id: str, lease_seconds: int) -> IngestionJob | None:
        job = next(
            (
                item
                for item in self.jobs.values()
                if item.status in {IngestionJobStatus.QUEUED, IngestionJobStatus.RETRY_SCHEDULED}
            ),
            None,
        )
        if job is None:
            return None
        claimed = job.model_copy(
            update={
                "status": IngestionJobStatus.RUNNING,
                "attempt": job.attempt + 1,
                "lease_owner": worker_id,
                "lease_expires_at": utc_now() + timedelta(seconds=lease_seconds),
                "started_at": job.started_at or utc_now(),
                "updated_at": utc_now(),
                "can_retry": False,
            }
        )
        self.jobs[job.job_id] = claimed
        return claimed

    async def renew_lease(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        job = self.jobs[job_id]
        return job.status == IngestionJobStatus.RUNNING and job.lease_owner == worker_id

    async def complete(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        document_id: UUID,
        deduplicated: bool,
    ) -> IngestionJob:
        job = self._owned(job_id, worker_id)
        completed = job.model_copy(
            update={
                "status": IngestionJobStatus.SUCCEEDED,
                "document_id": document_id,
                "deduplicated": deduplicated,
                "lease_owner": None,
                "lease_expires_at": None,
                "completed_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.jobs[job_id] = completed
        return completed

    async def fail(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: int,
    ) -> IngestionJob:
        job = self._owned(job_id, worker_id)
        scheduled = retryable and job.attempt < job.max_attempts
        failed = job.model_copy(
            update={
                "status": (
                    IngestionJobStatus.RETRY_SCHEDULED if scheduled else IngestionJobStatus.FAILED
                ),
                "available_at": utc_now() + timedelta(seconds=retry_delay_seconds),
                "can_retry": retryable,
                "error_code": error_code,
                "error_message": error_message,
                "lease_owner": None,
                "lease_expires_at": None,
                "completed_at": None if scheduled else utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.jobs[job_id] = failed
        return failed

    async def cancel(self, job_id: UUID, *, tenant_id: str, project_id: str) -> IngestionJob | None:
        job = await self.get(job_id, tenant_id=tenant_id, project_id=project_id)
        if job is None or job.status not in {
            IngestionJobStatus.QUEUED,
            IngestionJobStatus.RETRY_SCHEDULED,
            IngestionJobStatus.RUNNING,
        }:
            return None
        cancelled = job.model_copy(
            update={
                "status": IngestionJobStatus.CANCELLED,
                "can_retry": True,
                "lease_owner": None,
                "lease_expires_at": None,
                "completed_at": utc_now(),
            }
        )
        self.jobs[job_id] = cancelled
        return cancelled

    async def retry(self, job_id: UUID, *, tenant_id: str, project_id: str) -> IngestionJob | None:
        job = await self.get(job_id, tenant_id=tenant_id, project_id=project_id)
        if (
            job is None
            or job.status not in {IngestionJobStatus.FAILED, IngestionJobStatus.CANCELLED}
            or not job.can_retry
        ):
            return None
        retried = job.model_copy(
            update={
                "status": IngestionJobStatus.QUEUED,
                "attempt": 0,
                "can_retry": False,
                "error_code": None,
                "error_message": None,
                "completed_at": None,
            }
        )
        self.jobs[job_id] = retried
        return retried

    def _owned(self, job_id: UUID, worker_id: str) -> IngestionJob:
        job = self.jobs[job_id]
        if job.status != IngestionJobStatus.RUNNING or job.lease_owner != worker_id:
            raise IngestionJobLeaseLostError("Ingestion job lease was lost")
        return job


class _ScriptedIngestion:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = iter(outcomes)
        self.calls = 0
        self.archived: list[UUID] = []

    def validate_submission(self, filename: str, content: bytes) -> str:
        if not content:
            raise KnowledgeIngestionError("Uploaded file is empty")
        if not filename.endswith(".md"):
            raise KnowledgeIngestionError("Unsupported file type")
        return Path(filename).name

    async def ingest(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        outcome = next(self.outcomes)
        if outcome == "index_error":
            raise KnowledgeIndexError("Unable to index all document knowledge")
        if outcome == "invalid":
            raise KnowledgeIngestionError("Malformed document")
        content = kwargs["content"]
        document = KnowledgeDocument(
            tenant_id=kwargs["tenant_id"],
            project_id=kwargs["project_id"],
            user_id=kwargs["user_id"],
            filename=kwargs["filename"],
            title="fixture",
            media_type="text/markdown",
            byte_size=len(content),
            content_hash="a" * 64,
            storage_key="fixture/document.md",
            chunk_count=1,
        )
        return IngestionResult(document=document, deduplicated=False)

    async def archive(self, document_id: UUID, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        self.archived.append(document_id)
        return True


class _BlockingIngestion(_ScriptedIngestion):
    def __init__(self) -> None:
        super().__init__(["success"])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ingest(self, **kwargs):  # type: ignore[no-untyped-def]
        self.started.set()
        await self.release.wait()
        return await super().ingest(**kwargs)


def _service(
    tmp_path: Path,
    repository: _MemoryJobRepository,
    ingestion: _ScriptedIngestion,
) -> IngestionJobService:
    return IngestionJobService(
        repository,
        IngestionStagingStore(tmp_path / "staging", max_file_bytes=10_000),
        ingestion,  # type: ignore[arg-type]
        max_attempts=2,
        lease_seconds=10,
        poll_seconds=0.05,
        retry_base_seconds=1,
        worker_enabled=False,
    )


@pytest.mark.asyncio
async def test_job_submission_coalesces_and_success_cleans_staging(tmp_path: Path) -> None:
    repository = _MemoryJobRepository()
    ingestion = _ScriptedIngestion(["success"])
    service = _service(tmp_path, repository, ingestion)
    await service.start()

    first = await service.submit(
        filename="knowledge.md",
        content=b"HermesGraph uses Qdrant.",
        media_type="text/markdown",
    )
    duplicate = await service.submit(
        filename="renamed.md",
        content=b"HermesGraph uses Qdrant.",
        media_type="text/markdown",
    )

    assert first.coalesced is False
    assert duplicate.coalesced is True
    assert duplicate.job.job_id == first.job.job_id
    assert len(list((tmp_path / "staging").rglob("*.upload"))) == 1
    assert await service.run_worker_once() is True
    completed = await service.get(first.job.job_id)
    assert completed is not None
    assert completed.status == IngestionJobStatus.SUCCEEDED
    assert completed.document_id is not None
    assert list((tmp_path / "staging").rglob("*.upload")) == []
    await service.close()


@pytest.mark.asyncio
async def test_retryable_failure_is_scheduled_then_succeeds(tmp_path: Path) -> None:
    repository = _MemoryJobRepository()
    ingestion = _ScriptedIngestion(["index_error", "success"])
    service = _service(tmp_path, repository, ingestion)
    await service.start()
    submitted = await service.submit(
        filename="knowledge.md",
        content=b"Qdrant supports hybrid retrieval.",
        media_type="text/markdown",
    )

    assert await service.run_worker_once() is True
    scheduled = await service.get(submitted.job.job_id)
    assert scheduled is not None
    assert scheduled.status == IngestionJobStatus.RETRY_SCHEDULED
    assert scheduled.attempt == 1
    assert scheduled.can_retry is True
    assert await service.run_worker_once() is True
    completed = await service.get(submitted.job.job_id)
    assert completed is not None
    assert completed.status == IngestionJobStatus.SUCCEEDED
    assert completed.attempt == 2
    assert ingestion.calls == 2
    await service.close()


@pytest.mark.asyncio
async def test_permanent_failure_and_scope_safe_transitions(tmp_path: Path) -> None:
    repository = _MemoryJobRepository()
    service = _service(tmp_path, repository, _ScriptedIngestion(["invalid"]))
    await service.start()
    submitted = await service.submit(
        filename="broken.md",
        content=b"broken but staged",
        media_type="text/markdown",
        project_id="private",
    )

    assert await service.run_worker_once() is True
    failed = await service.get(submitted.job.job_id, project_id="private")
    assert failed is not None
    assert failed.status == IngestionJobStatus.FAILED
    assert failed.error_code == "invalid_document"
    assert failed.can_retry is False
    assert list((tmp_path / "staging").rglob("*.upload")) == []
    assert await service.get(submitted.job.job_id, project_id="other") is None
    with pytest.raises(IngestionJobTransitionError, match="Cannot retry"):
        await service.retry(submitted.job.job_id, project_id="private")
    with pytest.raises(KeyError, match="not found"):
        await service.cancel(submitted.job.job_id, project_id="other")
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_job_can_be_manually_retried(tmp_path: Path) -> None:
    repository = _MemoryJobRepository()
    service = _service(tmp_path, repository, _ScriptedIngestion(["success"]))
    await service.start()
    submitted = await service.submit(
        filename="queued.md",
        content=b"queued content",
        media_type="text/markdown",
    )

    cancelled = await service.cancel(submitted.job.job_id)
    assert cancelled.status == IngestionJobStatus.CANCELLED
    assert cancelled.can_retry is True
    retried = await service.retry(submitted.job.job_id)
    assert retried.status == IngestionJobStatus.QUEUED
    assert retried.attempt == 0
    await service.close()


@pytest.mark.asyncio
async def test_running_job_cancellation_archives_a_late_ingestion_result(
    tmp_path: Path,
) -> None:
    repository = _MemoryJobRepository()
    ingestion = _BlockingIngestion()
    service = _service(tmp_path, repository, ingestion)
    await service.start()
    submitted = await service.submit(
        filename="running.md",
        content=b"late ingestion result",
        media_type="text/markdown",
    )
    worker = asyncio.create_task(service.run_worker_once())
    await ingestion.started.wait()

    cancelled = await service.cancel(submitted.job.job_id)
    ingestion.release.set()
    assert await worker is True

    final = await service.get(submitted.job.job_id)
    assert cancelled.status == IngestionJobStatus.CANCELLED
    assert final is not None and final.status == IngestionJobStatus.CANCELLED
    assert len(ingestion.archived) == 1
    await service.close()


@pytest.mark.asyncio
async def test_submission_validation_and_staging_traversal_fail_closed(
    tmp_path: Path,
) -> None:
    repository = _MemoryJobRepository()
    service = _service(tmp_path, repository, _ScriptedIngestion([]))
    await service.start()

    with pytest.raises(KnowledgeIngestionError, match="Unsupported"):
        await service.submit(
            filename="payload.exe",
            content=b"payload",
            media_type="application/octet-stream",
        )
    assert repository.jobs == {}
    staging = IngestionStagingStore(tmp_path / "staging", max_file_bytes=10_000)
    with pytest.raises(IngestionStagingError, match="Invalid"):
        await staging.read("../outside")
    await service.close()
