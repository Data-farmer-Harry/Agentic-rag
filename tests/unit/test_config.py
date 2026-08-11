from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_hermes_runtime_requires_all_internal_credentials() -> None:
    with pytest.raises(ValidationError, match="HERMES_API_KEY"):
        Settings(runtime_mode="hermes")

    with pytest.raises(ValidationError, match="HERMES_BRIDGE_TOKEN"):
        Settings(runtime_mode="hermes", hermes_api_key="api-key")

    with pytest.raises(ValidationError, match="HERMES_NATIVE_ADMIN_TOKEN"):
        Settings(
            runtime_mode="hermes",
            hermes_api_key="api-key",
            hermes_bridge_token="bridge-key",
        )

    settings = Settings(
        runtime_mode="hermes",
        hermes_api_key="api-key",
        hermes_bridge_token="bridge-key",
        hermes_native_admin_token="native-admin-key",
    )

    assert settings.runtime_mode == "hermes"


def test_openai_agents_runtime_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(runtime_mode="openai")  # type: ignore[arg-type]


def test_qdrant_backend_requires_url_and_embedding_key() -> None:
    with pytest.raises(ValidationError, match="QDRANT_URL"):
        Settings(retrieval_backend="qdrant")

    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(retrieval_backend="qdrant", qdrant_url="http://localhost:6333")

    settings = Settings(
        retrieval_backend="qdrant",
        qdrant_url=":memory:",
        embedding_provider="deterministic",
        embedding_dimensions=64,
    )
    assert settings.qdrant_collection == "hermesgraph_chunks"


def test_bm25_qdrant_configuration_requires_idf() -> None:
    with pytest.raises(ValidationError, match="QDRANT_SPARSE_IDF"):
        Settings(
            retrieval_backend="qdrant",
            qdrant_url=":memory:",
            embedding_provider="deterministic",
            embedding_dimensions=64,
            qdrant_sparse_encoder="bm25",
            qdrant_sparse_idf=False,
        )

    settings = Settings(
        retrieval_backend="qdrant",
        qdrant_url=":memory:",
        embedding_provider="deterministic",
        embedding_dimensions=64,
        qdrant_sparse_encoder="bm25",
        qdrant_sparse_idf=True,
    )
    assert settings.qdrant_bm25_average_document_tokens == 150


def test_compatible_qdrant_embeddings_use_model_provider_credentials() -> None:
    with pytest.raises(ValidationError, match="MODEL_BASE_URL and MODEL_API_KEY"):
        Settings(
            retrieval_backend="qdrant",
            qdrant_url=":memory:",
            model_provider="compatible",
        )

    settings = Settings(
        retrieval_backend="qdrant",
        qdrant_url=":memory:",
        model_provider="compatible",
        model_base_url="http://localhost:55523/v1",
        model_api_key="test-key",
    )

    assert settings.embedding_provider == "openai"


def test_neo4j_backend_and_chunk_overlap_are_validated() -> None:
    with pytest.raises(ValidationError, match="NEO4J_URI"):
        Settings(graph_backend="neo4j")

    with pytest.raises(ValidationError, match="overlap"):
        Settings(knowledge_chunk_size=200, knowledge_chunk_overlap=200)


@pytest.mark.parametrize("mode", ["openai", "hybrid"])
def test_openai_graph_extractor_requires_api_key(mode: str) -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(graph_extractor_mode=mode)

    settings = Settings(
        graph_extractor_mode=mode,
        openai_api_key="test-key",
        graph_extraction_model="gpt-test",
    )
    assert settings.graph_extractor_mode == mode
    assert settings.graph_extraction_model == "gpt-test"


def test_compatible_graph_extractor_requires_provider_credentials() -> None:
    with pytest.raises(ValidationError, match="MODEL_BASE_URL and MODEL_API_KEY"):
        Settings(graph_extractor_mode="openai", model_provider="compatible")

    settings = Settings(
        graph_extractor_mode="openai",
        model_provider="compatible",
        model_base_url="http://localhost:55523/v1",
        model_api_key="test-key",
    )
    assert settings.model_provider == "compatible"


def test_rule_graph_extractor_remains_offline_default() -> None:
    settings = Settings()

    assert settings.graph_extractor_mode == "rule"
    assert settings.openai_api_key is None


def test_vision_requires_provider_credentials_when_enabled() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(vision_enabled=True)

    compatible = Settings(
        vision_enabled=True,
        model_provider="compatible",
        model_base_url="http://localhost:55523/v1",
        model_api_key="test-key",
        vision_model="gpt-vision-test",
    )

    assert compatible.vision_model == "gpt-vision-test"


