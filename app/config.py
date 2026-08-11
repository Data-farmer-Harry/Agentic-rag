import re
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.enums import KnowledgeLayer, WorkspaceMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "test", "staging", "production"] = "local"
    app_name: str = "hermesgraph"
    data_dir: Path = Path(".data")
    runtime_mode: Literal["offline", "hermes"] = "offline"
    api_auth_mode: Literal["local", "bearer"] = "local"
    api_bearer_token: SecretStr | None = None
    api_tenant_id: str = "local"
    api_user_id: str = "local-user"
    api_allowed_projects: list[str] = Field(default_factory=list)
    api_identity_role: Literal["viewer", "member", "owner"] = "owner"

    workspace_display_name: str = "Engineering Intelligence"
    workspace_mode: WorkspaceMode = WorkspaceMode.TEAM
    workspace_enabled_knowledge_layers: list[KnowledgeLayer] = Field(default_factory=list)
    workspace_default_domain_pack: str | None = None
    enterprise_fixture_enabled: bool = True
    enterprise_fixture_root: Path = Path("examples/enterprise_knowledge")

    hermes_api_url: str = "http://127.0.0.1:8642"
    hermes_api_key: SecretStr | None = None
    hermes_bridge_token: SecretStr | None = None
    hermes_native_admin_url: str = "http://127.0.0.1:8643"
    hermes_native_admin_token: SecretStr | None = None
    hermes_native_admin_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    hermes_native_snapshot_retention_days: int = Field(default=30, ge=1, le=3_650)
    hermes_poll_interval_seconds: float = Field(default=0.25, ge=0.05, le=5.0)
    hermes_post_publish_timeout_seconds: float = Field(
        default=120.0,
        ge=5.0,
        le=1_800.0,
    )
    hermes_native_review_timeout_seconds: float = Field(
        default=180.0,
        ge=1.0,
        le=1_800.0,
    )
    hermes_shutdown_grace_seconds: float = Field(default=10.0, ge=0.0, le=60.0)

    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6"
    model_provider: str = "openai"
    model_base_url: str | None = None
    model_api_key: SecretStr | None = None

    learning_mode: Literal["disabled", "observe", "shadow", "canary", "active"] = "shadow"
    learning_reflector_mode: Literal["deterministic", "openai"] = "deterministic"
    learning_reflection_model: str | None = None
    learning_reflection_max_output_tokens: int = Field(default=2_000, ge=512, le=10_000)
    learning_reflection_timeout_seconds: int = Field(default=45, ge=5, le=300)
    learning_reflection_max_input_chars: int = Field(default=20_000, ge=4_000, le=100_000)
    learning_reflection_min_memory_confidence: float = Field(
        default=0.75,
        ge=0.6,
        le=1.0,
    )
    learning_reflection_trigger_mode: Literal["signals", "all"] = "signals"
    learning_job_mode: Literal["inline", "async"] = "inline"
    learning_artifact_backend: Literal["local", "postgres"] = "local"
    learning_job_worker_enabled: bool = True
    learning_job_max_attempts: int = Field(default=3, ge=1, le=20)
    learning_job_lease_seconds: int = Field(default=300, ge=10, le=3_600)
    learning_job_poll_seconds: float = Field(default=1.0, ge=0.05, le=30.0)
    learning_job_retry_base_seconds: int = Field(default=5, ge=1, le=3_600)
    harness_experience_enabled: bool = True
    harness_distillation_enabled: bool = True
    harness_overlay_mode: Literal[
        "disabled",
        "observe",
        "shadow",
        "canary",
        "active",
    ] = "observe"
    harness_experience_ttl_days: int = Field(default=180, ge=30, le=3_650)
    harness_min_cluster_size: int = Field(default=5, ge=3, le=100)
    harness_repeated_failure_threshold: int = Field(default=3, ge=2, le=100)
    harness_max_patterns_per_run: int = Field(default=3, ge=1, le=3)
    harness_bounded_consumer_enabled: bool = True
    harness_canary_percentage: int = Field(default=10, ge=0, le=100)
    harness_max_capsule_memories: int = Field(default=20, ge=0, le=20)
    harness_max_graph_hops: int = Field(default=3, ge=1, le=3)
    harness_max_subqueries: int = Field(default=4, ge=1, le=4)
    harness_max_retrieval_rounds: int = Field(default=2, ge=1, le=2)
    max_agent_turns: int = Field(default=10, ge=1, le=50)
    max_tool_calls: int = Field(default=20, ge=1, le=100)
    max_retrieval_tool_calls: int = Field(default=3, ge=1, le=10)
    max_graph_tool_calls: int = Field(default=6, ge=1, le=20)
    max_web_search_tool_calls: int = Field(default=3, ge=1, le=10)
    max_computer_tool_calls: int = Field(default=8, ge=1, le=20)
    max_personal_tool_calls: int = Field(default=8, ge=1, le=20)
    max_skill_activations: int = Field(default=3, ge=1, le=10)
    max_tool_output_bytes: int = Field(default=100_000, ge=1_000, le=2_000_000)
    agent_timeout_seconds: int = Field(default=90, ge=5, le=3600)
    conversation_fast_path_enabled: bool = True
    conversation_fast_path_timeout_seconds: float = Field(
        default=20.0,
        ge=1.0,
        le=60.0,
    )
    conversation_fast_path_model: str | None = None
    adaptive_rag_router_enabled: bool = True
    adaptive_rag_router_timeout_seconds: float = Field(default=12.0, ge=1.0, le=60.0)
    adaptive_rag_router_model: str | None = "gpt-4.1-nano"
    adaptive_rag_router_max_completion_tokens: int = Field(
        default=256,
        ge=64,
        le=2_000,
    )
    adaptive_rag_router_reasoning_effort: Literal[
        "minimal", "low", "medium", "high"
    ] = "minimal"
    conversation_history_turns: int = Field(default=8, ge=0, le=50)
    context_total_tokens: int = Field(default=8_000, ge=1_000, le=100_000)
    context_history_tokens: int = Field(default=3_500, ge=0, le=50_000)
    context_summary_tokens: int = Field(default=1_200, ge=0, le=20_000)
    context_memory_tokens: int = Field(default=2_200, ge=160, le=30_000)
    context_skill_tokens: int = Field(default=700, ge=100, le=10_000)
    context_personal_tokens: int = Field(default=1_200, ge=100, le=20_000)
    context_memory_recency_half_life_days: float = Field(
        default=90.0,
        ge=1.0,
        le=3_650.0,
    )
    skill_min_similar_runs: int = Field(default=3, ge=2, le=100)
    skill_min_successful_runs: int = Field(default=2, ge=1, le=100)
    skill_canary_percent: int = Field(default=10, ge=0, le=100)
    skill_min_evaluation_cases: int = Field(default=2, ge=1, le=100)
    skill_replay_timeout_seconds: float = Field(default=5.0, ge=0.05, le=300.0)
    skill_replay_max_steps: int = Field(default=20, ge=1, le=100)
    skill_refinement_min_new_runs: int = Field(default=2, ge=1, le=100)
    skill_min_shadow_observations: int = Field(default=3, ge=1, le=10_000)
    skill_min_canary_observations: int = Field(default=5, ge=1, le=10_000)
    skill_min_quality_score: float = Field(default=0.65, ge=0.0, le=1.0)
    skill_max_score_regression: float = Field(default=0.02, ge=0.0, le=1.0)
    skill_max_failure_rate: float = Field(default=0.20, ge=0.0, le=1.0)
    skill_max_unsupported_claim_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    skill_max_negative_feedback_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    skill_severe_negative_feedback_threshold: float = Field(
        default=-0.5,
        ge=-1.0,
        le=0.0,
    )
    memory_min_trust_score: float = Field(default=0.6, ge=0.0, le=1.0)
    max_upload_bytes: int = Field(default=10_000_000, ge=1_024, le=100_000_000)
    knowledge_chunk_size: int = Field(default=1_600, ge=200, le=20_000)
    knowledge_chunk_overlap: int = Field(default=180, ge=0, le=5_000)
    knowledge_max_pdf_pages: int = Field(default=500, ge=1, le=10_000)
    knowledge_max_extracted_chars: int = Field(
        default=5_000_000,
        ge=10_000,
        le=100_000_000,
    )
    knowledge_max_chunks: int = Field(default=10_000, ge=1, le=100_000)
    knowledge_repository_backend: Literal["local", "postgres"] = "local"
    ingestion_mode: Literal["sync", "async"] = "sync"
    ingestion_worker_enabled: bool = True
    ingestion_job_max_attempts: int = Field(default=3, ge=1, le=20)
    ingestion_job_lease_seconds: int = Field(default=300, ge=10, le=3_600)
    ingestion_job_poll_seconds: float = Field(default=1.0, ge=0.05, le=30.0)
    ingestion_job_retry_base_seconds: int = Field(default=5, ge=1, le=3_600)
    outbox_dispatcher_enabled: bool = True
    outbox_lease_seconds: int = Field(default=60, ge=10, le=3_600)
    outbox_poll_seconds: float = Field(default=0.5, ge=0.05, le=30.0)
    outbox_retry_base_seconds: int = Field(default=2, ge=1, le=3_600)

    vision_enabled: bool = False
    vision_model: str | None = None
    vision_detail: Literal["low", "high", "auto"] = "high"
    vision_max_output_tokens: int = Field(default=4_000, ge=512, le=50_000)
    vision_max_pixels: int = Field(default=40_000_000, ge=1_000_000, le=200_000_000)
    vision_max_dimension: int = Field(default=12_000, ge=512, le=50_000)
    vision_max_regions: int = Field(default=40, ge=1, le=50)

    web_search_mode: Literal["disabled", "openai"] = "disabled"
    web_search_model: str | None = None
    web_search_context_size: Literal["low", "medium", "high"] = "medium"
    web_search_max_results: int = Field(default=8, ge=1, le=20)
    web_search_max_output_tokens: int = Field(default=3_000, ge=512, le=10_000)
    web_search_timeout_seconds: int = Field(default=45, ge=5, le=300)
    web_search_allowed_domains: list[str] = Field(default_factory=list, max_length=100)

    computer_workspace_enabled: bool = False
    computer_workspace_roots: dict[str, Path] = Field(default_factory=dict, max_length=10)
    computer_workspace_tenant_id: str = Field(default="local", min_length=1, max_length=200)
    computer_workspace_project_id: str = Field(default="default", min_length=1, max_length=200)
    computer_workspace_max_file_bytes: int = Field(
        default=10_000_000,
        ge=1_024,
        le=100_000_000,
    )
    computer_workspace_max_extracted_chars: int = Field(
        default=250_000,
        ge=1_000,
        le=5_000_000,
    )
    computer_workspace_max_pdf_pages: int = Field(default=100, ge=1, le=1_000)
    computer_workspace_max_scan_entries: int = Field(default=5_000, ge=1, le=100_000)
    computer_workspace_max_search_files: int = Field(default=500, ge=1, le=10_000)

    retrieval_backend: Literal["local", "qdrant"] = "local"
    embedding_provider: Literal["openai", "deterministic"] = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1_024, ge=32, le=3_072)
    embedding_base_url: str | None = None
    qdrant_collection: str = "hermesgraph_chunks"
    qdrant_prefetch_limit: int = Field(default=40, ge=1, le=1_000)
    qdrant_rrf_k: int = Field(default=60, ge=1, le=1_000)
    qdrant_sparse_idf: bool = False
    qdrant_sparse_encoder: Literal["hashed", "bm25"] = "hashed"
    qdrant_bm25_k1: float = Field(default=1.2, ge=0.1, le=10.0)
    qdrant_bm25_b: float = Field(default=0.75, ge=0.0, le=1.0)
    qdrant_bm25_average_document_tokens: float = Field(
        default=150.0,
        gt=0.0,
        le=100_000.0,
    )
    agentic_retrieval_enabled: bool = True
    retrieval_planner_mode: Literal["deterministic", "openai"] = "deterministic"
    retrieval_planner_model: str | None = None
    retrieval_planner_max_output_tokens: int = Field(default=1_500, ge=512, le=10_000)
    retrieval_planner_timeout_seconds: int = Field(default=30, ge=5, le=120)
    retrieval_max_rounds: int = Field(default=2, ge=1, le=2)
    retrieval_max_subqueries: int = Field(default=4, ge=1, le=4)
    graph_backend: Literal["local", "neo4j"] = "local"
    graph_extractor_mode: Literal["rule", "openai", "hybrid"] = "rule"
    graph_extraction_model: str | None = None
    graph_extraction_max_batch_chars: int = Field(default=60_000, ge=20_000, le=500_000)
    graph_extraction_window_max_chars: int = Field(default=6_000, ge=1_000, le=60_000)
    graph_extraction_window_max_chunks: int = Field(default=4, ge=1, le=20)
    graph_extraction_window_overlap_chunks: int = Field(default=1, ge=0, le=19)
    graph_extraction_input_char_budget: int = Field(
        default=20_000,
        ge=5_000,
        le=500_000,
    )
    graph_extraction_public_reference_char_budget: int = Field(
        default=20_000,
        ge=2_000,
        le=100_000,
    )
    graph_extraction_max_output_tokens: int = Field(default=4_000, ge=512, le=100_000)
    graph_extraction_timeout_seconds: int = Field(default=300, ge=30, le=1_800)
    graph_extraction_max_entities: int = Field(default=25, ge=1, le=1_000)
    graph_extraction_max_relations: int = Field(default=25, ge=1, le=1_000)
    neo4j_database: str = "neo4j"

    postgres_dsn: str | None = None
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = None
    neo4j_uri: str | None = None
    neo4j_user: str | None = None
    neo4j_password: SecretStr | None = None

    @field_validator("web_search_allowed_domains")
    @classmethod
    def validate_web_search_domains(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            domain = value.strip().lower().rstrip(".")
            if (
                not domain
                or "://" in domain
                or "/" in domain
                or ":" in domain
                or re.fullmatch(
                    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                    domain,
                )
                is None
            ):
                raise ValueError(
                    "web_search_allowed_domains entries must be bare DNS domains"
                )
            if domain not in normalized:
                normalized.append(domain)
        return normalized

    @field_validator("api_allowed_projects")
    @classmethod
    def validate_api_allowed_projects(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            project_id = value.strip()
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", project_id) is None:
                raise ValueError("api_allowed_projects entries must be safe project IDs")
            if project_id not in normalized:
                normalized.append(project_id)
        return normalized

    @field_validator("workspace_enabled_knowledge_layers")
    @classmethod
    def validate_workspace_knowledge_layers(
        cls,
        values: list[KnowledgeLayer],
    ) -> list[KnowledgeLayer]:
        if len(set(values)) != len(values):
            raise ValueError("workspace_enabled_knowledge_layers entries must be unique")
        return values

    @field_validator("computer_workspace_roots")
    @classmethod
    def validate_computer_workspace_roots(cls, values: dict[str, Path]) -> dict[str, Path]:
        normalized: dict[str, Path] = {}
        for alias, path in values.items():
            clean_alias = alias.strip().lower()
            if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", clean_alias) is None:
                raise ValueError(
                    "computer_workspace_roots aliases must use lowercase letters, "
                    "digits, underscores, or hyphens"
                )
            if not str(path).strip():
                raise ValueError("computer_workspace_roots paths cannot be empty")
            if clean_alias in normalized:
                raise ValueError("computer_workspace_roots aliases must be unique")
            normalized[clean_alias] = path
        return normalized

    @model_validator(mode="after")
    def validate_backend_configuration(self) -> Self:
        if self.context_summary_tokens > self.context_history_tokens:
            raise ValueError(
                "CONTEXT_SUMMARY_TOKENS must not exceed CONTEXT_HISTORY_TOKENS"
            )
        context_allocations = (
            self.context_history_tokens
            + self.context_memory_tokens
            + self.context_skill_tokens
            + self.context_personal_tokens
        )
        if context_allocations > self.context_total_tokens:
            raise ValueError(
                "Context component token budgets must not exceed CONTEXT_TOTAL_TOKENS"
            )
        for field_name, value in (
            ("API_TENANT_ID", self.api_tenant_id),
            ("API_USER_ID", self.api_user_id),
        ):
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value) is None:
                raise ValueError(f"{field_name} must be a safe scope identifier")
        if not self.workspace_display_name.strip():
            raise ValueError("WORKSPACE_DISPLAY_NAME must not be empty")
        if (
            self.workspace_default_domain_pack is not None
            and re.fullmatch(
                r"[a-z][a-z0-9_]{2,63}",
                self.workspace_default_domain_pack,
            )
            is None
        ):
            raise ValueError("WORKSPACE_DEFAULT_DOMAIN_PACK must be a safe pack ID")
        if self.api_auth_mode == "bearer":
            if self.api_bearer_token is None:
                raise ValueError("API_BEARER_TOKEN is required in bearer auth mode")
            if len(self.api_bearer_token.get_secret_value()) < 32:
                raise ValueError("API_BEARER_TOKEN must contain at least 32 characters")
            if not self.api_allowed_projects:
                raise ValueError("API_ALLOWED_PROJECTS is required in bearer auth mode")
        if self.app_env in {"staging", "production"} and self.api_auth_mode != "bearer":
            raise ValueError("Staging and production require API_AUTH_MODE=bearer")
        if self.knowledge_chunk_overlap >= self.knowledge_chunk_size:
            raise ValueError("knowledge_chunk_overlap must be smaller than chunk size")
        if (
            not self.harness_experience_enabled
            and self.harness_overlay_mode != "disabled"
        ):
            raise ValueError(
                "HARNESS_OVERLAY_MODE must be disabled when experience collection is disabled"
            )
        if (
            not self.harness_experience_enabled
            and self.harness_distillation_enabled
        ):
            raise ValueError(
                "HARNESS_DISTILLATION_ENABLED requires experience collection"
            )
        if (
            self.harness_overlay_mode in {"canary", "active"}
            and not self.harness_bounded_consumer_enabled
        ):
            raise ValueError(
                "HARNESS_OVERLAY_MODE canary/active requires "
                "HARNESS_BOUNDED_CONSUMER_ENABLED"
            )
        if self.computer_workspace_enabled and not self.computer_workspace_roots:
            raise ValueError(
                "COMPUTER_WORKSPACE_ROOTS is required when computer workspace tools are enabled"
            )
        if self.runtime_mode == "hermes":
            if self.hermes_api_key is None:
                raise ValueError("HERMES_API_KEY is required for the Hermes runtime")
            if self.hermes_bridge_token is None:
                raise ValueError("HERMES_BRIDGE_TOKEN is required for the Hermes runtime")
            if self.hermes_native_admin_token is None:
                raise ValueError(
                    "HERMES_NATIVE_ADMIN_TOKEN is required for the Hermes runtime"
                )
            if not self.hermes_api_url.startswith(("http://", "https://")):
                raise ValueError("HERMES_API_URL must be an HTTP(S) URL")
            if not self.hermes_native_admin_url.startswith(("http://", "https://")):
                raise ValueError("HERMES_NATIVE_ADMIN_URL must be an HTTP(S) URL")
            if self.app_env in {"staging", "production"}:
                if len(self.hermes_api_key.get_secret_value()) < 32:
                    raise ValueError("HERMES_API_KEY must contain at least 32 characters")
                if len(self.hermes_bridge_token.get_secret_value()) < 32:
                    raise ValueError("HERMES_BRIDGE_TOKEN must contain at least 32 characters")
                if len(self.hermes_native_admin_token.get_secret_value()) < 32:
                    raise ValueError(
                        "HERMES_NATIVE_ADMIN_TOKEN must contain at least 32 characters"
                    )
        if self.graph_extraction_window_max_chars > self.graph_extraction_max_batch_chars:
            raise ValueError("graph extraction window must not exceed max batch chars")
        if (
            self.graph_extraction_window_overlap_chunks
            >= self.graph_extraction_window_max_chunks
        ):
            raise ValueError(
                "graph extraction window overlap must be smaller than window chunks"
            )
        if (
            self.graph_extraction_public_reference_char_budget
            > self.graph_extraction_input_char_budget
        ):
            raise ValueError(
                "public reference graph budget must not exceed the general input budget"
            )
        if self.retrieval_backend == "qdrant":
            if not self.qdrant_url:
                raise ValueError("QDRANT_URL is required for the qdrant retrieval backend")
            if self.qdrant_sparse_encoder == "bm25" and not self.qdrant_sparse_idf:
                raise ValueError("QDRANT_SPARSE_IDF must be enabled for BM25 retrieval")
            if self.embedding_provider == "openai":
                if self.model_provider == "openai" and self.openai_api_key is None:
                    raise ValueError("OPENAI_API_KEY is required for OpenAI document embeddings")
                if self.model_provider != "openai" and (
                    not (self.embedding_base_url or self.model_base_url)
                    or self.model_api_key is None
                ):
                    raise ValueError(
                        "MODEL_BASE_URL and MODEL_API_KEY are required for compatible "
                        "document embeddings"
                    )
        if self.graph_backend == "neo4j" and not self.neo4j_uri:
            raise ValueError("NEO4J_URI is required for the neo4j graph backend")
        if self.agentic_retrieval_enabled and self.retrieval_planner_mode == "openai":
            if self.model_provider == "openai" and self.openai_api_key is None:
                raise ValueError("OPENAI_API_KEY is required for OpenAI retrieval planning")
            if self.model_provider != "openai" and (
                not self.model_base_url or self.model_api_key is None
            ):
                raise ValueError(
                    "MODEL_BASE_URL and MODEL_API_KEY are required for compatible "
                    "retrieval planning"
                )
            if not (self.retrieval_planner_model or self.openai_model).strip():
                raise ValueError("A retrieval planner model is required")
        if self.learning_reflector_mode == "openai":
            if self.model_provider == "openai" and self.openai_api_key is None:
                raise ValueError("OPENAI_API_KEY is required for OpenAI reflection")
            if self.model_provider != "openai" and (
                not self.model_base_url or self.model_api_key is None
            ):
                raise ValueError(
                    "MODEL_BASE_URL and MODEL_API_KEY are required for compatible reflection"
                )
            if not (self.learning_reflection_model or self.openai_model).strip():
                raise ValueError("A learning reflection model is required")
        if (
            self.learning_job_mode == "async"
            or self.learning_artifact_backend == "postgres"
            or self.ingestion_mode == "async"
            or self.knowledge_repository_backend == "postgres"
        ) and not self.postgres_dsn:
            raise ValueError(
                "POSTGRES_DSN is required for async learning, Postgres learning artifacts, "
                "async ingestion, or Postgres knowledge"
            )
        if self.graph_extractor_mode in {"openai", "hybrid"}:
            if self.model_provider == "openai" and self.openai_api_key is None:
                raise ValueError("OPENAI_API_KEY is required for OpenAI graph extraction")
            if self.model_provider != "openai" and (
                not self.model_base_url or self.model_api_key is None
            ):
                raise ValueError(
                    "MODEL_BASE_URL and MODEL_API_KEY are required for compatible graph extraction"
                )
            model = (self.graph_extraction_model or self.openai_model).strip()
            if not model:
                raise ValueError("A graph extraction model is required")
        if self.vision_enabled:
            if self.model_provider == "openai" and self.openai_api_key is None:
                raise ValueError("OPENAI_API_KEY is required for OpenAI Vision")
            if self.model_provider != "openai" and (
                not self.model_base_url or self.model_api_key is None
            ):
                raise ValueError(
                    "MODEL_BASE_URL and MODEL_API_KEY are required for compatible Vision"
                )
            if not (self.vision_model or self.openai_model).strip():
                raise ValueError("A Vision model is required")
        if self.web_search_mode == "openai":
            if self.model_provider == "openai" and self.openai_api_key is None:
                raise ValueError("OPENAI_API_KEY is required for OpenAI web search")
            if self.model_provider != "openai" and (
                not self.model_base_url or self.model_api_key is None
            ):
                raise ValueError(
                    "MODEL_BASE_URL and MODEL_API_KEY are required for compatible web search"
                )
            if not (self.web_search_model or self.openai_model).strip():
                raise ValueError("A web search model is required")
        return self


def get_settings() -> Settings:
    return Settings(_env_file=".env")
