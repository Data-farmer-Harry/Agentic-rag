from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from uuid import UUID

from app.bootstrap import build_components
from app.graph.backfill import GraphBackfillProgress, GraphBackfillService


class _ProgressRenderer:
    def __init__(self) -> None:
        self._started_at = time.monotonic()
        self._is_tty = sys.stderr.isatty()

    def __call__(self, progress: GraphBackfillProgress) -> None:
        elapsed = max(0.0, time.monotonic() - self._started_at)
        ratio = progress.processed / progress.total if progress.total else 1.0
        width = 28
        filled = min(width, int(ratio * width))
        bar = "#" * filled + "-" * (width - filled)
        if progress.processed:
            seconds_per_document = elapsed / progress.processed
            eta_seconds = seconds_per_document * (progress.total - progress.processed)
            eta = _format_duration(eta_seconds)
        else:
            eta = "--:--"
        line = (
            f"KG [{bar}] {progress.processed:>3}/{progress.total:<3} "
            f"ok={progress.completed:<3} failed={progress.failed:<3} "
            f"entities={progress.entity_candidates:<5} "
            f"relations={progress.relation_candidates:<5} "
            f"elapsed={_format_duration(elapsed)} eta={eta}"
        )
        finished = progress.processed >= progress.total
        if self._is_tty:
            print(
                f"\r{line}\033[K",
                file=sys.stderr,
                end="\n" if finished else "",
                flush=True,
            )
        else:
            print(line, file=sys.stderr, flush=True)


def _format_duration(seconds: float) -> str:
    bounded = max(0, int(seconds))
    minutes, remaining = divmod(bounded, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{remaining:02d}"
    return f"{minutes:02d}:{remaining:02d}"


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Run a bounded and resumable semantic knowledge-graph backfill"
    )
    cli.add_argument("--tenant-id", default="local")
    cli.add_argument("--project-id", default="computer-science")
    cli.add_argument("--document-id", action="append", default=[])
    cli.add_argument("--limit", type=int)
    cli.add_argument("--concurrency", type=int, default=1)
    cli.add_argument("--checkpoint", type=Path)
    cli.add_argument("--force", action="store_true")
    cli.add_argument(
        "--skip-errors",
        action="store_true",
        help="Keep current error checkpoints and continue with unseen documents",
    )
    cli.add_argument("--fail-fast", action="store_true")
    cli.add_argument("--dry-run", action="store_true")
    cli.add_argument(
        "--allow-rule",
        action="store_true",
        help="Permit a rule-only extractor; omitted by default to protect semantic backfills",
    )
    return cli


async def run(args: argparse.Namespace) -> int:
    components = build_components()
    if components.settings.graph_extractor_mode == "rule" and not args.allow_rule:
        raise RuntimeError(
            "Semantic graph backfill requires GRAPH_EXTRACTOR_MODE=openai or hybrid"
        )
    revision = str(
        getattr(
            components.graph_extractor,
            "revision",
            type(components.graph_extractor).__name__,
        )
    )
    service = GraphBackfillService(
        components.knowledge_repository,
        components.graph_candidate_repository,
        components.graph_enrichment_coordinator,
        extractor_revision=revision,
        checkpoint_path=(
            args.checkpoint
            or components.settings.data_dir / "graph_backfill_manifest.json"
        ),
        structural_index=components.graph_structural_index,
    )
    await components.start()
    try:
        summary = await service.run(
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            document_ids=[UUID(value) for value in args.document_id],
            limit=args.limit,
            force=args.force,
            skip_errors=args.skip_errors,
            fail_fast=args.fail_fast,
            dry_run=args.dry_run,
            concurrency=args.concurrency,
            progress_callback=None if args.dry_run else _ProgressRenderer(),
        )
    finally:
        await components.close()
    print(summary.model_dump_json(indent=2))
    return 0 if summary.documents_failed == 0 else 1


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
