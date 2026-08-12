from __future__ import annotations

import httpx
import pytest

from app.domain.models import RunContext, WebSearchRequest
from app.web_search.brave_search import BRAVE_WEB_SEARCH_ENDPOINT, BraveWebSearch
from app.web_search.openai_web_search import WebSearchPolicyError


@pytest.mark.asyncio
async def test_brave_search_normalizes_public_results_without_exposing_api_key() -> None:
    api_key = "brave-secret-token-that-must-not-reach-trace"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(BRAVE_WEB_SEARCH_ENDPOINT)
        assert request.headers["X-Subscription-Token"] == api_key
        assert request.url.params["q"] == "Brave Search API documentation"
        assert request.url.params["count"] == "3"
        assert request.url.params["country"] == "US"
        assert request.url.params["search_lang"] == "en"
        assert request.url.params["safesearch"] == "strict"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Official docs",
                            "url": "https://example.com/docs?utm_source=test",
                            "description": "Official public documentation.",
                        },
                        {
                            "title": "Local target",
                            "url": "http://127.0.0.1/private",
                            "description": "Must be rejected by URL policy.",
                        },
                    ]
                }
            },
        )

    search = BraveWebSearch(
        api_key=api_key,
        allowed_domains=["example.com"],
        country="US",
        search_lang="en",
        safesearch="strict",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await search.search_web(
        WebSearchRequest(query=" Brave  Search API   documentation ", max_results=3),
        RunContext(),
    )

    assert result.query == "Brave Search API documentation"
    assert result.sources[0].url == "https://example.com/docs"
    assert result.evidence[0].provenance.run_id is not None
    assert result.evidence[0].provenance.trust.value == "untrusted"
    assert result.trace["provider"] == "brave_search_api"
    assert result.trace["rejected_url_count"] == 1
    assert api_key not in str(result.model_dump())


@pytest.mark.asyncio
async def test_brave_search_rejects_secret_queries_before_transport() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"web": {"results": []}})

    search = BraveWebSearch(
        api_key="brave-test-key",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WebSearchPolicyError, match="secret"):
        await search.search_web(
            WebSearchRequest(query="find api_key=super-secret-value-123456789"),
            RunContext(),
        )

    assert called is False


@pytest.mark.asyncio
async def test_brave_search_rejects_non_json_shape() -> None:
    search = BraveWebSearch(
        api_key="brave-test-key",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
        ),
    )

    with pytest.raises(ValueError, match="JSON object"):
        await search.search_web(WebSearchRequest(query="safe query"), RunContext())
