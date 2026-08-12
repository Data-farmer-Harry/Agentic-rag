from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

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
from app.web_search.openai_web_search import (
    normalize_public_web_url,
    validate_web_search_query,
)

BRAVE_WEB_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveWebSearch:
    """Brave Search API adapter normalized into untrusted run-scoped evidence.

    The API key is deliberately passed only as an HTTP header. Provider response
    bodies and request headers are never included in result traces or exceptions.
    """

    revision = "brave-web-search-v1"

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 8.0,
        allowed_domains: list[str] | None = None,
        country: str | None = None,
        search_lang: str | None = None,
        safesearch: str = "moderate",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Brave Search API key must not be empty")
        self._api_key = api_key
        self._allowed_domains = allowed_domains or []
        self._country = country
        self._search_lang = search_lang
        self._safesearch = safesearch
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": "HermesGraph/1.0",
            },
        )
        self._owns_client = client is None

    async def search_web(
        self,
        request: WebSearchRequest,
        context: RunContext,
    ) -> WebSearchResult:
        query = validate_web_search_query(request.query)
        params: dict[str, str | int] = {
            "q": query,
            "count": request.max_results,
            "safesearch": self._safesearch,
        }
        if self._country:
            params["country"] = self._country
        if self._search_lang:
            params["search_lang"] = self._search_lang

        response = await self._client.get(
            BRAVE_WEB_SEARCH_ENDPOINT,
            params=params,
            headers={"X-Subscription-Token": self._api_key},
        )
        response.raise_for_status()
        payload = response.json()
        raw_results = _extract_results(payload)
        selected: OrderedDict[str, tuple[str, str]] = OrderedDict()
        rejected_url_count = 0
        for raw in raw_results:
            normalized_url = normalize_public_web_url(raw["url"], self._allowed_domains)
            if normalized_url is None:
                rejected_url_count += 1
                continue
            title = raw["title"][:500].strip() or "Web source"
            description = raw["description"][:2_000].strip()
            if description and normalized_url not in selected:
                selected[normalized_url] = (title, description)
            if len(selected) >= request.max_results:
                break

        observed_at = utc_now()
        query_fingerprint = hashlib.sha256(query.encode("utf-8")).hexdigest()
        evidence: list[EvidenceRef] = []
        sources: list[WebSearchSource] = []
        for url, (title, description) in selected.items():
            evidence.append(
                EvidenceRef(
                    evidence_id=uuid5(NAMESPACE_URL, f"{self.revision}:{query_fingerprint}:{url}"),
                    text=description,
                    title=title,
                    score=0.85,
                    provenance=Provenance(
                        source_type="web_search",
                        source_id=url,
                        run_id=context.run_id,
                        content_hash=hashlib.sha256(description.encode("utf-8")).hexdigest(),
                        locator={"uri": url},
                        trust=TrustLevel.UNTRUSTED,
                        observed_at=observed_at,
                    ),
                    metadata={
                        "uri": url,
                        "web_search": {
                            "query_fingerprint": query_fingerprint,
                            "provider_revision": self.revision,
                            "representation": "provider_result_description",
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
                "provider": "brave_search_api",
                "provider_revision": self.revision,
                "returned_source_count": len(sources),
                "rejected_url_count": rejected_url_count,
                "stop_reason": "search_results" if sources else "no_results",
                "country": self._country,
                "search_lang": self._search_lang,
                "safesearch": self._safesearch,
            },
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _extract_results(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("Brave Search response must be a JSON object")
    web = payload.get("web")
    if not isinstance(web, dict):
        return []
    results = web.get("results")
    if not isinstance(results, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            continue
        title = item.get("title")
        description = item.get("description")
        normalized.append(
            {
                "url": url,
                "title": title if isinstance(title, str) else "",
                "description": description if isinstance(description, str) else "",
            }
        )
    return normalized
