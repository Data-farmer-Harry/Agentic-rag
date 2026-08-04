from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from openai import AsyncOpenAI

_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_\-]+|[\u3400-\u9fff]", re.UNICODE)


@dataclass(frozen=True, slots=True)
class SparseEmbedding:
    indices: list[int]
    values: list[float]


@dataclass(frozen=True, slots=True)
class EmbeddingUsage:
    request_count: int = 0
    input_count: int = 0
    input_tokens: int = 0
    usage_reported_request_count: int = 0

    def delta(self, previous: EmbeddingUsage) -> EmbeddingUsage:
        values = (
            self.request_count - previous.request_count,
            self.input_count - previous.input_count,
            self.input_tokens - previous.input_tokens,
            self.usage_reported_request_count - previous.usage_reported_request_count,
        )
        if any(value < 0 for value in values):
            raise ValueError("Embedding usage snapshots must be monotonic")
        return EmbeddingUsage(*values)


class DenseEmbeddingPort(Protocol):
    dimension: int
    revision: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class SparseEmbeddingPort(Protocol):
    revision: str

    async def embed(self, texts: Sequence[str]) -> list[SparseEmbedding]: ...


def _tokens(text: str) -> list[str]:
    return _TOKEN_PATTERN.findall(text.casefold())


def _stable_hash(token: str, *, digest_size: int = 8) -> int:
    return int.from_bytes(
        hashlib.blake2b(token.encode("utf-8"), digest_size=digest_size).digest(),
        "big",
    )


class DeterministicDenseEmbedder:
    """Dependency-free test/offline encoder; not a semantic production model."""

    def __init__(self, dimension: int = 256) -> None:
        if not 32 <= dimension <= 4_096:
            raise ValueError("dimension must be between 32 and 4096")
        self.dimension = dimension
        self.revision = f"deterministic-hash-dense-v1:{dimension}"

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token, count in Counter(_tokens(text)).items():
                digest = _stable_hash(token)
                index = digest % self.dimension
                sign = 1.0 if (digest >> 63) == 0 else -1.0
                vector[index] += sign * (1.0 + math.log(count))
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
            vectors.append(vector)
        return vectors


class HashedSparseEmbedder:
    """Stable lexical sparse encoder suitable as a deterministic BM25/SPLADE boundary."""

    revision = "hashed-sparse-v1"

    async def embed(self, texts: Sequence[str]) -> list[SparseEmbedding]:
        encoded: list[SparseEmbedding] = []
        for text in texts:
            weights: dict[int, float] = {}
            for token, count in Counter(_tokens(text)).items():
                index = _stable_hash(token, digest_size=4)
                weights[index] = weights.get(index, 0.0) + 1.0 + math.log(count)
            ordered = sorted(weights.items())
            encoded.append(
                SparseEmbedding(
                    indices=[index for index, _ in ordered],
                    values=[value for _, value in ordered],
                )
            )
        return encoded


class OpenAIDenseEmbedder:
    """OpenAI embeddings adapter kept outside the Agent SDK orchestration loop."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str = "text-embedding-3-small",
        dimension: int = 1_024,
        max_batch_size: int = 128,
    ) -> None:
        if not 32 <= dimension <= 3_072:
            raise ValueError("dimension must be between 32 and 3072")
        if not 1 <= max_batch_size <= 2_048:
            raise ValueError("max_batch_size must be between 1 and 2048")
        self._client = client
        self._model = model
        self._max_batch_size = max_batch_size
        self.dimension = dimension
        self.revision = f"openai:{model}:{dimension}"
        self._usage = EmbeddingUsage()
        self._usage_lock = asyncio.Lock()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not text.strip() for text in texts):
            raise ValueError("Embedding inputs must not be empty")
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._max_batch_size):
            batch = texts[start : start + self._max_batch_size]
            vectors.extend(await self._embed_batch(batch))
        return vectors

    async def usage_snapshot(self) -> EmbeddingUsage:
        async with self._usage_lock:
            return self._usage

    async def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(
            model=self._model,
            input=list(texts),
            dimensions=self.dimension,
            encoding_format="float",
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        if [item.index for item in ordered] != list(range(len(texts))):
            raise RuntimeError("Embedding provider returned invalid batch indexes")
        vectors = [list(item.embedding) for item in ordered]
        if len(vectors) != len(texts) or any(
            len(vector) != self.dimension for vector in vectors
        ):
            raise RuntimeError("Embedding provider returned an invalid vector batch")
        usage = getattr(response, "usage", None)
        input_tokens = _usage_value(usage, "prompt_tokens")
        if input_tokens is None:
            input_tokens = _usage_value(usage, "total_tokens")
        observed = EmbeddingUsage(
            request_count=1,
            input_count=len(texts),
            input_tokens=input_tokens or 0,
            usage_reported_request_count=int(input_tokens is not None),
        )
        async with self._usage_lock:
            current = self._usage
            self._usage = EmbeddingUsage(
                request_count=current.request_count + observed.request_count,
                input_count=current.input_count + observed.input_count,
                input_tokens=current.input_tokens + observed.input_tokens,
                usage_reported_request_count=(
                    current.usage_reported_request_count
                    + observed.usage_reported_request_count
                ),
            )
        return vectors


def _usage_value(usage: Any, field: str) -> int | None:
    if usage is None:
        return None
    value = usage.get(field) if isinstance(usage, Mapping) else getattr(usage, field, None)
    return value if isinstance(value, int) and value >= 0 else None
