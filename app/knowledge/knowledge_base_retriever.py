from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.contracts import KnowledgeRepository
from app.domain.models import EvidenceRef, RunContext
from app.knowledge.knowledge_visibility import WorkspaceProfileResolver


class KnowledgeBaseRetriever:
    """Dynamic local retrieval branch backed by the persisted knowledge index."""

    def __init__(
        self,
        repository: KnowledgeRepository,
        *,
        workspace_profiles: WorkspaceProfileResolver | None = None,
    ) -> None:
        self._repository = repository
        self._workspace_profiles = workspace_profiles

    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: Mapping[str, Any] | None = None,
        top_k: int = 10,
    ) -> Sequence[EvidenceRef]:
        profile = (
            self._workspace_profiles.resolve(
                tenant_id=context.tenant_id,
                project_id=context.project_id,
            )
            if self._workspace_profiles is not None
            else None
        )
        requested = dict(filters or {})
        for key in ("user_id", "knowledge_layer", "enabled_knowledge_layers"):
            if key in requested:
                raise ValueError(f"Caller cannot select server-owned visibility field: {key}")
        return await self._repository.search(
            query,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            filters=requested,
            user_id=context.user_id if profile is not None else None,
            enabled_knowledge_layers=(
                [layer.value for layer in profile.enabled_knowledge_layers]
                if profile is not None
                else None
            ),
            top_k=top_k,
        )
