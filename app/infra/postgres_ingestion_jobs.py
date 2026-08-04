from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from asyncpg import Pool, Record

from app.domain.models import IngestionJob, OutboxEvent
from app.infra.postgres import PostgresDatabase, PostgresDatabaseError, PostgresMigration
from app.infra.postgres_outbox import insert_outbox_event
from app.knowledge.job_errors import (
    IngestionJobLeaseLostError,
    IngestionJobRepositoryError,
)

INGESTION_JOB_MIGRATIONS = (
    PostgresMigration(
        version=1,
        name="ingestion_jobs",
        statement="""
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    job_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    user_id text NOT NULL,
    filename text NOT NULL,
    media_type text,
    byte_size bigint NOT NULL CHECK (byte_size > 0),
    content_hash char(64) NOT NULL,
    staging_key text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('queued', 'running', 'retry_scheduled', 'succeeded', 'failed', 'cancelled')
    ),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
    available_at timestamptz NOT NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    document_id uuid,
    deduplicated boolean,
    can_retry boolean NOT NULL DEFAULT false,
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS ingestion_jobs_claim_idx
ON ingestion_jobs (status, available_at, created_at);

CREATE INDEX IF NOT EXISTS ingestion_jobs_scope_idx
ON ingestion_jobs (tenant_id, project_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ingestion_jobs_active_content_idx
ON ingestion_jobs (tenant_id, project_id, content_hash)
WHERE status IN ('queued', 'running', 'retry_scheduled');
""",
    ),
    PostgresMigration(
        version=3,
        name="ingestion_job_knowledge_source",
        statement="""
ALTER TABLE ingestion_jobs
ADD COLUMN IF NOT EXISTS source jsonb NOT NULL DEFAULT
    jsonb_build_object(
        'source_type', 'uploaded_document',
        'source_id', '',
        'privacy', 'private',
        'trust', 'user_asserted'
    );
""",
    ),
)


class PostgresIngestionJobRepository:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        manage_database: bool = False,
        emit_outbox: bool = False,
    ) -> None:
        self._database = database
        self._manage_database = manage_database
        self._emit_outbox = emit_outbox

    async def start(self) -> None:
        try:
            await self._database.start()
            await self._database.migrate(INGESTION_JOB_MIGRATIONS)
        except BaseException:
            if self._manage_database:
                await self._database.close()
            raise

    async def close(self) -> None:
        if self._manage_database:
            await self._database.close()

    async def enqueue(self, job: IngestionJob) -> tuple[IngestionJob, bool]:
        pool = self._require_pool()
        lock_id = _content_lock_id(job.tenant_id, job.project_id, job.content_hash)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", lock_id)
            existing = await connection.fetchrow(
                """
SELECT * FROM ingestion_jobs
WHERE tenant_id = $1 AND project_id = $2 AND content_hash = $3
  AND status IN ('queued', 'running', 'retry_scheduled')
ORDER BY created_at ASC
LIMIT 1
""",
                job.tenant_id,
                job.project_id,
                job.content_hash,
            )
            if existing is not None:
                return _job_from_record(existing), False
            inserted = await connection.fetchrow(
                """
INSERT INTO ingestion_jobs (
    job_id, tenant_id, project_id, user_id, filename, media_type,
    byte_size, content_hash, staging_key, source, status, attempt, max_attempts,
    available_at, lease_owner, lease_expires_at, document_id, deduplicated,
    can_retry, error_code, error_message, created_at, started_at,
    completed_at, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8, $9, $10::jsonb, $11, $12, $13,
    $14, $15, $16, $17, $18,
    $19, $20, $21, $22, $23,
    $24, $25
)
RETURNING *
""",
                job.job_id,
                job.tenant_id,
                job.project_id,
                job.user_id,
                job.filename,
                job.media_type,
                job.byte_size,
                job.content_hash,
                job.staging_key,
                json.dumps(job.source.model_dump(mode="json"), sort_keys=True),
                job.status.value,
                job.attempt,
                job.max_attempts,
                job.available_at,
                job.lease_owner,
                job.lease_expires_at,
                job.document_id,
                job.deduplicated,
                job.can_retry,
                job.error_code,
                job.error_message,
                job.created_at,
                job.started_at,
                job.completed_at,
                job.updated_at,
            )
        if inserted is None:
            raise IngestionJobRepositoryError("Postgres did not return the queued job")
        return _job_from_record(inserted), True

    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> IngestionJob | None:
        record = await self._require_pool().fetchrow(
            """
SELECT * FROM ingestion_jobs
WHERE job_id = $1 AND tenant_id = $2 AND project_id = $3
""",
            job_id,
            tenant_id,
            project_id,
        )
        return _job_from_record(record) if record is not None else None

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 100,
    ) -> Sequence[IngestionJob]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        records = await self._require_pool().fetch(
            """
SELECT * FROM ingestion_jobs
WHERE tenant_id = $1 AND project_id = $2
ORDER BY created_at DESC
LIMIT $3
""",
            tenant_id,
            project_id,
            limit,
        )
        return [_job_from_record(record) for record in records]

    async def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> IngestionJob | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if not 10 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
UPDATE ingestion_jobs
SET status = 'failed',
    can_retry = true,
    error_code = 'lease_exhausted',
    error_message = 'Worker lease expired after the maximum number of attempts.',
    lease_owner = NULL,
    lease_expires_at = NULL,
    completed_at = now(),
    updated_at = now()
WHERE status = 'running'
  AND lease_expires_at < now()
  AND attempt >= max_attempts
"""
            )
            record = await connection.fetchrow(
                """
