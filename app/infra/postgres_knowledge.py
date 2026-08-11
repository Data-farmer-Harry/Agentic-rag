from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from asyncpg import Connection, Record

from app.domain.enums import DocumentStatus
from app.domain.models import (
    EvidenceRef,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSource,
    OutboxEvent,
    utc_now,
)
from app.infra.postgres import PostgresDatabase, PostgresMigration
from app.infra.postgres_outbox import insert_outbox_event
from app.knowledge.knowledge_provenance import provenance_from_source, source_metadata
from app.knowledge.knowledge_repository import (
    FileKnowledgeObjectStore,
    JsonKnowledgeRepository,
    KnowledgeStoreError,
    _terms,
)
from app.knowledge.knowledge_visibility import (
    evidence_is_visible,
    knowledge_layer_for_source,
    knowledge_layer_priority,
)

KNOWLEDGE_MIGRATIONS = (
    PostgresMigration(
        version=2,
        name="knowledge_metadata_and_outbox",
        statement="""
CREATE TABLE IF NOT EXISTS knowledge_documents (
    document_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    user_id text NOT NULL,
    filename text NOT NULL,
    title text NOT NULL,
    media_type text NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size > 0),
    content_hash char(64) NOT NULL,
    storage_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'archived', 'failed')),
    chunk_count integer NOT NULL CHECK (chunk_count >= 0),
    parser_version text NOT NULL,
    error text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_scope_hash_idx
ON knowledge_documents (tenant_id, project_id, content_hash);

CREATE INDEX IF NOT EXISTS knowledge_documents_scope_status_idx
ON knowledge_documents (tenant_id, project_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES knowledge_documents(document_id) ON DELETE CASCADE,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    chunk_index integer NOT NULL CHECK (chunk_index >= 0),
    text text NOT NULL,
    content_hash char(64) NOT NULL,
    page_number integer CHECK (page_number >= 1),
    char_start integer NOT NULL CHECK (char_start >= 0),
    char_end integer NOT NULL CHECK (char_end >= 0),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS knowledge_chunks_scope_document_idx
ON knowledge_chunks (tenant_id, project_id, document_id, chunk_index);

CREATE TABLE IF NOT EXISTS outbox_events (
    event_id uuid PRIMARY KEY,
    aggregate_type text NOT NULL,
    aggregate_id text NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL CHECK (
        status IN ('pending', 'processing', 'published', 'dead_letter')
    ),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 100),
    available_at timestamptz NOT NULL,
    lease_owner text,
    lease_expires_at timestamptz,
    published_at timestamptz,
    error text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS outbox_events_claim_idx
ON outbox_events (status, available_at, created_at);

CREATE INDEX IF NOT EXISTS outbox_events_aggregate_idx
ON outbox_events (aggregate_type, aggregate_id, created_at);

CREATE INDEX IF NOT EXISTS outbox_events_scope_claim_idx
ON outbox_events (
    (payload ->> 'tenant_id'), (payload ->> 'project_id'),
    status, available_at, created_at
);
""",
    ),
)

_DOCUMENT_SOURCE_KEY = "_knowledge_source"


class PostgresKnowledgeRepositoryError(KnowledgeStoreError):
    pass


