from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from app.agent.hermes_native_learning import (
    HermesNativeAdminHealth,
    HermesNativeLearningAudit,
    HermesNativeLearningService,
)
from app.capabilities.agent_tool_runtime import AgentToolRuntime
from app.config import Settings
from app.domain.contracts import (
    ConversationRepository,
    HarnessExperienceRepository,
    HarnessPolicyRepository,
    KnowledgeRepository,
    LearningChangeSetRepository,
    MemoryRepository,
    OutboxRepository,
    SkillRepository,
)
from app.domain.enums import (
    CapabilityEffect,
    GraphCandidateStatus,
    MemoryType,
    SkillStatus,
    TrustLevel,
)
from app.domain.models import (
    CapabilitySpec,
    ConversationMetadata,
    ConversationSummary,
    EntityResolutionCandidate,
    GraphCandidateCollection,
    GraphEntityCandidate,
    GraphEntityCompareRequest,
    GraphEntityCompareResult,
    GraphEntityResolveRequest,
    GraphEntityResolveResult,
    GraphRAGRequest,
    GraphRAGResult,
    GraphRelationCandidate,
    GraphSearchRequest,
    GraphSearchResult,
    IngestionJob,
    IngestionJobSubmission,
    IngestionResult,
    KnowledgeDocument,
    KnowledgeSource,
    LearningChangeSet,
    LearningJob,
    MemoryCandidate,
    MemoryRecord,
    PromotionDecision,
    Provenance,
    RunContext,
    RunTrajectory,
    SkillDefinition,
    SkillEvaluation,
    SkillEvolutionResult,
    SkillEvolutionSnapshot,
    SkillTransitionEvent,
    WorkspaceProfile,
    utc_now,
)
from app.domain_packs.registry import DomainPackRegistry
from app.evaluation.self_learning import (
    SelfLearningEffectEvaluator,
    SelfLearningEffectReport,
)
from app.graph.graph_candidate_service import GraphCandidateService
from app.harness.evolution import HarnessPatternEvolutionService
from app.harness.models import (
    HarnessExperienceEntry,
    HarnessExperienceEvaluation,
    HarnessPattern,
    HarnessPatternEvaluation,
    HarnessPatternEvolutionResult,
    HarnessPatternPromotionEvidence,
    HarnessPatternStatus,
    HarnessPatternTransition,
    RunHarnessOverlay,
)
from app.infra.local_repositories import JsonlTrajectoryRepository
from app.knowledge.ingestion_jobs import (
    IngestionJobService,
    IngestionJobsUnavailableError,
)
from app.knowledge.knowledge_ingestion import KnowledgeIngestionError, KnowledgeIngestionService
from app.knowledge.knowledge_visibility import WorkspaceProfileResolver, document_is_visible
from app.learning.engine import LearningEngine
from app.learning.evolution import SkillEvolutionService
from app.learning.jobs import LearningJobService, LearningJobsUnavailableError

if TYPE_CHECKING:
    from app.demo.enterprise_fixture import (
        EnterpriseFixturePreview,
        EnterpriseFixtureResetResult,
        EnterpriseFixtureRun,
        EnterpriseFixtureService,
    )


def _clip_text(value: str, limit: int, *, fallback: str = "") -> str:
    normalized = " ".join(value.split()) or fallback
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)].rstrip() + "..."


_ATTACHMENT_BLOCK = re.compile(
    r"\n\n<attachments>\n[\s\S]*?\n</attachments>\s*$",
    re.IGNORECASE,
)


def _visible_user_input(value: str) -> str:
    return _ATTACHMENT_BLOCK.sub("", value).strip()


