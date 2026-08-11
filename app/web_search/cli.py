from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from typing import Any

from app.config import get_settings
from app.domain.models import RunContext, WebSearchRequest
from app.web_search.openai_web_search import OpenAIHostedWebSearch


async def _run(query: str, max_results: int) -> int:
    settings = get_settings()
    if settings.web_search_mode != "openai":
        print(
            json.dumps(
                {
                    "status": "disabled",
                    "message": "Set WEB_SEARCH_MODE=openai to run the live gate.",
                }
            )
        )
        return 2

    search = OpenAIHostedWebSearch(settings)
    try:
        try:
            result = await search.search_web(
                WebSearchRequest(query=query, max_results=max_results),
                RunContext(),
            )
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "status": "provider_error",
                        "error_type": type(exc).__name__,
                    }
                )
            )
            return 1
    finally:
        await search.close()

    cited = bool(result.evidence) and all(
        item.provenance.source_type == "web_search"
        and item.provenance.run_id is not None
        and item.provenance.locator.get("uri")
        for item in result.evidence
    )
    report: dict[str, Any] = {
        "status": "live_cited" if cited else "failed_no_cited_evidence",
        "query_fingerprint": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "model": result.trace.get("model"),
        "provider_revision": result.trace.get("provider_revision"),
        "response_id": result.trace.get("response_id"),
        "citation_count": result.trace.get("citation_count"),
        "returned_source_count": result.trace.get("returned_source_count"),
        "rejected_url_count": result.trace.get("rejected_url_count"),
        "stop_reason": result.trace.get("stop_reason"),
        "sources": [
            {"title": item.title, "url": item.url}
            for item in result.sources
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if cited else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Responses API hosted web search and URL-cited evidence."
    )
    parser.add_argument(
        "--query",
        default="What is the current OpenAI Responses API web search tool type?",
    )
    parser.add_argument("--max-results", type=int, default=3)
    args = parser.parse_args()
    return asyncio.run(_run(args.query, args.max_results))
