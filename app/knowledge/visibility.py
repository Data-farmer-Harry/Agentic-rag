"""Server-owned knowledge visibility projection.

The projection is deliberately independent from model tool arguments.  Retrieval
receives a ``RunContext`` and resolves its workspace profile locally; callers may
not select a different owner or visibility layer through filters.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from app.config import Settings
from app.domain.enums import KnowledgeLayer, WorkspaceMode
from app.domain.models import KnowledgeDocument, KnowledgeSource, WorkspaceProfile

_LAYER_KEY = "knowledge_layer"
_OWNER_KEY = "user_id"
_TEAM_SOURCE_TYPES = frozenset(
    {
        "enterprise_internal",
        "fixture_enterprise",
        "project_document",
        "repository",
        "team_upload",
    }
)
_PERSONAL_SOURCE_TYPES = frozenset(
    {
        "uploaded_document",
        "personal_document",
        "personal_note",
        "personal_upload",
    }
)
_PUBLIC_SOURCE_TYPES = frozenset({"arxiv", "public_reference", "web_reference"})
_LAYER_PRIORITY = {
    KnowledgeLayer.TEAM_INTERNAL: 0,
    KnowledgeLayer.PERSONAL: 1,
    KnowledgeLayer.PUBLIC_REFERENCE: 2,
}


class WorkspaceProfileResolver(Protocol):
    def resolve(self, *, tenant_id: str, project_id: str) -> WorkspaceProfile: ...


class SettingsWorkspaceProfileResolver:
    """Config-backed P0 profile resolver.

    Profiles are intentionally immutable at runtime for now.  A future persistent
    profile store can implement the same tiny protocol without moving the
    visibility decision to an API client or an agent prompt.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, *, tenant_id: str, project_id: str) -> WorkspaceProfile:
        mode = self._settings.workspace_mode
        configured_layers = self._settings.workspace_enabled_knowledge_layers
        if configured_layers:
            layers = tuple(configured_layers)
        elif mode == WorkspaceMode.TEAM:
            # Team workspaces deliberately do not inherit the large arXiv corpus.
            layers = (KnowledgeLayer.TEAM_INTERNAL, KnowledgeLayer.PERSONAL)
        else:
            layers = (KnowledgeLayer.PERSONAL, KnowledgeLayer.PUBLIC_REFERENCE)
        default_pack = self._settings.workspace_default_domain_pack or (
            "software_engineering" if mode == WorkspaceMode.TEAM else "general"
        )
        return WorkspaceProfile(
            tenant_id=tenant_id,
            project_id=project_id,
            display_name=self._settings.workspace_display_name,
            workspace_mode=mode,
            enabled_knowledge_layers=layers,
            default_domain_pack=default_pack,
        )


def knowledge_layer_for_source(
    source: KnowledgeSource,
    *,
    workspace_mode: WorkspaceMode | None = None,
) -> KnowledgeLayer | None:
    """Resolve a deterministic source layer, returning ``None`` for unknown input."""

    if source.privacy == "public_reference" or source.source_type in _PUBLIC_SOURCE_TYPES:
        return KnowledgeLayer.PUBLIC_REFERENCE
    if source.fixture_id is not None or source.source_type in _TEAM_SOURCE_TYPES:
        return KnowledgeLayer.TEAM_INTERNAL
    if source.source_type in _PERSONAL_SOURCE_TYPES:
        if source.source_type == "uploaded_document" and workspace_mode == WorkspaceMode.TEAM:
            return KnowledgeLayer.TEAM_INTERNAL
        return KnowledgeLayer.PERSONAL
    return None


def knowledge_layer_for_document(document: KnowledgeDocument) -> KnowledgeLayer | None:
    explicit = _coerce_layer(document.metadata.get(_LAYER_KEY))
    if explicit is not None:
        return explicit
    return knowledge_layer_for_source(document.source)


def visibility_metadata(
    *,
    source: KnowledgeSource,
    user_id: str,
    workspace_mode: WorkspaceMode | None = None,
) -> dict[str, str]:
    """Persist the exact server-resolved attributes used by vector indexes."""

    layer = knowledge_layer_for_source(source, workspace_mode=workspace_mode)
    return {
        _LAYER_KEY: layer.value if layer is not None else "unclassified",
        _OWNER_KEY: user_id,
    }


def document_visibility_metadata(document: KnowledgeDocument) -> dict[str, str]:
    layer = knowledge_layer_for_document(document)
    return {
        _LAYER_KEY: layer.value if layer is not None else "unclassified",
        _OWNER_KEY: document.user_id,
    }


def knowledge_layer_priority(value: object) -> int:
    """Stable presentation/ranking tie-breaker for visible evidence layers.

    This is deliberately a secondary ordering rule: it never changes the
    evidence score or makes an inactive/superseded source eligible.
    """

    layer = _coerce_layer(value)
    if layer is None:
        return len(_LAYER_PRIORITY)
    return _LAYER_PRIORITY.get(layer, len(_LAYER_PRIORITY))


def evidence_is_visible(
    metadata: Mapping[str, Any],
    *,
    user_id: str,
    enabled_layers: Sequence[KnowledgeLayer | str],
) -> bool:
    """Fail-closed post-filter for an individual returned evidence payload.

    This intentionally requires the persisted layer.  Old Qdrant points without
    it remain hidden until reindexed, even if their source type looks familiar.
    """

    if metadata.get("source_status") != "active":
        return False
    layer = _coerce_layer(metadata.get(_LAYER_KEY))
    if layer is None or layer not in _coerce_layers(enabled_layers):
        return False
    if layer == KnowledgeLayer.PERSONAL:
        owner = metadata.get(_OWNER_KEY)
        return isinstance(owner, str) and owner == user_id
    return True


def document_is_visible(
    document: KnowledgeDocument,
    *,
    user_id: str,
    enabled_layers: Sequence[KnowledgeLayer | str],
) -> bool:
    layer = knowledge_layer_for_document(document)
    if (
        document.source.source_status != "active"
        or layer is None
        or layer not in _coerce_layers(enabled_layers)
    ):
        return False
    return layer != KnowledgeLayer.PERSONAL or document.user_id == user_id


def _coerce_layer(value: object) -> KnowledgeLayer | None:
    try:
        return KnowledgeLayer(str(value))
    except ValueError:
        return None


def _coerce_layers(values: Sequence[KnowledgeLayer | str]) -> frozenset[KnowledgeLayer]:
    return frozenset(
        layer
        for value in values
        if (layer := _coerce_layer(value)) is not None
    )
