from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.agent.adaptive_rag_router import (
    ConversationRoutedRuntime,
    OpenAIConversationResponder,
)
from app.agent.context_engine import ContextEngine
from app.agent.hermes_bridge import HermesCapabilityBridge
from app.agent.hermes_native_learning import (
    HermesNativeAdminClient,
    HermesNativeLearningService,
)
from app.agent.hermes_runtime import HermesAgentRuntime
from app.agent.offline_runtime import OfflineAgentRuntime
from app.agent.workspace_file_tools import WorkspaceFileTools
from app.application.run_event_recorder import RunEventRecorder
from app.application.run_service import RunService
from app.application.workspace_service import WorkspaceService
from app.capabilities.agent_tool_runtime import AgentToolRuntime
from app.capabilities.general_tools import GeneralToolService
from app.config import Settings, get_settings
from app.demo.enterprise_fixture import EnterpriseFixtureService
from app.domain.contracts import (
    AgentRuntime,
    AsyncLifecycle,
    EntityRelationExtractorPort,
    GraphSearchPort,
    HarnessExperienceRepository,
    HarnessPolicyRepository,
    KnowledgeGraphIndexPort,
    KnowledgeRepository,
    KnowledgeVectorIndexPort,
    LearningChangeSetRepository,
    MemoryRepository,
    OutboxRepository,
    SemanticGraphIndexPort,
    SkillEvaluationRepository,
    SkillObservationRepository,
    SkillRepository,
    SkillTransitionRepository,
    VisionAnalyzerPort,
    WebSearchPort,
)
from app.domain.enums import TrustLevel
from app.domain.models import (
    EvidenceRef,
    GraphNode,
    GraphRelationship,
    Provenance,
    RunContext,
    RunTrajectory,
)
from app.domain_packs.registry import DomainPackRegistry
from app.graph.entity_resolution import DeterministicEntityResolver
from app.graph.graph_candidate_repository import JsonGraphCandidateRepository
from app.graph.graph_candidate_service import (
    GraphCandidateService,
    KnowledgeGraphIngestionCoordinator,
)
from app.graph.graph_extraction_pipeline import RuleBasedEntityRelationExtractor
from app.graph.graph_visibility import VisibilityFilteredGraph
from app.graph.in_memory_evidence_graph import InMemoryEvidenceGraph
from app.graph.openai_graph_extractor import (
    HybridEntityRelationExtractor,
    OpenAIStructuredEntityRelationExtractor,
)
from app.harness.consumer import BoundedHarnessConsumer, HarnessConsumerLimits
from app.harness.evaluation import DeterministicPatternEvaluator
from app.harness.evolution import HarnessPatternEvolutionService
from app.harness.experience import HarnessExperienceService
from app.harness.mining import DeterministicPatternMiner
from app.harness.models import HarnessOverlayMode
from app.harness.repository import (
    JsonHarnessExperienceRepository,
    JsonHarnessPolicyRepository,
)
from app.harness.selector import HarnessOverlaySelector
from app.infra.local_repositories import (
    JsonlConversationRepository,
    JsonlTrajectoryRepository,
)
from app.knowledge.ingestion_jobs import IngestionJobService, IngestionStagingStore
from app.knowledge.knowledge_base_retriever import KnowledgeBaseRetriever
from app.knowledge.knowledge_ingestion import KnowledgeIngestionService
from app.knowledge.knowledge_repository import JsonKnowledgeRepository
from app.knowledge.knowledge_visibility import SettingsWorkspaceProfileResolver
from app.learning.change_set import JsonLearningChangeSetRepository
from app.learning.engine import LearningEngine
from app.learning.evolution import SkillEvolutionService
from app.learning.jobs import LearningJobService, LearningTrigger, LearningWorkflowProcessor
from app.learning.openai_reflection import OpenAIStructuredExperienceReflector
from app.learning.refinement import SkillRefiner
from app.learning.reflection import (
    DeterministicExperienceReflector,
    ExperienceReflector,
)
from app.learning.skill_evaluation_store import (
    JsonSkillEvaluationRepository,
    JsonSkillObservationRepository,
)
from app.learning.skill_evaluator import DeterministicSkillEvaluator
from app.learning.skill_miner import RepeatedTrajectorySkillMiner
from app.learning.skill_replay import FrozenCapabilitySkillSandbox
from app.learning.transition_store import JsonSkillTransitionRepository
from app.memory.json_memory_repository import JsonMemoryStore
from app.personal import (
    JsonPersonalRepository,
    PersonalControlService,
    PersonalRepository,
)
from app.retrieval.agentic_retrieval import (
    AgenticRetrievalController,
    DeterministicQueryPlanner,
    OpenAIStructuredQueryPlanner,
)
from app.retrieval.hybrid_retrieval_pipeline import RetrievalPipeline
from app.retrieval.in_memory_retriever import InMemoryRetriever
from app.skills.skill_markdown_repository import SkillMarkdownRepository
from app.skills.skill_registry import skill_is_eligible
from app.web_search import DuckDuckGoWebSearch, FallbackWebSearch, OpenAIHostedWebSearch


