from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.enums import (
    LearningJobStatus,
    MemoryType,
    OutboxEventStatus,
    RunStatus,
    SkillStatus,
    TrustLevel,
)
from app.domain.models import (
    AnswerResponse,
    Claim,
    IngestionJob,
    KnowledgeSource,
    LearningJob,
    LearningJobCheckpoint,
    LearningJobResult,
    LearningReflectionArtifact,
    LearningTrajectoryEvaluation,
    MemoryCandidate,
    OutboxEvent,
    Provenance,
    RunContext,
    RunTrajectory,
    SkillDefinition,
    SkillEvaluation,
    SkillStep,
    SkillTransitionEvent,
)
from app.harness.evaluation import DeterministicPatternEvaluator
from app.harness.evolution import HarnessPatternEvolutionService
from app.harness.experience import HarnessExperienceService
from app.harness.models import (
    HarnessConfigDelta,
    HarnessDimension,
    HarnessOutputConfig,
    HarnessOverlayMode,
    HarnessPattern,
    HarnessPatternStatus,
    HarnessReasonCode,
    HarnessToolConfig,
    HarnessTriggerPredicate,
    RunHarnessOverlay,
    canonical_hash,
)
from app.harness.repository import HarnessExperienceConflictError
from app.infra.local_repositories import JsonlTrajectoryRepository
from app.infra.postgres import (
    PostgresDatabase,
    PostgresMigration,
    PostgresMigrationError,
)
from app.infra.postgres_harness import (
    HARNESS_MIGRATIONS,
    PostgresHarnessExperienceRepository,
    PostgresHarnessPolicyRepository,
)
from app.infra.postgres_ingestion_jobs import (
    INGESTION_JOB_MIGRATIONS,
    PostgresIngestionJobRepository,
)
from app.infra.postgres_knowledge import (
    KNOWLEDGE_MIGRATIONS,
    PostgresKnowledgeRepository,
)
from app.infra.postgres_learning_artifacts import (
    LEARNING_ARTIFACT_MIGRATIONS,
    LearningArtifactConflictError,
    PostgresLearningArtifactRepository,
    PostgresSkillEvaluationRepository,
    PostgresSkillRepository,
    PostgresSkillTransitionRepository,
)
from app.infra.postgres_learning_jobs import (
    LEARNING_JOB_MIGRATIONS,
    PostgresLearningJobRepository,
)
from app.infra.postgres_learning_reconciliation import (
    PostgresLearningReconciler,
    expected_learning_result,
)
from app.infra.postgres_outbox import (
    PostgresOutboxRepository,
    insert_outbox_event,
)
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.store import FileKnowledgeObjectStore
from app.learning.execution import LearningExecutionFence, learning_execution
from app.learning.job_errors import LearningJobLeaseLostError
from app.memory.json_store import JsonMemoryStore
from app.personal.models import TaskCreate, TaskPatch, TaskStatus
from app.personal.postgres import (
    PERSONAL_CONTROL_MIGRATIONS,
    PostgresPersonalRepository,
)
from app.personal.service import PersonalControlService

_TEST_DSN = os.getenv("HERMESGRAPH_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not _TEST_DSN,
    reason="HERMESGRAPH_TEST_POSTGRES_DSN is not configured",
)


@pytest.fixture
async def postgres_scope(
    tmp_path: Path,
) -> AsyncIterator[tuple[PostgresDatabase, str, Path]]:
    assert _TEST_DSN is not None
    database = PostgresDatabase(_TEST_DSN, min_pool_size=1, max_pool_size=3)
    await database.start()
    await database.migrate(
        (
            *INGESTION_JOB_MIGRATIONS,
            *KNOWLEDGE_MIGRATIONS,
            *LEARNING_JOB_MIGRATIONS,
            *LEARNING_ARTIFACT_MIGRATIONS,
            *PERSONAL_CONTROL_MIGRATIONS,
            *HARNESS_MIGRATIONS,
        )
    )
    tenant_id = f"contract-{uuid4().hex}"
    try:
        yield database, tenant_id, tmp_path / "objects"
    finally:
        async with database.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "DELETE FROM learning_harness_pattern_transitions "
                "WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_harness_pattern_promotion_evidence "
                "WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_harness_pattern_evaluations "
                "WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_harness_overlays WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_harness_patterns WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_harness_experience_evaluations "
                "WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_harness_experiences WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM personal_events WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM personal_records WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_change_sets WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_skill_transitions WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_skill_observations WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_skill_evaluations WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_skills WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_memories WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM outbox_events WHERE payload ->> 'tenant_id' = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM ingestion_jobs WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM learning_jobs WHERE tenant_id = $1",
                tenant_id,
            )
            await connection.execute(
                "DELETE FROM knowledge_documents WHERE tenant_id = $1",
                tenant_id,
            )
        await database.close()


