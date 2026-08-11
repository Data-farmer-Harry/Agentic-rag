from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
from contextlib import suppress
from pathlib import Path
from uuid import UUID, uuid4

from app.domain.contracts import IngestionJobRepository
from app.domain.models import IngestionJob, IngestionJobSubmission, KnowledgeSource
from app.knowledge.ingestion_job_errors import IngestionJobLeaseLostError
from app.knowledge.knowledge_ingestion import (
    KnowledgeIndexError,
    KnowledgeIngestionError,
    KnowledgeIngestionService,
)

logger = logging.getLogger(__name__)


class IngestionJobTransitionError(ValueError):
    pass


class IngestionJobsUnavailableError(RuntimeError):
    pass


class IngestionStagingError(RuntimeError):
    pass


class IngestionStagingStore:
    def __init__(self, root: Path, *, max_file_bytes: int) -> None:
        self._root = root.resolve()
        self._max_file_bytes = max_file_bytes

    async def stage(
        self,
        job_id: UUID,
        content: bytes,
        *,
        tenant_id: str,
        project_id: str,
    ) -> str:
        scope_hash = hashlib.sha256(f"{tenant_id}\0{project_id}".encode()).hexdigest()[:24]
        key = str(Path(scope_hash) / f"{job_id}.upload")
        await asyncio.to_thread(self._write, key, content)
        return key

    async def read(self, key: str) -> bytes:
        return await asyncio.to_thread(self._read, key)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)

    def _path(self, key: str) -> Path:
        relative = Path(key)
        if relative.is_absolute() or ".." in relative.parts:
            raise IngestionStagingError("Invalid ingestion staging key")
        path = (self._root / relative).resolve()
        if not path.is_relative_to(self._root):
            raise IngestionStagingError("Ingestion staging path escaped its root")
        return path

    def _write(self, key: str, content: bytes) -> None:
        if not content or len(content) > self._max_file_bytes:
            raise IngestionStagingError("Invalid staged upload size")
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, key: str) -> bytes:
        path = self._path(key)
        try:
            with path.open("rb") as handle:
                content = handle.read(self._max_file_bytes + 1)
        except FileNotFoundError as exc:
            raise IngestionStagingError("Staged upload is missing") from exc
        if not content or len(content) > self._max_file_bytes:
            raise IngestionStagingError("Staged upload has an invalid size")
        return content

    def _delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class IngestionJobService:
    def __init__(
        self,
        repository: IngestionJobRepository,
        staging: IngestionStagingStore,
        ingestion: KnowledgeIngestionService,
        *,
        max_attempts: int = 3,
        lease_seconds: int = 300,
        poll_seconds: float = 1.0,
        retry_base_seconds: int = 5,
        worker_enabled: bool = True,
    ) -> None:
        if not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if not 10 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        if not 0.05 <= poll_seconds <= 30:
            raise ValueError("poll_seconds must be between 0.05 and 30")
        if not 1 <= retry_base_seconds <= 3_600:
            raise ValueError("retry_base_seconds must be between 1 and 3600")
        self._repository = repository
        self._staging = staging
        self._ingestion = ingestion
        self._max_attempts = max_attempts
        self._worker = _IngestionWorker(
            repository,
            staging,
            ingestion,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
            retry_base_seconds=retry_base_seconds,
        )
        self._worker_enabled = worker_enabled
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self._repository.start()
        try:
            if self._worker_enabled:
                await self._worker.start()
        except BaseException:
            await self._repository.close()
            raise
        self._started = True

    async def close(self) -> None:
        if not self._started:
            return
        if self._worker_enabled:
            await self._worker.close()
        await self._repository.close()
        self._started = False

    async def submit(
        self,
        *,
        filename: str,
        content: bytes,
        media_type: str | None,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        source: KnowledgeSource | None = None,
    ) -> IngestionJobSubmission:
        safe_name = self._ingestion.validate_submission(filename, content)
        job_id = uuid4()
        staging_key = await self._staging.stage(
            job_id,
            content,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        job = IngestionJob(
            job_id=job_id,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            filename=safe_name,
            media_type=media_type,
            byte_size=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            staging_key=staging_key,
            source=source
            or KnowledgeSource(
                source_id=f"ingestion-job:{job_id}",
            ),
            max_attempts=self._max_attempts,
        )
        try:
            queued, created = await self._repository.enqueue(job)
        except BaseException:
            await self._staging.delete(staging_key)
            raise
        if not created:
            await self._staging.delete(staging_key)
        self._worker.wake()
        return IngestionJobSubmission(job=queued, coalesced=not created)

    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> IngestionJob | None:
        return await self._repository.get(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def list_jobs(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        limit: int = 100,
    ) -> list[IngestionJob]:
        return list(
            await self._repository.list_scoped(
                tenant_id=tenant_id,
                project_id=project_id,
                limit=limit,
            )
        )

    async def cancel(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> IngestionJob:
        cancelled = await self._repository.cancel(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if cancelled is not None:
            return cancelled
        current = await self.get(job_id, tenant_id=tenant_id, project_id=project_id)
        if current is None:
            raise KeyError("Ingestion job not found")
        raise IngestionJobTransitionError(
            f"Cannot cancel an ingestion job in {current.status.value} status"
        )

    async def retry(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> IngestionJob:
        retried = await self._repository.retry(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if retried is not None:
            self._worker.wake()
            return retried
        current = await self.get(job_id, tenant_id=tenant_id, project_id=project_id)
        if current is None:
            raise KeyError("Ingestion job not found")
        raise IngestionJobTransitionError(
            f"Cannot retry an ingestion job in {current.status.value} status"
        )

    async def run_worker_once(self) -> bool:
        return await self._worker.run_once()


class _IngestionWorker:
    def __init__(
        self,
        repository: IngestionJobRepository,
        staging: IngestionStagingStore,
        ingestion: KnowledgeIngestionService,
        *,
        lease_seconds: int,
        poll_seconds: float,
        retry_base_seconds: int,
    ) -> None:
        self._repository = repository
        self._staging = staging
        self._ingestion = ingestion
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
        self._task = asyncio.create_task(self._run(), name="ingestion-worker")

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
        job = await self._repository.claim(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if job is None:
            return False
        await self._process(job)
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("Ingestion worker claim loop failed")
                processed = False
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    async def _process(self, job: IngestionJob) -> None:
        ingested_document_id: UUID | None = None
        heartbeat = asyncio.create_task(
            self._heartbeat(job.job_id),
            name=f"ingestion-heartbeat:{job.job_id}",
        )
        try:
            content = await self._staging.read(job.staging_key)
            result = await self._ingestion.ingest(
                filename=job.filename,
                content=content,
                media_type=job.media_type,
                tenant_id=job.tenant_id,
                project_id=job.project_id,
                user_id=job.user_id,
                source=job.source,
            )
            ingested_document_id = result.document.document_id
            await self._repository.complete(
                job.job_id,
                worker_id=self._worker_id,
                document_id=result.document.document_id,
                deduplicated=result.deduplicated,
            )
            await self._staging.delete(job.staging_key)
        except KnowledgeIngestionError as exc:
            await self._record_failure(
                job,
                error_code="invalid_document",
                error_message=str(exc),
                retryable=False,
            )
        except IngestionStagingError as exc:
            await self._record_failure(
                job,
                error_code="staging_unavailable",
                error_message=str(exc),
                retryable=False,
            )
        except KnowledgeIndexError as exc:
            await self._record_failure(
                job,
                error_code="knowledge_index_failed",
                error_message=str(exc),
                retryable=True,
            )
        except IngestionJobLeaseLostError:
            if ingested_document_id is not None:
                await self._ingestion.archive(
                    ingested_document_id,
                    tenant_id=job.tenant_id,
                    project_id=job.project_id,
                )
            logger.warning("Lease lost while finalizing ingestion job %s", job.job_id)
        except Exception:
            logger.exception("Unexpected ingestion job failure: %s", job.job_id)
            await self._record_failure(
                job,
                error_code="unexpected_ingestion_failure",
                error_message="The ingestion worker encountered a retryable internal error.",
                retryable=True,
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _record_failure(
        self,
        job: IngestionJob,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        retry_delay = min(
            3_600,
            self._retry_base_seconds * (2 ** max(0, job.attempt - 1)),
        )
        try:
            failed = await self._repository.fail(
                job.job_id,
                worker_id=self._worker_id,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
                retry_delay_seconds=retry_delay,
            )
            if failed.status.value == "failed" and not failed.can_retry:
                await self._staging.delete(job.staging_key)
        except IngestionJobLeaseLostError:
            logger.warning("Lease lost while failing ingestion job %s", job.job_id)

    async def _heartbeat(self, job_id: UUID) -> None:
        interval = max(3.0, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._repository.renew_lease(
                    job_id,
                    worker_id=self._worker_id,
                    lease_seconds=self._lease_seconds,
                )
            except Exception:
                logger.exception("Unable to renew ingestion lease for %s", job_id)
                return
            if not renewed:
                return
