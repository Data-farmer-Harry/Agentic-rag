from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from app.domain.models import GraphExtractionBatch, KnowledgeChunk, KnowledgeDocument
from app.graph.backfill import GraphBackfillProgress, GraphBackfillService
from app.graph.candidate_store import JsonGraphCandidateRepository
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.store import JsonKnowledgeRepository


class _RecordingEnricher:
    def __init__(
        self,
        candidates: JsonGraphCandidateRepository,
        revision: str,
    ) -> None:
        self._candidates = candidates
        self._revision = revision
        self.calls: list[str] = []

    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        assert chunks
        self.calls.append(str(document.document_id))
        await self._candidates.save_batch(
            GraphExtractionBatch(
                document_id=document.document_id,
                tenant_id=document.tenant_id,
                project_id=document.project_id,
                extractor_revision=self._revision,
            )
        )


class _RecordingStructuralIndex:
    def __init__(self) -> None:
        self.indexed: list[str] = []

    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        assert chunks
        self.indexed.append(str(document.document_id))

    async def archive_document(
        self,
        document_id,  # type: ignore[no-untyped-def]
        *,
        tenant_id: str,
        project_id: str,
    ) -> None:
        raise AssertionError("Backfill must not archive an active document")


class _FailingEnricher(_RecordingEnricher):
    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        self.calls.append(str(document.document_id))
        raise RuntimeError("simulated extraction failure")


@pytest.mark.asyncio
async def test_graph_backfill_is_bounded_resumable_and_revision_aware(
    tmp_path: Path,
) -> None:
    knowledge = JsonKnowledgeRepository(tmp_path / "knowledge")
    ingestion = KnowledgeIngestionService(knowledge)
    for filename, title in (("b.md", "Beta"), ("a.md", "Alpha")):
        await ingestion.ingest(
            filename=filename,
            content=f"# {title}\n\n{title} uses GraphRAG.".encode(),
            media_type="text/markdown",
        )
    candidates = JsonGraphCandidateRepository(tmp_path / "candidates.json")
    checkpoint = tmp_path / "backfill.json"
    structural = _RecordingStructuralIndex()
    enricher_v1 = _RecordingEnricher(candidates, "openai-graph-v1")
    service_v1 = GraphBackfillService(
        knowledge,
        candidates,
        enricher_v1,
        extractor_revision="openai-graph-v1",
        checkpoint_path=checkpoint,
        structural_index=structural,
    )

    first = await service_v1.run(limit=1)
    second = await service_v1.run(limit=1)
    third = await service_v1.run()

    assert first.documents_selected == 1
    assert first.documents_completed == 1
    assert second.documents_selected == 1
    assert second.documents_skipped == 1
    assert third.documents_selected == 0
    assert third.documents_skipped == 2
    assert len(enricher_v1.calls) == 2
    assert structural.indexed == enricher_v1.calls

    enricher_v2 = _RecordingEnricher(candidates, "openai-graph-v2")
    service_v2 = GraphBackfillService(
        knowledge,
        candidates,
        enricher_v2,
        extractor_revision="openai-graph-v2",
        checkpoint_path=checkpoint,
    )
    dry_run = await service_v2.run(limit=1, dry_run=True, concurrency=2)

    assert dry_run.documents_selected == 1
    assert dry_run.documents_skipped == 0
    assert enricher_v2.calls == []

    progress: list[GraphBackfillProgress] = []
    concurrent = await service_v2.run(
        limit=2,
        concurrency=2,
        progress_callback=progress.append,
    )
    assert concurrent.documents_completed == 2
    assert len(enricher_v2.calls) == 2
    assert [item.processed for item in progress] == [0, 1, 2]
    assert progress[-1].completed == 2
    assert progress[-1].failed == 0


@pytest.mark.asyncio
async def test_graph_backfill_accepts_twelve_workers_and_rejects_more(
    tmp_path: Path,
) -> None:
    knowledge = JsonKnowledgeRepository(tmp_path / "knowledge")
    candidates = JsonGraphCandidateRepository(tmp_path / "candidates.json")
    service = GraphBackfillService(
        knowledge,
        candidates,
        _RecordingEnricher(candidates, "openai-graph-v1"),
        extractor_revision="openai-graph-v1",
        checkpoint_path=tmp_path / "backfill.json",
    )

    summary = await service.run(dry_run=True, concurrency=12)

    assert summary.documents_selected == 0
    with pytest.raises(ValueError, match="between 1 and 12"):
        await service.run(dry_run=True, concurrency=13)


@pytest.mark.asyncio
async def test_graph_backfill_can_skip_current_error_checkpoints(
    tmp_path: Path,
) -> None:
    knowledge = JsonKnowledgeRepository(tmp_path / "knowledge")
    ingestion = KnowledgeIngestionService(knowledge)
    for filename, title in (("a.md", "Alpha"), ("b.md", "Beta")):
        await ingestion.ingest(
            filename=filename,
            content=f"# {title}\n\n{title} uses GraphRAG.".encode(),
            media_type="text/markdown",
        )
    candidates = JsonGraphCandidateRepository(tmp_path / "candidates.json")
    checkpoint = tmp_path / "backfill.json"
    failing = _FailingEnricher(candidates, "openai-graph-v1")
    service = GraphBackfillService(
        knowledge,
        candidates,
        failing,
        extractor_revision="openai-graph-v1",
        checkpoint_path=checkpoint,
    )

    first = await service.run(limit=1)
    second = await service.run(limit=1, skip_errors=True)

    assert first.documents_failed == 1
    assert second.documents_selected == 1
    assert second.documents_skipped == 1
    assert second.documents_failed == 1
    assert len(set(failing.calls)) == 2
