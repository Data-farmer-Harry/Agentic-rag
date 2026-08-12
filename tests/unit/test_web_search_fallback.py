from __future__ import annotations

import httpx
import pytest

from app.domain.models import RunContext, WebSearchRequest
from app.web_search.fallback_web_search import DuckDuckGoWebSearch, FallbackWebSearch


class _FailingSearch:
    async def search_web(self, request: WebSearchRequest, context: RunContext):
        del request, context
        raise RuntimeError("hosted tool unsupported")


@pytest.mark.asyncio
async def test_fallback_search_returns_url_cited_results_after_primary_failure() -> None:
    html = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs">
        Example documentation
      </a>
      <a class="result__snippet">A concise result snippet from the public page.</a>
    </div>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "html.duckduckgo.com"
        return httpx.Response(200, text=html)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fallback = DuckDuckGoWebSearch(client=client)
    search = FallbackWebSearch(_FailingSearch(), fallback, primary_timeout_seconds=0.1)
    context = RunContext()

    result = await search.search_web(WebSearchRequest(query="example docs"), context)

    assert result.sources[0].url == "https://example.com/docs"
    assert result.evidence[0].provenance.run_id == context.run_id
    assert result.trace["fallback_used"] is True
    assert result.trace["primary_failure_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_public_fallback_uses_bing_when_duckduckgo_is_unavailable() -> None:
    bing_html = """
    <ol id="b_results"><li class="b_algo"><h2>
      <a href="https://example.org/guide">Example guide</a>
    </h2><div class="b_caption"><p>Public documentation from Bing.</p></div></li></ol>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "html.duckduckgo.com":
            return httpx.Response(503)
        return httpx.Response(200, text=bing_html)

    search = DuckDuckGoWebSearch(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    result = await search.search_web(WebSearchRequest(query="example guide"), RunContext())

    assert result.sources[0].url == "https://example.org/guide"
    assert result.trace["provider"] == "bing_html"
    assert result.trace["provider_failures"] == ["duckduckgo_html:HTTPStatusError"]
