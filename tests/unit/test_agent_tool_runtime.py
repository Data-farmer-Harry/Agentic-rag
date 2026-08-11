import asyncio
import json
from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.tools import tool

from app.capabilities import (
    AgentToolRuntime,
    Capability,
    CapabilityAlreadyRegisteredError,
    CapabilityError,
    CapabilityRegistry,
    CapabilityScopeError,
)
from app.capabilities.agent_tool_runtime import _bounded_sequence_result
from app.capabilities.capability_registry import serialized_json_size
from app.capabilities.langchain_adapters import capability_from_retriever, capability_from_tool
from app.domain.enums import CapabilityEffect, TrustLevel
from app.domain.models import (
    CapabilitySpec,
    EvidenceRef,
    GraphEntityCompareRequest,
    GraphEntityResolveRequest,
    GraphNode,
    GraphPath,
    GraphRAGRequest,
    GraphRAGResult,
    GraphRelationship,
    GraphSearchRequest,
    GraphSearchResult,
    Provenance,
    RetrievalBundle,
    RunContext,
    WebSearchRequest,
    WebSearchResult,
)
from app.graph import InMemoryEvidenceGraph
from app.retrieval import InMemoryRetriever, RetrievalPipeline


def capability(version: str) -> Capability:
    spec = CapabilitySpec(
        name="fixture_read",
        version=version,
        description="Read a deterministic fixture value",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        effect=CapabilityEffect.READ,
        required_scopes=["evidence:read"],
        provenance_required=False,
    )

    async def handler(payload: dict[str, Any], *_: Any) -> dict[str, Any]:
        return {"version": version, **payload}

    return Capability(spec, handler)


def test_capability_registry_resolves_versions_and_enforces_scopes() -> None:
    async def scenario() -> None:
        registry = CapabilityRegistry()
        registry.register(capability("1.0.0"))
        registry.register(capability("1.2.0"))

        assert registry.resolve("fixture_read").spec.version == "1.2.0"
        with pytest.raises(CapabilityAlreadyRegisteredError):
            registry.register(capability("1.2.0"))
        with pytest.raises(CapabilityScopeError, match="evidence:read"):
            await registry.invoke("fixture_read", {})

        output = await registry.invoke(
            "fixture_read",
            {"query": "offline"},
            granted_scopes=["evidence:read"],
        )
        assert output == {"version": "1.2.0", "query": "offline"}

    asyncio.run(scenario())


def test_capability_enforces_declared_json_schemas() -> None:
    async def scenario() -> None:
        registered = capability("1.0.0")
        with pytest.raises(CapabilityError, match="input schema"):
            await registered.ainvoke("not-an-object")  # type: ignore[arg-type]

    asyncio.run(scenario())


class MetadataCapture(BaseCallbackHandler):
    def __init__(self) -> None:
        self.metadata: list[dict[str, Any]] = []

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: Any,
        *,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        del serialized, inputs, kwargs
        if metadata:
            self.metadata.append(metadata)


def test_runnable_pipeline_normalizes_parallel_retrieval_rrf_and_metadata() -> None:
    async def scenario() -> None:
        shared = EvidenceRef(
            text="Agent retrieval uses reciprocal rank fusion.",
            provenance=Provenance(
                source_type="fixture",
                source_id="shared",
                content_hash="shared-hash",
                trust=TrustLevel.VERIFIED,
            ),
            metadata={"tenant_id": "tenant-a", "project_id": "project-a"},
        )
        dense = InMemoryRetriever(
            [
                shared,
                EvidenceRef(
                    text="Agent retrieval can use dense vectors.",
                    provenance=Provenance(source_type="fixture", source_id="dense"),
                    metadata={"tenant_id": "tenant-a", "project_id": "project-a"},
                ),
            ]
        )
        sparse = InMemoryRetriever(
            [
                shared,
                EvidenceRef(
                    text="Agent retrieval can use sparse terms.",
                    provenance=Provenance(source_type="fixture", source_id="sparse"),
                    metadata={"tenant_id": "tenant-a", "project_id": "project-a"},
                ),
            ]
        )
        callback = MetadataCapture()
        pipeline = RetrievalPipeline(
            {"dense": dense, "sparse": sparse},
            rrf_k=60,
            callbacks=[callback],
        )
        context = RunContext(tenant_id="tenant-a", project_id="project-a")

        bundle = await pipeline.retrieve("  Agent   retrieval  ", context, top_k=3)

        assert bundle.query == "Agent retrieval"
        assert len(bundle.evidence) == 3
        assert bundle.evidence[0].provenance.source_id == "shared"
        assert bundle.evidence[0].metadata["retrieval"]["branches"] == ["dense", "sparse"]
        assert bundle.trace["branch_counts"] == {"dense": 2, "sparse": 2}
        assert any(
            metadata.get("run_id") == str(context.run_id)
            and metadata.get("tenant_id") == "tenant-a"
            and metadata.get("project_id") == "project-a"
            for metadata in callback.metadata
        )

    asyncio.run(scenario())


