from __future__ import annotations

import asyncio
import fcntl
import json
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from app.domain.enums import GraphCandidateStatus
from app.domain.models import (
    EntityResolutionCandidate,
    GraphCandidateReviewEvent,
    GraphEntityCandidate,
    GraphExtractionBatch,
    GraphRelationCandidate,
    utc_now,
)


class GraphCandidateStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CandidateEvidenceReconciliationCounts:
    entities_archived: int = 0
    relations_archived: int = 0
    resolutions_archived: int = 0


class JsonGraphCandidateRepository:
    """Atomic, scoped audit store for extracted graph candidates and reviews."""

    _FORMAT_VERSION = 2
    _READABLE_VERSIONS = {1, 2}

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def save_batch(self, batch: GraphExtractionBatch) -> GraphExtractionBatch:
        async with self._transaction_lock():
            entities, relations, resolutions, reviews = self._read_all()
            entities, relations, resolutions = _supersede_pending_document_candidates(
                batch,
                entities,
                relations,
                resolutions,
            )
            merged_entities: list[GraphEntityCandidate] = []
            for entity_candidate in batch.entities:
                self._require_batch_scope(batch, entity_candidate)
                existing_entity = entities.get(str(entity_candidate.candidate_id))
                merged_entity = self._merge_entity(
                    entity_candidate,
                    existing_entity,
                )
                entities[str(merged_entity.candidate_id)] = merged_entity
                merged_entities.append(merged_entity)

            available_entity_ids = set(entities)
            merged_relations: list[GraphRelationCandidate] = []
            for relation_candidate in batch.relations:
                self._require_batch_scope(batch, relation_candidate)
                if (
                    str(relation_candidate.source_candidate_id) not in available_entity_ids
                    or str(relation_candidate.target_candidate_id) not in available_entity_ids
                ):
                    raise GraphCandidateStoreError(
                        "Relation candidate references an unknown entity candidate"
                    )
                existing_relation = relations.get(str(relation_candidate.candidate_id))
                merged_relation = self._merge_relation(
                    relation_candidate,
                    existing_relation,
                )
                relations[str(merged_relation.candidate_id)] = merged_relation
                merged_relations.append(merged_relation)
            self._write_all(entities, relations, resolutions, reviews)
        return batch.model_copy(update={"entities": merged_entities, "relations": merged_relations})

    async def list_entities(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        document_id: UUID | None = None,
        status: str | None = None,
    ) -> Sequence[GraphEntityCandidate]:
        requested_status = _status_value(status)
        async with self._transaction_lock():
            entities = list(self._read_all()[0].values())
        scoped = [
            item
            for item in entities
            if item.tenant_id == tenant_id
            and item.project_id == project_id
            and (document_id is None or item.document_id == document_id)
            and (requested_status is None or item.status == requested_status)
        ]
        return sorted(
            scoped,
            key=lambda item: (item.updated_at, str(item.candidate_id)),
            reverse=True,
        )

    async def list_relations(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        document_id: UUID | None = None,
        status: str | None = None,
    ) -> Sequence[GraphRelationCandidate]:
        requested_status = _status_value(status)
        async with self._transaction_lock():
            relations = list(self._read_all()[1].values())
        scoped = [
            item
            for item in relations
            if item.tenant_id == tenant_id
            and item.project_id == project_id
            and (document_id is None or item.document_id == document_id)
            and (requested_status is None or item.status == requested_status)
        ]
        return sorted(
            scoped,
            key=lambda item: (item.updated_at, str(item.candidate_id)),
            reverse=True,
        )

    async def save_resolutions(
        self,
        candidates: Sequence[EntityResolutionCandidate],
    ) -> Sequence[EntityResolutionCandidate]:
        async with self._transaction_lock():
            entities, relations, resolutions, reviews = self._read_all()
            preserved_pending_ids = {
                candidate_id
                for candidate_id, candidate in resolutions.items()
                if candidate.status == GraphCandidateStatus.PENDING
            }
            for candidate in candidates:
                self._require_resolution_endpoints(candidate, entities)
                existing = resolutions.get(str(candidate.candidate_id))
                merged = self._merge_resolution(candidate, existing)
                resolutions[str(merged.candidate_id)] = merged
            resolutions = _compact_pending_resolution_forest(
                resolutions,
                preserved_pending_ids=preserved_pending_ids,
            )
            stored = [
                stored_candidate
                for candidate in candidates
                if (stored_candidate := resolutions.get(str(candidate.candidate_id))) is not None
            ]
            self._write_all(entities, relations, resolutions, reviews)
        return stored

    async def compact_pending_resolutions(self) -> tuple[int, int]:
        """Remove transitive pending proposals while preserving reviewed history."""

        async with self._transaction_lock():
            entities, relations, resolutions, reviews = self._read_all()
            before = len(resolutions)
            resolutions = _compact_pending_resolution_forest(resolutions)
            after = len(resolutions)
            if after != before:
                self._write_all(entities, relations, resolutions, reviews)
        return before, after

    async def reconcile_pending_evidence(
        self,
        active_chunk_ids: set[UUID],
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        dry_run: bool = False,
    ) -> CandidateEvidenceReconciliationCounts:
        """Archive unreviewed candidates whose complete evidence is no longer active."""

        async with self._transaction_lock():
            entities, relations, resolutions, reviews = self._read_all()
            now = utc_now()

            def stale_evidence(source_chunk_ids: Sequence[UUID]) -> bool:
                return not source_chunk_ids or any(
                    chunk_id not in active_chunk_ids for chunk_id in source_chunk_ids
                )

            entities_archived = 0
            for candidate_id, entity_candidate in tuple(entities.items()):
                if (
                    entity_candidate.tenant_id == tenant_id
                    and entity_candidate.project_id == project_id
                    and entity_candidate.status == GraphCandidateStatus.PENDING
                    and stale_evidence(entity_candidate.source_chunk_ids)
                ):
                    entities_archived += 1
                    entities[candidate_id] = entity_candidate.model_copy(
                        update={
                            "status": GraphCandidateStatus.ARCHIVED,
                            "updated_at": now,
                        }
                    )

            live_entity_ids = {
                candidate_id
                for candidate_id, candidate in entities.items()
                if candidate.status != GraphCandidateStatus.ARCHIVED
            }
            relations_archived = 0
            for candidate_id, relation_candidate in tuple(relations.items()):
                if (
                    relation_candidate.tenant_id == tenant_id
                    and relation_candidate.project_id == project_id
                    and relation_candidate.status == GraphCandidateStatus.PENDING
                    and (
                        stale_evidence(relation_candidate.source_chunk_ids)
                        or str(relation_candidate.source_candidate_id) not in live_entity_ids
                        or str(relation_candidate.target_candidate_id) not in live_entity_ids
                    )
                ):
                    relations_archived += 1
                    relations[candidate_id] = relation_candidate.model_copy(
                        update={
                            "status": GraphCandidateStatus.ARCHIVED,
                            "updated_at": now,
                        }
                    )

            resolutions_archived = 0
            for candidate_id, resolution_candidate in tuple(resolutions.items()):
                if (
                    resolution_candidate.tenant_id == tenant_id
                    and resolution_candidate.project_id == project_id
                    and resolution_candidate.status == GraphCandidateStatus.PENDING
                    and (
                        stale_evidence(resolution_candidate.source_chunk_ids)
                        or str(resolution_candidate.left_entity_id) not in live_entity_ids
                        or str(resolution_candidate.right_entity_id) not in live_entity_ids
                    )
                ):
                    resolutions_archived += 1
                    resolutions[candidate_id] = resolution_candidate.model_copy(
                        update={
                            "status": GraphCandidateStatus.ARCHIVED,
                            "updated_at": now,
                        }
                    )

            counts = CandidateEvidenceReconciliationCounts(
                entities_archived=entities_archived,
                relations_archived=relations_archived,
                resolutions_archived=resolutions_archived,
            )
            if not dry_run and any(
                (
                    counts.entities_archived,
                    counts.relations_archived,
                    counts.resolutions_archived,
                )
            ):
                self._write_all(entities, relations, resolutions, reviews)
        return counts

    async def list_resolutions(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        document_id: UUID | None = None,
        status: str | None = None,
    ) -> Sequence[EntityResolutionCandidate]:
        requested_status = _status_value(status)
        async with self._transaction_lock():
            resolutions = list(self._read_all()[2].values())
        scoped = [
            item
            for item in resolutions
            if item.tenant_id == tenant_id
            and item.project_id == project_id
            and (
                document_id is None
                or document_id in {item.left_document_id, item.right_document_id}
            )
            and (requested_status is None or item.status == requested_status)
        ]
        return sorted(
            scoped,
            key=lambda item: (item.updated_at, str(item.candidate_id)),
            reverse=True,
        )

    async def get_entity(
        self,
        candidate_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> GraphEntityCandidate | None:
        async with self._transaction_lock():
            candidate = self._read_all()[0].get(str(candidate_id))
        if (
            candidate is None
            or candidate.tenant_id != tenant_id
            or candidate.project_id != project_id
        ):
            return None
        return candidate

    async def get_relation(
        self,
        candidate_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> GraphRelationCandidate | None:
        async with self._transaction_lock():
            candidate = self._read_all()[1].get(str(candidate_id))
        if (
            candidate is None
            or candidate.tenant_id != tenant_id
            or candidate.project_id != project_id
        ):
            return None
        return candidate

    async def get_resolution(
        self,
        candidate_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> EntityResolutionCandidate | None:
        async with self._transaction_lock():
            candidate = self._read_all()[2].get(str(candidate_id))
        if (
            candidate is None
            or candidate.tenant_id != tenant_id
            or candidate.project_id != project_id
        ):
            return None
        return candidate

    async def save_entity(
        self,
        candidate: GraphEntityCandidate,
    ) -> GraphEntityCandidate:
        async with self._transaction_lock():
            entities, relations, resolutions, reviews = self._read_all()
            existing = entities.get(str(candidate.candidate_id))
            _require_same_identity(existing, candidate)
            entities[str(candidate.candidate_id)] = candidate
            self._write_all(entities, relations, resolutions, reviews)
        return candidate

    async def save_relation(
        self,
        candidate: GraphRelationCandidate,
    ) -> GraphRelationCandidate:
        async with self._transaction_lock():
            entities, relations, resolutions, reviews = self._read_all()
            existing = relations.get(str(candidate.candidate_id))
            _require_same_identity(existing, candidate)
            if (
                str(candidate.source_candidate_id) not in entities
                or str(candidate.target_candidate_id) not in entities
            ):
                raise GraphCandidateStoreError(
                    "Relation candidate references an unknown entity candidate"
                )
            relations[str(candidate.candidate_id)] = candidate
            self._write_all(entities, relations, resolutions, reviews)
        return candidate

    async def save_resolution(
        self,
        candidate: EntityResolutionCandidate,
    ) -> EntityResolutionCandidate:
        async with self._transaction_lock():
            entities, relations, resolutions, reviews = self._read_all()
            existing = resolutions.get(str(candidate.candidate_id))
            _require_same_resolution_identity(existing, candidate)
            self._require_resolution_endpoints(candidate, entities)
            resolutions[str(candidate.candidate_id)] = candidate
            self._write_all(entities, relations, resolutions, reviews)
        return candidate

    async def save_review(
        self,
        review: GraphCandidateReviewEvent,
    ) -> GraphCandidateReviewEvent:
        async with self._transaction_lock():
            entities, relations, resolutions, reviews = self._read_all()
            candidate: (
                GraphEntityCandidate | GraphRelationCandidate | EntityResolutionCandidate | None
            )
            if review.candidate_type == "entity":
                candidate = entities.get(str(review.candidate_id))
            elif review.candidate_type == "relation":
                candidate = relations.get(str(review.candidate_id))
            else:
                candidate = resolutions.get(str(review.candidate_id))
            if (
                candidate is None
                or candidate.tenant_id != review.tenant_id
                or candidate.project_id != review.project_id
            ):
                raise GraphCandidateStoreError("Review references an unknown candidate")
            reviews[str(review.review_id)] = review
            self._write_all(entities, relations, resolutions, reviews)
        return review

    async def list_reviews(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        candidate_id: UUID | None = None,
    ) -> Sequence[GraphCandidateReviewEvent]:
        async with self._transaction_lock():
            reviews = list(self._read_all()[3].values())
        scoped = [
            item
            for item in reviews
            if item.tenant_id == tenant_id
            and item.project_id == project_id
            and (candidate_id is None or item.candidate_id == candidate_id)
        ]
        return sorted(scoped, key=lambda item: (item.created_at, str(item.review_id)), reverse=True)

    async def archive_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> None:
        now = utc_now()
        async with self._transaction_lock():
            entities, relations, resolutions, reviews = self._read_all()
            entities = {
                key: (
                    item.model_copy(
                        update={"status": GraphCandidateStatus.ARCHIVED, "updated_at": now}
                    )
                    if item.document_id == document_id
                    and item.tenant_id == tenant_id
                    and item.project_id == project_id
                    else item
                )
                for key, item in entities.items()
            }
            relations = {
                key: (
                    item.model_copy(
                        update={"status": GraphCandidateStatus.ARCHIVED, "updated_at": now}
                    )
                    if item.document_id == document_id
                    and item.tenant_id == tenant_id
                    and item.project_id == project_id
                    else item
                )
                for key, item in relations.items()
            }
            resolutions = {
                key: (
                    item.model_copy(
                        update={"status": GraphCandidateStatus.ARCHIVED, "updated_at": now}
                    )
                    if document_id in {item.left_document_id, item.right_document_id}
                    and item.tenant_id == tenant_id
                    and item.project_id == project_id
                    else item
                )
                for key, item in resolutions.items()
            }
            self._write_all(entities, relations, resolutions, reviews)

    @asynccontextmanager
    async def _transaction_lock(self) -> AsyncIterator[None]:
        """Serialize the JSON read-modify-write transaction across worker processes."""

        async with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._path.with_name(f".{self._path.name}.lock")
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _require_batch_scope(
        batch: GraphExtractionBatch,
        candidate: GraphEntityCandidate | GraphRelationCandidate,
    ) -> None:
        if (
            candidate.document_id != batch.document_id
            or candidate.tenant_id != batch.tenant_id
            or candidate.project_id != batch.project_id
            or candidate.extractor_revision != batch.extractor_revision
        ):
            raise GraphCandidateStoreError("Candidate does not match extraction batch scope")

    @staticmethod
    def _merge_entity(
        candidate: GraphEntityCandidate,
        existing: GraphEntityCandidate | None,
    ) -> GraphEntityCandidate:
        _require_same_identity(existing, candidate)
        if existing is None:
            return candidate
        status_fields = _review_fields(existing, candidate)
        return candidate.model_copy(
            update={
                **status_fields,
                "aliases": sorted({*existing.aliases, *candidate.aliases}, key=str.casefold)[:20],
                "source_chunk_ids": sorted(
                    {*existing.source_chunk_ids, *candidate.source_chunk_ids}, key=str
                )[:50],
                "confidence": max(existing.confidence, candidate.confidence),
                "created_at": existing.created_at,
                "updated_at": utc_now(),
            }
        )

    @staticmethod
    def _merge_relation(
        candidate: GraphRelationCandidate,
        existing: GraphRelationCandidate | None,
    ) -> GraphRelationCandidate:
        _require_same_identity(existing, candidate)
        if existing is None:
            return candidate
        return candidate.model_copy(
            update={
                **_review_fields(existing, candidate),
                "source_chunk_ids": sorted(
                    {*existing.source_chunk_ids, *candidate.source_chunk_ids}, key=str
                )[:50],
                "confidence": max(existing.confidence, candidate.confidence),
                "created_at": existing.created_at,
                "updated_at": utc_now(),
            }
        )

    @staticmethod
    def _merge_resolution(
        candidate: EntityResolutionCandidate,
        existing: EntityResolutionCandidate | None,
    ) -> EntityResolutionCandidate:
        _require_same_resolution_identity(existing, candidate)
        if existing is None:
            return candidate
        return candidate.model_copy(
            update={
                **_review_fields(existing, candidate),
                "source_chunk_ids": sorted(
                    {*existing.source_chunk_ids, *candidate.source_chunk_ids}, key=str
                )[:100],
                "confidence": max(existing.confidence, candidate.confidence),
                "created_at": existing.created_at,
                "updated_at": utc_now(),
            }
        )

    @staticmethod
    def _require_resolution_endpoints(
        candidate: EntityResolutionCandidate,
        entities: dict[str, GraphEntityCandidate],
    ) -> None:
        left = entities.get(str(candidate.left_entity_id))
        right = entities.get(str(candidate.right_entity_id))
        if left is None or right is None:
            raise GraphCandidateStoreError(
                "Entity resolution references an unknown entity candidate"
            )
        if (
            left.tenant_id != candidate.tenant_id
            or right.tenant_id != candidate.tenant_id
            or left.project_id != candidate.project_id
            or right.project_id != candidate.project_id
            or left.document_id != candidate.left_document_id
            or right.document_id != candidate.right_document_id
            or left.document_id == right.document_id
        ):
            raise GraphCandidateStoreError(
                "Entity resolution endpoints do not match candidate scope"
            )

    def _read_all(
        self,
    ) -> tuple[
        dict[str, GraphEntityCandidate],
        dict[str, GraphRelationCandidate],
        dict[str, EntityResolutionCandidate],
        dict[str, GraphCandidateReviewEvent],
    ]:
        if not self._path.exists():
            return {}, {}, {}, {}
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            if document.get("version") not in self._READABLE_VERSIONS:
                raise ValueError("Unsupported graph candidate store version")
            raw_entities = document["entities"]
            raw_relations = document["relations"]
            raw_resolutions = document.get("resolutions", [])
            raw_reviews = document.get("reviews", [])
            if (
                not isinstance(raw_entities, list)
                or not isinstance(raw_relations, list)
                or not isinstance(raw_resolutions, list)
                or not isinstance(raw_reviews, list)
            ):
                raise TypeError("Graph candidate records must be lists")
            entities = [GraphEntityCandidate.model_validate(item) for item in raw_entities]
            relations = [GraphRelationCandidate.model_validate(item) for item in raw_relations]
            resolutions = [
                EntityResolutionCandidate.model_validate(item) for item in raw_resolutions
            ]
            reviews = [GraphCandidateReviewEvent.model_validate(item) for item in raw_reviews]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GraphCandidateStoreError(
                f"Invalid graph candidate store at {self._path}"
            ) from exc
        return (
            {str(item.candidate_id): item for item in entities},
            {str(item.candidate_id): item for item in relations},
            {str(item.candidate_id): item for item in resolutions},
            {str(item.review_id): item for item in reviews},
        )

    def _write_all(
        self,
        entities: dict[str, GraphEntityCandidate],
        relations: dict[str, GraphRelationCandidate],
        resolutions: dict[str, EntityResolutionCandidate],
        reviews: dict[str, GraphCandidateReviewEvent],
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": self._FORMAT_VERSION,
            "entities": [
                item.model_dump(mode="json")
                for item in sorted(entities.values(), key=lambda item: str(item.candidate_id))
            ],
            "relations": [
                item.model_dump(mode="json")
                for item in sorted(relations.values(), key=lambda item: str(item.candidate_id))
            ],
            "resolutions": [
                item.model_dump(mode="json")
                for item in sorted(resolutions.values(), key=lambda item: str(item.candidate_id))
            ],
            "reviews": [
                item.model_dump(mode="json")
                for item in sorted(reviews.values(), key=lambda item: str(item.review_id))
            ],
        }
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)


def _supersede_pending_document_candidates(
    batch: GraphExtractionBatch,
    entities: dict[str, GraphEntityCandidate],
    relations: dict[str, GraphRelationCandidate],
    resolutions: dict[str, EntityResolutionCandidate],
) -> tuple[
    dict[str, GraphEntityCandidate],
    dict[str, GraphRelationCandidate],
    dict[str, EntityResolutionCandidate],
]:
    """Archive stale unreviewed candidates only after a replacement batch exists."""

    incoming_entity_ids = {str(item.candidate_id) for item in batch.entities}
    incoming_relation_ids = {str(item.candidate_id) for item in batch.relations}
    now = utc_now()

    def same_document(candidate: GraphEntityCandidate | GraphRelationCandidate) -> bool:
        return (
            candidate.document_id == batch.document_id
            and candidate.tenant_id == batch.tenant_id
            and candidate.project_id == batch.project_id
        )

    entities = {
        candidate_id: (
            candidate.model_copy(
                update={
                    "status": GraphCandidateStatus.ARCHIVED,
                    "updated_at": now,
                }
            )
            if candidate.status == GraphCandidateStatus.PENDING
            and same_document(candidate)
            and candidate_id not in incoming_entity_ids
            else candidate
        )
        for candidate_id, candidate in entities.items()
    }
    relations = {
        candidate_id: (
            candidate.model_copy(
                update={
                    "status": GraphCandidateStatus.ARCHIVED,
                    "updated_at": now,
                }
            )
            if candidate.status == GraphCandidateStatus.PENDING
            and same_document(candidate)
            and candidate_id not in incoming_relation_ids
            else candidate
        )
        for candidate_id, candidate in relations.items()
    }
    live_entity_ids = {
        candidate_id
        for candidate_id, candidate in entities.items()
        if candidate.status != GraphCandidateStatus.ARCHIVED
    } | incoming_entity_ids
    resolutions = {
        candidate_id: (
            candidate.model_copy(
                update={
                    "status": GraphCandidateStatus.ARCHIVED,
                    "updated_at": now,
                }
            )
            if candidate.status == GraphCandidateStatus.PENDING
            and candidate.tenant_id == batch.tenant_id
            and candidate.project_id == batch.project_id
            and (
                str(candidate.left_entity_id) not in live_entity_ids
                or str(candidate.right_entity_id) not in live_entity_ids
            )
            else candidate
        )
        for candidate_id, candidate in resolutions.items()
    }
    return entities, relations, resolutions


def _status_value(value: str | None) -> GraphCandidateStatus | None:
    if value is None:
        return None
    try:
        return GraphCandidateStatus(value)
    except ValueError as exc:
        raise ValueError(f"Unknown graph candidate status: {value}") from exc


_RESOLUTION_STRATEGY_PRIORITY = {
    "exact_identifier": 0,
    "exact_name": 1,
    "normalized_name": 2,
    "alias_overlap": 3,
}


def _compact_pending_resolution_forest(
    resolutions: dict[str, EntityResolutionCandidate],
    *,
    preserved_pending_ids: set[str] | None = None,
) -> dict[str, EntityResolutionCandidate]:
    parent: dict[UUID, UUID] = {}

    def find(candidate_id: UUID) -> UUID:
        root = parent.setdefault(candidate_id, candidate_id)
        while root != parent[root]:
            root = parent[root]
        while candidate_id != root:
            next_id = parent[candidate_id]
            parent[candidate_id] = root
            candidate_id = next_id
        return root

    def union(left_id: UUID, right_id: UUID) -> bool:
        left_root = find(left_id)
        right_root = find(right_id)
        if left_root == right_root:
            return False
        if str(left_root) > str(right_root):
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        return True

    compacted = {
        candidate_id: candidate
        for candidate_id, candidate in resolutions.items()
        if candidate.status != GraphCandidateStatus.PENDING
    }
    for candidate in compacted.values():
        if candidate.status == GraphCandidateStatus.APPROVED:
            union(candidate.left_entity_id, candidate.right_entity_id)

    preserved_pending_ids = preserved_pending_ids or set()
    for candidate_id in sorted(preserved_pending_ids):
        preserved_candidate = resolutions.get(candidate_id)
        if (
            preserved_candidate is None
            or preserved_candidate.status != GraphCandidateStatus.PENDING
        ):
            continue
        compacted[candidate_id] = preserved_candidate
        union(
            preserved_candidate.left_entity_id,
            preserved_candidate.right_entity_id,
        )

    pending = sorted(
        (
            candidate
            for candidate in resolutions.values()
            if candidate.status == GraphCandidateStatus.PENDING
            and str(candidate.candidate_id) not in preserved_pending_ids
        ),
        key=lambda candidate: (
            -candidate.confidence,
            _RESOLUTION_STRATEGY_PRIORITY.get(candidate.match_strategy, 99),
            str(candidate.candidate_id),
        ),
    )
    for candidate in pending:
        if not union(candidate.left_entity_id, candidate.right_entity_id):
            continue
        compacted[str(candidate.candidate_id)] = candidate
    return compacted


def _review_fields(
    existing: GraphEntityCandidate | GraphRelationCandidate | EntityResolutionCandidate,
    incoming: GraphEntityCandidate | GraphRelationCandidate | EntityResolutionCandidate,
) -> dict[str, object]:
    if existing.status in {GraphCandidateStatus.APPROVED, GraphCandidateStatus.REJECTED}:
        return {
            "status": existing.status,
            "reviewed_by": existing.reviewed_by,
            "reviewed_at": existing.reviewed_at,
        }
    if (
        existing.status == GraphCandidateStatus.ARCHIVED
        and incoming.status == GraphCandidateStatus.APPROVED
        and incoming.reviewed_by is not None
        and incoming.reviewed_by == existing.reviewed_by
        and incoming.reviewed_at == existing.reviewed_at
        and _candidate_revision(incoming) == _candidate_revision(existing)
    ):
        return {
            "status": incoming.status,
            "reviewed_by": incoming.reviewed_by,
            "reviewed_at": incoming.reviewed_at,
        }
    return {
        "status": GraphCandidateStatus.PENDING,
        "reviewed_by": None,
        "reviewed_at": None,
    }


def _candidate_revision(
    candidate: GraphEntityCandidate | GraphRelationCandidate | EntityResolutionCandidate,
) -> str:
    if isinstance(candidate, EntityResolutionCandidate):
        return candidate.resolver_revision
    return candidate.extractor_revision


def _require_same_identity(
    existing: GraphEntityCandidate | GraphRelationCandidate | None,
    candidate: GraphEntityCandidate | GraphRelationCandidate,
) -> None:
    if existing is None:
        return
    if (
        type(existing) is not type(candidate)
        or existing.document_id != candidate.document_id
        or existing.tenant_id != candidate.tenant_id
        or existing.project_id != candidate.project_id
    ):
        raise GraphCandidateStoreError("Candidate identity or scope cannot be changed")
    if isinstance(existing, GraphEntityCandidate) and isinstance(candidate, GraphEntityCandidate):
        if (
            existing.canonical_name != candidate.canonical_name
            or existing.entity_type != candidate.entity_type
        ):
            raise GraphCandidateStoreError("Entity candidate identity cannot be changed")
    if isinstance(existing, GraphRelationCandidate) and isinstance(
        candidate, GraphRelationCandidate
    ):
        if (
            existing.source_candidate_id != candidate.source_candidate_id
            or existing.target_candidate_id != candidate.target_candidate_id
            or existing.relation_type != candidate.relation_type
        ):
            raise GraphCandidateStoreError("Relation candidate identity cannot be changed")


def _require_same_resolution_identity(
    existing: EntityResolutionCandidate | None,
    candidate: EntityResolutionCandidate,
) -> None:
    if existing is None:
        return
    if (
        existing.tenant_id != candidate.tenant_id
        or existing.project_id != candidate.project_id
        or existing.left_entity_id != candidate.left_entity_id
        or existing.right_entity_id != candidate.right_entity_id
        or existing.left_document_id != candidate.left_document_id
        or existing.right_document_id != candidate.right_document_id
    ):
        raise GraphCandidateStoreError("Entity resolution identity or scope cannot be changed")
