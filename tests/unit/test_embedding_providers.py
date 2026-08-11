import math
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai import AsyncOpenAI

from app.retrieval.embedding_providers import (
    BM25SparseEmbedder,
    DeterministicDenseEmbedder,
    HashedSparseEmbedder,
    OpenAIDenseEmbedder,
    build_sparse_embedder,
)


@pytest.mark.asyncio
async def test_deterministic_embeddings_are_stable_and_normalized() -> None:
    dense = DeterministicDenseEmbedder(64)
    sparse = HashedSparseEmbedder()

    first, second = await dense.embed(["alpha beta beta", "alpha beta beta"])
    sparse_first, sparse_second = await sparse.embed_documents(
        ["alpha beta beta", "alpha beta beta"]
    )

    assert first == second
    assert math.isclose(sum(value * value for value in first), 1.0)
    assert sparse_first == sparse_second
    assert sparse_first.indices == sorted(sparse_first.indices)
    assert len(sparse_first.indices) == 2


@pytest.mark.asyncio
async def test_bm25_sparse_encoder_normalizes_length_and_saturates_term_frequency() -> None:
    sparse = BM25SparseEmbedder(k1=1.2, b=0.75, average_document_tokens=4)

    once, twice, long = await sparse.embed_documents(
        [
            "alpha beta",
            "alpha alpha beta",
            "alpha beta gamma delta epsilon zeta",
        ]
    )
    once_weights = dict(zip(once.indices, once.values, strict=True))
    twice_weights = dict(zip(twice.indices, twice.values, strict=True))
    long_weights = dict(zip(long.indices, long.values, strict=True))
    alpha_index = max(
        once_weights,
        key=lambda index: twice_weights[index] - once_weights[index],
    )

    assert once_weights[alpha_index] < twice_weights[alpha_index]
    assert twice_weights[alpha_index] < 2 * once_weights[alpha_index]
    assert long_weights[alpha_index] < once_weights[alpha_index]


@pytest.mark.asyncio
async def test_bm25_query_weights_are_binary_and_require_qdrant_idf() -> None:
    sparse = BM25SparseEmbedder()

    one, repeated = await sparse.embed_queries(["alpha beta", "alpha alpha beta"])

    assert one == repeated
    assert one.values == [1.0, 1.0]
    assert sparse.requires_idf is True
    assert build_sparse_embedder("bm25").revision.startswith(
        "bm25-hashed-cjk-bigram-v2:"
    )


@pytest.mark.asyncio
async def test_bm25_encoder_adds_cjk_bigrams_for_phrase_precision() -> None:
    sparse = BM25SparseEmbedder()

    vector = (await sparse.embed_queries(["检索延迟事故"]))[0]

    # Six characters plus five adjacent bigrams.
    assert len(vector.indices) == 11


class _FakeEmbeddings:
    async def create(self, **kwargs: Any) -> Any:
        assert kwargs["model"] == "text-embedding-3-small"
        assert kwargs["dimensions"] == 32
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=1, embedding=[0.0, 1.0, *([0.0] * 30)]),
                SimpleNamespace(index=0, embedding=[1.0, *([0.0] * 31)]),
            ],
            usage=SimpleNamespace(prompt_tokens=12, total_tokens=12),
        )


@pytest.mark.asyncio
async def test_openai_embedder_restores_provider_batch_order() -> None:
    fake_client = SimpleNamespace(embeddings=_FakeEmbeddings())
    embedder = OpenAIDenseEmbedder(
        cast(AsyncOpenAI, fake_client),
        dimension=32,
    )

    vectors = await embedder.embed(["first", "second"])

    assert vectors[0][:2] == [1.0, 0.0]
    assert vectors[1][:2] == [0.0, 1.0]
    usage = await embedder.usage_snapshot()
    assert usage.request_count == 1
    assert usage.input_count == 2
    assert usage.input_tokens == 12
    assert usage.usage_reported_request_count == 1


class _BatchingEmbeddings:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    async def create(self, **kwargs: Any) -> Any:
        inputs = kwargs["input"]
        self.batch_sizes.append(len(inputs))
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=[float(index), *([0.0] * 31)])
                for index in range(len(inputs))
            ],
            usage={"prompt_tokens": len(inputs) * 5},
        )


@pytest.mark.asyncio
async def test_openai_embedder_batches_requests_and_accumulates_usage() -> None:
    embeddings = _BatchingEmbeddings()
    fake_client = SimpleNamespace(embeddings=embeddings)
    embedder = OpenAIDenseEmbedder(
        cast(AsyncOpenAI, fake_client),
        dimension=32,
        max_batch_size=2,
    )

    vectors = await embedder.embed(["one", "two", "three", "four", "five"])

    assert len(vectors) == 5
    assert embeddings.batch_sizes == [2, 2, 1]
    usage = await embedder.usage_snapshot()
    assert usage.request_count == 3
    assert usage.input_count == 5
    assert usage.input_tokens == 25
    assert usage.usage_reported_request_count == 3