def test_weighted_rrf_suppresses_weak_secondary_branch_noise() -> None:
    async def scenario() -> None:
        metadata = {"tenant_id": "local", "project_id": "default"}
        primary = InMemoryRetriever.from_texts(
            ["AURORA-VAULT-8301 requires a blue seal."],
            metadatas=[metadata | {"source_id": "uploaded"}],
        )
        secondary = InMemoryRetriever.from_texts(
            ["A generic architecture note explains what systems require."],
            metadatas=[metadata | {"source_id": "builtin"}],
        )
        pipeline = RetrievalPipeline(
            {"knowledge": primary, "builtin": secondary},
            branch_weights={"knowledge": 1.0, "builtin": 0.35},
            min_relative_score=0.45,
        )

        bundle = await pipeline.retrieve(
            "What does AURORA-VAULT-8301 require?",
            RunContext(),
        )

        assert [item.provenance.source_id for item in bundle.evidence] == ["uploaded"]
        assert bundle.trace["branch_weights"] == {"knowledge": 1.0, "builtin": 0.35}
        assert bundle.trace["min_relative_score"] == 0.45

    asyncio.run(scenario())


@tool
async def uppercase(value: str) -> str:
    """Convert a value to uppercase."""
    return value.upper()


class FixtureLangChainRetriever(BaseRetriever):
    documents: list[Document]

    def _get_relevant_documents(self, query: str, **kwargs: Any) -> list[Document]:
        del query, kwargs
        return self.documents


def test_langchain_tool_and_retriever_adapt_to_internal_capabilities() -> None:
    async def scenario() -> None:
        tool_capability = capability_from_tool(
            uppercase,
            spec=CapabilitySpec(
                name="uppercase",
                version="1.0.0",
                description="Uppercase a fixture string",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                output_schema={"type": "string"},
                effect=CapabilityEffect.READ,
                provenance_required=False,
            ),
        )
        assert await tool_capability.ainvoke({"value": "hermes"}) == "HERMES"

        retriever = FixtureLangChainRetriever(
            documents=[
                Document(
                    page_content="Offline LangChain retrieval result.",
                    metadata={
                        "source_id": "doc-1",
                        "source_type": "fixture",
                        "tenant_id": "local",
                        "project_id": "default",
                    },
                )
            ]
        )
        retriever_capability = capability_from_retriever(retriever, name="fixture_retriever")
        output = await retriever_capability.ainvoke(
            {"query": "offline", "top_k": 1},
            context=RunContext(),
        )

        assert isinstance(output, RetrievalBundle)
        assert output.evidence[0].provenance.source_id == "doc-1"
        assert retriever_capability.spec.output_schema == RetrievalBundle.model_json_schema()

    asyncio.run(scenario())


