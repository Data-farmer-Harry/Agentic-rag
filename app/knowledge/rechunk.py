from __future__ import annotations

import asyncio
import fcntl
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TextIO
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.contracts import KnowledgeRepository, KnowledgeVectorIndexPort
from app.domain.enums import DocumentStatus
from app.domain.models import KnowledgeDocument, utc_now
from app.knowledge.chunking import HierarchicalDocumentChunker
from app.knowledge.document_ir import DocumentIR
from app.sources.arxiv_ocr import ArxivOcrEntry, ArxivOcrManifest


class RechunkEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_parser_version: str
    target_parser_version: str
    status: Literal["completed", "error"]
    attempts: int = Field(ge=1)
    old_chunk_count: int = Field(ge=0)
    new_chunk_count: int = Field(default=0, ge=0)
    unresolved_low_text_pages: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=2_000)
    updated_at: datetime = Field(default_factory=utc_now)


class RechunkManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    chunker_revision: str
    entries: dict[str, RechunkEntry] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class RechunkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunker_revision: str
    documents_discovered: int = Field(ge=0)
    documents_selected: int = Field(ge=0)
    documents_completed: int = Field(ge=0)
    documents_skipped: int = Field(ge=0)
    documents_failed: int = Field(ge=0)
    old_chunks_replaced: int = Field(ge=0)
    new_chunks_written: int = Field(ge=0)
    unresolved_low_text_pages: int = Field(ge=0)
    vector_index_updated: bool
    dry_run: bool = False
    checkpoint_path: str


class RechunkAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _RechunkOutcome:
    document_id: UUID
    entry: RechunkEntry


