from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from app.domain.models import AdaptiveRAGRoute, StrictModel

KnowledgeQueryRoute = Literal[
    "passage_lookup",
    "relationship",
    "global_summary",
]
KnowledgeTool = Literal["search_knowledge", "retrieve_evidence_subgraph"]
RouteConfidence = Literal["high", "medium"]


class KnowledgeQueryRouteDecision(StrictModel):
    """Bounded, content-free routing advice for the Hermes tool-selection layer."""

    route: KnowledgeQueryRoute
    primary_tool: KnowledgeTool
    fallback_tool: KnowledgeTool
    requires_graph: bool = False
    requires_multi_source: bool = False
    confidence: RouteConfidence = "medium"
    signals: list[str] = Field(default_factory=list, max_length=4)

    def as_instruction(self) -> str:
        signals = ",".join(self.signals) if self.signals else "default"
        return (
            "Current retrieval route (trusted application decision):\n"
            f"- route: {self.route}\n"
            f"- primary_tool: {self.primary_tool}\n"
            f"- fallback_tool: {self.fallback_tool}\n"
            f"- requires_graph: {str(self.requires_graph).lower()}\n"
            f"- requires_multi_source: {str(self.requires_multi_source).lower()}\n"
            f"- confidence: {self.confidence}\n"
            f"- signals: {signals}\n"
            "Use the primary tool unless the request clearly needs a more specific graph tool. "
            "Use the fallback only after a measured evidence gap; do not repeat the same search."
        )


def adaptive_route_instruction(route: AdaptiveRAGRoute) -> str:
    """Render a trusted Adaptive-RAG/Self-RAG execution contract for Hermes."""

    signals = ",".join(route.signals) if route.signals else "adaptive_model"
    lines = [
        "Current Adaptive-RAG route (trusted application decision):",
        f"- strategy: {route.strategy}",
        f"- route: {route.knowledge_route}",
        f"- requires_graph: {str(route.requires_graph).lower()}",
        f"- requires_multi_source: {str(route.requires_multi_source).lower()}",
        f"- self_reflection: {str(route.self_reflection).lower()}",
        f"- confidence: {route.confidence}",
        f"- signals: {signals}",
    ]
    if route.strategy == "no_retrieval":
        lines.append(
            "Do not call knowledge, graph, memory-recall, workspace-search, or web-search tools. "
            "Use only the explicit non-retrieval action tool needed by the request."
        )
    elif route.strategy == "single_step":
        lines.append(
            "Use one focused retrieval operation and answer from its evidence. Do not start a "
            "Self-RAG critique or corrective retrieval loop."
        )
    else:
        lines.append(
            "Self-RAG is enabled: after retrieval, judge evidence relevance and whether every "
            "material answer claim is supported. Perform at most one materially revised "
            "corrective retrieval when the evidence is insufficient, then publish with honest "
            "limitations if support is still incomplete."
        )
    return "\n".join(lines)


def public_adaptive_route(route: AdaptiveRAGRoute) -> dict[str, object]:
    """Project model routing without exposing free-form reasoning."""

    primary_tool: str | None = None
    fallback_tool: str | None = None
    if route.knowledge_route == "relationship":
        primary_tool = "retrieve_evidence_subgraph"
        fallback_tool = "search_knowledge"
    elif route.knowledge_route in {"passage_lookup", "global_summary"}:
        primary_tool = "search_knowledge"
        fallback_tool = "retrieve_evidence_subgraph"
    return {
        "route": route.knowledge_route,
        "strategy": route.strategy,
        "primary_tool": primary_tool,
        "fallback_tool": fallback_tool,
        "requires_graph": route.requires_graph,
        "requires_multi_source": route.requires_multi_source,
        "self_reflection": route.self_reflection,
        "confidence": route.confidence,
        "signals": route.signals,
    }


_RELATIONSHIP_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:relationship|relation|path|dependency|dependencies|depends|dependent|"
        r"connected|connection|upstream|downstream|belongs?\s+to|owned\s+by|"
        r"responsible\s+for|shared\s+(?:neighbor|dependency)|causal|lineage)\b",
        r"(?:关系|关联|路径|依赖|上下游|隶属|属于|归属|负责|参与|调用|连接|共同依赖|"
        r"共同邻居|因果|血缘|演化链|引用链)",
    )
)

_GLOBAL_SCOPE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:all|entire|overall|global|whole)\s+(?:documents?|reports?|projects?|"
        r"corpus|knowledge\s+base|dataset)\b",
        r"\bacross\s+(?:all|the)\s+(?:documents?|reports?|projects?|teams?|corpus)\b",
        r"(?:全部|所有|整个|全局|跨文档|跨报告|跨项目|跨团队).{0,12}"
        r"(?:文档|报告|项目|知识库|数据|团队|语料)?",
    )
)

_GLOBAL_TASK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:main|major|overall|recurring)\s+(?:themes?|trends?|directions?|patterns?)\b",
        r"\b(?:summari[sz]e|overview|synthesi[sz]e)\b",
        r"(?:总结|概括|综述|梳理|整体概览|总体概览|全局总结|总体总结|主要主题|技术趋势|"
        r"研发趋势|研发方向|整体趋势|共性问题|演化趋势|总体分布)",
    )
)


def route_knowledge_query(query: str) -> KnowledgeQueryRouteDecision:
    """Classify retrieval shape without sending the query through another model call.

    The router deliberately uses only bounded signal names in its output so user text cannot be
    reflected into the trusted instruction block passed to Hermes.
    """

    normalized = " ".join(str(query).split())
    if not normalized or len(normalized) > 2_000 or "\x00" in normalized:
        return _passage_decision(signals=["safe_default"])

    relationship = any(pattern.search(normalized) for pattern in _RELATIONSHIP_PATTERNS)
    global_scope = any(pattern.search(normalized) for pattern in _GLOBAL_SCOPE_PATTERNS)
    global_task = any(pattern.search(normalized) for pattern in _GLOBAL_TASK_PATTERNS)

    if global_scope and global_task:
        signals = ["global_scope", "global_task"]
        if relationship:
            signals.append("relationship")
        return KnowledgeQueryRouteDecision(
            route="global_summary",
            primary_tool=(
                "retrieve_evidence_subgraph" if relationship else "search_knowledge"
            ),
            fallback_tool=(
                "search_knowledge" if relationship else "retrieve_evidence_subgraph"
            ),
            requires_graph=relationship,
            requires_multi_source=True,
            confidence="high",
            signals=signals,
        )

    if relationship:
        return KnowledgeQueryRouteDecision(
            route="relationship",
            primary_tool="retrieve_evidence_subgraph",
            fallback_tool="search_knowledge",
            requires_graph=True,
            requires_multi_source=False,
            confidence="high",
            signals=["relationship"],
        )

    # A summary request without corpus-wide scope is intentionally treated as passage retrieval;
    # this prevents a request such as "summarize this report" from becoming a global search.
    return _passage_decision(
        signals=["local_summary"] if global_task else ["default_lookup"]
    )


def _passage_decision(*, signals: list[str]) -> KnowledgeQueryRouteDecision:
    return KnowledgeQueryRouteDecision(
        route="passage_lookup",
        primary_tool="search_knowledge",
        fallback_tool="retrieve_evidence_subgraph",
        requires_graph=False,
        requires_multi_source=False,
        confidence="medium",
        signals=signals,
    )