def test_retrieval_enforces_scope_and_records_partial_branch_failure() -> None:
    async def scenario() -> None:
        good = InMemoryRetriever.from_texts(
            ["Scoped evidence for retrieval."],
            metadatas=[
                {
                    "tenant_id": "tenant-a",
                    "project_id": "project-a",
                    "source_id": "scoped",
                }
            ],
        )

        class FailingRetriever:
            async def retrieve(self, query: str, **kwargs: Any) -> list[EvidenceRef]:
                del query, kwargs
                raise TimeoutError("fixture timeout")

        pipeline = RetrievalPipeline({"good": good, "bad": FailingRetriever()})
        context = RunContext(tenant_id="tenant-a", project_id="project-a")
        bundle = await pipeline.retrieve("retrieval", context)

        assert [item.provenance.source_id for item in bundle.evidence] == ["scoped"]
        assert bundle.applied_filters == {
            "tenant_id": "tenant-a",
            "project_id": "project-a",
        }
        assert bundle.trace["branch_errors"] == {"bad": "TimeoutError"}
        with pytest.raises(ValueError, match="cannot override"):
            await pipeline.retrieve(
                "retrieval",
                context,
                filters={"tenant_id": "tenant-b"},
            )

    asyncio.run(scenario())


def test_integration_runtime_controls_retrieval_and_graph_capabilities() -> None:
    async def scenario() -> None:
        evidence = EvidenceRef(
            text="LangChain is the integration runtime for retrieval and graph data flows.",
            provenance=Provenance(
                source_type="fixture",
                source_id="architecture",
                trust=TrustLevel.VERIFIED,
            ),
            metadata={"tenant_id": "tenant-a", "project_id": "project-a"},
        )
        retrieval = RetrievalPipeline({"lexical": InMemoryRetriever([evidence])})
        graph = InMemoryEvidenceGraph(
            nodes=[
                GraphNode(
                    node_id="langchain",
                    tenant_id="tenant-a",
                    project_id="project-a",
                    label="Runtime",
                    name="LangChain Integration Runtime",
                    properties={"aliases": ["LC Runtime"]},
                ),
                GraphNode(
                    node_id="retrieval",
                    tenant_id="tenant-a",
                    project_id="project-a",
                    label="Capability",
                    name="Knowledge Retrieval",
                ),
            ],
            relationships=[
                GraphRelationship(
                    relationship_id="orchestrates",
                    tenant_id="tenant-a",
                    project_id="project-a",
                    relation_type="orchestrates",
                    source_node_id="langchain",
                    target_node_id="retrieval",
                    evidence=[evidence],
                )
            ],
        )
        runtime = AgentToolRuntime(retrieval, graph=graph)
        context = RunContext(tenant_id="tenant-a", project_id="project-a")

        bundle = await runtime.retrieve("LangChain", context)
        graph_result = await runtime.search_graph(
            GraphSearchRequest(entities=["LangChain"], template="neighbors"),
            context,
        )
        resolved = await runtime.resolve_graph_entities(
            GraphEntityResolveRequest(mentions=["LC Runtime"]),
            context,
        )
        fused = await runtime.retrieve_evidence_subgraph(
            GraphRAGRequest(
                query="How does LangChain Integration Runtime handle retrieval?",
                seed_entities=["LC Runtime"],
                max_hops=2,
                top_k=5,
            ),
            context,
        )
        compared = await runtime.compare_graph_entities(
            GraphEntityCompareRequest(
                left_entity="LC Runtime",
                right_entity="Knowledge Retrieval",
            ),
            context,
        )

        assert bundle.evidence[0].provenance.source_id == "architecture"
        assert graph_result.paths[0].nodes[-1].node_id == "retrieval"
        assert resolved.matches[0].node.node_id == "langchain"
        assert resolved.matches[0].matched_field == "alias"
        assert resolved.matches[0].score == 0.96
        assert fused.trace["strategy"] == "vector_graph_evidence_fusion"
        assert fused.graph_paths[0].relationships[0].relationship_id == "orchestrates"
        assert len(fused.evidence) == 1
        assert compared.trace["connected"] is True
        assert compared.connecting_paths[0].nodes[-1].node_id == "retrieval"
        assert {spec.name for spec in runtime.registry.list_specs()} == {
            "compare_graph_entities",
            "resolve_graph_entities",
            "retrieve_evidence_subgraph",
            "search_graph",
            "search_knowledge",
        }
        with pytest.raises(CapabilityScopeError, match="graph:read"):
            await runtime.registry.invoke(
                "search_graph",
                {"entities": ["LangChain"]},
                context=context,
            )
        with pytest.raises(CapabilityScopeError, match="knowledge:read"):
            await runtime.registry.invoke(
                "retrieve_evidence_subgraph",
                {"query": "LangChain", "seed_entities": ["LC Runtime"]},
                granted_scopes=["graph:read"],
                context=context,
            )
        other_scope = RunContext(tenant_id="tenant-b", project_id="project-a")
        empty = await runtime.search_graph(
            GraphSearchRequest(entities=["LangChain"]),
            other_scope,
        )
        assert empty.paths == []

    asyncio.run(scenario())


