import pytest

from app.domain.enums import TrustLevel
from app.domain.models import (
    EvidenceRef,
    GraphEntityCompareRequest,
    GraphEntityResolveRequest,
    GraphNode,
    GraphRAGRequest,
    GraphRelationship,
    Provenance,
    RunContext,
)
from app.graph import InMemoryEvidenceGraph
from app.graph.toolkit import GraphRetrievalToolkit
from app.retrieval import InMemoryRetriever, RetrievalPipeline


def _evidence(source_id: str, text: str) -> EvidenceRef:
    return EvidenceRef(
        text=text,
        score=0.9,
        provenance=Provenance(
            source_type="fixture",
            source_id=source_id,
            trust=TrustLevel.VERIFIED,
        ),
        metadata={"tenant_id": "local", "project_id": "default"},
    )


@pytest.mark.asyncio
async def test_graph_toolkit_fuses_evidence_and_compares_backed_topology() -> None:
    text_evidence = _evidence("text", "Alpha and Beta use a shared runtime.")
    graph_evidence = _evidence("graph", "The graph topology is source backed.")
    graph_evidence = graph_evidence.model_copy(
        update={"metadata": graph_evidence.metadata | {"chunk_id": "graph-chunk"}}
    )
    vector_duplicate = graph_evidence.model_copy(
        update={
            "provenance": graph_evidence.provenance.model_copy(
                update={"source_id": "vector-copy"}
            )
        }
    )
    nodes = [
        GraphNode(
            node_id="alpha",
            tenant_id="local",
            project_id="default",
            label="System",
            name="Alpha System",
            properties={"aliases": ["Alpha"]},
        ),
        GraphNode(
            node_id="beta",
            tenant_id="local",
            project_id="default",
            label="System",
            name="Beta System",
            properties={"aliases": ["Beta"]},
        ),
        GraphNode(
            node_id="shared",
            tenant_id="local",
            project_id="default",
            label="Runtime",
            name="Shared Runtime",
        ),
        GraphNode(
            node_id="left-only",
            tenant_id="local",
            project_id="default",
            label="Capability",
            name="Alpha Capability",
        ),
        GraphNode(
            node_id="right-only",
            tenant_id="local",
            project_id="default",
            label="Capability",
            name="Beta Capability",
        ),
        GraphNode(
            node_id="unbacked",
            tenant_id="local",
            project_id="default",
            label="Claim",
            name="Unsupported Claim",
        ),
        GraphNode(
            node_id="alpha-note",
            tenant_id="local",
            project_id="default",
            label="Chunk",
            name="Alpha System note",
        ),
        GraphNode(
            node_id="noise",
            tenant_id="local",
            project_id="default",
            label="Document",
            name="Noise Document",
        ),
    ]
    relationships = [
        GraphRelationship(
            relationship_id="alpha-shared",
            tenant_id="local",
            project_id="default",
            relation_type="uses",
            source_node_id="alpha",
            target_node_id="shared",
            evidence=[graph_evidence],
        ),
        GraphRelationship(
            relationship_id="beta-shared",
            tenant_id="local",
            project_id="default",
            relation_type="uses",
            source_node_id="beta",
            target_node_id="shared",
            evidence=[graph_evidence],
        ),
        GraphRelationship(
            relationship_id="alpha-capability",
            tenant_id="local",
            project_id="default",
            relation_type="provides",
            source_node_id="alpha",
            target_node_id="left-only",
            evidence=[graph_evidence],
        ),
        GraphRelationship(
            relationship_id="beta-capability",
            tenant_id="local",
            project_id="default",
            relation_type="provides",
            source_node_id="beta",
            target_node_id="right-only",
            evidence=[graph_evidence],
        ),
        GraphRelationship(
            relationship_id="unsupported",
            tenant_id="local",
            project_id="default",
            relation_type="claims",
            source_node_id="alpha",
            target_node_id="unbacked",
            evidence=[],
        ),
        GraphRelationship(
            relationship_id="structural-noise",
            tenant_id="local",
            project_id="default",
            relation_type="HAS_CHUNK",
            source_node_id="noise",
            target_node_id="alpha-note",
            evidence=[graph_evidence],
        ),
    ]
    graph = InMemoryEvidenceGraph(nodes, relationships)
    toolkit = GraphRetrievalToolkit(
        retrieval=RetrievalPipeline(
            {"fixture": InMemoryRetriever([text_evidence])}
        ),
        graph_search=graph,
        entity_resolution=graph,
    )
    context = RunContext()

    assert GraphRetrievalToolkit._deduplicate_evidence(
        [graph_evidence, vector_duplicate]
    ) == [graph_evidence]
    resolved = await toolkit.resolve_graph_entities(
        GraphEntityResolveRequest(mentions=["Alpha", "Alpha System"]),
        context,
    )
    fused = await toolkit.retrieve_evidence_subgraph(
        GraphRAGRequest(
            query="Compare Alpha and Beta architecture",
            seed_entities=["Alpha"],
            entity_types=["System"],
            max_hops=2,
        ),
        context,
    )
    compared = await toolkit.compare_graph_entities(
        GraphEntityCompareRequest(left_entity="Alpha", right_entity="Beta"),
        context,
    )

    assert resolved.matches[0].node.node_id == "alpha"
    assert resolved.matches[0].score == 1.0
    assert {item.provenance.source_id for item in fused.evidence} == {"text", "graph"}
    assert all(
        relationship.relationship_id != "unsupported"
        for path in fused.graph_paths
        for relationship in path.relationships
    )
    assert all(
        "alpha" in {node.node_id for node in path.nodes} for path in fused.graph_paths
    )
    assert fused.trace["rejected_unanchored_paths"] == 1
    assert [node.node_id for node in compared.shared_neighbors] == ["shared"]
    assert [node.node_id for node in compared.left_only_neighbors] == ["left-only"]
    assert [node.node_id for node in compared.right_only_neighbors] == ["right-only"]
    assert any(
        [node.node_id for node in path.nodes] == ["alpha", "shared", "beta"]
        for path in compared.connecting_paths
    )
    assert compared.trace["connected"] is True
