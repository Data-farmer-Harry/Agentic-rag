from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.domain.models import RunContext, WebSearchRequest, WebSearchResult
from app.web_search.provider_chain import WebSearchProvider, WebSearchProviderChain


@dataclass
class _Provider:
    result: WebSearchResult | None = None
    error: Exception | None = None
    received_query: str | None = None

    async def search_web(self, request: WebSearchRequest, context: RunContext) -> WebSearchResult:
        del context
        self.received_query = request.query
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@pytest.mark.asyncio
async def test_provider_chain_uses_brave_after_hosted_error_and_keeps_audit_trace() -> None:
    hosted = _Provider(error=RuntimeError("hosted unsupported"))
    brave = _Provider(
        result=WebSearchResult(
            query="current software release",
            evidence=[],
            trace={"provider": "brave_search_api"},
        )
    )
    public = _Provider(
        result=WebSearchResult(
            query="current software release",
            evidence=[],
            trace={"provider": "duckduckgo_html"},
        )
    )
    chain = WebSearchProviderChain(
        (
            WebSearchProvider("openai_responses", hosted, timeout_seconds=1),
            WebSearchProvider("brave_search_api", brave, timeout_seconds=1),
            WebSearchProvider("public_html_search", public, timeout_seconds=1),
        )
    )

    result = await chain.search_web(
        WebSearchRequest(query="current software release"),
        RunContext(),
    )

    assert result.trace["provider_chain"] == [
        "openai_responses",
        "brave_search_api",
        "duckduckgo_html",
    ]
    assert result.trace["provider_attempts"][0]["error_type"] == "RuntimeError"
    assert result.trace["stop_reason"] == "all_providers_exhausted"


@pytest.mark.asyncio
async def test_provider_chain_returns_first_cited_provider_and_marks_fallback() -> None:
    from app.domain.enums import TrustLevel
    from app.domain.models import EvidenceRef, Provenance, utc_now

    evidence = EvidenceRef(
        text="Brave result",
        title="Brave source",
        score=0.9,
        provenance=Provenance(
            source_type="web_search",
            source_id="https://example.com",
            locator={"uri": "https://example.com"},
            trust=TrustLevel.UNTRUSTED,
            observed_at=utc_now(),
        ),
    )
    hosted = _Provider(error=TimeoutError())
    brave = _Provider(
        result=WebSearchResult(
            query="current software release",
            evidence=[evidence],
            trace={"provider": "brave_search_api"},
        )
    )
    chain = WebSearchProviderChain(
        (
            WebSearchProvider("openai_responses", hosted, timeout_seconds=1),
            WebSearchProvider("brave_search_api", brave, timeout_seconds=1),
        )
    )

    result = await chain.search_web(
        WebSearchRequest(query="current software release"),
        RunContext(),
    )

    assert result.evidence == [evidence]
    assert result.trace["selected_provider"] == "brave_search_api"
    assert result.trace["fallback_used"] is True
    assert result.trace["provider_chain"] == ["openai_responses", "brave_search_api"]
    assert hosted.received_query == "current software release"
    assert brave.received_query == "current software release"