@dataclass(frozen=True)
class ApplicationComponents:
    settings: Settings
    run_service: RunService
    learning_engine: LearningEngine
    skill_evolution_service: SkillEvolutionService
    learning_reflector: ExperienceReflector
    memory_store: MemoryRepository
    skill_repository: SkillRepository
    trajectory_repository: JsonlTrajectoryRepository
    integration_runtime: AgentToolRuntime
    change_set_repository: LearningChangeSetRepository
    workspace_service: WorkspaceService
    knowledge_repository: KnowledgeRepository
    ingestion_service: KnowledgeIngestionService
    ingestion_job_service: IngestionJobService | None
    enterprise_fixture_service: EnterpriseFixtureService | None
    learning_job_service: LearningJobService | None
    harness_experience_repository: HarnessExperienceRepository
    harness_policy_repository: HarnessPolicyRepository
    harness_experience_service: HarnessExperienceService | None
    harness_pattern_evolution_service: HarnessPatternEvolutionService
    vector_index: KnowledgeVectorIndexPort | None
    graph_backend: GraphSearchPort
    graph_structural_index: KnowledgeGraphIndexPort | None
    graph_extractor: EntityRelationExtractorPort
    vision_analyzer: VisionAnalyzerPort | None
    graph_candidate_repository: JsonGraphCandidateRepository
    graph_candidate_service: GraphCandidateService
    graph_enrichment_coordinator: KnowledgeGraphIngestionCoordinator
    hermes_native_learning_service: HermesNativeLearningService
    personal_service: PersonalControlService
    hermes_bridge: HermesCapabilityBridge | None = None
    lifecycle_resources: tuple[AsyncLifecycle, ...] = ()

    async def start(self) -> None:
        started: list[AsyncLifecycle] = []
        try:
            for resource in self.lifecycle_resources:
                await resource.start()
                started.append(resource)
            if self.ingestion_job_service is not None:
                await self.ingestion_job_service.start()
            if self.learning_job_service is not None:
                await self.learning_job_service.start()
        except BaseException:
            if self.ingestion_job_service is not None:
                await self.ingestion_job_service.close()
            for resource in reversed(started):
                await resource.close()
            raise

    async def close(self) -> None:
        if self.learning_job_service is not None:
            await self.learning_job_service.close()
        if self.ingestion_job_service is not None:
            await self.ingestion_job_service.close()
        for resource in reversed(self.lifecycle_resources):
            await resource.close()
        seen: set[int] = set()
        for backend in (
            self.integration_runtime,
            self.vector_index,
            self.graph_backend,
            self.graph_extractor,
            self.vision_analyzer,
            self.learning_reflector,
        ):
            if backend is None or id(backend) in seen:
                continue
            seen.add(id(backend))
            close = getattr(backend, "close", None)
            if close is None:
                continue
            result = close()
            if inspect.isawaitable(result):
                await result


def _build_vector_index(
    settings: Settings,
    *,
    workspace_profiles: SettingsWorkspaceProfileResolver | None = None,
) -> Any | None:
    if settings.retrieval_backend == "local":
        return None
    from qdrant_client import AsyncQdrantClient

    from app.agent.model_provider import build_embedding_client
    from app.retrieval.embedding_providers import (
        DeterministicDenseEmbedder,
        OpenAIDenseEmbedder,
        build_sparse_embedder,
    )
    from app.retrieval.qdrant_hybrid_retriever import QdrantHybridStore

    if settings.qdrant_url == ":memory:":
        client = AsyncQdrantClient(location=":memory:")
        create_payload_indexes = False
    else:
        client = AsyncQdrantClient(
            url=settings.qdrant_url,
            api_key=(
                settings.qdrant_api_key.get_secret_value()
                if settings.qdrant_api_key is not None
                else None
            ),
            timeout=min(settings.agent_timeout_seconds, 60),
        )
        create_payload_indexes = True
    dense: Any
    if settings.embedding_provider == "deterministic":
        dense = DeterministicDenseEmbedder(settings.embedding_dimensions)
    else:
        embedding_client = build_embedding_client(
            settings,
            timeout=min(settings.agent_timeout_seconds, 60),
        )
        dense = OpenAIDenseEmbedder(
            embedding_client,
            model=settings.embedding_model,
            dimension=settings.embedding_dimensions,
        )
    return QdrantHybridStore(
        client,
        dense,
        build_sparse_embedder(
            settings.qdrant_sparse_encoder,
            bm25_k1=settings.qdrant_bm25_k1,
            bm25_b=settings.qdrant_bm25_b,
            bm25_average_document_tokens=(settings.qdrant_bm25_average_document_tokens),
        ),
        collection_name=settings.qdrant_collection,
        prefetch_limit=settings.qdrant_prefetch_limit,
        rrf_k=settings.qdrant_rrf_k,
        create_payload_indexes=create_payload_indexes,
        use_sparse_idf=settings.qdrant_sparse_idf,
        workspace_profiles=workspace_profiles,
    )


def _build_graph_extractor(settings: Settings) -> EntityRelationExtractorPort:
    rule_extractor = RuleBasedEntityRelationExtractor(
        max_entities=settings.graph_extraction_max_entities,
        max_relations=settings.graph_extraction_max_relations,
    )
    if settings.graph_extractor_mode == "rule":
        return rule_extractor

    from app.agent.model_provider import build_model_client

    client = build_model_client(
        settings,
        timeout=settings.graph_extraction_timeout_seconds,
        max_retries=2,
    )
    structured_extractor = OpenAIStructuredEntityRelationExtractor(
        client,
        model=settings.graph_extraction_model or settings.openai_model,
        max_batch_chars=settings.graph_extraction_max_batch_chars,
        window_max_chars=settings.graph_extraction_window_max_chars,
        window_max_chunks=settings.graph_extraction_window_max_chunks,
        window_overlap_chunks=settings.graph_extraction_window_overlap_chunks,
        max_output_tokens=settings.graph_extraction_max_output_tokens,
        max_entities=settings.graph_extraction_max_entities,
        max_relations=settings.graph_extraction_max_relations,
    )
    if settings.graph_extractor_mode == "openai":
        return structured_extractor
    return HybridEntityRelationExtractor([rule_extractor, structured_extractor])


