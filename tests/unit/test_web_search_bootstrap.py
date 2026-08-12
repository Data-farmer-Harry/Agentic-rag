from __future__ import annotations

from typing import Any

import pytest

from app import bootstrap
from app.config import Settings
from app.web_search.provider_chain import WebSearchProviderChain


class _FakeSearch:
    async def search_web(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise AssertionError("bootstrap factory test must not execute a provider")


def _settings(**updates: Any) -> Settings:
    defaults: dict[str, Any] = {
        "web_search_mode": "openai",
        "model_provider": "compatible",
        "model_base_url": "https://provider.example/v1",
        "model_api_key": "model-test-key",
        "web_search_model": "search-test-model",
    }
    return Settings(**(defaults | updates))


def test_bootstrap_keeps_existing_hosted_to_public_html_chain_without_brave_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(
        bootstrap,
        "OpenAIHostedWebSearch",
        lambda _: _FakeSearch(),
    )

    def public_factory(**kwargs: Any) -> _FakeSearch:
        created.append(("public", kwargs))
        return _FakeSearch()

    def brave_factory(**kwargs: Any) -> _FakeSearch:
        created.append(("brave", kwargs))
        return _FakeSearch()

    monkeypatch.setattr(bootstrap, "DuckDuckGoWebSearch", public_factory)
    monkeypatch.setattr(bootstrap, "BraveWebSearch", brave_factory)

    search = bootstrap._build_web_search(_settings(brave_search_api_key=""))

    assert isinstance(search, WebSearchProviderChain)
    assert search.provider_names == ("openai_responses", "public_html_search")
    assert [name for name, _ in created] == ["public"]


def test_bootstrap_inserts_brave_between_hosted_and_public_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(bootstrap, "OpenAIHostedWebSearch", lambda _: _FakeSearch())

    def factory(name: str):
        def build(**kwargs: Any) -> _FakeSearch:
            created.append((name, kwargs))
            return _FakeSearch()

        return build

    monkeypatch.setattr(bootstrap, "BraveWebSearch", factory("brave"))
    monkeypatch.setattr(bootstrap, "DuckDuckGoWebSearch", factory("public"))

    search = bootstrap._build_web_search(
        _settings(
            brave_search_api_key="brave-test-key",
            brave_search_country="us",
            brave_search_language="en",
            brave_search_safesearch="strict",
        )
    )

    assert isinstance(search, WebSearchProviderChain)
    assert search.provider_names == (
        "openai_responses",
        "brave_search_api",
        "public_html_search",
    )
    brave_kwargs = dict(created)["brave"]
    assert brave_kwargs["api_key"] == "brave-test-key"
    assert brave_kwargs["country"] == "US"
    assert brave_kwargs["search_lang"] == "en"
    assert brave_kwargs["safesearch"] == "strict"
