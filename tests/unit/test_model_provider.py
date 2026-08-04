import pytest

from app.agent.model_provider import build_model_client
from app.config import Settings


def test_openai_provider_builds_official_sdk_client() -> None:
    client = build_model_client(Settings(openai_api_key="test-key"), max_retries=1)

    assert str(client.base_url) == "https://api.openai.com/v1/"
    assert client.max_retries == 1


def test_model_client_requires_selected_provider_credentials() -> None:
    with pytest.raises(ValueError):
        build_model_client(Settings())
    with pytest.raises(ValueError):
        build_model_client(Settings(model_provider="compatible"))


def test_compatible_provider_client_uses_explicit_base_url() -> None:
    client = build_model_client(
        Settings(
            model_provider="compatible",
            model_base_url="https://example.invalid/v1",
            model_api_key="test-key",
        ),
        max_retries=2,
    )

    assert str(client.base_url) == "https://example.invalid/v1/"
    assert client.max_retries == 2
