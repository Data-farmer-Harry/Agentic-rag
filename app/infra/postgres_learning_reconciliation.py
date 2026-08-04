from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from asyncpg import Connection, Record

from app.domain.models import (
    LearningJobCheckpoint,
    LearningJobResult,
)
from app.infra.postgres import PostgresDatabase
from app.infra.postgres_learning_jobs import checkpoint_artifact_links

_ARTIFACT_LOCATIONS = {
    "memory": ("learning_memories", "memory_id"),
    "change_set": ("learning_change_sets", "change_set_id"),
    "skill": ("learning_skills", "skill_id"),
    "observation": ("learning_skill_observations", "observation_id"),
    "evaluation": ("learning_skill_evaluations", "evaluation_id"),
    "transition": ("learning_skill_transitions", "transition_id"),
    "harness_experience": (
        "learning_harness_experiences",
        "experience_id",
    ),
    "harness_evaluation": (
        "learning_harness_experience_evaluations",
        "evaluation_id",
    ),
}


@dataclass(frozen=True, slots=True)
class LearningReconciliationIssue:
    job_id: UUID
    error: str


@dataclass(frozen=True, slots=True)
class LearningReconciliationReport:
    inspected: int
    verified: int
    required: int
    links_repaired: int
    issues: tuple[LearningReconciliationIssue, ...]


class PostgresLearningReconciler:
    """Repairs derived links and verifies checkpoint, artifact, and result consistency."""

    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def reconcile(
        self,
        *,
        limit: int = 100,
        tenant_id: str | None = None,
        project_id: str | None = None,
        include_verified: bool = False,
    ) -> LearningReconciliationReport:
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        rows = await self._database.pool.fetch(
            """
SELECT job_id
FROM learning_jobs
WHERE checkpoint IS NOT NULL
  AND status <> 'running'
  AND ($1::text IS NULL OR tenant_id = $1)
  AND ($2::text IS NULL OR project_id = $2)
  AND ($3 OR reconciliation_status <> 'verified')
ORDER BY updated_at, job_id
LIMIT $4
""",
            tenant_id,
            project_id,
            include_verified,
            limit,
        )
        inspected = 0
        verified = 0
        required = 0
        links_repaired = 0
        issues: list[LearningReconciliationIssue] = []
        for candidate in rows:
            outcome = await self._reconcile_job(cast(UUID, candidate["job_id"]))
            if outcome is None:
                continue
            inspected += 1
            job_verified, repaired, error = outcome
            links_repaired += repaired
            if job_verified:
                verified += 1
            else:
                required += 1
                issues.append(
                    LearningReconciliationIssue(
                        job_id=cast(UUID, candidate["job_id"]),
                        error=error or "Unknown reconciliation error",
                    )
                )
        return LearningReconciliationReport(
            inspected=inspected,
            verified=verified,
            required=required,
            links_repaired=links_repaired,
            issues=tuple(issues),
        )

    async def _reconcile_job(
        self,
        job_id: UUID,
    ) -> tuple[bool, int, str | None] | None:
        async with self._database.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
SELECT *
FROM learning_jobs
WHERE job_id = $1
  AND checkpoint IS NOT NULL
  AND status <> 'running'
FOR UPDATE SKIP LOCKED
""",
                job_id,
            )
            if row is None:
                return None
            errors: list[str] = []
            repaired = 0
            try:
                checkpoint = LearningJobCheckpoint.model_validate(
                    _decode_json_object(row["checkpoint"], "checkpoint")
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                errors.append(f"invalid checkpoint: {exc}")
                checkpoint = None

            if checkpoint is not None:
                expected_links = checkpoint_artifact_links(checkpoint)
                existing_rows = await connection.fetch(
                    """
SELECT stage_name, artifact_type, artifact_id, ordinal, artifact_version
FROM learning_job_artifact_links
WHERE job_id = $1
""",
                    job_id,
                )
                expected = set(expected_links)
                expected_versions = {
                    item[:4]: item[4]
                    for item in expected_links
                    if item[4] is not None
                }
                existing: set[tuple[str, str, UUID, int, str | None]] = set()
                for link in existing_rows:
                    identity = (
                        cast(str, link["stage_name"]),
                        cast(str, link["artifact_type"]),
                        cast(UUID, link["artifact_id"]),
                        cast(int, link["ordinal"]),
                    )
                    artifact_version = cast(str | None, link["artifact_version"])
                    expected_version = expected_versions.get(identity)
                    if artifact_version is None and expected_version is not None:
                        await connection.execute(
                            """
