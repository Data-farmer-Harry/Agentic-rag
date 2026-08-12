from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx

from app.domain.contracts import WebSearchPort
from app.domain.enums import TrustLevel
from app.domain.models import (
    EvidenceRef,
    Provenance,
    RunContext,
    WebSearchRequest,
    WebSearchResult,
    WebSearchSource,
    utc_now,
)
from app.web_search.openai_web_search import normalize_public_web_url, validate_web_search_query


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._active: dict[str, str] | None = None
        self._capture: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "a" and "result__a" in classes:
            self._active = {"url": attributes.get("href") or "", "title": "", "snippet": ""}
            self._capture = "title"
            self._parts = []
        elif self._active is not None and "result__snippet" in classes:
            self._capture = "snippet"
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._active is None or self._capture is None:
            return
        if (self._capture == "title" and tag == "a") or (
            self._capture == "snippet" and tag in {"a", "div"}
        ):
            self._active[self._capture] = " ".join("".join(self._parts).split())
            if self._capture == "snippet":
                self.results.append(self._active)
                self._active = None
            self._capture = None
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._parts.append(data)


class _BingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._active: dict[str, str] | None = None
        self._in_heading = False
        self._capture: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "li" and "b_algo" in classes:
            self._active = {"url": "", "title": "", "snippet": ""}
        elif self._active is not None and tag == "h2":
            self._in_heading = True
        elif self._active is not None and self._in_heading and tag == "a":
            self._active["url"] = attributes.get("href") or ""
            self._capture = "title"
            self._parts = []
        elif self._active is not None and tag == "p":
            self._capture = "snippet"
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._active is None:
            return
        if self._capture == "title" and tag == "a":
            self._active["title"] = " ".join("".join(self._parts).split())
            self._capture = None
        elif self._capture == "snippet" and tag == "p":
            self._active["snippet"] = " ".join("".join(self._parts).split())
            self._capture = None
        if tag == "h2":
            self._in_heading = False
        elif tag == "li":
            if all(self._active.values()):
                self.results.append(self._active)
            self._active = None
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._parts.append(data)


class DuckDuckGoWebSearch:
    """No-key public search fallback with DuckDuckGo and Bing transports."""

    revision = "duckduckgo-html-search-v1"

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        allowed_domains: list[str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 HermesGraph/1.0"},
        )
        self._owns_client = client is None
        self._allowed_domains = allowed_domains or []

    async def search_web(self, request: WebSearchRequest, context: RunContext) -> WebSearchResult:
        query = validate_web_search_query(request.query)
        raw_results: list[dict[str, str]] = []
        provider = "none"
        provider_failures: list[str] = []
        providers = (
            ("duckduckgo_html", "https://html.duckduckgo.com/html/", _DuckDuckGoParser),
            ("bing_html", "https://www.bing.com/search", _BingParser),
        )
        for candidate, endpoint, parser_type in providers:
            try:
                response = await self._client.get(endpoint, params={"q": query})
                response.raise_for_status()
                parser = parser_type()
                parser.feed(response.text)
                raw_results = parser.results
            except (httpx.HTTPError, ValueError) as exc:
                provider_failures.append(f"{candidate}:{type(exc).__name__}")
                continue
            if raw_results:
                provider = candidate
                break
            provider_failures.append(f"{candidate}:EmptySearchResult")
        selected: OrderedDict[str, tuple[str, str]] = OrderedDict()
        rejected = 0
        for raw in raw_results:
            normalized = normalize_public_web_url(
                _decode_result_url(raw["url"]), self._allowed_domains
            )
            if normalized is None:
                rejected += 1
                continue
            title = raw["title"][:500].strip() or (urlsplit(normalized).hostname or "Web source")
            snippet = raw["snippet"][:2_000].strip()
            if snippet and normalized not in selected:
                selected[normalized] = (title, snippet)
            if len(selected) >= request.max_results:
                break

        observed_at = utc_now()
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        evidence: list[EvidenceRef] = []
        sources: list[WebSearchSource] = []
        for url, (title, snippet) in selected.items():
            evidence.append(
                EvidenceRef(
                    evidence_id=uuid5(NAMESPACE_URL, f"{self.revision}:{query_hash}:{url}"),
                    text=snippet,
                    title=title,
                    score=0.8,
                    provenance=Provenance(
                        source_type="web_search",
                        source_id=url,
                        run_id=context.run_id,
                        content_hash=hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
                        locator={"uri": url},
                        trust=TrustLevel.UNTRUSTED,
                        observed_at=observed_at,
                    ),
                    metadata={
                        "uri": url,
                        "web_search": {
                            "query_fingerprint": query_hash,
                            "provider_revision": self.revision,
                            "representation": "search_result_snippet",
                        },
                    },
                )
            )
            sources.append(WebSearchSource(url=url, title=title))
        return WebSearchResult(
            query=query,
            summary="\n\n".join(item.text for item in evidence)[:20_000],
            evidence=evidence,
            sources=sources,
            trace={
                "provider": provider,
                "provider_revision": self.revision,
                "provider_failures": provider_failures,
                "returned_source_count": len(sources),
                "rejected_url_count": rejected,
                "stop_reason": "search_results" if sources else "no_results",
            },
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class FallbackWebSearch:
    """Prefer hosted search, then quickly degrade to an independent provider."""

    def __init__(
        self,
        primary: WebSearchPort,
        fallback: WebSearchPort,
        *,
        primary_timeout_seconds: float,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_timeout_seconds = primary_timeout_seconds

    async def search_web(self, request: WebSearchRequest, context: RunContext) -> WebSearchResult:
        try:
            primary = await asyncio.wait_for(
                self._primary.search_web(request, context),
                timeout=self._primary_timeout_seconds,
            )
            if primary.evidence:
                return primary
            failure_type = "EmptySearchResult"
        except Exception as exc:
            failure_type = type(exc).__name__
        fallback = await self._fallback.search_web(request, context)
        return fallback.model_copy(
            update={
                "trace": fallback.trace
                | {
                    "fallback_used": True,
                    "primary_failure_type": failure_type,
                    "primary_timeout_seconds": self._primary_timeout_seconds,
                }
            }
        )

    async def close(self) -> None:
        for provider in (self._fallback, self._primary):
            close = getattr(provider, "close", None)
            if close is not None:
                await close()


def _decode_result_url(value: str) -> str:
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlsplit(value)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg", [])
        if target:
            return unquote(target[0])
    return value
