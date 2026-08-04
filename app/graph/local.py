from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Literal

from app.domain.models import (
    EvidenceRef,
    GraphEntityMatch,
    GraphEntityResolveRequest,
    GraphEntityResolveResult,
    GraphNode,
    GraphPath,
    GraphRelationship,
    GraphSearchRequest,
    GraphSearchResult,
    RunContext,
)


class InMemoryEvidenceGraph:
    """Deterministic evidence-backed graph for local mode and contract tests."""

    _TEMPLATES = {"neighbors", "paths", "conflicts"}

    def __init__(
        self,
        nodes: Sequence[GraphNode],
        relationships: Sequence[GraphRelationship],
    ) -> None:
        self._nodes = {node.node_id: node for node in nodes}
        self._relationships = tuple(relationships)
        missing = {
            node_id
            for relationship in relationships
            for node_id in (relationship.source_node_id, relationship.target_node_id)
            if node_id not in self._nodes
        }
        if missing:
            raise ValueError(f"Relationships reference missing nodes: {sorted(missing)}")

    async def search_graph(
        self,
        request: GraphSearchRequest,
        context: RunContext,
    ) -> GraphSearchResult:
        if request.template not in self._TEMPLATES:
            raise ValueError(f"Graph template is not allowlisted: {request.template}")
        scoped_nodes = {
            node_id: node
            for node_id, node in self._nodes.items()
            if node.tenant_id == context.tenant_id and node.project_id == context.project_id
        }
        scoped_relationships = [
            relationship
            for relationship in self._relationships
            if relationship.tenant_id == context.tenant_id
            and relationship.project_id == context.project_id
            and relationship.source_node_id in scoped_nodes
            and relationship.target_node_id in scoped_nodes
        ]
        if request.template == "conflicts":
            scoped_relationships = [
                relationship
                for relationship in scoped_relationships
                if relationship.relation_type.casefold()
                in {"conflicts_with", "contradicts", "disputes"}
            ]
        starts = [
            node
            for node in scoped_nodes.values()
            if any(entity.casefold() in node.name.casefold() for entity in request.entities)
        ]
        paths: list[GraphPath] = []
        adjacency: dict[str, list[GraphRelationship]] = {}
        for relationship in scoped_relationships:
            adjacency.setdefault(relationship.source_node_id, []).append(relationship)
            adjacency.setdefault(relationship.target_node_id, []).append(relationship)

        hop_limit = 1 if request.template == "neighbors" else request.max_hops
        for start in starts:
            queue: deque[tuple[str, list[GraphNode], list[GraphRelationship]]] = deque(
                [(start.node_id, [start], [])]
            )
            visited_depth: dict[str, int] = {start.node_id: 0}
            while queue and len(paths) < request.limit:
                current_id, path_nodes, path_relationships = queue.popleft()
                depth = len(path_relationships)
                if depth > 0:
                    evidence = [
                        item
                        for relationship in path_relationships
                        for item in relationship.evidence
                    ]
                    paths.append(
                        GraphPath(
                            nodes=path_nodes,
                            relationships=path_relationships,
                            evidence=evidence,
                        )
                    )
                if depth >= hop_limit:
                    continue
                for relationship in adjacency.get(current_id, []):
                    next_id = (
                        relationship.target_node_id
                        if relationship.source_node_id == current_id
                        else relationship.source_node_id
                    )
                    next_depth = depth + 1
                    if visited_depth.get(next_id, hop_limit + 1) <= next_depth:
                        continue
                    visited_depth[next_id] = next_depth
                    queue.append(
                        (
                            next_id,
                            [*path_nodes, scoped_nodes[next_id]],
                            [*path_relationships, relationship],
                        )
                    )

        evidence_by_id = {item.evidence_id: item for path in paths for item in path.evidence}
        return GraphSearchResult(
            paths=paths[: request.limit],
            evidence=list(evidence_by_id.values()),
            trace={
                "backend": "in_memory_graph",
                "template": request.template,
                "start_nodes": len(starts),
                "scoped_nodes": len(scoped_nodes),
                "scoped_relationships": len(scoped_relationships),
            },
        )

    async def resolve_graph_entities(
        self,
        request: GraphEntityResolveRequest,
        context: RunContext,
    ) -> GraphEntityResolveResult:
        scoped_nodes = [
            node
            for node in self._nodes.values()
            if node.tenant_id == context.tenant_id and node.project_id == context.project_id
        ]
        type_filters = {item.casefold() for item in request.entity_types}
        matches: list[GraphEntityMatch] = []
        unbacked_matches = 0
        for node in scoped_nodes:
            if type_filters and node.label.casefold() not in type_filters:
                continue
            aliases = [
                str(item)
                for item in node.properties.get("aliases", [])
                if isinstance(item, str) and item.strip()
            ]
            best: tuple[float, str, str] | None = None
            for mention in request.mentions:
                candidate = self._match_node(node.name, aliases, mention)
                if candidate is not None and (best is None or candidate[0] > best[0]):
                    best = candidate
            if best is None or best[0] < request.min_score:
                continue
            evidence = self._node_evidence(node.node_id, context)
            if not evidence:
                unbacked_matches += 1
                continue
            matches.append(
                GraphEntityMatch(
                    node=node,
                    matched_text=best[1],
                    matched_field=best[2],
                    score=best[0],
                    evidence=evidence,
                )
            )
        matches.sort(key=lambda item: (-item.score, item.node.name.casefold(), item.node.node_id))
        selected = matches[: request.limit]
        evidence_by_id = {item.evidence_id: item for match in selected for item in match.evidence}
        return GraphEntityResolveResult(
            mentions=request.mentions,
            matches=selected,
            evidence=list(evidence_by_id.values()),
            trace={
                "backend": "in_memory_graph",
                "strategy": "canonical_alias_deterministic",
                "scoped_nodes": len(scoped_nodes),
                "returned_matches": len(selected),
                "unbacked_matches_rejected": unbacked_matches,
                "matches_truncated": max(len(matches) - len(selected), 0),
            },
        )

    def _node_evidence(self, node_id: str, context: RunContext) -> list[EvidenceRef]:
        evidence_by_id = {
            evidence.evidence_id: evidence
            for relationship in self._relationships
            if relationship.tenant_id == context.tenant_id
            and relationship.project_id == context.project_id
            and node_id in (relationship.source_node_id, relationship.target_node_id)
            for evidence in relationship.evidence
        }
        return list(evidence_by_id.values())

    @staticmethod
    def _match_node(
        canonical_name: str,
        aliases: list[str],
        mention: str,
    ) -> tuple[float, str, Literal["canonical_name", "alias"]] | None:
        normalized_mention = mention.casefold()
        normalized_name = canonical_name.casefold()
        if normalized_name == normalized_mention:
            return 1.0, mention, "canonical_name"
        if any(alias.casefold() == normalized_mention for alias in aliases):
            return 0.96, mention, "alias"
        if normalized_name in normalized_mention:
            return 0.90, mention, "canonical_name"
        if any(alias.casefold() in normalized_mention for alias in aliases):
            return 0.88, mention, "alias"
        if normalized_mention in normalized_name:
            return 0.78, mention, "canonical_name"
        if any(normalized_mention in alias.casefold() for alias in aliases):
            return 0.74, mention, "alias"
        return None
