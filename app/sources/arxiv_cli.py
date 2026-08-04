from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.sources.arxiv import (
    DEFAULT_CATEGORIES,
    DEFAULT_TOPICS,
    ArxivIngestionClient,
    ArxivSourceConnector,
    ArxivSourceStore,
    ArxivSyncConfig,
)


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Synchronize a bounded, resumable computer-science arXiv corpus"
    )
    cli.add_argument("--root", type=Path, default=Path(".data/arxiv"))
    cli.add_argument("--query")
    cli.add_argument("--category", action="append", dest="categories")
    cli.add_argument("--topic", action="append", dest="topics")
    cli.add_argument("--max-results", type=int, default=100)
    cli.add_argument("--max-downloads", type=int, default=25)
    cli.add_argument("--max-pdf-mb", type=float, default=10.0)
    cli.add_argument("--max-total-mb", type=float, default=250.0)
    cli.add_argument("--request-delay", type=float, default=1.0)
    cli.add_argument("--user-agent")
    cli.add_argument(
        "--ingest-base-url",
        help="Optional HermesGraph base URL, for example http://127.0.0.1:8001",
    )
    cli.add_argument("--project-id", default="computer-science")
    cli.add_argument("--user-id", default="local-user")
    submission_mode = cli.add_mutually_exclusive_group()
    submission_mode.add_argument(
        "--refresh-submitted",
        action="store_true",
        help="Resubmit cached PDFs so richer source metadata can refresh existing indexes",
    )
    submission_mode.add_argument(
        "--submit-pending",
        action="store_true",
        help="Submit only cached PDFs that do not already have an ingestion job ID",
    )
    return cli


async def run(args: argparse.Namespace) -> int:
    defaults = ArxivSyncConfig()
    config = ArxivSyncConfig(
        query=args.query,
        categories=tuple(args.categories or DEFAULT_CATEGORIES),
        topics=tuple(args.topics or DEFAULT_TOPICS),
        max_results=args.max_results,
        max_downloads=args.max_downloads,
        max_pdf_bytes=int(args.max_pdf_mb * 1_000_000),
        max_total_bytes=int(args.max_total_mb * 1_000_000),
        request_delay_seconds=args.request_delay,
        user_agent=args.user_agent or defaults.user_agent,
    )
    ingestor = (
        ArxivIngestionClient(
            args.ingest_base_url,
            project_id=args.project_id,
            user_id=args.user_id,
        )
        if args.ingest_base_url
        else None
    )
    connector = ArxivSourceConnector(
        config,
        ArxivSourceStore(args.root),
        ingestor=ingestor,
    )
    try:
        if args.refresh_submitted:
            summary = await connector.refresh_cached_submissions()
        elif args.submit_pending:
            summary = await connector.submit_pending_cached()
        else:
            summary = await connector.sync()
    finally:
        await connector.close()
    print(summary.model_dump_json(indent=2))
    return 0 if summary.errors == 0 else 1


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
