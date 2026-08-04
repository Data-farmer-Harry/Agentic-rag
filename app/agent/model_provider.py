from openai import AsyncOpenAI

from app.config import Settings


def build_model_client(
    settings: Settings,
    *,
    max_retries: int = 0,
    timeout: float | None = None,
) -> AsyncOpenAI:
    """Build the OpenAI-protocol client selected by runtime configuration."""
    if settings.model_provider == "openai":
        if settings.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required for the OpenAI provider")
        return AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            max_retries=max_retries,
            timeout=timeout,
        )

    if settings.model_base_url is None or settings.model_api_key is None:
        raise ValueError(
            "MODEL_BASE_URL and MODEL_API_KEY are required for an OpenAI-compatible provider"
        )
    return AsyncOpenAI(
        base_url=settings.model_base_url,
        api_key=settings.model_api_key.get_secret_value(),
        max_retries=max_retries,
        timeout=timeout,
    )


def build_embedding_client(
    settings: Settings,
    *,
    max_retries: int = 2,
    timeout: float | None = None,
) -> AsyncOpenAI:
    """Build an embeddings client without coupling it to the Agent runtime loop."""
    if settings.model_provider == "openai":
        if settings.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY is required for OpenAI embeddings")
        return AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.embedding_base_url,
            max_retries=max_retries,
            timeout=timeout,
        )

    base_url = settings.embedding_base_url or settings.model_base_url
    if base_url is None or settings.model_api_key is None:
        raise ValueError(
            "MODEL_BASE_URL and MODEL_API_KEY are required for compatible embeddings"
        )
    return AsyncOpenAI(
        base_url=base_url,
        api_key=settings.model_api_key.get_secret_value(),
        max_retries=max_retries,
        timeout=timeout,
    )
