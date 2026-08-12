from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx

from app.bootstrap import build_components
from app.evaluation.self_learning import SelfLearningEffectEvaluator, SelfLearningEffectReport


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Audit real Experience/Pattern evidence for self-learning effectiveness"
    )
    value.add_argument("--tenant-id", default="local")
    value.add_argument("--project-id", default="default")
    value.add_argument("--output", type=Path, required=True)
    value.add_argument(
        "--base-url",
        help="Audit a running HermesGraph API instead of constructing local repositories",
    )
    value.add_argument("--api-token", help="Optional bearer token for the running API")
    value.add_argument("--minimum-experiences", type=int, default=20)
    value.add_argument("--minimum-feedback", type=int, default=5)
    return value


async def run(args: argparse.Namespace) -> int:
    if args.base_url:
        report = await _fetch_live_report(args)
        return _write_report(report, args.output)

    components = build_components()
    try:
        report = await SelfLearningEffectEvaluator(
            components.harness_experience_repository,
            components.harness_policy_repository,
            minimum_experiences=args.minimum_experiences,
            minimum_feedback=args.minimum_feedback,
        ).evaluate(tenant_id=args.tenant_id, project_id=args.project_id)
        return _write_report(report, args.output)
    finally:
        await components.close()


async def _fetch_live_report(args: argparse.Namespace) -> SelfLearningEffectReport:
    headers = {"Accept": "application/json"}
    if args.api_token:
        headers["Authorization"] = f"Bearer {args.api_token}"
    url = (
        f"{args.base_url.rstrip('/')}/v1/projects/{args.project_id}/"
        "harness/effectiveness"
    )
    params = {
        "minimum_experiences": args.minimum_experiences,
        "minimum_feedback": args.minimum_feedback,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
    return SelfLearningEffectReport.model_validate(response.json())


def _write_report(report: SelfLearningEffectReport, output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(report.model_dump_json(indent=2))
    return int(not report.passed)


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
