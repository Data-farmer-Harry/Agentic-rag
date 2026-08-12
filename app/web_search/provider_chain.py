from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from time import perf_counter

from app.domain.contracts import WebSearchPort
from app.domain.models import RunContext, WebSearchRequest, WebSearchResult
from app.web_search.openai_web_search import validate_web_search_query


@dataclass(frozen=True, slots=True)
class WebSearchProvider:
    """A named, bounded provider participating in a fallback chain."""

    name: str
    provider: WebSearchPort
    timeout_seconds: float


class WebSearchProviderChain:
    """Try independent providers in order without exposing provider internals.

    Only provider names, error classes, and bounded latency are preserved in the
    trace. This keeps transient HTTP response text and credentials out of run
    events while still making fallback decisions auditable.
    """

    revision = "web-search-provider-chain-v1"

    def __init__(self, providers: Sequence[WebSearchProvider]) -> None:
        if not providers:
            raise ValueError("web search provider chain requires at least one provider")
        if any(not item.name.strip() or item.timeout_seconds <= 0 for item in providers):
            raise ValueError("web search providers require a name and positive timeout")
        self._providers = tuple(providers)

    @property
    def provider_names(self) -> tuple[str, ...]:
        """Configured order, intended for diagnostics and bootstrap contract tests."""
        return tuple(item.name for item in self._providers)

    async def search_web(
        self,
        request: WebSearchRequest,
        context: RunContext,
    ) -> WebSearchResult:
        # Validate once before crossing any provider boundary. Individual adapters
        # validate again so they remain safe when called outside this chain.
        normalized_request = request.model_copy(
            update={"query": validate_web_search_query(request.query)}
        )
        attempts: list[dict[str, str | float]] = []
        for index, candidate in enumerate(self._providers):
            started = perf_counter()
            try:
                result = await asyncio.wait_for(
                    candidate.provider.search_web(normalized_request, context),
                    timeout=candidate.timeout_seconds,
                )
            except Exception as exc:
                attempts.append(
                    {
                        "provider": candidate.name,
                        "outcome": "error",
                        "error_type": type(exc).__name__,
                        "duration_ms": round((perf_counter() - started) * 1_000, 2),
                    }
                )
                continue

            reported_provider = str(result.trace.get("provider") or candidate.name)
            if not result.evidence:
                attempts.append(
                    {
                        "provider": (
                            reported_provider if reported_provider != "none" else candidate.name
                        ),
                        "outcome": "empty",
                        "duration_ms": round((perf_counter() - started) * 1_000, 2),
                    }
                )
                continue

            attempts.append(
                {
                    "provider": reported_provider,
                    "outcome": "success",
                    "duration_ms": round((perf_counter() - started) * 1_000, 2),
                }
            )
            return result.model_copy(
                update={
                    "trace": result.trace
                    | {
                        "provider_chain": [item["provider"] for item in attempts],
                        "provider_attempts": attempts,
                        "selected_provider": reported_provider,
                        "fallback_used": index > 0,
                        "provider_chain_revision": self.revision,
                    }
                }
            )

        return WebSearchResult(
            query=normalized_request.query,
            trace={
                "provider": "none",
                "provider_revision": self.revision,
                "provider_chain": [item["provider"] for item in attempts],
                "provider_attempts": attempts,
                "selected_provider": None,
                "fallback_used": len(attempts) > 1,
                "stop_reason": "all_providers_exhausted",
            },
        )

    async def close(self) -> None:
        seen: set[int] = set()
        for candidate in reversed(self._providers):
            provider = candidate.provider
            if id(provider) in seen:
                continue
            seen.add(id(provider))
            close = getattr(provider, "close", None)
            if close is not None:
                await close()
