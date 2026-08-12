from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field

from app.capabilities.capability_registry import (
    Capability,
    CapabilityRegistry,
    serialized_json_size,
)
from app.domain.contracts import (
    ComputerWorkspacePort,
    GeneralToolsPort,
    GraphEntityResolutionPort,
    GraphRetrievalToolPort,
    GraphSearchPort,
    RetrievalPort,
    WebSearchPort,
)
from app.domain.enums import CapabilityEffect, RetryOwner
from app.domain.models import (
    CalculationRequest,
    CalculationResult,
    CapabilitySpec,
    CurrentTimeRequest,
    CurrentTimeResult,
    GraphEntityCompareRequest,
    GraphEntityCompareResult,
    GraphEntityResolveRequest,
    GraphEntityResolveResult,
    GraphPath,
    GraphRAGRequest,
    GraphRAGResult,
    GraphSearchRequest,
    GraphSearchResult,
    RetrievalBundle,
    RunContext,
    WebPageReadRequest,
    WebPageReadResult,
    WebSearchRequest,
    WebSearchResult,
    WorkspaceFileReadRequest,
    WorkspaceFileReadResult,
    WorkspaceListRequest,
    WorkspaceListResult,
    WorkspaceSearchRequest,
    WorkspaceSearchResult,
)
from app.graph.graph_retrieval_tools import GraphRetrievalToolkit


class _RetrievalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2_000)
    filters: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=10, ge=1, le=50)


