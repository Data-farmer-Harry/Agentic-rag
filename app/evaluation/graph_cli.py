from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from app.agent.model_provider import build_model_client
from app.config import get_settings
from app.domain.contracts import EntityRelationExtractorPort
from app.evaluation.graph_extraction import (
    ExtractionEvalThresholds,
    GraphExtractionEvaluator,
    GraphExtractionGoldenSet,
    OpenAIUsageAccumulator,
)
from app.graph.graph_extraction_pipeline import RuleBasedEntityRelationExtractor
from app.graph.openai_graph_extractor import (
    HybridEntityRelationExtractor,
    OpenAIStructuredEntityRelationExtractor,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate rule/OpenAI graph extraction against a golden set"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("examples/evaluation/graph_extraction_golden.json"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("rule", "openai", "hybrid"), default="openai")
    parser.add_argument("--model")
    parser.add_argument("--input-cost-per-million", type=float)
    parser.add_argument("--cached-input-cost-per-million", type=float)
    parser.add_argument("--output-cost-per-million", type=float)
    parser.add_argument("--minimum-success-rate", type=float, default=0.95)
    parser.add_argument("--minimum-entity-precision", type=float, default=0.90)
    parser.add_argument("--minimum-entity-recall", type=float, default=0.85)
    parser.add_argument("--minimum-entity-type-accuracy", type=float, default=0.90)
    parser.add_argument("--minimum-relation-precision", type=float, default=0.90)
    parser.add_argument("--minimum-relation-recall", type=float, default=0.80)
    parser.add_argument("--minimum-evidence-accuracy", type=float, default=0.95)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit zero after writing the report, even when the gate fails",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if (args.input_cost_per_million is None) != (
        args.output_cost_per_million is None
    ):
        raise ValueError("Both input and output token prices must be provided together")
    if args.cached_input_cost_per_million is not None and args.input_cost_per_million is None:
        raise ValueError("Cached input price requires input and output token prices")
    golden_set = GraphExtractionGoldenSet.load(args.dataset)
    tracker = OpenAIUsageAccumulator()
    extractor = _build_extractor(
        mode=args.mode,
        model=args.model or settings.graph_extraction_model or settings.openai_model,
        tracker=tracker,
    )
    thresholds = ExtractionEvalThresholds(
        minimum_success_rate=args.minimum_success_rate,
        minimum_entity_precision=args.minimum_entity_precision,
        minimum_entity_recall=args.minimum_entity_recall,
        minimum_entity_type_accuracy=args.minimum_entity_type_accuracy,
        minimum_relation_precision=args.minimum_relation_precision,
        minimum_relation_recall=args.minimum_relation_recall,
        minimum_evidence_accuracy=args.minimum_evidence_accuracy,
    )
    evaluator = GraphExtractionEvaluator(
        extractor,
        thresholds=thresholds,
        usage_probe=tracker.snapshot,
        input_cost_per_million=args.input_cost_per_million,
        cached_input_cost_per_million=args.cached_input_cost_per_million,
        output_cost_per_million=args.output_cost_per_million,
    )
    try:
        report = await evaluator.run(golden_set)
    finally:
        close = getattr(extractor, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    output = args.output or _default_output_path()
    payload = report.model_dump_json(indent=2)
    _write_atomic(output, payload + "\n")
    print(payload)
    print(f"report_path={output.resolve()}")
    return 0 if report.passed or args.report_only else 1


def _build_extractor(
    *,
    mode: str,
    model: str,
    tracker: OpenAIUsageAccumulator,
) -> EntityRelationExtractorPort:
    rule = RuleBasedEntityRelationExtractor()
    if mode == "rule":
        return rule
    settings = get_settings()
    client = build_model_client(
        settings,
        timeout=min(settings.agent_timeout_seconds, 120),
        max_retries=2,
    )
    structured = OpenAIStructuredEntityRelationExtractor(
        client,
        model=model,
        max_batch_chars=settings.graph_extraction_max_batch_chars,
        window_max_chars=settings.graph_extraction_window_max_chars,
        window_max_chunks=settings.graph_extraction_window_max_chunks,
        window_overlap_chunks=settings.graph_extraction_window_overlap_chunks,
        max_output_tokens=settings.graph_extraction_max_output_tokens,
        max_entities=settings.graph_extraction_max_entities,
        max_relations=settings.graph_extraction_max_relations,
        response_observer=tracker.observe,
    )
    if mode == "openai":
        return cast(EntityRelationExtractorPort, structured)
    return HybridEntityRelationExtractor([rule, structured])


def _default_output_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(".data/evals") / f"graph_extraction_{timestamp}.json"


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
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
