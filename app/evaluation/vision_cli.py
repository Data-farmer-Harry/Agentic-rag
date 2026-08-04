from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.agent.model_provider import build_model_client
from app.config import get_settings
from app.evaluation.graph_extraction import OpenAIUsageAccumulator
from app.evaluation.vision import (
    VisionEvalThresholds,
    VisionEvaluator,
    VisionGoldenSet,
)
from app.vision import OpenAIVisionAnalyzer


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate OpenAI Vision knowledge extraction against a golden set"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("examples/evaluation/vision_golden.json"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--detail", choices=("low", "high", "auto"))
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Evaluate only this case ID; repeat the option to select more cases",
    )
    parser.add_argument("--max-retries", type=int, choices=range(0, 6), default=2)
    parser.add_argument(
        "--case-attempts",
        type=int,
        choices=range(1, 6),
        default=2,
        help="Retry a case only after a transient model transport or service error",
    )
    parser.add_argument("--input-cost-per-million", type=float)
    parser.add_argument("--cached-input-cost-per-million", type=float)
    parser.add_argument("--output-cost-per-million", type=float)
    parser.add_argument("--minimum-success-rate", type=float, default=0.95)
    parser.add_argument("--minimum-case-pass-rate", type=float, default=0.80)
    parser.add_argument("--minimum-title-term-recall", type=float, default=0.85)
    parser.add_argument("--minimum-summary-term-recall", type=float, default=0.80)
    parser.add_argument("--minimum-ocr-recall", type=float, default=0.85)
    parser.add_argument("--minimum-region-recall", type=float, default=0.80)
    parser.add_argument("--minimum-region-category-accuracy", type=float, default=0.90)
    parser.add_argument("--minimum-region-text-recall", type=float, default=0.80)
    parser.add_argument("--minimum-bbox-accuracy", type=float, default=0.70)
    parser.add_argument("--minimum-forbidden-content-accuracy", type=float, default=1.0)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit zero after writing the report, even when the gate fails",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    if (args.input_cost_per_million is None) != (args.output_cost_per_million is None):
        raise ValueError("Both input and output token prices must be provided together")
    if args.cached_input_cost_per_million is not None and args.input_cost_per_million is None:
        raise ValueError("Cached input price requires input and output token prices")
    dataset_path = args.dataset.resolve()
    golden_set = _select_cases(VisionGoldenSet.load(dataset_path), args.case_id)
    settings = get_settings()
    tracker = OpenAIUsageAccumulator()
    client = build_model_client(
        settings,
        timeout=min(settings.agent_timeout_seconds, 180),
        max_retries=args.max_retries,
    )
    analyzer = OpenAIVisionAnalyzer(
        client,
        model=args.model or settings.vision_model or settings.openai_model,
        detail=args.detail or settings.vision_detail,
        max_output_tokens=settings.vision_max_output_tokens,
        max_regions=settings.vision_max_regions,
        response_observer=tracker.observe,
    )
    evaluator = VisionEvaluator(
        analyzer,
        asset_root=dataset_path.parent,
        thresholds=VisionEvalThresholds(
            minimum_success_rate=args.minimum_success_rate,
            minimum_case_pass_rate=args.minimum_case_pass_rate,
            minimum_title_term_recall=args.minimum_title_term_recall,
            minimum_summary_term_recall=args.minimum_summary_term_recall,
            minimum_ocr_recall=args.minimum_ocr_recall,
            minimum_region_recall=args.minimum_region_recall,
            minimum_region_category_accuracy=(args.minimum_region_category_accuracy),
            minimum_region_text_recall=args.minimum_region_text_recall,
            minimum_bbox_accuracy=args.minimum_bbox_accuracy,
            minimum_forbidden_content_accuracy=(args.minimum_forbidden_content_accuracy),
        ),
        usage_probe=tracker.snapshot,
        input_cost_per_million=args.input_cost_per_million,
        cached_input_cost_per_million=args.cached_input_cost_per_million,
        output_cost_per_million=args.output_cost_per_million,
        max_case_attempts=args.case_attempts,
    )
    try:
        report = await evaluator.run(golden_set)
    finally:
        await analyzer.close()
    payload = report.model_dump_json(indent=2)
    output = args.output or _default_output_path()
    _write_atomic(output, payload + "\n")
    print(payload)
    print(f"report_path={output.resolve()}")
    return 0 if report.passed or args.report_only else 1


def _select_cases(
    golden_set: VisionGoldenSet,
    selected_case_ids: list[str],
) -> VisionGoldenSet:
    if not selected_case_ids:
        return golden_set
    if len(selected_case_ids) != len(set(selected_case_ids)):
        raise ValueError("Vision case IDs cannot be selected more than once")
    requested = set(selected_case_ids)
    available = {case.case_id for case in golden_set.cases}
    unknown = sorted(requested - available)
    if unknown:
        raise ValueError(f"Unknown Vision case IDs: {', '.join(unknown)}")
    selected = [case for case in golden_set.cases if case.case_id in requested]
    fingerprint = hashlib.sha256("\n".join(sorted(requested)).encode()).hexdigest()[:12]
    return golden_set.model_copy(
        update={
            "name": f"{golden_set.name} (selected cases)",
            "revision": f"{golden_set.revision}+subset.{fingerprint}",
            "cases": selected,
        }
    )


def _default_output_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(".data/evals") / f"vision_extraction_{timestamp}.json"


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
