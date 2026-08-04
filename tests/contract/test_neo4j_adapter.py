from types import SimpleNamespace
from typing import Any, cast

import pytest
from neo4j import AsyncDriver, Query, RoutingControl

from app.domain.enums import GraphCandidateStatus
from app.domain.models import (
    GraphEntityResolveRequest,
    GraphSearchRequest,
    KnowledgeChunk,
    KnowledgeDocument,
    RunContext,
    utc_now,
)
from app.graph.extraction import RuleBasedEntityRelationExtractor
from app.graph.neo4j import Neo4jEvidenceGraph
from app.graph.resolution import DeterministicEntityResolver


def _evidence() -> dict[str, Any]:
    return {
        "text": "LangChain connects deterministic retrieval dataflows.",
        "title": "Architecture",
        "score": 0.9,
        "provenance": {
            "source_type": "uploaded_document",
            "source_id": "doc-1:0",
            "content_hash": "a" * 64,
            "trust": "user_asserted",
        },
    }


def _path(project_id: str = "default") -> dict[str, Any]:
    return {
        "nodes": [
            {
                "node_id": "langchain",
                "tenant_id": "local",
                "project_id": project_id,
                "label": "Runtime",
                "name": "LangChain",
                "properties": {},
                "provenance": [],
            },
            {
                "node_id": "retrieval",
                "tenant_id": "local",
                "project_id": project_id,
                "label": "Capability",
                "name": "Hybrid Retrieval",
                "properties": {},
                "provenance": [],
            },
        ],
        "relationships": [
            {
                "relationship_id": "orchestrates",
                "tenant_id": "local",
                "project_id": project_id,
                "relation_type": "orchestrates",
                "source_node_id": "langchain",
                "target_node_id": "retrieval",
                "properties": {},
                "evidence": [_evidence()],
            }
        ],
    }


def _entity_match(project_id: str = "default", *, evidence: bool = True) -> dict[str, Any]:
    return {
        "node": {
            "node_id": "langchain",
            "tenant_id": "local",
            "project_id": project_id,
            "label": "Runtime",
            "name": "LangChain",
            "properties": {"aliases": ["LC"]},
            "provenance": [],
        },
        "matched_text": "lc",
        "matched_field": "alias",
        "score": 0.96,
        "evidence": [_evidence()] if evidence else [],
    }


class _FakeAsyncDriver:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def execute_query(self, query: Query, **kwargs: Any) -> Any:
        self.calls.append({"query": query, **kwargs})
        if "UNWIND $mentions" in str(query):
            return SimpleNamespace(
                records=[
                    _entity_match(),
                    _entity_match("other"),
                    _entity_match(evidence=False),
                ],
                summary=SimpleNamespace(result_available_after=5),
            )
        if "deleted_count" in str(query):
            return SimpleNamespace(
                records=[{"deleted_count": 3}],
                summary=SimpleNamespace(result_available_after=5),
            )
        if "stale_count" in str(query):
            return SimpleNamespace(
                records=[{"stale_count": 4}],
                summary=SimpleNamespace(result_available_after=5),
            )
        return SimpleNamespace(
            records=[_path(), _path("other")],
            summary=SimpleNamespace(result_available_after=7),
        )

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_neo4j_adapter_uses_allowlisted_parameters_and_rechecks_scope() -> None:
    fake = _FakeAsyncDriver()
    graph = Neo4jEvidenceGraph(cast(AsyncDriver, fake), timeout_seconds=3)
    malicious_entity = "LangChain') MATCH (secret) RETURN secret //"

    result = await graph.search_graph(
        GraphSearchRequest(
            entities=[malicious_entity],
            template="paths",
            max_hops=3,
        ),
        RunContext(),
    )

    assert len(result.paths) == 1
    assert len(result.evidence) == 1
    assert result.trace["rejected_scope_or_evidence"] == 1
    call = fake.calls[0]
    query_text = str(call["query"])
    assert "[:SEMANTIC_RELATION*1..3]" in query_text
    assert "$entities" in query_text
    assert malicious_entity not in query_text
    assert call["parameters_"]["entities"] == [malicious_entity.casefold()]
    assert call["parameters_"]["tenant_id"] == "local"
    assert call["parameters_"]["project_id"] == "default"
    assert call["routing_"] == RoutingControl.READ
    assert call["database_"] == "neo4j"

    await graph.verify_connectivity()
    await graph.close()
    connectivity = fake.calls[-1]
    assert "RETURN 1 AS ready" in str(connectivity["query"])
    assert connectivity["routing_"] == RoutingControl.READ
    assert connectivity["database_"] == "neo4j"
    assert fake.closed is True


