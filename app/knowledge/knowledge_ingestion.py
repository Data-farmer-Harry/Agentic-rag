from __future__ import annotations

import asyncio
import hashlib
import json
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from langchain_text_splitters import RecursiveCharacterTextSplitter
from PIL import Image, UnidentifiedImageError

from app.domain.contracts import (
    KnowledgeGraphIndexPort,
    KnowledgeIndexPort,
    KnowledgeRepository,
    KnowledgeVectorIndexPort,
    VisionAnalyzerPort,
)
from app.domain.enums import DocumentStatus
from app.domain.models import (
    IngestionResult,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSource,
    VisionAnalysis,
)
from app.knowledge.document_ir import (
    DocumentIR,
    DocumentParseError,
    parse_markdown_document_ir,
    parse_pdf_document_ir,
)
from app.knowledge.hierarchical_chunking import HierarchicalDocumentChunker
from app.knowledge.knowledge_visibility import (
    WorkspaceProfileResolver,
    visibility_metadata,
)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_IMAGE_FORMATS = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
}
_IMAGE_MEDIA_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_ALLOWED_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".html",
    ".htm",
    ".pdf",
    *_IMAGE_SUFFIXES,
}
_DISALLOWED_TEXT_CONTROLS = str.maketrans(
    {codepoint: None for codepoint in range(32) if codepoint not in {9, 10, 13}}
)


class KnowledgeIngestionError(ValueError):
    pass


class KnowledgeIndexError(RuntimeError):
    pass


def _same_source_identity(existing: KnowledgeSource, incoming: KnowledgeSource) -> bool:
    return (
        existing.source_type == incoming.source_type
        and existing.source_id == incoming.source_id
    )


