from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pypdf import PdfWriter

from app.knowledge.document_ir import DocumentIR
from app.sources.arxiv import ArxivManifest, ArxivManifestEntry, ArxivPaper
from app.sources.arxiv_ocr import (
    ArxivOcrProcessor,
    OcrPageAnalysis,
    _merge_page_text,
    _visible_char_count,
)

_PNG = b"\x89PNG\r\n\x1a\nfixture"


class _FakePageOcr:
    revision = "gpt-ocr-fixture-v1"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def transcribe(
        self,
        content: bytes,
        *,
        media_type: str,
        filename: str,
    ) -> OcrPageAnalysis:
        assert content == _PNG
        assert media_type == "image/png"
        self.calls.append(filename)
        return OcrPageAnalysis(
            text="Figure 1: Agent memory architecture",
            confidence=0.97,
        )

    async def close(self) -> None:
        return None


def _paper() -> ArxivPaper:
    now = datetime(2026, 7, 17, tzinfo=UTC)
    return ArxivPaper(
        arxiv_id="2607.00001",
        version=1,
        title="Agent memory fixture",
        published=now,
        updated=now,
        abstract_url="https://arxiv.org/abs/2607.00001v1",
        pdf_url="https://arxiv.org/pdf/2607.00001v1",
    )


@pytest.mark.asyncio
async def test_arxiv_ocr_uses_gpt_for_low_text_pages_and_resumes(tmp_path: Path) -> None:
    source_root = tmp_path / "arxiv"
    pdf_root = source_root / "pdfs"
    pdf_root.mkdir(parents=True)
    pdf_path = pdf_root / "2607.00001v1.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with pdf_path.open("wb") as handle:
        writer.write(handle)
    content_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    source_manifest = ArxivManifest(
        entries={
            "2607.00001v1": ArxivManifestEntry(
                paper=_paper(),
                status="downloaded",
                pdf_path="pdfs/2607.00001v1.pdf",
                content_hash=content_hash,
                byte_size=pdf_path.stat().st_size,
            )
        }
    )
    source_root.joinpath("manifest.json").write_text(
        source_manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    page_ocr = _FakePageOcr()

    async def render(_: Path, page_number: int, dpi: int) -> bytes:
        assert page_number == 1
        assert dpi == 180
        return _PNG

    processor = ArxivOcrProcessor(
        source_root=source_root,
        output_root=source_root / "ocr",
        page_ocr=page_ocr,
        page_renderer=render,
    )
    first = await processor.run()
    second = await processor.run()
    output = source_root / "ocr" / "texts" / "2607.00001v1.md"
    document_ir = source_root / "ocr" / "ir" / "2607.00001v1.json"

    assert first.documents_processed == 1
    assert first.gpt_ocr_pages == 1
    assert first.document_irs_written == 1
    assert first.unresolved_low_text_pages == 0
    assert second.documents_skipped == 1
    assert page_ocr.calls == ["2607.00001v1-page-1.png"]
    assert "Figure 1: Agent memory architecture" in output.read_text(encoding="utf-8")
    assert document_ir.is_file()
    retained_ir = DocumentIR.model_validate_json(document_ir.read_text(encoding="utf-8"))
    assert retained_ir.parser_revision == "document-ir-pdf-v1"
    assert retained_ir.blocks[0].extraction_method == "vision_ocr"
    assert retained_ir.metadata["vision_ocr_pages"] == 1


@pytest.mark.asyncio
async def test_text_only_ocr_defers_low_text_and_future_vision_fills_it(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "arxiv"
    pdf_root = source_root / "pdfs"
    pdf_root.mkdir(parents=True)
    pdf_path = pdf_root / "2607.00001v1.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with pdf_path.open("wb") as handle:
        writer.write(handle)
    content_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    source_manifest = ArxivManifest(
        entries={
            "2607.00001v1": ArxivManifestEntry(
                paper=_paper(),
                status="downloaded",
                pdf_path="pdfs/2607.00001v1.pdf",
                content_hash=content_hash,
                byte_size=pdf_path.stat().st_size,
            )
        }
    )
    source_root.joinpath("manifest.json").write_text(
        source_manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    render_calls: list[int] = []

    async def render(_: Path, page_number: int, dpi: int) -> bytes:
        assert dpi == 180
        render_calls.append(page_number)
        return _PNG

    output_root = source_root / "ocr"
    text_only = ArxivOcrProcessor(
        source_root=source_root,
        output_root=output_root,
        page_ocr=None,
        page_renderer=render,
    )
    deferred = await text_only.run()
    deferred_output = output_root / "texts" / "2607.00001v1.md"

    assert deferred.documents_processed == 1
    assert deferred.gpt_ocr_pages == 0
    assert deferred.unresolved_low_text_pages == 1
    assert render_calls == []
    assert "extraction: unresolved_low_text" in deferred_output.read_text(
        encoding="utf-8"
    )

    page_ocr = _FakePageOcr()
    with_vision = ArxivOcrProcessor(
        source_root=source_root,
        output_root=output_root,
        page_ocr=page_ocr,
        page_renderer=render,
    )
    completed = await with_vision.run()
    resumed = await with_vision.run()

    assert completed.documents_processed == 1
    assert completed.gpt_ocr_pages == 1
    assert completed.unresolved_low_text_pages == 0
    assert resumed.documents_skipped == 1
    assert render_calls == [1]
    assert "Figure 1: Agent memory architecture" in deferred_output.read_text(
        encoding="utf-8"
    )


def test_ocr_text_helpers_preserve_unique_fallback_text() -> None:
    assert _visible_char_count(" a \n b ") == 2
    assert _merge_page_text("Page 2", "Page 2\nAgent diagram") == "Page 2\nAgent diagram"
    assert _merge_page_text("Page\n2", "Page 2\nAgent diagram") == "Page 2\nAgent diagram"
    assert _merge_page_text("Page 2", "Agent diagram") == "Page 2\n\nAgent diagram"