class AgentToolRuntime(
    RetrievalPort,
    GraphSearchPort,
    GraphRetrievalToolPort,
    WebSearchPort,
    ComputerWorkspacePort,
    GeneralToolsPort,
):
    """The policy boundary between the Agent SDK and integration/data pipelines.

    LangChain-powered pipelines and infrastructure adapters are registered as typed
    capabilities here. Agent tools never invoke a retriever or graph driver directly.
    """

    KNOWLEDGE_SCOPE = "knowledge:read"
    GRAPH_SCOPE = "graph:read"
    WEB_SCOPE = "web:read"
    COMPUTER_SCOPE = "computer:read"
    UTILITY_SCOPE = "utility:execute"

    def __init__(
        self,
        retrieval: RetrievalPort,
        *,
        graph: GraphSearchPort | None = None,
        web_search: WebSearchPort | None = None,
        general_tools: GeneralToolsPort | None = None,
        workspace: ComputerWorkspacePort | None = None,
        registry: CapabilityRegistry | None = None,
        timeout_seconds: int = 30,
        web_timeout_seconds: int | None = None,
        max_output_bytes: int = 100_000,
    ) -> None:
        self._retrieval = retrieval
        self._graph = graph
        self._web_search = web_search
        self._general_tools = general_tools
        self._workspace = workspace
        self._graph_toolkit: GraphRetrievalToolkit | None = None
        if graph is not None and callable(getattr(graph, "resolve_graph_entities", None)):
            self._graph_toolkit = GraphRetrievalToolkit(
                retrieval=retrieval,
                graph_search=graph,
                entity_resolution=cast(GraphEntityResolutionPort, graph),
            )
        self.registry = registry or CapabilityRegistry()
        self._register_retrieval(timeout_seconds, max_output_bytes)
        if graph is not None:
            self._register_graph(timeout_seconds, max_output_bytes)
        if web_search is not None:
            self._register_web_search(
                web_timeout_seconds or timeout_seconds,
                max_output_bytes,
            )
        if general_tools is not None:
            self._register_general_tools(
                web_timeout_seconds or timeout_seconds,
                max_output_bytes,
            )
        if workspace is not None:
            self._register_workspace(timeout_seconds, max_output_bytes)

    @property
    def graph_enabled(self) -> bool:
        return self._graph is not None

    @property
    def graph_tooling_enabled(self) -> bool:
        return self._graph_toolkit is not None

    @property
    def web_search_enabled(self) -> bool:
        return self._web_search is not None

    @property
    def computer_workspace_enabled(self) -> bool:
        return self._workspace is not None

    @property
    def general_tools_enabled(self) -> bool:
        return self._general_tools is not None

    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> RetrievalBundle:
        result = await self.registry.invoke(
            "search_knowledge",
            {"query": query, "filters": filters or {}, "top_k": top_k},
            granted_scopes=(self.KNOWLEDGE_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return RetrievalBundle.model_validate(result)

    async def search_graph(
        self,
        request: GraphSearchRequest,
        context: RunContext,
    ) -> GraphSearchResult:
        if self._graph is None:
            raise RuntimeError("Graph search is not configured")
        result = await self.registry.invoke(
            "search_graph",
            request.model_dump(mode="json"),
            granted_scopes=(self.GRAPH_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return GraphSearchResult.model_validate(result)

    async def resolve_graph_entities(
        self,
        request: GraphEntityResolveRequest,
        context: RunContext,
    ) -> GraphEntityResolveResult:
        self._require_graph_toolkit()
        result = await self.registry.invoke(
            "resolve_graph_entities",
            request.model_dump(mode="json"),
            granted_scopes=(self.GRAPH_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return GraphEntityResolveResult.model_validate(result)

    async def retrieve_evidence_subgraph(
        self,
        request: GraphRAGRequest,
        context: RunContext,
    ) -> GraphRAGResult:
        self._require_graph_toolkit()
        result = await self.registry.invoke(
            "retrieve_evidence_subgraph",
            request.model_dump(mode="json"),
            granted_scopes=(self.GRAPH_SCOPE, self.KNOWLEDGE_SCOPE),
            context=context,
            metadata=self._metadata(context),
        )
        return GraphRAGResult.model_validate(result)

    async def compare_graph_entities(
        self,
        request: GraphEntityCompareRequest,
        context: RunContext,
    ) -> GraphEntityCompareResult:
        self._require_graph_toolkit()
        result = await self.registry.invoke(
            "compare_graph_entities",
            request.model_dump(mode="json"),
            granted_scopes=(self.GRAPH_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return GraphEntityCompareResult.model_validate(result)

    async def search_web(
        self,
        request: WebSearchRequest,
        context: RunContext,
    ) -> WebSearchResult:
        if self._web_search is None:
            raise RuntimeError("Web search is not configured")
        result = await self.registry.invoke(
            "search_web",
            request.model_dump(mode="json"),
            granted_scopes=(self.WEB_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return WebSearchResult.model_validate(result)

    async def read_web_page(
        self,
        request: WebPageReadRequest,
        context: RunContext,
    ) -> WebPageReadResult:
        self._require_general_tools()
        result = await self.registry.invoke(
            "read_web_page",
            request.model_dump(mode="json"),
            granted_scopes=(self.WEB_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return WebPageReadResult.model_validate(result)

    async def calculate(
        self,
        request: CalculationRequest,
        context: RunContext,
    ) -> CalculationResult:
        self._require_general_tools()
        result = await self.registry.invoke(
            "calculate",
            request.model_dump(mode="json"),
            granted_scopes=(self.UTILITY_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return CalculationResult.model_validate(result)

    async def current_time(
        self,
        request: CurrentTimeRequest,
        context: RunContext,
    ) -> CurrentTimeResult:
        self._require_general_tools()
        result = await self.registry.invoke(
            "current_time",
            request.model_dump(mode="json"),
            granted_scopes=(self.UTILITY_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return CurrentTimeResult.model_validate(result)

    async def list_workspace_files(
        self,
        request: WorkspaceListRequest,
        context: RunContext,
    ) -> WorkspaceListResult:
        self._require_workspace()
        result = await self.registry.invoke(
            "list_workspace_files",
            request.model_dump(mode="json"),
            granted_scopes=(self.COMPUTER_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return WorkspaceListResult.model_validate(result)

    async def read_workspace_file(
        self,
        request: WorkspaceFileReadRequest,
        context: RunContext,
    ) -> WorkspaceFileReadResult:
        self._require_workspace()
        result = await self.registry.invoke(
            "read_workspace_file",
            request.model_dump(mode="json"),
            granted_scopes=(self.COMPUTER_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return WorkspaceFileReadResult.model_validate(result)

    async def search_workspace_files(
        self,
        request: WorkspaceSearchRequest,
        context: RunContext,
    ) -> WorkspaceSearchResult:
        self._require_workspace()
        result = await self.registry.invoke(
            "search_workspace_files",
            request.model_dump(mode="json"),
            granted_scopes=(self.COMPUTER_SCOPE,),
            context=context,
            metadata=self._metadata(context),
        )
        return WorkspaceSearchResult.model_validate(result)

    async def invoke(
        self,
        name: str,
        payload: Mapping[str, Any],
        *,
        context: RunContext,
        granted_scopes: Sequence[str],
        version: str | None = None,
    ) -> Any:
        """Invoke domain-pack capabilities through the same control boundary."""
        return await self.registry.invoke(
            name,
            payload,
            version=version,
            granted_scopes=granted_scopes,
            context=context,
            metadata=self._metadata(context),
        )

    def _register_retrieval(self, timeout_seconds: int, max_output_bytes: int) -> None:
        async def handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> RetrievalBundle:
            del metadata
            if context is None:
                raise ValueError("search_knowledge requires RunContext")
            request = _RetrievalRequest.model_validate(payload)
            return await self._retrieval.retrieve(
                request.query,
                context,
                filters=request.filters,
                top_k=request.top_k,
            )

        self.registry.register(
            Capability(
                spec=CapabilitySpec(
                    name="search_knowledge",
                    version="1.0.0",
                    description="Search scoped keyword, vector, graph, and metadata sources.",
                    input_schema=_RetrievalRequest.model_json_schema(),
                    output_schema=RetrievalBundle.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.KNOWLEDGE_SCOPE],
                    timeout_seconds=timeout_seconds,
                    retry_owner=RetryOwner.INTEGRATION_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=True,
                ),
                handler=handler,
            )
        )

    def _register_graph(self, timeout_seconds: int, max_output_bytes: int) -> None:
        graph = self._graph
        if graph is None:
            return

        async def handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> GraphSearchResult:
            del metadata
            if context is None:
                raise ValueError("search_graph requires RunContext")
            request = GraphSearchRequest.model_validate(payload)
            request = request.model_copy(
                update={"max_hops": _clamped_hop_count(request.max_hops, context)}
            )
            result = await graph.search_graph(request, context)
            return _bounded_graph_search_result(result, max_output_bytes=max_output_bytes)

        self.registry.register(
            Capability(
                spec=CapabilitySpec(
                    name="search_graph",
                    version="1.0.0",
                    description="Traverse an evidence-backed graph through allowlisted templates.",
                    input_schema=GraphSearchRequest.model_json_schema(),
                    output_schema=GraphSearchResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.GRAPH_SCOPE],
                    timeout_seconds=timeout_seconds,
                    retry_owner=RetryOwner.INTEGRATION_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=True,
                ),
                handler=handler,
            )
        )
        toolkit = self._graph_toolkit
        if toolkit is None:
            return

        async def resolve_handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> GraphEntityResolveResult:
            del metadata
            if context is None:
                raise ValueError("resolve_graph_entities requires RunContext")
            request = GraphEntityResolveRequest.model_validate(payload)
            result = await toolkit.resolve_graph_entities(request, context)
            return _bounded_sequence_result(
                result,
                sequence_fields=("matches", "evidence"),
                scalar_sequence_fields=("mentions",),
                max_output_bytes=max_output_bytes,
            )

        async def subgraph_handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> GraphRAGResult:
            del metadata
            if context is None:
                raise ValueError("retrieve_evidence_subgraph requires RunContext")
            request = GraphRAGRequest.model_validate(payload)
            request = request.model_copy(
                update={"max_hops": _clamped_hop_count(request.max_hops, context)}
            )
            result = await toolkit.retrieve_evidence_subgraph(request, context)
            return _bounded_sequence_result(
                result,
                sequence_fields=("resolved_entities", "graph_paths", "evidence"),
                text_fields=("query",),
                max_output_bytes=max_output_bytes,
            )

        async def compare_handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> GraphEntityCompareResult:
            del metadata
            if context is None:
                raise ValueError("compare_graph_entities requires RunContext")
            request = GraphEntityCompareRequest.model_validate(payload)
            request = request.model_copy(
                update={"max_hops": _clamped_hop_count(request.max_hops, context)}
            )
            result = await toolkit.compare_graph_entities(request, context)
            return _bounded_sequence_result(
                result,
                sequence_fields=(
                    "connecting_paths",
                    "shared_neighbors",
                    "left_only_neighbors",
                    "right_only_neighbors",
                    "evidence",
                ),
                singleton_fields=("left_match", "right_match"),
                scalar_sequence_fields=("unresolved_entities",),
                max_output_bytes=max_output_bytes,
            )

        graph_tool_specs: tuple[tuple[CapabilitySpec, Any], ...] = (
            (
                CapabilitySpec(
                    name="resolve_graph_entities",
                    version="1.0.0",
                    description=(
                        "Resolve canonical graph entities from names or aliases with "
                        "deterministic scores and source evidence."
                    ),
                    input_schema=GraphEntityResolveRequest.model_json_schema(),
                    output_schema=GraphEntityResolveResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.GRAPH_SCOPE],
                    timeout_seconds=timeout_seconds,
                    retry_owner=RetryOwner.INTEGRATION_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=True,
                ),
                resolve_handler,
            ),
            (
                CapabilitySpec(
                    name="retrieve_evidence_subgraph",
                    version="1.0.0",
                    description=(
                        "Fuse scoped text retrieval with an evidence-backed multi-hop "
                        "knowledge subgraph."
                    ),
                    input_schema=GraphRAGRequest.model_json_schema(),
                    output_schema=GraphRAGResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.GRAPH_SCOPE, self.KNOWLEDGE_SCOPE],
                    timeout_seconds=timeout_seconds,
                    retry_owner=RetryOwner.INTEGRATION_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=True,
                ),
                subgraph_handler,
            ),
            (
                CapabilitySpec(
                    name="compare_graph_entities",
                    version="1.0.0",
                    description=(
                        "Compare two resolved entities through connecting paths and "
                        "shared or exclusive evidence-backed neighbors."
                    ),
                    input_schema=GraphEntityCompareRequest.model_json_schema(),
                    output_schema=GraphEntityCompareResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.GRAPH_SCOPE],
                    timeout_seconds=timeout_seconds,
                    retry_owner=RetryOwner.INTEGRATION_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=True,
                ),
                compare_handler,
            ),
        )
        for spec, handler in graph_tool_specs:
            self.registry.register(Capability(spec=spec, handler=handler))

    def _register_web_search(self, timeout_seconds: int, max_output_bytes: int) -> None:
        web_search = self._web_search
        if web_search is None:
            return

        async def handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> WebSearchResult:
            del metadata
            if context is None:
                raise ValueError("search_web requires RunContext")
            request = WebSearchRequest.model_validate(payload)
            return await web_search.search_web(request, context)

        self.registry.register(
            Capability(
                spec=CapabilitySpec(
                    name="search_web",
                    version="1.0.0",
                    description=(
                        "Search the public web through a hosted provider and return "
                        "run-scoped URL-cited evidence."
                    ),
                    input_schema=WebSearchRequest.model_json_schema(),
                    output_schema=WebSearchResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.WEB_SCOPE],
                    timeout_seconds=timeout_seconds,
                    retry_owner=RetryOwner.INTEGRATION_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=True,
                ),
                handler=handler,
            )
        )

    def _register_general_tools(self, timeout_seconds: int, max_output_bytes: int) -> None:
        general_tools = self._general_tools
        if general_tools is None:
            return

        async def read_page_handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> WebPageReadResult:
            del metadata
            if context is None:
                raise ValueError("read_web_page requires RunContext")
            return await general_tools.read_web_page(
                WebPageReadRequest.model_validate(payload), context
            )

        async def calculate_handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> CalculationResult:
            del metadata
            if context is None:
                raise ValueError("calculate requires RunContext")
            return await general_tools.calculate(
                CalculationRequest.model_validate(payload), context
            )

        async def time_handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> CurrentTimeResult:
            del metadata
            if context is None:
                raise ValueError("current_time requires RunContext")
            return await general_tools.current_time(
                CurrentTimeRequest.model_validate(payload), context
            )

        for spec, handler in (
            (
                CapabilitySpec(
                    name="read_web_page",
                    version="1.0.0",
                    description=(
                        "Read visible text from a public HTTP(S) page as untrusted evidence."
                    ),
                    input_schema=WebPageReadRequest.model_json_schema(),
                    output_schema=WebPageReadResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.WEB_SCOPE],
                    timeout_seconds=timeout_seconds,
                    retry_owner=RetryOwner.INTEGRATION_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=True,
                ),
                read_page_handler,
            ),
            (
                CapabilitySpec(
                    name="calculate",
                    version="1.0.0",
                    description="Evaluate a bounded arithmetic expression deterministically.",
                    input_schema=CalculationRequest.model_json_schema(),
                    output_schema=CalculationResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.UTILITY_SCOPE],
                    timeout_seconds=min(timeout_seconds, 5),
                    retry_owner=RetryOwner.AGENT_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=False,
                ),
                calculate_handler,
            ),
            (
                CapabilitySpec(
                    name="current_time",
                    version="1.0.0",
                    description="Return the current time in an IANA timezone.",
                    input_schema=CurrentTimeRequest.model_json_schema(),
                    output_schema=CurrentTimeResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.UTILITY_SCOPE],
                    timeout_seconds=min(timeout_seconds, 5),
                    retry_owner=RetryOwner.AGENT_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=False,
                ),
                time_handler,
            ),
        ):
            self.registry.register(Capability(spec=spec, handler=handler))

    def _register_workspace(self, timeout_seconds: int, max_output_bytes: int) -> None:
        workspace = self._workspace
        if workspace is None:
            return

        async def list_handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> WorkspaceListResult:
            del metadata
            if context is None:
                raise ValueError("list_workspace_files requires RunContext")
            return await workspace.list_workspace_files(
                WorkspaceListRequest.model_validate(payload),
                context,
            )

        async def read_handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> WorkspaceFileReadResult:
            del metadata
            if context is None:
                raise ValueError("read_workspace_file requires RunContext")
            return await workspace.read_workspace_file(
                WorkspaceFileReadRequest.model_validate(payload),
                context,
            )

        async def search_handler(
            payload: Mapping[str, Any],
            context: RunContext | None,
            metadata: Mapping[str, Any],
        ) -> WorkspaceSearchResult:
            del metadata
            if context is None:
                raise ValueError("search_workspace_files requires RunContext")
            return await workspace.search_workspace_files(
                WorkspaceSearchRequest.model_validate(payload),
                context,
            )

        for spec, handler in (
            (
                CapabilitySpec(
                    name="list_workspace_files",
                    version="1.0.0",
                    description=(
                        "List safe files under an explicitly allowlisted, read-only "
                        "computer workspace root."
                    ),
                    input_schema=WorkspaceListRequest.model_json_schema(),
                    output_schema=WorkspaceListResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.COMPUTER_SCOPE],
                    timeout_seconds=timeout_seconds,
                    retry_owner=RetryOwner.INTEGRATION_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=False,
                ),
                list_handler,
            ),
            (
                CapabilitySpec(
                    name="read_workspace_file",
                    version="1.0.0",
                    description=(
                        "Read a bounded text, code, PDF, DOCX, or XLSX segment from an "
                        "allowlisted computer workspace and return run-scoped evidence."
                    ),
                    input_schema=WorkspaceFileReadRequest.model_json_schema(),
                    output_schema=WorkspaceFileReadResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.COMPUTER_SCOPE],
                    timeout_seconds=timeout_seconds,
                    retry_owner=RetryOwner.INTEGRATION_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=True,
                ),
                read_handler,
            ),
            (
                CapabilitySpec(
                    name="search_workspace_files",
                    version="1.0.0",
                    description=(
                        "Search bounded, supported files inside an allowlisted computer "
                        "workspace and return line-level run-scoped evidence."
                    ),
                    input_schema=WorkspaceSearchRequest.model_json_schema(),
                    output_schema=WorkspaceSearchResult.model_json_schema(),
                    effect=CapabilityEffect.READ,
                    required_scopes=[self.COMPUTER_SCOPE],
                    timeout_seconds=timeout_seconds,
                    retry_owner=RetryOwner.INTEGRATION_RUNTIME,
                    max_output_bytes=max_output_bytes,
                    provenance_required=True,
                ),
                search_handler,
            ),
        ):
            self.registry.register(Capability(spec=spec, handler=handler))

    async def close(self) -> None:
        seen: set[int] = set()
        for backend in (
            self._retrieval,
            self._graph,
            self._web_search,
            self._general_tools,
        ):
            if backend is None or id(backend) in seen:
                continue
            seen.add(id(backend))
            close = getattr(backend, "close", None)
            if close is not None:
                await close()

    def _require_graph_toolkit(self) -> GraphRetrievalToolkit:
        if self._graph_toolkit is None:
            raise RuntimeError("Graph retrieval tools are not configured")
        return self._graph_toolkit

    def _require_workspace(self) -> ComputerWorkspacePort:
        if self._workspace is None:
            raise RuntimeError("Computer workspace tools are not configured")
        return self._workspace

    def _require_general_tools(self) -> GeneralToolsPort:
        if self._general_tools is None:
            raise RuntimeError("General tools are not configured")
        return self._general_tools

    @staticmethod
    def _metadata(context: RunContext) -> dict[str, str]:
        return {
            "run_id": str(context.run_id),
            "tenant_id": context.tenant_id,
            "project_id": context.project_id,
            "domain_pack": context.domain_pack,
        }


def _clamped_hop_count(
    requested: int,
    context: RunContext,
) -> int:
    policy = context.execution_policy
    if policy is None or not policy.behavior_applied or policy.graph_hop_cap is None:
        return requested
    return min(requested, policy.graph_hop_cap)


def _bounded_graph_search_result(
    result: GraphSearchResult,
    *,
    max_output_bytes: int,
) -> GraphSearchResult:
    """Keep whole evidence-backed paths while enforcing the capability byte budget."""

    if serialized_json_size(result) <= max_output_bytes:
        return result

    original_path_count = len(result.paths)
    selected: list[GraphPath] = []
    bounded = result.model_copy(
        update={
            "paths": [],
            "evidence": [],
            "trace": _graph_search_truncation_trace(original_path_count, 0),
        }
    )
    for path in result.paths:
        trial_paths = [*selected, path]
        evidence_by_id = {
            item.evidence_id: item for candidate in trial_paths for item in candidate.evidence
        }
        trial = result.model_copy(
            update={
                "paths": trial_paths,
                "evidence": list(evidence_by_id.values()),
                "trace": _graph_search_truncation_trace(
                    original_path_count,
                    len(trial_paths),
                ),
            }
        )
        if serialized_json_size(trial) <= max_output_bytes:
            selected.append(path)
            bounded = trial
    return bounded


def _graph_search_truncation_trace(
    requested_path_count: int,
    returned_path_count: int,
) -> dict[str, int | bool]:
    return {
        "output_truncated": True,
        "requested_path_count": requested_path_count,
        "returned_paths": returned_path_count,
        "omitted_path_count": requested_path_count - returned_path_count,
    }


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _bounded_sequence_result(
    result: _ModelT,
    *,
    sequence_fields: Sequence[str],
    singleton_fields: Sequence[str] = (),
    scalar_sequence_fields: Sequence[str] = (),
    text_fields: Sequence[str] = (),
    max_output_bytes: int,
) -> _ModelT:
    """Bound graph-tool output using Capability's exact JSON serialization contract."""

    if serialized_json_size(result) <= max_output_bytes:
        return result
    collection_fields = tuple(dict.fromkeys((*sequence_fields, *scalar_sequence_fields)))
    requested = {
        field: len(cast(Sequence[Any], getattr(result, field))) for field in collection_fields
    }
    requested.update(
        {
            field: int(getattr(result, field) is not None)
            for field in dict.fromkeys(singleton_fields)
        }
    )
    returned = {field: 0 for field in requested}
    bounded = result.model_copy(
        update={
            **{field: [] for field in collection_fields},
            **{field: None for field in dict.fromkeys(singleton_fields)},
            "trace": _sequence_truncation_trace(requested, returned),
        }
    )
    bounded = _truncate_text_fields(
        bounded,
        text_fields=text_fields,
        max_output_bytes=max_output_bytes,
    )
    for field in dict.fromkeys(singleton_fields):
        item = getattr(result, field)
        if item is None:
            continue
        trial_returned = {**returned, field: 1}
        trial = bounded.model_copy(
            update={
                field: item,
                "trace": _sequence_truncation_trace(requested, trial_returned),
            }
        )
        if serialized_json_size(trial) <= max_output_bytes:
            bounded = trial
            returned = trial_returned
    for field in collection_fields:
        selected: list[Any] = []
        for item in cast(Sequence[Any], getattr(result, field)):
            trial_returned = {**returned, field: len(selected) + 1}
            trial = bounded.model_copy(
                update={
                    field: [*selected, item],
                    "trace": _sequence_truncation_trace(requested, trial_returned),
                }
            )
            if serialized_json_size(trial) <= max_output_bytes:
                selected.append(item)
                bounded = trial
        returned[field] = len(selected)
    return bounded


def _sequence_truncation_trace(
    requested: Mapping[str, int],
    returned: Mapping[str, int],
) -> dict[str, Any]:
    return {
        "output_truncated": True,
        "requested_items": dict(requested),
        "returned_items": dict(returned),
    }


def _truncate_text_fields(
    result: _ModelT,
    *,
    text_fields: Sequence[str],
    max_output_bytes: int,
) -> _ModelT:
    bounded = result
    for field in text_fields:
        if serialized_json_size(bounded) <= max_output_bytes:
            break
        value = getattr(bounded, field)
        if not isinstance(value, str):
            continue
        empty = bounded.model_copy(update={field: ""})
        if serialized_json_size(empty) > max_output_bytes:
            bounded = empty
            continue
        low, high = 0, len(value)
        best = empty
        while low <= high:
            length = (low + high) // 2
            suffix = "" if length == len(value) else "..."
            trial = bounded.model_copy(update={field: value[:length] + suffix})
            if serialized_json_size(trial) <= max_output_bytes:
                best = trial
                low = length + 1
            else:
                high = length - 1
        bounded = best
    return bounded
