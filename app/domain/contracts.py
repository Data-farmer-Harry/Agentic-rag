from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from app.domain.enums import DocumentStatus
from app.domain.models import (
    AnswerResponse,
    CalculationRequest,
    CalculationResult,
    ConversationMetadata,
    CurrentTimeRequest,
    CurrentTimeResult,
    DomainPackManifest,
    EntityResolutionCandidate,
    EvidenceRef,
    GraphCandidateReviewEvent,
    GraphEntityCandidate,
    GraphEntityCompareRequest,
    GraphEntityCompareResult,
    GraphEntityResolveRequest,
    GraphEntityResolveResult,
    GraphExtractionBatch,
    GraphRAGRequest,
    GraphRAGResult,
    GraphRelationCandidate,
    GraphSearchRequest,
    GraphSearchResult,
    IngestionJob,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSource,
    LearningChangeSet,
    LearningJob,
    LearningJobCheckpoint,
    LearningJobResult,
    MemoryCandidate,
    MemoryRecord,
    OutboxEvent,
    RetrievalBundle,
    RunContext,
    RunTrajectory,
    SkillDefinition,
    SkillEvaluation,
    SkillObservation,
    SkillTransitionEvent,
    VisionAnalysis,
    WebPageReadRequest,
    WebPageReadResult,
    WebSearchRequest,
    WebSearchResult,
    WorkspaceFileReadRequest,
    WorkspaceFileReadResult,
    WorkspaceListRequest,
    WorkspaceListResult,
    WorkspaceSearchRequest,
    WorkspaceSearchResult,
)
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


class AgentRuntime(Protocol):
    async def run(self, user_input: str, context: RunContext) -> AnswerResponse: ...


