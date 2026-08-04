from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, TypeVar, cast
from uuid import UUID

from asyncpg import Connection, Pool, Record
from pydantic import BaseModel

from app.domain.enums import SkillStatus
from app.domain.models import (
    LearningChangeSet,
    MemoryCandidate,
    MemoryRecord,
    SkillDefinition,
    SkillEvaluation,
    SkillObservation,
    SkillTransitionEvent,
    utc_now,
)
from app.infra.postgres import PostgresDatabase, PostgresDatabaseError, PostgresMigration
from app.infra.postgres_learning_context import (
    current_postgres_learning_transaction,
)
from app.learning.execution import current_learning_fence
from app.learning.job_errors import LearningJobLeaseLostError


class LearningArtifactConflictError(ValueError):
    """Raised when a stable artifact identity is reused for different content."""


ModelT = TypeVar("ModelT", bound=BaseModel)


LEARNING_ARTIFACT_MIGRATIONS = (
    PostgresMigration(
        version=6,
        name="learning_artifact_audit_store",
        statement="""
CREATE TABLE IF NOT EXISTS learning_memories (
    memory_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    user_id text,
    memory_type text NOT NULL,
    logical_key text NOT NULL,
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    expires_at timestamptz,
    revoked_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS learning_memories_logical_identity_idx
ON learning_memories (
    tenant_id, project_id, COALESCE(user_id, ''), memory_type, logical_key
);

CREATE INDEX IF NOT EXISTS learning_memories_scope_idx
ON learning_memories (tenant_id, project_id, user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS learning_skills (
    skill_id uuid NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    name text NOT NULL,
    version text NOT NULL,
    status text NOT NULL,
    payload jsonb NOT NULL,
    definition_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (skill_id, version),
    UNIQUE (tenant_id, project_id, name, version)
);

CREATE INDEX IF NOT EXISTS learning_skills_scope_status_idx
ON learning_skills (tenant_id, project_id, status, name, version);

CREATE TABLE IF NOT EXISTS learning_skill_evaluations (
    evaluation_id uuid PRIMARY KEY,
    skill_id uuid NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    skill_version text NOT NULL,
    evaluator_revision text NOT NULL,
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    generated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS learning_skill_evaluations_skill_idx
ON learning_skill_evaluations (
    tenant_id, project_id, skill_id, generated_at, evaluation_id
);

CREATE TABLE IF NOT EXISTS learning_skill_observations (
    observation_id uuid PRIMARY KEY,
    skill_id uuid NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    skill_version text NOT NULL,
    evaluator_revision text NOT NULL,
    run_id uuid NOT NULL,
    cohort text NOT NULL CHECK (cohort IN ('shadow', 'canary', 'active')),
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS learning_skill_observations_skill_idx
ON learning_skill_observations (
    tenant_id, project_id, skill_id, skill_version, cohort, created_at
);

CREATE TABLE IF NOT EXISTS learning_change_sets (
    change_set_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    source_run_ids uuid[] NOT NULL,
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS learning_change_sets_scope_idx
ON learning_change_sets (tenant_id, project_id, created_at, change_set_id);

CREATE INDEX IF NOT EXISTS learning_change_sets_source_runs_idx
ON learning_change_sets USING gin (source_run_ids);

CREATE TABLE IF NOT EXISTS learning_artifact_imports (
    import_key text PRIMARY KEY,
    counts jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);
""",
    ),
    PostgresMigration(
        version=8,
        name="skill_transition_ledger",
        statement="""
CREATE TABLE IF NOT EXISTS learning_skill_transitions (
    transition_id uuid PRIMARY KEY,
    skill_id uuid NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    skill_version text NOT NULL,
    transition_type text NOT NULL CHECK (
        transition_type IN ('promotion', 'rollback', 'health_gate')
    ),
    from_status text NOT NULL,
    to_status text NOT NULL,
    allowed boolean NOT NULL,
    applied boolean NOT NULL,
    evaluation_id uuid,
    learning_job_id uuid,
    payload jsonb NOT NULL,
    payload_hash char(64) NOT NULL,
    decided_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS learning_skill_transitions_skill_idx
ON learning_skill_transitions (
    tenant_id, project_id, skill_id, decided_at, transition_id
);

CREATE INDEX IF NOT EXISTS learning_skill_transitions_job_idx
ON learning_skill_transitions (learning_job_id)
WHERE learning_job_id IS NOT NULL;
""",
    ),
)


