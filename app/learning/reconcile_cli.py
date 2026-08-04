from __future__ import annotations

import argparse
import asyncio
import json

from app.config import get_settings
from app.infra.postgres import PostgresDatabase
from app.infra.postgres_learning_artifacts import LEARNING_ARTIFACT_MIGRATIONS
from app.infra.postgres_learning_jobs import LEARNING_JOB_MIGRATIONS
from app.infra.postgres_learning_reconciliation import PostgresLearningReconciler


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Verify and repair durable learning checkpoint artifact links.",
    )
    command.add_argument("--limit", type=int, default=1_000)
    command.add_argument("--tenant-id")
    command.add_argument("--project-id")
    command.add_argument("--include-verified", action="store_true")
    command.add_argument(
        "--allow-required",
        action="store_true",
        help="Return exit code 0 even when manual reconciliation is required.",
    )
    return command


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.postgres_dsn:
        raise ValueError("POSTGRES_DSN is required for learning reconciliation")
    database = PostgresDatabase(
        settings.postgres_dsn,
        command_timeout_seconds=min(settings.agent_timeout_seconds, 60),
    )
    await database.start()
    try:
        await database.migrate(
            (*LEARNING_JOB_MIGRATIONS, *LEARNING_ARTIFACT_MIGRATIONS)
        )
        report = await PostgresLearningReconciler(database).reconcile(
            limit=args.limit,
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            include_verified=args.include_verified,
        )
    finally:
        await database.close()
    print(
        json.dumps(
            {
                "inspected": report.inspected,
                "verified": report.verified,
                "required": report.required,
                "links_repaired": report.links_repaired,
                "issues": [
                    {"job_id": str(issue.job_id), "error": issue.error}
                    for issue in report.issues
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if args.allow_required or report.required == 0 else 2


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
