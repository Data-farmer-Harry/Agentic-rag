from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import UUID

from app.bootstrap import build_components
from app.graph.structure_reindex import GraphStructureReindexService


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Rebuild the Neo4j Document/Chunk graph without model calls"
    )
    cli.add_argument("--tenant-id", default="local")
    cli.add_argument("--project-id", default="computer-science")
    cli.add_argument("--document-id", action="append", default=[])
    cli.add_argument("--limit", type=int)
    cli.add_argument("--concurrency", type=int, default=4)
    cli.add_argument("--checkpoint", type=Path)
    cli.add_argument("--force", action="store_true")
    cli.add_argument("--fail-fast", action="store_true")
    cli.add_argument("--dry-run", action="store_true")
    return cli


async def run(args: argparse.Namespace) -> int:
    components = build_components()
    if components.graph_structural_index is None:
        raise RuntimeError("Structural graph reindex requires GRAPH_BACKEND=neo4j")
    service = GraphStructureReindexService(
        components.knowledge_repository,
        components.graph_structural_index,
        checkpoint_path=(
            args.checkpoint
            or components.settings.data_dir / "graph_structure_reindex_manifest.json"
        ),
    )
    await components.start()
    try:
        summary = await service.run(
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            document_ids=[UUID(value) for value in args.document_id],
            limit=args.limit,
            force=args.force,
            fail_fast=args.fail_fast,
            dry_run=args.dry_run,
            concurrency=args.concurrency,
        )
    finally:
        await components.close()
    print(summary.model_dump_json(indent=2))
    return 0 if summary.documents_failed == 0 else 1


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
