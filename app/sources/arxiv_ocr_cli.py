from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.agent.model_provider import build_model_client
from app.config import get_settings
from app.sources.arxiv_ocr import ArxivOcrProcessor, OpenAIPdfPageOcr


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="Extract an arXiv corpus text layer and use GPT Vision OCR only when needed"
    )
    cli.add_argument("--source-root", type=Path, default=Path(".data/arxiv"))
    cli.add_argument("--output-root", type=Path, default=Path(".data/arxiv/ocr"))
    cli.add_argument("--arxiv-id", action="append", default=[])
    cli.add_argument("--min-text-chars", type=int, default=80)
    cli.add_argument("--render-dpi", type=int, default=180)
    cli.add_argument("--model")
    cli.add_argument("--detail", choices=("low", "high", "auto"))
    cli.add_argument("--max-retries", type=int, choices=range(0, 6), default=2)
    cli.add_argument(
        "--text-only",
        action="store_true",
        help="Extract embedded PDF text and defer low-text pages without calling a model",
    )
    cli.add_argument("--force", action="store_true")
    return cli


async def run(args: argparse.Namespace) -> int:
    page_ocr = None
    if not args.text_only:
        settings = get_settings()
        client = build_model_client(
            settings,
            timeout=min(settings.agent_timeout_seconds, 180),
            max_retries=args.max_retries,
        )
        page_ocr = OpenAIPdfPageOcr(
            client,
            model=args.model or settings.vision_model or settings.openai_model,
            detail=args.detail or settings.vision_detail,
            max_output_tokens=max(settings.vision_max_output_tokens, 8_000),
        )
    processor = ArxivOcrProcessor(
        source_root=args.source_root,
        output_root=args.output_root,
        page_ocr=page_ocr,
        min_text_chars=args.min_text_chars,
        render_dpi=args.render_dpi,
    )
    try:
        summary = await processor.run(arxiv_ids=args.arxiv_id, force=args.force)
    finally:
        if page_ocr is not None:
            await page_ocr.close()
    print(summary.model_dump_json(indent=2))
    return 0 if summary.documents_failed == 0 else 1


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
