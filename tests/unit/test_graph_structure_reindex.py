from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.models import KnowledgeChunk, KnowledgeDocument
from app.graph.structure_reindex import GraphStructureReindexService
from app.knowledge.knowledge_ingestion import KnowledgeIngestionService
from app.knowledge.knowledge_repository import JsonKnowledgeRepository


class _RecordingGraph:
    def __init__(self) -> None:
        self.indexed: list[tuple[UUID, int]] = []

    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        self.indexed.append((document.document_id, len(chunks)))

    async def archive_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> None:
        raise AssertionError("Structural reindex must not archive an active document")


@pytest.mark.asyncio
async def test_graph_structure_reindex_is_bounded_and_resumable(
    tmp_path: Path,
) -> None:
    knowledge = JsonKnowledgeRepository(tmp_path / "knowledge")
    ingestion = KnowledgeIngestionService(knowledge)
    for filename, title in (("b.md", "Beta"), ("a.md", "Alpha")):
        await ingestion.ingest(
            filename=filename,
            content=f"# {title}\n\n{title} uses GraphRAG.".encode(),
            media_type="text/markdown",
            project_id="computer-science",
        )
    graph = _RecordingGraph()
    service = GraphStructureReindexService(
        knowledge,
        graph,
        checkpoint_path=tmp_path / "structure.json",
    )

    first = await service.run(limit=1)
    second = await service.run()
    dry_run = await service.run(dry_run=True)

    assert first.documents_completed == 1
    assert second.documents_completed == 1
    assert second.documents_skipped == 1
    assert dry_run.documents_selected == 0
    assert dry_run.documents_skipped == 2
    assert len(graph.indexed) == 2
    assert sum(count for _, count in graph.indexed) == (
        first.chunks_projected + second.chunks_projected
    )
