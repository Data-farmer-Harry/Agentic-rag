from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.domain.models import KnowledgeChunk, KnowledgeDocument
from app.knowledge.document_ir import DocumentBlock, DocumentIR
from app.knowledge.knowledge_ingestion import KnowledgeIngestionService
from app.knowledge.knowledge_repository import JsonKnowledgeRepository
from app.knowledge.rechunk import KnowledgeRechunkService, RechunkManifest
from app.sources.arxiv_ocr import (
    ArxivOcrEntry,
    ArxivOcrManifest,
    ArxivOcrPage,
)


class _RecordingVectorIndex:
    def __init__(self) -> None:
        self.calls: list[tuple[KnowledgeDocument, Sequence[KnowledgeChunk]]] = []

    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        self.calls.append((document, chunks))

    async def archive_document(
        self,
        document_id: object,
        *,
        tenant_id: str,
        project_id: str,
    ) -> None:
        del document_id, tenant_id, project_id


@pytest.mark.asyncio
async def test_rechunk_is_resumable_and_replaces_retained_chunks(tmp_path: Path) -> None:
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    content = b"legacy retained source"
    source_hash = hashlib.sha256(content).hexdigest()
    ingested = await KnowledgeIngestionService(repository).ingest(
        filename="paper.txt",
        content=content,
        media_type="text/plain",
        project_id="computer-science",
    )
    document = ingested.document
    assert document.parser_version == "knowledge-v3"

    ocr_root = tmp_path / "ocr"
    ir_root = ocr_root / "ir"
    ir_root.mkdir(parents=True)
    document_ir = DocumentIR(
        parser_revision="document-ir-pdf-v1",
        source_hash=source_hash,
        title="Agent Memory",
        blocks=(
            DocumentBlock(
                block_id="title",
                kind="title",
                text="Agent Memory",
                order=0,
                page_number=1,
                heading_level=1,
                heading_path=("Agent Memory",),
                char_start=0,
                char_end=12,
                extraction_method="native_text",
            ),
            DocumentBlock(
                block_id="method",
                kind="heading",
                text="1 Method",
                order=1,
                page_number=1,
                heading_level=2,
                heading_path=("Agent Memory", "1 Method"),
                char_start=13,
                char_end=21,
                extraction_method="native_text",
            ),
            DocumentBlock(
                block_id="body",
                kind="paragraph",
                text=(
                    "The memory agent retrieves durable evidence and preserves "
                    "section provenance."
                ),
                order=2,
                page_number=1,
                heading_path=("Agent Memory", "1 Method"),
                char_start=22,
                char_end=98,
                extraction_method="native_text",
            ),
        ),
        metadata={"page_count": 1},
    )
    ir_root.joinpath("fixture.json").write_text(
        document_ir.model_dump_json(indent=2),
        encoding="utf-8",
    )
    ocr_manifest = ArxivOcrManifest(
        extractor_revision="fixture-ocr-v1",
        min_text_chars=80,
        render_dpi=180,
        entries={
            "fixture": ArxivOcrEntry(
                arxiv_id="2607.00001",
                version=1,
                title="Agent Memory",
                source_pdf_path="pdfs/fixture.pdf",
                source_content_hash=source_hash,
                extractor_revision="fixture-ocr-v1",
                output_path="texts/fixture.md",
                document_ir_path="ir/fixture.json",
                status="completed",
                page_count=1,
                pdf_text_pages=1,
                gpt_ocr_pages=0,
                unresolved_low_text_pages=0,
                char_count=98,
                pages=(
                    ArxivOcrPage(
                        page_number=1,
                        method="pdf_text",
                        char_count=98,
                    ),
                ),
            )
        },
    )
    ocr_root.joinpath("manifest.json").write_text(
        ocr_manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    vector_index = _RecordingVectorIndex()
    checkpoint = tmp_path / "rechunk.json"
    service = KnowledgeRechunkService(
        repository,
        ocr_root=ocr_root,
        checkpoint_path=checkpoint,
        vector_index=vector_index,
    )

    first = await service.run(project_id="computer-science")
    second = await service.run(project_id="computer-science")

    assert first.documents_completed == 1
    assert first.old_chunks_replaced == document.chunk_count
    assert first.new_chunks_written == 1
    assert second.documents_selected == 0
    assert second.documents_skipped == 1
    assert len(vector_index.calls) == 1
    retained = await repository.get_document(
        document.document_id,
        project_id="computer-science",
    )
    assert retained is not None
    assert retained.parser_version.startswith(
        "knowledge-v3+document-ir-pdf-v1+hierarchical-token-chunker-v2"
    )
    chunks = await repository.list_chunks(
        document.document_id,
        project_id="computer-science",
    )
    assert chunks[0].metadata["heading_path"][-1] == "1 Method"
    assert chunks[0].metadata["chunk_strategy"] == "structure_first_token_aware"
    assert chunks[0].metadata["token_count"] <= 400
    manifest = RechunkManifest.model_validate_json(
        checkpoint.read_text(encoding="utf-8")
    )
    assert manifest.entries[str(document.document_id)].status == "completed"