class PostgresKnowledgeRepository:
    def __init__(
        self,
        database: PostgresDatabase,
        objects: FileKnowledgeObjectStore,
    ) -> None:
        self._database = database
        self._objects = objects

    async def ingest(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        raw_content: bytes,
    ) -> tuple[KnowledgeDocument, bool]:
        _validate_document_chunks(document, chunks)
        await self._objects.put(document.storage_key, raw_content)
        stored = document.model_copy(
            update={"chunk_count": len(chunks), "updated_at": utc_now()}
        )
        lock_id = _content_lock_id(
            stored.tenant_id,
            stored.project_id,
            stored.content_hash,
        )
        async with self._database.pool.acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", lock_id)
            duplicate = await connection.fetchrow(
                """
SELECT * FROM knowledge_documents
WHERE tenant_id = $1 AND project_id = $2 AND content_hash = $3
  AND status = 'active'
LIMIT 1
""",
                stored.tenant_id,
                stored.project_id,
                stored.content_hash,
            )
            if duplicate is not None:
                return _document_from_record(duplicate), True
            await _upsert_document(connection, stored)
            await connection.execute(
                "DELETE FROM knowledge_chunks WHERE document_id = $1",
                stored.document_id,
            )
            for chunk in chunks:
                await _insert_chunk(connection, chunk)
            await insert_outbox_event(
                connection,
                _document_event(stored, "knowledge.document.metadata_stored"),
            )
        return stored, False

    async def import_legacy(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> KnowledgeDocument:
        _validate_document_chunks(document, chunks)
        if not self._objects.exists(document.storage_key):
            raise PostgresKnowledgeRepositoryError(
                f"Legacy source object is missing for {document.document_id}"
            )
        async with self._database.pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                "SELECT * FROM knowledge_documents WHERE document_id = $1",
                document.document_id,
            )
            if existing is not None:
                return _document_from_record(existing)
            await _upsert_document(connection, document)
            for chunk in chunks:
                await _insert_chunk(connection, chunk)
            await insert_outbox_event(
                connection,
                _document_event(document, "knowledge.document.legacy_imported"),
            )
        return document

    async def list_documents(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        include_archived: bool = False,
    ) -> Sequence[KnowledgeDocument]:
        if include_archived:
            rows = await self._database.pool.fetch(
                """
SELECT * FROM knowledge_documents
WHERE tenant_id = $1 AND project_id = $2
ORDER BY updated_at DESC, document_id
""",
                tenant_id,
                project_id,
            )
        else:
            rows = await self._database.pool.fetch(
                """
SELECT * FROM knowledge_documents
WHERE tenant_id = $1 AND project_id = $2 AND status != 'archived'
ORDER BY updated_at DESC, document_id
""",
                tenant_id,
                project_id,
            )
        return [_document_from_record(row) for row in rows]

    async def find_by_hash(
        self,
        content_hash: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> KnowledgeDocument | None:
        record = await self._database.pool.fetchrow(
            """
SELECT * FROM knowledge_documents
WHERE tenant_id = $1 AND project_id = $2 AND content_hash = $3
  AND status = 'active'
LIMIT 1
""",
            tenant_id,
            project_id,
            content_hash,
        )
        return _document_from_record(record) if record is not None else None

    async def get_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> KnowledgeDocument | None:
        record = await self._database.pool.fetchrow(
            """
SELECT * FROM knowledge_documents
WHERE document_id = $1 AND tenant_id = $2 AND project_id = $3
""",
            document_id,
            tenant_id,
            project_id,
        )
        return _document_from_record(record) if record is not None else None

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
        async with self._database.pool.acquire() as connection, connection.transaction():
            record = await connection.fetchrow(
                """
SELECT * FROM knowledge_documents
WHERE document_id = $1 AND tenant_id = $2 AND project_id = $3
FOR UPDATE
""",
                document_id,
                tenant_id,
                project_id,
            )
            if record is None:
                return None
            document = _document_from_record(record)
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
            await _upsert_document(connection, updated)
            await insert_outbox_event(
                connection,
                _document_event(updated, "knowledge.document.source_enriched"),
            )
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
        rows = await self._database.pool.fetch(
            """
SELECT chunk.*
FROM knowledge_chunks AS chunk
JOIN knowledge_documents AS document ON document.document_id = chunk.document_id
WHERE chunk.document_id = $1
  AND document.tenant_id = $2 AND document.project_id = $3
ORDER BY chunk.chunk_index
""",
            document_id,
            tenant_id,
            project_id,
        )
        return [_chunk_from_record(row) for row in rows]

    async def replace_chunks(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        *,
        expected_parser_version: str,
    ) -> KnowledgeDocument:
        _validate_document_chunks(document, chunks)
        stored = document.model_copy(
            update={"chunk_count": len(chunks), "updated_at": utc_now()}
        )
        async with self._database.pool.acquire() as connection, connection.transaction():
            record = await connection.fetchrow(
                """
SELECT * FROM knowledge_documents
WHERE document_id = $1 AND tenant_id = $2 AND project_id = $3
FOR UPDATE
""",
                stored.document_id,
                stored.tenant_id,
                stored.project_id,
            )
            if record is None:
                raise PostgresKnowledgeRepositoryError(
                    "Knowledge document does not exist"
                )
            current = _document_from_record(record)
            if (
                current.content_hash != stored.content_hash
                or current.storage_key != stored.storage_key
            ):
                raise PostgresKnowledgeRepositoryError(
                    "Knowledge document identity changed during rechunk"
                )
            if current.parser_version != expected_parser_version:
                raise PostgresKnowledgeRepositoryError(
                    "Knowledge document parser version changed during rechunk"
                )
            await _upsert_document(connection, stored)
            await connection.execute(
                "DELETE FROM knowledge_chunks WHERE document_id = $1",
                stored.document_id,
            )
            for chunk in chunks:
                await _insert_chunk(connection, chunk)
            await insert_outbox_event(
                connection,
                _document_event(stored, "knowledge.document.rechunked"),
            )
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
        async with self._database.pool.acquire() as connection, connection.transaction():
            record = await connection.fetchrow(
                """
UPDATE knowledge_documents
SET status = $4, error = $5, updated_at = now()
WHERE document_id = $1 AND tenant_id = $2 AND project_id = $3
RETURNING *
""",
                document_id,
                tenant_id,
                project_id,
                status.value,
                error,
            )
            if record is None:
                return None
            document = _document_from_record(record)
            await insert_outbox_event(
                connection,
                _document_event(document, "knowledge.document.status_changed"),
            )
        return document

    async def archive(
        self,
        document_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> bool:
        async with self._database.pool.acquire() as connection, connection.transaction():
            record = await connection.fetchrow(
                """
UPDATE knowledge_documents
SET status = 'archived', updated_at = now()
WHERE document_id = $1 AND tenant_id = $2 AND project_id = $3
  AND status != 'archived'
RETURNING *
""",
                document_id,
                tenant_id,
                project_id,
            )
            if record is None:
                return False
            document = _document_from_record(record)
            await insert_outbox_event(
                connection,
                _document_event(document, "knowledge.document.archived"),
            )
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
        rows = await self._database.pool.fetch(
            """
SELECT
    chunk.*,
    document.filename AS document_filename,
    document.title AS document_title,
    document.metadata AS document_metadata,
    document.user_id AS document_user_id
FROM knowledge_chunks AS chunk
JOIN knowledge_documents AS document ON document.document_id = chunk.document_id
WHERE document.tenant_id = $1 AND document.project_id = $2
  AND document.status = 'active'
""",
            tenant_id,
            project_id,
        )
        return _rank_rows(
            query,
            rows,
            filters=filters,
            user_id=user_id,
            enabled_knowledge_layers=enabled_knowledge_layers,
            top_k=top_k,
        )

    async def chunk_count(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> int:
        record = await self._database.pool.fetchrow(
            """
SELECT COALESCE(sum(chunk_count), 0) AS count
FROM knowledge_documents
WHERE tenant_id = $1 AND project_id = $2 AND status = 'active'
""",
            tenant_id,
            project_id,
        )
        return int(record["count"]) if record is not None else 0


class LegacyKnowledgeMetadataMigrator:
    def __init__(
        self,
        source: JsonKnowledgeRepository,
        target: PostgresKnowledgeRepository,
    ) -> None:
        self._source = source
        self._target = target

    async def start(self) -> None:
        documents = await self._source.list_all_documents()
        for document in documents:
            chunks = await self._source.list_chunks(
                document.document_id,
                tenant_id=document.tenant_id,
                project_id=document.project_id,
            )
            await self._target.import_legacy(document, chunks)

    async def close(self) -> None:
        return None


async def _upsert_document(
    connection: Connection,
    document: KnowledgeDocument,
) -> None:
    await connection.execute(
        """
INSERT INTO knowledge_documents (
    document_id, tenant_id, project_id, user_id, filename, title, media_type,
    byte_size, content_hash, storage_key, status, chunk_count, parser_version,
    error, metadata, created_at, updated_at
) VALUES (
    $1, $2, $3, $4, $5, $6, $7,
    $8, $9, $10, $11, $12, $13,
    $14, $15::jsonb, $16, $17
)
ON CONFLICT (document_id) DO UPDATE SET
    user_id = EXCLUDED.user_id,
    filename = EXCLUDED.filename,
    title = EXCLUDED.title,
    media_type = EXCLUDED.media_type,
    byte_size = EXCLUDED.byte_size,
    content_hash = EXCLUDED.content_hash,
    storage_key = EXCLUDED.storage_key,
    status = EXCLUDED.status,
    chunk_count = EXCLUDED.chunk_count,
    parser_version = EXCLUDED.parser_version,
    error = EXCLUDED.error,
    metadata = EXCLUDED.metadata,
    updated_at = EXCLUDED.updated_at
""",
        document.document_id,
        document.tenant_id,
        document.project_id,
        document.user_id,
        document.filename,
        document.title,
        document.media_type,
        document.byte_size,
        document.content_hash,
        document.storage_key,
        document.status.value,
        document.chunk_count,
        document.parser_version,
        document.error,
        json.dumps(
            {
                **document.metadata,
                _DOCUMENT_SOURCE_KEY: document.source.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        document.created_at,
        document.updated_at,
    )


async def _insert_chunk(connection: Connection, chunk: KnowledgeChunk) -> None:
    await connection.execute(
        """
INSERT INTO knowledge_chunks (
    chunk_id, document_id, tenant_id, project_id, chunk_index, text,
    content_hash, page_number, char_start, char_end, metadata
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
ON CONFLICT (chunk_id) DO UPDATE SET
    text = EXCLUDED.text,
    content_hash = EXCLUDED.content_hash,
    page_number = EXCLUDED.page_number,
    char_start = EXCLUDED.char_start,
    char_end = EXCLUDED.char_end,
    metadata = EXCLUDED.metadata
""",
        chunk.chunk_id,
        chunk.document_id,
        chunk.tenant_id,
        chunk.project_id,
        chunk.chunk_index,
        chunk.text,
        chunk.content_hash,
        chunk.page_number,
        chunk.char_start,
        chunk.char_end,
        json.dumps(chunk.metadata, ensure_ascii=False, sort_keys=True),
    )


def _document_from_record(record: Record) -> KnowledgeDocument:
    payload = dict(record)
    metadata = _decode_json(payload.get("metadata"))
    payload["source"] = metadata.pop(_DOCUMENT_SOURCE_KEY, {})
    payload["metadata"] = metadata
    return KnowledgeDocument.model_validate(payload)


def _chunk_from_record(record: Record) -> KnowledgeChunk:
    payload = dict(record)
    payload.pop("document_filename", None)
    payload.pop("document_title", None)
    payload.pop("document_metadata", None)
    payload["metadata"] = _decode_json(payload.get("metadata"))
    return KnowledgeChunk.model_validate(payload)


def _decode_json(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise PostgresKnowledgeRepositoryError("Expected a JSON object")
        return cast(dict[str, Any], decoded)
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    raise PostgresKnowledgeRepositoryError("Invalid JSON metadata from Postgres")


def _validate_document_chunks(
    document: KnowledgeDocument,
    chunks: Sequence[KnowledgeChunk],
) -> None:
    if not chunks:
        raise PostgresKnowledgeRepositoryError("A knowledge document requires chunks")
    indexes: set[int] = set()
    for chunk in chunks:
        if (
            chunk.document_id != document.document_id
            or chunk.tenant_id != document.tenant_id
            or chunk.project_id != document.project_id
        ):
            raise PostgresKnowledgeRepositoryError("Chunk scope does not match its document")
        if chunk.chunk_index in indexes:
            raise PostgresKnowledgeRepositoryError("Chunk indexes must be unique")
        indexes.add(chunk.chunk_index)


def _document_event(document: KnowledgeDocument, event_type: str) -> OutboxEvent:
    event_id = uuid5(
        NAMESPACE_URL,
        f"hermesgraph:outbox:{event_type}:{document.document_id}:{document.updated_at.isoformat()}",
    )
    return OutboxEvent(
        event_id=event_id,
        aggregate_type="knowledge_document",
        aggregate_id=str(document.document_id),
        event_type=event_type,
        payload={
            "document_id": str(document.document_id),
            "tenant_id": document.tenant_id,
            "project_id": document.project_id,
            "status": document.status.value,
            "content_hash": document.content_hash,
            "chunk_count": document.chunk_count,
        },
    )


def _rank_rows(
    query: str,
    rows: Sequence[Record],
    *,
    filters: Mapping[str, object] | None,
    user_id: str | None,
    enabled_knowledge_layers: Sequence[str] | None,
    top_k: int,
) -> list[EvidenceRef]:
    query_terms = _terms(query)
    normalized_query = " ".join(query.casefold().split())
    ranked: list[tuple[float, int, str, EvidenceRef]] = []
    for row in rows:
        chunk = _chunk_from_record(row)
        document_metadata = _decode_json(row["document_metadata"])
        source = KnowledgeSource.model_validate(
            document_metadata.get(_DOCUMENT_SOURCE_KEY, {})
        )
        if source.source_status != "active":
            continue
        layer = document_metadata.get("knowledge_layer")
        if not isinstance(layer, str):
            resolved_layer = knowledge_layer_for_source(source)
            layer = resolved_layer.value if resolved_layer is not None else "unclassified"
        metadata = {
            **chunk.metadata,
            "tenant_id": chunk.tenant_id,
            "project_id": chunk.project_id,
            "document_id": str(chunk.document_id),
            "filename": cast(str, row["document_filename"]),
            "chunk_index": chunk.chunk_index,
            "user_id": cast(str, row["document_user_id"]),
            "knowledge_layer": layer,
            **source_metadata(source, document_id=chunk.document_id),
        }
        if filters and any(metadata.get(key) != value for key, value in filters.items()):
            continue
        if enabled_knowledge_layers is not None and (
            user_id is None
            or not evidence_is_visible(
                metadata,
                user_id=user_id,
                enabled_layers=enabled_knowledge_layers,
            )
        ):
            continue
        chunk_terms = _terms(chunk.text)
        overlap = len(query_terms & chunk_terms)
        exact = bool(normalized_query and normalized_query in chunk.text.casefold())
        if query_terms and overlap == 0 and not exact:
            continue
        score = overlap / max(len(query_terms), 1) + (1.0 if exact else 0.0)
        if chunk.page_number is not None:
            metadata["page_number"] = chunk.page_number
        evidence = EvidenceRef(
            text=chunk.text,
            title=cast(str, row["document_title"]),
            score=score,
            provenance=provenance_from_source(
                source,
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                content_hash=chunk.content_hash,
                page_number=chunk.page_number,
            ),
            metadata=metadata,
        )
        ranked.append(
            (
                score,
                knowledge_layer_priority(metadata.get("knowledge_layer")),
                str(chunk.chunk_id),
                evidence,
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in ranked[:top_k]]


def _content_lock_id(tenant_id: str, project_id: str, content_hash: str) -> int:
    digest = hashlib.sha256(
        f"knowledge\0{tenant_id}\0{project_id}\0{content_hash}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)
