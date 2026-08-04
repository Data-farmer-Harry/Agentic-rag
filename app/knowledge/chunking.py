from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.domain.models import KnowledgeChunk
from app.knowledge.document_ir import DocumentBlock, DocumentIR

_O200K_CACHE_KEY = "fb374d419588a4632f3f557e76b4b70aebbca790"


def _configure_bundled_tiktoken_cache() -> None:
    if os.environ.get("TIKTOKEN_CACHE_DIR"):
        return
    candidates = (
        Path(__file__).resolve().parents[2] / "assets" / "tiktoken",
        Path("/opt/hermesgraph/tiktoken-cache"),
    )
    for candidate in candidates:
        if (candidate / _O200K_CACHE_KEY).is_file():
            os.environ["TIKTOKEN_CACHE_DIR"] = str(candidate)
            return


@dataclass(frozen=True, slots=True)
class _Section:
    section_id: str
    heading_path: tuple[str, ...]
    blocks: tuple[DocumentBlock, ...]


@dataclass(frozen=True, slots=True)
class _SectionGroup:
    section_id: str
    heading_path: tuple[str, ...]
    sections: tuple[_Section, ...]

    @property
    def blocks(self) -> tuple[DocumentBlock, ...]:
        return tuple(block for section in self.sections for block in section.blocks)