def _build_vision_analyzer(settings: Settings) -> VisionAnalyzerPort | None:
    if not settings.vision_enabled:
        return None
    from app.agent.model_provider import build_model_client
    from app.knowledge.openai_vision_analyzer import OpenAIVisionAnalyzer

    client = build_model_client(
        settings,
        timeout=min(settings.agent_timeout_seconds, 180),
        max_retries=2,
    )
    return OpenAIVisionAnalyzer(
        client,
        model=settings.vision_model or settings.openai_model,
        detail=settings.vision_detail,
        max_output_tokens=settings.vision_max_output_tokens,
        max_regions=settings.vision_max_regions,
    )


def _build_retrieval_planner(settings: Settings) -> Any:
    if settings.retrieval_planner_mode == "deterministic":
        return DeterministicQueryPlanner(max_subqueries=settings.retrieval_max_subqueries)
    from app.agent.model_provider import build_model_client

    client = build_model_client(
        settings,
        timeout=settings.retrieval_planner_timeout_seconds,
        max_retries=0,
    )
    return OpenAIStructuredQueryPlanner(
        client,
        model=settings.retrieval_planner_model or settings.openai_model,
        max_output_tokens=settings.retrieval_planner_max_output_tokens,
    )


def _build_learning_reflector(settings: Settings) -> ExperienceReflector:
    if settings.learning_reflector_mode == "deterministic":
        return DeterministicExperienceReflector()
    from app.agent.model_provider import build_model_client

    client = build_model_client(
        settings,
        timeout=settings.learning_reflection_timeout_seconds,
        max_retries=1,
    )
    return OpenAIStructuredExperienceReflector(
        client,
        model=settings.learning_reflection_model or settings.openai_model,
        max_output_tokens=settings.learning_reflection_max_output_tokens,
        timeout_seconds=settings.learning_reflection_timeout_seconds,
        max_input_chars=settings.learning_reflection_max_input_chars,
        min_memory_confidence=settings.learning_reflection_min_memory_confidence,
        trigger_mode=settings.learning_reflection_trigger_mode,
    )


def _build_graph_backend(
    settings: Settings,
    texts: list[str],
    metadata: list[dict[str, str]],
) -> GraphSearchPort:
    if settings.graph_backend == "local":
        return _build_reference_graph(texts, metadata)
    from neo4j import AsyncGraphDatabase

    from app.graph.neo4j_evidence_graph import Neo4jEvidenceGraph

    auth: tuple[str, str] | None = None
    if settings.neo4j_user and settings.neo4j_password is not None:
        auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
    if settings.neo4j_uri is None:
        raise ValueError("NEO4J_URI is required for the neo4j graph backend")
    driver = AsyncGraphDatabase.driver(settings.neo4j_uri, auth=auth)
    return Neo4jEvidenceGraph(
        driver,
        database=settings.neo4j_database,
        timeout_seconds=min(settings.agent_timeout_seconds, 60),
    )


def _read_reference_documents() -> tuple[list[str], list[dict[str, str]]]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=2_000, chunk_overlap=200)
    texts: list[str] = []
    metadata: list[dict[str, str]] = []
    project_root = Path(__file__).resolve().parent.parent
    for relative_path in (
        Path("docs/INTENT.md"),
        Path("docs/PRD.md"),
        Path("docs/TECHNICAL_DESIGN.md"),
    ):
        path = project_root / relative_path
        if not path.exists():
            continue
        for index, chunk in enumerate(splitter.split_text(path.read_text(encoding="utf-8"))):
            texts.append(chunk)
            metadata.append(
                {
                    "source_id": f"{relative_path}:{index}",
                    "source_type": "project_document",
                    "title": path.name,
                    "tenant_id": "local",
                    "project_id": "default",
                    "user_id": "system",
                    "knowledge_layer": "team_internal",
                    "source_status": "active",
                }
            )
    return texts, metadata


def _build_reference_graph(
    texts: list[str],
    metadata: list[dict[str, str]],
) -> InMemoryEvidenceGraph:
    def evidence_for(*terms: str) -> EvidenceRef:
        selected_index = next(
            (
                index
                for index, text in enumerate(texts)
                if all(term.casefold() in text.casefold() for term in terms)
            ),
            0,
        )
        selected_text = texts[selected_index] if texts else "Local project architecture reference."
        selected_metadata = (
            metadata[selected_index]
            if metadata
            else {
                "source_id": "bootstrap",
                "source_type": "project_document",
                "title": "Bootstrap",
            }
        )
        return EvidenceRef(
            text=selected_text[:8_000],
            title=selected_metadata.get("title"),
            score=1.0,
            provenance=Provenance(
                source_type=selected_metadata.get("source_type", "project_document"),
                source_id=selected_metadata.get("source_id", "bootstrap"),
                trust=TrustLevel.VERIFIED,
            ),
            metadata={
                "tenant_id": selected_metadata.get("tenant_id", "local"),
                "project_id": selected_metadata.get("project_id", "default"),
                "user_id": selected_metadata.get("user_id", "system"),
                "knowledge_layer": selected_metadata.get(
                    "knowledge_layer",
                    "team_internal",
                ),
                "source_status": selected_metadata.get("source_status", "active"),
            },
        )

    architecture_evidence = evidence_for("Hermes", "LangChain")
    learning_evidence = evidence_for("Skill")
    node_names = {
        "hermes_agent": ("Runtime", "Hermes Agent"),
        "integration_runtime": ("Runtime", "LangChain Integration Runtime"),
        "knowledge_retrieval": ("Capability", "Knowledge Retrieval"),
        "knowledge_graph": ("Capability", "Knowledge Graph"),
        "memory": ("LearningArtifact", "Long-term Memory"),
        "skills": ("LearningArtifact", "Declarative Skills"),
        "learning_engine": ("ControlPlane", "Learning Engine"),
    }
    nodes = [
        GraphNode(
            node_id=node_id,
            tenant_id="local",
            project_id="default",
            label=label,
            name=name,
            provenance=[architecture_evidence.provenance],
        )
        for node_id, (label, name) in node_names.items()
    ]

    def relationship(
        relationship_id: str,
        source: str,
        target: str,
        relation_type: str,
        evidence: EvidenceRef,
    ) -> GraphRelationship:
        return GraphRelationship(
            relationship_id=relationship_id,
            tenant_id="local",
            project_id="default",
            relation_type=relation_type,
            source_node_id=source,
            target_node_id=target,
            evidence=[evidence],
        )

    relationships = [
        relationship(
            "hermes_delegates_integration",
            "hermes_agent",
            "integration_runtime",
            "delegates_capabilities_to",
            architecture_evidence,
        ),
        relationship(
            "integration_orchestrates_retrieval",
            "integration_runtime",
            "knowledge_retrieval",
            "orchestrates",
            architecture_evidence,
        ),
        relationship(
            "integration_orchestrates_graph",
            "integration_runtime",
            "knowledge_graph",
            "orchestrates",
            architecture_evidence,
        ),
        relationship(
            "learning_proposes_memory",
            "learning_engine",
            "memory",
            "proposes",
            learning_evidence,
        ),
        relationship(
            "learning_mines_skills",
            "learning_engine",
            "skills",
            "mines",
            learning_evidence,
        ),
    ]
    return InMemoryEvidenceGraph(nodes, relationships)


