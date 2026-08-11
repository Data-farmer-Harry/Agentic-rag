from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import (
    Runnable,
    RunnableConfig,
    RunnableLambda,
    RunnableParallel,
    RunnablePassthrough,
)

from app.capabilities.langchain_callbacks import (
    BaseCallbackHandler,
    build_runnable_config,
    require_langchain,
)
from app.domain.enums import TrustLevel
from app.domain.models import EvidenceRef, Provenance, RetrievalBundle, RunContext
from app.knowledge.knowledge_visibility import (
    WorkspaceProfileResolver,
    evidence_is_visible,
    knowledge_layer_priority,
)


class RetrievalPipeline:
    """LangChain Runnable pipeline: normalize, parallel retrieve, RRF/dedup, bundle."""

    def __init__(
        self,
        retrievers: Mapping[str, Any],
        *,
        rrf_k: int = 60,
        branch_weights: Mapping[str, float] | None = None,
        min_relative_score: float = 0.0,
        callbacks: Sequence[BaseCallbackHandler] = (),
        workspace_profiles: WorkspaceProfileResolver | None = None,
    ) -> None:
        require_langchain()
        if not retrievers:
            raise ValueError("At least one retriever is required")
        if rrf_k < 1:
            raise ValueError("rrf_k must be positive")
        if not 0.0 <= min_relative_score <= 1.0:
            raise ValueError("min_relative_score must be between 0 and 1")
        self._retrievers = dict(retrievers)
        self._rrf_k = rrf_k
        requested_weights = dict(branch_weights or {})
        unknown_weights = set(requested_weights) - set(self._retrievers)
        if unknown_weights:
            raise ValueError(f"Weights reference unknown branches: {sorted(unknown_weights)}")
        self._branch_weights = {
            name: float(requested_weights.get(name, 1.0)) for name in self._retrievers
        }
        if any(weight <= 0 for weight in self._branch_weights.values()):
            raise ValueError("All branch weights must be positive")
        self._min_relative_score = min_relative_score
        self._callbacks = tuple(callbacks)
        self._workspace_profiles = workspace_profiles

        branches = {
            name: RunnableLambda(self._branch(retriever))
            for name, retriever in self._retrievers.items()
        }
        self.runnable: Runnable[Any, RetrievalBundle] = (
            RunnableLambda(self._normalize)
            | RunnableParallel(
                request=RunnablePassthrough(),
                results=RunnableParallel(branches),
            )
            | RunnableLambda(self._fuse)
        )

    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> RetrievalBundle:
        if not 1 <= top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        requested_filters = dict(filters or {})
        for key in ("user_id", "knowledge_layer", "enabled_knowledge_layers"):
            if key in requested_filters:
                raise ValueError(f"Caller cannot select server-owned visibility field: {key}")
        enforced_scope = {
            "tenant_id": context.tenant_id,
            "project_id": context.project_id,
        }
        for key, value in enforced_scope.items():
            if key in requested_filters and requested_filters[key] != value:
                raise ValueError(f"Caller cannot override enforced filter: {key}")
        effective_filters = {**requested_filters, **enforced_scope}
        profile = (
            self._workspace_profiles.resolve(
                tenant_id=context.tenant_id,
                project_id=context.project_id,
            )
            if self._workspace_profiles is not None
            else None
        )

        config = build_runnable_config(
            context,
            callbacks=self._callbacks,
            metadata={
                "component": "retrieval_pipeline",
                "retrievers": list(self._retrievers),
            },
            tags=("hermesgraph", "retrieval"),
        )
        return await self.runnable.ainvoke(
            {
                "query": query,
                "context": context,
                "filters": effective_filters,
                "visibility": (
                    {
                        "user_id": context.user_id,
                        "enabled_layers": [
                            layer.value for layer in profile.enabled_knowledge_layers
                        ],
                    }
                    if profile is not None
                    else None
                ),
                "top_k": top_k,
            },
            config=config,
        )

    @staticmethod
    def _normalize(value: str | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(value, str):
            value = {"query": value}
        query = " ".join(str(value.get("query", "")).split())
        if not query:
            raise ValueError("query must not be empty")
        top_k = int(value.get("top_k", 10))
        if top_k < 1:
            raise ValueError("top_k must be positive")
        context = value.get("context")
        if context is not None and not isinstance(context, RunContext):
            context = RunContext.model_validate(context)
        return {
            "query": query,
            "context": context,
            "filters": dict(value.get("filters") or {}),
            "visibility": value.get("visibility"),
            "top_k": top_k,
        }

    def _branch(self, retriever: Any) -> Any:
        async def run(request: Mapping[str, Any], config: RunnableConfig) -> dict[str, Any]:
            try:
                result = await _invoke_retriever(retriever, request, config)
                items = normalize_retrieval_results(result)
                scoped = [
                    item
                    for item in items
                    if all(
                        item.metadata.get(key) == value for key, value in request["filters"].items()
                    )
                ]
                visibility = request.get("visibility")
                if isinstance(visibility, Mapping):
                    user_id = visibility.get("user_id")
                    enabled_layers = visibility.get("enabled_layers")
                    if not isinstance(user_id, str) or not isinstance(
                        enabled_layers,
                        Sequence,
                    ):
                        return {"items": [], "error": "InvalidVisibilityProjection"}
                    scoped = [
                        item
                        for item in scoped
                        if evidence_is_visible(
                            item.metadata,
                            user_id=user_id,
                            enabled_layers=[str(layer) for layer in enabled_layers],
                        )
                    ]
                return {"items": scoped, "error": None}
            except Exception as exc:
                return {"items": [], "error": type(exc).__name__}

        return run

    def _fuse(self, value: Mapping[str, Any]) -> RetrievalBundle:
        request = value["request"]
        results = value["results"]
        top_k = request["top_k"]
        scores: dict[str, float] = {}
        selected: dict[str, EvidenceRef] = {}
        ranks: dict[str, dict[str, int]] = {}

        for branch, branch_result in results.items():
            items = branch_result["items"]
            branch_weight = self._branch_weights[branch]
            seen_in_branch: set[str] = set()
            for rank, item in enumerate(items, start=1):
                identity = evidence_identity(item)
                if identity in seen_in_branch:
                    continue
                seen_in_branch.add(identity)
                scores[identity] = scores.get(identity, 0.0) + branch_weight / (
                    self._rrf_k + rank
                )
                selected.setdefault(identity, item)
                ranks.setdefault(identity, {})[branch] = rank

        ordered = sorted(
            scores,
            key=lambda key: (
                -scores[key],
                knowledge_layer_priority(selected[key].metadata.get("knowledge_layer")),
                key,
            ),
        )
        if ordered and self._min_relative_score:
            cutoff = scores[ordered[0]] * self._min_relative_score
            ordered = [identity for identity in ordered if scores[identity] >= cutoff]
        ordered = ordered[:top_k]
        evidence: list[EvidenceRef] = []
        for identity in ordered:
            item = selected[identity]
            metadata = dict(item.metadata)
            metadata["retrieval"] = {
                "branches": sorted(ranks[identity]),
                "ranks": ranks[identity],
                "rrf_score": scores[identity],
            }
            evidence.append(
                item.model_copy(update={"score": scores[identity], "metadata": metadata})
            )

        return RetrievalBundle(
            query=request["query"],
            evidence=evidence,
            applied_filters=request["filters"],
            trace={
                "pipeline": "langchain_runnable",
                "retrievers": list(self._retrievers),
                "branch_counts": {name: len(result["items"]) for name, result in results.items()},
                "branch_errors": {
                    name: result["error"]
                    for name, result in results.items()
                    if result["error"] is not None
                },
                "rrf_k": self._rrf_k,
                "branch_weights": self._branch_weights,
                "min_relative_score": self._min_relative_score,
                "deduplicated_count": len(scores),
            },
        )


LangChainRetrievalPipeline = RetrievalPipeline


async def _invoke_retriever(
    retriever: Any,
    request: Mapping[str, Any],
    config: RunnableConfig,
) -> Any:
    if isinstance(retriever, BaseRetriever):
        return await retriever.ainvoke(request["query"], config=config)

    method = getattr(retriever, "retrieve", None)
    if method is not None:
        parameters = inspect.signature(method).parameters
        kwargs: dict[str, Any] = {}
        if "context" in parameters:
            kwargs["context"] = request["context"]
        if "filters" in parameters:
            kwargs["filters"] = request["filters"]
        if "top_k" in parameters:
            kwargs["top_k"] = request["top_k"]
        result = method(request["query"], **kwargs)
        return await result if inspect.isawaitable(result) else result

    method = getattr(retriever, "ainvoke", None)
    if method is not None:
        return await method(request["query"], config=config)

    raise TypeError(
        "Retriever must implement retrieve(...) or be a LangChain BaseRetriever/Runnable"
    )


def normalize_retrieval_results(value: Any) -> list[EvidenceRef]:
    if isinstance(value, RetrievalBundle):
        value = value.evidence
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        value = [value]
    return [_to_evidence(item) for item in value]


def _to_evidence(item: Any) -> EvidenceRef:
    if isinstance(item, EvidenceRef):
        return item
    if isinstance(item, Document):
        return _document_to_evidence(item.page_content, item.metadata)
    if isinstance(item, Mapping):
        if "provenance" in item and "text" in item:
            return EvidenceRef.model_validate(item)
        text = item.get("text") or item.get("page_content") or item.get("content")
        if text is None:
            raise TypeError("Retriever mapping result must contain text, page_content, or content")
        metadata = dict(item.get("metadata") or {})
        for key, value in item.items():
            if key not in {"text", "page_content", "content", "metadata"}:
                metadata.setdefault(key, value)
        return _document_to_evidence(str(text), metadata)
    raise TypeError(f"Unsupported retriever result type: {type(item).__name__}")


def _document_to_evidence(text: str, raw_metadata: Mapping[str, Any]) -> EvidenceRef:
    metadata = dict(raw_metadata)
    source_id = str(
        metadata.get("source_id")
        or metadata.get("chunk_id")
        or metadata.get("document_id")
        or sha256(text.encode()).hexdigest()[:16]
    )
    locator = metadata.get("locator")
    if not isinstance(locator, dict):
        locator = {}
    trust = metadata.get("trust", TrustLevel.UNTRUSTED)
    score = max(float(metadata.get("score", 0.0)), 0.0)
    return EvidenceRef(
        text=text,
        title=str(metadata["title"]) if metadata.get("title") is not None else None,
        score=score,
        provenance=Provenance(
            source_type=str(metadata.get("source_type", "langchain_retriever")),
            source_id=source_id,
            content_hash=str(metadata.get("content_hash") or sha256(text.encode()).hexdigest()),
            locator=locator,
            trust=trust,
        ),
        metadata=metadata,
    )


def evidence_identity(item: EvidenceRef) -> str:
    if item.provenance.content_hash:
        return f"hash:{item.provenance.content_hash}"
    provenance = item.provenance
    if provenance.source_id:
        locator = json.dumps(provenance.locator, sort_keys=True, default=str)
        return f"source:{provenance.source_type}:{provenance.source_id}:{locator}"
    return f"text:{sha256(' '.join(item.text.split()).casefold().encode()).hexdigest()}"