def test_integration_runtime_bounds_large_graph_results_by_whole_path() -> None:
    async def scenario() -> None:
        evidence = EvidenceRef(
            text="Evidence payload " + ("x" * 2_000),
            provenance=Provenance(
                source_type="fixture",
                source_id="large-graph-evidence",
                content_hash="large-graph-hash",
                trust=TrustLevel.VERIFIED,
            ),
            metadata={"tenant_id": "tenant-a", "project_id": "project-a"},
        )

        class LargeGraph:
            async def search_graph(
                self,
                request: GraphSearchRequest,
                context: RunContext,
            ) -> GraphSearchResult:
                del request
                paths = [
                    GraphPath(
                        nodes=[
                            GraphNode(
                                node_id=f"node-{index}",
                                tenant_id=context.tenant_id,
                                project_id=context.project_id,
                                label="Service",
                                name=f"Service {index}",
                            )
                        ],
                        evidence=[
                            evidence.model_copy(
                                update={
                                    "provenance": evidence.provenance.model_copy(
                                        update={"source_id": f"large-{index}"}
                                    )
                                }
                            )
                        ],
                    )
                    for index in range(20)
                ]
                return GraphSearchResult(paths=paths, evidence=[evidence])

        runtime = AgentToolRuntime(
            InMemoryRetriever([]),
            graph=LargeGraph(),
            max_output_bytes=10_000,
        )
        result = await runtime.search_graph(
            GraphSearchRequest(entities=["Service"], limit=20),
            RunContext(tenant_id="tenant-a", project_id="project-a"),
        )

        assert serialized_json_size(result) <= 10_000
        assert len(result.paths) < 20
        assert result.trace["output_truncated"] is True
        assert result.trace["requested_path_count"] == 20
        assert result.trace["returned_paths"] == len(result.paths)
        assert result.trace["omitted_path_count"] == 20 - len(result.paths)

        raw_subgraph = GraphRAGResult(
            query="Service dependencies",
            graph_paths=[
                GraphPath(
                    nodes=[
                        GraphNode(
                            node_id=f"subgraph-{index}",
                            tenant_id="tenant-a",
                            project_id="project-a",
                            label="Service",
                            name=f"Subgraph Service {index}",
                        )
                    ],
                    evidence=[evidence],
                )
                for index in range(20)
            ],
            evidence=[evidence],
        )
        bounded_subgraph = _bounded_sequence_result(
            raw_subgraph,
            sequence_fields=("resolved_entities", "graph_paths", "evidence"),
            max_output_bytes=10_000,
        )
        assert serialized_json_size(bounded_subgraph) <= 10_000
        assert len(bounded_subgraph.graph_paths) < 20
        assert bounded_subgraph.trace["output_truncated"] is True
        assert bounded_subgraph.trace["requested_items"]["graph_paths"] == 20

    asyncio.run(scenario())


