from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast
from uuid import UUID, uuid4

from asyncpg import Connection, Pool, Record
from asyncpg.exceptions import UniqueViolationError  # type: ignore[import-untyped]

from app.domain.models import LearningJob, LearningJobCheckpoint, LearningJobResult
from app.infra.postgres import PostgresDatabase, PostgresDatabaseError, PostgresMigration
from app.infra.postgres_learning_context import (
    PostgresLearningTransaction,
    current_postgres_learning_transaction,
    postgres_learning_transaction,
)
from app.learning.job_errors import (
    LearningJobLeaseLostError,
    LearningJobRepositoryError,
)

LEARNING_JOB_MIGRATIONS = (
    PostgresMigration(
        version=4,
        name="learning_jobs",
        statement="""
CREATE TABLE IF NOT EXISTS learning_jobs (
    job_id uuid PRIMARY KEY,
    idempotency_key char(64) NOT NULL UNIQUE,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    user_id text NOT NULL,
    run_id uuid NOT NULL,
    trigger text NOT NULL CHECK (trigger IN ('run_completed', 'feedback_received')),
    trajectory jsonb NOT NULL,
    status text NOT NULL CHECK (
        status IN ('queued', 'running', 'retry_scheduled', 'succeeded', 'failed', 'cancelled')
    ),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
    available_at timestamptz NOT NULL,
    lease_owner text,
    lease_token uuid,
    lease_expires_at timestamptz,
    result jsonb,
    can_retry boolean NOT NULL DEFAULT false,
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS learning_jobs_claim_idx
ON learning_jobs (available_at, created_at)
WHERE status IN ('queued', 'retry_scheduled', 'running');

CREATE INDEX IF NOT EXISTS learning_jobs_running_lease_idx
ON learning_jobs (lease_expires_at)
WHERE status = 'running';

CREATE INDEX IF NOT EXISTS learning_jobs_scope_idx
ON learning_jobs (tenant_id, project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS learning_jobs_run_idx
ON learning_jobs (tenant_id, project_id, run_id, created_at);
""",
    ),
    PostgresMigration(
        version=5,
        name="learning_jobs_single_active_run",
        statement="""
CREATE UNIQUE INDEX IF NOT EXISTS learning_jobs_single_active_run_idx
ON learning_jobs (tenant_id, project_id, run_id)
WHERE status = 'running';
""",
    ),
    PostgresMigration(
        version=7,
        name="learning_job_checkpoints",
        statement="""
ALTER TABLE learning_jobs
ADD COLUMN IF NOT EXISTS checkpoint jsonb;

ALTER TABLE learning_jobs
ADD COLUMN IF NOT EXISTS checkpoint_updated_at timestamptz;
""",
    ),
    PostgresMigration(
        version=9,
        name="learning_stage_artifact_links",
        statement="""
ALTER TABLE learning_jobs
ADD COLUMN IF NOT EXISTS reconciliation_status text NOT NULL DEFAULT 'not_required'
CHECK (
    reconciliation_status IN ('not_required', 'pending', 'verified', 'required')
);

ALTER TABLE learning_jobs
ADD COLUMN IF NOT EXISTS reconciliation_error text;

CREATE TABLE IF NOT EXISTS learning_job_artifact_links (
    job_id uuid NOT NULL REFERENCES learning_jobs(job_id) ON DELETE CASCADE,
    stage_name text NOT NULL CHECK (
        stage_name IN (
            'reflection_completed',
            'artifacts_committed',
            'observations_committed',
            'evolution_committed'
        )
    ),
    artifact_type text NOT NULL CHECK (
        artifact_type IN (
            'reflection',
            'memory',
            'change_set',
            'skill',
            'observation',
            'evaluation',
            'transition'
        )
    ),
    artifact_id uuid NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, artifact_type, artifact_id),
    UNIQUE (job_id, stage_name, artifact_type, ordinal)
);

CREATE INDEX IF NOT EXISTS learning_job_artifact_links_stage_idx
ON learning_job_artifact_links (job_id, stage_name, ordinal);
""",
    ),
    PostgresMigration(
        version=10,
        name="learning_artifact_link_versions",
        statement="""
ALTER TABLE learning_job_artifact_links
ADD COLUMN IF NOT EXISTS artifact_version text;

CREATE INDEX IF NOT EXISTS learning_job_artifact_links_version_idx
ON learning_job_artifact_links (artifact_type, artifact_id, artifact_version);
""",
    ),
)


