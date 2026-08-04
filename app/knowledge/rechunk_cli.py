from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient

from app.config import Settings
from app.infra.postgres import PostgresDatabase
from app.infra.postgres_knowledge import PostgresKnowledgeRepository
from app.knowledge.chunking import HierarchicalDocumentChunker
from app.knowledge.rechunk import KnowledgeRechunkService
from app.knowledge.store import FileKnowledgeObjectStore
from app.retrieval.embeddings import DeterministicDenseEmbedder, HashedSparseEmbedder
from app.retrieval.qdrant_hybrid import QdrantHybridStore


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description=(
            "Rebuild retained arXiv documents from OCR Document IR without model or KG calls"
        )
    )
    cli.add_argument("--tenant-id", default="local")
    cli.add_argument("--project-id", default="computer-science")
    cli.add_argument("--document-id", action="append", default=[])
    cli.add_argument("--ocr-root", type=Path, default=Path(".data/arxiv/ocr"))
    cli.add_argument(
        "--checkpoint",
        type=Path,
        default=Path(".data/knowledge_rechunk_manifest_v2.json"),
    )
    cli.add_argument("--postgres-dsn")
    cli.add_argument("--qdrant-url")
    cli.add_argument("--qdrant-collection")
    cli.add_argument(
        "--sparse-idf",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable Qdrant collection-level IDF weighting for sparse retrieval",
    )
    cli.add_argument("--embedding-dimensions", type=int)
    cli.add_argument("--target-tokens", type=int, default=400)
    cli.add_argument("--overlap-tokens", type=int, default=45)
    cli.add_argument("--min-section-tokens", type=int, default=80)
    cli.add_argument("--max-chunks", type=int, default=10_000)
    cli.add_argument("--limit", type=int)
    cli.add_argument("--concurrency", type=int, default=4)
    cli.add_argument("--force", action="store_true")
    cli.add_argument("--fail-fast", action="store_true")
    cli.add_argument("--dry-run", action="store_true")
    cli.add_argument(
        "--skip-vector-index",
        action="store_true",
        help="Replace Postgres chunks only; normally the active Qdrant index is updated too",
    )
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
    qdrant_collection = (
        args.qdrant_collection or settings.qdrant_collection or "hermesgraph_chunks"
    )
    embedding_dimensions = args.embedding_dimensions or settings.embedding_dimensions

    database = PostgresDatabase(
        postgres_dsn,
        command_timeout_seconds=min(settings.agent_timeout_seconds, 60),
    )
    repository = PostgresKnowledgeRepository(
        database,
        FileKnowledgeObjectStore(settings.data_dir / "knowledge"),
    )
    vector_store: QdrantHybridStore | None = None
    if not args.skip_vector_index:
        qdrant_client = AsyncQdrantClient(
            url=qdrant_url,
            api_key=(
                settings.qdrant_api_key.get_secret_value()
                if isinstance(settings.qdrant_api_key, SecretStr)
                else None
            ),
            timeout=min(settings.agent_timeout_seconds, 60),
        )
        vector_store = QdrantHybridStore(
            qdrant_client,
            DeterministicDenseEmbedder(embedding_dimensions),
            HashedSparseEmbedder(),
            collection_name=qdrant_collection,
            prefetch_limit=settings.qdrant_prefetch_limit,
            rrf_k=settings.qdrant_rrf_k,
            use_sparse_idf=args.sparse_idf,
        )
    service = KnowledgeRechunkService(
        repository,
        ocr_root=args.ocr_root,
        checkpoint_path=args.checkpoint,
        chunker=HierarchicalDocumentChunker(
            target_tokens=args.target_tokens,
            overlap_tokens=args.overlap_tokens,
            min_section_tokens=args.min_section_tokens,
            max_chunks=args.max_chunks,
        ),
        vector_index=vector_store,
    )
    await database.start()
    try:
        summary = await service.run(
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            document_ids=[UUID(value) for value in args.document_id],
            limit=args.limit,
            force=args.force,
            fail_fast=args.fail_fast,
            dry_run=args.dry_run,
            concurrency=args.concurrency,
        )
    finally:
        if vector_store is not None:
            await vector_store.close()
        await database.close()
    print(summary.model_dump_json(indent=2))
    return 0 if summary.documents_failed == 0 else 1


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