class PostgresLearningArtifactRepository:
    """One transactional Postgres adapter for all governed learning artifacts."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def upsert(self, candidate: MemoryCandidate) -> MemoryRecord:
        identity = (
            f"{candidate.tenant_id}\x00{candidate.project_id}\x00"
            f"{candidate.user_id or ''}\x00{candidate.memory_type.value}\x00{candidate.key}"
        )
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            await connection.execute(
                "SELECT pg_advisory_xact_lock($1)",
                _lock_id(f"memory:{identity}"),
            )
            row = await connection.fetchrow(
                """
SELECT payload
FROM learning_memories
WHERE tenant_id = $1 AND project_id = $2
  AND user_id IS NOT DISTINCT FROM $3
  AND memory_type = $4 AND logical_key = $5
FOR UPDATE
""",
                candidate.tenant_id,
                candidate.project_id,
                candidate.user_id,
                candidate.memory_type.value,
                candidate.key,
            )
            existing = _model_from_record(row, MemoryRecord) if row is not None else None
            now = utc_now()
            if existing is not None and existing.revoked_at is not None:
                return existing
            if existing is not None and _memory_candidate_matches(existing, candidate):
                return existing
            values = {
                **candidate.model_dump(),
                "created_at": existing.created_at if existing is not None else now,
                "updated_at": now,
                "revoked_at": existing.revoked_at if existing is not None else None,
            }
            if existing is not None:
                values["memory_id"] = existing.memory_id
            record = MemoryRecord.model_validate(values)
            await _write_memory(connection, record)
        return record

    async def search(
        self,
        query: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = "local-user",
        limit: int = 10,
    ) -> Sequence[MemoryRecord]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []
        records = list(
            await self.list_scoped(
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
            )
        )
        now = datetime.now(UTC)
        records = [record for record in records if not _memory_expired(record, now)]
        query_tokens = _tokens(query)

        def rank(record: MemoryRecord) -> tuple[float, float, datetime, str]:
            searchable = " ".join(
                [record.key, record.summary, json.dumps(record.detail, ensure_ascii=False)]
            )
            record_tokens = _tokens(searchable)
            overlap = len(query_tokens & record_tokens) / len(query_tokens) if query_tokens else 0.0
            exact = 1.0 if query and query.casefold() in searchable.casefold() else 0.0
            return overlap, exact, record.updated_at, str(record.memory_id)

        if query_tokens or query.strip():
            records = [
                record
                for record in records
                if rank(record)[0] > 0.0 or rank(record)[1] > 0.0
            ]
        records.sort(key=rank, reverse=True)
        return records[:limit]

    async def revoke(
        self,
        memory_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = "local-user",
    ) -> bool:
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            row = await connection.fetchrow(
                """
SELECT payload
FROM learning_memories
WHERE memory_id = $1 AND tenant_id = $2 AND project_id = $3
  AND (user_id IS NULL OR user_id = $4)
FOR UPDATE
""",
                memory_id,
                tenant_id,
                project_id,
                user_id,
            )
            if row is None:
                return False
            record = _model_from_record(row, MemoryRecord)
            if record.revoked_at is not None:
                return False
            now = utc_now()
            await _write_memory(
                connection,
                record.model_copy(update={"revoked_at": now, "updated_at": now}),
            )
        return True

    async def list_all(
        self,
        *,
        include_revoked: bool = False,
    ) -> Sequence[MemoryRecord]:
        where = "" if include_revoked else "WHERE revoked_at IS NULL"
        rows = await self._fetch(
            f"""
SELECT payload FROM learning_memories
{where}
ORDER BY created_at, memory_id
"""
        )
        return [_model_from_record(row, MemoryRecord) for row in rows]

    async def list_scoped(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = "local-user",
        include_revoked: bool = False,
    ) -> Sequence[MemoryRecord]:
        rows = await self._fetch(
            """