@pytest.mark.asyncio
async def test_migrations_are_idempotent_and_checksum_guarded(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, _, _ = postgres_scope
    migrations = (
        *INGESTION_JOB_MIGRATIONS,
        *KNOWLEDGE_MIGRATIONS,
        *LEARNING_JOB_MIGRATIONS,
        *LEARNING_ARTIFACT_MIGRATIONS,
        *PERSONAL_CONTROL_MIGRATIONS,
        *HARNESS_MIGRATIONS,
    )

    await database.migrate(migrations)
    rows = await database.pool.fetch(
        "SELECT version, name, checksum FROM hermesgraph_schema_migrations "
        "WHERE version = ANY($1::integer[]) ORDER BY version",
        [migration.version for migration in migrations],
    )
    assert [(row["version"], row["name"]) for row in rows] == [
        (migration.version, migration.name)
        for migration in sorted(migrations, key=lambda item: item.version)
    ]
    assert all(row["checksum"] for row in rows)

    with pytest.raises(PostgresMigrationError, match="checksum"):
        await database.migrate(
            (
                PostgresMigration(
                    version=KNOWLEDGE_MIGRATIONS[0].version,
                    name="tampered",
                    statement="SELECT 1",
                ),
            )
        )


@pytest.mark.asyncio
async def test_postgres_harness_experience_is_immutable_scoped_and_feedback_linked(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    repository = PostgresHarnessExperienceRepository(database)
    service = HarnessExperienceService(repository)
    trajectory = RunTrajectory(
        context=RunContext(
            tenant_id=tenant_id,
            project_id="harness",
            user_id="contract-user",
        ),
        user_input="Remember the successful graph retrieval strategy.",
        status=RunStatus.COMPLETED,
        answer=AnswerResponse(answer_markdown="The graph retrieval succeeded."),
        completed_at=datetime.now(UTC),
    )

    first = await service.collect(trajectory, trigger="run_completed")
    duplicate = await service.collect(trajectory, trigger="run_completed")
    feedback = await service.collect(
        trajectory.model_copy(
            update={
                "feedback_score": -0.75,
                "feedback_text": "The answer was too broad.",
            }
        ),
        trigger="feedback_received",
    )

    assert first.experience_created is True
    assert duplicate.experience_created is False
    assert duplicate.evaluation_created is False
    assert feedback.experience.experience_id == first.experience.experience_id
    assert feedback.evaluation.experience_id == first.experience.experience_id
    assert feedback.evaluation.signal_kind == "explicit_feedback"
    assert await repository.get(
        first.experience.experience_id,
        tenant_id=tenant_id,
        project_id="other-project",
    ) is None
    evaluations = await repository.list_evaluations(
        first.experience.experience_id,
        tenant_id=tenant_id,
        project_id="harness",
    )
    assert [item.signal_kind for item in evaluations] == [
        "run_outcome",
        "explicit_feedback",
    ]
    with pytest.raises(HarnessExperienceConflictError):
        await repository.save(
            first.experience.model_copy(update={"payload_hash": "0" * 64})
        )


@pytest.mark.asyncio
async def test_postgres_harness_pattern_and_overlay_are_versioned_and_frozen(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    repository = PostgresHarnessPolicyRepository(database)
    pattern_id = uuid4()
    pattern_payload = {
        "pattern_id": pattern_id,
        "version": "0.1.0",
        "parent_version": None,
        "tenant_id": tenant_id,
        "project_id": "harness-policy",
        "name": "Require complete citation coverage",
        "trigger_predicate": HarnessTriggerPredicate(
            domain_pack="research_reference",
            primary_intent="research",
            required_reason_codes=["citation_coverage_below_threshold"],
        ),
        "dimensions": [HarnessDimension.OUTPUT],
        "proposed_delta": HarnessConfigDelta(
            output=HarnessOutputConfig(
                minimum_citation_coverage=0.9,
                claim_support_mode="supported",
            )
        ),
        "supporting_experience_ids": [],
        "contradicting_experience_ids": [],
        "support_count": 3,
        "failure_count": 3,
        "estimated_quality_lift": 0.0,
        "confidence": 1.0,
        "status": HarnessPatternStatus.DRAFT,
        "miner_revision": "contract-miner-v1",
        "evaluator_revision": "pending",
        "created_at": datetime.now(UTC),
    }
    pattern = HarnessPattern.model_validate(
        {**pattern_payload, "payload_hash": canonical_hash(pattern_payload)}
    )
    stored = await repository.save_pattern(pattern)
    duplicate = await repository.save_pattern(pattern)

    assert stored == duplicate
    assert await repository.get_pattern(
        pattern_id,
        tenant_id=tenant_id,
        project_id="other-project",
    ) is None
    overlay_payload = {
        "overlay_id": uuid4(),
        "run_id": uuid4(),
        "tenant_id": tenant_id,
        "project_id": "harness-policy",
        "baseline_policy_versions": {"harness": "baseline-v1"},
        "selected_pattern_versions": [],
        "positive_experience_ids": [],
        "negative_experience_ids": [],
        "effective_delta": HarnessConfigDelta(),
        "clamped_fields": [],
        "rejected_conflicts": [],
        "selection_trace_codes": ["mode:observe"],
        "selector_revision": "contract-selector-v1",
        "experience_bank_revision": "experience-v1",
        "pattern_bank_revision": "pattern-v1",
        "mode": HarnessOverlayMode.OBSERVE,
        "created_at": datetime.now(UTC),
        "expires_at": None,
    }
    overlay = RunHarnessOverlay.model_validate(
        {**overlay_payload, "payload_hash": canonical_hash(overlay_payload)}
    )
    await repository.save_overlay(overlay)

    assert await repository.get_overlay(
        overlay.run_id,
        tenant_id=tenant_id,
        project_id="harness-policy",
    ) == overlay


@pytest.mark.asyncio
async def test_postgres_harness_pattern_governance_is_immutable_and_staged(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    experiences = PostgresHarnessExperienceRepository(database)
    policies = PostgresHarnessPolicyRepository(database)
    experience_service = HarnessExperienceService(experiences)
    support_ids = []
    for index in range(3):
        result = await experience_service.collect(
            RunTrajectory(
                context=RunContext(
                    tenant_id=tenant_id,
                    project_id="harness-governance",
                    domain_pack="research_reference",
                    session_id=f"governance-{index}",
                ),
                user_input="查找 GraphRAG 社区之间的证据路径",
                status=RunStatus.COMPLETED,
                answer=AnswerResponse(
                    answer_markdown="No graph follow-up.",
                    claims=[Claim(text="The relationship is unresolved")],
                ),
                tags=["graph_followup_missing"],
                completed_at=datetime(2026, 5, index + 1, tzinfo=UTC),
            ),
            trigger="run_completed",
        )
        support_ids.append(result.experience.experience_id)
    pattern_id = uuid4()
    pattern_payload = {
        "pattern_id": pattern_id,
        "version": "0.1.0",
        "parent_version": None,
        "tenant_id": tenant_id,
        "project_id": "harness-governance",
        "name": "Bound graph traversal depth",
        "trigger_predicate": HarnessTriggerPredicate(
            domain_pack="research_reference",
            primary_intent="research",
            graph_relations=True,
            required_reason_codes=[HarnessReasonCode.GRAPH_FOLLOWUP_MISSING],
        ),
        "dimensions": [HarnessDimension.TOOL],
        "proposed_delta": HarnessConfigDelta(
            tool=HarnessToolConfig(graph_hops=2)
        ),
        "supporting_experience_ids": support_ids,
        "contradicting_experience_ids": [],
        "support_count": 3,
        "failure_count": 3,
        "estimated_quality_lift": 0.0,
        "confidence": 1.0,
        "status": HarnessPatternStatus.DRAFT,
        "miner_revision": "contract",
        "evaluator_revision": "pending",
        "created_at": datetime(2026, 5, 4, tzinfo=UTC),
    }
    pattern = HarnessPattern.model_validate(
        {**pattern_payload, "payload_hash": canonical_hash(pattern_payload)}
    )
    await policies.save_pattern(pattern)
    evolution = HarnessPatternEvolutionService(
        policies,
        DeterministicPatternEvaluator(experiences, min_support_cases=3),
    )

    staged = await evolution.evaluate_and_stage(
        pattern_id,
        tenant_id=tenant_id,
        project_id="harness-governance",
    )
    duplicate = await evolution.evaluate_and_stage(
        pattern_id,
        tenant_id=tenant_id,
        project_id="harness-governance",
    )

    assert staged.effective_status == HarnessPatternStatus.SHADOW
    assert staged.promotion_evidence.offline_ready is True
    assert duplicate.transitions == []
    assert len(
        await policies.list_pattern_evaluations(
            pattern_id,
            tenant_id=tenant_id,
            project_id="harness-governance",
        )
    ) == 1
    assert len(
        await policies.list_pattern_promotion_evidence(
            pattern_id,
            tenant_id=tenant_id,
            project_id="harness-governance",
        )
    ) == 1
    assert len(
        await policies.list_pattern_transitions(
            pattern_id,
            tenant_id=tenant_id,
            project_id="harness-governance",
        )
    ) == 2
    with pytest.raises(HarnessExperienceConflictError):
        await policies.save_pattern_transition(
            staged.transitions[0].model_copy(
                update={"transition_id": uuid4()}
            )
        )


@pytest.mark.asyncio
async def test_postgres_personal_control_is_scoped_and_evented(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, object_root = postgres_scope
    service = PersonalControlService(
        PostgresPersonalRepository(database),
        memories=JsonMemoryStore(object_root / "personal-memory.json"),
        trajectories=JsonlTrajectoryRepository(object_root / "personal-runs.jsonl"),
    )
    context = RunContext(
        tenant_id=tenant_id,
        project_id="personal",
        user_id="contract-user",
    )

    created = await service.create_task(
        TaskCreate(title="Postgres personal contract"),
        context,
    )
    updated = await service.update_task(
        created.task_id,
        TaskPatch(
            status=TaskStatus.IN_PROGRESS,
            expected_version=created.version,
        ),
        context,
    )
    isolated = await service.list_tasks(
        context.model_copy(update={"project_id": "other-project"})
    )
    event_count = await database.pool.fetchval(
        """
SELECT count(*)
FROM personal_events
WHERE tenant_id = $1 AND project_id = $2 AND record_id = $3
""",
        tenant_id,
        context.project_id,
        created.task_id,
    )

    assert updated.version == 2
    assert updated.status == TaskStatus.IN_PROGRESS
    assert isolated == []
    assert event_count == 2


@pytest.mark.asyncio
async def test_postgres_knowledge_and_outbox_contract(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, object_root = postgres_scope
    project_id = "knowledge"
    repository = PostgresKnowledgeRepository(
        database,
        FileKnowledgeObjectStore(object_root),
    )
    ingestion = KnowledgeIngestionService(
        repository,
        chunk_size=200,
        chunk_overlap=20,
    )
    content = (
        b"HELIOS-CONTRACT-2049 requires evidence-first hybrid retrieval. "
        b"Every answer must preserve tenant and project provenance. " * 6
    )
    source = KnowledgeSource(
        source_type="arxiv",
        source_id="arxiv:2607.99999",
        source_revision="v1",
        canonical_uri="https://arxiv.org/abs/2607.99999v1",
        privacy="public_reference",
        trust=TrustLevel.OBSERVED,
    )

    first, second = await asyncio.gather(
        ingestion.ingest(
            filename="contract.md",
            content=content,
            media_type="text/markdown",
            tenant_id=tenant_id,
            project_id=project_id,
            source=source,
        ),
        ingestion.ingest(
            filename="same-content.md",
            content=content,
            media_type="text/markdown",
            tenant_id=tenant_id,
            project_id=project_id,
            source=source,
        ),
    )

    assert first.document.document_id == second.document.document_id
    assert {first.deduplicated, second.deduplicated} == {False, True}
    assert await repository.chunk_count(
        tenant_id=tenant_id,
        project_id=project_id,
    ) >= 2
    evidence = await repository.search(
        "HELIOS-CONTRACT-2049 provenance",
        tenant_id=tenant_id,
        project_id=project_id,
    )
    assert evidence
    assert all(item.metadata["tenant_id"] == tenant_id for item in evidence)
    assert all(item.provenance.source_type == "arxiv" for item in evidence)
    assert all(item.provenance.trust == TrustLevel.OBSERVED for item in evidence)
    assert all(item.metadata["privacy"] == "public_reference" for item in evidence)
    assert (
        await repository.search(
            "HELIOS-CONTRACT-2049",
            tenant_id=f"{tenant_id}-other",
            project_id=project_id,
        )
        == []
    )
    outbox = PostgresOutboxRepository(database)
    assert await outbox.count_unpublished(
        tenant_id=tenant_id,
        project_id=project_id,
    ) == 1
    assert await outbox.count_unpublished(
        tenant_id=tenant_id,
        project_id="other",
    ) == 0

    old = datetime(2000, 1, 1, tzinfo=UTC)
    probe = OutboxEvent(
        aggregate_type="contract_probe",
        aggregate_id=uuid4().hex,
        event_type="contract.probe",
        payload={"tenant_id": tenant_id, "project_id": project_id},
        available_at=old,
        created_at=old,
        updated_at=old,
    )
    async with database.pool.acquire() as connection, connection.transaction():
        await insert_outbox_event(connection, probe)

    claimed = await outbox.claim(worker_id="contract-worker", lease_seconds=10)
    assert claimed is not None
    assert claimed.event_id == probe.event_id
    assert claimed.status == OutboxEventStatus.PROCESSING
    assert claimed.attempt == 1
    assert not await outbox.renew_lease(
        claimed.event_id,
        worker_id="other-worker",
        lease_seconds=10,
    )
    assert await outbox.renew_lease(
        claimed.event_id,
        worker_id="contract-worker",
        lease_seconds=10,
    )
    retried = await outbox.fail(
        claimed.event_id,
        worker_id="contract-worker",
        error="transient failure",
        retry_delay_seconds=1,
    )
    assert retried.status == OutboxEventStatus.PENDING
    await database.pool.execute(
        "UPDATE outbox_events SET available_at = $2 WHERE event_id = $1",
        probe.event_id,
        old,
    )
    claimed_again = await outbox.claim(
        worker_id="contract-worker",
        lease_seconds=10,
    )
    assert claimed_again is not None
    assert claimed_again.event_id == probe.event_id
    assert claimed_again.attempt == 2
    published = await outbox.mark_published(
        claimed_again.event_id,
        worker_id="contract-worker",
    )
    assert published.status == OutboxEventStatus.PUBLISHED

    assert await ingestion.archive(
        first.document.document_id,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    assert await repository.chunk_count(
        tenant_id=tenant_id,
        project_id=project_id,
    ) == 0
    assert (
        await repository.search(
            "HELIOS-CONTRACT-2049",
            tenant_id=tenant_id,
            project_id=project_id,
        )
        == []
    )


@pytest.mark.asyncio
async def test_postgres_ingestion_job_preserves_source_contract(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    source = KnowledgeSource(
        source_type="arxiv",
        source_id="arxiv:2607.12764",
        source_revision="v1",
        canonical_uri="https://arxiv.org/abs/2607.12764v1",
        privacy="public_reference",
        trust=TrustLevel.OBSERVED,
    )
    repository = PostgresIngestionJobRepository(database)
    await repository.start()
    job = IngestionJob(
        tenant_id=tenant_id,
        project_id="computer-science",
        filename="2607.12764v1.pdf",
        media_type="application/pdf",
        byte_size=1_024,
        content_hash="f" * 64,
        staging_key="fixture.upload",
        source=source,
    )

    queued, created = await repository.enqueue(job)
    restored = await repository.get(
        queued.job_id,
        tenant_id=tenant_id,
        project_id="computer-science",
    )

    assert created is True
    assert restored is not None
    assert restored.source == source


@pytest.mark.asyncio
async def test_postgres_learning_job_coalesces_and_fences_completion(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    repository = PostgresLearningJobRepository(database)
    await repository.start()
    trajectory = RunTrajectory(
        context=RunContext(
            tenant_id=tenant_id,
            project_id="learning",
            user_id="contract-user",
        ),
        user_input="Learn this durable workflow.",
        status=RunStatus.COMPLETED,
    )
    job = LearningJob(
        idempotency_key="d" * 64,
        tenant_id=tenant_id,
        project_id="learning",
        user_id="contract-user",
        run_id=trajectory.context.run_id,
        trigger="run_completed",
        trajectory=trajectory,
    )

    queued, created = await repository.enqueue(job)
    duplicate, duplicate_created = await repository.enqueue(job)
    claimed = await repository.claim(worker_id="contract-worker", lease_seconds=10)

    assert created is True
    assert duplicate_created is False
    assert duplicate.job_id == queued.job_id
    assert claimed is not None
    assert claimed.status == LearningJobStatus.RUNNING
    assert claimed.lease_token is not None
    assert not await repository.renew_lease(
        claimed.job_id,
        worker_id="contract-worker",
        lease_token=uuid4(),
        lease_seconds=10,
    )
    result = LearningJobResult(
        run_id=trajectory.context.run_id,
        reflector_revision="contract-v1",
        reflection_outcome="success",
        reflection_summary="Durable learning completed.",
    )
    with pytest.raises(LearningJobLeaseLostError):
        await repository.complete(
            claimed.job_id,
            worker_id="contract-worker",
            lease_token=uuid4(),
            result=result,
        )
    completed = await repository.complete(
        claimed.job_id,
        worker_id="contract-worker",
        lease_token=claimed.lease_token,
        result=result,
    )

    assert completed.status == LearningJobStatus.SUCCEEDED
    assert completed.result == result
    assert completed.trajectory == trajectory


@pytest.mark.asyncio
async def test_postgres_learning_jobs_serialize_the_same_run(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    repository = PostgresLearningJobRepository(database)
    await repository.start()
    trajectory = RunTrajectory(
        context=RunContext(tenant_id=tenant_id, project_id="ordered-learning"),
        user_input="Preserve completion before feedback.",
        status=RunStatus.COMPLETED,
    )
    completion = LearningJob(
        idempotency_key="e" * 64,
        tenant_id=tenant_id,
        project_id="ordered-learning",
        run_id=trajectory.context.run_id,
        trigger="run_completed",
        trajectory=trajectory,
    )
    feedback_trajectory = trajectory.model_copy(update={"feedback_score": -0.5})
    feedback = LearningJob(
        idempotency_key="f" * 64,
        tenant_id=tenant_id,
        project_id="ordered-learning",
        run_id=trajectory.context.run_id,
        trigger="feedback_received",
        trajectory=feedback_trajectory,
    )
    await repository.enqueue(completion)
    await repository.enqueue(feedback)

    claims = await asyncio.gather(
        repository.claim(worker_id="worker-a", lease_seconds=10),
        repository.claim(worker_id="worker-b", lease_seconds=10),
    )
    running = [item for item in claims if item is not None]

    assert len(running) == 1
    assert running[0].trigger == "run_completed"
    assert running[0].lease_token is not None
    result = LearningJobResult(
        run_id=trajectory.context.run_id,
        reflector_revision="contract-v1",
        reflection_outcome="success",
        reflection_summary="Completion processed first.",
    )
    await repository.complete(
        running[0].job_id,
        worker_id=running[0].lease_owner or "",
        lease_token=running[0].lease_token,
        result=result,
    )
    next_job = await repository.claim(worker_id="worker-c", lease_seconds=10)

    assert next_job is not None
    assert next_job.trigger == "feedback_received"


@pytest.mark.asyncio
async def test_postgres_learning_checkpoint_is_fenced_and_monotonic(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    repository = PostgresLearningJobRepository(database)
    trajectory = RunTrajectory(
        context=RunContext(tenant_id=tenant_id, project_id="checkpoint"),
        user_input="Persist the reflection before applying artifacts.",
        status=RunStatus.COMPLETED,
    )
    job = LearningJob(
        idempotency_key=hashlib.sha256(
            f"{tenant_id}:checkpoint".encode()
        ).hexdigest(),
        tenant_id=tenant_id,
        project_id="checkpoint",
        run_id=trajectory.context.run_id,
        trigger="run_completed",
        trajectory=trajectory,
    )
    await repository.enqueue(job)
    claimed = await repository.claim(worker_id="checkpoint-worker", lease_seconds=10)
    assert claimed is not None
    assert claimed.lease_token is not None
    reflection = LearningReflectionArtifact(
        run_id=trajectory.context.run_id,
        trajectory_hash="a" * 64,
        evaluation=LearningTrajectoryEvaluation(
            run_id=str(trajectory.context.run_id),
            quality_score=1.0,
            completion_score=1.0,
            tool_success_rate=1.0,
            citation_coverage=1.0,
            unsupported_claim_rate=0.0,
            passed=True,
        ),
        outcome="success",
        summary="The durable checkpoint is valid.",
        reflector_revision="contract-v1",
    )
    first = LearningJobCheckpoint(
        stage="reflection_completed",
        reflection=reflection,
    )

    with pytest.raises(LearningJobLeaseLostError):
        await repository.save_checkpoint(
            claimed.job_id,
            worker_id="checkpoint-worker",
            lease_token=uuid4(),
            checkpoint=first,
        )
    saved = await repository.save_checkpoint(
        claimed.job_id,
        worker_id="checkpoint-worker",
        lease_token=claimed.lease_token,
        checkpoint=first,
    )

    assert saved.checkpoint == first
    with pytest.raises(ValueError, match="advance exactly once"):
        await repository.save_checkpoint(
            claimed.job_id,
            worker_id="checkpoint-worker",
            lease_token=claimed.lease_token,
            checkpoint=first.model_copy(update={"stage": "evolution_committed"}),
        )
    advanced = await repository.save_checkpoint(
        claimed.job_id,
        worker_id="checkpoint-worker",
        lease_token=claimed.lease_token,
        checkpoint=first.model_copy(update={"stage": "artifacts_committed"}),
    )

    assert advanced.checkpoint is not None
    assert advanced.checkpoint.stage == "artifacts_committed"


@pytest.mark.asyncio
async def test_postgres_learning_artifacts_are_fenced_and_immutable(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    jobs = PostgresLearningJobRepository(database)
    store = PostgresLearningArtifactRepository(database)
    skills = PostgresSkillRepository(store)
    evaluations = PostgresSkillEvaluationRepository(store)
    transitions = PostgresSkillTransitionRepository(store)
    trajectory = RunTrajectory(
        context=RunContext(tenant_id=tenant_id, project_id="artifacts"),
        user_input="Exercise artifact fencing.",
        status=RunStatus.COMPLETED,
    )
    job = LearningJob(
        idempotency_key=hashlib.sha256(f"{tenant_id}:artifacts".encode()).hexdigest(),
        tenant_id=tenant_id,
        project_id="artifacts",
        run_id=trajectory.context.run_id,
        trigger="run_completed",
        trajectory=trajectory,
    )
    await jobs.enqueue(job)
    claimed = await jobs.claim(worker_id="artifact-worker", lease_seconds=10)
    assert claimed is not None
    assert claimed.lease_token is not None
    fence = LearningExecutionFence(
        job_id=claimed.job_id,
        worker_id="artifact-worker",
        lease_token=claimed.lease_token,
    )
    candidate = MemoryCandidate(
        tenant_id=tenant_id,
        project_id="artifacts",
        memory_type=MemoryType.EPISODIC,
        key=f"run:{trajectory.context.run_id}",
        summary="A stable contract memory.",
        confidence=0.9,
        provenance=[
            Provenance(
                source_type="run_trajectory",
                source_id=str(trajectory.context.run_id),
                run_id=trajectory.context.run_id,
                content_hash="contract",
                trust=TrustLevel.OBSERVED,
            )
        ],
    )
    skill = SkillDefinition(
        tenant_id=tenant_id,
        project_id="artifacts",
        name="contract_learning_skill",
        version="1.0.0",
        description="A stable skill used by the Postgres contract test.",
        steps=[
            SkillStep(
                action="search_knowledge",
                purpose="Retrieve grounded contract evidence.",
            )
        ],
        allowed_capabilities=["search_knowledge"],
        source_run_ids=[trajectory.context.run_id],
    )
    evaluation = SkillEvaluation(
        skill_id=skill.skill_id,
        tenant_id=tenant_id,
        project_id="artifacts",
        skill_version=skill.version,
        evaluator_revision="contract-v1",
        baseline_score=0.8,
        candidate_score=0.9,
        unsupported_claim_rate=0.0,
        security_passed=True,
        regression_passed=True,
    )
    transition = SkillTransitionEvent(
        skill_id=skill.skill_id,
        tenant_id=tenant_id,
        project_id="artifacts",
        skill_version=skill.version,
        transition_type="promotion",
        from_status=SkillStatus.DRAFT,
        to_status=SkillStatus.SECURITY_REVIEW,
        allowed=True,
        applied=True,
        reasons=["promotion_gates_passed"],
        evaluation_id=evaluation.evaluation_id,
        learning_job_id=claimed.job_id,
    )

    with learning_execution(fence):
        created = await store.upsert(candidate)
        replayed = await store.upsert(candidate)
        assert replayed == created
        await skills.save(skill)
        promoted = await skills.save(
            skill.model_copy(update={"status": SkillStatus.SECURITY_REVIEW})
        )
        assert promoted.status == SkillStatus.SECURITY_REVIEW
        with pytest.raises(LearningArtifactConflictError, match="definition changed"):
            await skills.save(
                skill.model_copy(update={"description": "A different immutable definition."})
            )
        await evaluations.save(evaluation)
        with pytest.raises(LearningArtifactConflictError, match="different content"):
            await evaluations.save(
                evaluation.model_copy(update={"candidate_score": 0.1})
            )
        await transitions.save(transition)
        assert len(
            await transitions.list_for_skill(
                skill.skill_id,
                tenant_id=tenant_id,
                project_id="artifacts",
            )
        ) == 1
        with pytest.raises(LearningArtifactConflictError, match="different content"):
            await transitions.save(
                transition.model_copy(update={"human_approved": True})
            )

    await jobs.complete(
        claimed.job_id,
        worker_id="artifact-worker",
        lease_token=claimed.lease_token,
        result=LearningJobResult(
            run_id=trajectory.context.run_id,
            reflector_revision="contract-v1",
            reflection_outcome="success",
            reflection_summary="Artifact contract complete.",
        ),
    )
    with learning_execution(fence), pytest.raises(LearningJobLeaseLostError):
        await store.upsert(
            candidate.model_copy(update={"key": "stale-worker-write"})
        )


@pytest.mark.asyncio
async def test_postgres_learning_stage_rolls_back_artifact_and_checkpoint(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    jobs = PostgresLearningJobRepository(database)
    artifacts = PostgresLearningArtifactRepository(database)
    trajectory = RunTrajectory(
        context=RunContext(tenant_id=tenant_id, project_id="atomic-stage"),
        user_input="Commit this learning stage atomically.",
        status=RunStatus.COMPLETED,
    )
    job = LearningJob(
        idempotency_key=hashlib.sha256(f"{tenant_id}:atomic-stage".encode()).hexdigest(),
        tenant_id=tenant_id,
        project_id="atomic-stage",
        run_id=trajectory.context.run_id,
        trigger="run_completed",
        trajectory=trajectory,
    )
    await jobs.enqueue(job)
    claimed = await jobs.claim(worker_id="atomic-worker", lease_seconds=30)
    assert claimed is not None
    assert claimed.lease_token is not None
    reflection = LearningReflectionArtifact(
        run_id=trajectory.context.run_id,
        trajectory_hash="b" * 64,
        evaluation=LearningTrajectoryEvaluation(
            run_id=str(trajectory.context.run_id),
            quality_score=1.0,
            completion_score=1.0,
            tool_success_rate=1.0,
            citation_coverage=1.0,
            unsupported_claim_rate=0.0,
            passed=True,
        ),
        outcome="success",
        summary="The atomic stage is ready.",
        reflector_revision="contract-v9",
    )
    first = LearningJobCheckpoint(
        stage="reflection_completed",
        reflection=reflection,
    )
    await jobs.save_checkpoint(
        claimed.job_id,
        worker_id="atomic-worker",
        lease_token=claimed.lease_token,
        checkpoint=first,
    )
    candidate = MemoryCandidate(
        tenant_id=tenant_id,
        project_id="atomic-stage",
        memory_type=MemoryType.EPISODIC,
        key=f"atomic:{trajectory.context.run_id}",
        summary="An artifact that must share the checkpoint transaction.",
        confidence=0.95,
        provenance=[
            Provenance(
                source_type="run_trajectory",
                source_id=str(trajectory.context.run_id),
                run_id=trajectory.context.run_id,
                content_hash="atomic-contract",
                trust=TrustLevel.OBSERVED,
            )
        ],
    )
    fence = LearningExecutionFence(
        job_id=claimed.job_id,
        worker_id="atomic-worker",
        lease_token=claimed.lease_token,
    )
    attempted_memory_id = None

    async def fail_after_artifact() -> LearningJobCheckpoint:
        nonlocal attempted_memory_id
        memory = await artifacts.upsert(candidate)
        attempted_memory_id = memory.memory_id
        raise RuntimeError("inject stage failure")

    with learning_execution(fence), pytest.raises(
        RuntimeError,
        match="inject stage failure",
    ):
        await jobs.commit_stage(
            claimed.job_id,
            worker_id="atomic-worker",
            lease_token=claimed.lease_token,
            operation=fail_after_artifact,
        )

    assert attempted_memory_id is not None
    assert (
        await database.pool.fetchval(
            "SELECT count(*) FROM learning_memories WHERE memory_id = $1",
            attempted_memory_id,
        )
        == 0
    )
    after_failure = await jobs.get(
        claimed.job_id,
        tenant_id=tenant_id,
        project_id="atomic-stage",
    )
    assert after_failure is not None
    assert after_failure.checkpoint == first

    async def commit_artifact() -> LearningJobCheckpoint:
        memory = await artifacts.upsert(candidate)
        return first.model_copy(
            update={
                "stage": "artifacts_committed",
                "memory_ids": [memory.memory_id],
            }
        )

    with learning_execution(fence):
        committed = await jobs.commit_stage(
            claimed.job_id,
            worker_id="atomic-worker",
            lease_token=claimed.lease_token,
            operation=commit_artifact,
        )

    assert committed.checkpoint is not None
    assert len(committed.checkpoint.memory_ids) == 1
    assert (
        await database.pool.fetchval(
            """
SELECT count(*)
FROM learning_job_artifact_links
WHERE job_id = $1 AND artifact_type = 'memory'
""",
            claimed.job_id,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_postgres_learning_stage_rolls_back_when_lease_changes_before_commit(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    jobs = PostgresLearningJobRepository(database)
    artifacts = PostgresLearningArtifactRepository(database)
    trajectory = RunTrajectory(
        context=RunContext(tenant_id=tenant_id, project_id="stale-stage"),
        user_input="Reject stale stage output.",
        status=RunStatus.COMPLETED,
    )
    job = LearningJob(
        idempotency_key=hashlib.sha256(f"{tenant_id}:stale-stage".encode()).hexdigest(),
        tenant_id=tenant_id,
        project_id="stale-stage",
        run_id=trajectory.context.run_id,
        trigger="run_completed",
        trajectory=trajectory,
    )
    await jobs.enqueue(job)
    claimed = await jobs.claim(worker_id="stale-stage-worker", lease_seconds=30)
    assert claimed is not None
    assert claimed.lease_token is not None
    first = LearningJobCheckpoint(
        stage="reflection_completed",
        reflection=LearningReflectionArtifact(
            run_id=trajectory.context.run_id,
            trajectory_hash="d" * 64,
            evaluation=LearningTrajectoryEvaluation(
                run_id=str(trajectory.context.run_id),
                quality_score=1.0,
                completion_score=1.0,
                tool_success_rate=1.0,
                citation_coverage=1.0,
                unsupported_claim_rate=0.0,
                passed=True,
            ),
            outcome="success",
            summary="The stale lease fixture is ready.",
            reflector_revision="contract-v9",
        ),
    )
    await jobs.save_checkpoint(
        claimed.job_id,
        worker_id="stale-stage-worker",
        lease_token=claimed.lease_token,
        checkpoint=first,
    )
    candidate = MemoryCandidate(
        tenant_id=tenant_id,
        project_id="stale-stage",
        memory_type=MemoryType.EPISODIC,
        key=f"stale:{trajectory.context.run_id}",
        summary="This write must roll back when the fencing token changes.",
        confidence=0.95,
        provenance=[
            Provenance(
                source_type="run_trajectory",
                source_id=str(trajectory.context.run_id),
                run_id=trajectory.context.run_id,
                content_hash="stale-stage-contract",
                trust=TrustLevel.OBSERVED,
            )
        ],
    )
    fence = LearningExecutionFence(
        job_id=claimed.job_id,
        worker_id="stale-stage-worker",
        lease_token=claimed.lease_token,
    )
    attempted_memory_id = None

    async def lose_lease_after_artifact() -> LearningJobCheckpoint:
        nonlocal attempted_memory_id
        memory = await artifacts.upsert(candidate)
        attempted_memory_id = memory.memory_id
        await database.pool.execute(
            """
UPDATE learning_jobs
SET lease_token = $2, updated_at = now()
WHERE job_id = $1
""",
            claimed.job_id,
            uuid4(),
        )
        return first.model_copy(
            update={
                "stage": "artifacts_committed",
                "memory_ids": [memory.memory_id],
            }
        )

    with learning_execution(fence), pytest.raises(LearningJobLeaseLostError):
        await jobs.commit_stage(
            claimed.job_id,
            worker_id="stale-stage-worker",
            lease_token=claimed.lease_token,
            operation=lose_lease_after_artifact,
        )

    assert attempted_memory_id is not None
    assert (
        await database.pool.fetchval(
            "SELECT count(*) FROM learning_memories WHERE memory_id = $1",
            attempted_memory_id,
        )
        == 0
    )
    after_loss = await jobs.get(
        claimed.job_id,
        tenant_id=tenant_id,
        project_id="stale-stage",
    )
    assert after_loss is not None
    assert after_loss.checkpoint == first


@pytest.mark.asyncio
async def test_postgres_skill_state_and_transition_ledger_commit_atomically(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    jobs = PostgresLearningJobRepository(database)
    store = PostgresLearningArtifactRepository(database)
    skills = PostgresSkillRepository(store)
    transitions = PostgresSkillTransitionRepository(store)
    trajectory = RunTrajectory(
        context=RunContext(tenant_id=tenant_id, project_id="atomic-transition"),
        user_input="Keep skill state and transition evidence atomic.",
        status=RunStatus.COMPLETED,
    )
    job = LearningJob(
        idempotency_key=hashlib.sha256(
            f"{tenant_id}:atomic-transition".encode()
        ).hexdigest(),
        tenant_id=tenant_id,
        project_id="atomic-transition",
        run_id=trajectory.context.run_id,
        trigger="run_completed",
        trajectory=trajectory,
    )
    await jobs.enqueue(job)
    claimed = await jobs.claim(worker_id="transition-worker", lease_seconds=30)
    assert claimed is not None
    assert claimed.lease_token is not None
    first = LearningJobCheckpoint(
        stage="reflection_completed",
        reflection=LearningReflectionArtifact(
            run_id=trajectory.context.run_id,
            trajectory_hash="e" * 64,
            evaluation=LearningTrajectoryEvaluation(
                run_id=str(trajectory.context.run_id),
                quality_score=1.0,
                completion_score=1.0,
                tool_success_rate=1.0,
                citation_coverage=1.0,
                unsupported_claim_rate=0.0,
                passed=True,
            ),
            outcome="success",
            summary="The transition fixture is ready.",
            reflector_revision="contract-v9",
        ),
    )
    await jobs.save_checkpoint(
        claimed.job_id,
        worker_id="transition-worker",
        lease_token=claimed.lease_token,
        checkpoint=first,
    )
    skill = SkillDefinition(
        tenant_id=tenant_id,
        project_id="atomic-transition",
        name="atomic_transition_skill",
        version="1.0.0",
        description="A skill whose state and ledger must commit together.",
        steps=[
            SkillStep(
                action="search_knowledge",
                purpose="Retrieve grounded transition evidence.",
            )
        ],
        allowed_capabilities=["search_knowledge"],
        source_run_ids=[trajectory.context.run_id],
    )
    fence = LearningExecutionFence(
        job_id=claimed.job_id,
        worker_id="transition-worker",
        lease_token=claimed.lease_token,
    )

    async def commit_candidate() -> LearningJobCheckpoint:
        await skills.save(skill)
        return first.model_copy(
            update={
                "stage": "artifacts_committed",
                "skill_candidate_id": skill.skill_id,
            }
        )

    with learning_execution(fence):
        candidate_job = await jobs.commit_stage(
            claimed.job_id,
            worker_id="transition-worker",
            lease_token=claimed.lease_token,
            operation=commit_candidate,
        )
    assert candidate_job.checkpoint is not None
    observed = candidate_job.checkpoint.model_copy(
        update={"stage": "observations_committed"}
    )
    await jobs.save_checkpoint(
        claimed.job_id,
        worker_id="transition-worker",
        lease_token=claimed.lease_token,
        checkpoint=observed,
    )
    transition = SkillTransitionEvent(
        skill_id=skill.skill_id,
        tenant_id=tenant_id,
        project_id="atomic-transition",
        skill_version=skill.version,
        transition_type="promotion",
        from_status=SkillStatus.DRAFT,
        to_status=SkillStatus.SECURITY_REVIEW,
        allowed=True,
        applied=True,
        reasons=["atomic_contract"],
        learning_job_id=claimed.job_id,
    )

    async def fail_after_transition() -> LearningJobCheckpoint:
        await skills.save(
            skill.model_copy(update={"status": SkillStatus.SECURITY_REVIEW})
        )
        await transitions.save(transition)
        raise RuntimeError("inject transition failure")

    with learning_execution(fence), pytest.raises(
        RuntimeError,
        match="inject transition failure",
    ):
        await jobs.commit_stage(
            claimed.job_id,
            worker_id="transition-worker",
            lease_token=claimed.lease_token,
            operation=fail_after_transition,
        )

    rolled_back = await skills.get(
        skill.skill_id,
        tenant_id=tenant_id,
        project_id="atomic-transition",
    )
    assert rolled_back is not None
    assert rolled_back.status == SkillStatus.DRAFT
    assert (
        await transitions.list_for_skill(
            skill.skill_id,
            tenant_id=tenant_id,
            project_id="atomic-transition",
        )
        == []
    )
    after_failure = await jobs.get(
        claimed.job_id,
        tenant_id=tenant_id,
        project_id="atomic-transition",
    )
    assert after_failure is not None
    assert after_failure.checkpoint == observed

    async def commit_transition() -> LearningJobCheckpoint:
        await skills.save(
            skill.model_copy(update={"status": SkillStatus.SECURITY_REVIEW})
        )
        await transitions.save(transition)
        return observed.model_copy(
            update={
                "stage": "evolution_committed",
                "transition_ids": [transition.transition_id],
            }
        )

    with learning_execution(fence):
        committed = await jobs.commit_stage(
            claimed.job_id,
            worker_id="transition-worker",
            lease_token=claimed.lease_token,
            operation=commit_transition,
        )

    current_skill = await skills.get(
        skill.skill_id,
        tenant_id=tenant_id,
        project_id="atomic-transition",
    )
    assert current_skill is not None
    assert current_skill.status == SkillStatus.SECURITY_REVIEW
    assert committed.checkpoint is not None
    assert committed.checkpoint.transition_ids == [transition.transition_id]
    assert (
        await database.pool.fetchval(
            """
SELECT count(*)
FROM learning_job_artifact_links
WHERE job_id = $1 AND artifact_type = 'transition'
""",
            claimed.job_id,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_postgres_learning_reconciliation_repairs_links_and_detects_loss(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    jobs = PostgresLearningJobRepository(database)
    artifacts = PostgresLearningArtifactRepository(database)
    trajectory = RunTrajectory(
        context=RunContext(tenant_id=tenant_id, project_id="reconciliation"),
        user_input="Reconcile durable learning artifacts.",
        status=RunStatus.COMPLETED,
    )
    job = LearningJob(
        idempotency_key=hashlib.sha256(
            f"{tenant_id}:reconciliation".encode()
        ).hexdigest(),
        tenant_id=tenant_id,
        project_id="reconciliation",
        run_id=trajectory.context.run_id,
        trigger="run_completed",
        trajectory=trajectory,
    )
    await jobs.enqueue(job)
    claimed = await jobs.claim(worker_id="reconcile-worker", lease_seconds=30)
    assert claimed is not None
    assert claimed.lease_token is not None
    first = LearningJobCheckpoint(
        stage="reflection_completed",
        reflection=LearningReflectionArtifact(
            run_id=trajectory.context.run_id,
            trajectory_hash="c" * 64,
            evaluation=LearningTrajectoryEvaluation(
                run_id=str(trajectory.context.run_id),
                quality_score=1.0,
                completion_score=1.0,
                tool_success_rate=1.0,
                citation_coverage=1.0,
                unsupported_claim_rate=0.0,
                passed=True,
            ),
            outcome="success",
            summary="The reconciliation fixture is complete.",
            reflector_revision="contract-v9",
        ),
    )
    await jobs.save_checkpoint(
        claimed.job_id,
        worker_id="reconcile-worker",
        lease_token=claimed.lease_token,
        checkpoint=first,
    )
    candidate = MemoryCandidate(
        tenant_id=tenant_id,
        project_id="reconciliation",
        memory_type=MemoryType.EPISODIC,
        key=f"reconcile:{trajectory.context.run_id}",
        summary="A reconciled durable memory.",
        confidence=0.95,
        provenance=[
            Provenance(
                source_type="run_trajectory",
                source_id=str(trajectory.context.run_id),
                run_id=trajectory.context.run_id,
                content_hash="reconciliation-contract",
                trust=TrustLevel.OBSERVED,
            )
        ],
    )
    fence = LearningExecutionFence(
        job_id=claimed.job_id,
        worker_id="reconcile-worker",
        lease_token=claimed.lease_token,
    )

    async def commit_memory() -> LearningJobCheckpoint:
        memory = await artifacts.upsert(candidate)
        return first.model_copy(
            update={
                "stage": "artifacts_committed",
                "memory_ids": [memory.memory_id],
            }
        )

    with learning_execution(fence):
        committed = await jobs.commit_stage(
            claimed.job_id,
            worker_id="reconcile-worker",
            lease_token=claimed.lease_token,
            operation=commit_memory,
        )
    assert committed.checkpoint is not None
    observations = committed.checkpoint.model_copy(
        update={"stage": "observations_committed"}
    )
    await jobs.save_checkpoint(
        claimed.job_id,
        worker_id="reconcile-worker",
        lease_token=claimed.lease_token,
        checkpoint=observations,
    )
    final = observations.model_copy(update={"stage": "evolution_committed"})
    await jobs.save_checkpoint(
        claimed.job_id,
        worker_id="reconcile-worker",
        lease_token=claimed.lease_token,
        checkpoint=final,
    )
    await jobs.complete(
        claimed.job_id,
        worker_id="reconcile-worker",
        lease_token=claimed.lease_token,
        result=expected_learning_result(
            run_id=trajectory.context.run_id,
            checkpoint=final,
        ),
    )
    memory_id = final.memory_ids[0]
    await database.pool.execute(
        """
DELETE FROM learning_job_artifact_links
WHERE job_id = $1 AND artifact_type = 'memory'
""",
        claimed.job_id,
    )
    await database.pool.execute(
        """
UPDATE learning_jobs
SET reconciliation_status = 'pending'
WHERE job_id = $1
""",
        claimed.job_id,
    )

    reconciler = PostgresLearningReconciler(database)
    repaired = await reconciler.reconcile(
        tenant_id=tenant_id,
        project_id="reconciliation",
    )

    assert repaired.verified == 1
    assert repaired.required == 0
    assert repaired.links_repaired == 1
    await database.pool.execute(
        "DELETE FROM learning_memories WHERE memory_id = $1",
        memory_id,
    )
    await database.pool.execute(
        """
UPDATE learning_jobs
SET reconciliation_status = 'pending'
WHERE job_id = $1
""",
        claimed.job_id,
    )

    damaged = await reconciler.reconcile(
        tenant_id=tenant_id,
        project_id="reconciliation",
    )

    assert damaged.verified == 0
    assert damaged.required == 1
    assert "missing artifacts" in damaged.issues[0].error


@pytest.mark.asyncio
async def test_postgres_learning_reconciliation_requires_exact_skill_version(
    postgres_scope: tuple[PostgresDatabase, str, Path],
) -> None:
    database, tenant_id, _ = postgres_scope
    project_id = "versioned-reconciliation"
    jobs = PostgresLearningJobRepository(database)
    artifact_store = PostgresLearningArtifactRepository(database)
    skills = PostgresSkillRepository(artifact_store)
    trajectory = RunTrajectory(
        context=RunContext(tenant_id=tenant_id, project_id=project_id),
        user_input="Verify exact Skill versions during durable reconciliation.",
        status=RunStatus.COMPLETED,
    )
    job = LearningJob(
        idempotency_key=hashlib.sha256(
            f"{tenant_id}:versioned-reconciliation".encode()
        ).hexdigest(),
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=trajectory.context.run_id,
        trigger="run_completed",
        trajectory=trajectory,
    )
    await jobs.enqueue(job)
    claimed = await jobs.claim(worker_id="version-worker", lease_seconds=30)
    assert claimed is not None
    assert claimed.lease_token is not None
    reflection = LearningReflectionArtifact(
        run_id=trajectory.context.run_id,
        trajectory_hash="d" * 64,
        evaluation=LearningTrajectoryEvaluation(
            run_id=str(trajectory.context.run_id),
            quality_score=1.0,
            completion_score=1.0,
            tool_success_rate=1.0,
            citation_coverage=1.0,
            unsupported_claim_rate=0.0,
            passed=True,
        ),
        outcome="success",
        summary="The exact Skill version fixture is complete.",
        reflector_revision="contract-v10",
    )
    first = LearningJobCheckpoint(
        stage="reflection_completed",
        reflection=reflection,
    )
    await jobs.save_checkpoint(
        claimed.job_id,
        worker_id="version-worker",
        lease_token=claimed.lease_token,
        checkpoint=first,
    )
    parent = SkillDefinition(
        tenant_id=tenant_id,
        project_id=project_id,
        name="versioned_reconciliation_skill",
        version="0.1.0",
        description="A parent Skill retained while its refinement is reconciled.",
        status=SkillStatus.SHADOW,
        steps=[
            SkillStep(
                action="search_knowledge",
                purpose="Retrieve the original evidence fixture.",
            )
        ],
        allowed_capabilities=["search_knowledge"],
        source_run_ids=[trajectory.context.run_id],
    )
    child = parent.model_copy(
        update={
            "version": "0.2.0",
            "status": SkillStatus.DRAFT,
            "parent_version": parent.version,
            "steps": [
                *parent.steps,
                SkillStep(
                    action="verify_evidence",
                    purpose="Verify retrieved evidence before synthesis.",
                ),
            ],
            "allowed_capabilities": ["search_knowledge", "verify_evidence"],
        }
    )
    fence = LearningExecutionFence(
        job_id=claimed.job_id,
        worker_id="version-worker",
        lease_token=claimed.lease_token,
    )
    with learning_execution(fence):
        await skills.save(parent)

    async def commit_child() -> LearningJobCheckpoint:
        await skills.save(child)
        return first.model_copy(
            update={
                "stage": "artifacts_committed",
                "skill_candidate_id": child.skill_id,
                "skill_candidate_version": child.version,
            }
        )

    with learning_execution(fence):
        committed = await jobs.commit_stage(
            claimed.job_id,
            worker_id="version-worker",
            lease_token=claimed.lease_token,
            operation=commit_child,
        )
    assert committed.checkpoint is not None
    assert (
        await skills.get(
            child.skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            version=parent.version,
        )
        == parent
    )
    assert (
        await skills.get(
            child.skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            version=child.version,
        )
        == child
    )
    link_version = await database.pool.fetchval(
        """
SELECT artifact_version
FROM learning_job_artifact_links
WHERE job_id = $1 AND artifact_type = 'skill' AND artifact_id = $2
""",
        claimed.job_id,
        child.skill_id,
    )
    assert link_version == child.version

    observations = committed.checkpoint.model_copy(
        update={"stage": "observations_committed"}
    )
    await jobs.save_checkpoint(
        claimed.job_id,
        worker_id="version-worker",
        lease_token=claimed.lease_token,
        checkpoint=observations,
    )
    final = observations.model_copy(update={"stage": "evolution_committed"})
    await jobs.save_checkpoint(
        claimed.job_id,
        worker_id="version-worker",
        lease_token=claimed.lease_token,
        checkpoint=final,
    )
    result = expected_learning_result(
        run_id=trajectory.context.run_id,
        checkpoint=final,
    )
    await jobs.complete(
        claimed.job_id,
        worker_id="version-worker",
        lease_token=claimed.lease_token,
        result=result,
    )
    assert result.skill_candidate_version == child.version

    reconciler = PostgresLearningReconciler(database)
    verified = await reconciler.reconcile(
        tenant_id=tenant_id,
        project_id=project_id,
        include_verified=True,
    )
    assert verified.verified == 1
    assert verified.required == 0

    await database.pool.execute(
        "DELETE FROM learning_skills WHERE skill_id = $1 AND version = $2",
        child.skill_id,
        child.version,
    )
    await database.pool.execute(
        """
UPDATE learning_jobs
SET reconciliation_status = 'pending'
WHERE job_id = $1
""",
        claimed.job_id,
    )
    damaged = await reconciler.reconcile(
        tenant_id=tenant_id,
        project_id=project_id,
    )

    assert damaged.verified == 0
    assert damaged.required == 1
    assert f"skill:{child.skill_id}@{child.version}" in damaged.issues[0].error
