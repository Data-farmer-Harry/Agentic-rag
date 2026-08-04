from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from io import BytesIO
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pypdf import PdfReader

BlockKind = Literal[
    "title",
    "heading",
    "paragraph",
    "list_item",
    "table",
    "code",
    "equation",
    "caption",
    "reference",
]
ExtractionMethod = Literal[
    "native_text",
    "vision_ocr",
    "source_text",
    "vision_analysis",
]

_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HEADING_NUMBER_PATTERN = re.compile(
    r"^(?:(?:\d+(?:\.\d+){0,4})|(?:[IVXLC]+))[\s.:)\-]+(.+)$",
    re.IGNORECASE,
)
_MARKDOWN_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_PAGE_HEADING_PATTERN = re.compile(r"^Page\s+(\d+)$", re.IGNORECASE)
_EXTRACTION_COMMENT_PATTERN = re.compile(
    r"^<!--\s*extraction:\s*([a-z0-9_]+)\s*-->$",
    re.IGNORECASE,
)
_LIST_PATTERN = re.compile(r"^(?:[-*+]\s+|\d+[.)]\s+)")
_REFERENCE_PATTERN = re.compile(r"^\s*(?:\[\d+]|[A-Z][\w'-]+,\s+[A-Z])")
_CAPTION_PATTERN = re.compile(
    r"^(?:figure|fig\.|table|algorithm|listing)\s+\d+[\s.:]",
    re.IGNORECASE,
)
_COMMON_HEADINGS = {
    "abstract",
    "acknowledgements",
    "acknowledgments",
    "appendix",
    "background",
    "conclusion",
    "conclusions",
    "discussion",
    "evaluation",
    "experiment",
    "experiments",
    "introduction",
    "limitations",
    "method",
    "methodology",
    "methods",
    "references",
    "related work",
    "results",
}
_SKIPPED_OCR_METADATA_PREFIXES = ("- arxiv:", "- source:", "- extractor:")


class DocumentParseError(ValueError):
    pass


class PageTextLayer(BaseModel):
    """Optional page replacement produced by a governed OCR pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(default="", max_length=1_000_000)
    method: ExtractionMethod
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    warnings: tuple[str, ...] = ()


class DocumentBlock(BaseModel):
    """One reading-order element with stable provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: str = Field(min_length=1, max_length=160)
    kind: BlockKind
    text: str = Field(min_length=1, max_length=200_000)
    order: int = Field(ge=0)
    page_number: int | None = Field(default=None, ge=1)
    heading_level: int | None = Field(default=None, ge=1, le=6)
    heading_path: tuple[str, ...] = ()
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=1)
    extraction_method: ExtractionMethod
    extraction_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    bbox: tuple[float, float, float, float] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_extent(self) -> Self:
        if self.char_end <= self.char_start:
            raise ValueError("Document block character ranges must have positive extent")
        return self