WITH candidate AS (
    SELECT job_id
    FROM ingestion_jobs
    WHERE (
        status IN ('queued', 'retry_scheduled')
        AND available_at <= now()
    ) OR (
        status = 'running'
        AND lease_expires_at < now()
        AND attempt < max_attempts
    )
    ORDER BY available_at ASC, created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE ingestion_jobs AS job
SET status = 'running',
    attempt = job.attempt + 1,
    lease_owner = $1,
    lease_expires_at = now() + ($2 * interval '1 second'),
    started_at = COALESCE(job.started_at, now()),
    completed_at = NULL,
    can_retry = false,
    error_code = NULL,
    error_message = NULL,
    updated_at = now()
FROM candidate
WHERE job.job_id = candidate.job_id
RETURNING job.*
""",
                worker_id,
                lease_seconds,
            )
        return _job_from_record(record) if record is not None else None

    async def renew_lease(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        result = await self._require_pool().execute(
            """
UPDATE ingestion_jobs
SET lease_expires_at = now() + ($3 * interval '1 second'), updated_at = now()
WHERE job_id = $1 AND status = 'running' AND lease_owner = $2
""",
            job_id,
            worker_id,
            lease_seconds,
        )
        return result == "UPDATE 1"

    async def complete(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        document_id: UUID,
        deduplicated: bool,
    ) -> IngestionJob:
        async with self._require_pool().acquire() as connection, connection.transaction():
            record = await connection.fetchrow(
                """
UPDATE ingestion_jobs
SET status = 'succeeded', document_id = $3, deduplicated = $4,
    can_retry = false, lease_owner = NULL, lease_expires_at = NULL,
    completed_at = now(), updated_at = now()
WHERE job_id = $1 AND status = 'running' AND lease_owner = $2
RETURNING *
""",
                job_id,
                worker_id,
                document_id,
                deduplicated,
            )
            completed = self._leased_result(record)
            if self._emit_outbox:
                await insert_outbox_event(connection, _completed_event(completed))
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
        record = await self._require_pool().fetchrow(
            """
UPDATE ingestion_jobs
SET status = CASE
        WHEN $5 AND attempt < max_attempts THEN 'retry_scheduled'
        ELSE 'failed'
    END,
    available_at = CASE
        WHEN $5 AND attempt < max_attempts
        THEN now() + ($6 * interval '1 second')
        ELSE available_at
    END,
    can_retry = $5,
    error_code = $3,
    error_message = $4,
    lease_owner = NULL,
    lease_expires_at = NULL,
    completed_at = CASE
        WHEN $5 AND attempt < max_attempts THEN NULL
        ELSE now()
    END,
    updated_at = now()
WHERE job_id = $1 AND status = 'running' AND lease_owner = $2
RETURNING *
""",
            job_id,
            worker_id,
            error_code,
            error_message[:1_000],
            retryable,
            max(1, retry_delay_seconds),
        )
        return self._leased_result(record)

    async def cancel(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> IngestionJob | None:
        record = await self._require_pool().fetchrow(
            """
UPDATE ingestion_jobs
SET status = 'cancelled', can_retry = true,
    error_code = 'cancelled_by_user',
    error_message = 'The ingestion job was cancelled before completion.',
    lease_owner = NULL, lease_expires_at = NULL,
    completed_at = now(), updated_at = now()
WHERE job_id = $1 AND tenant_id = $2 AND project_id = $3
  AND status IN ('queued', 'retry_scheduled', 'running')
RETURNING *
""",
            job_id,
            tenant_id,
            project_id,
        )
        return _job_from_record(record) if record is not None else None

    async def retry(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> IngestionJob | None:
        record = await self._require_pool().fetchrow(
            """
UPDATE ingestion_jobs
SET status = 'queued', attempt = 0, available_at = now(),
    can_retry = false, error_code = NULL, error_message = NULL,
    started_at = NULL, completed_at = NULL, updated_at = now()
WHERE job_id = $1 AND tenant_id = $2 AND project_id = $3
  AND status IN ('failed', 'cancelled') AND can_retry = true
RETURNING *
""",
            job_id,
            tenant_id,
            project_id,
        )
        return _job_from_record(record) if record is not None else None

    def _require_pool(self) -> Pool:
        try:
            return self._database.pool
        except PostgresDatabaseError as exc:
            raise IngestionJobRepositoryError(
                "Postgres ingestion repository is not started"
            ) from exc

    @staticmethod
    def _leased_result(record: Record | None) -> IngestionJob:
        if record is None:
            raise IngestionJobLeaseLostError(
                "The ingestion job lease is no longer owned by this worker"
            )
        return _job_from_record(record)


def _job_from_record(record: Record) -> IngestionJob:
    payload = dict(record)
    payload["source"] = _decode_json_object(payload.get("source"))
    return IngestionJob.model_validate(payload)


def _decode_json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return cast(dict[str, Any], decoded)
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    raise IngestionJobRepositoryError("Invalid JSON source metadata from Postgres")


def _content_lock_id(tenant_id: str, project_id: str, content_hash: str) -> int:
    digest = hashlib.sha256(f"{tenant_id}\0{project_id}\0{content_hash}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _completed_event(job: IngestionJob) -> OutboxEvent:
    return OutboxEvent(
        event_id=uuid5(
            NAMESPACE_URL,
            f"hermesgraph:outbox:ingestion.job.succeeded:{job.job_id}:{job.attempt}",
        ),
        aggregate_type="ingestion_job",
        aggregate_id=str(job.job_id),
        event_type="ingestion.job.succeeded",
        payload={
            "job_id": str(job.job_id),
            "tenant_id": job.tenant_id,
            "project_id": job.project_id,
            "document_id": str(job.document_id),
            "deduplicated": job.deduplicated,
            "attempt": job.attempt,
        },
    )