@pytest.mark.asyncio
async def test_neo4j_entity_resolution_is_parameterized_scoped_and_evidence_backed() -> None:
    fake = _FakeAsyncDriver()
    graph = Neo4jEvidenceGraph(cast(AsyncDriver, fake), timeout_seconds=3)
    malicious_alias = "LC') MATCH (secret) RETURN secret //"

    result = await graph.resolve_graph_entities(
        GraphEntityResolveRequest(
            mentions=["LC", malicious_alias],
            entity_types=["Runtime"],
        ),
        RunContext(),
    )

    assert [match.node.node_id for match in result.matches] == ["langchain"]
    assert result.matches[0].matched_field == "alias"
    assert result.matches[0].evidence
    assert result.trace["rejected_scope_or_evidence"] == 2
    call = fake.calls[0]
    query_text = str(call["query"])
    assert "$mentions" in query_text
    assert "ORDER BY entity.node_id, score DESC" in query_text
    assert malicious_alias not in query_text
    assert call["parameters_"]["mentions"] == ["lc", malicious_alias.casefold()]
    assert call["parameters_"]["entity_types"] == ["runtime"]
    assert call["parameters_"]["tenant_id"] == "local"
    assert call["routing_"] == RoutingControl.READ


@pytest.mark.asyncio
async def test_neo4j_adapter_rejects_unknown_template_before_driver_call() -> None:
    fake = _FakeAsyncDriver()
    graph = Neo4jEvidenceGraph(cast(AsyncDriver, fake))

    with pytest.raises(ValueError, match="not allowlisted"):
        await graph.search_graph(
            GraphSearchRequest(entities=["LangChain"], template="free_cypher"),
            RunContext(),
        )

    assert fake.calls == []


def test_graph_search_request_rejects_blank_or_single_character_entities() -> None:
    with pytest.raises(ValueError, match="2 to 300"):
        GraphSearchRequest(entities=["   "])
    with pytest.raises(ValueError, match="2 to 300"):
        GraphSearchRequest(entities=["x"])


@pytest.mark.asyncio
async def test_neo4j_indexes_and_archives_structural_document_graph() -> None:
    fake = _FakeAsyncDriver()
    graph = Neo4jEvidenceGraph(cast(AsyncDriver, fake))
    document = KnowledgeDocument(
        filename="graph.md",
        title="Graph Fixture",
        media_type="text/markdown",
        byte_size=42,
        content_hash="b" * 64,
        storage_key="uploads/graph.md",
        chunk_count=1,
    )
    chunk = KnowledgeChunk(
        document_id=document.document_id,
        chunk_index=0,
        text="AURORA-GRAPH-301 requires a verified source.",
        content_hash="c" * 64,
        char_end=45,
    )

    await graph.index_document(document, [chunk])

    assert len(fake.calls) == 8
    index_call = fake.calls[-2]
    query_text = str(index_call["query"])
    assert "UNWIND $chunks AS item" in query_text
    assert "MERGE (document)-[relationship:HAS_CHUNK" in query_text
    assert chunk.text not in query_text
    assert index_call["routing_"] == RoutingControl.WRITE
    assert index_call["parameters_"]["chunks"][0]["chunk_id"] == str(chunk.chunk_id)
    assert index_call["parameters_"]["chunks"][0]["source_id"].endswith("#chunk=0")
    stale_call = fake.calls[-1]
    assert "NOT chunk.node_id IN $active_chunk_ids" in str(stale_call["query"])
    assert stale_call["parameters_"]["active_chunk_ids"] == [str(chunk.chunk_id)]

    await graph.archive_document(
        document.document_id,
        tenant_id="local",
        project_id="default",
    )
    structural_archive_call = fake.calls[-2]
    semantic_archive_call = fake.calls[-1]
    assert "relationship.status = 'archived'" in str(structural_archive_call["query"])
    assert "SEMANTIC_RELATION" in str(semantic_archive_call["query"])
    assert structural_archive_call["routing_"] == RoutingControl.WRITE
    assert semantic_archive_call["routing_"] == RoutingControl.WRITE


