from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID

from asyncpg import Connection, Record

from app.harness.models import (
    HarnessExperienceEntry,
    HarnessExperienceEvaluation,
    HarnessPattern,
    HarnessPatternEvaluation,
    HarnessPatternPromotionEvidence,
    HarnessPatternStatus,
    HarnessPatternTransition,
    RunHarnessOverlay,
)
from app.harness.repository import HarnessExperienceConflictError
from app.infra.postgres import PostgresDatabase, PostgresMigration
from app.infra.postgres_learning_context import current_postgres_learning_transaction
from app.learning.execution import current_learning_fence
from app.learning.job_errors import LearningJobLeaseLostError

HARNESS_MIGRATIONS = (
    PostgresMigration(
        version=12,
        name="harness_experience_bank",
        statement="""
CREATE TABLE IF NOT EXISTS learning_harness_experiences (
    experience_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    user_id text NOT NULL,
    run_id uuid NOT NULL,
    task_fingerprint char(64) NOT NULL,
    primary_dimension text,
    success boolean NOT NULL,
    learnable boolean NOT NULL,
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS learning_harness_experiences_scope_idx
ON learning_harness_experiences (tenant_id, project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS learning_harness_experiences_run_idx
ON learning_harness_experiences (tenant_id, project_id, run_id);

CREATE INDEX IF NOT EXISTS learning_harness_experiences_fingerprint_idx
ON learning_harness_experiences (
    tenant_id, project_id, task_fingerprint, success, learnable
);

CREATE TABLE IF NOT EXISTS learning_harness_experience_evaluations (
    evaluation_id uuid PRIMARY KEY,
    experience_id uuid NOT NULL
        REFERENCES learning_harness_experiences(experience_id) ON DELETE RESTRICT,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    run_id uuid NOT NULL,
    signal_kind text NOT NULL CHECK (
        signal_kind IN ('run_outcome', 'explicit_feedback')
    ),
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS learning_harness_evaluations_experience_idx
ON learning_harness_experience_evaluations (
    tenant_id, project_id, experience_id, created_at
);

ALTER TABLE learning_job_artifact_links
DROP CONSTRAINT IF EXISTS learning_job_artifact_links_stage_name_check;

ALTER TABLE learning_job_artifact_links
ADD CONSTRAINT learning_job_artifact_links_stage_name_check CHECK (
    stage_name IN (
        'reflection_completed',
        'artifacts_committed',
        'observations_committed',
        'evolution_committed',
        'harness_experience_committed'
    )
);

ALTER TABLE learning_job_artifact_links
DROP CONSTRAINT IF EXISTS learning_job_artifact_links_artifact_type_check;

ALTER TABLE learning_job_artifact_links
ADD CONSTRAINT learning_job_artifact_links_artifact_type_check CHECK (
    artifact_type IN (
        'reflection',
        'memory',
        'change_set',
        'skill',
        'observation',
        'evaluation',
        'transition',
        'harness_experience',
        'harness_evaluation'
    )
);
""",
    ),
    PostgresMigration(
        version=13,
        name="harness_pattern_bank",
        statement="""
CREATE TABLE IF NOT EXISTS learning_harness_patterns (
    pattern_id uuid NOT NULL,
    version text NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    status text NOT NULL,
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (pattern_id, version)
);

CREATE INDEX IF NOT EXISTS learning_harness_patterns_scope_idx
ON learning_harness_patterns (
    tenant_id, project_id, status, created_at DESC
);

CREATE TABLE IF NOT EXISTS learning_harness_overlays (
    overlay_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    mode text NOT NULL,
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL,
    UNIQUE (tenant_id, project_id, run_id)
);

CREATE INDEX IF NOT EXISTS learning_harness_overlays_scope_idx
ON learning_harness_overlays (tenant_id, project_id, created_at DESC);
""",
    ),
    PostgresMigration(
        version=14,
        name="harness_pattern_governance",
        statement="""
CREATE TABLE IF NOT EXISTS learning_harness_pattern_evaluations (
    evaluation_id uuid PRIMARY KEY,
    pattern_id uuid NOT NULL,
    pattern_version text NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    generated_at timestamptz NOT NULL,
    FOREIGN KEY (pattern_id, pattern_version)
        REFERENCES learning_harness_patterns(pattern_id, version) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS learning_harness_pattern_evaluations_scope_idx
ON learning_harness_pattern_evaluations (
    tenant_id, project_id, pattern_id, pattern_version, generated_at
);

CREATE TABLE IF NOT EXISTS learning_harness_pattern_promotion_evidence (
    evidence_id uuid PRIMARY KEY,
    pattern_id uuid NOT NULL,
    pattern_version text NOT NULL,
    evaluation_id uuid NOT NULL
        REFERENCES learning_harness_pattern_evaluations(evaluation_id) ON DELETE RESTRICT,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    generated_at timestamptz NOT NULL,
    FOREIGN KEY (pattern_id, pattern_version)
        REFERENCES learning_harness_patterns(pattern_id, version) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS learning_harness_pattern_promotion_scope_idx
ON learning_harness_pattern_promotion_evidence (
    tenant_id, project_id, pattern_id, pattern_version, generated_at
);

CREATE TABLE IF NOT EXISTS learning_harness_pattern_transitions (
    transition_id uuid PRIMARY KEY,
    pattern_id uuid NOT NULL,
    pattern_version text NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    from_status text NOT NULL,
    to_status text NOT NULL,
    allowed boolean NOT NULL,
    applied boolean NOT NULL,
    evaluation_id uuid
        REFERENCES learning_harness_pattern_evaluations(evaluation_id) ON DELETE RESTRICT,
    promotion_evidence_id uuid
        REFERENCES learning_harness_pattern_promotion_evidence(evidence_id)
        ON DELETE RESTRICT,
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    decided_at timestamptz NOT NULL,
    FOREIGN KEY (pattern_id, pattern_version)
        REFERENCES learning_harness_patterns(pattern_id, version) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS learning_harness_pattern_transitions_scope_idx
ON learning_harness_pattern_transitions (
    tenant_id, project_id, pattern_id, pattern_version, decided_at
);

CREATE UNIQUE INDEX IF NOT EXISTS learning_harness_pattern_applied_from_idx
ON learning_harness_pattern_transitions (
    tenant_id, project_id, pattern_id, pattern_version, from_status
)
WHERE applied;
""",
    ),
)


class PostgresHarnessExperienceRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def save(self, experience: HarnessExperienceEntry) -> HarnessExperienceEntry:
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            existing = await connection.fetchrow(
                """
SELECT payload_hash, payload
FROM learning_harness_experiences
WHERE experience_id = $1
FOR UPDATE
""",
                experience.experience_id,
            )
            if existing is not None:
                _assert_same_hash(
                    existing,
                    experience.payload_hash,
                    kind="Experience",
                    identity=experience.experience_id,
                )
                return _experience_from_record(existing)
            await connection.execute(
                """
INSERT INTO learning_harness_experiences (
    experience_id, tenant_id, project_id, user_id, run_id,
    task_fingerprint, primary_dimension, success, learnable,
    payload, payload_hash, created_at
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9,
    $10::jsonb, $11, $12
)
""",
                experience.experience_id,
                experience.tenant_id,
                experience.project_id,
                experience.user_id,
                experience.run_id,
                experience.task_fingerprint,
                (
                    experience.diagnosis.primary_dimension.value
                    if experience.diagnosis.primary_dimension is not None
                    else None
                ),
                experience.diagnosis.success,
                experience.diagnosis.learnable,
                _encode(experience),
                experience.payload_hash,
                experience.created_at,
            )
            return experience

    async def get(
        self,
        experience_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> HarnessExperienceEntry | None:
        async with _read_connection(self._database) as connection:
            row = await connection.fetchrow(
                """
SELECT payload
FROM learning_harness_experiences
WHERE experience_id = $1 AND tenant_id = $2 AND project_id = $3
""",
                experience_id,
                tenant_id,
                project_id,
            )
        return _experience_from_record(row) if row is not None else None

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 100,
        learnable: bool | None = None,
        success: bool | None = None,
    ) -> Sequence[HarnessExperienceEntry]:
        _validate_limit(limit)
        async with _read_connection(self._database) as connection:
            rows = await connection.fetch(
                """
SELECT payload
FROM learning_harness_experiences
WHERE tenant_id = $1 AND project_id = $2
  AND ($3::boolean IS NULL OR learnable = $3)
  AND ($4::boolean IS NULL OR success = $4)
ORDER BY created_at DESC, experience_id DESC
LIMIT $5
""",
                tenant_id,
                project_id,
                learnable,
                success,
                limit,
            )
        return [_experience_from_record(row) for row in rows]

    async def list_for_run(
        self,
        run_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> Sequence[HarnessExperienceEntry]:
        async with _read_connection(self._database) as connection:
            rows = await connection.fetch(
                """
SELECT payload
FROM learning_harness_experiences
WHERE run_id = $1 AND tenant_id = $2 AND project_id = $3
ORDER BY created_at, experience_id
""",
                run_id,
                tenant_id,
                project_id,
            )
        return [_experience_from_record(row) for row in rows]

    async def save_evaluation(
        self,
        evaluation: HarnessExperienceEvaluation,
    ) -> HarnessExperienceEvaluation:
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            existing = await connection.fetchrow(
                """
SELECT payload_hash, payload
FROM learning_harness_experience_evaluations
WHERE evaluation_id = $1
FOR UPDATE
""",
                evaluation.evaluation_id,
            )
            if existing is not None:
                _assert_same_hash(
                    existing,
                    evaluation.payload_hash,
                    kind="Evaluation",
                    identity=evaluation.evaluation_id,
                )
                return _evaluation_from_record(existing)
            parent = await connection.fetchrow(
                """
SELECT tenant_id, project_id, run_id
FROM learning_harness_experiences
WHERE experience_id = $1
""",
                evaluation.experience_id,
            )
            if parent is None:
                raise ValueError("Harness evaluation references a missing experience")
            if (
                parent["tenant_id"] != evaluation.tenant_id
                or parent["project_id"] != evaluation.project_id
                or parent["run_id"] != evaluation.run_id
            ):
                raise ValueError("Harness evaluation scope does not match its experience")
            await connection.execute(
                """
INSERT INTO learning_harness_experience_evaluations (
    evaluation_id, experience_id, tenant_id, project_id, run_id,
    signal_kind, payload, payload_hash, created_at
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
""",
                evaluation.evaluation_id,
                evaluation.experience_id,
                evaluation.tenant_id,
                evaluation.project_id,
                evaluation.run_id,
                evaluation.signal_kind,
                _encode(evaluation),
                evaluation.payload_hash,
                evaluation.created_at,
            )
            return evaluation

    async def get_evaluation(
        self,
        evaluation_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> HarnessExperienceEvaluation | None:
        async with _read_connection(self._database) as connection:
            row = await connection.fetchrow(
                """
SELECT payload
FROM learning_harness_experience_evaluations
WHERE evaluation_id = $1 AND tenant_id = $2 AND project_id = $3
""",
                evaluation_id,
                tenant_id,
                project_id,
            )
        return _evaluation_from_record(row) if row is not None else None

    async def list_evaluations(
        self,
        experience_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> Sequence[HarnessExperienceEvaluation]:
        async with _read_connection(self._database) as connection:
            rows = await connection.fetch(
                """
SELECT payload
FROM learning_harness_experience_evaluations
WHERE experience_id = $1 AND tenant_id = $2 AND project_id = $3
ORDER BY
    created_at,
    CASE signal_kind
        WHEN 'run_outcome' THEN 0
        WHEN 'explicit_feedback' THEN 1
    END,
    evaluation_id
""",
                experience_id,
                tenant_id,
                project_id,
            )
        return [_evaluation_from_record(row) for row in rows]


class PostgresHarnessPolicyRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def save_pattern(self, pattern: HarnessPattern) -> HarnessPattern:
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            existing = await connection.fetchrow(
                """
SELECT payload_hash, payload
FROM learning_harness_patterns
WHERE pattern_id = $1 AND version = $2
FOR UPDATE
""",
                pattern.pattern_id,
                pattern.version,
            )
            if existing is not None:
                _assert_same_hash(
                    existing,
                    pattern.payload_hash,
                    kind="Pattern",
                    identity=pattern.pattern_id,
                )
                return _pattern_from_record(existing)
            await connection.execute(
                """
INSERT INTO learning_harness_patterns (
    pattern_id, version, tenant_id, project_id, status,
    payload, payload_hash, created_at
) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
""",
                pattern.pattern_id,
                pattern.version,
                pattern.tenant_id,
                pattern.project_id,
                pattern.status.value,
                _encode(pattern),
                pattern.payload_hash,
                pattern.created_at,
            )
            return pattern

    async def get_pattern(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        version: str | None = None,
    ) -> HarnessPattern | None:
        async with _read_connection(self._database) as connection:
            row = await connection.fetchrow(
                """
SELECT payload
FROM learning_harness_patterns
WHERE pattern_id = $1
  AND tenant_id = $2
  AND project_id = $3
  AND ($4::text IS NULL OR version = $4)
ORDER BY string_to_array(version, '.')::int[] DESC
LIMIT 1
""",
                pattern_id,
                tenant_id,
                project_id,
                version,
            )
        return _pattern_from_record(row) if row is not None else None

    async def list_patterns(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 100,
        status: HarnessPatternStatus | None = None,
    ) -> Sequence[HarnessPattern]:
        _validate_limit(limit)
        async with _read_connection(self._database) as connection:
            rows = await connection.fetch(
                """
SELECT payload
FROM learning_harness_patterns
WHERE tenant_id = $1
  AND project_id = $2
  AND ($3::text IS NULL OR status = $3)
ORDER BY created_at DESC, pattern_id, string_to_array(version, '.')::int[] DESC
LIMIT $4
""",
                tenant_id,
                project_id,
                status.value if status is not None else None,
                limit,
            )
        return [_pattern_from_record(row) for row in rows]

    async def save_pattern_evaluation(
        self,
        evaluation: HarnessPatternEvaluation,
    ) -> HarnessPatternEvaluation:
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            existing = await connection.fetchrow(
                """
SELECT payload_hash, payload
FROM learning_harness_pattern_evaluations
WHERE evaluation_id = $1
FOR UPDATE
""",
                evaluation.evaluation_id,
            )
            if existing is not None:
                _assert_same_hash(
                    existing,
                    evaluation.payload_hash,
                    kind="Pattern evaluation",
                    identity=evaluation.evaluation_id,
                )
                return _pattern_evaluation_from_record(existing)
            parent = await connection.fetchrow(
                """
SELECT tenant_id, project_id, payload_hash
FROM learning_harness_patterns
WHERE pattern_id = $1 AND version = $2
FOR UPDATE
""",
                evaluation.pattern_id,
                evaluation.pattern_version,
            )
            if parent is None:
                raise ValueError("Pattern evaluation references a missing pattern")
            if (
                parent["tenant_id"] != evaluation.tenant_id
                or parent["project_id"] != evaluation.project_id
                or parent["payload_hash"] != evaluation.pattern_payload_hash
            ):
                raise ValueError("Pattern evaluation scope or definition hash mismatch")
            await connection.execute(
                """
INSERT INTO learning_harness_pattern_evaluations (
    evaluation_id, pattern_id, pattern_version, tenant_id, project_id,
    payload, payload_hash, generated_at
) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
""",
                evaluation.evaluation_id,
                evaluation.pattern_id,
                evaluation.pattern_version,
                evaluation.tenant_id,
                evaluation.project_id,
                _encode(evaluation),
                evaluation.payload_hash,
                evaluation.generated_at,
            )
            return evaluation

    async def list_pattern_evaluations(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternEvaluation]:
        async with _read_connection(self._database) as connection:
            rows = await connection.fetch(
                """
SELECT payload
FROM learning_harness_pattern_evaluations
WHERE pattern_id = $1 AND tenant_id = $2 AND project_id = $3
  AND ($4::text IS NULL OR pattern_version = $4)
ORDER BY generated_at, evaluation_id
""",
                pattern_id,
                tenant_id,
                project_id,
                pattern_version,
            )
        return [_pattern_evaluation_from_record(row) for row in rows]

    async def save_pattern_promotion_evidence(
        self,
        evidence: HarnessPatternPromotionEvidence,
    ) -> HarnessPatternPromotionEvidence:
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            existing = await connection.fetchrow(
                """
SELECT payload_hash, payload
FROM learning_harness_pattern_promotion_evidence
WHERE evidence_id = $1
FOR UPDATE
""",
                evidence.evidence_id,
            )
            if existing is not None:
                _assert_same_hash(
                    existing,
                    evidence.payload_hash,
                    kind="Pattern promotion evidence",
                    identity=evidence.evidence_id,
                )
                return _promotion_evidence_from_record(existing)
            parents = await connection.fetchrow(
                """
SELECT
    p.tenant_id AS pattern_tenant_id,
    p.project_id AS pattern_project_id,
    e.pattern_id AS evaluation_pattern_id,
    e.pattern_version AS evaluation_pattern_version,
    e.tenant_id AS evaluation_tenant_id,
    e.project_id AS evaluation_project_id,
    e.payload_hash AS evaluation_payload_hash
FROM learning_harness_patterns p
JOIN learning_harness_pattern_evaluations e
  ON e.evaluation_id = $3
WHERE p.pattern_id = $1 AND p.version = $2
FOR UPDATE OF p, e
""",
                evidence.pattern_id,
                evidence.pattern_version,
                evidence.evaluation_id,
            )
            if parents is None:
                raise ValueError(
                    "Pattern promotion evidence references a missing parent"
                )
            if (
                parents["pattern_tenant_id"] != evidence.tenant_id
                or parents["pattern_project_id"] != evidence.project_id
                or parents["evaluation_pattern_id"] != evidence.pattern_id
                or parents["evaluation_pattern_version"] != evidence.pattern_version
                or parents["evaluation_tenant_id"] != evidence.tenant_id
                or parents["evaluation_project_id"] != evidence.project_id
                or parents["evaluation_payload_hash"]
                != evidence.evaluation_payload_hash
            ):
                raise ValueError("Pattern promotion evidence scope or hash mismatch")
            await connection.execute(
                """
INSERT INTO learning_harness_pattern_promotion_evidence (
    evidence_id, pattern_id, pattern_version, evaluation_id,
    tenant_id, project_id, payload, payload_hash, generated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
""",
                evidence.evidence_id,
                evidence.pattern_id,
                evidence.pattern_version,
                evidence.evaluation_id,
                evidence.tenant_id,
                evidence.project_id,
                _encode(evidence),
                evidence.payload_hash,
                evidence.generated_at,
            )
            return evidence

    async def list_pattern_promotion_evidence(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternPromotionEvidence]:
        async with _read_connection(self._database) as connection:
            rows = await connection.fetch(
                """
SELECT payload
FROM learning_harness_pattern_promotion_evidence
WHERE pattern_id = $1 AND tenant_id = $2 AND project_id = $3
  AND ($4::text IS NULL OR pattern_version = $4)
ORDER BY generated_at, evidence_id
""",
                pattern_id,
                tenant_id,
                project_id,
                pattern_version,
            )
        return [_promotion_evidence_from_record(row) for row in rows]

    async def save_pattern_transition(
        self,
        transition: HarnessPatternTransition,
    ) -> HarnessPatternTransition:
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            existing = await connection.fetchrow(
                """
SELECT payload_hash, payload
FROM learning_harness_pattern_transitions
WHERE transition_id = $1
FOR UPDATE
""",
                transition.transition_id,
            )
            if existing is not None:
                _assert_same_hash(
                    existing,
                    transition.payload_hash,
                    kind="Pattern transition",
                    identity=transition.transition_id,
                )
                return _pattern_transition_from_record(existing)
            pattern = await connection.fetchrow(
                """
SELECT tenant_id, project_id, status
FROM learning_harness_patterns
WHERE pattern_id = $1 AND version = $2
FOR UPDATE
""",
                transition.pattern_id,
                transition.pattern_version,
            )
            if pattern is None:
                raise ValueError("Pattern transition references a missing pattern")
            if (
                pattern["tenant_id"] != transition.tenant_id
                or pattern["project_id"] != transition.project_id
            ):
                raise ValueError("Pattern transition scope mismatch")
            if transition.evaluation_id is not None:
                evaluation = await connection.fetchrow(
                    """
SELECT pattern_id, pattern_version, tenant_id, project_id, payload_hash
FROM learning_harness_pattern_evaluations
WHERE evaluation_id = $1
""",
                    transition.evaluation_id,
                )
                if evaluation is None or (
                    evaluation["pattern_id"] != transition.pattern_id
                    or evaluation["pattern_version"] != transition.pattern_version
                    or evaluation["tenant_id"] != transition.tenant_id
                    or evaluation["project_id"] != transition.project_id
                    or evaluation["payload_hash"]
                    != transition.evaluation_payload_hash
                ):
                    raise ValueError("Pattern transition evaluation mismatch")
            if transition.promotion_evidence_id is not None:
                evidence = await connection.fetchrow(
                    """
SELECT pattern_id, pattern_version, tenant_id, project_id, payload_hash
FROM learning_harness_pattern_promotion_evidence
WHERE evidence_id = $1
""",
                    transition.promotion_evidence_id,
                )
                if evidence is None or (
                    evidence["pattern_id"] != transition.pattern_id
                    or evidence["pattern_version"] != transition.pattern_version
                    or evidence["tenant_id"] != transition.tenant_id
                    or evidence["project_id"] != transition.project_id
                    or evidence["payload_hash"]
                    != transition.promotion_evidence_payload_hash
                ):
                    raise ValueError(
                        "Pattern transition promotion evidence mismatch"
                    )
            if transition.applied:
                latest = await connection.fetchrow(
                    """
SELECT to_status
FROM learning_harness_pattern_transitions
WHERE pattern_id = $1 AND pattern_version = $2
  AND tenant_id = $3 AND project_id = $4 AND applied
ORDER BY decided_at DESC, transition_id DESC
LIMIT 1
""",
                    transition.pattern_id,
                    transition.pattern_version,
                    transition.tenant_id,
                    transition.project_id,
                )
                current = (
                    HarnessPatternStatus(cast(str, latest["to_status"]))
                    if latest is not None
                    else HarnessPatternStatus(cast(str, pattern["status"]))
                )
                if current != transition.from_status:
                    raise HarnessExperienceConflictError(
                        "Pattern transition is stale for the current effective status"
                    )
            await connection.execute(
                """
INSERT INTO learning_harness_pattern_transitions (
    transition_id, pattern_id, pattern_version, tenant_id, project_id,
    from_status, to_status, allowed, applied,
    evaluation_id, promotion_evidence_id,
    payload, payload_hash, decided_at
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9,
    $10, $11,
    $12::jsonb, $13, $14
)
""",
                transition.transition_id,
                transition.pattern_id,
                transition.pattern_version,
                transition.tenant_id,
                transition.project_id,
                transition.from_status.value,
                transition.to_status.value,
                transition.allowed,
                transition.applied,
                transition.evaluation_id,
                transition.promotion_evidence_id,
                _encode(transition),
                transition.payload_hash,
                transition.decided_at,
            )
            return transition

    async def list_pattern_transitions(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternTransition]:
        async with _read_connection(self._database) as connection:
            rows = await connection.fetch(
                """
SELECT payload
FROM learning_harness_pattern_transitions
WHERE pattern_id = $1 AND tenant_id = $2 AND project_id = $3
  AND ($4::text IS NULL OR pattern_version = $4)
ORDER BY decided_at, transition_id
""",
                pattern_id,
                tenant_id,
                project_id,
                pattern_version,
            )
        return [_pattern_transition_from_record(row) for row in rows]

    async def save_overlay(self, overlay: RunHarnessOverlay) -> RunHarnessOverlay:
        async with _write_connection(self._database) as connection:
            existing = await connection.fetchrow(
                """
SELECT payload_hash, payload
FROM learning_harness_overlays
WHERE tenant_id = $1 AND project_id = $2 AND run_id = $3
FOR UPDATE
""",
                overlay.tenant_id,
                overlay.project_id,
                overlay.run_id,
            )
            if existing is not None:
                _assert_same_hash(
                    existing,
                    overlay.payload_hash,
                    kind="Overlay",
                    identity=overlay.overlay_id,
                )
                return _overlay_from_record(existing)
            await connection.execute(
                """
INSERT INTO learning_harness_overlays (
    overlay_id, run_id, tenant_id, project_id, mode,
    payload, payload_hash, created_at
) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
""",
                overlay.overlay_id,
                overlay.run_id,
                overlay.tenant_id,
                overlay.project_id,
                overlay.mode.value,
                _encode(overlay),
                overlay.payload_hash,
                overlay.created_at,
            )
            return overlay

    async def get_overlay(
        self,
        run_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> RunHarnessOverlay | None:
        async with _read_connection(self._database) as connection:
            row = await connection.fetchrow(
                """
SELECT payload
FROM learning_harness_overlays
WHERE run_id = $1 AND tenant_id = $2 AND project_id = $3
""",
                run_id,
                tenant_id,
                project_id,
            )
        return _overlay_from_record(row) if row is not None else None


def _assert_same_hash(
    row: Record,
    payload_hash: str,
    *,
    kind: str,
    identity: UUID,
) -> None:
    if cast(str, row["payload_hash"]) != payload_hash:
        raise HarnessExperienceConflictError(
            f"{kind} {identity} already contains different content"
        )


def _experience_from_record(row: Record) -> HarnessExperienceEntry:
    return HarnessExperienceEntry.model_validate(_decode(row["payload"]))


def _evaluation_from_record(row: Record) -> HarnessExperienceEvaluation:
    return HarnessExperienceEvaluation.model_validate(_decode(row["payload"]))


def _pattern_from_record(row: Record) -> HarnessPattern:
    return HarnessPattern.model_validate(_decode(row["payload"]))


def _pattern_evaluation_from_record(row: Record) -> HarnessPatternEvaluation:
    return HarnessPatternEvaluation.model_validate(_decode(row["payload"]))


def _promotion_evidence_from_record(
    row: Record,
) -> HarnessPatternPromotionEvidence:
    return HarnessPatternPromotionEvidence.model_validate(_decode(row["payload"]))


def _pattern_transition_from_record(row: Record) -> HarnessPatternTransition:
    return HarnessPatternTransition.model_validate(_decode(row["payload"]))


def _overlay_from_record(row: Record) -> RunHarnessOverlay:
    return RunHarnessOverlay.model_validate(_decode(row["payload"]))


def _decode(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return cast(dict[str, Any], decoded)
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    raise RuntimeError("Invalid harness artifact JSON from Postgres")


def _encode(value: object) -> str:
    payload = cast(Any, value).model_dump(mode="json")
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _assert_execution_fence(connection: Connection) -> None:
    fence = current_learning_fence()
    if fence is None:
        return
    row = await connection.fetchrow(
        """
SELECT 1
FROM learning_jobs
WHERE job_id = $1
  AND status = 'running'
  AND lease_owner = $2
  AND lease_token = $3
  AND lease_expires_at >= now()
""",
        fence.job_id,
        fence.worker_id,
        fence.lease_token,
    )
    if row is None:
        raise LearningJobLeaseLostError(
            "The learning job lease is no longer valid for harness writes"
        )


@asynccontextmanager
async def _read_connection(
    database: PostgresDatabase,
) -> AsyncIterator[Connection]:
    transaction = current_postgres_learning_transaction(database)
    if transaction is not None:
        yield transaction.connection
        return
    async with database.pool.acquire() as connection:
        yield connection


@asynccontextmanager
async def _write_connection(
    database: PostgresDatabase,
) -> AsyncIterator[Connection]:
    transaction = current_postgres_learning_transaction(database)
    if transaction is not None:
        yield transaction.connection
        return
    async with database.pool.acquire() as connection, connection.transaction():
        yield connection


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")


__all__ = [
    "HARNESS_MIGRATIONS",
    "PostgresHarnessExperienceRepository",
    "PostgresHarnessPolicyRepository",
]