def test_web_search_is_opt_in_and_requires_provider_credentials() -> None:
    assert Settings().web_search_mode == "disabled"

    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(web_search_mode="openai")
    with pytest.raises(ValidationError, match="MODEL_BASE_URL and MODEL_API_KEY"):
        Settings(web_search_mode="openai", model_provider="compatible")

    compatible = Settings(
        web_search_mode="openai",
        model_provider="compatible",
        model_base_url="http://localhost:55523/v1",
        model_api_key="test-key",
        web_search_model="gpt-search-test",
        web_search_allowed_domains=[" OpenAI.com.", "openai.com"],
    )

    assert compatible.web_search_model == "gpt-search-test"
    assert compatible.web_search_allowed_domains == ["openai.com"]


def test_web_search_domain_filters_reject_urls_and_ports() -> None:
    with pytest.raises(ValidationError, match="bare DNS domains"):
        Settings(web_search_allowed_domains=["https://openai.com/docs"])
    with pytest.raises(ValidationError, match="bare DNS domains"):
        Settings(web_search_allowed_domains=["openai.com:443"])


def test_computer_workspace_is_opt_in_and_requires_valid_roots(tmp_path: Path) -> None:
    assert Settings().computer_workspace_enabled is False

    with pytest.raises(ValidationError, match="COMPUTER_WORKSPACE_ROOTS"):
        Settings(computer_workspace_enabled=True)
    with pytest.raises(ValidationError, match="aliases"):
        Settings(computer_workspace_roots={"Bad Alias": tmp_path})

    settings = Settings(
        computer_workspace_enabled=True,
        computer_workspace_roots={"Workspace": tmp_path},
    )

    assert settings.computer_workspace_roots == {"workspace": tmp_path}
    assert settings.max_computer_tool_calls == 8
    assert settings.max_skill_activations == 3


def test_graph_extraction_window_must_fit_model_batch() -> None:
    with pytest.raises(ValidationError, match="window must not exceed"):
        Settings(
            graph_extraction_max_batch_chars=20_000,
            graph_extraction_window_max_chars=20_001,
        )
    with pytest.raises(ValidationError, match="window overlap"):
        Settings(
            graph_extraction_window_max_chunks=2,
            graph_extraction_window_overlap_chunks=2,
        )
    with pytest.raises(ValidationError, match="public reference graph budget"):
        Settings(
            graph_extraction_input_char_budget=5_000,
            graph_extraction_public_reference_char_budget=6_000,
        )


def test_graph_extraction_has_an_independent_bounded_timeout() -> None:
    assert Settings().graph_extraction_timeout_seconds == 300
    with pytest.raises(ValidationError):
        Settings(graph_extraction_timeout_seconds=29)
    with pytest.raises(ValidationError):
        Settings(graph_extraction_timeout_seconds=1_801)


def test_async_ingestion_requires_postgres_and_valid_worker_bounds() -> None:
    with pytest.raises(ValidationError, match="POSTGRES_DSN"):
        Settings(ingestion_mode="async")

    settings = Settings(
        ingestion_mode="async",
        postgres_dsn="postgresql://app:app@localhost/hermesgraph",
        ingestion_job_max_attempts=4,
        ingestion_job_lease_seconds=120,
    )
    assert settings.ingestion_mode == "async"
    assert settings.ingestion_job_max_attempts == 4
    assert settings.ingestion_job_lease_seconds == 120


def test_async_learning_requires_postgres_and_valid_worker_bounds() -> None:
    with pytest.raises(ValidationError, match="POSTGRES_DSN"):
        Settings(learning_job_mode="async")

    settings = Settings(
        learning_job_mode="async",
        postgres_dsn="postgresql://app:app@localhost/hermesgraph",
        learning_job_max_attempts=4,
        learning_job_lease_seconds=120,
    )
    assert settings.learning_job_mode == "async"
    assert settings.learning_job_max_attempts == 4
    assert settings.learning_job_lease_seconds == 120


def test_postgres_knowledge_repository_requires_dsn() -> None:
    with pytest.raises(ValidationError, match="POSTGRES_DSN"):
        Settings(knowledge_repository_backend="postgres")

    settings = Settings(
        knowledge_repository_backend="postgres",
        postgres_dsn="postgresql://app:app@localhost/hermesgraph",
    )
    assert settings.knowledge_repository_backend == "postgres"


def test_postgres_learning_artifacts_require_dsn() -> None:
    with pytest.raises(ValidationError, match="POSTGRES_DSN"):
        Settings(learning_artifact_backend="postgres")

    settings = Settings(
        learning_artifact_backend="postgres",
        postgres_dsn="postgresql://app:app@localhost/hermesgraph",
    )
    assert settings.learning_artifact_backend == "postgres"


def test_harness_online_overlay_modes_require_bounded_consumer() -> None:
    with pytest.raises(ValidationError, match="HARNESS_BOUNDED_CONSUMER_ENABLED"):
        Settings(
            harness_overlay_mode="active",
            harness_bounded_consumer_enabled=False,
        )
    assert Settings(harness_overlay_mode="active").harness_overlay_mode == "active"
    with pytest.raises(ValidationError, match="requires experience collection"):
        Settings(
            harness_experience_enabled=False,
            harness_distillation_enabled=True,
            harness_overlay_mode="disabled",
        )