def test_integration_runtime_bounds_graph_outputs_with_capability_json_size() -> None:
    async def scenario() -> None:
        evidence = EvidenceRef(
            text="证据" * 1_500,
            provenance=Provenance(
                source_type="fixture",
                source_id="utf8-graph-evidence",
                content_hash="utf8-graph-hash",
                trust=TrustLevel.VERIFIED,
            ),
            metadata={"tenant_id": "tenant-a", "project_id": "project-a"},
        )
        alpha = GraphNode(
            node_id="alpha",
            tenant_id="tenant-a",
            project_id="project-a",
            label="Service",
            name="Alpha Service",
        )
        beta = GraphNode(
            node_id="beta",
            tenant_id="tenant-a",
            project_id="project-a",
            label="Service",
            name="Beta Service",
        )
        graph = InMemoryEvidenceGraph(
            nodes=[alpha, beta],
            relationships=[
                GraphRelationship(
                    relationship_id="alpha-beta",
                    tenant_id="tenant-a",
                    project_id="project-a",
                    relation_type="depends_on",
                    source_node_id=alpha.node_id,
                    target_node_id=beta.node_id,
                    evidence=[evidence],
                )
            ],
        )

        class StaticRetrieval:
            async def retrieve(
                self,
                query: str,
                context: RunContext,
                *,
                filters: dict[str, object] | None = None,
                top_k: int = 10,
            ) -> RetrievalBundle:
                del context, filters, top_k
                return RetrievalBundle(query=query, evidence=[evidence])

        context = RunContext(tenant_id="tenant-a", project_id="project-a")
        request = GraphSearchRequest(entities=["Alpha Service"], template="neighbors")
        raw_search = await graph.search_graph(request, context)
        compact_limit = len(
            json.dumps(
                raw_search.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        assert compact_limit < serialized_json_size(raw_search)

        boundary_runtime = AgentToolRuntime(
            StaticRetrieval(),
            graph=graph,
            max_output_bytes=compact_limit,
        )
        bounded_search = await boundary_runtime.search_graph(request, context)
        assert serialized_json_size(bounded_search) <= compact_limit
        assert bounded_search.trace["output_truncated"] is True

        runtime = AgentToolRuntime(
            StaticRetrieval(),
            graph=graph,
            max_output_bytes=1_000,
        )
        resolved = await runtime.resolve_graph_entities(
            GraphEntityResolveRequest(mentions=["Alpha Service"]),
            context,
        )
        long_query = "知识图谱" * 500
        subgraph = await runtime.retrieve_evidence_subgraph(
            GraphRAGRequest(query=long_query, seed_entities=["Alpha Service"]),
            context,
        )
        comparison = await runtime.compare_graph_entities(
            GraphEntityCompareRequest(
                left_entity="Alpha Service",
                right_entity="Beta Service",
            ),
            context,
        )

        for bounded in (resolved, subgraph, comparison):
            assert serialized_json_size(bounded) <= 1_000
            assert bounded.trace["output_truncated"] is True
        assert subgraph.query != long_query
        assert comparison.left_match is None
        assert comparison.right_match is None
        assert comparison.evidence == []

    asyncio.run(scenario())


def test_integration_runtime_controls_web_search_scope_and_contract() -> None:
    async def scenario() -> None:
        evidence = EvidenceRef(
            text="A cited public web result.",
            provenance=Provenance(
                source_type="web_search",
                source_id="https://example.com/source",
                trust=TrustLevel.UNTRUSTED,
            ),
        )

        class FixtureWebSearch:
            async def search_web(
                self,
                request: WebSearchRequest,
                context: RunContext,
            ) -> WebSearchResult:
                assert context.tenant_id == "tenant-a"
                return WebSearchResult(
                    query=request.query,
                    summary=evidence.text,
                    evidence=[evidence],
                )

        runtime = AgentToolRuntime(
            RetrievalPipeline({"lexical": InMemoryRetriever([])}),
            web_search=FixtureWebSearch(),
        )
        context = RunContext(tenant_id="tenant-a", project_id="project-a")

        result = await runtime.search_web(
            WebSearchRequest(query="current public fact"),
            context,
        )

        assert result.evidence == [evidence]
        assert runtime.web_search_enabled is True
        assert {spec.name for spec in runtime.registry.list_specs()} == {
            "search_knowledge",
            "search_web",
        }
        with pytest.raises(CapabilityScopeError, match="web:read"):
            await runtime.registry.invoke(
                "search_web",
                {"query": "current public fact"},
                context=context,
            )

    asyncio.run(scenario())