SELECT payload
FROM learning_memories
WHERE tenant_id = $1 AND project_id = $2
  AND (user_id IS NULL OR user_id = $3)
  AND ($4 OR revoked_at IS NULL)
ORDER BY updated_at DESC, memory_id
""",
            tenant_id,
            project_id,
            user_id,
            include_revoked,
        )
        return [_model_from_record(row, MemoryRecord) for row in rows]

    async def list_by_status(
        self,
        status: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> Sequence[SkillDefinition]:
        requested = SkillStatus(status)
        rows = await self._fetch(
            """
SELECT payload
FROM learning_skills
WHERE tenant_id = $1 AND project_id = $2 AND status = $3
ORDER BY name, version
""",
            tenant_id,
            project_id,
            requested.value,
        )
        return sorted(
            [_model_from_record(row, SkillDefinition) for row in rows],
            key=lambda skill: (skill.name, _version_key(skill.version)),
        )

    async def get(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        version: str | None = None,
    ) -> SkillDefinition | None:
        rows = await self._fetch(
            """
SELECT payload
FROM learning_skills
WHERE skill_id = $1 AND tenant_id = $2 AND project_id = $3
  AND ($4::text IS NULL OR version = $4)
""",
            skill_id,
            tenant_id,
            project_id,
            version,
        )
        skills = [_model_from_record(row, SkillDefinition) for row in rows]
        return max(skills, key=lambda item: _version_key(item.version)) if skills else None

    async def get_by_name(
        self,
        name: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        version: str | None = None,
    ) -> SkillDefinition | None:
        rows = await self._fetch(
            """
SELECT payload
FROM learning_skills
WHERE name = $1 AND tenant_id = $2 AND project_id = $3
  AND ($4::text IS NULL OR version = $4)
""",
            name,
            tenant_id,
            project_id,
            version,
        )
        skills = [_model_from_record(row, SkillDefinition) for row in rows]
        return max(skills, key=lambda item: _version_key(item.version)) if skills else None

    async def list_all_skills(self) -> Sequence[SkillDefinition]:
        rows = await self._fetch(
            "SELECT payload FROM learning_skills ORDER BY name, version"
        )
        return sorted(
            [_model_from_record(row, SkillDefinition) for row in rows],
            key=lambda skill: (skill.name, _version_key(skill.version)),
        )

    async def _list_evaluations(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillEvaluation]:
        rows = await self._fetch(
            """
SELECT payload
FROM learning_skill_evaluations
WHERE skill_id = $1 AND tenant_id = $2 AND project_id = $3
  AND ($4::text IS NULL OR skill_version = $4)
ORDER BY generated_at, evaluation_id
""",
            skill_id,
            tenant_id,
            project_id,
            skill_version,
        )
        return [_model_from_record(row, SkillEvaluation) for row in rows]

    async def _list_observations(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
        cohort: str | None = None,
    ) -> Sequence[SkillObservation]:
        rows = await self._fetch(
            """
SELECT payload
FROM learning_skill_observations
WHERE skill_id = $1 AND tenant_id = $2 AND project_id = $3
  AND ($4::text IS NULL OR skill_version = $4)
  AND ($5::text IS NULL OR cohort = $5)
ORDER BY created_at, observation_id
""",
            skill_id,
            tenant_id,
            project_id,
            skill_version,
            cohort,
        )
        return [_model_from_record(row, SkillObservation) for row in rows]

    async def latest(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> SkillEvaluation | None:
        row = await self._fetchrow(
            """
SELECT payload
FROM learning_skill_evaluations
WHERE skill_id = $1 AND tenant_id = $2 AND project_id = $3
  AND ($4::text IS NULL OR skill_version = $4)
ORDER BY generated_at DESC, evaluation_id DESC
LIMIT 1
""",
            skill_id,
            tenant_id,
            project_id,
            skill_version,
        )
        return _model_from_record(row, SkillEvaluation) if row is not None else None

    async def list_for_run(self, run_id: UUID) -> Sequence[LearningChangeSet]:
        rows = await self._fetch(
            """
