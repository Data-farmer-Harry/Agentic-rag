from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from qdrant_client import AsyncQdrantClient, models

from app.domain.enums import DocumentStatus
from app.domain.models import (
    EvidenceRef,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSource,
    RunContext,
)
from app.knowledge.provenance import provenance_from_source, source_metadata
from app.knowledge.visibility import (
    WorkspaceProfileResolver,
    document_visibility_metadata,
    evidence_is_visible,
    knowledge_layer_priority,
)
from app.retrieval.embeddings import DenseEmbeddingPort, SparseEmbeddingPort

_LEXICAL_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_\-]+|[\u3400-\u9fff]", re.UNICODE)
_LEXICAL_STOP_TOKENS = {
    "a",
    "an",
    "and",
    "are",
    "does",
    "how",
    "in",
    "is",
    "of",
    "or",
    "the",
    "to",
    "what",
    "与",
    "了",
    "什",
    "么",
    "和",
    "在",
    "是",
    "有",
    "的",
}


class QdrantHybridStore:
    """Named dense+sparse vectors with server-side RRF and mandatory scope filters."""

    DENSE_VECTOR = "dense"
    SPARSE_VECTOR = "sparse"
    _FILTER_FIELDS = {
        "tenant_id",
        "project_id",
        "document_id",
        "filename",
        "media_type",
        "source_type",
        "privacy",
        "status",
        "source_status",
        "user_id",
        "knowledge_layer",
    }

    def __init__(
        self,
        client: AsyncQdrantClient,
        dense: DenseEmbeddingPort,
        sparse: SparseEmbeddingPort,
        *,
        collection_name: str = "hermesgraph_chunks",
        prefetch_limit: int = 40,
        rrf_k: int = 60,
        create_payload_indexes: bool = True,
        require_lexical_overlap: bool | None = None,
        use_sparse_idf: bool = False,
        workspace_profiles: WorkspaceProfileResolver | None = None,
    ) -> None:
        if not collection_name or len(collection_name) > 200:
            raise ValueError("A valid Qdrant collection name is required")
        if prefetch_limit < 1:
            raise ValueError("prefetch_limit must be positive")
        if rrf_k < 1:
            raise ValueError("rrf_k must be positive")
        self._client = client
        self._dense = dense
        self._sparse = sparse
        self._collection_name = collection_name
        self._prefetch_limit = prefetch_limit
        self._rrf_k = rrf_k
        self._create_payload_indexes = create_payload_indexes
        self._use_sparse_idf = use_sparse_idf
        self._workspace_profiles = workspace_profiles
        self._require_lexical_overlap = (
            dense.revision.startswith("deterministic-hash-dense-")
            if require_lexical_overlap is None
            else require_lexical_overlap
        )
        self._ready = False
        self._ready_lock = asyncio.Lock()

    @property
    def collection_name(self) -> str:
        return self._collection_name

    async def ensure_collection(self) -> None:
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            exists = await self._client.collection_exists(self._collection_name)
            if not exists:
                await self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config={
                        self.DENSE_VECTOR: models.VectorParams(
                            size=self._dense.dimension,
                            distance=models.Distance.COSINE,
                        )
                    },
                    sparse_vectors_config={
                        self.SPARSE_VECTOR: models.SparseVectorParams(
                            index=models.SparseIndexParams(on_disk=False),
                            modifier=(
                                models.Modifier.IDF if self._use_sparse_idf else None
                            ),
                        )
                    },
                )
            await self._validate_collection()
            if self._create_payload_indexes:
                await self._ensure_payload_indexes()
            self._ready = True

    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        if any(
            chunk.document_id != document.document_id
            or chunk.tenant_id != document.tenant_id
            or chunk.project_id != document.project_id
            for chunk in chunks
        ):
            raise ValueError("All indexed chunks must belong to the document scope")
        if not chunks:
            raise ValueError("At least one chunk is required for indexing")
        await self.ensure_collection()
        previous_ids = await self._document_point_ids(document)
        texts = [_indexable_text(document, chunk) for chunk in chunks]
        dense_vectors, sparse_vectors = await asyncio.gather(
            self._dense.embed(texts),
            self._sparse.embed(texts),
        )
        if len(dense_vectors) != len(chunks) or len(sparse_vectors) != len(chunks):
            raise RuntimeError("Embedding providers returned an invalid batch size")
        points = [
            models.PointStruct(
                id=chunk.chunk_id,
                vector={
                    self.DENSE_VECTOR: dense_vector,
                    self.SPARSE_VECTOR: models.SparseVector(
                        indices=sparse_vector.indices,
                        values=sparse_vector.values,
                    ),
                },
                payload=self._payload(document, chunk),
            )
            for chunk, dense_vector, sparse_vector in zip(
                chunks,
                dense_vectors,
                sparse_vectors,
                strict=True,
            )
        ]
        await self._client.upsert(
            collection_name=self._collection_name,
            points=points,
            wait=True,
        )
        current_ids = {str(chunk.chunk_id) for chunk in chunks}
        stale_ids = [
            point_id for point_id in previous_ids if str(point_id) not in current_ids
        ]
        if stale_ids:
            await self._client.delete(
                collection_name=self._collection_name,
                points_selector=models.PointIdsList(points=stale_ids),
                wait=True,
            )

    async def archive_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> None:
        await self.ensure_collection()
        await self._client.set_payload(
            collection_name=self._collection_name,
            payload={"status": DocumentStatus.ARCHIVED.value},
            points=models.FilterSelector(
                filter=self._build_filter(
                    {
                        "tenant_id": tenant_id,
                        "project_id": project_id,
                        "document_id": str(document_id),
                    },
                    active_only=False,
                )
            ),
            wait=True,
        )

    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: Mapping[str, Any] | None = None,
        top_k: int = 10,
    ) -> Sequence[EvidenceRef]:
        if not 1 <= top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        requested = dict(filters or {})
        for key in ("user_id", "knowledge_layer", "enabled_knowledge_layers"):
            if key in requested:
                raise ValueError(f"Caller cannot select server-owned visibility field: {key}")
        enforced = {"tenant_id": context.tenant_id, "project_id": context.project_id}
        for key, value in enforced.items():
            if key in requested and requested[key] != value:
                raise ValueError(f"Caller cannot override enforced filter: {key}")
        profile = (
            self._workspace_profiles.resolve(
                tenant_id=context.tenant_id,
                project_id=context.project_id,
            )
            if self._workspace_profiles is not None
            else None
        )
        return await self.search(
            query,
            filters={**requested, **enforced},
            top_k=top_k,
            visibility=(
                context.user_id,
                [layer.value for layer in profile.enabled_knowledge_layers],
            )
            if profile is not None
            else None,
        )

    async def search(
        self,
        query: str,
        *,
        filters: Mapping[str, Any],
        top_k: int = 10,
        visibility: tuple[str, Sequence[str]] | None = None,
    ) -> Sequence[EvidenceRef]:
        if not query.strip():
            raise ValueError("query must not be empty")
        await self.ensure_collection()
        dense_batch, sparse_batch = await asyncio.gather(
            self._dense.embed([query]),
            self._sparse.embed([query]),
        )
        if not dense_batch or not sparse_batch:
            return []
        sparse = sparse_batch[0]
        prefetch_limit = max(self._prefetch_limit, top_k * 2)
        if any(key in filters for key in ("user_id", "knowledge_layer")):
            raise ValueError("Visibility filters are reserved for server-side projection")
        query_filter = self._build_filter(
            filters,
            active_only=True,
            visibility=visibility,
        )
        candidate_limit = min(top_k * 2, 50)
        response = await self._client.query_points(
            collection_name=self._collection_name,
            prefetch=[
                models.Prefetch(
                    query=models.SparseVector(
                        indices=sparse.indices,
                        values=sparse.values,
                    ),
                    using=self.SPARSE_VECTOR,
                    filter=query_filter,
                    limit=prefetch_limit,
                ),
                models.Prefetch(
                    query=dense_batch[0],
                    using=self.DENSE_VECTOR,
                    filter=query_filter,
                    limit=prefetch_limit,
                ),
            ],
            query=models.RrfQuery(rrf=models.Rrf(k=self._rrf_k)),
            query_filter=query_filter,
            limit=candidate_limit,
            with_payload=True,
            with_vectors=False,
        )
        evidence: list[EvidenceRef] = []
        for point in response.points:
            payload = dict(point.payload or {})
            expected_payload = {
                **filters,
                "status": DocumentStatus.ACTIVE.value,
                "source_status": "active",
            }
            if any(payload.get(key) != value for key, value in expected_payload.items()):
                continue
            if visibility is not None and not evidence_is_visible(
                payload,
                user_id=visibility[0],
                enabled_layers=visibility[1],
            ):
                # Backward-compatible but fail closed: old points lack the two
                # persisted visibility fields and are hidden until reindexed.
                continue
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                continue
            lexical_text = "\n".join(
                part
                for part in (str(payload.get("title") or ""), text)
                if part
            )
            if self._require_lexical_overlap and not _has_lexical_overlap(
                query, lexical_text
            ):
                continue
            document_id = str(payload.get("document_id", ""))
            chunk_index = int(payload.get("chunk_index", 0))
            page_number = payload.get("page_number")
            source = _source_from_payload(payload)
            evidence.append(
                EvidenceRef(
                    text=text,
                    title=str(payload.get("title") or payload.get("filename") or "Document"),
                    score=max(float(point.score), 0.0),
                    provenance=provenance_from_source(
                        source,
                        document_id=document_id,
                        chunk_index=chunk_index,
                        content_hash=str(payload.get("content_hash") or "") or None,
                        page_number=page_number if isinstance(page_number, int) else None,
                        chunk_metadata=payload,
                    ),
                    metadata={
                        key: value
                        for key, value in payload.items()
                        if key != "text"
                    }
                    | {
                        "retrieval_backend": "qdrant_hybrid",
                        "dense_revision": self._dense.revision,
                        "sparse_revision": self._sparse.revision,
                    },
                )
            )
        evidence.sort(
            key=lambda item: (
                -item.score,
                knowledge_layer_priority(item.metadata.get("knowledge_layer")),
                str(item.evidence_id),
            )
        )
        return _source_diversified(evidence)[:top_k]

    async def close(self) -> None:
        await self._client.close()

    async def _validate_collection(self) -> None:
        info = await self._client.get_collection(self._collection_name)
        vectors = info.config.params.vectors
        if not isinstance(vectors, dict):
            raise RuntimeError("Qdrant collection must use named vectors")
        dense = vectors.get(self.DENSE_VECTOR)
        if dense is None or dense.size != self._dense.dimension:
            raise RuntimeError("Qdrant dense vector dimension does not match the encoder")
        sparse_vectors = info.config.params.sparse_vectors or {}
        sparse = sparse_vectors.get(self.SPARSE_VECTOR)
        if sparse is None:
            raise RuntimeError("Qdrant collection is missing the sparse vector")
        actual_modifier = getattr(sparse, "modifier", None)
        expected_modifier = models.Modifier.IDF if self._use_sparse_idf else None
        if actual_modifier != expected_modifier:
            raise RuntimeError("Qdrant sparse IDF configuration does not match the store")

    async def _ensure_payload_indexes(self) -> None:
        for field in (
            "tenant_id",
            "project_id",
            "document_id",
            "status",
            "source_status",
            "user_id",
            "knowledge_layer",
        ):
            await self._client.create_payload_index(
                collection_name=self._collection_name,
                field_name=field,
                field_schema=models.PayloadSchemaType.KEYWORD,
                wait=True,
            )

    async def _document_point_ids(self, document: KnowledgeDocument) -> list[Any]:
        point_ids: list[Any] = []
        offset: Any = None
        document_filter = self._build_filter(
            {
                "tenant_id": document.tenant_id,
                "project_id": document.project_id,
                "document_id": str(document.document_id),
            },
            active_only=False,
        )
        while True:
            records, offset = await self._client.scroll(
                collection_name=self._collection_name,
                scroll_filter=document_filter,
                limit=256,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            point_ids.extend(record.id for record in records)
            if offset is None:
                return point_ids

    def _build_filter(
        self,
        filters: Mapping[str, Any],
        *,
        active_only: bool,
        visibility: tuple[str, Sequence[str]] | None = None,
    ) -> models.Filter:
        unknown = set(filters) - self._FILTER_FIELDS
        if unknown:
            raise ValueError(f"Unsupported Qdrant filter fields: {sorted(unknown)}")
        values = dict(filters)
        if active_only:
            values["status"] = DocumentStatus.ACTIVE.value
            values["source_status"] = "active"
        conditions: list[models.FieldCondition] = []
        for key, value in values.items():
            if not isinstance(value, str | int | bool):
                raise ValueError(f"Qdrant filter {key} must be a scalar")
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
            )
        if visibility is None:
            return models.Filter(must=conditions)
        user_id, enabled_layers = visibility
        layer_conditions: list[models.Filter] = []
        for layer in dict.fromkeys(enabled_layers):
            if layer == "team_internal" or layer == "public_reference":
                layer_conditions.append(
                    models.Filter(
                        must=[
                            models.FieldCondition(
                                key="knowledge_layer",
                                match=models.MatchValue(value=layer),
                            )
                        ]
                    )
                )
            elif layer == "personal":
                layer_conditions.append(
                    models.Filter(
                        must=[
                            models.FieldCondition(
                                key="knowledge_layer",
                                match=models.MatchValue(value="personal"),
                            ),
                            models.FieldCondition(
                                key="user_id",
                                match=models.MatchValue(value=user_id),
                            ),
                        ]
                    )
                )
        if not layer_conditions:
            # The resolver never emits this in normal operation. It still makes
            # an empty configuration fail closed without scanning the collection.
            conditions.append(
                models.FieldCondition(
                    key="knowledge_layer",
                    match=models.MatchValue(value="__no_visible_layer__"),
                )
            )
            return models.Filter(must=conditions)
        return models.Filter(
            must=conditions,
            min_should=models.MinShould(conditions=layer_conditions, min_count=1),
        )

    @staticmethod
    def _payload(document: KnowledgeDocument, chunk: KnowledgeChunk) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tenant_id": document.tenant_id,
            "project_id": document.project_id,
            "document_id": str(document.document_id),
            "filename": document.filename,
            "title": document.title,
            "media_type": document.media_type,
            "status": DocumentStatus.ACTIVE.value,
            "chunk_id": str(chunk.chunk_id),
            "chunk_index": chunk.chunk_index,
            "text": chunk.text,
            "content_hash": chunk.content_hash,
            "parser_version": document.parser_version,
            "created_at": document.created_at.isoformat(),
            **document_visibility_metadata(document),
            **source_metadata(document.source, document_id=document.document_id),
            "source_trust": document.source.trust.value,
            "source_acquired_at": document.source.acquired_at.isoformat(),
        }
        if chunk.page_number is not None:
            payload["page_number"] = chunk.page_number
        for key in (
            "modality",
            "visual_kind",
            "visual_region_id",
            "visual_category",
            "visual_bbox",
            "vision_confidence",
            "document_ir_schema",
            "parser_revision",
            "chunker_revision",
            "chunk_strategy",
            "chunk_level",
            "parent_section_id",
            "section_id",
            "section_ids",
            "packed_section_count",
            "heading_path",
            "heading_paths",
            "block_ids",
            "block_kinds",
            "page_start",
            "page_end",
            "extraction_methods",
            "ocr_confidence_min",
            "token_count",
        ):
            if key in chunk.metadata:
                payload[key] = chunk.metadata[key]
        return payload