class DocumentIR(BaseModel):
    """Parser-neutral document structure used by retrieval and graph extraction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    parser_revision: str = Field(min_length=1, max_length=200)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str | None = Field(default=None, min_length=1, max_length=500)
    blocks: tuple[DocumentBlock, ...]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_reading_order(self) -> Self:
        orders = [block.order for block in self.blocks]
        if orders != list(range(len(orders))):
            raise ValueError("Document IR blocks must have contiguous reading order")
        return self


def parse_pdf_document_ir(
    content: bytes,
    *,
    source_hash: str,
    max_pages: int,
    max_extracted_chars: int,
    page_layers: Mapping[int, PageTextLayer] | None = None,
) -> DocumentIR:
    """Extract a layout-lite IR while preserving a replaceable OCR page boundary."""

    try:
        reader = PdfReader(BytesIO(content))
    except Exception as exc:
        raise DocumentParseError("Unable to parse PDF content") from exc
    if len(reader.pages) > max_pages:
        raise DocumentParseError(f"PDF exceeds the {max_pages} page extraction limit")

    raw_pages: list[str] = []
    methods: list[ExtractionMethod] = []
    confidences: list[float | None] = []
    warnings: list[tuple[str, ...]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        layer = (page_layers or {}).get(page_number)
        if layer is not None:
            raw_pages.append(_normalize_text(layer.text))
            methods.append(layer.method)
            confidences.append(layer.confidence)
            warnings.append(layer.warnings)
            continue
        try:
            raw_pages.append(_normalize_text(page.extract_text() or ""))
        except Exception as exc:
            raise DocumentParseError(
                f"Unable to extract PDF page {page_number}"
            ) from exc
        methods.append("native_text")
        confidences.append(None)
        warnings.append(())

    repeated_edges = _repeated_page_edges(raw_pages)
    drafts: list[_BlockDraft] = []
    heading_stack: list[str] = []
    removed_edge_lines = 0
    for page_number, text in enumerate(raw_pages, start=1):
        page_lines = text.splitlines()
        filtered_lines: list[str] = []
        for index, line in enumerate(page_lines):
            normalized_edge = _edge_key(line)
            at_edge = index < 2 or index >= max(0, len(page_lines) - 2)
            if at_edge and normalized_edge and normalized_edge in repeated_edges:
                removed_edge_lines += 1
                continue
            filtered_lines.append(line)
        page_drafts, heading_stack = _segment_lines(
            filtered_lines,
            page_number=page_number,
            extraction_method=methods[page_number - 1],
            extraction_confidence=confidences[page_number - 1],
            extraction_warnings=warnings[page_number - 1],
            heading_stack=heading_stack,
        )
        drafts.extend(page_drafts)

    blocks = _materialize_blocks(drafts, source_hash=source_hash)
    extracted_chars = sum(len(block.text) for block in blocks)
    if extracted_chars > max_extracted_chars:
        raise DocumentParseError("PDF extracted text exceeds the configured character limit")
    title = _first_title(blocks)
    method_counts = Counter(methods)
    return DocumentIR(
        parser_revision="document-ir-pdf-v1",
        source_hash=source_hash,
        title=title,
        blocks=blocks,
        metadata={
            "page_count": len(raw_pages),
            "removed_repeated_edge_lines": removed_edge_lines,
            "extraction_method_counts": dict(sorted(method_counts.items())),
            "empty_page_count": sum(not page.strip() for page in raw_pages),
        },
    )


def parse_markdown_document_ir(
    text: str,
    *,
    source_hash: str,
    max_extracted_chars: int,
) -> DocumentIR:
    normalized = _normalize_text(text)
    if len(normalized) > max_extracted_chars:
        raise DocumentParseError("Extracted text exceeds the configured character limit")

    drafts: list[_BlockDraft] = []
    heading_stack: list[str] = []
    paragraph: list[str] = []
    code_lines: list[str] = []
    in_code = False
    page_number: int | None = None
    extraction_method: ExtractionMethod = "source_text"

    def flush_paragraph() -> None:
        if not paragraph:
            return
        value = "\n".join(paragraph).strip()
        paragraph.clear()
        if value:
            drafts.append(
                _BlockDraft(
                    kind=_classify_text_block(value, in_references=_in_references(heading_stack)),
                    text=value,
                    page_number=page_number,
                    heading_path=tuple(heading_stack),
                    extraction_method=extraction_method,
                )
            )

    for raw_line in normalized.splitlines():
        line = raw_line.rstrip()
        if line.strip().startswith("```"):
            flush_paragraph()
            if in_code:
                value = "\n".join(code_lines).strip()
                code_lines.clear()
                if value:
                    drafts.append(
                        _BlockDraft(
                            kind="code",
                            text=value,
                            page_number=page_number,
                            heading_path=tuple(heading_stack),
                            extraction_method=extraction_method,
                        )
                    )
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue

        comment_match = _EXTRACTION_COMMENT_PATTERN.fullmatch(line.strip())
        if comment_match:
            flush_paragraph()
            extraction_method = _markdown_extraction_method(comment_match.group(1))
            continue
        heading_match = _MARKDOWN_HEADING_PATTERN.fullmatch(line.strip())
        if heading_match:
            flush_paragraph()
            level = len(heading_match.group(1))
            value = heading_match.group(2).strip()
            page_match = _PAGE_HEADING_PATTERN.fullmatch(value)
            if page_match:
                page_number = int(page_match.group(1))
                continue
            heading_stack = _updated_heading_stack(heading_stack, level, value)
            drafts.append(
                _BlockDraft(
                    kind="title" if level == 1 and not drafts else "heading",
                    text=value,
                    page_number=page_number,
                    heading_level=level,
                    heading_path=tuple(heading_stack),
                    extraction_method=extraction_method,
                )
            )
            continue
        if not line.strip():
            flush_paragraph()
            continue
        if line.strip().casefold().startswith(_SKIPPED_OCR_METADATA_PREFIXES):
            flush_paragraph()
            continue
        if _LIST_PATTERN.match(line.strip()):
            flush_paragraph()
            drafts.append(
                _BlockDraft(
                    kind="list_item",
                    text=line.strip(),
                    page_number=page_number,
                    heading_path=tuple(heading_stack),
                    extraction_method=extraction_method,
                )
            )
            continue
        paragraph.append(line)
    flush_paragraph()
    if code_lines:
        drafts.append(
            _BlockDraft(
                kind="code",
                text="\n".join(code_lines).strip(),
                page_number=page_number,
                heading_path=tuple(heading_stack),
                extraction_method=extraction_method,
            )
        )
    blocks = _materialize_blocks(drafts, source_hash=source_hash)
    return DocumentIR(
        parser_revision="document-ir-markdown-v1",
        source_hash=source_hash,
        title=_first_title(blocks),
        blocks=blocks,
        metadata={"page_count": max((block.page_number or 0 for block in blocks), default=0)},
    )


class _BlockDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: BlockKind
    text: str
    page_number: int | None
    heading_path: tuple[str, ...]
    extraction_method: ExtractionMethod
    heading_level: int | None = None
    extraction_confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _segment_lines(
    lines: Sequence[str],
    *,
    page_number: int,
    extraction_method: ExtractionMethod,
    extraction_confidence: float | None,
    extraction_warnings: Sequence[str],
    heading_stack: Sequence[str],
) -> tuple[list[_BlockDraft], list[str]]:
    drafts: list[_BlockDraft] = []
    stack = list(heading_stack)
    paragraph: list[str] = []

    def append_block(kind: BlockKind, text: str, heading_level: int | None = None) -> None:
        nonlocal stack
        value = text.strip()
        if not value:
            return
        if kind in {"title", "heading"} and heading_level is not None:
            stack = _updated_heading_stack(stack, heading_level, value)
        metadata = (
            {"extraction_warnings": list(extraction_warnings)}
            if extraction_warnings
            else {}
        )
        drafts.append(
            _BlockDraft(
                kind=kind,
                text=value,
                page_number=page_number,
                heading_level=heading_level,
                heading_path=tuple(stack),
                extraction_method=extraction_method,
                extraction_confidence=extraction_confidence,
                metadata=metadata,
            )
        )

    def flush_paragraph() -> None:
        if not paragraph:
            return
        value = _join_pdf_lines(paragraph)
        paragraph.clear()
        append_block(
            _classify_text_block(value, in_references=_in_references(stack)),
            value,
        )

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            continue
        heading_level = _pdf_heading_level(line)
        if heading_level is not None:
            flush_paragraph()
            append_block("heading", _clean_heading(line), heading_level)
            continue
        if _LIST_PATTERN.match(line):
            flush_paragraph()
            append_block("list_item", line)
            continue
        if _CAPTION_PATTERN.match(line):
            flush_paragraph()
            append_block("caption", line)
            continue
        if _looks_like_table_row(line):
            flush_paragraph()
            append_block("table", line)
            continue
        paragraph.append(line)
        if sum(len(item) for item in paragraph) >= 1_500:
            flush_paragraph()
    flush_paragraph()
    return drafts, stack


def _materialize_blocks(
    drafts: Sequence[_BlockDraft],
    *,
    source_hash: str,
) -> tuple[DocumentBlock, ...]:
    blocks: list[DocumentBlock] = []
    cursor = 0
    for order, draft in enumerate(draft for draft in drafts if draft.text.strip()):
        text = draft.text.strip()
        digest = hashlib.sha256(
            f"{source_hash}\0{order}\0{draft.page_number}\0{draft.kind}\0{text}".encode()
        ).hexdigest()
        blocks.append(
            DocumentBlock(
                block_id=f"block:{digest[:32]}",
                kind=draft.kind,
                text=text,
                order=order,
                page_number=draft.page_number,
                heading_level=draft.heading_level,
                heading_path=draft.heading_path,
                char_start=cursor,
                char_end=cursor + len(text),
                extraction_method=draft.extraction_method,
                extraction_confidence=draft.extraction_confidence,
                metadata=draft.metadata,
            )
        )
        cursor += len(text) + 2
    return tuple(blocks)


def _repeated_page_edges(pages: Sequence[str]) -> set[str]:
    if len(pages) < 3:
        return set()
    counts: Counter[str] = Counter()
    for page in pages:
        lines = [line for line in page.splitlines() if line.strip()]
        page_edges = {
            key
            for line in [*lines[:2], *lines[-2:]]
            if (key := _edge_key(line))
        }
        counts.update(page_edges)
    threshold = max(3, math.ceil(len(pages) * 0.6))
    return {key for key, count in counts.items() if count >= threshold}


def _edge_key(line: str) -> str:
    compact = re.sub(r"\d+", "#", " ".join(line.casefold().split()))
    return compact if 3 <= len(compact) <= 200 else ""


def _pdf_heading_level(line: str) -> int | None:
    compact = " ".join(line.split())
    if not compact or len(compact) > 180:
        return None
    numbered = _HEADING_NUMBER_PATTERN.match(compact)
    if numbered:
        prefix = compact[: numbered.start(1)]
        return min(prefix.count(".") + 2, 6)
    normalized = compact.rstrip(":").casefold()
    if normalized in _COMMON_HEADINGS:
        return 2
    words = compact.split()
    if (
        1 <= len(words) <= 12
        and not compact.endswith((".", "?", "!", ";", ","))
        and sum(character.isalpha() for character in compact) >= 4
        and (
            compact.isupper()
            or sum(word[:1].isupper() for word in words) / len(words) >= 0.8
        )
    ):
        return 2
    return None


def _clean_heading(value: str) -> str:
    match = _HEADING_NUMBER_PATTERN.match(value)
    return (match.group(1) if match else value).strip().rstrip(":")


def _updated_heading_stack(
    current: Sequence[str],
    level: int,
    value: str,
) -> list[str]:
    index = max(0, level - 1)
    stack = list(current[:index])
    while len(stack) < index:
        stack.append("")
    stack.append(value)
    return [item for item in stack if item]


def _classify_text_block(text: str, *, in_references: bool) -> BlockKind:
    if in_references and _REFERENCE_PATTERN.match(text):
        return "reference"
    if _CAPTION_PATTERN.match(text):
        return "caption"
    if _looks_like_table_row(text):
        return "table"
    if _looks_like_equation(text):
        return "equation"
    return "paragraph"


def _looks_like_table_row(text: str) -> bool:
    line = text.splitlines()[0] if text else ""
    return (
        line.count("|") >= 2
        or len(re.findall(r"\S+\s{2,}", line)) >= 2
        or bool(re.match(r"^\s*[-+:| ]{6,}\s*$", line))
    )


def _looks_like_equation(text: str) -> bool:
    compact = " ".join(text.split())
    if len(compact) > 300:
        return False
    operators = len(re.findall(r"[=+\-*/∑∏√≤≥≈]", compact))
    alphabetic = sum(character.isalpha() for character in compact)
    return operators >= 2 and operators >= max(2, alphabetic // 4)


def _in_references(heading_stack: Sequence[str]) -> bool:
    return bool(heading_stack and heading_stack[-1].casefold() in {"references", "bibliography"})


def _join_pdf_lines(lines: Sequence[str]) -> str:
    merged = ""
    for line in lines:
        value = line.strip()
        if not value:
            continue
        if merged.endswith("-") and value[:1].islower():
            merged = merged[:-1] + value
        else:
            merged = f"{merged} {value}".strip()
    return merged


def _first_title(blocks: Sequence[DocumentBlock]) -> str | None:
    for block in blocks:
        if block.kind == "title":
            return block.text[:500]
    return None


def _markdown_extraction_method(value: str) -> ExtractionMethod:
    normalized = value.casefold()
    if normalized == "gpt_vision_ocr":
        return "vision_ocr"
    if normalized in {"pdf_text", "unresolved_low_text"}:
        return "native_text"
    return "source_text"


def _normalize_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _CONTROL_PATTERN.sub("", normalized)
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{4,}", "\n\n\n", normalized)
    return normalized.strip()
