from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.config import get_settings
from app.evaluation.web_search import (
    OpenAIWebSearchEvaluationBackend,
    WebSearchEvalThresholds,
    WebSearchEvaluator,
    WebSearchGoldenSet,
)

ExecutionSelection = Literal["all", "live", "contract"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate hosted Web Search quality, citations, and policy contracts"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("examples/evaluation/web_search_golden.json"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model")
    parser.add_argument(
        "--execution",
        choices=("all", "live", "contract"),
        default="all",
        help="Run every case, live-provider cases, or deterministic contract cases",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=[],
        help="Evaluate only this case ID; repeat the option to select more cases",
    )
    parser.add_argument("--case-attempts", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--retry-base-seconds", type=float, default=0.25)
    parser.add_argument("--input-cost-per-million", type=float)
    parser.add_argument("--cached-input-cost-per-million", type=float)
    parser.add_argument("--output-cost-per-million", type=float)
    parser.add_argument("--minimum-case-pass-rate", type=float, default=0.90)
    parser.add_argument(
        "--minimum-provider-only-success-rate", type=float, default=0.90
    )
    parser.add_argument("--minimum-citation-coverage", type=float, default=0.95)
    parser.add_argument("--minimum-source-precision", type=float, default=0.90)
    parser.add_argument("--minimum-primary-source-rate", type=float, default=0.80)
    parser.add_argument("--minimum-term-recall", type=float, default=0.80)
    parser.add_argument("--minimum-freshness-accuracy", type=float, default=1.0)
    parser.add_argument("--minimum-policy-accuracy", type=float, default=1.0)
    parser.add_argument("--minimum-resilience-accuracy", type=float, default=1.0)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit zero after writing the report, even when the gate fails",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    _validate_prices(args)
    dataset_path = args.dataset.resolve()
    golden_set = _select_cases(
        WebSearchGoldenSet.load(dataset_path),
        args.case_id,
        args.execution,
    )
    settings = get_settings()
    if args.model:
        settings = settings.model_copy(update={"web_search_model": args.model})
    backend = OpenAIWebSearchEvaluationBackend(
        settings,
        contract_only=not any(case.execution_mode == "live" for case in golden_set.cases),
    )
    evaluator = WebSearchEvaluator(
        backend,
        thresholds=WebSearchEvalThresholds(
            minimum_case_pass_rate=args.minimum_case_pass_rate,
            minimum_provider_only_success_rate=(
                args.minimum_provider_only_success_rate
            ),
            minimum_citation_coverage=args.minimum_citation_coverage,
            minimum_source_precision=args.minimum_source_precision,
            minimum_primary_source_rate=args.minimum_primary_source_rate,
            minimum_term_recall=args.minimum_term_recall,
            minimum_freshness_accuracy=args.minimum_freshness_accuracy,
            minimum_policy_accuracy=args.minimum_policy_accuracy,
            minimum_resilience_accuracy=args.minimum_resilience_accuracy,
        ),
        max_case_attempts=args.case_attempts,
        retry_base_seconds=args.retry_base_seconds,
        input_cost_per_million=args.input_cost_per_million,
        cached_input_cost_per_million=args.cached_input_cost_per_million,
        output_cost_per_million=args.output_cost_per_million,
    )
    try:
        report = await evaluator.run(golden_set)
    finally:
        await backend.close()
    payload = report.model_dump_json(indent=2)
    output = args.output or _default_output_path()
    _write_atomic(output, payload + "\n")
    print(payload)
    print(f"report_path={output.resolve()}")
    return 0 if report.passed or args.report_only else 1


def _validate_prices(args: argparse.Namespace) -> None:
    if (args.input_cost_per_million is None) != (args.output_cost_per_million is None):
        raise ValueError("Both input and output token prices must be provided together")
    if args.cached_input_cost_per_million is not None and args.input_cost_per_million is None:
        raise ValueError("Cached input price requires input and output token prices")


def _select_cases(
    golden_set: WebSearchGoldenSet,
    selected_case_ids: list[str],
    execution: ExecutionSelection,
) -> WebSearchGoldenSet:
    if len(selected_case_ids) != len(set(selected_case_ids)):
        raise ValueError("Web Search case IDs cannot be selected more than once")
    available = {case.case_id for case in golden_set.cases}
    requested = set(selected_case_ids)
    unknown = sorted(requested - available)
    if unknown:
        raise ValueError(f"Unknown Web Search case IDs: {', '.join(unknown)}")
    selected = [
        case
        for case in golden_set.cases
        if (not requested or case.case_id in requested)
        and (
            execution == "all"
            or (execution == "live" and case.execution_mode == "live")
            or (execution == "contract" and case.execution_mode != "live")
        )
    ]
    if not selected:
        raise ValueError("Web Search case selection is empty")
    if not requested and execution == "all":
        return golden_set
    selection = "\n".join(case.case_id for case in selected)
    fingerprint = hashlib.sha256(f"{execution}\n{selection}".encode()).hexdigest()[:12]
    return golden_set.model_copy(
        update={
            "name": f"{golden_set.name} (selected cases)",
            "revision": f"{golden_set.revision}+subset.{fingerprint}",
            "cases": selected,
        }
    )


def _default_output_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(".data/evals") / f"web_search_{timestamp}.json"


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