def _source_from_payload(payload: Mapping[str, Any]) -> KnowledgeSource:
    source_payload: dict[str, object] = {
        "source_type": payload.get("source_type") or "uploaded_document",
        "source_id": payload.get("source_id") or "",
        "privacy": payload.get("privacy") or "private",
        "trust": payload.get("source_trust") or "user_asserted",
    }
    optional_fields = {
        "source_revision": payload.get("source_revision"),
        "canonical_uri": payload.get("canonical_uri"),
        "license_uri": payload.get("license_uri"),
        "acquired_at": payload.get("source_acquired_at"),
        "source_status": payload.get("source_status"),
        "owner": payload.get("source_owner"),
        "last_reviewed_at": payload.get("source_last_reviewed_at"),
        "effective_from": payload.get("source_effective_from"),
        "effective_to": payload.get("source_effective_to"),
        "supersedes_source_id": payload.get("source_supersedes"),
        "superseded_by_source_id": payload.get("source_superseded_by"),
        "fixture_id": payload.get("fixture_id"),
    }
    source_payload.update(
        {key: value for key, value in optional_fields.items() if value is not None}
    )
    return KnowledgeSource.model_validate(source_payload)


def _has_lexical_overlap(query: str, text: str) -> bool:
    query_tokens = {
        token
        for token in _LEXICAL_TOKEN_PATTERN.findall(query.casefold())
        if token not in _LEXICAL_STOP_TOKENS
    }
    if not query_tokens:
        return False
    text_tokens = set(_LEXICAL_TOKEN_PATTERN.findall(text.casefold()))
    query_identifiers = {
        token
        for token in query_tokens
        if len(token) >= 4 and any(character.isdigit() for character in token)
    }
    if query_identifiers:
        return bool(query_identifiers.intersection(text_tokens))
    overlap = query_tokens.intersection(text_tokens)
    if not overlap:
        return False
    required_overlap = 1 if len(query_tokens) <= 2 else 2
    return len(overlap) >= required_overlap


def _indexable_text(document: KnowledgeDocument, chunk: KnowledgeChunk) -> str:
    if chunk.metadata.get("contextualized") is True:
        return chunk.text
    heading_path = chunk.metadata.get("heading_path")
    headings = (
        "\n".join(str(item) for item in heading_path if str(item).strip())
        if isinstance(heading_path, list)
        else ""
    )
    return "\n".join(
        part for part in (document.title, headings, chunk.text) if part.strip()
    )


def _source_diversified(evidence: Sequence[EvidenceRef]) -> list[EvidenceRef]:
    first_from_source: list[EvidenceRef] = []
    repeated_sources: list[EvidenceRef] = []
    seen: set[str] = set()
    for item in evidence:
        source = str(
            item.metadata.get("document_id")
            or item.provenance.source_id.split("#", 1)[0]
        )
        if source in seen:
            repeated_sources.append(item)
        else:
            first_from_source.append(item)
            seen.add(source)
    return [*first_from_source, *repeated_sources]