@pytest.mark.asyncio
async def test_neo4j_indexes_pending_semantic_candidates_and_applies_reviews() -> None:
    fake = _FakeAsyncDriver()
    graph = Neo4jEvidenceGraph(cast(AsyncDriver, fake))
    document = KnowledgeDocument(
        filename="semantic.md",
        title="Semantic Fixture",
        media_type="text/markdown",
        byte_size=52,
        content_hash="d" * 64,
        storage_key="uploads/semantic.md",
        chunk_count=1,
    )
    chunk = KnowledgeChunk(
        document_id=document.document_id,
        chunk_index=0,
        text="HermesGraph uses Qdrant for hybrid retrieval.",
        content_hash="e" * 64,
        char_end=46,
    )
    batch = await RuleBasedEntityRelationExtractor().extract(document, [chunk])

    await graph.index_extraction(batch)

    assert len(batch.relations) == 1
    stale_call = fake.calls[-3]
    entity_call = fake.calls[-2]
    relation_call = fake.calls[-1]
    assert "candidate_status = 'archived'" in str(stale_call["query"])
    assert stale_call["parameters_"]["active_entity_ids"] == [
        str(item.candidate_id) for item in batch.entities
    ]
    assert "UNWIND $entities AS item" in str(entity_call["query"])
    assert "SEMANTIC_RELATION" in str(relation_call["query"])
    assert all(
        item["graph_status"] == "candidate"
        for item in entity_call["parameters_"]["entities"]
    )
    relation_payload = relation_call["parameters_"]["relations"][0]
    assert relation_payload["relation_type"] == "uses"
    assert relation_payload["source_chunk_ids"] == [str(chunk.chunk_id)]
    assert chunk.text not in str(relation_call["query"])

    now = utc_now()
    approved_entities = [
        item.model_copy(
            update={
                "status": GraphCandidateStatus.APPROVED,
                "reviewed_by": "reviewer",
                "reviewed_at": now,
                "updated_at": now,
            }
        )
        for item in batch.entities
    ]
    for candidate in approved_entities:
        await graph.set_entity_status(candidate)
    approved_relation = batch.relations[0].model_copy(
        update={
            "status": GraphCandidateStatus.APPROVED,
            "reviewed_by": "reviewer",
            "reviewed_at": now,
            "updated_at": now,
        }
    )
    await graph.set_relation_status(approved_relation)

    assert fake.calls[-1]["parameters_"]["candidate_status"] == "approved"
    assert fake.calls[-1]["parameters_"]["graph_status"] == "active"
    assert "$candidate_id" in str(fake.calls[-1]["query"])

    second_document = KnowledgeDocument(
        filename="operations.md",
        title="Operations",
        media_type="text/markdown",
        byte_size=48,
        content_hash="f" * 64,
        storage_key="uploads/operations.md",
        chunk_count=1,
    )
    second_chunk = KnowledgeChunk(
        document_id=second_document.document_id,
        chunk_index=0,
        text="HermesGraph supports Neo4j.",
        content_hash="1" * 64,
        char_end=28,
    )
    second_batch = await RuleBasedEntityRelationExtractor().extract(
        second_document, [second_chunk]
    )
    resolutions = await DeterministicEntityResolver().propose(
        second_batch, batch.entities
    )
    resolution = next(
        item
        for item in resolutions
        if {item.left_name, item.right_name} == {"HermesGraph"}
    )

    await graph.index_resolutions([resolution])

    resolution_call = fake.calls[-1]
    assert "ENTITY_RESOLUTION" in str(resolution_call["query"])
    resolution_payload = resolution_call["parameters_"]["resolutions"][0]
    assert resolution_payload["graph_status"] == "candidate"
    assert resolution_payload["match_strategy"] == "exact_name"
    assert resolution_payload["source_chunk_ids"] == [
        str(item) for item in resolution.source_chunk_ids
    ]

    approved_resolution = resolution.model_copy(
        update={
            "status": GraphCandidateStatus.APPROVED,
            "reviewed_by": "reviewer",
            "reviewed_at": now,
            "updated_at": now,
        }
    )
    await graph.set_resolution_status(approved_resolution)

    assert "ENTITY_RESOLUTION" in str(fake.calls[-1]["query"])
    assert fake.calls[-1]["parameters_"]["graph_status"] == "active"

    deleted = await graph.prune_pending_resolutions(
        [resolution.candidate_id],
        tenant_id="local",
        project_id="default",
    )
    prune_call = fake.calls[-1]
    assert deleted == 3
    assert "$kept_candidate_ids" in str(prune_call["query"])
    assert prune_call["parameters_"]["kept_candidate_ids"] == [
        str(resolution.candidate_id)
    ]
    assert prune_call["parameters_"]["tenant_id"] == "local"
    assert prune_call["parameters_"]["project_id"] == "default"

    reconciled = await graph.reconcile_stale_pending_candidates(
        tenant_id="local",
        project_id="default",
        dry_run=True,
    )
    assert reconciled == (4, 4, 4)
    reconciliation_calls = fake.calls[-3:]
    assert all(call["parameters_"]["dry_run"] is True for call in reconciliation_calls)
    assert all(call["routing_"] == RoutingControl.WRITE for call in reconciliation_calls)
    assert "Entity" in str(reconciliation_calls[0]["query"])
    assert "SEMANTIC_RELATION" in str(reconciliation_calls[1]["query"])
    assert "ENTITY_RESOLUTION" in str(reconciliation_calls[2]["query"])
