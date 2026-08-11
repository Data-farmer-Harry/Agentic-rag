from __future__ import annotations

import argparse
import asyncio

from app.bootstrap import build_components
from app.graph.maintenance import CandidateEvidenceReconciliationService
from app.graph.neo4j_evidence_graph import Neo4jEvidenceGraph


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Archive pending graph candidates backed by inactive chunks"
    )
    cli.add_argument("--tenant-id", default="local")
    cli.add_argument("--project-id", default="computer-science")
    cli.add_argument("--concurrency", type=int, default=8)
    cli.add_argument("--dry-run", action="store_true")
    return cli


async def run(args: argparse.Namespace) -> int:
    components = build_components()
    if not isinstance(components.graph_backend, Neo4jEvidenceGraph):
        raise RuntimeError("Candidate reconciliation requires GRAPH_BACKEND=neo4j")
    service = CandidateEvidenceReconciliationService(
        components.knowledge_repository,
        components.graph_candidate_repository,
        components.graph_backend,
    )
    await components.start()
    try:
        summary = await service.run(
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
    finally:
        await components.close()
    print(summary.model_dump_json(indent=2))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
