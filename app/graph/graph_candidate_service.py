from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar
from uuid import UUID

from app.domain.contracts import (
    EntityRelationExtractorPort,
    EntityResolverPort,
    GraphCandidateRepository,
    KnowledgeGraphIndexPort,
    SemanticGraphIndexPort,
)
from app.domain.enums import GraphCandidateStatus
from app.domain.models import (
    EntityResolutionCandidate,
    GraphCandidateCollection,
    GraphCandidateReviewEvent,
    GraphEntityCandidate,
    GraphRelationCandidate,
    KnowledgeChunk,
    KnowledgeDocument,
    utc_now,
)


class GraphCandidateReviewError(ValueError):
    pass


_CandidateT = TypeVar(
    "_CandidateT",
    GraphEntityCandidate,
    GraphRelationCandidate,
    EntityResolutionCandidate,
)


class KnowledgeGraphIngestionCoordinator:
    """Coordinates structural graph writes with candidate extraction and audit storage."""

    def __init__(
        self,
        extractor: EntityRelationExtractorPort,
        repository: GraphCandidateRepository,
        *,
        entity_resolver: EntityResolverPort | None = None,
        structural_index: KnowledgeGraphIndexPort | None = None,
        semantic_index: SemanticGraphIndexPort | None = None,
        domain_pack: str = "general",
        max_extraction_chars: int = 20_000,
        public_reference_max_extraction_chars: int = 5_000,
    ) -> None:
        if not 5_000 <= max_extraction_chars <= 500_000:
            raise ValueError("max_extraction_chars must be between 5000 and 500000")
        if not 2_000 <= public_reference_max_extraction_chars <= max_extraction_chars:
            raise ValueError(
                "public reference extraction budget must fit the general budget"
            )
        self._extractor = extractor
        self._repository = repository
        self._entity_resolver = entity_resolver
        self._structural_index = structural_index
        self._semantic_index = semantic_index
        self._domain_pack = domain_pack
        self._max_extraction_chars = max_extraction_chars
        self._public_reference_max_extraction_chars = (
            public_reference_max_extraction_chars
        )

    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        extraction_budget = (
            self._public_reference_max_extraction_chars
            if document.source.privacy == "public_reference"
            else self._max_extraction_chars
        )
        extraction_chunks = _representative_chunks(
            chunks,
            max_chars=extraction_budget,
        )
        batch = await self._extractor.extract(
            document,
            extraction_chunks,
            domain_pack=self._domain_pack,
        )
        batch = batch.model_copy(
            update={
                "entities": [
                    candidate.model_copy(
                        update={
                            "domain_pack": self._domain_pack,
                            "activation_policy": "review_required",
                        }
                    )
                    for candidate in batch.entities
                ],
                "relations": [
                    candidate.model_copy(
                        update={
                            "domain_pack": self._domain_pack,
                            "activation_policy": "review_required",
                        }
                    )
                    for candidate in batch.relations
                ],
            }
        )
        try:
            if self._structural_index is not None:
                await self._structural_index.index_document(document, chunks)
            stored_batch = await self._repository.save_batch(batch)
            if self._semantic_index is not None:
                await self._semantic_index.index_extraction(stored_batch)
            if self._entity_resolver is not None:
                scoped_entities = await self._repository.list_entities(
                    tenant_id=document.tenant_id,
                    project_id=document.project_id,
                )
                proposals = await self._entity_resolver.propose(
                    stored_batch,
                    [
                        candidate
                        for candidate in scoped_entities
                        if candidate.status
                        in {
                            GraphCandidateStatus.PENDING,
                            GraphCandidateStatus.APPROVED,
                        }
                    ],
                )
                stored_resolutions = await self._repository.save_resolutions(proposals)
                if self._semantic_index is not None and stored_resolutions:
                    await self._semantic_index.index_resolutions(stored_resolutions)
        except Exception:
            await self._repository.archive_document(
                document.document_id,
                tenant_id=document.tenant_id,
                project_id=document.project_id,
            )
            if self._structural_index is not None:
                await self._structural_index.archive_document(
                    document.document_id,
                    tenant_id=document.tenant_id,
                    project_id=document.project_id,
                )
            raise

    async def archive_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> None:
        if self._structural_index is not None:
            await self._structural_index.archive_document(
                document_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        await self._repository.archive_document(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )


def _representative_chunks(
    chunks: Sequence[KnowledgeChunk],
    *,
    max_chars: int,
) -> list[KnowledgeChunk]:
    """Select section-stratified evidence with broad document coverage."""

    ordered = list(chunks)
    if sum(len(item.text) for item in ordered) <= max_chars:
        return ordered
    selected_indexes: set[int] = set()
    used = 0

    def select(index: int) -> bool:
        nonlocal used
        if index in selected_indexes:
            return False
        size = len(ordered[index].text)
        if selected_indexes and used + size > max_chars:
            return False
        selected_indexes.add(index)
        used += size
        return True

    select(0)
    priority_sections = (
        ("abstract",),
        ("introduction", "background"),
        ("method", "methodology", "approach", "architecture"),
        ("experiment", "evaluation", "results"),
        ("discussion", "limitations"),
        ("conclusion", "conclusions"),
    )
    for keywords in priority_sections:
        for index, chunk in enumerate(ordered):
            heading_path = chunk.metadata.get("heading_path", [])
            heading = " ".join(
                str(item).casefold()
                for item in heading_path
                if isinstance(heading_path, list)
            )
            if any(keyword in heading for keyword in keywords) and select(index):
                break
    select(len(ordered) - 1)

    remaining = set(range(len(ordered))) - selected_indexes
    while remaining:
        eligible = {
            index
            for index in remaining
            if used + len(ordered[index].text) <= max_chars
        }
        if not eligible:
            break
        index = max(
            eligible,
            key=lambda candidate: (
                min(abs(candidate - selected) for selected in selected_indexes),
                -candidate,
            ),
        )
        remaining.remove(index)
        select(index)
    return [ordered[index] for index in sorted(selected_indexes)]


class GraphCandidateService:
    """Scoped review gate; approved relations alone become graph-searchable facts."""

    _TRANSITIONS = {
        GraphCandidateStatus.PENDING: {
            GraphCandidateStatus.APPROVED,
            GraphCandidateStatus.REJECTED,
        },
        GraphCandidateStatus.APPROVED: {GraphCandidateStatus.REJECTED},
        GraphCandidateStatus.REJECTED: {GraphCandidateStatus.PENDING},
        GraphCandidateStatus.ARCHIVED: set(),
    }

    def __init__(
        self,
        repository: GraphCandidateRepository,
        *,
        semantic_index: SemanticGraphIndexPort | None = None,
    ) -> None:
        self._repository = repository
        self._semantic_index = semantic_index

    async def list_candidates(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        document_id: UUID | None = None,
        status: GraphCandidateStatus | None = None,
    ) -> GraphCandidateCollection:
        status_value = status.value if status is not None else None
        entities, relations, resolutions = await _gather_candidates(
            self._repository,
            tenant_id=tenant_id,
            project_id=project_id,
            document_id=document_id,
            status=status_value,
        )
        return GraphCandidateCollection(
            entities=list(entities),
            relations=list(relations),
            resolutions=list(resolutions),
        )

    async def review_entity(
        self,
        candidate_id: UUID,
        target_status: GraphCandidateStatus,
        *,
        reviewer_id: str,
        reason: str = "",
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> GraphEntityCandidate:
        candidate = await self._repository.get_entity(
            candidate_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if candidate is None:
            raise KeyError("Graph entity candidate not found")
        self._require_transition(candidate.status, target_status)
        if candidate.status == target_status:
            return candidate
        updated = _reviewed_candidate(candidate, target_status, reviewer_id)
        relation_updates: list[
            tuple[GraphRelationCandidate, GraphRelationCandidate]
        ] = []
        resolution_updates: list[
            tuple[EntityResolutionCandidate, EntityResolutionCandidate]
        ] = []
        if target_status == GraphCandidateStatus.REJECTED:
            scoped_relations = await self._repository.list_relations(
                tenant_id=tenant_id,
                project_id=project_id,
            )
            relation_updates = [
                (
                    relation,
                    _reviewed_candidate(
                        relation,
                        GraphCandidateStatus.REJECTED,
                        reviewer_id,
                    ),
                )
                for relation in scoped_relations
                if candidate.candidate_id
                in {relation.source_candidate_id, relation.target_candidate_id}
                and relation.status
                not in {
                    GraphCandidateStatus.REJECTED,
                    GraphCandidateStatus.ARCHIVED,
                }
            ]
            scoped_resolutions = await self._repository.list_resolutions(
                tenant_id=tenant_id,
                project_id=project_id,
            )
            resolution_updates = [
                (
                    resolution,
                    _reviewed_candidate(
                        resolution,
                        GraphCandidateStatus.REJECTED,
                        reviewer_id,
                    ),
                )
                for resolution in scoped_resolutions
                if candidate.candidate_id
                in {resolution.left_entity_id, resolution.right_entity_id}
                and resolution.status
                not in {
                    GraphCandidateStatus.REJECTED,
                    GraphCandidateStatus.ARCHIVED,
                }
            ]
        try:
            if self._semantic_index is not None:
                await self._semantic_index.set_entity_status(updated)
                for _, relation in relation_updates:
                    await self._semantic_index.set_relation_status(relation)
                for _, resolution_update in resolution_updates:
                    await self._semantic_index.set_resolution_status(resolution_update)
            await self._repository.save_entity(updated)
            for _, relation in relation_updates:
                await self._repository.save_relation(relation)
            for _, resolution_update in resolution_updates:
                await self._repository.save_resolution(resolution_update)
        except Exception:
            if self._semantic_index is not None:
                await self._semantic_index.set_entity_status(candidate)
                for original, _ in relation_updates:
                    await self._semantic_index.set_relation_status(original)
                for original_resolution, _ in resolution_updates:
                    await self._semantic_index.set_resolution_status(
                        original_resolution
                    )
            raise
        await self._record_review(
            "entity",
            candidate.candidate_id,
            candidate.status,
            target_status,
            reviewer_id,
            reason,
            tenant_id,
            project_id,
            domain_pack=candidate.domain_pack,
            activation_policy=candidate.activation_policy,
        )
        for original, relation in relation_updates:
            await self._record_review(
                "relation",
                relation.candidate_id,
                original.status,
                relation.status,
                reviewer_id,
                f"Auto-rejected with entity {candidate.candidate_id}. {reason}".strip(),
                tenant_id,
                project_id,
                domain_pack=relation.domain_pack,
                activation_policy=relation.activation_policy,
            )
        for original_resolution, resolution_update in resolution_updates:
            await self._record_review(
                "resolution",
                resolution_update.candidate_id,
                original_resolution.status,
                resolution_update.status,
                reviewer_id,
                f"Auto-rejected with entity {candidate.candidate_id}. {reason}".strip(),
                tenant_id,
                project_id,
            )
        return updated

    async def review_relation(
        self,
        candidate_id: UUID,
        target_status: GraphCandidateStatus,
        *,
        reviewer_id: str,
        reason: str = "",
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> GraphRelationCandidate:
        candidate = await self._repository.get_relation(
            candidate_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if candidate is None:
            raise KeyError("Graph relation candidate not found")
        self._require_transition(candidate.status, target_status)
        if candidate.status == target_status:
            return candidate

        entity_updates: list[tuple[GraphEntityCandidate, GraphEntityCandidate]] = []
        if target_status == GraphCandidateStatus.APPROVED:
            entity_updates = await self._approve_relation_entities(
                candidate,
                reviewer_id=reviewer_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        updated = _reviewed_candidate(candidate, target_status, reviewer_id)
        try:
            if self._semantic_index is not None:
                for _, entity in entity_updates:
                    await self._semantic_index.set_entity_status(entity)
                await self._semantic_index.set_relation_status(updated)
            for _, entity in entity_updates:
                await self._repository.save_entity(entity)
            await self._repository.save_relation(updated)
        except Exception:
            if self._semantic_index is not None:
                for original, _ in entity_updates:
                    await self._semantic_index.set_entity_status(original)
                await self._semantic_index.set_relation_status(candidate)
            raise

        for original, entity in entity_updates:
            await self._record_review(
                "entity",
                entity.candidate_id,
                original.status,
                entity.status,
                reviewer_id,
                f"Auto-approved with relation {candidate.candidate_id}. {reason}".strip(),
                tenant_id,
                project_id,
                domain_pack=entity.domain_pack,
                activation_policy=entity.activation_policy,
            )
        await self._record_review(
            "relation",
            candidate.candidate_id,
            candidate.status,
            target_status,
            reviewer_id,
            reason,
            tenant_id,
            project_id,
            domain_pack=candidate.domain_pack,
            activation_policy=candidate.activation_policy,
        )
        return updated

    async def review_resolution(
        self,
        candidate_id: UUID,
        target_status: GraphCandidateStatus,
        *,
        reviewer_id: str,
        reason: str = "",
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> EntityResolutionCandidate:
        candidate = await self._repository.get_resolution(
            candidate_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if candidate is None:
            raise KeyError("Entity resolution candidate not found")
        self._require_transition(candidate.status, target_status)
        if candidate.status == target_status:
            return candidate

        entity_updates: list[tuple[GraphEntityCandidate, GraphEntityCandidate]] = []
        if target_status == GraphCandidateStatus.APPROVED:
            entity_updates = await self._approve_resolution_entities(
                candidate,
                reviewer_id=reviewer_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
        updated = _reviewed_candidate(candidate, target_status, reviewer_id)
        try:
            if self._semantic_index is not None:
                for _, entity in entity_updates:
                    await self._semantic_index.set_entity_status(entity)
                await self._semantic_index.set_resolution_status(updated)
            for _, entity in entity_updates:
                await self._repository.save_entity(entity)
            await self._repository.save_resolution(updated)
        except Exception:
            if self._semantic_index is not None:
                for original, _ in entity_updates:
                    await self._semantic_index.set_entity_status(original)
                await self._semantic_index.set_resolution_status(candidate)
            raise

        for original, entity in entity_updates:
            await self._record_review(
                "entity",
                entity.candidate_id,
                original.status,
                entity.status,
                reviewer_id,
                f"Auto-approved with entity resolution {candidate.candidate_id}. "
                f"{reason}".strip(),
                tenant_id,
                project_id,
                domain_pack=entity.domain_pack,
                activation_policy=entity.activation_policy,
            )
        await self._record_review(
            "resolution",
            candidate.candidate_id,
            candidate.status,
            target_status,
            reviewer_id,
            reason,
            tenant_id,
            project_id,
        )
        return updated

    async def _approve_relation_entities(
        self,
        relation: GraphRelationCandidate,
        *,
        reviewer_id: str,
        tenant_id: str,
        project_id: str,
    ) -> list[tuple[GraphEntityCandidate, GraphEntityCandidate]]:
        updates: list[tuple[GraphEntityCandidate, GraphEntityCandidate]] = []
        for candidate_id in (
            relation.source_candidate_id,
            relation.target_candidate_id,
        ):
            entity = await self._repository.get_entity(
                candidate_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if entity is None:
                raise GraphCandidateReviewError(
                    "Relation references a missing entity candidate"
                )
            if entity.status in {
                GraphCandidateStatus.REJECTED,
                GraphCandidateStatus.ARCHIVED,
            }:
                raise GraphCandidateReviewError(
                    "Rejected or archived entities block relation approval"
                )
            if entity.status == GraphCandidateStatus.PENDING:
                updates.append(
                    (
                        entity,
                        _reviewed_candidate(
                            entity,
                            GraphCandidateStatus.APPROVED,
                            reviewer_id,
                        ),
                    )
                )
        return updates

    async def _approve_resolution_entities(
        self,
        resolution: EntityResolutionCandidate,
        *,
        reviewer_id: str,
        tenant_id: str,
        project_id: str,
    ) -> list[tuple[GraphEntityCandidate, GraphEntityCandidate]]:
        updates: list[tuple[GraphEntityCandidate, GraphEntityCandidate]] = []
        for candidate_id in (
            resolution.left_entity_id,
            resolution.right_entity_id,
        ):
            entity = await self._repository.get_entity(
                candidate_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if entity is None:
                raise GraphCandidateReviewError(
                    "Entity resolution references a missing entity candidate"
                )
            if entity.status in {
                GraphCandidateStatus.REJECTED,
                GraphCandidateStatus.ARCHIVED,
            }:
                raise GraphCandidateReviewError(
                    "Rejected or archived entities block resolution approval"
                )
            if entity.status == GraphCandidateStatus.PENDING:
                updates.append(
                    (
                        entity,
                        _reviewed_candidate(
                            entity,
                            GraphCandidateStatus.APPROVED,
                            reviewer_id,
                        ),
                    )
                )
        return updates

    @classmethod
    def _require_transition(
        cls,
        current: GraphCandidateStatus,
        target: GraphCandidateStatus,
    ) -> None:
        if current == target:
            return
        if target not in cls._TRANSITIONS[current]:
            raise GraphCandidateReviewError(
                f"Graph candidate transition is not allowed: {current} -> {target}"
            )

    async def _record_review(
        self,
        candidate_type: str,
        candidate_id: UUID,
        from_status: GraphCandidateStatus,
        to_status: GraphCandidateStatus,
        reviewer_id: str,
        reason: str,
        tenant_id: str,
        project_id: str,
        *,
        domain_pack: str = "general",
        activation_policy: str = "review_required",
    ) -> None:
        await self._repository.save_review(
            GraphCandidateReviewEvent(
                tenant_id=tenant_id,
                project_id=project_id,
                candidate_type=candidate_type,
                candidate_id=candidate_id,
                from_status=from_status,
                to_status=to_status,
                reviewer_id=reviewer_id,
                reason=reason,
                domain_pack=domain_pack,
                activation_policy=activation_policy,
            )
        )


async def _gather_candidates(
    repository: GraphCandidateRepository,
    *,
    tenant_id: str,
    project_id: str,
    document_id: UUID | None,
    status: str | None,
) -> tuple[
    Sequence[GraphEntityCandidate],
    Sequence[GraphRelationCandidate],
    Sequence[EntityResolutionCandidate],
]:
    entities = await repository.list_entities(
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=document_id,
        status=status,
    )
    relations = await repository.list_relations(
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=document_id,
        status=status,
    )
    resolutions = await repository.list_resolutions(
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=document_id,
        status=status,
    )
    return entities, relations, resolutions


def _reviewed_candidate(
    candidate: _CandidateT,
    target_status: GraphCandidateStatus,
    reviewer_id: str,
) -> _CandidateT:
    now = utc_now()
    reviewed = target_status != GraphCandidateStatus.PENDING
    return candidate.model_copy(
        update={
            "status": target_status,
            "reviewed_by": reviewer_id if reviewed else None,
            "reviewed_at": now if reviewed else None,
            "updated_at": now,
        }
    )
