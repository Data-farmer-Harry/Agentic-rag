from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.contracts import KnowledgeRepository
from app.graph.candidate_store import JsonGraphCandidateRepository


class StaleCandidateGraphArchiver(Protocol):
    async def reconcile_stale_pending_candidates(
        self,
        *,
        tenant_id: str,
        project_id: str,
        dry_run: bool = False,
    ) -> tuple[int, int, int]: ...


class CandidateEvidenceReconciliationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    documents_scanned: int = Field(ge=0)
    active_chunks_scanned: int = Field(ge=0)
    store_entities_archived: int = Field(ge=0)
    store_relations_archived: int = Field(ge=0)
    store_resolutions_archived: int = Field(ge=0)
    graph_entities_archived: int = Field(ge=0)
    graph_relations_archived: int = Field(ge=0)
    graph_resolutions_archived: int = Field(ge=0)
    dry_run: bool = False


class CandidateEvidenceReconciliationService:
    """Align candidate review queues with the current retained chunk revision."""

    def __init__(
        self,
        knowledge: KnowledgeRepository,
        candidates: JsonGraphCandidateRepository,
        graph: StaleCandidateGraphArchiver,
    ) -> None:
        self._knowledge = knowledge
        self._candidates = candidates
        self._graph = graph

    async def run(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "computer-science",
        concurrency: int = 8,
        dry_run: bool = False,
    ) -> CandidateEvidenceReconciliationSummary:
        if not 1 <= concurrency <= 32:
            raise ValueError("Candidate reconciliation concurrency must be between 1 and 32")
        documents = await self._knowledge.list_documents(
            tenant_id=tenant_id,
            project_id=project_id,
        )
        semaphore = asyncio.Semaphore(concurrency)

        async def chunk_ids(document_id: UUID) -> Sequence[UUID]:
            async with semaphore:
                chunks = await self._knowledge.list_chunks(
                    document_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                )
            return [chunk.chunk_id for chunk in chunks]

        active_chunk_ids = {
            chunk_id
            for document_chunk_ids in await asyncio.gather(
                *(chunk_ids(document.document_id) for document in documents)
            )
            for chunk_id in document_chunk_ids
        }
        store_counts = await self._candidates.reconcile_pending_evidence(
            active_chunk_ids,
            tenant_id=tenant_id,
            project_id=project_id,
            dry_run=dry_run,
        )
        graph_entities, graph_relations, graph_resolutions = (
            await self._graph.reconcile_stale_pending_candidates(
                tenant_id=tenant_id,
                project_id=project_id,
                dry_run=dry_run,
            )
        )
        return CandidateEvidenceReconciliationSummary(
            documents_scanned=len(documents),
            active_chunks_scanned=len(active_chunk_ids),
            store_entities_archived=store_counts.entities_archived,
            store_relations_archived=store_counts.relations_archived,
            store_resolutions_archived=store_counts.resolutions_archived,
            graph_entities_archived=graph_entities,
            graph_relations_archived=graph_relations,
            graph_resolutions_archived=graph_resolutions,
            dry_run=dry_run,
        )
