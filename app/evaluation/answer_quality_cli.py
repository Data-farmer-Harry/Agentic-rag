"""CLI for deterministic answer-quality evaluation.

This command never invokes a provider.  It evaluates a recorded answer artifact
or, only when explicitly requested, the clearly labelled offline fixture stored
inside the golden set.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from app.evaluation.answer_quality import (
    AnswerQualityArtifactSet,
    AnswerQualityEvaluator,
    embedded_fixture_artifacts,
    load_answer_quality_golden_set,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate answer claim support, citation coverage, and hallucination rate"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("examples/evaluation/answer_quality_golden.json"),
    )
    parser.add_argument(
        "--answers",
        type=Path,
        help="Recorded answer artifact JSON; this command does not generate answers.",
    )
    parser.add_argument(
        "--use-embedded-fixture",
        action="store_true",
        help="Evaluate only the dataset's explicitly labelled offline fixture artifacts.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Write the report but return success even when quality gates fail.",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.answers is not None and args.use_embedded_fixture:
        raise ValueError("--answers and --use-embedded-fixture cannot be used together")
    if args.answers is None and not args.use_embedded_fixture:
        raise ValueError("provide --answers or explicitly pass --use-embedded-fixture")
    dataset = load_answer_quality_golden_set(args.dataset)
    artifacts = (
        AnswerQualityArtifactSet.load(args.answers)
        if args.answers is not None
        else embedded_fixture_artifacts(dataset)
    )
    report = AnswerQualityEvaluator(dataset).evaluate(artifacts)
    payload = report.model_dump_json(indent=2)
    _write_atomic(args.output, payload + "\n")
    print(payload)
    print(f"report_path={args.output.resolve()}")
    return 0 if args.report_only or report.passed else 1


def main() -> None:
    raise SystemExit(_run(_parser().parse_args()))


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