UPDATE learning_job_artifact_links
SET artifact_version = $3
WHERE job_id = $1 AND artifact_type = $2 AND artifact_id = $4
  AND artifact_version IS NULL
""",
                            job_id,
                            identity[1],
                            expected_version,
                            identity[2],
                        )
                        artifact_version = expected_version
                        repaired += 1
                    existing.add((*identity, artifact_version))
                unexpected = existing - expected
                if unexpected:
                    errors.append(
                        f"{len(unexpected)} unexpected or conflicting artifact link(s)"
                    )
                else:
                    missing = expected - existing
                    for (
                        stage_name,
                        artifact_type,
                        artifact_id,
                        ordinal,
                        artifact_version,
                    ) in sorted(
                        missing,
                        key=lambda item: (item[0], item[1], item[3], str(item[2])),
                    ):
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
                    repaired += len(missing)

                missing_artifacts = await _missing_artifacts(
                    connection,
                    row,
                    expected_links,
                )
                if missing_artifacts:
                    errors.append(
                        "missing artifacts: "
                        + ", ".join(
                            f"{artifact_type}:{artifact_id}"
                            + (f"@{artifact_version}" if artifact_version else "")
                            for artifact_type, artifact_id, artifact_version in missing_artifacts
                        )
                    )
                _validate_result_consistency(row, checkpoint, errors)

            error = "; ".join(errors)[:1_000] if errors else None
            status = "required" if errors else "verified"
            await connection.execute(
                """
UPDATE learning_jobs
SET reconciliation_status = $2,
    reconciliation_error = $3,
    updated_at = now()
WHERE job_id = $1
""",
                job_id,
                status,
                error,
            )
            return not errors, repaired, error


async def _missing_artifacts(
    connection: Connection,
    job: Record,
    links: list[tuple[str, str, UUID, int, str | None]],
) -> list[tuple[str, UUID, str | None]]:
    missing: list[tuple[str, UUID, str | None]] = []
    for _, artifact_type, artifact_id, _, artifact_version in links:
        if artifact_type == "reflection":
            continue
        table, identity_column = _ARTIFACT_LOCATIONS[artifact_type]
        extra = ""
        args: list[object] = [
            artifact_id,
            cast(str, job["tenant_id"]),
            cast(str, job["project_id"]),
        ]
        if artifact_type == "skill" and artifact_version is not None:
            extra = " AND version = $4"
            args.append(artifact_version)
        elif artifact_type == "transition":
            extra = " AND learning_job_id = $4"
            args.append(cast(UUID, job["job_id"]))
        record = await connection.fetchrow(
            f"""
SELECT 1
FROM {table}
WHERE {identity_column} = $1
  AND tenant_id = $2
  AND project_id = $3
  {extra}
LIMIT 1
""",
            *args,
        )
        if record is None:
            missing.append((artifact_type, artifact_id, artifact_version))
    return missing


def _validate_result_consistency(
    job: Record,
    checkpoint: LearningJobCheckpoint,
    errors: list[str],
) -> None:
    raw_result = job["result"]
    status = cast(str, job["status"])
    if status != "succeeded":
        if raw_result is not None:
            errors.append("non-succeeded job contains a result")
        return
    if checkpoint.stage not in {
        "evolution_committed",
        "harness_experience_committed",
    }:
        errors.append("succeeded job does not have a final checkpoint")
        return
    if raw_result is None:
        errors.append("succeeded job has no result")
        return
    try:
        actual = LearningJobResult.model_validate(
            _decode_json_object(raw_result, "result")
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid result: {exc}")
        return
    expected = expected_learning_result(
        run_id=cast(UUID, job["run_id"]),
        checkpoint=checkpoint,
    )
    if actual != expected:
        errors.append("result does not match the final checkpoint")


def expected_learning_result(
    *,
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


def _decode_json_object(value: object, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return cast(dict[str, Any], decoded)
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    raise ValueError(f"Invalid JSON {field} from Postgres")


__all__ = [
    "LearningReconciliationIssue",
    "LearningReconciliationReport",
    "PostgresLearningReconciler",
    "expected_learning_result",
]
