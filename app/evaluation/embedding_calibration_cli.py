from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient

from app.agent.model_provider import build_embedding_client, build_model_client
from app.config import get_settings
from app.evaluation.embedding_calibration import (
    EmbeddingCalibrationReport,
    EmbeddingUsageMetrics,
    compare_retrieval_reports,
    index_calibration_scopes,
    scopes_from_dataset,
    usage_metrics,
)
from app.evaluation.retrieval import (
    AgenticRetrievalEvaluator,
    RetrievalEvalReport,
    load_retrieval_golden_set,
)
from app.infra.postgres import PostgresDatabase
from app.infra.postgres_knowledge import PostgresKnowledgeRepository
from app.knowledge.knowledge_repository import FileKnowledgeObjectStore
from app.retrieval.agentic_retrieval import (
    AgenticRetrievalController,
    DeterministicQueryPlanner,
    OpenAIStructuredQueryPlanner,
)
from app.retrieval.embedding_providers import (
    EmbeddingUsage,
    OpenAIDenseEmbedder,
    build_sparse_embedder,
)
from app.retrieval.hybrid_retrieval_pipeline import RetrievalPipeline
from app.retrieval.qdrant_hybrid_retriever import QdrantHybridStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reindex knowledge into an isolated OpenAI embedding collection and gate it"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("examples/evaluation/arxiv_retrieval_golden.json"),
    )
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-collection", required=True)
    parser.add_argument("--embedding-model")
    parser.add_argument("--embedding-dimensions", type=int)
    parser.add_argument(
        "--planner-mode",
        choices=("deterministic", "openai"),
        default="deterministic",
    )
    parser.add_argument("--price-per-million-input-tokens", type=float)
    parser.add_argument("--maximum-mean-mrr-drop", type=float, default=0.05)
    parser.add_argument("--continue-on-index-error", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.postgres_dsn:
        raise ValueError("POSTGRES_DSN is required for embedding calibration")
    if not settings.qdrant_url:
        raise ValueError("QDRANT_URL is required for embedding calibration")
    if args.target_collection == settings.qdrant_collection:
        raise ValueError("target collection must differ from the active Qdrant collection")
    if (
        args.price_per_million_input_tokens is not None
        and args.price_per_million_input_tokens < 0
    ):
        raise ValueError("embedding token price must not be negative")
    if not 0 <= args.maximum_mean_mrr_drop <= 1:
        raise ValueError("maximum mean MRR drop must be between 0 and 1")

    dataset = load_retrieval_golden_set(args.dataset)
    scopes = scopes_from_dataset(dataset)
    database = PostgresDatabase(
        settings.postgres_dsn,
        command_timeout_seconds=min(settings.agent_timeout_seconds, 60),
    )
    embedding_client = build_embedding_client(
        settings,
        timeout=min(settings.agent_timeout_seconds, 120),
    )
    embedder = OpenAIDenseEmbedder(
        embedding_client,
        model=args.embedding_model or settings.embedding_model,
        dimension=args.embedding_dimensions or settings.embedding_dimensions,
    )
    qdrant_client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=(
            settings.qdrant_api_key.get_secret_value()
            if settings.qdrant_api_key is not None
            else None
        ),
        timeout=min(settings.agent_timeout_seconds, 60),
    )
    store = QdrantHybridStore(
        qdrant_client,
        embedder,
        build_sparse_embedder(
            settings.qdrant_sparse_encoder,
            bm25_k1=settings.qdrant_bm25_k1,
            bm25_b=settings.qdrant_bm25_b,
            bm25_average_document_tokens=settings.qdrant_bm25_average_document_tokens,
        ),
        collection_name=args.target_collection,
        prefetch_limit=settings.qdrant_prefetch_limit,
        rrf_k=settings.qdrant_rrf_k,
        use_sparse_idf=settings.qdrant_sparse_idf,
    )
    repository = PostgresKnowledgeRepository(
        database,
        FileKnowledgeObjectStore(settings.data_dir / "knowledge"),
    )
    zero_usage = EmbeddingUsage()
    indexing_usage: EmbeddingUsageMetrics = usage_metrics(
        zero_usage,
        zero_usage,
        price_per_million_input_tokens=args.price_per_million_input_tokens,
    )
    retrieval_usage: EmbeddingUsageMetrics = indexing_usage
    evaluation: RetrievalEvalReport | None = None
    baseline_diff = None
    error: str | None = None
    controller: AgenticRetrievalController | None = None

    try:
        await database.start()
        indexing = await index_calibration_scopes(
            repository,
            store,
            scopes,
            fail_fast=not args.continue_on_index_error,
        )
        after_indexing = await embedder.usage_snapshot()
        indexing_usage = usage_metrics(
            after_indexing,
            zero_usage,
            price_per_million_input_tokens=args.price_per_million_input_tokens,
        )
        if indexing.discovered_document_count == 0:
            error = "No active knowledge documents were found for the dataset scopes"
        elif indexing.failures:
            error = "One or more documents failed isolated embedding reindexing"
        else:
            planner: Any
            if args.planner_mode == "openai":
                planner_client = build_model_client(settings, timeout=120, max_retries=1)
                planner = OpenAIStructuredQueryPlanner(
                    planner_client,
                    model=settings.retrieval_planner_model or settings.openai_model,
                    max_output_tokens=settings.retrieval_planner_max_output_tokens,
                )
            else:
                planner = DeterministicQueryPlanner(
                    max_subqueries=settings.retrieval_max_subqueries
                )
            controller = AgenticRetrievalController(
                RetrievalPipeline({"qdrant_hybrid": store}),
                planner=planner,
                max_rounds=settings.retrieval_max_rounds,
                max_subqueries=settings.retrieval_max_subqueries,
            )
            evaluation = await AgenticRetrievalEvaluator(controller).run(dataset)
            after_retrieval = await embedder.usage_snapshot()
            retrieval_usage = usage_metrics(
                after_retrieval,
                after_indexing,
                price_per_million_input_tokens=args.price_per_million_input_tokens,
            )
            if args.baseline is not None:
                baseline = RetrievalEvalReport.model_validate_json(
                    args.baseline.read_text(encoding="utf-8")
                )
                baseline_diff = compare_retrieval_reports(baseline, evaluation)
    except Exception as exc:
        if "indexing" not in locals():
            raise
        error = f"{type(exc).__name__}: {exc}"[:1_000]
    finally:
        if controller is not None:
            await controller.close()
        await store.close()
        await embedding_client.close()
        await database.close()

    gate_failures: list[str] = []
    if error is not None:
        gate_failures.append("calibration_error")
    if indexing.failures:
        gate_failures.append("embedding_indexing_failure")
    if evaluation is None:
        gate_failures.append("evaluation_missing")
    elif evaluation.passed != evaluation.total:
        gate_failures.append("retrieval_cases_failed")
    if baseline_diff is not None:
        if baseline_diff.newly_failed_case_ids:
            gate_failures.append("new_retrieval_regressions")
        if baseline_diff.mean_reciprocal_rank_delta < -args.maximum_mean_mrr_drop:
            gate_failures.append("mean_mrr_drop_exceeded")
    passed = not gate_failures
    report = EmbeddingCalibrationReport(
        target_collection=args.target_collection,
        active_collection=settings.qdrant_collection,
        embedding_revision=embedder.revision,
        planner_mode=args.planner_mode,
        scopes=scopes,
        indexing=indexing,
        indexing_usage=indexing_usage,
        retrieval_usage=retrieval_usage,
        evaluation=evaluation,
        baseline_diff=baseline_diff,
        gate_failures=gate_failures,
        error=error,
        passed=passed,
    )
    payload = report.model_dump_json(indent=2)
    print(payload)
    _write_atomic(args.output, payload + "\n")
    return 0 if args.report_only or passed else 1


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