SELECT payload
FROM learning_change_sets
WHERE $1 = ANY(source_run_ids)
ORDER BY created_at, change_set_id
""",
            run_id,
        )
        return [_model_from_record(row, LearningChangeSet) for row in rows]

    async def list_all_change_sets(self) -> Sequence[LearningChangeSet]:
        rows = await self._fetch(
            "SELECT payload FROM learning_change_sets ORDER BY created_at, change_set_id"
        )
        return [_model_from_record(row, LearningChangeSet) for row in rows]

    async def _list_transitions(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillTransitionEvent]:
        rows = await self._fetch(
            """
SELECT payload
FROM learning_skill_transitions
WHERE skill_id = $1 AND tenant_id = $2 AND project_id = $3
  AND ($4::text IS NULL OR skill_version = $4)
ORDER BY decided_at, transition_id
""",
            skill_id,
            tenant_id,
            project_id,
            skill_version,
        )
        return [_model_from_record(row, SkillTransitionEvent) for row in rows]

    async def import_legacy(
        self,
        *,
        import_key: str,
        memories: Sequence[MemoryRecord],
        skills: Sequence[SkillDefinition],
        evaluations: Sequence[SkillEvaluation],
        observations: Sequence[SkillObservation],
        transitions: Sequence[SkillTransitionEvent],
        change_sets: Sequence[LearningChangeSet],
    ) -> dict[str, int]:
        async with _write_connection(self._database) as connection:
            await connection.execute(
                "SELECT pg_advisory_xact_lock($1)",
                _lock_id(f"learning-import:{import_key}"),
            )
            existing_row = await connection.fetchrow(
                "SELECT counts FROM learning_artifact_imports WHERE import_key = $1",
                import_key,
            )
            if existing_row is not None:
                return cast(dict[str, int], _decode_json(existing_row["counts"]))
            counts = {
                "memories": 0,
                "skills": 0,
                "evaluations": 0,
                "observations": 0,
                "transitions": 0,
                "change_sets": 0,
            }
            for memory in memories:
                counts["memories"] += await _insert_legacy_memory(connection, memory)
            for skill in skills:
                counts["skills"] += await _insert_legacy_skill(connection, skill)
            for evaluation in evaluations:
                counts["evaluations"] += await _insert_legacy_evaluation(
                    connection,
                    evaluation,
                )
            for observation in observations:
                counts["observations"] += await _insert_legacy_observation(
                    connection,
                    observation,
                )
            for transition in transitions:
                counts["transitions"] += await _insert_legacy_transition(
                    connection,
                    transition,
                )
            for change_set in change_sets:
                counts["change_sets"] += await _insert_legacy_change_set(
                    connection,
                    change_set,
                )
            await connection.execute(
                """
INSERT INTO learning_artifact_imports (import_key, counts)
VALUES ($1, $2::jsonb)
""",
                import_key,
                _encode(counts),
            )
        return counts

    async def _save_skill(self, skill: SkillDefinition) -> SkillDefinition:
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            await connection.execute(
                "SELECT pg_advisory_xact_lock($1)",
                _lock_id(
                    f"skill:{skill.tenant_id}:{skill.project_id}:"
                    f"{skill.name}:{skill.version}"
                ),
            )
            existing_row = await connection.fetchrow(
                """
SELECT skill_id, definition_hash
FROM learning_skills
WHERE tenant_id = $1 AND project_id = $2 AND name = $3 AND version = $4
FOR UPDATE
""",
                skill.tenant_id,
                skill.project_id,
                skill.name,
                skill.version,
            )
            if existing_row is not None and existing_row["skill_id"] != skill.skill_id:
                raise LearningArtifactConflictError(
                    f"Skill {skill.name}@{skill.version} already has a different identity"
                )
            definition_hash = _skill_definition_hash(skill)
            if (
                existing_row is not None
                and existing_row["definition_hash"] != definition_hash
            ):
                raise LearningArtifactConflictError(
                    f"Skill {skill.name}@{skill.version} immutable definition changed"
                )
            await connection.execute(
                """
INSERT INTO learning_skills (
    skill_id, tenant_id, project_id, name, version, status,
    payload, definition_hash, created_at, updated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, now())
