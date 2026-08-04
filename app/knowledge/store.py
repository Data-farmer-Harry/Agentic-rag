from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID, uuid4

from app.domain.enums import DocumentStatus
from app.domain.models import (
    EvidenceRef,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSource,
    utc_now,
)
from app.knowledge.provenance import evidence_from_chunk
from app.knowledge.visibility import document_is_visible, knowledge_layer_priority

_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_\-]+|[\u3400-\u9fff]+", re.UNICODE)
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]+")


class KnowledgeStoreError(RuntimeError):
    pass


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for token in _TOKEN_PATTERN.findall(text.casefold()):
        terms.add(token)
        if _CJK_PATTERN.fullmatch(token):
            terms.update(token[index : index + 2] for index in range(max(0, len(token) - 1)))
    return terms


class FileKnowledgeObjectStore:
    """Atomic, path-safe storage for retained source objects."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._uploads_root = (self._root / "uploads").resolve()

    async def put(self, storage_key: str, content: bytes) -> None:
        await asyncio.to_thread(self._put, storage_key, content)

    async def read(self, storage_key: str) -> bytes:
        return await asyncio.to_thread(self._path(storage_key).read_bytes)

    def exists(self, storage_key: str) -> bool:
        return self._path(storage_key).exists()

    def _path(self, storage_key: str) -> Path:
        relative = Path(storage_key)
        if relative.is_absolute() or ".." in relative.parts:
            raise KnowledgeStoreError("Document storage key is invalid")
        path = (self._root / relative).resolve()
        if not path.is_relative_to(self._uploads_root):
            raise KnowledgeStoreError("Document storage key escapes the upload root")
        return path

    def _put(self, storage_key: str, content: bytes) -> None:
        if not content:
            raise KnowledgeStoreError("Cannot persist an empty source object")
        path = self._path(storage_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class JsonKnowledgeRepository:
    """Atomic local document/chunk index with separately retained source files."""

    _FORMAT_VERSION = 1

    def __init__(self, root: Path) -> None:
        self._root = root
        self._index_path = root / "index.json"
        self._objects = FileKnowledgeObjectStore(root)
        self._lock = asyncio.Lock()

    async def ingest(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        raw_content: bytes,
    ) -> tuple[KnowledgeDocument, bool]:
        async with self._lock:
            documents, existing_chunks = self._read_all()
            duplicate = next(
                (
                    item
                    for item in documents.values()
                    if item.tenant_id == document.tenant_id
                    and item.project_id == document.project_id
                    and item.content_hash == document.content_hash
                    and item.status == DocumentStatus.ACTIVE
                ),
                None,
            )
            if duplicate is not None:
                return duplicate, True

            stored = document.model_copy(
                update={"chunk_count": len(chunks), "updated_at": utc_now()}
            )
            await self._objects.put(stored.storage_key, raw_content)

            documents[str(stored.document_id)] = stored
            existing_chunks = {
                key: item
                for key, item in existing_chunks.items()
                if item.document_id != stored.document_id
            }
            existing_chunks.update({str(item.chunk_id): item for item in chunks})
            self._write_all(documents, existing_chunks)
            return stored, False

    async def list_documents(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        include_archived: bool = False,
    ) -> Sequence[KnowledgeDocument]:
        async with self._lock:
            documents = list(self._read_all()[0].values())
        scoped = [
            item
            for item in documents
            if item.tenant_id == tenant_id
            and item.project_id == project_id
            and (include_archived or item.status != DocumentStatus.ARCHIVED)
        ]
        return sorted(scoped, key=lambda item: item.updated_at, reverse=True)

    async def list_all_documents(self) -> Sequence[KnowledgeDocument]:
        """Read every legacy scope for one-time metadata migration only."""
        async with self._lock:
            documents = list(self._read_all()[0].values())
        return sorted(documents, key=lambda item: item.updated_at, reverse=True)

    async def find_by_hash(
        self,
        content_hash: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> KnowledgeDocument | None:
        async with self._lock:
            documents = self._read_all()[0].values()
            return next(
                (
                    item
                    for item in documents
                    if item.tenant_id == tenant_id
                    and item.project_id == project_id
                    and item.content_hash == content_hash
                    and item.status == DocumentStatus.ACTIVE
                ),
                None,
            )

    async def get_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> KnowledgeDocument | None:
        async with self._lock:
            document = self._read_all()[0].get(str(document_id))
        if (
            document is None
            or document.tenant_id != tenant_id
            or document.project_id != project_id
        ):
            return None
        return document

    async def enrich_source(
        self,
        document_id: UUID,
        source: KnowledgeSource,
        *,
        title: str | None = None,
        metadata: Mapping[str, object] | None = None,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> KnowledgeDocument | None:
        async with self._lock:
            documents, chunks = self._read_all()
            document = documents.get(str(document_id))
            if (
                document is None
                or document.tenant_id != tenant_id
                or document.project_id != project_id
            ):
                return None
            resolved_title = title or document.title
            resolved_metadata = {**document.metadata, **dict(metadata or {})}
            if (
                document.source == source
                and document.title == resolved_title
                and document.metadata == resolved_metadata
            ):
                return document
            updated = document.model_copy(
                update={
                    "source": source,
                    "title": resolved_title,
                    "metadata": resolved_metadata,
                    "updated_at": utc_now(),
                }
            )
            documents[str(document_id)] = updated
            self._write_all(documents, chunks)
            return updated

    async def read_content(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> tuple[KnowledgeDocument, bytes] | None:
        document = await self.get_document(
            document_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if document is None:
            return None
        return document, await self._objects.read(document.storage_key)

    async def list_chunks(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> Sequence[KnowledgeChunk]:
        async with self._lock:
            documents, chunks = self._read_all()
        document = documents.get(str(document_id))
        if (
            document is None
            or document.tenant_id != tenant_id
            or document.project_id != project_id
        ):
            return []
        return sorted(
            (item for item in chunks.values() if item.document_id == document_id),
            key=lambda item: item.chunk_index,
        )

    async def replace_chunks(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        *,
        expected_parser_version: str,
    ) -> KnowledgeDocument:
        if not chunks:
            raise KnowledgeStoreError("A knowledge document requires chunks")
        if any(
            chunk.document_id != document.document_id
            or chunk.tenant_id != document.tenant_id
            or chunk.project_id != document.project_id
            for chunk in chunks
        ):
            raise KnowledgeStoreError("Chunk scope does not match its document")
        if len({chunk.chunk_index for chunk in chunks}) != len(chunks):
            raise KnowledgeStoreError("Chunk indexes must be unique")
        async with self._lock:
            documents, existing_chunks = self._read_all()
            current = documents.get(str(document.document_id))
            if current is None:
                raise KnowledgeStoreError("Knowledge document does not exist")
            if (
                current.tenant_id != document.tenant_id
                or current.project_id != document.project_id
                or current.content_hash != document.content_hash
                or current.storage_key != document.storage_key
            ):
                raise KnowledgeStoreError("Knowledge document identity changed during rechunk")
            if current.parser_version != expected_parser_version:
                raise KnowledgeStoreError(
                    "Knowledge document parser version changed during rechunk"
                )
            stored = document.model_copy(
                update={"chunk_count": len(chunks), "updated_at": utc_now()}
            )
            documents[str(document.document_id)] = stored
            retained = {
                key: item
                for key, item in existing_chunks.items()
                if item.document_id != document.document_id
            }
            retained.update({str(item.chunk_id): item for item in chunks})
            self._write_all(documents, retained)
            return stored

    async def set_status(
        self,
        document_id: UUID,
        status: DocumentStatus,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        error: str | None = None,
    ) -> KnowledgeDocument | None:
        async with self._lock:
            documents, chunks = self._read_all()
            document = documents.get(str(document_id))
            if (
                document is None
                or document.tenant_id != tenant_id
                or document.project_id != project_id
            ):
                return None
            updated = document.model_copy(
                update={"status": status, "error": error, "updated_at": utc_now()}
            )
            documents[str(document_id)] = updated
            self._write_all(documents, chunks)
            return updated

    async def archive(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> bool:
        async with self._lock:
            documents, chunks = self._read_all()
            document = documents.get(str(document_id))
            if (
                document is None
                or document.tenant_id != tenant_id
                or document.project_id != project_id
                or document.status == DocumentStatus.ARCHIVED
            ):
                return False
            documents[str(document_id)] = document.model_copy(
                update={"status": DocumentStatus.ARCHIVED, "updated_at": utc_now()}
            )
            self._write_all(documents, chunks)
            return True

    async def search(
        self,
        query: str,
        *,
        tenant_id: str,
        project_id: str,
        filters: Mapping[str, object] | None = None,
        user_id: str | None = None,
        enabled_knowledge_layers: Sequence[str] | None = None,
        top_k: int = 10,
    ) -> Sequence[EvidenceRef]:
        if not 1 <= top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        async with self._lock:
            documents, chunks = self._read_all()
        active_documents = {
            item.document_id: item
            for item in documents.values()
            if item.tenant_id == tenant_id
            and item.project_id == project_id
            and item.status == DocumentStatus.ACTIVE
            and item.source.source_status == "active"
            and (
                enabled_knowledge_layers is None
                or (
                    user_id is not None
                    and document_is_visible(
                        item,
                        user_id=user_id,
                        enabled_layers=enabled_knowledge_layers,
                    )
                )
            )
        }
        query_terms = _terms(query)
        normalized_query = " ".join(query.casefold().split())
        ranked: list[tuple[float, int, str, EvidenceRef]] = []
        for chunk in chunks.values():
            document = active_documents.get(chunk.document_id)
            if document is None:
                continue
            metadata = {
                **chunk.metadata,
                "tenant_id": chunk.tenant_id,
                "project_id": chunk.project_id,
                "document_id": str(document.document_id),
                "filename": document.filename,
                "chunk_index": chunk.chunk_index,
                "user_id": document.user_id,
            }
            if filters and any(metadata.get(key) != value for key, value in filters.items()):
                continue
            chunk_terms = _terms(chunk.text)
            overlap = len(query_terms & chunk_terms)
            exact = bool(normalized_query and normalized_query in chunk.text.casefold())
            if query_terms and overlap == 0 and not exact:
                continue
            score = overlap / max(len(query_terms), 1) + (1.0 if exact else 0.0)
            if chunk.page_number is not None:
                metadata["page_number"] = chunk.page_number
            evidence = evidence_from_chunk(
                document,
                chunk,
                score=score,
                metadata=metadata,
            )
            ranked.append(
                (
                    score,
                    knowledge_layer_priority(evidence.metadata.get("knowledge_layer")),
                    str(chunk.chunk_id),
                    evidence,
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        return [item[3] for item in ranked[:top_k]]

    async def chunk_count(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> int:
        async with self._lock:
            documents, _ = self._read_all()
        return sum(
            item.chunk_count
            for item in documents.values()
            if item.tenant_id == tenant_id
            and item.project_id == project_id
            and item.status == DocumentStatus.ACTIVE
        )

    def _read_all(
        self,
    ) -> tuple[dict[str, KnowledgeDocument], dict[str, KnowledgeChunk]]:
        if not self._index_path.exists():
            return {}, {}
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
            documents = [KnowledgeDocument.model_validate(item) for item in payload["documents"]]
            chunks = [KnowledgeChunk.model_validate(item) for item in payload["chunks"]]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KnowledgeStoreError(f"Invalid knowledge index at {self._index_path}") from exc
        return (
            {str(item.document_id): item for item in documents},
            {str(item.chunk_id): item for item in chunks},
        )

    def _write_all(
        self,
        documents: Mapping[str, KnowledgeDocument],
        chunks: Mapping[str, KnowledgeChunk],
    ) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self._FORMAT_VERSION,
            "documents": [
                item.model_dump(mode="json", exclude_none=True)
                for item in sorted(
                    documents.values(),
                    key=lambda value: (value.created_at, str(value.document_id)),
                )
            ],
            "chunks": [
                item.model_dump(mode="json", exclude_none=True)
                for item in sorted(
                    chunks.values(),
                    key=lambda value: (str(value.document_id), value.chunk_index),
                )
            ],
        }
        temporary = self._index_path.with_name(
            f".{self._index_path.name}.{uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._index_path)
        finally:
            temporary.unlink(missing_ok=True)