class KnowledgeRechunkService:
    """Resumable Document IR to v3 chunk migration without graph enrichment."""

    PARSER_FAMILY = "knowledge-v3"

    def __init__(
        self,
        knowledge: KnowledgeRepository,
        *,
        ocr_root: Path,
        checkpoint_path: Path,
        chunker: HierarchicalDocumentChunker | None = None,
        vector_index: KnowledgeVectorIndexPort | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._ocr_root = ocr_root.resolve()
        self._ir_root = (self._ocr_root / "ir").resolve()
        self._ocr_manifest_path = self._ocr_root / "manifest.json"
        self._checkpoint_path = checkpoint_path.resolve()
        self._lock_path = self._checkpoint_path.with_suffix(
            self._checkpoint_path.suffix + ".lock"
        )
        self._chunker = chunker or HierarchicalDocumentChunker()
        self._vector_index = vector_index

    async def run(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "computer-science",
        document_ids: Sequence[UUID] = (),
        limit: int | None = None,
        force: bool = False,
        fail_fast: bool = False,
        dry_run: bool = False,
        concurrency: int = 4,
    ) -> RechunkSummary:
        if limit is not None and limit < 1:
            raise ValueError("Rechunk limit must be positive")
        if not 1 <= concurrency <= 16:
            raise ValueError("Rechunk concurrency must be between 1 and 16")
        if fail_fast and concurrency != 1:
            raise ValueError("fail_fast requires concurrency=1")
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock():
            ocr_entries = self._ocr_entries_by_hash()
            manifest = self._load_manifest()
            entries = dict(manifest.entries)
            documents = list(
                await self._knowledge.list_documents(
                    tenant_id=tenant_id,
                    project_id=project_id,
                )
            )
            requested_ids = set(document_ids)
            if requested_ids:
                documents = [
                    document
                    for document in documents
                    if document.document_id in requested_ids
                ]
            documents.sort(key=lambda item: (item.title.casefold(), str(item.document_id)))
            discovered = len(documents)

            selected: list[KnowledgeDocument] = []
            skipped = 0
            for document in documents:
                current = entries.get(str(document.document_id))
                if not force and self._is_current(current, document):
                    skipped += 1
                    continue
                selected.append(document)
                if limit is not None and len(selected) >= limit:
                    break

            if dry_run:
                return self._summary(
                    discovered=discovered,
                    selected=len(selected),
                    skipped=skipped,
                    dry_run=True,
                )

            completed = failed = old_chunks = new_chunks = unresolved_pages = 0
            semaphore = asyncio.Semaphore(concurrency)

            async def process(document: KnowledgeDocument) -> _RechunkOutcome:
                key = str(document.document_id)
                previous = entries.get(key)
                attempts = (previous.attempts if previous is not None else 0) + 1
                async with semaphore:
                    try:
                        ocr_entry = ocr_entries.get(document.content_hash)
                        if ocr_entry is None:
                            raise RuntimeError("No OCR manifest entry matches document hash")
                        document_ir = self._load_document_ir(ocr_entry, document)
                        chunks = await asyncio.to_thread(
                            self._chunker.chunk,
                            document_ir,
                            document_id=document.document_id,
                            tenant_id=document.tenant_id,
                            project_id=document.project_id,
                            filename=document.filename,
                            title=document.title,
                        )
                        if not chunks:
                            raise RuntimeError("Document IR produced no chunks")
                        target_parser_version = self._target_parser_version(document_ir)
                        metadata = {
                            **document.metadata,
                            "document_ir_schema": document_ir.schema_version,
                            "document_ir_parser_revision": document_ir.parser_revision,
                            "document_ir_block_count": len(document_ir.blocks),
                            "document_ir_metadata": document_ir.metadata,
                            "chunk_strategy": "structure_first_token_aware",
                            "chunker_revision": self._chunker.revision,
                            "ocr_extractor_revision": ocr_entry.extractor_revision,
                            "ocr_document_ir_path": ocr_entry.document_ir_path,
                            "ocr_page_count": ocr_entry.page_count,
                            "ocr_pdf_text_pages": ocr_entry.pdf_text_pages,
                            "ocr_vision_pages": ocr_entry.gpt_ocr_pages,
                            "ocr_unresolved_low_text_pages": (
                                ocr_entry.unresolved_low_text_pages
                            ),
                            "rechunked_from_parser_version": document.metadata.get(
                                "rechunked_from_parser_version",
                                document.parser_version,
                            ),
                        }
                        updated = document.model_copy(
                            update={
                                "status": DocumentStatus.ACTIVE,
                                "chunk_count": len(chunks),
                                "parser_version": target_parser_version,
                                "error": None,
                                "metadata": metadata,
                            }
                        )
                        if self._vector_index is not None:
                            await self._vector_index.index_document(updated, chunks)
                        stored = await self._knowledge.replace_chunks(
                            updated,
                            chunks,
                            expected_parser_version=document.parser_version,
                        )
                        entry = RechunkEntry(
                            document_id=document.document_id,
                            content_hash=document.content_hash,
                            source_parser_version=document.parser_version,
                            target_parser_version=stored.parser_version,
                            status="completed",
                            attempts=attempts,
                            old_chunk_count=document.chunk_count,
                            new_chunk_count=len(chunks),
                            unresolved_low_text_pages=(
                                ocr_entry.unresolved_low_text_pages
                            ),
                        )
                    except Exception as exc:
                        entry = RechunkEntry(
                            document_id=document.document_id,
                            content_hash=document.content_hash,
                            source_parser_version=document.parser_version,
                            target_parser_version=self._expected_parser_version,
                            status="error",
                            attempts=attempts,
                            old_chunk_count=document.chunk_count,
                            error=f"{type(exc).__name__}: {exc}"[:2_000],
                        )
                return _RechunkOutcome(document_id=document.document_id, entry=entry)

            tasks = [
                asyncio.create_task(
                    process(document),
                    name=f"knowledge-rechunk:{document.document_id}",
                )
                for document in selected
            ]
            for task in asyncio.as_completed(tasks):
                outcome = await task
                entries[str(outcome.document_id)] = outcome.entry
                if outcome.entry.status == "completed":
                    completed += 1
                    old_chunks += outcome.entry.old_chunk_count
                    new_chunks += outcome.entry.new_chunk_count
                    unresolved_pages += outcome.entry.unresolved_low_text_pages
                else:
                    failed += 1
                self._save_manifest(
                    RechunkManifest(
                        chunker_revision=self._chunker.revision,
                        entries=entries,
                        updated_at=utc_now(),
                    )
                )
                if failed and fail_fast:
                    for pending in tasks:
                        pending.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    break

            return self._summary(
                discovered=discovered,
                selected=len(selected),
                completed=completed,
                skipped=skipped,
                failed=failed,
                old_chunks=old_chunks,
                new_chunks=new_chunks,
                unresolved_pages=unresolved_pages,
            )

    @property
    def _expected_parser_version(self) -> str:
        return (
            f"{self.PARSER_FAMILY}+document-ir-pdf-v1+{self._chunker.revision}"
        )

    def _target_parser_version(self, document_ir: DocumentIR) -> str:
        return (
            f"{self.PARSER_FAMILY}+{document_ir.parser_revision}+"
            f"{self._chunker.revision}"
        )

    def _is_current(
        self,
        entry: RechunkEntry | None,
        document: KnowledgeDocument,
    ) -> bool:
        parser_is_current = document.parser_version == self._expected_parser_version
        checkpoint_is_current = bool(
            entry is not None
            and entry.status == "completed"
            and entry.content_hash == document.content_hash
            and entry.target_parser_version == document.parser_version
            and entry.new_chunk_count == document.chunk_count
        )
        return parser_is_current and (entry is None or checkpoint_is_current)

    def _ocr_entries_by_hash(self) -> dict[str, ArxivOcrEntry]:
        if not self._ocr_manifest_path.is_file():
            raise RuntimeError("OCR manifest does not exist")
        manifest = ArxivOcrManifest.model_validate_json(
            self._ocr_manifest_path.read_text(encoding="utf-8")
        )
        entries: dict[str, ArxivOcrEntry] = {}
        for entry in manifest.entries.values():
            if entry.status != "completed" or entry.document_ir_path is None:
                continue
            if entry.source_content_hash in entries:
                raise RuntimeError("OCR manifest contains duplicate source hashes")
            entries[entry.source_content_hash] = entry
        return entries

    def _load_document_ir(
        self,
        entry: ArxivOcrEntry,
        document: KnowledgeDocument,
    ) -> DocumentIR:
        if entry.document_ir_path is None:
            raise RuntimeError("OCR manifest entry has no Document IR")
        path = (self._ocr_root / entry.document_ir_path).resolve()
        if not path.is_relative_to(self._ir_root) or not path.is_file():
            raise RuntimeError("Document IR path is outside the controlled OCR root")
        document_ir = DocumentIR.model_validate_json(path.read_text(encoding="utf-8"))
        if document_ir.source_hash != document.content_hash:
            raise RuntimeError("Document IR source hash does not match retained document")
        if document_ir.parser_revision != "document-ir-pdf-v1":
            raise RuntimeError("Document IR must be refreshed to document-ir-pdf-v1")
        return document_ir

    def _load_manifest(self) -> RechunkManifest:
        if not self._checkpoint_path.exists():
            return RechunkManifest(chunker_revision=self._chunker.revision)
        manifest = RechunkManifest.model_validate_json(
            self._checkpoint_path.read_text(encoding="utf-8")
        )
        if manifest.chunker_revision != self._chunker.revision:
            raise RuntimeError("Rechunk checkpoint belongs to another chunker revision")
        return manifest

    def _save_manifest(self, manifest: RechunkManifest) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self._checkpoint_path.name}.",
            dir=self._checkpoint_path.parent,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(manifest.model_dump_json(indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self._checkpoint_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _summary(
        self,
        *,
        discovered: int,
        selected: int,
        completed: int = 0,
        skipped: int = 0,
        failed: int = 0,
        old_chunks: int = 0,
        new_chunks: int = 0,
        unresolved_pages: int = 0,
        dry_run: bool = False,
    ) -> RechunkSummary:
        return RechunkSummary(
            chunker_revision=self._chunker.revision,
            documents_discovered=discovered,
            documents_selected=selected,
            documents_completed=completed,
            documents_skipped=skipped,
            documents_failed=failed,
            old_chunks_replaced=old_chunks,
            new_chunks_written=new_chunks,
            unresolved_low_text_pages=unresolved_pages,
            vector_index_updated=self._vector_index is not None,
            dry_run=dry_run,
            checkpoint_path=str(self._checkpoint_path),
        )

    def _exclusive_lock(self) -> _RechunkLock:
        return _RechunkLock(self._lock_path)


class _RechunkLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: TextIO | None = None

    def __enter__(self) -> _RechunkLock:
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RechunkAlreadyRunning(
                "Another knowledge rechunk owns this checkpoint"
            ) from exc
        self._handle = handle
        return self

    def __exit__(self, *_: object) -> None:
        handle = self._handle
        if handle is None:
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._handle = None