class WorkspaceService:
    """Scoped read model and guarded control actions for the operator UI."""

    def __init__(
        self,
        *,
        settings: Settings,
        trajectories: JsonlTrajectoryRepository,
        conversations: ConversationRepository,
        memories: MemoryRepository,
        skills: SkillRepository,
        change_sets: LearningChangeSetRepository,
        integration_runtime: AgentToolRuntime,
        learning_engine: LearningEngine,
        skill_evolution: SkillEvolutionService,
        knowledge_repository: KnowledgeRepository,
        ingestion_service: KnowledgeIngestionService,
        ingestion_job_service: IngestionJobService | None,
        learning_job_service: LearningJobService | None,
        harness_experiences: HarnessExperienceRepository,
        harness_policies: HarnessPolicyRepository,
        harness_pattern_evolution: HarnessPatternEvolutionService,
        graph_candidate_service: GraphCandidateService,
        hermes_native_learning_service: HermesNativeLearningService,
        domain_packs: DomainPackRegistry | None = None,
        outbox_repository: OutboxRepository | None = None,
        workspace_profiles: WorkspaceProfileResolver | None = None,
        enterprise_fixture_service: EnterpriseFixtureService | None = None,
    ) -> None:
        self._settings = settings
        self._trajectories = trajectories
        self._conversations = conversations
        self._memories = memories
        self._skills = skills
        self._change_sets = change_sets
        self._integration = integration_runtime
        self._learning = learning_engine
        self._skill_evolution = skill_evolution
        self._knowledge = knowledge_repository
        self._ingestion = ingestion_service
        self._ingestion_jobs = ingestion_job_service
        self._learning_jobs = learning_job_service
        self._harness_experiences = harness_experiences
        self._harness_policies = harness_policies
        self._harness_pattern_evolution = harness_pattern_evolution
        self._graph_candidates = graph_candidate_service
        self._hermes_native_learning = hermes_native_learning_service
        self._domain_packs = domain_packs or DomainPackRegistry()
        self._outbox = outbox_repository
        self._workspace_profiles = workspace_profiles
        self._enterprise_fixture = enterprise_fixture_service

    @property
    def max_upload_bytes(self) -> int:
        return self._settings.max_upload_bytes

    async def overview(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> dict[str, object]:
        runs = await self.list_runs(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            limit=200,
        )
        memories = await self.list_memories(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        skills = await self.list_skills(tenant_id=tenant_id, project_id=project_id)
        changes = await self.list_change_sets(tenant_id=tenant_id, project_id=project_id)
        profile = self._profile(tenant_id=tenant_id, project_id=project_id)
        documents = await self.list_documents(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        graph_candidates = await self.list_graph_candidates(
            tenant_id=tenant_id,
            project_id=project_id,
        )
        ingestion_jobs = (
            await self.list_ingestion_jobs(
                tenant_id=tenant_id,
                project_id=project_id,
                limit=200,
            )
            if self._ingestion_jobs is not None
            else []
        )
        learning_jobs = (
            await self.list_learning_jobs(
                tenant_id=tenant_id,
                project_id=project_id,
                limit=200,
            )
            if self._learning_jobs is not None
            else []
        )
        harness_experiences = await self.list_harness_experiences(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=500,
        )
        harness_patterns = await self.list_harness_patterns(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=500,
        )
        outbox_unpublished = (
            await self._outbox.count_unpublished(
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if self._outbox is not None
            else 0
        )
        return {
            "runtime_mode": self._settings.runtime_mode,
            "model": self._settings.openai_model,
            "conversation_fast_path_model": (
                self._settings.adaptive_rag_router_model
                or self._settings.conversation_fast_path_model
                or self._settings.openai_model
            ),
            "conversation_history_turns": self._settings.conversation_history_turns,
            "context_total_tokens": self._settings.context_total_tokens,
            "adaptive_rag_router_timeout_seconds": (
                self._settings.adaptive_rag_router_timeout_seconds
            ),
            "adaptive_rag_router_max_completion_tokens": (
                self._settings.adaptive_rag_router_max_completion_tokens
            ),
            "model_provider": self._settings.model_provider,
            "learning_mode": self._settings.learning_mode,
            "learning_reflector_mode": self._settings.learning_reflector_mode,
            "learning_job_mode": self._settings.learning_job_mode,
            "learning_artifact_backend": self._settings.learning_artifact_backend,
            "harness_experience_enabled": self._settings.harness_experience_enabled,
            "harness_distillation_enabled": self._settings.harness_distillation_enabled,
            "harness_overlay_mode": self._settings.harness_overlay_mode,
            "web_search_mode": self._settings.web_search_mode,
            "retrieval_backend": self._settings.retrieval_backend,
            "embedding_provider": self._settings.embedding_provider,
            "qdrant_collection": self._settings.qdrant_collection,
            "qdrant_sparse_idf": self._settings.qdrant_sparse_idf,
            "qdrant_sparse_encoder": self._settings.qdrant_sparse_encoder,
            "qdrant_bm25_k1": self._settings.qdrant_bm25_k1,
            "qdrant_bm25_b": self._settings.qdrant_bm25_b,
            "qdrant_bm25_average_document_tokens": (
                self._settings.qdrant_bm25_average_document_tokens
            ),
            "graph_backend": self._settings.graph_backend,
            "graph_extractor_mode": self._settings.graph_extractor_mode,
            "knowledge_repository_backend": self._settings.knowledge_repository_backend,
            "ingestion_mode": self._settings.ingestion_mode,
            "counts": {
                "runs": len(runs),
                "memories": len(memories),
                "skills": len(skills),
                "active_skills": sum(skill.status == SkillStatus.ACTIVE for skill in skills),
                "change_sets": len(changes),
                "documents": len(documents),
                "chunks": sum(item.chunk_count for item in documents),
                "graph_entity_candidates": len(graph_candidates.entities),
                "graph_relation_candidates": len(graph_candidates.relations),
                "graph_resolution_candidates": len(graph_candidates.resolutions),
                "pending_graph_candidates": (
                    sum(
                        item.status == GraphCandidateStatus.PENDING
                        for item in graph_candidates.entities
                    )
                    + sum(
                        item.status == GraphCandidateStatus.PENDING
                        for item in graph_candidates.relations
                    )
                    + sum(
                        item.status == GraphCandidateStatus.PENDING
                        for item in graph_candidates.resolutions
                    )
                ),
                "ingestion_jobs": len(ingestion_jobs),
                "active_ingestion_jobs": sum(
                    item.status.value in {"queued", "running", "retry_scheduled"}
                    for item in ingestion_jobs
                ),
                "learning_jobs": len(learning_jobs),
                "harness_experiences": len(harness_experiences),
                "learnable_harness_experiences": sum(
                    item.diagnosis.learnable for item in harness_experiences
                ),
                "negative_harness_experiences": sum(
                    not item.diagnosis.success for item in harness_experiences
                ),
                "harness_patterns": len(harness_patterns),
                "draft_harness_patterns": sum(
                    item.status == HarnessPatternStatus.DRAFT
                    for item in harness_patterns
                ),
                "active_learning_jobs": sum(
                    item.status.value in {"queued", "running", "retry_scheduled"}
                    for item in learning_jobs
                ),
                "outbox_unpublished": outbox_unpublished,
            },
            "domain_packs": self._domain_packs.names(),
            "workspace_profile": (
                profile.model_dump(mode="json") if profile is not None else None
            ),
            "routing_lane_counts": {
                lane: sum(
                    run.answer is not None
                    and (
                        run.answer.routing_lane.value
                        if (
                            run.answer.routing_lane is not None
                            and run.snapshot is not None
                            and run.snapshot.component_versions.get(
                                "conversation_router"
                            )
                            == "2"
                        )
                        else "legacy"
                    )
                    == lane
                    for run in runs
                )
                for lane in ("deterministic", "conversation", "agent", "legacy")
            },
            "capabilities": [spec.name for spec in self.capabilities()],
        }

    async def list_runs(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = None,
        limit: int = 50,
    ) -> Sequence[RunTrajectory]:
        runs = await self._trajectories.list_recent(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=limit,
        )
        if user_id is None:
            return runs
        return tuple(run for run in runs if run.context.user_id == user_id)

    async def list_conversations(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        limit: int = 50,
        include_archived: bool = False,
    ) -> Sequence[ConversationSummary]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        runs = await self._trajectories.list_recent(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=10_000,
        )
        grouped: dict[str, ConversationSummary] = {}
        for run in runs:
            if run.context.user_id != user_id:
                continue
            session_id = run.context.session_id.strip()
            if not session_id:
                continue
            current = grouped.get(session_id)
            if current is None:
                latest_text = (
                    run.answer.answer_markdown
                    if run.answer is not None and run.answer.answer_markdown.strip()
                    else _visible_user_input(run.user_input)
                )
                grouped[session_id] = ConversationSummary(
                    session_id=session_id,
                    title=_clip_text(
                        _visible_user_input(run.user_input),
                        200,
                        fallback="New conversation",
                    ),
                    preview=_clip_text(latest_text, 500),
                    run_count=1,
                    last_run_id=run.context.run_id,
                    last_status=run.status,
                    domain_pack=run.context.domain_pack,
                    created_at=run.context.started_at,
                    updated_at=run.completed_at or run.context.started_at,
                )
                continue
            grouped[session_id] = current.model_copy(
                update={
                    "run_count": current.run_count + 1,
                    "title": _clip_text(
                        _visible_user_input(run.user_input),
                        200,
                        fallback="New conversation",
                    ),
                    "created_at": min(run.context.started_at, current.created_at),
                }
            )
        metadata = {
            item.session_id: item
            for item in await self._conversations.list_scoped(
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
            )
        }
        summaries = []
        for session_id, summary in grouped.items():
            preferences = metadata.get(session_id)
            if preferences is not None:
                summary = summary.model_copy(
                    update={
                        "title": preferences.title or summary.title,
                        "archived": preferences.archived,
                    }
                )
            if include_archived or not summary.archived:
                summaries.append(summary)
        summaries.sort(
            key=lambda item: (item.updated_at, str(item.last_run_id)),
            reverse=True,
        )
        return summaries[:limit]

    async def update_conversation(
        self,
        session_id: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        title: str | None = None,
        archived: bool | None = None,
    ) -> ConversationMetadata:
        normalized_session = session_id.strip()
        if not normalized_session:
            raise ValueError("session_id must not be empty")
        if title is None and archived is None:
            raise ValueError("At least one conversation field must be updated")
        runs = await self._trajectories.list_session(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            session_id=normalized_session,
            limit=1,
        )
        if not runs:
            raise KeyError("Conversation not found")
        normalized_title = title.strip() if title is not None else None
        if title is not None and not normalized_title:
            raise ValueError("Conversation title must not be empty")
        current = await self._conversations.get(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            session_id=normalized_session,
        )
        updated = ConversationMetadata(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            session_id=normalized_session,
            title=(normalized_title if title is not None else current.title if current else None),
            archived=(archived if archived is not None else current.archived if current else False),
            context_summary=current.context_summary if current is not None else "",
            summarized_run_ids=(
                current.summarized_run_ids if current is not None else []
            ),
            context_summary_revision=(
                current.context_summary_revision if current is not None else None
            ),
            context_summary_updated_at=(
                current.context_summary_updated_at if current is not None else None
            ),
            created_at=current.created_at if current is not None else utc_now(),
            updated_at=utc_now(),
        )
        await self._conversations.save(updated)
        return updated

    async def list_conversation_runs(
        self,
        session_id: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        limit: int = 200,
    ) -> Sequence[RunTrajectory]:
        normalized = session_id.strip()
        if not normalized:
            raise ValueError("session_id must not be empty")
        runs = await self._trajectories.list_session(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            session_id=normalized,
            limit=limit,
        )
        return tuple(reversed(runs))

    async def list_harness_experiences(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        limit: int = 100,
        learnable: bool | None = None,
        success: bool | None = None,
    ) -> Sequence[HarnessExperienceEntry]:
        return await self._harness_experiences.list_scoped(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=limit,
            learnable=learnable,
            success=success,
        )

    async def self_learning_effectiveness(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        limit: int = 500,
        minimum_experiences: int = 20,
        minimum_feedback: int = 5,
    ) -> SelfLearningEffectReport:
        evaluator = SelfLearningEffectEvaluator(
            self._harness_experiences,
            self._harness_policies,
            minimum_experiences=minimum_experiences,
            minimum_feedback=minimum_feedback,
        )
        return await evaluator.evaluate(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=limit,
        )

    async def get_harness_experience(
        self,
        experience_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> HarnessExperienceEntry | None:
        return await self._harness_experiences.get(
            experience_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def list_harness_experience_evaluations(
        self,
        experience_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> Sequence[HarnessExperienceEvaluation]:
        return await self._harness_experiences.list_evaluations(
            experience_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def list_harness_patterns(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        limit: int = 100,
        status: HarnessPatternStatus | None = None,
    ) -> Sequence[HarnessPattern]:
        return await self._harness_policies.list_patterns(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=limit,
            status=status,
        )

    async def get_harness_pattern(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        version: str | None = None,
    ) -> HarnessPattern | None:
        return await self._harness_policies.get_pattern(
            pattern_id,
            tenant_id=tenant_id,
            project_id=project_id,
            version=version,
        )

    async def evaluate_harness_pattern(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        pattern_version: str | None = None,
    ) -> HarnessPatternEvolutionResult:
        return await self._harness_pattern_evolution.evaluate_and_stage(
            pattern_id,
            tenant_id=tenant_id,
            project_id=project_id,
            pattern_version=pattern_version,
        )

    async def transition_harness_pattern(
        self,
        pattern_id: UUID,
        target_status: HarnessPatternStatus,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        pattern_version: str | None = None,
        human_approved: bool = False,
        expected_from_status: HarnessPatternStatus | None = None,
        reason: str = "",
    ) -> HarnessPatternTransition:
        if target_status == HarnessPatternStatus.ROLLED_BACK:
            return await self._harness_pattern_evolution.rollback(
                pattern_id,
                reason=reason,
                tenant_id=tenant_id,
                project_id=project_id,
                pattern_version=pattern_version,
                actor="human",
                expected_from_status=expected_from_status,
            )
        return await self._harness_pattern_evolution.transition(
            pattern_id,
            target_status,
            tenant_id=tenant_id,
            project_id=project_id,
            pattern_version=pattern_version,
            human_approved=human_approved,
            actor="human" if human_approved else "system",
            expected_from_status=expected_from_status,
        )

    async def list_harness_pattern_evaluations(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternEvaluation]:
        return await self._harness_policies.list_pattern_evaluations(
            pattern_id,
            tenant_id=tenant_id,
            project_id=project_id,
            pattern_version=pattern_version,
        )

    async def list_harness_pattern_promotion_evidence(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternPromotionEvidence]:
        return await self._harness_policies.list_pattern_promotion_evidence(
            pattern_id,
            tenant_id=tenant_id,
            project_id=project_id,
            pattern_version=pattern_version,
        )

    async def list_harness_pattern_transitions(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternTransition]:
        return await self._harness_policies.list_pattern_transitions(
            pattern_id,
            tenant_id=tenant_id,
            project_id=project_id,
            pattern_version=pattern_version,
        )

    async def get_harness_overlay(
        self,
        run_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> RunHarnessOverlay | None:
        return await self._harness_policies.get_overlay(
            run_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def get_run(
        self,
        run_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str | None = None,
        user_id: str | None = None,
    ) -> RunTrajectory | None:
        run = await self._trajectories.get(run_id)
        if run is None or run.context.tenant_id != tenant_id:
            return None
        if project_id is not None and run.context.project_id != project_id:
            return None
        if user_id is not None and run.context.user_id != user_id:
            return None
        return run

    async def list_memories(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        include_revoked: bool = False,
    ) -> Sequence[MemoryRecord]:
        return await self._memories.list_scoped(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            include_revoked=include_revoked,
        )

    async def remember(
        self,
        summary: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        memory_type: MemoryType = MemoryType.SEMANTIC,
        source_session_id: str | None = None,
    ) -> MemoryRecord:
        normalized = " ".join(summary.split())
        if not normalized:
            raise ValueError("Memory summary must not be empty")
        content_hash = hashlib.sha256(normalized.encode()).hexdigest()
        return await self._memories.upsert(
            MemoryCandidate(
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
                memory_type=memory_type,
                key=f"user_explicit:{content_hash[:32]}",
                summary=normalized,
                detail={
                    "source": "explicit_user_action",
                    "source_session_id": source_session_id,
                },
                confidence=1.0,
                provenance=[
                    Provenance(
                        source_type="user_explicit",
                        source_id=source_session_id or "workbench",
                        content_hash=content_hash,
                        trust=TrustLevel.USER_ASSERTED,
                    )
                ],
            )
        )

    async def revoke_memory(
        self,
        memory_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> bool:
        return await self._memories.revoke(
            memory_id,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )

    async def list_skills(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        status: SkillStatus | None = None,
    ) -> Sequence[SkillDefinition]:
        skills = [
            skill
            for skill in await self._skills.list_all()
            if skill.tenant_id == tenant_id and skill.project_id == project_id
        ]
        if status is not None:
            skills = [skill for skill in skills if skill.status == status]
        return sorted(skills, key=lambda skill: (skill.created_at, skill.name), reverse=True)

    async def transition_skill(
        self,
        skill_id: UUID,
        target_status: SkillStatus,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
        human_approved: bool = False,
    ) -> PromotionDecision:
        return await self._skill_evolution.transition_skill(
            skill_id,
            target_status,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
            human_approved=human_approved,
        )

    async def evaluate_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> SkillEvolutionResult:
        return await self._skill_evolution.evaluate_and_stage(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )

    async def list_skill_evolution(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> Sequence[SkillEvolutionSnapshot]:
        skills = await self.list_skills(tenant_id=tenant_id, project_id=project_id)
        return await self._skill_evolution.snapshots(skills)

    async def list_skill_evaluations(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillEvaluation]:
        return await self._skill_evolution.list_evaluations(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )

    async def list_skill_transitions(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillTransitionEvent]:
        return await self._skill_evolution.list_transitions(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )

    async def list_change_sets(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> Sequence[LearningChangeSet]:
        changes = [
            item
            for item in await self._change_sets.list_all()
            if item.scope.get("tenant_id", "local") == tenant_id
            and item.scope.get("project_id", "default") == project_id
        ]
        return sorted(changes, key=lambda item: item.created_at, reverse=True)

    async def list_hermes_native_learning(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> list[HermesNativeLearningAudit]:
        return await self._hermes_native_learning.list_audits(
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def hermes_native_learning_health(self) -> HermesNativeAdminHealth:
        return await self._hermes_native_learning.health()

    async def review_hermes_native_learning(
        self,
        change_set_id: UUID,
        decision: Literal["accept", "rollback"],
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        reviewer_id: str = "local-user",
        reason: str = "",
    ) -> HermesNativeLearningAudit:
        return await self._hermes_native_learning.review(
            change_set_id,
            decision,
            tenant_id=tenant_id,
            project_id=project_id,
            reviewer_id=reviewer_id,
            reason=reason,
        )

    async def graph_search(
        self,
        request: GraphSearchRequest,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> GraphSearchResult:
        return await self._integration.search_graph(
            request,
            self._graph_context(
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
            ),
        )

    async def resolve_graph_entities(
        self,
        request: GraphEntityResolveRequest,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> GraphEntityResolveResult:
        return await self._integration.resolve_graph_entities(
            request,
            self._graph_context(
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
            ),
        )

    async def retrieve_evidence_subgraph(
        self,
        request: GraphRAGRequest,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> GraphRAGResult:
        return await self._integration.retrieve_evidence_subgraph(
            request,
            self._graph_context(
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
            ),
        )

    async def compare_graph_entities(
        self,
        request: GraphEntityCompareRequest,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> GraphEntityCompareResult:
        return await self._integration.compare_graph_entities(
            request,
            self._graph_context(
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
            ),
        )

    async def list_graph_candidates(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        document_id: UUID | None = None,
        status: GraphCandidateStatus | None = None,
    ) -> GraphCandidateCollection:
        return await self._graph_candidates.list_candidates(
            tenant_id=tenant_id,
            project_id=project_id,
            document_id=document_id,
            status=status,
        )

    async def review_graph_entity(
        self,
        candidate_id: UUID,
        target_status: GraphCandidateStatus,
        *,
        reviewer_id: str,
        reason: str = "",
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> GraphEntityCandidate:
        return await self._graph_candidates.review_entity(
            candidate_id,
            target_status,
            reviewer_id=reviewer_id,
            reason=reason,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def review_graph_relation(
        self,
        candidate_id: UUID,
        target_status: GraphCandidateStatus,
        *,
        reviewer_id: str,
        reason: str = "",
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> GraphRelationCandidate:
        return await self._graph_candidates.review_relation(
            candidate_id,
            target_status,
            reviewer_id=reviewer_id,
            reason=reason,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def review_entity_resolution(
        self,
        candidate_id: UUID,
        target_status: GraphCandidateStatus,
        *,
        reviewer_id: str,
        reason: str = "",
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> EntityResolutionCandidate:
        return await self._graph_candidates.review_resolution(
            candidate_id,
            target_status,
            reviewer_id=reviewer_id,
            reason=reason,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    def capabilities(self) -> Sequence[CapabilitySpec]:
        capabilities = list(self._integration.registry.list_specs())
        if self._enterprise_fixture is not None:
            capabilities.append(
                CapabilitySpec(
                    name="enterprise_fixture_import",
                    version="1.0.0",
                    description=(
                        "Owner-only, idempotent import of the built-in enterprise "
                        "knowledge fixture."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"dry_run": {"type": "boolean"}},
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    effect=CapabilityEffect.WRITE,
                    idempotent=True,
                    provenance_required=False,
                )
            )
        return capabilities

    async def ingest_document(
        self,
        *,
        filename: str,
        content: bytes,
        media_type: str | None,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        source: KnowledgeSource | None = None,
    ) -> IngestionResult:
        return await self._ingestion.ingest(
            filename=filename,
            content=content,
            media_type=media_type,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            source=_user_upload_source(source),
        )

    async def submit_ingestion_job(
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
        return await self._require_ingestion_jobs().submit(
            filename=filename,
            content=content,
            media_type=media_type,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            source=_user_upload_source(source),
        )

    async def list_ingestion_jobs(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        limit: int = 100,
    ) -> Sequence[IngestionJob]:
        return await self._require_ingestion_jobs().list_jobs(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=limit,
        )

    async def get_ingestion_job(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> IngestionJob | None:
        return await self._require_ingestion_jobs().get(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def cancel_ingestion_job(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> IngestionJob:
        return await self._require_ingestion_jobs().cancel(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def retry_ingestion_job(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> IngestionJob:
        return await self._require_ingestion_jobs().retry(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    def _require_ingestion_jobs(self) -> IngestionJobService:
        if self._ingestion_jobs is None:
            raise IngestionJobsUnavailableError(
                "Async ingestion is not enabled for this deployment"
            )
        return self._ingestion_jobs

    def _require_enterprise_fixture(self) -> EnterpriseFixtureService:
        if self._enterprise_fixture is None:
            raise RuntimeError("Enterprise fixture import is disabled")
        return self._enterprise_fixture

    async def preview_enterprise_fixture(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> EnterpriseFixturePreview:
        return await self._require_enterprise_fixture().preview(
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def start_enterprise_fixture(
        self,
        *,
        tenant_id: str,
        project_id: str,
        requested_by: str,
        dry_run: bool = False,
    ) -> EnterpriseFixtureRun:
        return await self._require_enterprise_fixture().start(
            tenant_id=tenant_id,
            project_id=project_id,
            requested_by=requested_by,
            dry_run=dry_run,
        )

    async def enterprise_fixture_status(
        self,
        run_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> EnterpriseFixtureRun | None:
        return await self._require_enterprise_fixture().get_status(
            run_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def reset_enterprise_fixture(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> EnterpriseFixtureResetResult:
        return await self._require_enterprise_fixture().reset(
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def list_learning_jobs(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        limit: int = 100,
    ) -> Sequence[LearningJob]:
        return await self._require_learning_jobs().list_jobs(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=limit,
        )

    async def get_learning_job(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> LearningJob | None:
        return await self._require_learning_jobs().get(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def cancel_learning_job(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> LearningJob:
        return await self._require_learning_jobs().cancel(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def retry_learning_job(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> LearningJob:
        return await self._require_learning_jobs().retry(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    def _require_learning_jobs(self) -> LearningJobService:
        if self._learning_jobs is None:
            raise LearningJobsUnavailableError("Async learning is not enabled for this deployment")
        return self._learning_jobs

    async def list_documents(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = None,
        include_archived: bool = False,
    ) -> Sequence[KnowledgeDocument]:
        documents = await self._knowledge.list_documents(
            tenant_id=tenant_id,
            project_id=project_id,
            include_archived=include_archived,
        )
        profile = self._profile(tenant_id=tenant_id, project_id=project_id)
        if profile is None or user_id is None:
            return documents
        return tuple(
            document
            for document in documents
            if document_is_visible(
                document,
                user_id=user_id,
                enabled_layers=profile.enabled_knowledge_layers,
            )
        )

    async def get_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = None,
    ) -> KnowledgeDocument | None:
        document = await self._knowledge.get_document(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        profile = self._profile(tenant_id=tenant_id, project_id=project_id)
        if (
            document is not None
            and profile is not None
            and user_id is not None
            and not document_is_visible(
                document,
                user_id=user_id,
                enabled_layers=profile.enabled_knowledge_layers,
            )
        ):
            return None
        return document

    async def get_document_content(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = None,
    ) -> tuple[KnowledgeDocument, bytes] | None:
        document = await self.get_document(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )
        if document is None:
            return None
        content = await self._knowledge.read_content(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        return content

    async def archive_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = None,
    ) -> bool:
        if user_id is not None and await self.get_document(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        ) is None:
            return False
        return await self._ingestion.archive(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    def _profile(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> WorkspaceProfile | None:
        if self._workspace_profiles is None:
            return None
        return self._workspace_profiles.resolve(
            tenant_id=tenant_id,
            project_id=project_id,
        )

    def _graph_context(
        self,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> RunContext:
        profile = self._profile(tenant_id=tenant_id, project_id=project_id)
        return RunContext(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            enabled_knowledge_layers=(
                profile.enabled_knowledge_layers if profile is not None else None
            ),
            workspace_mode=profile.workspace_mode if profile is not None else None,
        )


def _user_upload_source(source: KnowledgeSource | None) -> KnowledgeSource | None:
    """Drop user-controlled provenance fields at the public upload boundary."""

    if source is None:
        return None
    protected = (
        source.source_type != "uploaded_document"
        or source.privacy != "private"
        or source.trust != TrustLevel.USER_ASSERTED
        or source.source_status != "active"
        or source.fixture_id is not None
        or source.owner is not None
        or source.last_reviewed_at is not None
        or source.effective_from is not None
        or source.effective_to is not None
        or source.supersedes_source_id is not None
        or source.superseded_by_source_id is not None
    )
    if protected:
        raise KnowledgeIngestionError("Upload provenance is assigned by the server")
    # A client may suggest a display title only. Source identity, privacy, trust,
    # and visibility are server-owned and are never copied from the request.
    return KnowledgeSource(title=source.title)