@dataclass(frozen=True, slots=True)
class _Section:
    text: str
    page_number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _ImageInfo:
    media_type: str
    width: int
    height: int
    mode: str


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif tag.casefold() in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._skip_depth = max(0, self._skip_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(line.strip() for line in " ".join(self.parts).splitlines() if line.strip())


class KnowledgeIngestionService:
    """Parse, split, deduplicate, and persist user-provided knowledge files."""

    PARSER_VERSION = "knowledge-v3"

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        max_file_bytes: int = 10_000_000,
        chunk_size: int = 1_600,
        chunk_overlap: int = 180,
        max_pdf_pages: int = 500,
        max_extracted_chars: int = 5_000_000,
        max_chunks: int = 10_000,
        max_image_pixels: int = 40_000_000,
        max_image_dimension: int = 12_000,
        vision_analyzer: VisionAnalyzerPort | None = None,
        vector_index: KnowledgeVectorIndexPort | None = None,
        graph_index: KnowledgeGraphIndexPort | None = None,
        workspace_profiles: WorkspaceProfileResolver | None = None,
    ) -> None:
        if max_file_bytes < 1_024:
            raise ValueError("max_file_bytes must be at least 1024")
        if not 200 <= chunk_size <= 20_000:
            raise ValueError("chunk_size must be between 200 and 20000")
        if not 0 <= chunk_overlap < chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        if max_pdf_pages < 1 or max_extracted_chars < 10_000 or max_chunks < 1:
            raise ValueError("Knowledge extraction budgets must be positive")
        if max_image_pixels < 1_000_000 or max_image_dimension < 512:
            raise ValueError("Image extraction budgets are too small")
        self._repository = repository
        self._max_file_bytes = max_file_bytes
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", "。", ". ", " ", ""],
        )
        self._document_chunker = HierarchicalDocumentChunker(
            target_tokens=max(50, round(chunk_size / 4)),
            overlap_tokens=max(0, round(chunk_overlap / 4)),
            max_chunks=max_chunks,
        )
        self._chunk_overlap = chunk_overlap
        self._max_pdf_pages = max_pdf_pages
        self._max_extracted_chars = max_extracted_chars
        self._max_chunks = max_chunks
        self._max_image_pixels = max_image_pixels
        self._max_image_dimension = max_image_dimension
        self._vision_analyzer = vision_analyzer
        self._workspace_profiles = workspace_profiles
        self._indexes: tuple[KnowledgeIndexPort, ...] = tuple(
            index for index in (vector_index, graph_index) if index is not None
        )

    async def ingest(
        self,
        *,
        filename: str,
        content: bytes,
        media_type: str | None,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        source: KnowledgeSource | None = None,
    ) -> IngestionResult:
        safe_name = self.validate_submission(filename, content)
        suffix = Path(safe_name).suffix.casefold()

        content_hash = hashlib.sha256(content).hexdigest()
        duplicate = await self._repository.find_by_hash(
            content_hash,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if duplicate is not None:
            enriched = duplicate
            if source is not None and _same_source_identity(duplicate.source, source):
                stable_source = source.model_copy(
                    update={"acquired_at": duplicate.source.acquired_at}
                )
                updated = await self._repository.enrich_source(
                    duplicate.document_id,
                    stable_source,
                    title=source.title,
                    metadata=self._visibility_metadata(
                        source=stable_source,
                        tenant_id=tenant_id,
                        project_id=project_id,
                        user_id=duplicate.user_id,
                    ),
                    tenant_id=tenant_id,
                    project_id=project_id,
                )
                if updated is not None:
                    enriched = updated
            if self._indexes:
                chunks = await self._repository.list_chunks(
                    enriched.document_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                )
                await self._index_or_fail(enriched, chunks)
            return IngestionResult(document=enriched, deduplicated=True)

        title = source.title if source is not None and source.title else Path(safe_name).stem
        resolved_media_type = (
            media_type
            if media_type and media_type != "application/octet-stream"
            else self._default_media_type(suffix)
        )
        document_metadata: dict[str, Any] = {"suffix": suffix}
        parser_version = self.PARSER_VERSION
        document_ir: DocumentIR | None = None
        if suffix in _IMAGE_SUFFIXES:
            image_info = await asyncio.to_thread(self._inspect_image, content, suffix)
            if self._vision_analyzer is None:
                raise KnowledgeIngestionError(
                    "Image ingestion requires an enabled Vision analyzer"
                )
            analysis = await self._vision_analyzer.analyze(
                content,
                media_type=image_info.media_type,
                filename=safe_name,
            )
            sections = self._vision_sections(analysis)
            title = _sanitize_extracted_text(analysis.title).strip() or title
            resolved_media_type = image_info.media_type
            parser_version = f"{self.PARSER_VERSION}+{self._vision_analyzer.revision}"
            document_metadata.update(
                {
                    "modality": "image",
                    "image_width": image_info.width,
                    "image_height": image_info.height,
                    "image_mode": image_info.mode,
                    "visual_region_count": len(analysis.regions),
                    "vision_warnings": analysis.warnings,
                }
            )
        else:
            if suffix in {".pdf", ".md", ".markdown"}:
                document_ir = await asyncio.to_thread(
                    self._parse_document_ir,
                    content,
                    suffix,
                    content_hash,
                )
                sections = []
                if source is None and document_ir.title:
                    title = document_ir.title
                parser_version = (
                    f"{self.PARSER_VERSION}+{document_ir.parser_revision}"
                    f"+{self._document_chunker.revision}"
                )
                document_metadata.update(
                    {
                        "document_ir_schema": document_ir.schema_version,
                        "document_ir_parser_revision": document_ir.parser_revision,
                        "document_ir_block_count": len(document_ir.blocks),
                        "document_ir_metadata": document_ir.metadata,
                        "chunk_strategy": "structure_first_token_aware",
                    }
                )
            else:
                sections = await asyncio.to_thread(self._parse, content, suffix)
        document_id = uuid5(
            NAMESPACE_URL,
            f"hermesgraph:document:{tenant_id}:{project_id}:{content_hash}",
        )
        resolved_source = source or KnowledgeSource(
            source_id=f"document:{document_id}",
        )
        document_metadata.update(
            self._visibility_metadata(
                source=resolved_source,
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=user_id,
            )
        )
        scope_hash = hashlib.sha256(f"{tenant_id}\0{project_id}".encode()).hexdigest()[:24]
        storage_key = str(Path("uploads") / scope_hash / str(document_id) / safe_name)
        try:
            chunks = (
                self._document_chunker.chunk(
                    document_ir,
                    document_id=document_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    filename=safe_name,
                    title=title,
                )
                if document_ir is not None
                else self._split_sections(
                    sections,
                    document_id=document_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    filename=safe_name,
                )
            )
        except ValueError as exc:
            raise KnowledgeIngestionError(str(exc)) from exc
        if not chunks:
            raise KnowledgeIngestionError("No extractable text was found in the uploaded file")
        document = KnowledgeDocument(
            document_id=document_id,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            filename=safe_name,
            title=title,
            media_type=resolved_media_type,
            byte_size=len(content),
            content_hash=content_hash,
            storage_key=storage_key,
            chunk_count=len(chunks),
            parser_version=parser_version,
            source=resolved_source,
            metadata=document_metadata,
        )
        stored, deduplicated = await self._repository.ingest(document, chunks, content)
        if self._indexes:
            await self._index_or_fail(stored, chunks)
        return IngestionResult(document=stored, deduplicated=deduplicated)

    def _visibility_metadata(
        self,
        *,
        source: KnowledgeSource,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> dict[str, str]:
        profile = (
            self._workspace_profiles.resolve(
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if self._workspace_profiles is not None
            else None
        )
        return visibility_metadata(
            source=source,
            user_id=user_id,
            workspace_mode=profile.workspace_mode if profile is not None else None,
        )

    def validate_submission(self, filename: str, content: bytes) -> str:
        safe_name = self._safe_filename(filename)
        if not content:
            raise KnowledgeIngestionError("Uploaded file is empty")
        if len(content) > self._max_file_bytes:
            raise KnowledgeIngestionError(
                f"Uploaded file exceeds the {self._max_file_bytes} byte limit"
            )
        suffix = Path(safe_name).suffix.casefold()
        if suffix not in _ALLOWED_SUFFIXES:
            raise KnowledgeIngestionError(
                f"Unsupported file type {suffix or '<none>'}; allowed: "
                + ", ".join(sorted(_ALLOWED_SUFFIXES))
            )
        return safe_name

    async def archive(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> bool:
        document = await self._repository.get_document(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if document is None or document.status == DocumentStatus.ARCHIVED:
            return False
        if self._indexes:
            results = await asyncio.gather(
                *(
                    index.archive_document(
                        document_id,
                        tenant_id=tenant_id,
                        project_id=project_id,
                    )
                    for index in self._indexes
                ),
                return_exceptions=True,
            )
            if any(isinstance(result, BaseException) for result in results):
                raise KnowledgeIndexError("Unable to archive all knowledge indexes")
        return await self._repository.archive(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def _index_or_fail(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        if not self._indexes:
            return
        results = await asyncio.gather(
            *(index.index_document(document, chunks) for index in self._indexes),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            await asyncio.gather(
                *(
                    index.archive_document(
                        document.document_id,
                        tenant_id=document.tenant_id,
                        project_id=document.project_id,
                    )
                    for index in self._indexes
                ),
                return_exceptions=True,
            )
            await self._repository.set_status(
                document.document_id,
                DocumentStatus.FAILED,
                tenant_id=document.tenant_id,
                project_id=document.project_id,
                error="knowledge_index_failed",
            )
            raise KnowledgeIndexError("Unable to index all document knowledge") from failures[0]

    def _parse(self, content: bytes, suffix: str) -> list[_Section]:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise KnowledgeIngestionError("Text files must use UTF-8 encoding") from exc
        if suffix == ".json":
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except json.JSONDecodeError as exc:
                raise KnowledgeIngestionError("Uploaded JSON is malformed") from exc
        elif suffix in {".html", ".htm"}:
            parser = _VisibleTextParser()
            parser.feed(text)
            text = parser.text()
        text = _sanitize_extracted_text(text)
        if len(text) > self._max_extracted_chars:
            raise KnowledgeIngestionError(
                "Extracted text exceeds the configured character limit"
            )
        return [_Section(text=text.strip())] if text.strip() else []

    def _parse_document_ir(
        self,
        content: bytes,
        suffix: str,
        content_hash: str,
    ) -> DocumentIR:
        try:
            if suffix == ".pdf":
                return parse_pdf_document_ir(
                    content,
                    source_hash=content_hash,
                    max_pages=self._max_pdf_pages,
                    max_extracted_chars=self._max_extracted_chars,
                )
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise KnowledgeIngestionError(
                    "Text files must use UTF-8 encoding"
                ) from exc
            return parse_markdown_document_ir(
                text,
                source_hash=content_hash,
                max_extracted_chars=self._max_extracted_chars,
            )
        except DocumentParseError as exc:
            raise KnowledgeIngestionError(str(exc)) from exc

    def _inspect_image(self, content: bytes, suffix: str) -> _ImageInfo:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as image:
                    image_format = (image.format or "").upper()
                    width, height = image.size
                    mode = image.mode
                    frame_count = int(getattr(image, "n_frames", 1))
                    if image_format != _IMAGE_FORMATS[suffix]:
                        raise KnowledgeIngestionError(
                            "Image content does not match its filename extension"
                        )
                    if frame_count != 1:
                        raise KnowledgeIngestionError("Animated images are not supported")
                    if width < 1 or height < 1:
                        raise KnowledgeIngestionError("Image dimensions are invalid")
                    if max(width, height) > self._max_image_dimension:
                        raise KnowledgeIngestionError(
                            f"Image exceeds the {self._max_image_dimension} pixel dimension limit"
                        )
                    if width * height > self._max_image_pixels:
                        raise KnowledgeIngestionError(
                            f"Image exceeds the {self._max_image_pixels} pixel budget"
                        )
                    image.verify()
                with Image.open(BytesIO(content)) as decoded:
                    decoded.load()
        except KnowledgeIngestionError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise KnowledgeIngestionError("Image exceeds safe decompression limits") from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise KnowledgeIngestionError("Unable to decode image content") from exc
        return _ImageInfo(
            media_type=_IMAGE_MEDIA_TYPES[image_format],
            width=width,
            height=height,
            mode=mode,
        )

    def _vision_sections(self, analysis: VisionAnalysis) -> list[_Section]:
        overview_parts = [
            f"Image title: {analysis.title}",
            f"Visual summary: {analysis.summary}",
        ]
        if analysis.visible_text.strip():
            overview_parts.append(f"Visible text:\n{analysis.visible_text.strip()}")
        sections = [
            _Section(
                text="\n\n".join(overview_parts),
                metadata={"modality": "image", "visual_kind": "overview"},
            )
        ]
        for index, region in enumerate(analysis.regions, start=1):
            region_id = f"region-{index:02d}"
            parts = [
                f"Visual region {region_id}: {region.label}",
                f"Category: {region.category}",
                f"Description: {region.description}",
            ]
            if region.visible_text.strip():
                parts.append(f"Visible text:\n{region.visible_text.strip()}")
            metadata: dict[str, Any] = {
                "modality": "image",
                "visual_kind": "region",
                "visual_region_id": region_id,
                "visual_category": region.category,
                "vision_confidence": region.confidence,
            }
            if region.bounding_box is not None:
                metadata["visual_bbox"] = region.bounding_box.as_list()
            sections.append(_Section(text="\n".join(parts), metadata=metadata))
        extracted_chars = sum(len(section.text) for section in sections)
        if extracted_chars > self._max_extracted_chars:
            raise KnowledgeIngestionError(
                "Vision extracted text exceeds the configured character limit"
            )
        return sections

    def _split_sections(
        self,
        sections: list[_Section],
        *,
        document_id: UUID,
        tenant_id: str,
        project_id: str,
        filename: str,
    ) -> list[KnowledgeChunk]:
        chunks: list[KnowledgeChunk] = []
        global_index = 0
        for section in sections:
            cursor = 0
            for text in self._splitter.split_text(section.text):
                search_start = max(0, cursor - self._chunk_overlap)
                char_start = section.text.find(text, search_start)
                if char_start < 0:
                    char_start = cursor
                char_end = char_start + len(text)
                chunk_hash = hashlib.sha256(text.encode()).hexdigest()
                chunk_id = uuid5(
                    NAMESPACE_URL,
                    f"hermesgraph:chunk:{document_id}:{global_index}:{chunk_hash}",
                )
                chunks.append(
                    KnowledgeChunk(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        tenant_id=tenant_id,
                        project_id=project_id,
                        chunk_index=global_index,
                        text=text,
                        content_hash=chunk_hash,
                        page_number=section.page_number,
                        char_start=char_start,
                        char_end=char_end,
                        metadata={"filename": filename, **section.metadata},
                    )
                )
                if len(chunks) > self._max_chunks:
                    raise KnowledgeIngestionError(
                        f"Document exceeds the {self._max_chunks} chunk limit"
                    )
                cursor = char_end
                global_index += 1
        return chunks

    @staticmethod
    def _safe_filename(filename: str) -> str:
        safe = Path(filename.replace("\0", "")).name.strip()
        if not safe or safe in {".", ".."}:
            raise KnowledgeIngestionError("A valid filename is required")
        return safe[:255]

    @staticmethod
    def _default_media_type(suffix: str) -> str:
        return {
            ".pdf": "application/pdf",
            ".json": "application/json",
            ".yaml": "application/yaml",
            ".yml": "application/yaml",
            ".csv": "text/csv",
            ".html": "text/html",
            ".htm": "text/html",
            ".md": "text/markdown",
            ".markdown": "text/markdown",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "text/plain")


def _sanitize_extracted_text(text: str) -> str:
    return text.translate(_DISALLOWED_TEXT_CONTROLS)