class AsyncLifecycle(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...


class RetrievalPort(Protocol):
    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> RetrievalBundle: ...


class GraphSearchPort(Protocol):
    async def search_graph(
        self,
        request: GraphSearchRequest,
        context: RunContext,
    ) -> GraphSearchResult: ...


class GraphEntityResolutionPort(Protocol):
    async def resolve_graph_entities(
        self,
        request: GraphEntityResolveRequest,
        context: RunContext,
    ) -> GraphEntityResolveResult: ...


class GraphRetrievalToolPort(Protocol):
    async def resolve_graph_entities(
        self,
        request: GraphEntityResolveRequest,
        context: RunContext,
    ) -> GraphEntityResolveResult: ...

    async def retrieve_evidence_subgraph(
        self,
        request: GraphRAGRequest,
        context: RunContext,
    ) -> GraphRAGResult: ...

    async def compare_graph_entities(
        self,
        request: GraphEntityCompareRequest,
        context: RunContext,
    ) -> GraphEntityCompareResult: ...


class WebSearchPort(Protocol):
    async def search_web(
        self,
        request: WebSearchRequest,
        context: RunContext,
    ) -> WebSearchResult: ...


class GeneralToolsPort(Protocol):
    async def read_web_page(
        self,
        request: WebPageReadRequest,
        context: RunContext,
    ) -> WebPageReadResult: ...

    async def calculate(
        self,
        request: CalculationRequest,
        context: RunContext,
    ) -> CalculationResult: ...

    async def current_time(
        self,
        request: CurrentTimeRequest,
        context: RunContext,
    ) -> CurrentTimeResult: ...


class ComputerWorkspacePort(Protocol):
    async def list_workspace_files(
        self,
        request: WorkspaceListRequest,
        context: RunContext,
    ) -> WorkspaceListResult: ...

    async def read_workspace_file(
        self,
        request: WorkspaceFileReadRequest,
        context: RunContext,
    ) -> WorkspaceFileReadResult: ...

    async def search_workspace_files(
        self,
        request: WorkspaceSearchRequest,
        context: RunContext,
    ) -> WorkspaceSearchResult: ...


class KnowledgeIndexPort(Protocol):
    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None: ...

    async def archive_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> None: ...


class KnowledgeVectorIndexPort(KnowledgeIndexPort, Protocol):
    pass


class KnowledgeGraphIndexPort(KnowledgeIndexPort, Protocol):
    pass


class VisionAnalyzerPort(Protocol):
    revision: str

    async def analyze(
        self,
        content: bytes,
        *,
        media_type: str,
        filename: str,
    ) -> VisionAnalysis: ...

    async def close(self) -> None: ...


class KnowledgeRepository(Protocol):
    async def ingest(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        raw_content: bytes,
    ) -> tuple[KnowledgeDocument, bool]: ...

    async def list_documents(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        include_archived: bool = False,
    ) -> Sequence[KnowledgeDocument]: ...

    async def find_by_hash(
        self,
        content_hash: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> KnowledgeDocument | None: ...

    async def get_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> KnowledgeDocument | None: ...

    async def enrich_source(
        self,
        document_id: UUID,
        source: KnowledgeSource,
        *,
        title: str | None = None,
        metadata: Mapping[str, object] | None = None,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> KnowledgeDocument | None: ...

    async def read_content(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> tuple[KnowledgeDocument, bytes] | None: ...

    async def list_chunks(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> Sequence[KnowledgeChunk]: ...

    async def replace_chunks(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        *,
        expected_parser_version: str,
    ) -> KnowledgeDocument: ...

    async def set_status(
        self,
        document_id: UUID,
        status: DocumentStatus,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        error: str | None = None,
    ) -> KnowledgeDocument | None: ...

    async def archive(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> bool: ...

    async def search(
        self,
        query: str,
        *,
        tenant_id: str,
        project_id: str,
        filters: Mapping[str, object] | None = None,
        user_id: str | None = None,
        enabled_knowledge_layers: Sequence[str] | None = None,
        top_k: int = 10,
    ) -> Sequence[EvidenceRef]: ...

    async def chunk_count(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> int: ...


class IngestionJobRepository(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def enqueue(self, job: IngestionJob) -> tuple[IngestionJob, bool]: ...

    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> IngestionJob | None: ...

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 100,
    ) -> Sequence[IngestionJob]: ...

    async def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> IngestionJob | None: ...

    async def renew_lease(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> bool: ...

    async def complete(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        document_id: UUID,
        deduplicated: bool,
    ) -> IngestionJob: ...

    async def fail(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: int,
    ) -> IngestionJob: ...

    async def cancel(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> IngestionJob | None: ...

    async def retry(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> IngestionJob | None: ...


class LearningJobRepository(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def enqueue(self, job: LearningJob) -> tuple[LearningJob, bool]: ...

    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningJob | None: ...

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 100,
    ) -> Sequence[LearningJob]: ...

    async def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> LearningJob | None: ...

    async def renew_lease(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        lease_seconds: int,
    ) -> bool: ...

    async def save_checkpoint(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        checkpoint: LearningJobCheckpoint,
    ) -> LearningJob: ...

    async def commit_stage(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        operation: Callable[[], Awaitable[LearningJobCheckpoint]],
    ) -> LearningJob: ...

    async def complete(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        result: LearningJobResult,
    ) -> LearningJob: ...

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
    ) -> LearningJob: ...

    async def cancel(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningJob | None: ...

    async def retry(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningJob | None: ...


class HarnessExperienceRepository(Protocol):
    async def save(
        self,
        experience: HarnessExperienceEntry,
    ) -> HarnessExperienceEntry: ...

    async def get(
        self,
        experience_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> HarnessExperienceEntry | None: ...

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 100,
        learnable: bool | None = None,
        success: bool | None = None,
    ) -> Sequence[HarnessExperienceEntry]: ...

    async def list_for_run(
        self,
        run_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> Sequence[HarnessExperienceEntry]: ...

    async def save_evaluation(
        self,
        evaluation: HarnessExperienceEvaluation,
    ) -> HarnessExperienceEvaluation: ...

    async def get_evaluation(
        self,
        evaluation_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> HarnessExperienceEvaluation | None: ...

    async def list_evaluations(
        self,
        experience_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> Sequence[HarnessExperienceEvaluation]: ...


class HarnessPolicyRepository(Protocol):
    async def save_pattern(self, pattern: HarnessPattern) -> HarnessPattern: ...

    async def get_pattern(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        version: str | None = None,
    ) -> HarnessPattern | None: ...

    async def list_patterns(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 100,
        status: HarnessPatternStatus | None = None,
    ) -> Sequence[HarnessPattern]: ...

    async def save_pattern_evaluation(
        self,
        evaluation: HarnessPatternEvaluation,
    ) -> HarnessPatternEvaluation: ...

    async def list_pattern_evaluations(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternEvaluation]: ...

    async def save_pattern_promotion_evidence(
        self,
        evidence: HarnessPatternPromotionEvidence,
    ) -> HarnessPatternPromotionEvidence: ...

    async def list_pattern_promotion_evidence(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternPromotionEvidence]: ...

    async def save_pattern_transition(
        self,
        transition: HarnessPatternTransition,
    ) -> HarnessPatternTransition: ...

    async def list_pattern_transitions(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternTransition]: ...

    async def save_overlay(self, overlay: RunHarnessOverlay) -> RunHarnessOverlay: ...

    async def get_overlay(
        self,
        run_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> RunHarnessOverlay | None: ...


class OutboxRepository(Protocol):
    async def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> OutboxEvent | None: ...

    async def renew_lease(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> bool: ...

    async def mark_published(
        self,
        event_id: UUID,
        *,
        worker_id: str,
    ) -> OutboxEvent: ...

    async def fail(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: int,
    ) -> OutboxEvent: ...

    async def count_unpublished(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> int: ...


class EntityRelationExtractorPort(Protocol):
    async def extract(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        *,
        domain_pack: str = "general",
    ) -> GraphExtractionBatch: ...


class EntityResolverPort(Protocol):
    async def propose(
        self,
        batch: GraphExtractionBatch,
        existing_entities: Sequence[GraphEntityCandidate],
    ) -> Sequence[EntityResolutionCandidate]: ...


class SemanticGraphIndexPort(Protocol):
    async def index_extraction(self, batch: GraphExtractionBatch) -> None: ...

    async def index_resolutions(self, candidates: Sequence[EntityResolutionCandidate]) -> None: ...

    async def set_entity_status(self, candidate: GraphEntityCandidate) -> None: ...

    async def set_relation_status(self, candidate: GraphRelationCandidate) -> None: ...

    async def set_resolution_status(self, candidate: EntityResolutionCandidate) -> None: ...


class GraphCandidateRepository(Protocol):
    async def save_batch(self, batch: GraphExtractionBatch) -> GraphExtractionBatch: ...

    async def list_entities(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        document_id: UUID | None = None,
        status: str | None = None,
    ) -> Sequence[GraphEntityCandidate]: ...

    async def list_relations(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        document_id: UUID | None = None,
        status: str | None = None,
    ) -> Sequence[GraphRelationCandidate]: ...

    async def save_resolutions(
        self, candidates: Sequence[EntityResolutionCandidate]
    ) -> Sequence[EntityResolutionCandidate]: ...

    async def list_resolutions(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        document_id: UUID | None = None,
        status: str | None = None,
    ) -> Sequence[EntityResolutionCandidate]: ...

    async def get_entity(
        self,
        candidate_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> GraphEntityCandidate | None: ...

    async def get_relation(
        self,
        candidate_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> GraphRelationCandidate | None: ...

    async def get_resolution(
        self,
        candidate_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> EntityResolutionCandidate | None: ...

    async def save_entity(self, candidate: GraphEntityCandidate) -> GraphEntityCandidate: ...

    async def save_relation(self, candidate: GraphRelationCandidate) -> GraphRelationCandidate: ...

    async def save_resolution(
        self, candidate: EntityResolutionCandidate
    ) -> EntityResolutionCandidate: ...

    async def save_review(self, review: GraphCandidateReviewEvent) -> GraphCandidateReviewEvent: ...

    async def list_reviews(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        candidate_id: UUID | None = None,
    ) -> Sequence[GraphCandidateReviewEvent]: ...

    async def archive_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> None: ...


class LearningChangeSetRepository(Protocol):
    async def save(self, change_set: LearningChangeSet) -> LearningChangeSet: ...

    async def list_for_run(self, run_id: UUID) -> Sequence[LearningChangeSet]: ...

    async def list_all(self) -> Sequence[LearningChangeSet]: ...


class SkillEvaluationRepository(Protocol):
    async def save(self, evaluation: SkillEvaluation) -> SkillEvaluation: ...

    async def list_for_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillEvaluation]: ...

    async def latest(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> SkillEvaluation | None: ...

    async def list_all(self) -> Sequence[SkillEvaluation]: ...


class SkillObservationRepository(Protocol):
    async def save(self, observation: SkillObservation) -> SkillObservation: ...

    async def list_for_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
        cohort: str | None = None,
    ) -> Sequence[SkillObservation]: ...

    async def list_all(self) -> Sequence[SkillObservation]: ...


class SkillTransitionRepository(Protocol):
    async def save(self, transition: SkillTransitionEvent) -> SkillTransitionEvent: ...

    async def list_for_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillTransitionEvent]: ...

    async def list_all(self) -> Sequence[SkillTransitionEvent]: ...


class MemoryRepository(Protocol):
    async def upsert(self, candidate: MemoryCandidate) -> MemoryRecord: ...

    async def search(
        self,
        query: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = "local-user",
        limit: int = 10,
    ) -> Sequence[MemoryRecord]: ...

    async def revoke(
        self,
        memory_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = "local-user",
    ) -> bool: ...

    async def list_all(
        self,
        *,
        include_revoked: bool = False,
    ) -> Sequence[MemoryRecord]: ...

    async def list_scoped(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = "local-user",
        include_revoked: bool = False,
    ) -> Sequence[MemoryRecord]: ...


class SkillRepository(Protocol):
    async def save(self, skill: SkillDefinition) -> SkillDefinition: ...

    async def list_by_status(
        self,
        status: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> Sequence[SkillDefinition]: ...

    async def get(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        version: str | None = None,
    ) -> SkillDefinition | None: ...

    async def get_by_name(
        self,
        name: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        version: str | None = None,
    ) -> SkillDefinition | None: ...

    async def list_all(self) -> Sequence[SkillDefinition]: ...


class TrajectoryRepository(Protocol):
    async def save(self, trajectory: RunTrajectory) -> None: ...

    async def get(self, run_id: UUID) -> RunTrajectory | None: ...

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> RunTrajectory | None: ...

    async def find_similar(
        self, trajectory: RunTrajectory, *, limit: int = 20
    ) -> Sequence[RunTrajectory]: ...

    async def list_recent(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        limit: int = 50,
    ) -> Sequence[RunTrajectory]: ...

    async def list_session(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        session_id: str = "default",
        limit: int = 50,
    ) -> Sequence[RunTrajectory]: ...


class ConversationRepository(Protocol):
    async def save(self, metadata: ConversationMetadata) -> None: ...

    async def get(
        self,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
        session_id: str,
    ) -> ConversationMetadata | None: ...

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> Sequence[ConversationMetadata]: ...


class DomainPack(Protocol):
    name: str

    def manifest(self) -> DomainPackManifest: ...

    def system_context(self) -> str: ...

    def graph_templates(self) -> dict[str, str]: ...

    def output_schemas(self) -> dict[str, type[Any]]: ...
