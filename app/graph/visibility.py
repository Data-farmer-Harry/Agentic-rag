"""Graph result policy projection for the same user/layer context as retrieval."""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from typing import cast

from app.domain.contracts import GraphEntityResolutionPort, GraphSearchPort
from app.domain.models import (
    EvidenceRef,
    GraphEntityResolveRequest,
    GraphEntityResolveResult,
    GraphPath,
    GraphSearchRequest,
    GraphSearchResult,
    RunContext,
)
from app.knowledge.visibility import evidence_is_visible


class VisibilityFilteredGraph(
    GraphSearchPort,
    GraphEntityResolutionPort,
):
    """Filters graph evidence server-side without changing the graph schema.

    The underlying graph remains scope-aware (tenant/project); this adapter adds
    the personal and enabled-layer policy before any result reaches a graph tool
    or the answer publisher.
    """

    def __init__(self, graph: GraphSearchPort) -> None:
        self._graph = graph

    async def search_graph(
        self,
        request: GraphSearchRequest,
        context: RunContext,
    ) -> GraphSearchResult:
        result = await self._graph.search_graph(request, context)
        if context.enabled_knowledge_layers is None:
            return result.model_copy(
                update={
                    "paths": [],
                    "evidence": [],
                    "trace": {
                        **result.trace,
                        "visibility_policy": "missing_layer_context_fail_closed",
                        "visible_path_count": 0,
                    },
                }
            )
        paths = [
            filtered
            for path in result.paths
            if (filtered := _filter_path(path, context)) is not None
        ]
        evidence = _dedupe(item for path in paths for item in path.evidence)
        return result.model_copy(
            update={
                "paths": paths,
                "evidence": evidence,
                "trace": {
                    **result.trace,
                    "visibility_policy": "server_layer_projection",
                    "visible_path_count": len(paths),
                },
            }
        )

    async def resolve_graph_entities(
        self,
        request: GraphEntityResolveRequest,
        context: RunContext,
    ) -> GraphEntityResolveResult:
        resolver = cast(GraphEntityResolutionPort, self._graph)
        result = await resolver.resolve_graph_entities(request, context)
        if context.enabled_knowledge_layers is None:
            return result.model_copy(
                update={
                    "matches": [],
                    "evidence": [],
                    "trace": {
                        **result.trace,
                        "visibility_policy": "missing_layer_context_fail_closed",
                        "visible_match_count": 0,
                    },
                }
            )
        matches = []
        for match in result.matches:
            evidence = _visible_evidence(match.evidence, context)
            if evidence:
                matches.append(match.model_copy(update={"evidence": evidence}))
        return result.model_copy(
            update={
                "matches": matches,
                "evidence": _dedupe(item for match in matches for item in match.evidence),
                "trace": {
                    **result.trace,
                    "visibility_policy": "server_layer_projection",
                    "visible_match_count": len(matches),
                },
            }
        )

    async def close(self) -> None:
        close = getattr(self._graph, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result


def _filter_path(path: GraphPath, context: RunContext) -> GraphPath | None:
    relationships = []
    for relationship in path.relationships:
        evidence = _visible_evidence(relationship.evidence, context)
        if not evidence:
            return None
        relationships.append(relationship.model_copy(update={"evidence": evidence}))
    evidence = _dedupe(
        [
            *_visible_evidence(path.evidence, context),
            *(item for relationship in relationships for item in relationship.evidence),
        ]
    )
    if not relationships or not evidence:
        return None
    return path.model_copy(update={"relationships": relationships, "evidence": evidence})


def _visible_evidence(
    evidence: Iterable[EvidenceRef],
    context: RunContext,
) -> list[EvidenceRef]:
    layers = context.enabled_knowledge_layers
    if layers is None:
        return []
    return [
        item
        for item in evidence
        if evidence_is_visible(
            item.metadata,
            user_id=context.user_id,
            enabled_layers=layers,
        )
    ]


def _dedupe(evidence: Iterable[EvidenceRef]) -> list[EvidenceRef]:
    by_id = {str(item.evidence_id): item for item in evidence}
    return list(by_id.values())