class HierarchicalDocumentChunker:
    """Structure-first leaf chunker with stable section-parent provenance."""

    revision = "hierarchical-token-chunker-v2:o200k_base:min80"

    def __init__(
        self,
        *,
        target_tokens: int = 400,
        overlap_tokens: int = 45,
        min_section_tokens: int | None = None,
        max_chunks: int = 10_000,
    ) -> None:
        if not 50 <= target_tokens <= 8_000:
            raise ValueError("target_tokens must be between 50 and 8000")
        if not 0 <= overlap_tokens < target_tokens:
            raise ValueError("overlap_tokens must be smaller than target_tokens")
        resolved_min_section_tokens = (
            min(80, max(1, target_tokens // 2))
            if min_section_tokens is None
            else min_section_tokens
        )
        if not 1 <= resolved_min_section_tokens <= target_tokens:
            raise ValueError("min_section_tokens must not exceed the target")
        if max_chunks < 1:
            raise ValueError("max_chunks must be positive")
        self._target_tokens = target_tokens
        self._overlap_tokens = overlap_tokens
        self._min_section_tokens = resolved_min_section_tokens
        self._max_chunks = max_chunks
        _configure_bundled_tiktoken_cache()
        self._encoding = tiktoken.get_encoding("o200k_base")
        self.revision = (
            "hierarchical-token-chunker-v2:o200k_base:"
            f"min{resolved_min_section_tokens}"
        )

    def chunk(
        self,
        document_ir: DocumentIR,
        *,
        document_id: UUID,
        tenant_id: str,
        project_id: str,
        filename: str,
        title: str,
    ) -> list[KnowledgeChunk]:
        chunks: list[KnowledgeChunk] = []
        groups = self._pack_sections(_sections(document_ir.blocks), title=title)
        for group in groups:
            body = _group_body(group)
            if not body:
                continue
            context = _context_prefix(title, group.heading_path)
            context_tokens = self._token_count(context)
            separator_tokens = self._token_count("\n\n") if context else 0
            body_budget = max(
                50,
                self._target_tokens - context_tokens - separator_tokens,
            )
            overlap = min(self._overlap_tokens, max(0, body_budget // 3))
            splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
                encoding_name="o200k_base",
                chunk_size=body_budget,
                chunk_overlap=overlap,
                separators=["\n\n", "\n", "。", "！", "？", ". ", " ", ""],
            )
            cursor = 0
            for body_part in splitter.split_text(body):
                search_start = max(0, cursor - max(1, len(body_part) // 3))
                local_start = body.find(body_part, search_start)
                if local_start < 0:
                    local_start = cursor
                local_end = local_start + len(body_part)
                source_blocks = (
                    _intersecting_blocks(
                        group.blocks,
                        body,
                        local_start,
                        local_end,
                    )
                    if len(group.sections) == 1
                    else group.blocks
                )
                text = f"{context}\n\n{body_part}".strip() if context else body_part
                content_hash = hashlib.sha256(text.encode()).hexdigest()
                chunk_index = len(chunks)
                chunk_id = uuid5(
                    NAMESPACE_URL,
                    (
                        f"hermesgraph:chunk:{document_id}:{self.revision}:"
                        f"{group.section_id}:{chunk_index}:{content_hash}"
                    ),
                )
                pages = sorted(
                    {
                        block.page_number
                        for block in source_blocks
                        if block.page_number is not None
                    }
                )
                confidences = [
                    block.extraction_confidence
                    for block in source_blocks
                    if block.extraction_confidence is not None
                ]
                methods = sorted(
                    {block.extraction_method for block in source_blocks}
                )
                chunks.append(
                    KnowledgeChunk(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        tenant_id=tenant_id,
                        project_id=project_id,
                        chunk_index=chunk_index,
                        text=text,
                        content_hash=content_hash,
                        page_number=pages[0] if pages else None,
                        char_start=min(
                            (block.char_start for block in source_blocks),
                            default=0,
                        ),
                        char_end=max(
                            (block.char_end for block in source_blocks),
                            default=len(body_part),
                        ),
                        metadata={
                            "filename": filename,
                            "document_ir_schema": document_ir.schema_version,
                            "parser_revision": document_ir.parser_revision,
                            "chunker_revision": self.revision,
                            "chunk_strategy": "structure_first_token_aware",
                            "chunk_level": "leaf",
                            "parent_section_id": group.section_id,
                            "section_id": group.section_id,
                            "section_ids": [
                                section.section_id for section in group.sections
                            ],
                            "packed_section_count": len(group.sections),
                            "heading_path": list(group.heading_path),
                            "heading_paths": [
                                list(section.heading_path)
                                for section in group.sections
                            ],
                            "block_ids": [block.block_id for block in source_blocks],
                            "block_kinds": sorted({block.kind for block in source_blocks}),
                            "page_start": pages[0] if pages else None,
                            "page_end": pages[-1] if pages else None,
                            "extraction_methods": methods,
                            "ocr_confidence_min": min(confidences)
                            if confidences
                            else None,
                            "token_count": self._token_count(text),
                            "contextualized": bool(context),
                        },
                    )
                )
                if len(chunks) > self._max_chunks:
                    raise ValueError(
                        f"Document exceeds the {self._max_chunks} chunk limit"
                    )
                cursor = local_end
        return chunks

    def _pack_sections(
        self,
        sections: Sequence[_Section],
        *,
        title: str,
    ) -> list[_SectionGroup]:
        groups: list[tuple[_Section, ...]] = []
        pending: list[_Section] = []
        for section in sections:
            body_tokens = self._token_count(_section_body(section))
            if body_tokens < self._min_section_tokens:
                candidate = (*pending, section)
                if pending and self._group_token_count(candidate, title) > self._target_tokens:
                    groups.append(tuple(pending))
                    pending = [section]
                else:
                    pending.append(section)
                continue
            if pending:
                candidate = (*pending, section)
                if self._group_token_count(candidate, title) <= self._target_tokens:
                    groups.append(candidate)
                    pending = []
                    continue
                groups.append(tuple(pending))
                pending = []
            groups.append((section,))
        if pending:
            if (
                groups
                and self._group_token_count((*groups[-1], *pending), title)
                <= self._target_tokens
            ):
                groups[-1] = (*groups[-1], *pending)
            else:
                groups.append(tuple(pending))
        return [_section_group(group) for group in groups]

    def _group_token_count(
        self,
        sections: Sequence[_Section],
        title: str,
    ) -> int:
        group = _section_group(sections)
        context = _context_prefix(title, group.heading_path)
        return self._token_count(
            f"{context}\n\n{_group_body(group)}".strip()
        )

    def _token_count(self, text: str) -> int:
        return len(self._encoding.encode(text, disallowed_special=()))


def _sections(blocks: Sequence[DocumentBlock]) -> list[_Section]:
    grouped: list[tuple[tuple[str, ...], list[DocumentBlock]]] = []
    for block in blocks:
        if block.kind in {"title", "heading"}:
            continue
        heading_path = block.heading_path
        if not grouped or grouped[-1][0] != heading_path:
            grouped.append((heading_path, []))
        grouped[-1][1].append(block)

    sections: list[_Section] = []
    for index, (heading_path, members) in enumerate(grouped):
        if not members:
            continue
        digest = hashlib.sha256(
            (
                f"{index}\0{'/'.join(heading_path)}\0"
                f"{members[0].block_id}\0{members[-1].block_id}"
            ).encode()
        ).hexdigest()
        sections.append(
            _Section(
                section_id=f"section:{digest[:32]}",
                heading_path=heading_path,
                blocks=tuple(members),
            )
        )
    return sections


def _section_group(sections: Sequence[_Section]) -> _SectionGroup:
    members = tuple(sections)
    if not members:
        raise ValueError("A section group requires at least one section")
    section_ids = "\0".join(section.section_id for section in members)
    digest = hashlib.sha256(section_ids.encode()).hexdigest()
    return _SectionGroup(
        section_id=f"section-group:{digest[:32]}",
        heading_path=_common_heading_path(
            [section.heading_path for section in members]
        ),
        sections=members,
    )


def _section_body(section: _Section) -> str:
    return "\n\n".join(block.text for block in section.blocks).strip()


def _group_body(group: _SectionGroup) -> str:
    if len(group.sections) == 1:
        return _section_body(group.sections[0])
    parts: list[str] = []
    for section in group.sections:
        relative_headings = section.heading_path[len(group.heading_path) :]
        heading_prefix = "\n".join(
            f"{'#' * min(index + 2, 6)} {heading}"
            for index, heading in enumerate(relative_headings)
            if heading.strip()
        )
        body = _section_body(section)
        parts.append(
            "\n\n".join(part for part in (heading_prefix, body) if part).strip()
        )
    return "\n\n".join(part for part in parts if part).strip()


def _common_heading_path(
    paths: Sequence[Sequence[str]],
) -> tuple[str, ...]:
    if not paths:
        return ()
    common: list[str] = []
    for values in zip(*paths, strict=False):
        if len(set(values)) != 1:
            break
        common.append(values[0])
    return tuple(common)


def _context_prefix(title: str, heading_path: Sequence[str]) -> str:
    lines: list[str] = []
    clean_title = " ".join(title.split())
    if clean_title:
        lines.append(f"# {clean_title}")
    for index, heading in enumerate(heading_path, start=2):
        clean = " ".join(heading.split())
        if clean and clean.casefold() != clean_title.casefold():
            lines.append(f"{'#' * min(index, 6)} {clean}")
    return "\n".join(lines)


def _intersecting_blocks(
    blocks: Sequence[DocumentBlock],
    body: str,
    start: int,
    end: int,
) -> tuple[DocumentBlock, ...]:
    positions: list[tuple[int, int, DocumentBlock]] = []
    cursor = 0
    for block in blocks:
        block_start = body.find(block.text, cursor)
        if block_start < 0:
            block_start = cursor
        block_end = block_start + len(block.text)
        positions.append((block_start, block_end, block))
        cursor = block_end
    selected = tuple(
        block
        for block_start, block_end, block in positions
        if block_end > start and block_start < end
    )
    return selected or tuple(blocks[:1])
