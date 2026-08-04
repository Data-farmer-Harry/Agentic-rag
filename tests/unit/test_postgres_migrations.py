from app.infra.postgres_harness import HARNESS_MIGRATIONS
from app.infra.postgres_ingestion_jobs import INGESTION_JOB_MIGRATIONS
from app.infra.postgres_knowledge import KNOWLEDGE_MIGRATIONS
from app.infra.postgres_learning_artifacts import LEARNING_ARTIFACT_MIGRATIONS
from app.infra.postgres_learning_jobs import LEARNING_JOB_MIGRATIONS
from app.personal.postgres import PERSONAL_CONTROL_MIGRATIONS


def test_postgres_migration_versions_are_globally_unique_and_contiguous() -> None:
    migrations = (
        *INGESTION_JOB_MIGRATIONS,
        *KNOWLEDGE_MIGRATIONS,
        *LEARNING_JOB_MIGRATIONS,
        *LEARNING_ARTIFACT_MIGRATIONS,
        *PERSONAL_CONTROL_MIGRATIONS,
        *HARNESS_MIGRATIONS,
    )
    versions = sorted(item.version for item in migrations)

    assert len(versions) == len(set(versions))
    assert versions == list(range(1, max(versions) + 1))
