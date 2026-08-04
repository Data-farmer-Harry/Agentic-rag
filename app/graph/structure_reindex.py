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

from app.domain.contracts import KnowledgeGraphIndexPort, KnowledgeRepository
from app.domain.models import KnowledgeDocument, utc_now


class GraphStructureReindexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser_version: str
    chunk_count: int = Field(ge=1)
    status: Literal["completed", "error"]
    attempts: int = Field(ge=1)
    error: str | None = Field(default=None, max_length=2_000)
    updated_at: datetime = Field(default_factory=utc_now)


class GraphStructureReindexManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    entries: dict[str, GraphStructureReindexEntry] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class GraphStructureReindexSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    documents_discovered: int = Field(ge=0)
    documents_selected: int = Field(ge=0)
    documents_completed: int = Field(ge=0)
    documents_skipped: int = Field(ge=0)
    documents_failed: int = Field(ge=0)
    chunks_projected: int = Field(ge=0)
    dry_run: bool = False
    checkpoint_path: str


class GraphStructureReindexAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _ProjectionOutcome:
    document_id: UUID
    entry: GraphStructureReindexEntry


class GraphStructureReindexService:
    """Resumable Postgres-to-Neo4j structural projection without model calls."""

    def __init__(
        self,
        knowledge: KnowledgeRepository,
        graph: KnowledgeGraphIndexPort,
        *,
        checkpoint_path: Path,
    ) -> None:
        self._knowledge = knowledge
        self._graph = graph
        self._checkpoint_path = checkpoint_path.resolve()
        self._lock_path = self._checkpoint_path.with_suffix(
            self._checkpoint_path.suffix + ".lock"
        )

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
    ) -> GraphStructureReindexSummary:
        if limit is not None and limit < 1:
            raise ValueError("Graph structure reindex limit must be positive")
        if not 1 <= concurrency <= 16:
            raise ValueError("Graph structure reindex concurrency must be between 1 and 16")
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

            completed = failed = chunks_projected = 0
            semaphore = asyncio.Semaphore(concurrency)

            async def project(document: KnowledgeDocument) -> _ProjectionOutcome:
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
                        if len(chunks) != document.chunk_count or not chunks:
                            raise RuntimeError(
                                "Retained chunk count does not match the document"
                            )
                        await self._graph.index_document(document, chunks)
                        entry = GraphStructureReindexEntry(
                            document_id=document.document_id,
                            content_hash=document.content_hash,
                            parser_version=document.parser_version,
                            chunk_count=len(chunks),
                            status="completed",
                            attempts=attempts,
                        )
                    except Exception as exc:
                        entry = GraphStructureReindexEntry(
                            document_id=document.document_id,
                            content_hash=document.content_hash,
                            parser_version=document.parser_version,
                            chunk_count=max(1, document.chunk_count),
                            status="error",
                            attempts=attempts,
                            error=f"{type(exc).__name__}: {exc}"[:2_000],
                        )
                return _ProjectionOutcome(document.document_id, entry)

            tasks = [
                asyncio.create_task(
                    project(document),
                    name=f"graph-structure-reindex:{document.document_id}",
                )
                for document in selected
            ]
            for task in asyncio.as_completed(tasks):
                outcome = await task
                entries[str(outcome.document_id)] = outcome.entry
                if outcome.entry.status == "completed":
                    completed += 1
                    chunks_projected += outcome.entry.chunk_count
                else:
                    failed += 1
                self._save_manifest(
                    GraphStructureReindexManifest(
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
                chunks_projected=chunks_projected,
            )

    @staticmethod
    def _is_current(
        entry: GraphStructureReindexEntry | None,
        document: KnowledgeDocument,
    ) -> bool:
        return bool(
            entry is not None
            and entry.status == "completed"
            and entry.content_hash == document.content_hash
            and entry.parser_version == document.parser_version
            and entry.chunk_count == document.chunk_count
        )

    def _load_manifest(self) -> GraphStructureReindexManifest:
        if not self._checkpoint_path.exists():
            return GraphStructureReindexManifest()
        return GraphStructureReindexManifest.model_validate_json(
            self._checkpoint_path.read_text(encoding="utf-8")
        )

    def _save_manifest(self, manifest: GraphStructureReindexManifest) -> None:
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
        chunks_projected: int = 0,
        dry_run: bool = False,
    ) -> GraphStructureReindexSummary:
        return GraphStructureReindexSummary(
            documents_discovered=discovered,
            documents_selected=selected,
            documents_completed=completed,
            documents_skipped=skipped,
            documents_failed=failed,
            chunks_projected=chunks_projected,
            dry_run=dry_run,
            checkpoint_path=str(self._checkpoint_path),
        )

    def _exclusive_lock(self) -> _GraphStructureReindexLock:
        return _GraphStructureReindexLock(self._lock_path)


class _GraphStructureReindexLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: TextIO | None = None

    def __enter__(self) -> _GraphStructureReindexLock:
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise GraphStructureReindexAlreadyRunning(
                "Another structural graph reindex owns this checkpoint"
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
