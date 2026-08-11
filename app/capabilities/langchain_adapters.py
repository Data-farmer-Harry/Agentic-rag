from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from app.capabilities.capability_registry import Capability
from app.capabilities.langchain_callbacks import (
    BaseCallbackHandler,
    build_runnable_config,
    require_langchain,
)
from app.domain.enums import CapabilityEffect
from app.domain.models import CapabilitySpec, RetrievalBundle, RunContext
from app.retrieval.hybrid_retrieval_pipeline import RetrievalPipeline


def capability_from_runnable(
    runnable: Any,
    spec: CapabilitySpec,
    *,
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> Capability:
    """Adapt a bounded LangChain Runnable to the framework-neutral capability contract."""
    require_langchain()
    if not hasattr(runnable, "ainvoke"):
        raise TypeError("LangChain Runnable adapter requires an object with ainvoke")

    async def handler(
        payload: Mapping[str, Any],
        context: RunContext | None,
        metadata: Mapping[str, Any],
    ) -> Any:
        context = context or RunContext()
        config = build_runnable_config(
            context,
            callbacks=callbacks,
            metadata={"capability": spec.name, "capability_version": spec.version, **metadata},
            tags=("hermesgraph", "capability"),
        )
        return await runnable.ainvoke(dict(payload), config=config)

    return Capability(spec=spec, handler=handler)


def capability_from_tool(
    tool: Any,
    *,
    spec: CapabilitySpec,
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> Capability:
    """Adapt a LangChain BaseTool after its effects and scopes are declared explicitly."""
    require_langchain()
    if not hasattr(tool, "ainvoke"):
        raise TypeError("LangChain tool adapter requires an object with ainvoke")
    return capability_from_runnable(tool, spec, callbacks=callbacks)


def capability_from_retriever(
    retriever: Any,
    *,
    name: str = "langchain_retriever",
    version: str = "1.0.0",
    spec: CapabilitySpec | None = None,
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> Capability:
    """Adapt a LangChain retriever to a capability returning RetrievalBundle."""
    require_langchain()
    resolved_spec = spec or CapabilitySpec(
        name=_capability_name(name),
        version=version,
        description="Retrieve normalized evidence through a LangChain retriever",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "filters": {"type": "object"},
                "top_k": {"type": "integer", "minimum": 1},
            },
            "required": ["query"],
        },
        output_schema=RetrievalBundle.model_json_schema(),
        effect=CapabilityEffect.READ,
        provenance_required=True,
    )
    pipeline = RetrievalPipeline({name: retriever}, callbacks=callbacks)

    async def handler(
        payload: Mapping[str, Any],
        context: RunContext | None,
        metadata: Mapping[str, Any],
    ) -> RetrievalBundle:
        del metadata
        context = context or RunContext()
        return await pipeline.retrieve(
            str(payload.get("query", "")),
            context,
            filters=dict(payload.get("filters") or {}),
            top_k=int(payload.get("top_k", 10)),
        )

    return Capability(spec=resolved_spec, handler=handler)


adapt_langchain_runnable = capability_from_runnable
adapt_langchain_tool = capability_from_tool
adapt_langchain_retriever = capability_from_retriever


def _capability_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", name.casefold()).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = f"lc_{normalized}"
    if len(normalized) < 3:
        normalized = f"lc_{normalized}"
    return normalized[:64]


def _tool_input_schema(tool: Any) -> dict[str, Any]:
    args_schema = getattr(tool, "args_schema", None)
    if args_schema is not None and hasattr(args_schema, "model_json_schema"):
        return dict(args_schema.model_json_schema())
    args = getattr(tool, "args", None)
    if isinstance(args, Mapping):
        return {"type": "object", "properties": dict(args)}
    return {"type": "object"}
