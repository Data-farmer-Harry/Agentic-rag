"""Import the checked-in enterprise fixture through the standard ingestion path."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.bootstrap import ApplicationComponents, build_components
from app.config import get_settings
from app.demo.enterprise_fixture import EnterpriseFixtureRun
from app.domain.enums import DocumentStatus


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Validate and import the enterprise engineering knowledge fixture."
    )
    command.add_argument(
        "--root",
        type=Path,
        default=Path("examples/enterprise_knowledge"),
    )
    command.add_argument("--tenant", default="local")
    command.add_argument("--project", default="default")
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--reset-fixture", action="store_true")
    command.add_argument(
        "--no-wait",
        action="store_true",
        help="Return after durable ingestion jobs are queued.",
    )
    return command


async def run(args: argparse.Namespace) -> int:
    settings = get_settings().model_copy(
        update={"enterprise_fixture_root": Path(args.root)}
    )
    components = build_components(settings)
    await components.start()
    try:
        workspace = components.workspace_service
        if args.reset_fixture:
            result = await workspace.reset_enterprise_fixture(
                tenant_id=args.tenant,
                project_id=args.project,
            )
            print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
            return 0

        preview = await workspace.preview_enterprise_fixture(
            tenant_id=args.tenant,
            project_id=args.project,
        )
        run_result = await workspace.start_enterprise_fixture(
            tenant_id=args.tenant,
            project_id=args.project,
            requested_by="fixture-cli",
            dry_run=args.dry_run,
        )
        if not args.no_wait:
            run_result = await _wait_for_terminal(workspace=workspace, run=run_result)
        summary = await _fixture_summary(
            components,
            tenant_id=args.tenant,
            project_id=args.project,
            run=run_result,
        )
        print(
            json.dumps(
                {
                    "preview": preview.model_dump(mode="json"),
                    "run": run_result.model_dump(mode="json"),
                    "summary": summary,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if run_result.status == "succeeded" else 1
    finally:
        await components.close()


async def _wait_for_terminal(
    *,
    workspace: Any,
    run: EnterpriseFixtureRun,
) -> EnterpriseFixtureRun:
    current = run
    while current.status in {"queued", "running"}:
        await asyncio.sleep(0.2)
        refreshed = await workspace.enterprise_fixture_status(
            current.run_id,
            tenant_id=current.tenant_id,
            project_id=current.project_id,
        )
        if refreshed is None:
            return current.model_copy(
                update={
                    "status": "failed",
                    "errors": {**current.errors, "run": "run_not_found"},
                }
            )
        current = refreshed
    return current


async def _fixture_summary(
    components: ApplicationComponents,
    *,
    tenant_id: str,
    project_id: str,
    run: EnterpriseFixtureRun,
) -> dict[str, int]:
    documents = await components.knowledge_repository.list_documents(
        tenant_id=tenant_id,
        project_id=project_id,
        include_archived=True,
    )
    fixture_documents = [
        document
        for document in documents
        if document.source.fixture_id == "enterprise_knowledge"
    ]
    fixture_document_ids = {document.document_id for document in fixture_documents}
    candidates = await components.graph_candidate_service.list_candidates(
        tenant_id=tenant_id,
        project_id=project_id,
    )
    graph_candidate_count = sum(
        candidate.document_id in fixture_document_ids for candidate in candidates.entities
    ) + sum(
        candidate.document_id in fixture_document_ids for candidate in candidates.relations
    )
    return {
        "planned": len(run.plan),
        "succeeded": len(run.completed_document_ids),
        "deduplicated": sum(item.action == "unchanged" for item in run.plan),
        "failed": len(run.errors),
        "fixture_documents": len(fixture_documents),
        "active_fixture_documents": sum(
            document.status == DocumentStatus.ACTIVE for document in fixture_documents
        ),
        "fixture_chunks": sum(document.chunk_count for document in fixture_documents),
        "graph_candidates": graph_candidate_count,
    }


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