def build_components(settings: Settings | None = None) -> ApplicationComponents:
    resolved = settings or get_settings()
    resolved.data_dir.mkdir(parents=True, exist_ok=True)
    workspace_profiles = SettingsWorkspaceProfileResolver(resolved)
    domain_packs = DomainPackRegistry()
    texts, metadata = _read_reference_documents()
    local_retriever = InMemoryRetriever.from_texts(
        texts,
        source_type="project_document",
        metadatas=metadata,
        trust=TrustLevel.VERIFIED,
        min_score=0.25,
    )
    legacy_knowledge = JsonKnowledgeRepository(resolved.data_dir / "knowledge")
    knowledge: KnowledgeRepository = legacy_knowledge
    lifecycle_resources_list: list[AsyncLifecycle] = []
    postgres: Any | None = None
    postgres_migrations: list[Any] = []
    needs_postgres = (
        resolved.learning_job_mode == "async"
        or resolved.ingestion_mode == "async"
        or resolved.knowledge_repository_backend == "postgres"
        or resolved.learning_artifact_backend == "postgres"
    )
    if needs_postgres:
        if resolved.postgres_dsn is None:
            raise ValueError("POSTGRES_DSN is required for configured Postgres features")
        from app.infra.postgres import PostgresDatabase

        postgres = PostgresDatabase(
            resolved.postgres_dsn,
            command_timeout_seconds=min(resolved.agent_timeout_seconds, 60),
        )
    knowledge_migrator: AsyncLifecycle | None = None
    outbox_repository: OutboxRepository | None = None
    outbox_dispatcher: AsyncLifecycle | None = None
    if resolved.knowledge_repository_backend == "postgres":
        from app.infra.postgres_knowledge import (
            KNOWLEDGE_MIGRATIONS,
            LegacyKnowledgeMetadataMigrator,
            PostgresKnowledgeRepository,
        )
        from app.knowledge.knowledge_repository import FileKnowledgeObjectStore

        if postgres is None:
            raise ValueError("Postgres knowledge backend requires a database")
        postgres_migrations.extend(KNOWLEDGE_MIGRATIONS)
        postgres_knowledge = PostgresKnowledgeRepository(
            postgres,
            FileKnowledgeObjectStore(resolved.data_dir / "knowledge"),
        )
        knowledge = postgres_knowledge
        knowledge_migrator = LegacyKnowledgeMetadataMigrator(
            legacy_knowledge,
            postgres_knowledge,
        )
        from app.infra.postgres_outbox import PostgresOutboxRepository

        outbox_repository = PostgresOutboxRepository(postgres)
    vector_index = _build_vector_index(resolved, workspace_profiles=workspace_profiles)
    knowledge_retriever: Any = (
        vector_index
        if vector_index is not None
        else KnowledgeBaseRetriever(knowledge, workspace_profiles=workspace_profiles)
    )
    knowledge_branch = "qdrant_hybrid" if vector_index is not None else "knowledge_base"
    retrieval_pipeline = RetrievalPipeline(
        {
            "builtin_lexical": local_retriever,
            knowledge_branch: knowledge_retriever,
        },
        branch_weights={"builtin_lexical": 0.35, knowledge_branch: 1.0},
        min_relative_score=0.45,
        workspace_profiles=workspace_profiles,
    )
    retrieval: Any = retrieval_pipeline
    if resolved.agentic_retrieval_enabled:
        retrieval = AgenticRetrievalController(
            retrieval_pipeline,
            planner=_build_retrieval_planner(resolved),
            max_rounds=resolved.retrieval_max_rounds,
            max_subqueries=resolved.retrieval_max_subqueries,
            rrf_k=resolved.qdrant_rrf_k,
        )
    raw_graph = _build_graph_backend(resolved, texts, metadata)
    structural_graph_index = (
        cast(KnowledgeGraphIndexPort, raw_graph) if resolved.graph_backend == "neo4j" else None
    )
    semantic_graph_index = (
        cast(SemanticGraphIndexPort, raw_graph) if resolved.graph_backend == "neo4j" else None
    )
    graph = VisibilityFilteredGraph(raw_graph)
    workspace_default_pack = workspace_profiles.resolve(
        tenant_id=resolved.api_tenant_id,
        project_id="default",
    ).default_domain_pack
    domain_packs.get(workspace_default_pack)
    graph_candidates = JsonGraphCandidateRepository(resolved.data_dir / "graph_candidates.json")
    graph_candidate_service = GraphCandidateService(
        graph_candidates,
        semantic_index=semantic_graph_index,
    )
    graph_extractor = _build_graph_extractor(resolved)
    graph_enrichment = KnowledgeGraphIngestionCoordinator(
        graph_extractor,
        graph_candidates,
        entity_resolver=DeterministicEntityResolver(),
        semantic_index=semantic_graph_index,
        max_extraction_chars=resolved.graph_extraction_input_char_budget,
        public_reference_max_extraction_chars=(
            resolved.graph_extraction_public_reference_char_budget
        ),
        domain_pack=workspace_default_pack,
    )
    durable_graph_enrichment = outbox_repository is not None and resolved.ingestion_mode == "async"
    inline_graph_index: KnowledgeGraphIndexPort | None = None
    if not durable_graph_enrichment:
        inline_graph_index = KnowledgeGraphIngestionCoordinator(
            graph_extractor,
            graph_candidates,
            entity_resolver=DeterministicEntityResolver(),
            structural_index=structural_graph_index,
            semantic_index=semantic_graph_index,
            max_extraction_chars=resolved.graph_extraction_input_char_budget,
            public_reference_max_extraction_chars=(
                resolved.graph_extraction_public_reference_char_budget
            ),
            domain_pack=workspace_default_pack,
        )
    ingestion_graph_index = (
        structural_graph_index if durable_graph_enrichment else inline_graph_index
    )
    vision_analyzer = _build_vision_analyzer(resolved)
    ingestion = KnowledgeIngestionService(
        knowledge,
        max_file_bytes=resolved.max_upload_bytes,
        chunk_size=resolved.knowledge_chunk_size,
        chunk_overlap=resolved.knowledge_chunk_overlap,
        max_pdf_pages=resolved.knowledge_max_pdf_pages,
        max_extracted_chars=resolved.knowledge_max_extracted_chars,
        max_chunks=resolved.knowledge_max_chunks,
        max_image_pixels=resolved.vision_max_pixels,
        max_image_dimension=resolved.vision_max_dimension,
        vision_analyzer=vision_analyzer,
        vector_index=vector_index,
        graph_index=ingestion_graph_index,
        workspace_profiles=workspace_profiles,
    )
    if outbox_repository is not None and resolved.outbox_dispatcher_enabled:
        from app.infra.outbox_dispatcher import (
            AuditLogOutboxPublisher,
            CompositeOutboxPublisher,
            KnowledgeGraphEnrichmentPublisher,
            OutboxDispatcher,
            OutboxPublisher,
        )

        publishers: list[OutboxPublisher] = [AuditLogOutboxPublisher()]
        if durable_graph_enrichment:
            publishers.insert(
                0,
                KnowledgeGraphEnrichmentPublisher(knowledge, graph_enrichment),
            )
        outbox_dispatcher = OutboxDispatcher(
            outbox_repository,
            CompositeOutboxPublisher(publishers),
            lease_seconds=resolved.outbox_lease_seconds,
            poll_seconds=resolved.outbox_poll_seconds,
            retry_base_seconds=resolved.outbox_retry_base_seconds,
        )
    ingestion_jobs: IngestionJobService | None = None
    learning_job_repository: Any | None = None
    if resolved.ingestion_mode == "async":
        from app.infra.postgres_ingestion_jobs import (
            INGESTION_JOB_MIGRATIONS,
            PostgresIngestionJobRepository,
        )

        if postgres is None:
            raise ValueError("Async ingestion requires a Postgres database")
        postgres_migrations.extend(INGESTION_JOB_MIGRATIONS)

        ingestion_jobs = IngestionJobService(
            PostgresIngestionJobRepository(
                postgres,
                emit_outbox=outbox_repository is not None,
            ),
            IngestionStagingStore(
                resolved.data_dir / "ingestion_staging",
                max_file_bytes=resolved.max_upload_bytes,
            ),
            ingestion,
            max_attempts=resolved.ingestion_job_max_attempts,
            lease_seconds=resolved.ingestion_job_lease_seconds,
            poll_seconds=resolved.ingestion_job_poll_seconds,
            retry_base_seconds=resolved.ingestion_job_retry_base_seconds,
            worker_enabled=resolved.ingestion_worker_enabled,
        )
    enterprise_fixture = (
        EnterpriseFixtureService(
            root=resolved.enterprise_fixture_root,
            data_dir=resolved.data_dir,
            knowledge_repository=knowledge,
            ingestion=ingestion,
            ingestion_jobs=ingestion_jobs,
            graph_candidate_repository=(
                graph_candidates if semantic_graph_index is not None else None
            ),
            semantic_graph_index=semantic_graph_index,
        )
        if resolved.enterprise_fixture_enabled
        else None
    )
    if resolved.learning_job_mode == "async":
        from app.infra.postgres_learning_jobs import (
            LEARNING_JOB_MIGRATIONS,
            PostgresLearningJobRepository,
        )

        if postgres is None:
            raise ValueError("Async learning requires a Postgres database")
        postgres_migrations.extend(LEARNING_JOB_MIGRATIONS)
        learning_job_repository = PostgresLearningJobRepository(postgres)
    legacy_memories = JsonMemoryStore(resolved.data_dir / "memory.json")
    legacy_skills = SkillMarkdownRepository(resolved.data_dir / "skills")
    legacy_change_sets = JsonLearningChangeSetRepository(
        resolved.data_dir / "learning_changes.json"
    )
    legacy_skill_evaluations = JsonSkillEvaluationRepository(
        resolved.data_dir / "skill_evaluations.json"
    )
    legacy_skill_observations = JsonSkillObservationRepository(
        resolved.data_dir / "skill_observations.json"
    )
    legacy_skill_transitions = JsonSkillTransitionRepository(
        resolved.data_dir / "skill_transitions.json"
    )
    memories: MemoryRepository = legacy_memories
    skills: SkillRepository = legacy_skills
    change_sets: LearningChangeSetRepository = legacy_change_sets
    skill_evaluations: SkillEvaluationRepository = legacy_skill_evaluations
    skill_observations: SkillObservationRepository = legacy_skill_observations
    skill_transitions: SkillTransitionRepository = legacy_skill_transitions
    learning_artifact_migrator: AsyncLifecycle | None = None
    harness_experiences: HarnessExperienceRepository = JsonHarnessExperienceRepository(
        resolved.data_dir / "harness_experiences.jsonl"
    )
    harness_policies: HarnessPolicyRepository = JsonHarnessPolicyRepository(
        resolved.data_dir / "harness_policies.jsonl"
    )
    if resolved.learning_artifact_backend == "postgres":
        from app.infra.learning_artifact_migration import (
            LegacyLearningArtifactMigrator,
        )
        from app.infra.postgres_learning_artifacts import (
            LEARNING_ARTIFACT_MIGRATIONS,
            PostgresLearningArtifactRepository,
            PostgresLearningChangeSetRepository,
            PostgresSkillEvaluationRepository,
            PostgresSkillObservationRepository,
            PostgresSkillRepository,
            PostgresSkillTransitionRepository,
        )

        if postgres is None:
            raise ValueError("Postgres learning artifacts require a database")
        postgres_migrations.extend(LEARNING_ARTIFACT_MIGRATIONS)
        learning_artifacts = PostgresLearningArtifactRepository(postgres)
        memories = learning_artifacts
        skills = PostgresSkillRepository(learning_artifacts)
        change_sets = PostgresLearningChangeSetRepository(learning_artifacts)
        skill_evaluations = PostgresSkillEvaluationRepository(learning_artifacts)
        skill_observations = PostgresSkillObservationRepository(learning_artifacts)
        skill_transitions = PostgresSkillTransitionRepository(learning_artifacts)
        learning_artifact_migrator = LegacyLearningArtifactMigrator(
            learning_artifacts,
            memories=legacy_memories,
            skills=legacy_skills,
            evaluations=legacy_skill_evaluations,
            observations=legacy_skill_observations,
            transitions=legacy_skill_transitions,
            change_sets=legacy_change_sets,
        )
        from app.infra.postgres_harness import (
            HARNESS_MIGRATIONS,
            PostgresHarnessExperienceRepository,
            PostgresHarnessPolicyRepository,
        )

        postgres_migrations.extend(HARNESS_MIGRATIONS)
        harness_experiences = PostgresHarnessExperienceRepository(postgres)
        harness_policies = PostgresHarnessPolicyRepository(postgres)
    personal_repository: PersonalRepository = JsonPersonalRepository(
        resolved.data_dir / "personal_control.json"
    )
    if postgres is not None:
        from app.personal.postgres import (
            PERSONAL_CONTROL_MIGRATIONS,
            PostgresPersonalRepository,
        )

        postgres_migrations.extend(PERSONAL_CONTROL_MIGRATIONS)
        personal_repository = PostgresPersonalRepository(postgres)
    if postgres is not None:
        from app.infra.postgres import PostgresRuntimeResource

        lifecycle_resources_list.append(PostgresRuntimeResource(postgres, postgres_migrations))
    if knowledge_migrator is not None:
        lifecycle_resources_list.append(knowledge_migrator)
    if learning_artifact_migrator is not None:
        lifecycle_resources_list.append(learning_artifact_migrator)
    if outbox_dispatcher is not None:
        lifecycle_resources_list.append(outbox_dispatcher)
    web_search: WebSearchPort | None = None
    if resolved.web_search_mode == "openai":
        primary_web_search = OpenAIHostedWebSearch(resolved)
        if resolved.web_search_fallback_mode == "duckduckgo":
            web_search = FallbackWebSearch(
                primary_web_search,
                DuckDuckGoWebSearch(
                    timeout_seconds=min(8, resolved.web_page_timeout_seconds),
                    allowed_domains=resolved.web_search_allowed_domains,
                ),
                primary_timeout_seconds=resolved.web_search_primary_timeout_seconds,
            )
        else:
            web_search = primary_web_search
    general_tools = GeneralToolService(
        timeout_seconds=resolved.web_page_timeout_seconds,
        max_download_bytes=resolved.web_page_max_download_bytes,
        allowed_domains=resolved.web_search_allowed_domains,
    )
    computer_workspace = (
        WorkspaceFileTools(
            resolved.computer_workspace_roots,
            tenant_id=resolved.computer_workspace_tenant_id,
            project_id=resolved.computer_workspace_project_id,
            max_file_bytes=resolved.computer_workspace_max_file_bytes,
            max_extracted_chars=resolved.computer_workspace_max_extracted_chars,
            max_pdf_pages=resolved.computer_workspace_max_pdf_pages,
            max_scan_entries=resolved.computer_workspace_max_scan_entries,
            max_search_files=resolved.computer_workspace_max_search_files,
        )
        if resolved.computer_workspace_enabled
        else None
    )
    integration_runtime = AgentToolRuntime(
        retrieval,
        graph=graph,
        web_search=web_search,
        general_tools=general_tools,
        workspace=computer_workspace,
        timeout_seconds=min(resolved.agent_timeout_seconds, 60),
        web_timeout_seconds=resolved.web_search_timeout_seconds,
        max_output_bytes=resolved.max_tool_output_bytes,
    )
    event_recorder = RunEventRecorder(resolved.data_dir / "run_events.jsonl")
    trajectories = JsonlTrajectoryRepository(resolved.data_dir / "trajectories.jsonl")
    conversations = JsonlConversationRepository(resolved.data_dir / "conversations.jsonl")
    personal = PersonalControlService(
        personal_repository,
        memories=memories,
        trajectories=trajectories,
    )
    skill_miner = RepeatedTrajectorySkillMiner(
        min_similar_runs=resolved.skill_min_similar_runs,
        min_successful_runs=resolved.skill_min_successful_runs,
        allowed_actions={
            "compare_graph_entities",
            "correct_personal_memory",
            "list_workspace_files",
            "manage_personal_notes",
            "manage_personal_plans",
            "manage_personal_profile",
            "manage_personal_journal",
            "manage_personal_tasks",
            "read_workspace_file",
            "recall_project_memory",
            "resolve_graph_entities",
            "retrieve_evidence_subgraph",
            "search_graph",
            "search_knowledge",
            "search_web",
            "read_web_page",
            "calculate",
            "current_time",
            "search_workspace_files",
        },
    )
    learning_reflector = _build_learning_reflector(resolved)
    learning = LearningEngine(
        trajectories,
        memories,
        skills,
        reflector=learning_reflector,
        skill_miner=skill_miner,
        skill_refiner=SkillRefiner(
            min_new_source_runs=resolved.skill_refinement_min_new_runs,
        ),
        change_set_repository=change_sets,
        transition_repository=skill_transitions,
    )
    skill_evaluator = DeterministicSkillEvaluator(
        trajectories,
        sandbox=FrozenCapabilitySkillSandbox(
            timeout_seconds=resolved.skill_replay_timeout_seconds,
            max_steps=resolved.skill_replay_max_steps,
            max_fixture_output_bytes=resolved.max_tool_output_bytes,
        ),
        min_cases=resolved.skill_min_evaluation_cases,
        max_score_regression=resolved.skill_max_score_regression,
        max_unsupported_claim_rate=resolved.skill_max_unsupported_claim_rate,
    )
    skill_evolution = SkillEvolutionService(
        learning_engine=learning,
        skills=skills,
        evaluator=skill_evaluator,
        evaluations=skill_evaluations,
        observations=skill_observations,
        transitions=skill_transitions,
        change_sets=change_sets,
        min_shadow_observations=resolved.skill_min_shadow_observations,
        min_canary_observations=resolved.skill_min_canary_observations,
        min_quality_score=resolved.skill_min_quality_score,
        max_score_regression=resolved.skill_max_score_regression,
        max_failure_rate=resolved.skill_max_failure_rate,
        max_unsupported_claim_rate=resolved.skill_max_unsupported_claim_rate,
        max_negative_feedback_rate=resolved.skill_max_negative_feedback_rate,
        severe_negative_feedback_threshold=(resolved.skill_severe_negative_feedback_threshold),
    )
    harness_experience_service = (
        HarnessExperienceService(harness_experiences)
        if resolved.harness_experience_enabled
        else None
    )
    harness_pattern_miner = (
        DeterministicPatternMiner(
            harness_experiences,
            harness_policies,
            repeated_failure_threshold=resolved.harness_repeated_failure_threshold,
            min_cluster_size=resolved.harness_min_cluster_size,
        )
        if resolved.harness_distillation_enabled
        else None
    )
    harness_consumer = BoundedHarnessConsumer(
        HarnessConsumerLimits(
            max_capsule_memories=resolved.harness_max_capsule_memories,
            max_graph_hops=resolved.harness_max_graph_hops,
            max_subqueries=resolved.harness_max_subqueries,
            max_retrieval_rounds=resolved.harness_max_retrieval_rounds,
        )
    )
    harness_pattern_evolution = HarnessPatternEvolutionService(
        harness_policies,
        DeterministicPatternEvaluator(
            harness_experiences,
            consumer=harness_consumer,
            min_support_cases=resolved.harness_repeated_failure_threshold,
        ),
    )
    harness_overlay_selector = (
        HarnessOverlaySelector(
            harness_experiences,
            harness_policies,
            mode=HarnessOverlayMode(resolved.harness_overlay_mode),
            max_patterns=resolved.harness_max_patterns_per_run,
            consumer=harness_consumer,
            canary_percentage=resolved.harness_canary_percentage,
        )
        if resolved.harness_experience_enabled
        else None
    )
    learning_workflow = LearningWorkflowProcessor(
        learning,
        skill_evolution,
        learning_mode=resolved.learning_mode,
        harness_experiences=harness_experience_service,
        harness_pattern_miner=harness_pattern_miner,
    )
    learning_jobs = (
        LearningJobService(
            learning_job_repository,
            learning_workflow,
            max_attempts=resolved.learning_job_max_attempts,
            lease_seconds=resolved.learning_job_lease_seconds,
            poll_seconds=resolved.learning_job_poll_seconds,
            retry_base_seconds=resolved.learning_job_retry_base_seconds,
            worker_enabled=resolved.learning_job_worker_enabled,
        )
        if learning_job_repository is not None
        else None
    )

    async def snapshot_skills(context: RunContext) -> dict[str, str]:
        active = await skills.list_by_status(
            "active",
            tenant_id=context.tenant_id,
            project_id=context.project_id,
        )
        canary = await skills.list_by_status(
            "canary",
            tenant_id=context.tenant_id,
            project_id=context.project_id,
        )
        eligible = [
            skill
            for skill in [*active, *canary]
            if skill_is_eligible(
                skill,
                context,
                canary_percent=resolved.skill_canary_percent,
            )
        ]
        selected: dict[str, str] = {}
        for skill in sorted(
            eligible,
            key=lambda item: (
                item.name,
                tuple(int(part) for part in item.version.split(".")),
            ),
        ):
            selected[skill.name] = skill.version
        return selected

    runtime: AgentRuntime
    hermes_bridge: HermesCapabilityBridge | None = None
    context_engine = ContextEngine(
        trajectories,
        conversations,
        memories,
        skills,
        personal=personal,
        max_turns=resolved.conversation_history_turns,
        total_tokens=resolved.context_total_tokens,
        history_tokens=resolved.context_history_tokens,
        summary_tokens=resolved.context_summary_tokens,
        memory_tokens=resolved.context_memory_tokens,
        skill_tokens=resolved.context_skill_tokens,
        personal_tokens=resolved.context_personal_tokens,
        memory_recency_half_life_days=(resolved.context_memory_recency_half_life_days),
    )
    hermes_native_learning = HermesNativeLearningService(
        change_sets=change_sets,
        admin_client=None,
        snapshot_retention_days=resolved.hermes_native_snapshot_retention_days,
    )
    if resolved.runtime_mode == "hermes":
        hermes_native_admin = HermesNativeAdminClient(resolved)
        hermes_native_learning = HermesNativeLearningService(
            change_sets=change_sets,
            admin_client=hermes_native_admin,
            snapshot_retention_days=resolved.hermes_native_snapshot_retention_days,
        )
        lifecycle_resources_list.append(hermes_native_admin)
        hermes_bridge = HermesCapabilityBridge(
            settings=resolved,
            retrieval=integration_runtime,
            graph_search=integration_runtime,
            graph_tools=integration_runtime,
            web_search=(integration_runtime if integration_runtime.web_search_enabled else None),
            general_tools=integration_runtime,
            workspace=(
                integration_runtime if integration_runtime.computer_workspace_enabled else None
            ),
            memory_repository=memories,
            skill_repository=skills,
            event_recorder=event_recorder,
            change_set_repository=change_sets,
            personal=personal,
        )
        hermes_runtime = HermesAgentRuntime(
            settings=resolved,
            bridge=hermes_bridge,
            capsule_provider=context_engine.capsule,
            history_provider=context_engine.history,
        )
        runtime = hermes_runtime
        lifecycle_resources_list.append(hermes_runtime)
    else:
        runtime = OfflineAgentRuntime(integration_runtime, event_recorder=event_recorder)
    direct_conversation = None
    adaptive_router_enabled = (
        resolved.conversation_fast_path_enabled and resolved.adaptive_rag_router_enabled
    )
    if resolved.runtime_mode == "hermes" and adaptive_router_enabled:
        from app.agent.model_provider import build_model_client

        try:
            direct_conversation = OpenAIConversationResponder(
                build_model_client(
                    resolved,
                    max_retries=0,
                    timeout=resolved.adaptive_rag_router_timeout_seconds,
                ),
                model=(
                    resolved.adaptive_rag_router_model
                    or resolved.conversation_fast_path_model
                    or resolved.openai_model
                ),
                max_completion_tokens=(resolved.adaptive_rag_router_max_completion_tokens),
                reasoning_effort=resolved.adaptive_rag_router_reasoning_effort,
            )
        except ValueError:
            direct_conversation = None
        if direct_conversation is not None:
            lifecycle_resources_list.append(direct_conversation)
    runtime = ConversationRoutedRuntime(
        runtime,
        enabled=adaptive_router_enabled,
        direct_responder=direct_conversation,
        history_provider=context_engine.history,
        context_trace_provider=context_engine.trace,
    )

    async def learn_after_run(
        trajectory: RunTrajectory,
        trigger: LearningTrigger,
    ) -> None:
        if learning_jobs is not None:
            await learning_jobs.submit(trajectory, trigger=trigger)
            return
        await learning_workflow(trajectory)

    run_service = RunService(
        runtime=runtime,
        trajectories=trajectories,
        settings=resolved,
        learning_processor=learn_after_run,
        event_recorder=event_recorder,
        skill_version_provider=snapshot_skills,
        overlay_selector=harness_overlay_selector,
        harness_consumer=harness_consumer,
        domain_packs=domain_packs,
        workspace_profiles=workspace_profiles,
    )
    workspace_service = WorkspaceService(
        settings=resolved,
        trajectories=trajectories,
        conversations=conversations,
        memories=memories,
        skills=skills,
        change_sets=change_sets,
        integration_runtime=integration_runtime,
        learning_engine=learning,
        skill_evolution=skill_evolution,
        knowledge_repository=knowledge,
        ingestion_service=ingestion,
        ingestion_job_service=ingestion_jobs,
        learning_job_service=learning_jobs,
        harness_experiences=harness_experiences,
        harness_policies=harness_policies,
        harness_pattern_evolution=harness_pattern_evolution,
        graph_candidate_service=graph_candidate_service,
        outbox_repository=outbox_repository,
        hermes_native_learning_service=hermes_native_learning,
        domain_packs=domain_packs,
        workspace_profiles=workspace_profiles,
        enterprise_fixture_service=enterprise_fixture,
    )
    return ApplicationComponents(
        settings=resolved,
        run_service=run_service,
        learning_engine=learning,
        skill_evolution_service=skill_evolution,
        learning_reflector=learning_reflector,
        memory_store=memories,
        skill_repository=skills,
        trajectory_repository=trajectories,
        integration_runtime=integration_runtime,
        change_set_repository=change_sets,
        workspace_service=workspace_service,
        knowledge_repository=knowledge,
        ingestion_service=ingestion,
        ingestion_job_service=ingestion_jobs,
        enterprise_fixture_service=enterprise_fixture,
        learning_job_service=learning_jobs,
        harness_experience_repository=harness_experiences,
        harness_policy_repository=harness_policies,
        harness_experience_service=harness_experience_service,
        harness_pattern_evolution_service=harness_pattern_evolution,
        vector_index=vector_index,
        graph_backend=graph,
        graph_structural_index=structural_graph_index,
        graph_extractor=graph_extractor,
        vision_analyzer=vision_analyzer,
        graph_candidate_repository=graph_candidates,
        graph_candidate_service=graph_candidate_service,
        graph_enrichment_coordinator=graph_enrichment,
        hermes_native_learning_service=hermes_native_learning,
        personal_service=personal,
        hermes_bridge=hermes_bridge,
        lifecycle_resources=tuple(lifecycle_resources_list),
    )
