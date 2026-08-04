from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5


def normalized_entity_key(value: str) -> str:
    return " ".join(value.casefold().split())


def entity_candidate_id(
    *,
    tenant_id: str,
    project_id: str,
    document_id: UUID,
    entity_type: str,
    canonical_name: str,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"hermesgraph:entity-candidate:{tenant_id}:{project_id}:"
        f"{document_id}:{entity_type}:{normalized_entity_key(canonical_name)}",
    )


def relation_candidate_id(
    *,
    tenant_id: str,
    project_id: str,
    document_id: UUID,
    source_candidate_id: UUID,
    relation_type: str,
    target_candidate_id: UUID,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"hermesgraph:relation-candidate:{tenant_id}:{project_id}:"
        f"{document_id}:{source_candidate_id}:{relation_type}:{target_candidate_id}",
    )


def extraction_batch_id(
    *,
    tenant_id: str,
    project_id: str,
    document_id: UUID,
    domain_pack: str,
    extractor_revision: str,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        f"hermesgraph:graph-extraction:{tenant_id}:{project_id}:"
        f"{document_id}:{domain_pack}:{extractor_revision}",
    )