ON CONFLICT (tenant_id, project_id, name, version)
DO UPDATE SET status = EXCLUDED.status, payload = EXCLUDED.payload, updated_at = now()
""",
                skill.skill_id,
                skill.tenant_id,
                skill.project_id,
                skill.name,
                skill.version,
                skill.status.value,
                _encode_model(skill),
                definition_hash,
                skill.created_at,
            )
        return skill

    async def _save_evaluation(self, evaluation: SkillEvaluation) -> SkillEvaluation:
        payload_hash = _immutable_model_hash(evaluation, exclude={"generated_at"})
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            await _assert_immutable_hash(
                connection,
                table="learning_skill_evaluations",
                identity_column="evaluation_id",
                identity=evaluation.evaluation_id,
                payload_hash=payload_hash,
            )
            await connection.execute(
                """
INSERT INTO learning_skill_evaluations (
    evaluation_id, skill_id, tenant_id, project_id, skill_version,
    evaluator_revision, payload, payload_hash, generated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
ON CONFLICT (evaluation_id) DO NOTHING
""",
                evaluation.evaluation_id,
                evaluation.skill_id,
                evaluation.tenant_id,
                evaluation.project_id,
                evaluation.skill_version,
                evaluation.evaluator_revision,
                _encode_model(evaluation),
                payload_hash,
                evaluation.generated_at,
            )
        return evaluation

    async def _save_observation(self, observation: SkillObservation) -> SkillObservation:
        payload_hash = _immutable_model_hash(observation, exclude={"created_at"})
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            await _assert_immutable_hash(
                connection,
                table="learning_skill_observations",
                identity_column="observation_id",
                identity=observation.observation_id,
                payload_hash=payload_hash,
            )
            await connection.execute(
                """
INSERT INTO learning_skill_observations (
    observation_id, skill_id, tenant_id, project_id, skill_version,
    evaluator_revision, run_id, cohort, payload, payload_hash, created_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
ON CONFLICT (observation_id) DO NOTHING
""",
                observation.observation_id,
                observation.skill_id,
                observation.tenant_id,
                observation.project_id,
                observation.skill_version,
                observation.evaluator_revision,
                observation.run_id,
                observation.cohort,
                _encode_model(observation),
                payload_hash,
                observation.created_at,
            )
        return observation

    async def _save_change_set(self, change_set: LearningChangeSet) -> LearningChangeSet:
        tenant_id = change_set.scope.get("tenant_id", "local")
        project_id = change_set.scope.get("project_id", "default")
        payload_hash = _immutable_model_hash(change_set, exclude={"created_at"})
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            await _assert_immutable_hash(
                connection,
                table="learning_change_sets",
                identity_column="change_set_id",
                identity=change_set.change_set_id,
                payload_hash=payload_hash,
            )
            await connection.execute(
                """
INSERT INTO learning_change_sets (
    change_set_id, tenant_id, project_id, target_type, target_id,
    source_run_ids, payload, payload_hash, created_at
) VALUES ($1, $2, $3, $4, $5, $6::uuid[], $7::jsonb, $8, $9)
ON CONFLICT (change_set_id) DO NOTHING
""",
                change_set.change_set_id,
                tenant_id,
                project_id,
                change_set.target_type,
                change_set.target_id,
                list(change_set.source_run_ids),
                _encode_model(change_set),
                payload_hash,
                change_set.created_at,
            )
        return change_set

    async def _save_transition(
        self,
        transition: SkillTransitionEvent,
    ) -> SkillTransitionEvent:
        payload_hash = _immutable_model_hash(transition, exclude={"decided_at"})
        async with _write_connection(self._database) as connection:
            await _assert_execution_fence(connection)
            await _assert_immutable_hash(
                connection,
                table="learning_skill_transitions",
                identity_column="transition_id",
                identity=transition.transition_id,
                payload_hash=payload_hash,
            )
            await connection.execute(
                """
INSERT INTO learning_skill_transitions (
    transition_id, skill_id, tenant_id, project_id, skill_version,
    transition_type, from_status, to_status, allowed, applied,
    evaluation_id, learning_job_id, payload, payload_hash, decided_at
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10,
    $11, $12, $13::jsonb, $14, $15
)
ON CONFLICT (transition_id) DO NOTHING
""",
                transition.transition_id,
                transition.skill_id,
                transition.tenant_id,
                transition.project_id,
                transition.skill_version,
                transition.transition_type,
                transition.from_status.value,
                transition.to_status.value,
                transition.allowed,
                transition.applied,
                transition.evaluation_id,
                transition.learning_job_id,
                _encode_model(transition),
                payload_hash,
                transition.decided_at,
            )
        return transition

    def _require_pool(self) -> Pool:
        try:
            return self._database.pool
        except PostgresDatabaseError as exc:
            raise RuntimeError("Postgres learning artifact repository is not started") from exc

    async def _fetch(self, query: str, *args: object) -> list[Record]:
        async with _read_connection(self._database) as connection:
            return list(await connection.fetch(query, *args))

    async def _fetchrow(self, query: str, *args: object) -> Record | None:
        async with _read_connection(self._database) as connection:
            return await connection.fetchrow(query, *args)


class PostgresSkillRepository:
    def __init__(self, store: PostgresLearningArtifactRepository) -> None:
        self._store = store

    async def save(self, skill: SkillDefinition) -> SkillDefinition:
        return await self._store._save_skill(skill)

    async def list_by_status(
        self,
        status: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> Sequence[SkillDefinition]:
        return await self._store.list_by_status(
            status,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def get(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        version: str | None = None,
    ) -> SkillDefinition | None:
        return await self._store.get(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            version=version,
        )

    async def get_by_name(
        self,
        name: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        version: str | None = None,
    ) -> SkillDefinition | None:
        return await self._store.get_by_name(
            name,
            tenant_id=tenant_id,
            project_id=project_id,
            version=version,
        )

    async def list_all(self) -> Sequence[SkillDefinition]:
        return await self._store.list_all_skills()


class PostgresSkillEvaluationRepository:
    def __init__(self, store: PostgresLearningArtifactRepository) -> None:
        self._store = store

    async def save(self, evaluation: SkillEvaluation) -> SkillEvaluation:
        return await self._store._save_evaluation(evaluation)

    async def list_for_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillEvaluation]:
        return await self._store._list_evaluations(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )

    async def latest(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> SkillEvaluation | None:
        return await self._store.latest(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )

    async def list_all(self) -> Sequence[SkillEvaluation]:
        rows = await self._store._fetch(
            """
SELECT payload
FROM learning_skill_evaluations
ORDER BY generated_at, evaluation_id
"""
        )
        return [_model_from_record(row, SkillEvaluation) for row in rows]


class PostgresSkillObservationRepository:
    def __init__(self, store: PostgresLearningArtifactRepository) -> None:
        self._store = store

    async def save(self, observation: SkillObservation) -> SkillObservation:
        return await self._store._save_observation(observation)

    async def list_for_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
        cohort: str | None = None,
    ) -> Sequence[SkillObservation]:
        return await self._store._list_observations(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
            cohort=cohort,
        )

    async def list_all(self) -> Sequence[SkillObservation]:
        rows = await self._store._fetch(
            """
SELECT payload
FROM learning_skill_observations
ORDER BY created_at, observation_id
"""
        )
        return [_model_from_record(row, SkillObservation) for row in rows]


class PostgresLearningChangeSetRepository:
    def __init__(self, store: PostgresLearningArtifactRepository) -> None:
        self._store = store

    async def save(self, change_set: LearningChangeSet) -> LearningChangeSet:
        return await self._store._save_change_set(change_set)

    async def list_for_run(self, run_id: UUID) -> Sequence[LearningChangeSet]:
        return await self._store.list_for_run(run_id)

    async def list_all(self) -> Sequence[LearningChangeSet]:
        return await self._store.list_all_change_sets()


class PostgresSkillTransitionRepository:
    def __init__(self, store: PostgresLearningArtifactRepository) -> None:
        self._store = store

    async def save(
        self,
        transition: SkillTransitionEvent,
    ) -> SkillTransitionEvent:
        return await self._store._save_transition(transition)

    async def list_for_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillTransitionEvent]:
        return await self._store._list_transitions(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )

    async def list_all(self) -> Sequence[SkillTransitionEvent]:
        rows = await self._store._fetch(
            """
SELECT payload
FROM learning_skill_transitions
ORDER BY decided_at, transition_id
"""
        )
        return [_model_from_record(row, SkillTransitionEvent) for row in rows]


async def _write_memory(connection: Connection, record: MemoryRecord) -> None:
    await connection.execute(
        """
INSERT INTO learning_memories (
    memory_id, tenant_id, project_id, user_id, memory_type, logical_key,
    payload, payload_hash, created_at, updated_at, expires_at, revoked_at
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12)
ON CONFLICT (memory_id) DO UPDATE SET
    payload = EXCLUDED.payload,
    payload_hash = EXCLUDED.payload_hash,
    updated_at = EXCLUDED.updated_at,
    expires_at = EXCLUDED.expires_at,
    revoked_at = EXCLUDED.revoked_at
""",
        record.memory_id,
        record.tenant_id,
        record.project_id,
        record.user_id,
        record.memory_type.value,
        record.key,
        _encode_model(record),
        _model_hash(record),
        record.created_at,
        record.updated_at,
        record.expires_at,
        record.revoked_at,
    )


async def _insert_legacy_memory(connection: Connection, item: MemoryRecord) -> int:
    result = await connection.execute(
        """
INSERT INTO learning_memories (
    memory_id, tenant_id, project_id, user_id, memory_type, logical_key,
    payload, payload_hash, created_at, updated_at, expires_at, revoked_at
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12)
ON CONFLICT DO NOTHING
""",
        item.memory_id,
        item.tenant_id,
        item.project_id,
        item.user_id,
        item.memory_type.value,
        item.key,
        _encode_model(item),
        _immutable_model_hash(item, exclude={"generated_at"}),
        item.created_at,
        item.updated_at,
        item.expires_at,
        item.revoked_at,
    )
    return int(result.endswith("1"))


async def _insert_legacy_skill(connection: Connection, item: SkillDefinition) -> int:
    result = await connection.execute(
        """
INSERT INTO learning_skills (
    skill_id, tenant_id, project_id, name, version, status,
    payload, definition_hash, created_at, updated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, now())
ON CONFLICT DO NOTHING
""",
        item.skill_id,
        item.tenant_id,
        item.project_id,
        item.name,
        item.version,
        item.status.value,
        _encode_model(item),
        _skill_definition_hash(item),
        item.created_at,
    )
    return int(result.endswith("1"))


async def _insert_legacy_evaluation(connection: Connection, item: SkillEvaluation) -> int:
    result = await connection.execute(
        """
INSERT INTO learning_skill_evaluations (
    evaluation_id, skill_id, tenant_id, project_id, skill_version,
    evaluator_revision, payload, payload_hash, generated_at
) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
ON CONFLICT DO NOTHING
""",
        item.evaluation_id,
        item.skill_id,
        item.tenant_id,
        item.project_id,
        item.skill_version,
        item.evaluator_revision,
        _encode_model(item),
        _immutable_model_hash(item, exclude={"created_at"}),
        item.generated_at,
    )
    return int(result.endswith("1"))


async def _insert_legacy_observation(connection: Connection, item: SkillObservation) -> int:
    result = await connection.execute(
        """
INSERT INTO learning_skill_observations (
    observation_id, skill_id, tenant_id, project_id, skill_version,
    evaluator_revision, run_id, cohort, payload, payload_hash, created_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
ON CONFLICT DO NOTHING
""",
        item.observation_id,
        item.skill_id,
        item.tenant_id,
        item.project_id,
        item.skill_version,
        item.evaluator_revision,
        item.run_id,
        item.cohort,
        _encode_model(item),
        _immutable_model_hash(item, exclude={"created_at"}),
        item.created_at,
    )
    return int(result.endswith("1"))


async def _insert_legacy_change_set(connection: Connection, item: LearningChangeSet) -> int:
    result = await connection.execute(
        """
INSERT INTO learning_change_sets (
    change_set_id, tenant_id, project_id, target_type, target_id,
    source_run_ids, payload, payload_hash, created_at
) VALUES ($1, $2, $3, $4, $5, $6::uuid[], $7::jsonb, $8, $9)
ON CONFLICT DO NOTHING
""",
        item.change_set_id,
        item.scope.get("tenant_id", "local"),
        item.scope.get("project_id", "default"),
        item.target_type,
        item.target_id,
        list(item.source_run_ids),
        _encode_model(item),
        _model_hash(item),
        item.created_at,
    )
    return int(result.endswith("1"))


async def _insert_legacy_transition(
    connection: Connection,
    item: SkillTransitionEvent,
) -> int:
    result = await connection.execute(
        """
INSERT INTO learning_skill_transitions (
    transition_id, skill_id, tenant_id, project_id, skill_version,
    transition_type, from_status, to_status, allowed, applied,
    evaluation_id, learning_job_id, payload, payload_hash, decided_at
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9, $10,
    $11, $12, $13::jsonb, $14, $15
)
ON CONFLICT DO NOTHING
""",
        item.transition_id,
        item.skill_id,
        item.tenant_id,
        item.project_id,
        item.skill_version,
        item.transition_type,
        item.from_status.value,
        item.to_status.value,
        item.allowed,
        item.applied,
        item.evaluation_id,
        item.learning_job_id,
        _encode_model(item),
        _immutable_model_hash(item, exclude={"decided_at"}),
        item.decided_at,
    )
    return int(result.endswith("1"))


def _model_from_record(record: Record, model: type[ModelT]) -> ModelT:
    return model.model_validate(_decode_json(record["payload"]))


def _decode_json(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return cast(dict[str, Any], decoded)
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    raise RuntimeError("Invalid learning artifact JSON from Postgres")


def _encode_model(value: Any) -> str:
    return _encode(value.model_dump(mode="json"))


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _model_hash(value: Any) -> str:
    return hashlib.sha256(_encode(value.model_dump(mode="json")).encode("utf-8")).hexdigest()


def _immutable_model_hash(value: Any, *, exclude: set[str]) -> str:
    payload = value.model_dump(mode="json", exclude=exclude)
    return hashlib.sha256(_encode(payload).encode("utf-8")).hexdigest()


def _skill_definition_hash(skill: SkillDefinition) -> str:
    payload = skill.model_dump(mode="json", exclude={"status", "created_at"})
    return hashlib.sha256(_encode(payload).encode("utf-8")).hexdigest()


def _memory_candidate_matches(
    existing: MemoryRecord,
    candidate: MemoryCandidate,
) -> bool:
    existing_candidate = {
        field: getattr(existing, field)
        for field in MemoryCandidate.model_fields
    }
    return MemoryCandidate.model_validate(existing_candidate) == candidate


async def _assert_execution_fence(connection: Connection) -> None:
    fence = current_learning_fence()
    if fence is None:
        return
    record = await connection.fetchrow(
        """
SELECT job_id
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
    if record is None:
        raise LearningJobLeaseLostError(
            "The learning job lease is no longer valid for artifact writes"
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


async def _assert_immutable_hash(
    connection: Connection,
    *,
    table: str,
    identity_column: str,
    identity: UUID,
    payload_hash: str,
) -> None:
    allowed = {
        ("learning_skill_evaluations", "evaluation_id"),
        ("learning_skill_observations", "observation_id"),
        ("learning_change_sets", "change_set_id"),
        ("learning_skill_transitions", "transition_id"),
    }
    if (table, identity_column) not in allowed:
        raise RuntimeError("Unsupported immutable learning artifact table")
    row = await connection.fetchrow(
        f"""
SELECT payload_hash
FROM {table}
WHERE {identity_column} = $1
FOR UPDATE
""",
        identity,
    )
    if row is not None and row["payload_hash"] != payload_hash:
        raise LearningArtifactConflictError(
            f"{table} identity {identity} already contains different content"
        )


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[\w-]+", text, flags=re.UNICODE)}


def _memory_expired(record: MemoryRecord, now: datetime) -> bool:
    expires_at = record.expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


def _version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def _lock_id(value: str) -> int:
    digest = hashlib.sha256(value.encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


__all__ = [
    "LEARNING_ARTIFACT_MIGRATIONS",
    "LearningArtifactConflictError",
    "PostgresLearningArtifactRepository",
    "PostgresLearningChangeSetRepository",
    "PostgresSkillEvaluationRepository",
    "PostgresSkillObservationRepository",
    "PostgresSkillRepository",
    "PostgresSkillTransitionRepository",
]
