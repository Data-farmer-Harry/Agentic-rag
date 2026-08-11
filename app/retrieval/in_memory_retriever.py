from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from app.domain.enums import TrustLevel
from app.domain.models import EvidenceRef, Provenance, RunContext

_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)


class InMemoryRetriever:
    """Small deterministic lexical retriever for offline use and tests."""

    def __init__(
        self,
        evidence: Sequence[EvidenceRef],
        *,
        min_score: float = 0.0,
    ) -> None:
        if not 0.0 <= min_score <= 2.0:
            raise ValueError("min_score must be between 0 and 2")
        self._evidence = tuple(evidence)
        self._min_score = min_score

    @classmethod
    def from_texts(
        cls,
        texts: Sequence[str],
        *,
        source_type: str = "memory",
        metadatas: Sequence[Mapping[str, Any]] | None = None,
        trust: TrustLevel = TrustLevel.UNTRUSTED,
        min_score: float = 0.0,
    ) -> InMemoryRetriever:
        if metadatas is not None and len(metadatas) != len(texts):
            raise ValueError("metadatas must have the same length as texts")
        evidence: list[EvidenceRef] = []
        for index, text in enumerate(texts):
            metadata = dict(metadatas[index]) if metadatas is not None else {}
            source_id = str(metadata.get("source_id") or sha256(text.encode()).hexdigest()[:16])
            title = metadata.pop("title", None)
            evidence.append(
                EvidenceRef(
                    text=text,
                    title=str(title) if title is not None else None,
                    provenance=Provenance(
                        source_type=source_type,
                        source_id=source_id,
                        content_hash=sha256(text.encode()).hexdigest(),
                        trust=trust,
                    ),
                    metadata=metadata,
                )
            )
        return cls(evidence, min_score=min_score)

    async def retrieve(
        self,
        query: str,
        context: RunContext | None = None,
        *,
        filters: Mapping[str, Any] | None = None,
        top_k: int = 10,
    ) -> Sequence[EvidenceRef]:
        del context
        if top_k < 1:
            raise ValueError("top_k must be positive")
        query_tokens = set(_tokens(query))
        normalized_query = " ".join(query.casefold().split())
        ranked: list[tuple[float, int, EvidenceRef]] = []
        for index, item in enumerate(self._evidence):
            if filters and not _matches_filters(item.metadata, filters):
                continue
            document_tokens = set(_tokens(item.text))
            overlap = len(query_tokens & document_tokens)
            if query_tokens and overlap == 0 and normalized_query not in item.text.casefold():
                continue
            score = overlap / max(len(query_tokens), 1)
            if normalized_query and normalized_query in item.text.casefold():
                score += 1.0
            if score < self._min_score:
                continue
            ranked.append((score, index, item.model_copy(update={"score": score})))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        return tuple(row[2] for row in ranked[:top_k])


def _tokens(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_PATTERN.findall(text)]


def _matches_filters(metadata: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    return all(metadata.get(key) == value for key, value in filters.items())
