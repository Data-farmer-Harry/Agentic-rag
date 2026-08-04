from __future__ import annotations

import asyncio
import fcntl
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, TextIO
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.contracts import (
    GraphCandidateRepository,
    KnowledgeGraphIndexPort,
    KnowledgeRepository,
)
from app.domain.models import KnowledgeChunk, KnowledgeDocument, utc_now


class GraphDocumentEnricher(Protocol):
    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None: ...


class GraphBackfillEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser_version: str
    extractor_revision: str
    status: Literal["completed", "error"]
    attempts: int = Field(ge=1)
    entity_candidates: int = Field(default=0, ge=0)
    relation_candidates: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=2_000)
    updated_at: datetime = Field(default_factory=utc_now)


class GraphBackfillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    entries: dict[str, GraphBackfillEntry] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class GraphBackfillSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    extractor_revision: str
    documents_discovered: int = Field(ge=0)
    documents_selected: int = Field(ge=0)
    documents_completed: int = Field(ge=0)
    documents_skipped: int = Field(ge=0)
    documents_failed: int = Field(ge=0)
    entity_candidates: int = Field(ge=0)
    relation_candidates: int = Field(ge=0)
    dry_run: bool = False
    checkpoint_path: str


class GraphBackfillProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    processed: int = Field(ge=0)
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    entity_candidates: int = Field(ge=0)
    relation_candidates: int = Field(ge=0)
    last_document_id: UUID | None = None
    last_status: Literal["completed", "error"] | None = None


class GraphBackfillAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _BackfillOutcome:
    document_id: UUID
    entry: GraphBackfillEntry


