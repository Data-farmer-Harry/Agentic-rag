from __future__ import annotations

import asyncio
from collections.abc import Iterable

from app.domain.contracts import GraphEntityResolutionPort, GraphSearchPort, RetrievalPort
from app.domain.models import (
    EvidenceRef,
    GraphEntityCompareRequest,
    GraphEntityCompareResult,
    GraphEntityMatch,
    GraphEntityResolveRequest,
    GraphEntityResolveResult,
    GraphNode,
    GraphPath,
    GraphRAGRequest,
    GraphRAGResult,
    GraphSearchRequest,
    RunContext,
)


class GraphRetrievalToolkit:
    """Evidence-constrained GraphRAG operations exposed as semantic tools."""

    def __init__(
        self,
        *,
        retrieval: RetrievalPort,
        graph_search: GraphSearchPort,
        entity_resolution: GraphEntityResolutionPort,
    ) -> None:
        self._retrieval = retrieval
        self._graph_search = graph_search
        self._entity_resolution = entity_resolution

    async def resolve_graph_entities(
        self,
        request: GraphEntityResolveRequest,
        context: RunContext,
    ) -> GraphEntityResolveResult:
        return await self._entity_resolution.resolve_graph_entities(request, context)

    async def retrieve_evidence_subgraph(
        self,
        request: GraphRAGRequest,
        context: RunContext,
    ) -> GraphRAGResult:
        mentions = request.seed_entities or [request.query]
        resolved, retrieval_bundle = await asyncio.gather(
            self.resolve_graph_entities(
                GraphEntityResolveRequest(
                    mentions=mentions,
                    entity_types=request.entity_types,
                    limit=min(max(len(mentions) * 3, 10), 50),
                ),
                context,
            ),
            self._retrieval.retrieve(
                request.query,
                context,
                top_k=request.top_k,
            ),
        )
        graph_seeds = self._canonical_seeds(resolved.matches)
        if not graph_seeds:
            graph_seeds = list(request.seed_entities)

        paths: list[GraphPath] = []
        rejected_unanchored_paths = 0
        if graph_seeds:
            graph_result = await self._graph_search.search_graph(
                GraphSearchRequest(
                    entities=graph_seeds[:10],
                    template="paths",
                    max_hops=request.max_hops,
                    limit=request.path_limit,
                ),
                context,
            )
            backed_paths = self._evidence_backed_paths(graph_result.paths, context)
            resolved_node_ids = {match.node.node_id for match in resolved.matches}
            paths = [
                path
                for path in backed_paths
                if not resolved_node_ids
                or resolved_node_ids & {node.node_id for node in path.nodes}
            ]
            rejected_unanchored_paths = len(backed_paths) - len(paths)

        evidence = self._deduplicate_evidence(
            [
                *retrieval_bundle.evidence,
                *resolved.evidence,
                *(item for path in paths for item in path.evidence),
            ]
        )
        return GraphRAGResult(
            query=request.query,
            resolved_entities=resolved.matches,
            graph_paths=paths,
            evidence=evidence,
            trace={
                "strategy": "vector_graph_evidence_fusion",
                "graph_seed_names": graph_seeds[:10],
                "resolved_entity_count": len(resolved.matches),
                "text_evidence_count": len(retrieval_bundle.evidence),
                "graph_path_count": len(paths),
                "rejected_unanchored_paths": rejected_unanchored_paths,
                "fused_evidence_count": len(evidence),
                "max_hops": request.max_hops,
                "scope": {
                    "tenant_id": context.tenant_id,
                    "project_id": context.project_id,
                },
                "graph_trace": resolved.trace,
                "retrieval_trace": retrieval_bundle.trace,
            },
        )

    async def compare_graph_entities(
        self,
        request: GraphEntityCompareRequest,
        context: RunContext,
    ) -> GraphEntityCompareResult:
        resolved = await self.resolve_graph_entities(
            GraphEntityResolveRequest(
                mentions=[request.left_entity, request.right_entity],
                limit=20,
            ),
            context,
        )
        left = self._best_match(resolved.matches, request.left_entity)
        right = self._best_match(resolved.matches, request.right_entity)
        unresolved = [
            mention
            for mention, match in (
                (request.left_entity, left),
                (request.right_entity, right),
            )
            if match is None
        ]
        if left is None or right is None or left.node.node_id == right.node.node_id:
            if left is not None and right is not None:
                unresolved = [request.right_entity]
            return GraphEntityCompareResult(
                left_match=left,
                right_match=right,
                unresolved_entities=unresolved,
                evidence=self._deduplicate_evidence(resolved.evidence),
                trace={
                    "strategy": "evidence_backed_entity_comparison",
                    "connected": False,
                    "reason": "entity_resolution_incomplete",
                },
            )

        left_paths = await self._neighbor_paths(left, request.limit, context)
        right_paths = await self._neighbor_paths(right, request.limit, context)
        candidate_connections = await self._graph_search.search_graph(
            GraphSearchRequest(
                entities=[left.node.name],
                template="paths",
                max_hops=request.max_hops,
                limit=request.limit,
            ),
            context,
        )
        connecting_paths = [
            path
            for path in self._evidence_backed_paths(candidate_connections.paths, context)
            if {left.node.node_id, right.node.node_id} <= {node.node_id for node in path.nodes}
        ]

        left_neighbors = self._neighbor_nodes(left_paths, left.node.node_id)
        right_neighbors = self._neighbor_nodes(right_paths, right.node.node_id)
        shared_ids = left_neighbors.keys() & right_neighbors.keys()
        excluded_ids = {left.node.node_id, right.node.node_id}
        shared = [left_neighbors[node_id] for node_id in sorted(shared_ids - excluded_ids)]
        left_only = [
            left_neighbors[node_id]
            for node_id in sorted(left_neighbors.keys() - right_neighbors.keys() - excluded_ids)
        ]
        right_only = [
            right_neighbors[node_id]
            for node_id in sorted(right_neighbors.keys() - left_neighbors.keys() - excluded_ids)
        ]
        evidence = self._deduplicate_evidence(
            [
                *resolved.evidence,
                *(
                    item
                    for path in [*left_paths, *right_paths, *connecting_paths]
                    for item in path.evidence
                ),
            ]
        )
        return GraphEntityCompareResult(
            left_match=left,
            right_match=right,
            connecting_paths=connecting_paths,
            shared_neighbors=shared,
            left_only_neighbors=left_only,
            right_only_neighbors=right_only,
            evidence=evidence,
            trace={
                "strategy": "evidence_backed_entity_comparison",
                "connected": bool(connecting_paths),
                "connection_path_count": len(connecting_paths),
                "shared_neighbor_count": len(shared),
                "max_hops": request.max_hops,
                "scope": {
                    "tenant_id": context.tenant_id,
                    "project_id": context.project_id,
                },
            },
        )

    async def _neighbor_paths(
        self,
        match: GraphEntityMatch,
        limit: int,
        context: RunContext,
    ) -> list[GraphPath]:
        result = await self._graph_search.search_graph(
            GraphSearchRequest(
                entities=[match.node.name],
                template="neighbors",
                max_hops=1,
                limit=limit,
            ),
            context,
        )
        return [
            path
            for path in self._evidence_backed_paths(result.paths, context)
            if match.node.node_id in {node.node_id for node in path.nodes}
        ]

    @staticmethod
    def _best_match(
        matches: list[GraphEntityMatch],
        mention: str,
    ) -> GraphEntityMatch | None:
        exact = [match for match in matches if match.matched_text.casefold() == mention.casefold()]
        return max(exact, key=lambda item: item.score, default=None)

    @staticmethod
    def _canonical_seeds(matches: list[GraphEntityMatch]) -> list[str]:
        return list(dict.fromkeys(match.node.name for match in matches))

    @staticmethod
    def _neighbor_nodes(paths: list[GraphPath], origin_id: str) -> dict[str, GraphNode]:
        return {
            node.node_id: node for path in paths for node in path.nodes if node.node_id != origin_id
        }

    @staticmethod
    def _evidence_backed_paths(
        paths: Iterable[GraphPath],
        context: RunContext,
    ) -> list[GraphPath]:
        return [
            path
            for path in paths
            if path.relationships
            and all(
                node.tenant_id == context.tenant_id and node.project_id == context.project_id
                for node in path.nodes
            )
            and all(
                relationship.tenant_id == context.tenant_id
                and relationship.project_id == context.project_id
                and relationship.evidence
                for relationship in path.relationships
            )
        ]

    @staticmethod
    def _deduplicate_evidence(items: Iterable[EvidenceRef]) -> list[EvidenceRef]:
        unique: dict[tuple[str, str, str | None, str], EvidenceRef] = {}
        for item in items:
            chunk_id = item.metadata.get("chunk_id")
            key = (
                "chunk" if chunk_id else item.provenance.source_type,
                str(chunk_id or item.provenance.source_id),
                item.provenance.content_hash,
                item.text,
            )
            current = unique.get(key)
            if current is None or item.score > current.score:
                unique[key] = item
        return list(unique.values())
