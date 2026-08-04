from __future__ import annotations

import argparse
import asyncio
import os

from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient

from app.config import Settings
from app.evaluation.embedding_calibration import (
    CalibrationScope,
    index_calibration_scopes,
)
from app.infra.postgres import PostgresDatabase
from app.infra.postgres_knowledge import PostgresKnowledgeRepository
from app.knowledge.store import FileKnowledgeObjectStore
from app.retrieval.embeddings import DeterministicDenseEmbedder, HashedSparseEmbedder
from app.retrieval.qdrant_hybrid import QdrantHybridStore


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Idempotently rebuild scoped Qdrant indexes without model or KG calls"
    )
    cli.add_argument("--tenant-id", default="local")
    cli.add_argument(
        "--project-id",
        action="append",
        dest="project_ids",
        help="Repeat for multiple projects; defaults to default and computer-science",
    )
    cli.add_argument("--postgres-dsn")
    cli.add_argument("--qdrant-url")
    cli.add_argument("--qdrant-collection")
    cli.add_argument("--embedding-dimensions", type=int)
    cli.add_argument(
        "--sparse-idf",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    cli.add_argument("--continue-on-error", action="store_true")
    return cli


def _offline_settings() -> Settings:
    return Settings(
        _env_file=".env",
        runtime_mode="offline",
        learning_reflector_mode="deterministic",
        learning_job_mode="inline",
        learning_artifact_backend="local",
        knowledge_repository_backend="local",
        ingestion_mode="sync",
        outbox_dispatcher_enabled=False,
        vision_enabled=False,
        web_search_mode="disabled",
        retrieval_backend="local",
        embedding_provider="deterministic",
        agentic_retrieval_enabled=False,
        retrieval_planner_mode="deterministic",
        graph_backend="local",
        graph_extractor_mode="rule",
        computer_workspace_enabled=False,
    )


async def run(args: argparse.Namespace) -> int:
    settings = _offline_settings()
    password = os.getenv("HERMESGRAPH_POSTGRES_PASSWORD", "hermesgraph-dev")
    postgres_dsn = (
        args.postgres_dsn
        or settings.postgres_dsn
        or f"postgresql://hermesgraph:{password}@127.0.0.1:5432/hermesgraph"
    )
    qdrant_url = args.qdrant_url or settings.qdrant_url or "http://127.0.0.1:6333"
    collection = args.qdrant_collection or settings.qdrant_collection
    dimensions = args.embedding_dimensions or settings.embedding_dimensions
    project_ids = tuple(args.project_ids or ("default", "computer-science"))

    database = PostgresDatabase(
        postgres_dsn,
        command_timeout_seconds=min(settings.agent_timeout_seconds, 60),
    )
    repository = PostgresKnowledgeRepository(
        database,
        FileKnowledgeObjectStore(settings.data_dir / "knowledge"),
    )
    store = QdrantHybridStore(
        AsyncQdrantClient(
            url=qdrant_url,
            api_key=(
                settings.qdrant_api_key.get_secret_value()
                if isinstance(settings.qdrant_api_key, SecretStr)
                else None
            ),
            timeout=min(settings.agent_timeout_seconds, 60),
        ),
        DeterministicDenseEmbedder(dimensions),
        HashedSparseEmbedder(),
        collection_name=collection,
        prefetch_limit=settings.qdrant_prefetch_limit,
        rrf_k=settings.qdrant_rrf_k,
        use_sparse_idf=args.sparse_idf,
    )
    await database.start()
    try:
        outcome = await index_calibration_scopes(
            repository,
            store,
            [
                CalibrationScope(tenant_id=args.tenant_id, project_id=project_id)
                for project_id in project_ids
            ],
            fail_fast=not args.continue_on_error,
        )
    finally:
        await store.close()
        await database.close()
    print(outcome.model_dump_json(indent=2))
    return 0 if not outcome.failures else 1


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