class GraphBackfillService:
    """Bounded, resumable semantic graph enrichment for retained documents."""

    def __init__(
        self,
        knowledge: KnowledgeRepository,
        candidates: GraphCandidateRepository,
        enricher: GraphDocumentEnricher,
        *,
        extractor_revision: str,
        checkpoint_path: Path,
        structural_index: KnowledgeGraphIndexPort | None = None,
    ) -> None:
        if not extractor_revision.strip() or len(extractor_revision) > 200:
            raise ValueError("A valid graph extractor revision is required")
        self._knowledge = knowledge
        self._candidates = candidates
        self._enricher = enricher
        self._extractor_revision = extractor_revision.strip()
        self._checkpoint_path = checkpoint_path.resolve()
        self._structural_index = structural_index
        self._lock_path = self._checkpoint_path.with_suffix(
            self._checkpoint_path.suffix + ".lock"
        )

    async def run(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        document_ids: Sequence[UUID] = (),
        limit: int | None = None,
        force: bool = False,
        skip_errors: bool = False,
        fail_fast: bool = False,
        dry_run: bool = False,
        concurrency: int = 1,
        progress_callback: Callable[[GraphBackfillProgress], None] | None = None,
    ) -> GraphBackfillSummary:
        if limit is not None and limit < 1:
            raise ValueError("Backfill limit must be positive")
        if not 1 <= concurrency <= 12:
            raise ValueError("Backfill concurrency must be between 1 and 12")
        if fail_fast and concurrency != 1:
            raise ValueError("fail_fast requires concurrency=1")
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock():
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
                if (
                    not force
                    and skip_errors
                    and current is not None
                    and current.status == "error"
                    and current.content_hash == document.content_hash
                    and current.parser_version == document.parser_version
                    and current.extractor_revision == self._extractor_revision
                ):
                    skipped += 1
                    continue
                selected.append(document)
                if limit is not None and len(selected) >= limit:
                    break

            if dry_run:
                return GraphBackfillSummary(
                    extractor_revision=self._extractor_revision,
                    documents_discovered=discovered,
                    documents_selected=len(selected),
                    documents_completed=0,
                    documents_skipped=skipped,
                    documents_failed=0,
                    entity_candidates=0,
                    relation_candidates=0,
                    dry_run=True,
                    checkpoint_path=str(self._checkpoint_path),
                )

            completed = failed = entity_count = relation_count = 0
            semaphore = asyncio.Semaphore(concurrency)
            if progress_callback is not None:
                progress_callback(
                    GraphBackfillProgress(
                        processed=0,
                        total=len(selected),
                        completed=0,
                        failed=0,
                        entity_candidates=0,
                        relation_candidates=0,
                    )
                )

            async def process(document: KnowledgeDocument) -> _BackfillOutcome:
                key = str(document.document_id)
                previous = entries.get(key)
                attempts = (previous.attempts if previous is not None else 0) + 1
                async with semaphore:
                    try:
                        chunks = await self._knowledge.list_chunks(
                            document.document_id,
                            tenant_id=tenant_id,
                            project_id=project_id,
                        )
                        if not chunks:
                            raise RuntimeError("Document has no retained chunks")
                        await self._enricher.index_document(document, chunks)
                        if self._structural_index is not None:
                            await self._structural_index.index_document(document, chunks)
                        entities = await self._candidates.list_entities(
                            tenant_id=tenant_id,
                            project_id=project_id,
                            document_id=document.document_id,
                        )
                        relations = await self._candidates.list_relations(
                            tenant_id=tenant_id,
                            project_id=project_id,
                            document_id=document.document_id,
                        )
                        current_entities = sum(
                            item.extractor_revision == self._extractor_revision
                            for item in entities
                        )
                        current_relations = sum(
                            item.extractor_revision == self._extractor_revision
                            for item in relations
                        )
                        entry = GraphBackfillEntry(
                            document_id=document.document_id,
                            content_hash=document.content_hash,
                            parser_version=document.parser_version,
                            extractor_revision=self._extractor_revision,
                            status="completed",
                            attempts=attempts,
                            entity_candidates=current_entities,
                            relation_candidates=current_relations,
                        )
                    except Exception as exc:
                        entry = GraphBackfillEntry(
                            document_id=document.document_id,
                            content_hash=document.content_hash,
                            parser_version=document.parser_version,
                            extractor_revision=self._extractor_revision,
                            status="error",
                            attempts=attempts,
                            error=f"{type(exc).__name__}: {exc}"[:2_000],
                        )
                return _BackfillOutcome(document_id=document.document_id, entry=entry)

            tasks = [
                asyncio.create_task(
                    process(document),
                    name=f"graph-backfill:{document.document_id}",
                )
                for document in selected
            ]
            for task in asyncio.as_completed(tasks):
                outcome = await task
                entries[str(outcome.document_id)] = outcome.entry
                if outcome.entry.status == "completed":
                    completed += 1
                    entity_count += outcome.entry.entity_candidates
                    relation_count += outcome.entry.relation_candidates
                else:
                    failed += 1
                self._save_manifest(
                    GraphBackfillManifest(entries=entries, updated_at=utc_now())
                )
                if progress_callback is not None:
                    progress_callback(
                        GraphBackfillProgress(
                            processed=completed + failed,
                            total=len(selected),
                            completed=completed,
                            failed=failed,
                            entity_candidates=entity_count,
                            relation_candidates=relation_count,
                            last_document_id=outcome.document_id,
                            last_status=outcome.entry.status,
                        )
                    )
                if failed and fail_fast:
                    for pending in tasks:
                        pending.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    break

            return GraphBackfillSummary(
                extractor_revision=self._extractor_revision,
                documents_discovered=discovered,
                documents_selected=len(selected),
                documents_completed=completed,
                documents_skipped=skipped,
                documents_failed=failed,
                entity_candidates=entity_count,
                relation_candidates=relation_count,
                dry_run=False,
                checkpoint_path=str(self._checkpoint_path),
            )

    def _is_current(
        self,
        entry: GraphBackfillEntry | None,
        document: KnowledgeDocument,
    ) -> bool:
        return bool(
            entry is not None
            and entry.status == "completed"
            and entry.content_hash == document.content_hash
            and entry.parser_version == document.parser_version
            and entry.extractor_revision == self._extractor_revision
        )

    def _load_manifest(self) -> GraphBackfillManifest:
        if not self._checkpoint_path.exists():
            return GraphBackfillManifest()
        return GraphBackfillManifest.model_validate_json(
            self._checkpoint_path.read_text(encoding="utf-8")
        )

    def _save_manifest(self, manifest: GraphBackfillManifest) -> None:
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

    def _exclusive_lock(self) -> _BackfillLock:
        return _BackfillLock(self._lock_path)


class _BackfillLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: TextIO | None = None

    def __enter__(self) -> _BackfillLock:
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise GraphBackfillAlreadyRunning(
                "Another graph backfill owns this checkpoint"
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
