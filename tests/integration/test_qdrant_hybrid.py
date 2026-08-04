from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from qdrant_client import AsyncQdrantClient

from app.api.app import create_app
from app.bootstrap import build_components
from app.config import Settings
from app.domain.enums import DocumentStatus, TrustLevel
from app.domain.models import (
    EvidenceRef,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSource,
    Provenance,
    RunContext,
)
from app.knowledge.ingestion import KnowledgeIndexError, KnowledgeIngestionService
from app.knowledge.store import JsonKnowledgeRepository
from app.retrieval.embeddings import DeterministicDenseEmbedder, HashedSparseEmbedder
from app.retrieval.qdrant_hybrid import (
    QdrantHybridStore,
    _has_lexical_overlap,
    _indexable_text,
    _source_diversified,
)


def test_deterministic_lexical_gate_rejects_single_generic_term_overlap() -> None:
    assert _has_lexical_overlap(
        "What does ORBITAL-LANTERN-519 require?",
        "ORBITAL-LANTERN-519 requires a copper key.",
    )
    assert _has_lexical_overlap("MemOps", "The MemOps benchmark evaluates memory.")
    assert not _has_lexical_overlap(
        "What retention rule does QUASAR-NONE-9927 apply to lunar database snapshots?",
        "The evaluation protocol applies a confidence threshold.",
    )
    assert not _has_lexical_overlap(
        "Which benchmark result is reported for NEBULA-BENCH-881?",
        "The benchmark result reports a strong agent score.",
    )
    assert _has_lexical_overlap(
        "Which self-improving agent evolves drawback-detector metrics?",
        "This self-improving agent evolves drawback-detector metrics.",
    )


def test_source_diversification_places_distinct_documents_before_repeated_chunks() -> None:
    def item(source_id: str, document_id: str) -> EvidenceRef:
        return EvidenceRef(
            text=f"Evidence from {source_id}",
            provenance=Provenance(source_type="fixture", source_id=source_id),
            metadata={"document_id": document_id},
        )

    reordered = _source_diversified(
        [
            item("dominant#chunk=0", "dominant"),
            item("dominant#chunk=1", "dominant"),
            item("target#chunk=0", "target"),
            item("dominant#chunk=2", "dominant"),
        ]
    )

    assert [entry.metadata["document_id"] for entry in reordered] == [
        "dominant",
        "target",
        "dominant",
        "dominant",
    ]


def test_contextualized_chunk_is_not_prefixed_twice_for_embedding() -> None:
    document = KnowledgeDocument(
        filename="context.md",
        title="Agent Memory",
        media_type="text/markdown",
        byte_size=10,
        content_hash="a" * 64,
        storage_key="uploads/context.md",
    )
    chunk = KnowledgeChunk(
        document_id=document.document_id,
        chunk_index=0,
        text="# Agent Memory\n\n## Method\n\nRetain durable evidence.",
        content_hash="b" * 64,
        metadata={
            "contextualized": True,
            "heading_path": ["Agent Memory", "Method"],
        },
    )

    assert _indexable_text(document, chunk) == chunk.text


@pytest.mark.asyncio
async def test_qdrant_hybrid_ingest_scope_retrieve_and_archive(tmp_path: Path) -> None:
    client = AsyncQdrantClient(location=":memory:")
    vector_store = QdrantHybridStore(
        client,
        DeterministicDenseEmbedder(64),
        HashedSparseEmbedder(),
        collection_name="contract_chunks",
        create_payload_indexes=False,
    )
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    ingestion = KnowledgeIngestionService(
        repository,
        chunk_size=200,
        chunk_overlap=20,
        vector_index=vector_store,
    )
    try:
        default = await ingestion.ingest(
            filename="mission.md",
            content=b"ORBITAL-LANTERN-519 requires a copper key before launch.",
            media_type="text/markdown",
        )
        await ingestion.ingest(
            filename="other.md",
            content=b"ORBITAL-LANTERN-519 is disabled in the other project.",
            media_type="text/markdown",
            project_id="other",
        )
        await ingestion.ingest(
            filename="title-only.md",
            content=b"This document contains general supporting background.",
            media_type="text/markdown",
            source=KnowledgeSource(title="ORBITAL-TITLE-731 field handbook"),
        )

        evidence = await vector_store.retrieve(
            "What does ORBITAL-LANTERN-519 require?",
            RunContext(project_id="default"),
            filters={"tenant_id": "local", "project_id": "default"},
        )

        assert evidence
        assert "copper key" in evidence[0].text
        assert evidence[0].provenance.trust == TrustLevel.USER_ASSERTED
        assert evidence[0].metadata["retrieval_backend"] == "qdrant_hybrid"
        assert evidence[0].metadata["project_id"] == "default"
        assert evidence[0].metadata["dense_revision"] == "deterministic-hash-dense-v1:64"

        title_match = await vector_store.retrieve(
            "ORBITAL-TITLE-731",
            RunContext(project_id="default"),
        )
        assert title_match[0].title == "ORBITAL-TITLE-731 field handbook"

        assert await vector_store.retrieve(
            "How does agentic retrieval differ from ordinary RAG?",
            RunContext(project_id="default"),
        ) == []

        assert await ingestion.archive(default.document.document_id) is True
        assert await vector_store.retrieve(
            "ORBITAL-LANTERN-519",
            RunContext(project_id="default"),
        ) == []
    finally:
        await vector_store.close()


