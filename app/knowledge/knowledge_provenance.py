from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.domain.models import (
    EvidenceRef,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSource,
    Provenance,
)
from app.knowledge.knowledge_visibility import document_visibility_metadata


def provenance_from_source(
    source: KnowledgeSource,
    *,
    document_id: UUID | str,
    chunk_index: int,
    content_hash: str | None,
    page_number: int | None = None,
    chunk_metadata: Mapping[str, Any] | None = None,
) -> Provenance:
    locator: dict[str, Any] = {"chunk_index": chunk_index}
    if page_number is not None:
        locator["page"] = page_number
    if source.canonical_uri:
        locator["canonical_uri"] = source.canonical_uri
    region_id = (chunk_metadata or {}).get("visual_region_id")
    if isinstance(region_id, str) and region_id:
        locator["region_id"] = region_id
    bounding_box = (chunk_metadata or {}).get("visual_bbox")
    if (
        isinstance(bounding_box, list)
        and len(bounding_box) == 4
        and all(isinstance(value, int | float) for value in bounding_box)
    ):
        locator["bounding_box"] = bounding_box
    modality = (chunk_metadata or {}).get("modality")
    if isinstance(modality, str) and modality:
        locator["modality"] = modality
    base_source_id = source.source_id or str(document_id)
    source_fragment = f"#chunk={chunk_index}"
    if isinstance(region_id, str) and region_id:
        source_fragment += f"&region={region_id}"
    return Provenance(
        source_type=source.source_type,
        source_id=f"{base_source_id}{source_fragment}",
        content_hash=content_hash,
        locator=locator,
        trust=source.trust,
        observed_at=source.acquired_at,
    )


def source_metadata(source: KnowledgeSource, *, document_id: UUID) -> dict[str, object]:
    return {
        "source_type": source.source_type,
        "source_id": source.source_id or str(document_id),
        "source_revision": source.source_revision,
        "canonical_uri": source.canonical_uri,
        "license_uri": source.license_uri,
        "privacy": source.privacy,
        "source_status": source.source_status,
        "source_owner": source.owner,
        "source_last_reviewed_at": (
            source.last_reviewed_at.isoformat()
            if source.last_reviewed_at is not None
            else None
        ),
        "source_effective_from": (
            source.effective_from.isoformat() if source.effective_from is not None else None
        ),
        "source_effective_to": (
            source.effective_to.isoformat() if source.effective_to is not None else None
        ),
        "source_supersedes": source.supersedes_source_id,
        "source_superseded_by": source.superseded_by_source_id,
        "fixture_id": source.fixture_id,
    }


def evidence_from_chunk(
    document: KnowledgeDocument,
    chunk: KnowledgeChunk,
    *,
    score: float,
    metadata: dict[str, object],
) -> EvidenceRef:
    """Build one citation from the document's normalized source contract."""

    return EvidenceRef(
        text=chunk.text,
        title=document.title,
        score=score,
        provenance=provenance_from_source(
            document.source,
            document_id=document.document_id,
            chunk_index=chunk.chunk_index,
            content_hash=chunk.content_hash,
            page_number=chunk.page_number,
            chunk_metadata=chunk.metadata,
        ),
        metadata={
            **metadata,
            **source_metadata(document.source, document_id=document.document_id),
            **document_visibility_metadata(document),
        },
    )
