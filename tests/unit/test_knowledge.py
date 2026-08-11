from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.enums import DocumentStatus, TrustLevel
from app.domain.models import KnowledgeChunk, KnowledgeDocument, KnowledgeSource
from app.knowledge.knowledge_ingestion import KnowledgeIngestionError, KnowledgeIngestionService
from app.knowledge.knowledge_repository import JsonKnowledgeRepository


class _RecordingGraphIndex:
    def __init__(self) -> None:
        self.indexed: list[UUID] = []
        self.archived: list[UUID] = []

    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        assert chunks
        self.indexed.append(document.document_id)

    async def archive_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> None:
        assert tenant_id == "local"
        assert project_id == "default"
        self.archived.append(document_id)


@pytest.mark.asyncio
async def test_ingestion_dedup_scope_search_and_archive(tmp_path: Path) -> None:
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    service = KnowledgeIngestionService(
        repository,
        max_file_bytes=10_000,
        chunk_size=200,
        chunk_overlap=20,
    )
    content = (
        b"# Private launch note\n\n"
        b"The NEBULA-CROWN-7429 protocol requires two independent approvals."
    )

    first = await service.ingest(
        filename="../launch.md",
        content=content,
        media_type="application/octet-stream",
    )
    duplicate = await service.ingest(
        filename="renamed.md",
        content=content,
        media_type="text/markdown",
    )
    other_project = await service.ingest(
        filename="launch.md",
        content=content,
        media_type="text/markdown",
        project_id="other",
    )

    assert first.document.filename == "launch.md"
    assert first.document.media_type == "text/markdown"
    assert first.document.chunk_count == 1
    assert duplicate.deduplicated is True
    assert duplicate.document.document_id == first.document.document_id
    assert other_project.deduplicated is False
    assert other_project.document.document_id != first.document.document_id

    matches = await repository.search(
        "NEBULA-CROWN-7429",
        tenant_id="local",
        project_id="default",
        filters={"tenant_id": "local", "project_id": "default"},
    )
    assert len(matches) == 1
    assert matches[0].provenance.source_type == "uploaded_document"
    assert matches[0].provenance.trust == TrustLevel.USER_ASSERTED
    assert matches[0].metadata["filename"] == "launch.md"

    assert await repository.archive(first.document.document_id) is True
    archived = await repository.get_document(first.document.document_id)
    assert archived is not None
    assert archived.status == DocumentStatus.ARCHIVED
    assert await repository.search(
        "NEBULA-CROWN-7429",
        tenant_id="local",
        project_id="default",
    ) == []
    assert len(await repository.list_documents()) == 0
    assert len(await repository.list_documents(include_archived=True)) == 1
    assert await repository.chunk_count() == 0


@pytest.mark.asyncio
async def test_ingestion_coordinates_graph_index_and_archive(tmp_path: Path) -> None:
    graph = _RecordingGraphIndex()
    service = KnowledgeIngestionService(
        JsonKnowledgeRepository(tmp_path / "knowledge"),
        graph_index=graph,
    )

    result = await service.ingest(
        filename="graph.md",
        content=b"AURORA-GRAPH-301 requires a verified source.",
        media_type="text/markdown",
    )

    assert graph.indexed == [result.document.document_id]
    assert await service.archive(result.document.document_id) is True
    assert graph.archived == [result.document.document_id]


@pytest.mark.asyncio
async def test_public_source_contract_survives_storage_and_retrieval(tmp_path: Path) -> None:
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    service = KnowledgeIngestionService(repository)
    source = KnowledgeSource(
        source_type="arxiv",
        source_id="arxiv:2607.12764",
        title="EvoGraph-R1",
        source_revision="v1",
        canonical_uri="https://arxiv.org/abs/2607.12764v1",
        privacy="public_reference",
        trust=TrustLevel.OBSERVED,
    )

    result = await service.ingest(
        filename="2607.12764v1.md",
        content=b"EvoGraph-R1 studies self-evolving multimodal knowledge hypergraphs.",
        media_type="text/markdown",
        source=source,
    )
    stored = await repository.get_document(result.document.document_id)
    evidence = await repository.search(
        "EvoGraph-R1",
        tenant_id="local",
        project_id="default",
    )

    assert stored is not None
    assert stored.source == source
    assert stored.title == "EvoGraph-R1"
    assert evidence[0].provenance.source_type == "arxiv"
    assert evidence[0].provenance.source_id == "arxiv:2607.12764#chunk=0"
    assert evidence[0].provenance.trust == TrustLevel.OBSERVED
    assert evidence[0].metadata["privacy"] == "public_reference"
    assert evidence[0].metadata["canonical_uri"] == source.canonical_uri

    enriched_source = source.model_copy(update={"title": "EvoGraph-R1 Updated"})
    duplicate = await service.ingest(
        filename="renamed.md",
        content=b"EvoGraph-R1 studies self-evolving multimodal knowledge hypergraphs.",
        media_type="text/markdown",
        source=enriched_source,
    )
    assert duplicate.deduplicated is True
    assert duplicate.document.title == "EvoGraph-R1 Updated"
    assert duplicate.document.source.title == "EvoGraph-R1 Updated"
    assert duplicate.document.source.acquired_at == source.acquired_at


@pytest.mark.asyncio
async def test_html_ignores_executable_content(tmp_path: Path) -> None:
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    service = KnowledgeIngestionService(repository, chunk_size=200, chunk_overlap=20)

    result = await service.ingest(
        filename="page.html",
        content=(
            b"<h1>Visible policy</h1><p>ORBIT-VISIBLE-991 is approved.</p>"
            b"<script>HIDDEN-SCRIPT-449 shouldNotBeIndexed()</script>"
        ),
        media_type="text/html",
    )

    assert result.document.chunk_count == 1
    assert await repository.search(
        "ORBIT-VISIBLE-991",
        tenant_id="local",
        project_id="default",
    )
    assert await repository.search(
        "HIDDEN-SCRIPT-449",
        tenant_id="local",
        project_id="default",
    ) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "expected"),
    [
        ("payload.exe", b"not allowed", "Unsupported file type"),
        ("payload.json", b"{broken", "malformed"),
        ("empty.txt", b"", "empty"),
    ],
)
async def test_ingestion_rejects_invalid_input(
    tmp_path: Path,
    filename: str,
    content: bytes,
    expected: str,
) -> None:
    service = KnowledgeIngestionService(JsonKnowledgeRepository(tmp_path / "knowledge"))

    with pytest.raises(KnowledgeIngestionError, match=expected):
        await service.ingest(filename=filename, content=content, media_type=None)


@pytest.mark.asyncio
async def test_ingestion_enforces_extracted_text_budget(tmp_path: Path) -> None:
    service = KnowledgeIngestionService(
        JsonKnowledgeRepository(tmp_path / "knowledge"),
        max_file_bytes=20_000,
        max_extracted_chars=10_000,
    )

    with pytest.raises(KnowledgeIngestionError, match="character limit"):
        await service.ingest(
            filename="oversized.txt",
            content=b"x" * 10_001,
            media_type="text/plain",
        )


@pytest.mark.asyncio
async def test_ingestion_removes_database_invalid_text_controls(tmp_path: Path) -> None:
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    service = KnowledgeIngestionService(repository)

    result = await service.ingest(
        filename="controls.txt",
        content=b"Graph\x00RAG\x01 keeps\tmeaningful whitespace.",
        media_type="text/plain",
    )
    chunks = await repository.list_chunks(result.document.document_id)

    assert chunks[0].text == "GraphRAG keeps\tmeaningful whitespace."
