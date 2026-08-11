"""Import the checked-in enterprise fixture through the standard ingestion path."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from app.bootstrap import ApplicationComponents, build_components
from app.config import get_settings
from app.demo.enterprise_fixture import (
    EnterpriseFixtureRun,
    EnterpriseFixtureValidator,
    _fixture_fingerprint,
)
from app.domain.enums import DocumentStatus, GraphCandidateStatus


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
    operation = command.add_mutually_exclusive_group()
    operation.add_argument("--reset-fixture", action="store_true")
    operation.add_argument(
        "--status",
        type=UUID,
        metavar="RUN_ID",
        help="Read one fixture import run without starting another import.",
    )
    command.add_argument(
        "--no-wait",
        action="store_true",
        help="Return after durable ingestion jobs are queued.",
    )
    return command


async def run(args: argparse.Namespace) -> int:
    settings = get_settings().model_copy(update={"enterprise_fixture_root": Path(args.root)})
    components = build_components(settings)
    await components.start()
    try:
        workspace = components.workspace_service
        status_run_id = getattr(args, "status", None)
        if status_run_id is not None:
            run_result = await workspace.enterprise_fixture_status(
                status_run_id,
                tenant_id=args.tenant,
                project_id=args.project,
            )
            if run_result is None:
                print(
                    json.dumps(
                        {
                            "run_id": str(status_run_id),
                            "status": "not_found",
                            **_lifecycle_payload(
                                operation="status",
                                run=None,
                                lifecycle_status="not_found",
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 1
            summary = await _fixture_summary(
                components,
                tenant_id=args.tenant,
                project_id=args.project,
                run=run_result,
            )
            print(
                json.dumps(
                    {
                        "run": run_result.model_dump(mode="json"),
                        "summary": summary,
                        **_lifecycle_payload(
                            operation="status",
                            run=run_result,
                            summary=summary,
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if run_result.status != "failed" else 1

        if args.reset_fixture:
            fixture_fingerprint = _fixture_snapshot_fingerprint(Path(args.root))
            result = await workspace.reset_enterprise_fixture(
                tenant_id=args.tenant,
                project_id=args.project,
            )
            summary = await _fixture_summary(
                components,
                tenant_id=args.tenant,
                project_id=args.project,
                run=None,
            )
            print(
                json.dumps(
                    {
                        **result.model_dump(mode="json"),
                        "summary": summary,
                        **_lifecycle_payload(
                            operation="reset",
                            run=None,
                            fixture_fingerprint=fixture_fingerprint,
                            lifecycle_status="reset",
                            summary=summary,
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
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
                    **_lifecycle_payload(
                        operation="import",
                        run=run_result,
                        summary=summary,
                    ),
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
    run: EnterpriseFixtureRun | None,
) -> dict[str, int | None]:
    documents = await components.knowledge_repository.list_documents(
        tenant_id=tenant_id,
        project_id=project_id,
        include_archived=True,
    )
    fixture_documents = [
        document for document in documents if document.source.fixture_id == "enterprise_knowledge"
    ]
    fixture_document_ids = {document.document_id for document in fixture_documents}
    candidates = await components.graph_candidate_service.list_candidates(
        tenant_id=tenant_id,
        project_id=project_id,
    )
    graph_candidate_count = sum(
        candidate.document_id in fixture_document_ids for candidate in candidates.entities
    ) + sum(candidate.document_id in fixture_document_ids for candidate in candidates.relations)
    curated_entities = [
        candidate
        for candidate in candidates.entities
        if candidate.extractor_revision.startswith("curated-enterprise-fixture:")
    ]
    curated_relations = [
        candidate
        for candidate in candidates.relations
        if candidate.extractor_revision.startswith("curated-enterprise-fixture:")
    ]
    return {
        "planned": len(run.plan) if run is not None else 0,
        "succeeded": len(run.completed_document_ids) if run is not None else 0,
        "deduplicated": (
            sum(item.action == "unchanged" for item in run.plan) if run is not None else 0
        ),
        "failed": len(run.errors) if run is not None else 0,
        "fixture_documents": len(fixture_documents),
        "active_fixture_documents": sum(
            document.status == DocumentStatus.ACTIVE for document in fixture_documents
        ),
        "fixture_chunks": sum(document.chunk_count for document in fixture_documents),
        "graph_candidates": graph_candidate_count,
        "curated_graph_entities": sum(
            candidate.status == GraphCandidateStatus.APPROVED for candidate in curated_entities
        ),
        "curated_graph_relations": sum(
            candidate.status == GraphCandidateStatus.APPROVED for candidate in curated_relations
        ),
        "archived_curated_graph_entities": sum(
            candidate.status == GraphCandidateStatus.ARCHIVED for candidate in curated_entities
        ),
        "archived_curated_graph_relations": sum(
            candidate.status == GraphCandidateStatus.ARCHIVED for candidate in curated_relations
        ),
        "run_curated_graph_entities": (run.curated_graph_entities if run is not None else None),
        "run_curated_graph_relations": (run.curated_graph_relations if run is not None else None),
    }


def _fixture_snapshot_fingerprint(root: Path) -> str:
    """Use the same immutable fixture snapshot identity as the importer."""

    return _fixture_fingerprint(EnterpriseFixtureValidator(root).load())


def _lifecycle_payload(
    *,
    operation: str,
    run: EnterpriseFixtureRun | None,
    fixture_fingerprint: str | None = None,
    lifecycle_status: str | None = None,
    summary: dict[str, int | None] | None = None,
) -> dict[str, dict[str, object]]:
    """Add stable operational fields without changing the legacy result envelopes."""

    run_payload = run.model_dump(mode="json") if run is not None else None
    curated_graph: dict[str, object] = {
        "entities": summary["curated_graph_entities"] if summary is not None else None,
        "relations": summary["curated_graph_relations"] if summary is not None else None,
        "archived_entities": (
            summary["archived_curated_graph_entities"] if summary is not None else None
        ),
        "archived_relations": (
            summary["archived_curated_graph_relations"] if summary is not None else None
        ),
        "run_entities": run.curated_graph_entities if run is not None else None,
        "run_relations": run.curated_graph_relations if run is not None else None,
    }
    return {
        "lifecycle": {
            "operation": operation,
            "run_id": run_payload["run_id"] if run_payload is not None else None,
            "status": run.status if run is not None else lifecycle_status,
            "fixture_fingerprint": (
                run.fixture_fingerprint if run is not None else fixture_fingerprint
            ),
            "lifecycle_generation": run.lifecycle_generation if run is not None else None,
            "generation_observed": run is not None,
            "created_at": run_payload["created_at"] if run_payload is not None else None,
            "updated_at": run_payload["updated_at"] if run_payload is not None else None,
            "completed_at": run_payload["completed_at"] if run_payload is not None else None,
        },
        "curated_graph": curated_graph,
    }


def main(argv: Sequence[str] | None = None) -> None:
    args = parser().parse_args(argv)
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