class PostgresLearningJobRepository:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        manage_database: bool = False,
    ) -> None:
        self._database = database
        self._manage_database = manage_database

    async def start(self) -> None:
        try:
            await self._database.start()
            await self._database.migrate(LEARNING_JOB_MIGRATIONS)
        except BaseException:
            if self._manage_database:
                await self._database.close()
            raise

    async def close(self) -> None:
        if self._manage_database:
            await self._database.close()

    async def enqueue(self, job: LearningJob) -> tuple[LearningJob, bool]:
        pool = self._require_pool()
        lock_id = _idempotency_lock_id(job.idempotency_key)
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", lock_id)
            existing = await connection.fetchrow(
                "SELECT * FROM learning_jobs WHERE idempotency_key = $1",
                job.idempotency_key,
            )
            if existing is not None:
                return _job_from_record(existing), False
            inserted = await connection.fetchrow(
                """
INSERT INTO learning_jobs (
    job_id, idempotency_key, tenant_id, project_id, user_id, run_id,
    trigger, trajectory, status, attempt, max_attempts, available_at,
    lease_owner, lease_token, lease_expires_at, checkpoint, result, can_retry,
    error_code, error_message, created_at, started_at, completed_at, updated_at,
    checkpoint_updated_at, reconciliation_status, reconciliation_error
) VALUES (
    $1, $2, $3, $4, $5, $6,
    $7, $8::jsonb, $9, $10, $11, $12,
    $13, $14, $15, $16::jsonb, $17::jsonb, $18,
    $19, $20, $21, $22, $23, $24,
    $25, $26, $27
)
RETURNING *
""",
                job.job_id,
                job.idempotency_key,
                job.tenant_id,
                job.project_id,
                job.user_id,
                job.run_id,
                job.trigger,
                json.dumps(
                    job.trajectory.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                job.status.value,
                job.attempt,
                job.max_attempts,
                job.available_at,
                job.lease_owner,
                job.lease_token,
                job.lease_expires_at,
                _encode_checkpoint(job.checkpoint),
                _encode_result(job.result),
                job.can_retry,
                job.error_code,
                job.error_message,
                job.created_at,
                job.started_at,
                job.completed_at,
                job.updated_at,
                job.checkpoint.updated_at if job.checkpoint is not None else None,
                job.reconciliation_status,
                job.reconciliation_error,
            )
        if inserted is None:
            raise LearningJobRepositoryError("Postgres did not return the queued job")
        return _job_from_record(inserted), True

    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningJob | None:
        record = await self._require_pool().fetchrow(
            """
SELECT * FROM learning_jobs
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
    ) -> Sequence[LearningJob]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        records = await self._require_pool().fetch(
            """
SELECT * FROM learning_jobs
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
    ) -> LearningJob | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if not 10 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        lease_token = uuid4()
        pool = self._require_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                await connection.execute(
                    """
UPDATE learning_jobs
SET status = 'failed',
    can_retry = true,
    error_code = 'lease_exhausted',
    error_message = 'Worker lease expired after the maximum number of attempts.',
    lease_owner = NULL,
    lease_token = NULL,
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
    SELECT candidate.job_id
    FROM learning_jobs AS candidate
    WHERE (
        (
            candidate.status IN ('queued', 'retry_scheduled')
            AND candidate.available_at <= now()
        ) OR (
            candidate.status = 'running'
            AND candidate.lease_expires_at < now()
            AND candidate.attempt < candidate.max_attempts
        )
    )
    AND NOT EXISTS (
        SELECT 1
        FROM learning_jobs AS active
        WHERE active.tenant_id = candidate.tenant_id
          AND active.project_id = candidate.project_id
          AND active.run_id = candidate.run_id
          AND active.status = 'running'
          AND active.lease_expires_at >= now()
          AND active.job_id <> candidate.job_id
    )
    AND NOT EXISTS (
        SELECT 1
        FROM learning_jobs AS earlier
        WHERE earlier.tenant_id = candidate.tenant_id
          AND earlier.project_id = candidate.project_id
          AND earlier.run_id = candidate.run_id
          AND earlier.status IN ('queued', 'retry_scheduled', 'running')
          AND (earlier.created_at, earlier.job_id)
              < (candidate.created_at, candidate.job_id)
    )
    ORDER BY candidate.available_at ASC, candidate.created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE learning_jobs AS job
SET status = 'running',
    attempt = job.attempt + 1,
    lease_owner = $1,
    lease_token = $3,
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
                    lease_token,
                )
        except UniqueViolationError:
            return None
        return _job_from_record(record) if record is not None else None

    async def renew_lease(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        lease_seconds: int,
    ) -> bool:
        result = await self._require_pool().execute(
            """
UPDATE learning_jobs
SET lease_expires_at = now() + ($4 * interval '1 second'), updated_at = now()
WHERE job_id = $1 AND status = 'running'
  AND lease_owner = $2 AND lease_token = $3
  AND lease_expires_at >= now()
""",
            job_id,
            worker_id,
            lease_token,
            lease_seconds,
        )
        return result == "UPDATE 1"

    async def save_checkpoint(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        checkpoint: LearningJobCheckpoint,
    ) -> LearningJob:
        transaction = current_postgres_learning_transaction(self._database)
        if transaction is not None:
            return await self._save_checkpoint_on_connection(
                transaction.connection,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                checkpoint=checkpoint,
            )
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            return await self._save_checkpoint_on_connection(
                connection,
                job_id,
                worker_id=worker_id,
                lease_token=lease_token,
                checkpoint=checkpoint,
            )

    async def commit_stage(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        operation: Callable[[], Awaitable[LearningJobCheckpoint]],
    ) -> LearningJob:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            with postgres_learning_transaction(
                PostgresLearningTransaction(
                    database=self._database,
                    connection=connection,
                )
            ):
                checkpoint = await operation()
                return await self._save_checkpoint_on_connection(
                    connection,
                    job_id,
                    worker_id=worker_id,
                    lease_token=lease_token,
                    checkpoint=checkpoint,
                )

    async def _save_checkpoint_on_connection(
        self,
        connection: Connection,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        checkpoint: LearningJobCheckpoint,
    ) -> LearningJob:
        current = await connection.fetchrow(
            """
SELECT *
FROM learning_jobs
WHERE job_id = $1
  AND status = 'running'
  AND lease_owner = $2
  AND lease_token = $3
  AND lease_expires_at >= now()
FOR UPDATE
""",
            job_id,
            worker_id,
            lease_token,
        )
        if current is None:
            raise LearningJobLeaseLostError(
                "The learning job lease is no longer owned by this execution"
            )
        job = _job_from_record(current)
        if checkpoint.reflection.run_id != job.run_id:
            raise ValueError("Learning checkpoint run does not match its job")
        if job.checkpoint is not None:
            if _checkpoint_semantics(job.checkpoint) != _checkpoint_semantics(
                checkpoint
            ):
                _validate_checkpoint_progression(job.checkpoint, checkpoint)
        elif checkpoint.stage != "reflection_completed":
            raise ValueError("The first learning checkpoint must contain reflection")
        await _write_artifact_links(connection, job_id, checkpoint)
        if (
            job.checkpoint is not None
            and _checkpoint_semantics(job.checkpoint)
            == _checkpoint_semantics(checkpoint)
        ):
            return job
        updated = await connection.fetchrow(
            """
UPDATE learning_jobs
SET checkpoint = $4::jsonb,
    checkpoint_updated_at = now(),
    reconciliation_status = 'pending',
    reconciliation_error = NULL,
    updated_at = now()
WHERE job_id = $1
  AND status = 'running'
  AND lease_owner = $2
  AND lease_token = $3
  AND lease_expires_at >= now()
RETURNING *
""",
                job_id,
                worker_id,
                lease_token,
                _encode_checkpoint(checkpoint),
            )
        return self._leased_result(updated)

    async def complete(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        result: LearningJobResult,
    ) -> LearningJob:
        pool = self._require_pool()
        async with pool.acquire() as connection, connection.transaction():
            current = await connection.fetchrow(
                """
SELECT *
FROM learning_jobs
WHERE job_id = $1 AND status = 'running'
  AND lease_owner = $2 AND lease_token = $3
  AND lease_expires_at >= now()
FOR UPDATE
""",
                job_id,
                worker_id,
                lease_token,
            )
            if current is None:
                raise LearningJobLeaseLostError(
                    "The learning job lease is no longer owned by this execution"
                )
            job = _job_from_record(current)
            reconciliation_status = "not_required"
            if job.checkpoint is not None:
                if job.checkpoint.stage not in {
                    "evolution_committed",
                    "harness_experience_committed",
                }:
                    raise ValueError(
                        "A durable learning job cannot complete before its final checkpoint"
                    )
                expected = _result_for_checkpoint(job.run_id, job.checkpoint)
                if result != expected:
                    raise ValueError(
                        "Learning job result does not match its final checkpoint"
                    )
                reconciliation_status = "verified"
            record = await connection.fetchrow(
                """
UPDATE learning_jobs
SET status = 'succeeded', result = $4::jsonb, can_retry = false,
    reconciliation_status = $5, reconciliation_error = NULL,
    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
    completed_at = now(), updated_at = now()
WHERE job_id = $1 AND status = 'running'
  AND lease_owner = $2 AND lease_token = $3
  AND lease_expires_at >= now()
RETURNING *
""",
                job_id,
                worker_id,
                lease_token,
                _encode_result(result),
                reconciliation_status,
            )
        return self._leased_result(record)

    async def fail(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: int,
    ) -> LearningJob:
        record = await self._require_pool().fetchrow(
            """
UPDATE learning_jobs
SET status = CASE
        WHEN $6 AND attempt < max_attempts THEN 'retry_scheduled'
        ELSE 'failed'
    END,
    available_at = CASE
        WHEN $6 AND attempt < max_attempts
        THEN now() + ($7 * interval '1 second')
        ELSE available_at
    END,
    can_retry = $6,
    error_code = $4,
    error_message = $5,
    lease_owner = NULL,
    lease_token = NULL,
    lease_expires_at = NULL,
    completed_at = CASE
        WHEN $6 AND attempt < max_attempts THEN NULL
        ELSE now()
    END,
    updated_at = now()
WHERE job_id = $1 AND status = 'running'
  AND lease_owner = $2 AND lease_token = $3
  AND lease_expires_at >= now()
RETURNING *
""",
            job_id,
            worker_id,
            lease_token,
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
    ) -> LearningJob | None:
        record = await self._require_pool().fetchrow(
            """
UPDATE learning_jobs
SET status = 'cancelled', can_retry = true,
    error_code = 'cancelled_by_user',
    error_message = 'The learning job was cancelled before execution.',
    completed_at = now(), updated_at = now()
WHERE job_id = $1 AND tenant_id = $2 AND project_id = $3
  AND status IN ('queued', 'retry_scheduled')
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
    ) -> LearningJob | None:
        record = await self._require_pool().fetchrow(
            """
UPDATE learning_jobs
SET status = 'queued', attempt = 0, available_at = now(),
    can_retry = false, error_code = NULL, error_message = NULL,
    reconciliation_status = CASE
        WHEN checkpoint IS NULL THEN 'not_required'
        ELSE 'pending'
    END,
    reconciliation_error = NULL,
    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
    result = NULL, started_at = NULL, completed_at = NULL, updated_at = now()
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
            raise LearningJobRepositoryError(
                "Postgres learning repository is not started"
            ) from exc

    @staticmethod
    def _leased_result(record: Record | None) -> LearningJob:
        if record is None:
            raise LearningJobLeaseLostError(
                "The learning job lease is no longer owned by this execution"
            )
        return _job_from_record(record)


def _job_from_record(record: Record) -> LearningJob:
    payload = dict(record)
    payload["trajectory"] = _decode_json_object(payload.get("trajectory"), "trajectory")
    raw_result = payload.get("result")
    payload["result"] = (
        _decode_json_object(raw_result, "result") if raw_result is not None else None
    )
    raw_checkpoint = payload.get("checkpoint")
    payload["checkpoint"] = (
        _decode_json_object(raw_checkpoint, "checkpoint")
        if raw_checkpoint is not None
        else None
    )
    payload.pop("checkpoint_updated_at", None)
    return LearningJob.model_validate(payload)


def _decode_json_object(value: object, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return cast(dict[str, Any], decoded)
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    raise LearningJobRepositoryError(f"Invalid JSON {field} from Postgres")


def _encode_result(result: LearningJobResult | None) -> str | None:
    if result is None:
        return None
    return json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
    )


def _encode_checkpoint(checkpoint: LearningJobCheckpoint | None) -> str | None:
    if checkpoint is None:
        return None
    return json.dumps(
        checkpoint.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _checkpoint_semantics(checkpoint: LearningJobCheckpoint) -> dict[str, Any]:
    return checkpoint.model_dump(mode="json", exclude={"updated_at"})


def _result_for_checkpoint(
    run_id: UUID,
    checkpoint: LearningJobCheckpoint,
) -> LearningJobResult:
    reflection = checkpoint.reflection
    return LearningJobResult(
        run_id=run_id,
        reflector_revision=reflection.reflector_revision,
        reflection_outcome=reflection.outcome,
        reflection_summary=reflection.summary,
        memory_ids=checkpoint.memory_ids,
        change_set_ids=checkpoint.change_set_ids,
        skill_candidate_id=checkpoint.skill_candidate_id,
        skill_candidate_version=checkpoint.skill_candidate_version,
        observation_ids=checkpoint.observation_ids,
        evaluation_id=checkpoint.evaluation_id,
        transition_ids=checkpoint.transition_ids,
        harness_experience_ids=checkpoint.harness_experience_ids,
        harness_evaluation_ids=checkpoint.harness_evaluation_ids,
    )


def _validate_checkpoint_progression(
    previous: LearningJobCheckpoint,
    current: LearningJobCheckpoint,
) -> None:
    stages = (
        "reflection_completed",
        "artifacts_committed",
        "observations_committed",
        "evolution_committed",
        "harness_experience_committed",
    )
    previous_index = stages.index(previous.stage)
    current_index = stages.index(current.stage)
    if current.revision != previous.revision:
        raise ValueError("Learning checkpoint revision cannot change during a job")
    if current_index != previous_index + 1:
        raise ValueError("Learning checkpoint stages must advance exactly once")
    if current.reflection != previous.reflection:
        raise ValueError("Learning reflection checkpoint is immutable")
    if not set(previous.memory_ids).issubset(current.memory_ids):
        raise ValueError("Learning checkpoint cannot remove memory artifacts")
    if not set(previous.change_set_ids).issubset(current.change_set_ids):
        raise ValueError("Learning checkpoint cannot remove change sets")
    if not set(previous.observation_ids).issubset(current.observation_ids):
        raise ValueError("Learning checkpoint cannot remove observations")
    if not set(previous.transition_ids).issubset(current.transition_ids):
        raise ValueError("Learning checkpoint cannot remove transitions")
    if not set(previous.harness_experience_ids).issubset(
        current.harness_experience_ids
    ):
        raise ValueError("Learning checkpoint cannot remove harness experiences")
    if not set(previous.harness_evaluation_ids).issubset(
        current.harness_evaluation_ids
    ):
        raise ValueError("Learning checkpoint cannot remove harness evaluations")
    if (
        previous.skill_candidate_id is not None
        and current.skill_candidate_id != previous.skill_candidate_id
    ):
        raise ValueError("Learning checkpoint cannot replace its skill candidate")
    if (
        previous.skill_candidate_version is not None
        and current.skill_candidate_version != previous.skill_candidate_version
    ):
        raise ValueError("Learning checkpoint cannot replace its skill candidate version")
    if previous.evaluation_id is not None and current.evaluation_id != previous.evaluation_id:
        raise ValueError("Learning checkpoint cannot replace its evaluation")


async def _write_artifact_links(
    connection: Connection,
    job_id: UUID,
    checkpoint: LearningJobCheckpoint,
) -> None:
    links = checkpoint_artifact_links(checkpoint)
    for stage_name, artifact_type, artifact_id, ordinal, artifact_version in links:
        await connection.execute(
            """
INSERT INTO learning_job_artifact_links (
    job_id, stage_name, artifact_type, artifact_id, ordinal, artifact_version
) VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (job_id, artifact_type, artifact_id) DO UPDATE SET
    artifact_version = COALESCE(
        learning_job_artifact_links.artifact_version,
        EXCLUDED.artifact_version
    )
""",
            job_id,
            stage_name,
            artifact_type,
            artifact_id,
            ordinal,
            artifact_version,
        )


def checkpoint_artifact_links(
    checkpoint: LearningJobCheckpoint,
) -> list[tuple[str, str, UUID, int, str | None]]:
    stages = (
        "reflection_completed",
        "artifacts_committed",
        "observations_committed",
        "evolution_committed",
        "harness_experience_committed",
    )
    stage_index = stages.index(checkpoint.stage)
    links: list[tuple[str, str, UUID, int, str | None]] = [
        (
            "reflection_completed",
            "reflection",
            checkpoint.reflection.artifact_id,
            0,
            None,
        )
    ]
    if stage_index >= 1:
        links.extend(
            ("artifacts_committed", "memory", artifact_id, ordinal, None)
            for ordinal, artifact_id in enumerate(checkpoint.memory_ids)
        )
        links.extend(
            ("artifacts_committed", "change_set", artifact_id, ordinal, None)
            for ordinal, artifact_id in enumerate(checkpoint.change_set_ids)
        )
        if checkpoint.skill_candidate_id is not None:
            links.append(
                (
                    "artifacts_committed",
                    "skill",
                    checkpoint.skill_candidate_id,
                    0,
                    checkpoint.skill_candidate_version,
                )
            )
    if stage_index >= 2:
        links.extend(
            ("observations_committed", "observation", artifact_id, ordinal, None)
            for ordinal, artifact_id in enumerate(checkpoint.observation_ids)
        )
    if stage_index >= 3:
        links.extend(
            ("evolution_committed", "transition", artifact_id, ordinal, None)
            for ordinal, artifact_id in enumerate(checkpoint.transition_ids)
        )
        if checkpoint.evaluation_id is not None:
            links.append(
                (
                    "evolution_committed",
                    "evaluation",
                    checkpoint.evaluation_id,
                    0,
                    None,
                )
            )
    if stage_index >= 4:
        links.extend(
            (
                "harness_experience_committed",
                "harness_experience",
                artifact_id,
                ordinal,
                None,
            )
            for ordinal, artifact_id in enumerate(checkpoint.harness_experience_ids)
        )
        links.extend(
            (
                "harness_experience_committed",
                "harness_evaluation",
                artifact_id,
                ordinal,
                None,
            )
            for ordinal, artifact_id in enumerate(checkpoint.harness_evaluation_ids)
        )
    return links


def _idempotency_lock_id(idempotency_key: str) -> int:
    digest = hashlib.sha256(idempotency_key.encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


__all__ = [
    "LEARNING_JOB_MIGRATIONS",
    "PostgresLearningJobRepository",
    "checkpoint_artifact_links",
]
