from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from qdrant_client import AsyncQdrantClient

from app.agent.model_provider import build_embedding_client, build_model_client
from app.config import get_settings
from app.evaluation.retrieval import (
    AgenticRetrievalEvaluator,
    RetrievalGoldenSet,
    load_retrieval_golden_set,
)
from app.retrieval.agentic import (
    AgenticRetrievalController,
    DeterministicQueryPlanner,
    OpenAIStructuredQueryPlanner,
)
from app.retrieval.embeddings import (
    DeterministicDenseEmbedder,
    HashedSparseEmbedder,
    OpenAIDenseEmbedder,
)
from app.retrieval.memory import InMemoryRetriever
from app.retrieval.pipeline import RetrievalPipeline
from app.retrieval.qdrant_hybrid import QdrantHybridStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate bounded agentic retrieval")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("examples/evaluation/agentic_retrieval_golden.json"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--planner-mode",
        choices=("deterministic", "openai"),
        default="deterministic",
    )
    parser.add_argument(
        "--backend",
        choices=("fixture", "qdrant"),
        default="fixture",
        help="Use embedded fixture documents or the configured read-only Qdrant corpus.",
    )
    parser.add_argument("--report-only", action="store_true")
    return parser


def _fixture_retriever(dataset: RetrievalGoldenSet) -> InMemoryRetriever:
    metadatas: list[dict[str, Any]] = []
    for item in dataset.documents:
        metadatas.append(
            {
                **item.metadata,
                "source_id": item.source_id,
                "title": item.title,
            }
        )
    return InMemoryRetriever.from_texts(
        [item.text for item in dataset.documents],
        source_type="retrieval_eval_fixture",
        metadatas=metadatas,
        min_score=0.2,
    )


async def _run(args: argparse.Namespace) -> int:
    dataset = load_retrieval_golden_set(args.dataset)
    settings = get_settings()
    if args.planner_mode == "openai":
        client = build_model_client(settings, timeout=120, max_retries=1)
        planner: Any = OpenAIStructuredQueryPlanner(
            client,
            model=settings.retrieval_planner_model or settings.openai_model,
            max_output_tokens=settings.retrieval_planner_max_output_tokens,
        )
    else:
        planner = DeterministicQueryPlanner(
            max_subqueries=settings.retrieval_max_subqueries
        )
    qdrant_store: QdrantHybridStore | None = None
    embedding_client: AsyncOpenAI | None = None
    if args.backend == "qdrant":
        if not settings.qdrant_url:
            raise ValueError("QDRANT_URL is required for --backend qdrant")
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
        qdrant_store = QdrantHybridStore(
            AsyncQdrantClient(
                url=settings.qdrant_url,
                api_key=(
                    settings.qdrant_api_key.get_secret_value()
                    if settings.qdrant_api_key is not None
                    else None
                ),
                timeout=min(settings.agent_timeout_seconds, 60),
            ),
            dense,
            HashedSparseEmbedder(),
            collection_name=settings.qdrant_collection,
            prefetch_limit=settings.qdrant_prefetch_limit,
            rrf_k=settings.qdrant_rrf_k,
            use_sparse_idf=settings.qdrant_sparse_idf,
        )
        pipeline = RetrievalPipeline({"qdrant_hybrid": qdrant_store})
    else:
        pipeline = RetrievalPipeline({"fixture_lexical": _fixture_retriever(dataset)})
    controller = AgenticRetrievalController(
        pipeline,
        planner=planner,
        max_rounds=settings.retrieval_max_rounds,
        max_subqueries=settings.retrieval_max_subqueries,
    )
    try:
        report = await AgenticRetrievalEvaluator(controller).run(dataset)
    finally:
        await controller.close()
        if qdrant_store is not None:
            await qdrant_store.close()
        if embedding_client is not None:
            await embedding_client.close()
    payload = report.model_dump_json(indent=2)
    print(payload)
    if args.output is not None:
        _write_atomic(args.output, payload + "\n")
    return 0 if args.report_only or report.passed == report.total else 1


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(asyncio.run(_run(args)))


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


if __name__ == "__main__":
    main()