@pytest.mark.asyncio
async def test_qdrant_reindex_removes_stale_document_chunks() -> None:
    client = AsyncQdrantClient(location=":memory:")
    vector_store = QdrantHybridStore(
        client,
        DeterministicDenseEmbedder(64),
        HashedSparseEmbedder(),
        collection_name="replacement_chunks",
        create_payload_indexes=False,
    )
    document = KnowledgeDocument(
        filename="replacement.md",
        title="Replacement",
        media_type="text/markdown",
        byte_size=20,
        content_hash="a" * 64,
        storage_key="uploads/replacement.md",
        chunk_count=1,
    )
    old_chunk = KnowledgeChunk(
        document_id=document.document_id,
        chunk_index=0,
        text="STALE-CHUNK-440 contains obsolete retrieval evidence.",
        content_hash="b" * 64,
    )
    new_chunk = KnowledgeChunk(
        document_id=document.document_id,
        chunk_index=0,
        text="CURRENT-CHUNK-441 contains the retained retrieval evidence.",
        content_hash="c" * 64,
    )
    try:
        await vector_store.index_document(document, [old_chunk])
        await vector_store.index_document(
            document.model_copy(update={"parser_version": "knowledge-v3"}),
            [new_chunk],
        )

        assert await vector_store.retrieve(
            "STALE-CHUNK-440",
            RunContext(),
        ) == []
        current = await vector_store.retrieve(
            "CURRENT-CHUNK-441",
            RunContext(),
        )
        assert len(current) == 1
        count = await client.count(
            collection_name="replacement_chunks",
            exact=True,
        )
        assert count.count == 1
    finally:
        await vector_store.close()


class _FailingVectorIndex:
    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: list[KnowledgeChunk],
    ) -> None:
        del document, chunks
        raise RuntimeError("index unavailable")

    async def archive_document(
        self,
        document_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> None:
        del document_id, tenant_id, project_id


@pytest.mark.asyncio
async def test_vector_index_failure_marks_document_failed(tmp_path: Path) -> None:
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    ingestion = KnowledgeIngestionService(repository, vector_index=_FailingVectorIndex())

    with pytest.raises(KnowledgeIndexError, match="index all document knowledge"):
        await ingestion.ingest(
            filename="failed.md",
            content=b"This document cannot reach the vector index.",
            media_type="text/markdown",
        )

    documents = await repository.list_documents(include_archived=True)
    assert len(documents) == 1
    assert documents[0].status == DocumentStatus.FAILED
    assert documents[0].error == "knowledge_index_failed"
    assert await repository.search(
        "vector index",
        tenant_id="local",
        project_id="default",
    ) == []


@pytest.mark.asyncio
async def test_qdrant_backend_is_wired_through_upload_and_agent_api(tmp_path: Path) -> None:
    components = build_components(
        Settings(
            app_env="test",
            data_dir=tmp_path,
            runtime_mode="offline",
            retrieval_backend="qdrant",
            qdrant_url=":memory:",
            embedding_provider="deterministic",
            embedding_dimensions=64,
        )
    )
    transport = ASGITransport(
        app=create_app(components.run_service, components.workspace_service)
    )
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            upload = await client.post(
                "/v1/projects/default/documents",
                files={
                    "file": (
                        "hybrid.md",
                        b"QUARTZ-HARBOR-287 requires three signed manifests.",
                        "text/markdown",
                    )
                },
            )
            run = await client.post(
                "/v1/projects/default/runs",
                json={
                    "input": "What does QUARTZ-HARBOR-287 require?",
                    "domain_pack": "general",
                },
            )
            overview = await client.get("/v1/workspace/overview")

        assert upload.status_code == 200
        citations = run.json()["answer"]["citations"]
        uploaded = next(
            item
            for item in citations
            if item["provenance"]["source_type"] == "uploaded_document"
        )
        assert uploaded["metadata"]["retrieval_backend"] == "qdrant_hybrid"
        assert uploaded["metadata"]["retrieval"]["branches"] == ["qdrant_hybrid"]
        assert overview.json()["retrieval_backend"] == "qdrant"
        assert overview.json()["embedding_provider"] == "deterministic"
        assert overview.json()["qdrant_collection"] == "hermesgraph_chunks"
        assert overview.json()["qdrant_sparse_idf"] is False
    finally:
        await components.close()
