from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig

from app.domain.models import RunContext

__all__ = ["BaseCallbackHandler", "build_run_metadata", "build_runnable_config"]


def require_langchain() -> None:
    """Compatibility hook retained for adapters; LangChain is a required dependency."""


def build_run_metadata(
    context: RunContext,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "run_id": str(context.run_id),
        "tenant_id": context.tenant_id,
        "project_id": context.project_id,
        "user_id": context.user_id,
        "session_id": context.session_id,
        "domain_pack": context.domain_pack,
        "model": context.model,
        "skill_versions": dict(context.skill_versions),
    }
    if extra:
        metadata.update(extra)
    return metadata


def build_runnable_config(
    context: RunContext,
    *,
    callbacks: Sequence[BaseCallbackHandler] = (),
    metadata: Mapping[str, Any] | None = None,
    tags: Sequence[str] = (),
) -> RunnableConfig:
    require_langchain()
    return RunnableConfig(
        callbacks=list(callbacks),
        metadata=build_run_metadata(context, metadata),
        tags=list(tags),
    )
