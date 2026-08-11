from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

from app.domain.enums import GraphCandidateStatus
from app.domain.models import (
    EntityResolutionCandidate,
    GraphEntityCandidate,
    GraphExtractionBatch,
)

_SEPARATOR_PATTERN = re.compile(r"[^\w]+", re.UNICODE)
_IDENTIFIER_TYPES = {"Identifier", "SoftwareSymbol"}


class DeterministicEntityResolver:
    """Proposes conservative cross-document identity links without promoting them."""

    revision = "deterministic-entity-resolution-v2"

    def __init__(
        self,
        *,
        max_candidates: int = 1_000,
        max_matches_per_entity: int = 1,
    ) -> None:
        if not 1 <= max_candidates <= 10_000:
            raise ValueError("max_candidates must be between 1 and 10000")
        if not 1 <= max_matches_per_entity <= 10:
            raise ValueError("max_matches_per_entity must be between 1 and 10")
        self._max_candidates = max_candidates
        self._max_matches_per_entity = max_matches_per_entity

    async def propose(
        self,
        batch: GraphExtractionBatch,
        existing_entities: Sequence[GraphEntityCandidate],
    ) -> Sequence[EntityResolutionCandidate]:
        batch_entities = [
            item
            for item in batch.entities
            if item.status not in {
                GraphCandidateStatus.REJECTED,
                GraphCandidateStatus.ARCHIVED,
            }
        ]
        existing = [
            item
            for item in existing_entities
            if item.tenant_id == batch.tenant_id
            and item.project_id == batch.project_id
            and item.document_id != batch.document_id
            and item.status
            not in {GraphCandidateStatus.REJECTED, GraphCandidateStatus.ARCHIVED}
        ]
        proposals: dict[str, EntityResolutionCandidate] = {}
        for current in batch_entities:
            matches: list[
                tuple[tuple[int, int, float, str], EntityResolutionCandidate]
            ] = []
            for other in existing:
                match = _match_entities(current, other)
                if match is None:
                    continue
                strategy, confidence, shared_form = match
                left, right = sorted(
                    (current, other), key=lambda item: str(item.candidate_id)
                )
                candidate_id = uuid5(
                    NAMESPACE_URL,
                    f"hermesgraph:entity-resolution:{batch.tenant_id}:"
                    f"{batch.project_id}:{left.candidate_id}:{right.candidate_id}:"
                    f"{self.revision}",
                )
                source_chunk_ids = sorted(
                    {*left.source_chunk_ids, *right.source_chunk_ids}, key=str
                )[:100]
                if len(source_chunk_ids) < 2:
                    continue
                proposal = EntityResolutionCandidate(
                    candidate_id=candidate_id,
                    tenant_id=batch.tenant_id,
                    project_id=batch.project_id,
                    left_entity_id=left.candidate_id,
                    right_entity_id=right.candidate_id,
                    left_document_id=left.document_id,
                    right_document_id=right.document_id,
                    left_name=left.canonical_name,
                    right_name=right.canonical_name,
                    canonical_name=_canonical_name(left, right),
                    entity_type=left.entity_type,
                    match_strategy=strategy,
                    source_chunk_ids=source_chunk_ids,
                    confidence=confidence,
                    resolver_revision=self.revision,
                    rationale=(
                        f"Cross-document {strategy.replace('_', ' ')} match on "
                        f"'{shared_form}'; requires review before SAME_AS activation."
                    ),
                )
                rank = (
                    0 if other.status == GraphCandidateStatus.APPROVED else 1,
                    _MATCH_STRATEGY_PRIORITY[strategy],
                    -confidence,
                    str(other.candidate_id),
                )
                matches.append((rank, proposal))
            for _, proposal in sorted(matches, key=lambda item: item[0])[
                : self._max_matches_per_entity
            ]:
                proposals[str(proposal.candidate_id)] = proposal
                if len(proposals) >= self._max_candidates:
                    return sorted(
                        proposals.values(), key=lambda item: str(item.candidate_id)
                    )
        return sorted(proposals.values(), key=lambda item: str(item.candidate_id))


def _match_entities(
    left: GraphEntityCandidate,
    right: GraphEntityCandidate,
) -> tuple[str, float, str] | None:
    if left.entity_type != right.entity_type:
        return None
    left_exact = _exact_form(left.canonical_name)
    right_exact = _exact_form(right.canonical_name)
    left_normalized = _normalized_form(left.canonical_name)
    right_normalized = _normalized_form(right.canonical_name)
    if not left_normalized or not right_normalized:
        return None
    if left_exact == right_exact:
        strategy = (
            "exact_identifier"
            if left.entity_type in _IDENTIFIER_TYPES
            else "exact_name"
        )
        return strategy, 0.99 if strategy == "exact_identifier" else 0.98, left_exact
    if left_normalized == right_normalized and len(left_normalized) >= 3:
        return "normalized_name", 0.95, left_normalized

    left_aliases = {_exact_form(item) for item in left.aliases}
    right_aliases = {_exact_form(item) for item in right.aliases}
    overlap = (
        ({left_exact} | left_aliases) & ({right_exact} | right_aliases)
    ) - {""}
    if overlap:
        shared = min(overlap, key=lambda item: (len(item), item))
        if len(_normalized_form(shared)) >= 3:
            return "alias_overlap", 0.92, shared
    return None


_MATCH_STRATEGY_PRIORITY = {
    "exact_identifier": 0,
    "exact_name": 1,
    "normalized_name": 2,
    "alias_overlap": 3,
}


def _exact_form(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _normalized_form(value: str) -> str:
    return _SEPARATOR_PATTERN.sub("", _exact_form(value))


def _canonical_name(
    left: GraphEntityCandidate,
    right: GraphEntityCandidate,
) -> str:
    approved = [
        item
        for item in (left, right)
        if item.status == GraphCandidateStatus.APPROVED
    ]
    choices = approved or [left, right]
    return min(
        (item.canonical_name for item in choices),
        key=lambda value: (len(value), value.casefold(), value),
    )
