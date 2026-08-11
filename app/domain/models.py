from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any, Literal, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import (
    AnswerMode,
    CapabilityEffect,
    DocumentStatus,
    EventKind,
    EvidenceLevel,
    GraphCandidateStatus,
    IngestionJobStatus,
    KnowledgeLayer,
    LearningJobStatus,
    MemoryType,
    OutboxEventStatus,
    RetryOwner,
    RoutingLane,
    RunStatus,
    SkillStatus,
    TrustLevel,
    WorkspaceMode,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provenance(StrictModel):
    source_type: str
    source_id: str
    run_id: UUID | None = None
    content_hash: str | None = None
    locator: dict[str, Any] = Field(default_factory=dict)
    trust: TrustLevel = TrustLevel.UNTRUSTED
    observed_at: datetime = Field(default_factory=utc_now)


class EvidenceRef(StrictModel):
    evidence_id: UUID = Field(default_factory=uuid4)
    text: str = Field(min_length=1, max_length=20_000)
    title: str | None = None
    score: float = Field(default=0.0, ge=0.0)
    provenance: Provenance
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSource(StrictModel):
    """Normalized origin and policy boundary for one retained knowledge object."""

    source_type: str = Field(
        default="uploaded_document",
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    )
    source_id: str = Field(default="", max_length=1_000)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    source_revision: str | None = Field(default=None, max_length=200)
    canonical_uri: str | None = Field(default=None, max_length=2_000)
    license_uri: str | None = Field(default=None, max_length=2_000)
    privacy: Literal["private", "public_reference"] = "private"
    trust: TrustLevel = TrustLevel.USER_ASSERTED
    source_status: Literal["draft", "active", "superseded", "archived"] = "active"
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    last_reviewed_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    supersedes_source_id: str | None = Field(default=None, min_length=1, max_length=1_000)
    superseded_by_source_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=1_000,
    )
    fixture_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{2,63}$",
    )
    acquired_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_effective_range(self) -> Self:
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_from > self.effective_to
        ):
            raise ValueError("Knowledge source effective_from must not follow effective_to")
        return self


class WorkspaceProfile(StrictModel):
    """Stable workspace defaults. It does not replace request identity or RBAC."""

    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    workspace_mode: WorkspaceMode
    enabled_knowledge_layers: tuple[KnowledgeLayer, ...] = Field(min_length=1)
    default_domain_pack: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]{2,63}$",
    )
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    version: int = Field(default=1, ge=1)

    @field_validator("enabled_knowledge_layers")
    @classmethod
    def unique_enabled_layers(
        cls,
        values: tuple[KnowledgeLayer, ...],
    ) -> tuple[KnowledgeLayer, ...]:
        if len(set(values)) != len(values):
            raise ValueError("Workspace knowledge layers must be unique")
        return values


class NormalizedBoundingBox(StrictModel):
    x_min: float = Field(ge=0.0, le=1.0)
    y_min: float = Field(ge=0.0, le=1.0)
    x_max: float = Field(ge=0.0, le=1.0)
    y_max: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_extent(self) -> Self:
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("Visual bounding boxes must have positive normalized area")
        return self

    def as_list(self) -> list[float]:
        return [self.x_min, self.y_min, self.x_max, self.y_max]


class VisualRegionDraft(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    category: Literal[
        "text",
        "diagram",
        "chart",
        "table",
        "code",
        "interface",
        "object",
        "other",
    ]
    description: str = Field(min_length=1, max_length=10_000)
    visible_text: str = Field(default="", max_length=20_000)
    bounding_box: NormalizedBoundingBox | None = None
    confidence: float = Field(ge=0.0, le=1.0)


class VisionAnalysis(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=20_000)
    visible_text: str = Field(default="", max_length=50_000)
    regions: list[VisualRegionDraft] = Field(default_factory=list, max_length=50)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class GraphNode(StrictModel):
    node_id: str
    tenant_id: str
    project_id: str
    label: str
    name: str
    properties: dict[str, Any] = Field(default_factory=dict)
    provenance: list[Provenance] = Field(default_factory=list)


class GraphRelationship(StrictModel):
    relationship_id: str
    tenant_id: str
    project_id: str
    relation_type: str
    source_node_id: str
    target_node_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class GraphPath(StrictModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    relationships: list[GraphRelationship] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class GraphSearchRequest(StrictModel):
    entities: list[str] = Field(min_length=1, max_length=10)
    template: str = "neighbors"
    max_hops: int = Field(default=2, ge=1, le=3)
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("entities")
    @classmethod
    def normalize_entities(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized or any(
            len(value) < 2 or len(value) > 300 or "\x00" in value for value in normalized
        ):
            raise ValueError("Graph entities must contain 2 to 300 safe characters")
        return normalized


class GraphSearchResult(StrictModel):
    paths: list[GraphPath] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)


class GraphEntityResolveRequest(StrictModel):
    mentions: list[str] = Field(min_length=1, max_length=10)
    entity_types: list[str] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=10, ge=1, le=50)
    min_score: float = Field(default=0.65, ge=0.0, le=1.0)

    @field_validator("mentions", "entity_types")
    @classmethod
    def normalize_terms(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized and values:
            raise ValueError("Graph lookup terms must contain non-whitespace text")
        if any(len(value) > 500 or "\x00" in value for value in normalized):
            raise ValueError("Graph lookup terms must contain at most 500 safe characters")
        return normalized


class GraphEntityMatch(StrictModel):
    node: GraphNode
    matched_text: str = Field(min_length=1, max_length=500)
    matched_field: Literal["canonical_name", "alias"]
    score: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class GraphEntityResolveResult(StrictModel):
    mentions: list[str] = Field(default_factory=list)
    matches: list[GraphEntityMatch] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)


class GraphRAGRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2_000)
    seed_entities: list[str] = Field(default_factory=list, max_length=10)
    entity_types: list[str] = Field(default_factory=list, max_length=10)
    max_hops: int = Field(default=2, ge=1, le=3)
    top_k: int = Field(default=10, ge=1, le=50)
    path_limit: int = Field(default=30, ge=1, le=100)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized or "\x00" in normalized:
            raise ValueError("GraphRAG query must contain safe text")
        return normalized

    @field_validator("seed_entities", "entity_types")
    @classmethod
    def normalize_graph_terms(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if any(len(value) > 500 or "\x00" in value for value in normalized):
            raise ValueError("GraphRAG terms must contain at most 500 safe characters")
        return normalized


class GraphRAGResult(StrictModel):
    query: str
    resolved_entities: list[GraphEntityMatch] = Field(default_factory=list)
    graph_paths: list[GraphPath] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)


class GraphEntityCompareRequest(StrictModel):
    left_entity: str = Field(min_length=1, max_length=500)
    right_entity: str = Field(min_length=1, max_length=500)
    max_hops: int = Field(default=3, ge=1, le=3)
    limit: int = Field(default=30, ge=1, le=100)

    @field_validator("left_entity", "right_entity")
    @classmethod
    def normalize_entity(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized or "\x00" in normalized:
            raise ValueError("Comparison entities must contain safe text")
        return normalized


class GraphEntityCompareResult(StrictModel):
    left_match: GraphEntityMatch | None = None
    right_match: GraphEntityMatch | None = None
    connecting_paths: list[GraphPath] = Field(default_factory=list)
    shared_neighbors: list[GraphNode] = Field(default_factory=list)
    left_only_neighbors: list[GraphNode] = Field(default_factory=list)
    right_only_neighbors: list[GraphNode] = Field(default_factory=list)
    unresolved_entities: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)


class RetrievalBundle(StrictModel):
    query: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    graph_paths: list[GraphPath] = Field(default_factory=list)
    applied_filters: dict[str, Any] = Field(default_factory=dict)
    trace: dict[str, Any] = Field(default_factory=dict)


class WebSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2_000)
    max_results: int = Field(default=8, ge=1, le=20)


class WebSearchSource(StrictModel):
    url: str = Field(min_length=8, max_length=2_000)
    title: str = Field(min_length=1, max_length=500)


class WebSearchResult(StrictModel):
    query: str
    summary: str = Field(default="", max_length=20_000)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=20)
    sources: list[WebSearchSource] = Field(default_factory=list, max_length=20)
    trace: dict[str, Any] = Field(default_factory=dict)


class WorkspaceListRequest(StrictModel):
    root: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    path: str = Field(default=".", min_length=1, max_length=1_000)
    recursive: bool = False
    max_entries: int = Field(default=100, ge=1, le=500)


class WorkspaceEntry(StrictModel):
    path: str = Field(min_length=1, max_length=2_000)
    kind: Literal["file", "directory"]
    byte_size: int | None = Field(default=None, ge=0)
    modified_at: datetime
    format: str | None = Field(default=None, max_length=32)


class WorkspaceListResult(StrictModel):
    root: str
    path: str
    entries: list[WorkspaceEntry] = Field(default_factory=list, max_length=500)
    scanned_entries: int = Field(default=0, ge=0)
    truncated: bool = False


class WorkspaceFileReadRequest(StrictModel):
    root: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    path: str = Field(min_length=1, max_length=1_000)
    start_line: int = Field(default=1, ge=1, le=10_000_000)
    max_lines: int = Field(default=200, ge=1, le=400)


class WorkspaceFileReadResult(StrictModel):
    root: str
    path: str
    format: str = Field(min_length=1, max_length=32)
    byte_size: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=0)
    total_lines: int = Field(ge=0)
    text: str = Field(default="", max_length=20_000)
    truncated: bool = False
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=1)
    warnings: list[str] = Field(default_factory=list, max_length=20)


class WorkspaceSearchRequest(StrictModel):
    root: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    query: str = Field(min_length=1, max_length=500)
    path: str = Field(default=".", min_length=1, max_length=1_000)
    case_sensitive: bool = False
    max_results: int = Field(default=20, ge=1, le=50)
    max_files: int = Field(default=100, ge=1, le=500)


class WorkspaceSearchMatch(StrictModel):
    path: str = Field(min_length=1, max_length=2_000)
    line_number: int = Field(ge=1)
    excerpt: str = Field(min_length=1, max_length=1_000)
    evidence_id: UUID


class WorkspaceSearchResult(StrictModel):
    root: str
    path: str
    query: str
    matches: list[WorkspaceSearchMatch] = Field(default_factory=list, max_length=50)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=50)
    scanned_files: int = Field(default=0, ge=0)
    skipped_files: int = Field(default=0, ge=0)
    truncated: bool = False


class KnowledgeDocument(StrictModel):
    document_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = "local"
    project_id: str = "default"
    user_id: str = "local-user"
    filename: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=200)
    byte_size: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    storage_key: str
    status: DocumentStatus = DocumentStatus.ACTIVE
    chunk_count: int = Field(default=0, ge=0)
    parser_version: str = "local-v1"
    source: KnowledgeSource = Field(default_factory=KnowledgeSource)
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeChunk(StrictModel):
    chunk_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    tenant_id: str = "local"
    project_id: str = "default"
    chunk_index: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=20_000)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_number: int | None = Field(default=None, ge=1)
    char_start: int = Field(default=0, ge=0)
    char_end: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestionResult(StrictModel):
    document: KnowledgeDocument
    deduplicated: bool = False


class IngestionJob(StrictModel):
    job_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = "local"
    project_id: str = "default"
    user_id: str = "local-user"
    filename: str = Field(min_length=1, max_length=255)
    media_type: str | None = Field(default=None, max_length=200)
    byte_size: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    staging_key: str = Field(min_length=1, max_length=1_000, exclude=True)
    source: KnowledgeSource = Field(default_factory=KnowledgeSource)
    status: IngestionJobStatus = IngestionJobStatus.QUEUED
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    available_at: datetime = Field(default_factory=utc_now)
    lease_owner: str | None = Field(default=None, max_length=200, exclude=True)
    lease_expires_at: datetime | None = None
    document_id: UUID | None = None
    deduplicated: bool | None = None
    can_retry: bool = False
    error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,99}$",
    )
    error_message: str | None = Field(default=None, max_length=1_000)
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class IngestionJobSubmission(StrictModel):
    job: IngestionJob
    coalesced: bool = False


class OutboxEvent(StrictModel):
    event_id: UUID = Field(default_factory=uuid4)
    aggregate_type: str = Field(min_length=1, max_length=100)
    aggregate_id: str = Field(min_length=1, max_length=255)
    event_type: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: OutboxEventStatus = OutboxEventStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=10, ge=1, le=100)
    available_at: datetime = Field(default_factory=utc_now)
    lease_owner: str | None = Field(default=None, exclude=True)
    lease_expires_at: datetime | None = None
    published_at: datetime | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GraphEntityCandidate(StrictModel):
    candidate_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    tenant_id: str = "local"
    project_id: str = "default"
    canonical_name: str = Field(min_length=2, max_length=300)
    entity_type: str = Field(pattern=r"^[A-Z][A-Za-z0-9_]{1,63}$")
    aliases: list[str] = Field(default_factory=list, max_length=20)
    source_chunk_ids: list[UUID] = Field(min_length=1, max_length=50)
    confidence: float = Field(ge=0.0, le=1.0)
    extractor_revision: str = Field(min_length=1, max_length=200)
    domain_pack: str = Field(default="general", min_length=1, max_length=100)
    activation_policy: Literal["review_required"] = "review_required"
    status: GraphCandidateStatus = GraphCandidateStatus.PENDING
    rationale: str = Field(default="", max_length=1_000)
    reviewed_by: str | None = Field(default=None, max_length=200)
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GraphRelationCandidate(StrictModel):
    candidate_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    tenant_id: str = "local"
    project_id: str = "default"
    source_candidate_id: UUID
    target_candidate_id: UUID
    source_name: str = Field(min_length=2, max_length=300)
    target_name: str = Field(min_length=2, max_length=300)
    relation_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    source_chunk_ids: list[UUID] = Field(min_length=1, max_length=50)
    confidence: float = Field(ge=0.0, le=1.0)
    extractor_revision: str = Field(min_length=1, max_length=200)
    domain_pack: str = Field(default="general", min_length=1, max_length=100)
    activation_policy: Literal["review_required"] = "review_required"
    status: GraphCandidateStatus = GraphCandidateStatus.PENDING
    rationale: str = Field(default="", max_length=1_000)
    reviewed_by: str | None = Field(default=None, max_length=200)
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EntityResolutionCandidate(StrictModel):
    candidate_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = "local"
    project_id: str = "default"
    left_entity_id: UUID
    right_entity_id: UUID
    left_document_id: UUID
    right_document_id: UUID
    left_name: str = Field(min_length=2, max_length=300)
    right_name: str = Field(min_length=2, max_length=300)
    canonical_name: str = Field(min_length=2, max_length=300)
    entity_type: str = Field(pattern=r"^[A-Z][A-Za-z0-9_]{1,63}$")
    match_strategy: str = Field(
        pattern=r"^(exact_identifier|exact_name|normalized_name|alias_overlap)$"
    )
    source_chunk_ids: list[UUID] = Field(min_length=2, max_length=100)
    confidence: float = Field(ge=0.0, le=1.0)
    resolver_revision: str = Field(min_length=1, max_length=200)
    status: GraphCandidateStatus = GraphCandidateStatus.PENDING
    rationale: str = Field(default="", max_length=1_000)
    reviewed_by: str | None = Field(default=None, max_length=200)
    reviewed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GraphExtractionBatch(StrictModel):
    batch_id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    tenant_id: str = "local"
    project_id: str = "default"
    domain_pack: str = "general"
    extractor_revision: str = Field(min_length=1, max_length=200)
    entities: list[GraphEntityCandidate] = Field(default_factory=list, max_length=2_000)
    relations: list[GraphRelationCandidate] = Field(default_factory=list, max_length=2_000)
    created_at: datetime = Field(default_factory=utc_now)


class GraphCandidateCollection(StrictModel):
    entities: list[GraphEntityCandidate] = Field(default_factory=list)
    relations: list[GraphRelationCandidate] = Field(default_factory=list)
    resolutions: list[EntityResolutionCandidate] = Field(default_factory=list)


class GraphCandidateReviewEvent(StrictModel):
    review_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = "local"
    project_id: str = "default"
    candidate_type: str = Field(pattern=r"^(entity|relation|resolution)$")
    candidate_id: UUID
    from_status: GraphCandidateStatus
    to_status: GraphCandidateStatus
    reviewer_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2_000)
    domain_pack: str = Field(default="general", min_length=1, max_length=100)
    activation_policy: Literal["review_required"] = "review_required"
    created_at: datetime = Field(default_factory=utc_now)


class CapabilitySpec(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    effect: CapabilityEffect = CapabilityEffect.READ
    required_scopes: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=30, ge=1, le=3600)
    retry_owner: RetryOwner = RetryOwner.INTEGRATION_RUNTIME
    idempotent: bool = True
    max_output_bytes: int = Field(default=100_000, ge=1)
    sensitive_fields: list[str] = Field(default_factory=list)
    provenance_required: bool = True


class DomainPackManifest(StrictModel):
    pack_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    core_compatibility: str
    description: str
    capability_names: list[str] = Field(default_factory=list)
    required_scopes: list[str] = Field(default_factory=list)
    schema_hash: str


class RunSnapshot(StrictModel):
    model: str
    model_settings: dict[str, Any] = Field(default_factory=dict)
    prompt_hash: str
    domain_pack: str
    domain_pack_version: str
    skill_versions: dict[str, str] = Field(default_factory=dict)
    policy_versions: dict[str, str] = Field(default_factory=dict)
    corpus_snapshot: str = "local"
    component_versions: dict[str, str] = Field(default_factory=dict)
    config_hash: str
    harness_overlay_id: UUID | None = None
    harness_overlay_hash: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    harness_pattern_versions: list[str] = Field(default_factory=list, max_length=3)
    harness_overlay_mode: Literal[
        "disabled",
        "observe",
        "shadow",
        "canary",
        "active",
    ] = "disabled"
    harness_execution_policy: dict[str, Any] = Field(default_factory=dict)
    harness_execution_policy_hash: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )


class Claim(StrictModel):
    text: str
    evidence_ids: list[UUID] = Field(default_factory=list)
    level: EvidenceLevel = EvidenceLevel.INSUFFICIENT


class AgentAnswerDraft(StrictModel):
    """Strict model-authored answer; citation payloads are hydrated server-side."""

    answer_markdown: str
    response_mode: AnswerMode = AnswerMode.GROUNDED
    claims: list[Claim] = Field(default_factory=list)
    citation_ids: list[UUID] = Field(default_factory=list)
    memory_ids: list[UUID] = Field(default_factory=list, max_length=20)
    confidence: EvidenceLevel = EvidenceLevel.INSUFFICIENT
    limitations: list[str] = Field(default_factory=list)
    followup_queries: list[str] = Field(default_factory=list)


class FollowUpAction(StrictModel):
    """Read-only, deterministic projection of a model-proposed follow-up query."""

    action_id: str = Field(pattern=r"^query:[a-f0-9]{16}$")
    kind: Literal["query"] = "query"
    label: str = Field(min_length=1, max_length=500)
    query: str = Field(min_length=1, max_length=2_000)


class AdaptiveRAGRoute(StrictModel):
    """Model-authored, server-normalized retrieval strategy for one user turn."""

    strategy: Literal["no_retrieval", "single_step", "multi_step"]
    knowledge_route: Literal[
        "conversation",
        "tool_action",
        "passage_lookup",
        "relationship",
        "global_summary",
    ]
    requires_graph: bool = False
    requires_multi_source: bool = False
    self_reflection: bool = False
    confidence: Literal["high", "medium"] = "medium"
    signals: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_strategy(self) -> Self:
        if self.strategy == "no_retrieval":
            if self.requires_graph or self.requires_multi_source or self.self_reflection:
                raise ValueError("No-retrieval routes cannot request retrieval features")
            if self.knowledge_route not in {"conversation", "tool_action"}:
                raise ValueError("No-retrieval routes must be conversation or tool_action")
        elif self.knowledge_route in {"conversation", "tool_action"}:
            raise ValueError("Retrieval routes must select a knowledge strategy")
        if self.self_reflection != (self.strategy == "multi_step"):
            raise ValueError("Self-reflection is reserved for multi-step retrieval")
        return self


class ContextTrace(StrictModel):
    """Auditable record of the bounded context assembled for one answer."""

    revision: str = Field(default="context-engine-v2", min_length=1, max_length=100)
    total_budget_tokens: int = Field(ge=0)
    used_tokens: int = Field(ge=0)
    component_tokens: dict[str, int] = Field(default_factory=dict)
    selected_memory_ids: list[UUID] = Field(default_factory=list, max_length=20)
    omitted_memory_count: int = Field(default=0, ge=0)
    duplicate_memory_count: int = Field(default=0, ge=0)
    conflicting_memory_count: int = Field(default=0, ge=0)
    recent_turn_count: int = Field(default=0, ge=0)
    summarized_turn_count: int = Field(default=0, ge=0)
    summary_revision: str | None = Field(default=None, max_length=100)
    truncated_components: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_budget(self) -> Self:
        if self.used_tokens > self.total_budget_tokens:
            raise ValueError("Context usage cannot exceed its total token budget")
        if any(value < 0 for value in self.component_tokens.values()):
            raise ValueError("Context component token counts cannot be negative")
        return self


class AnswerResponse(StrictModel):
    answer_markdown: str
    response_mode: AnswerMode = AnswerMode.GROUNDED
    routing_lane: RoutingLane | None = None
    claims: list[Claim] = Field(default_factory=list)
    citations: list[EvidenceRef] = Field(default_factory=list)
    memory_ids: list[UUID] = Field(default_factory=list, max_length=20)
    confidence: EvidenceLevel = EvidenceLevel.INSUFFICIENT
    limitations: list[str] = Field(default_factory=list)
    followup_queries: list[str] = Field(default_factory=list)
    graph_paths: list[GraphPath] = Field(default_factory=list)
    follow_up_actions: list[FollowUpAction] = Field(default_factory=list)
    adaptive_rag_route: AdaptiveRAGRoute | None = None
    context_trace: ContextTrace | None = None


class RunExecutionPolicy(StrictModel):
    resolver_revision: str = Field(min_length=1, max_length=200)
    behavior_applied: bool = False
    overlay_id: UUID | None = None
    overlay_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    selected_pattern_versions: list[str] = Field(default_factory=list, max_length=3)
    applied_pattern_versions: list[str] = Field(default_factory=list, max_length=3)
    capsule_memory_limit: int | None = Field(default=None, ge=0, le=20)
    memory_min_confidence: float | None = Field(default=None, ge=0.6, le=1.0)
    retrieval_profile: (
        Literal[
            "lookup",
            "compare",
            "synthesis",
            "personal_recall",
            "visual_lookup",
        ]
        | None
    ) = None
    max_subqueries: int | None = Field(default=None, ge=1, le=4)
    max_retrieval_rounds: int | None = Field(default=None, ge=1, le=2)
    graph_hop_cap: int | None = Field(default=None, ge=1, le=3)
    clamped_fields: list[str] = Field(default_factory=list, max_length=50)
    rejected_fields: list[str] = Field(default_factory=list, max_length=50)
    policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.behavior_applied != bool(self.applied_pattern_versions):
            raise ValueError("Applied Pattern versions must match behavior_applied")
        if not set(self.applied_pattern_versions).issubset(self.selected_pattern_versions):
            raise ValueError("Applied Pattern versions must be selected")
        payload = self.model_dump(mode="json", exclude={"policy_hash"})
        expected = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if expected != self.policy_hash:
            raise ValueError("Run execution policy hash does not match its content")
        return self


class RunContext(StrictModel):
    run_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = "local"
    project_id: str = "default"
    user_id: str = "local-user"
    session_id: str = "default"
    domain_pack: str = "general"
    model: str = "gpt-5.6"
    enabled_knowledge_layers: tuple[KnowledgeLayer, ...] | None = None
    workspace_mode: WorkspaceMode | None = None
    skill_versions: dict[str, str] = Field(default_factory=dict)
    execution_policy: RunExecutionPolicy | None = None
    adaptive_rag_route: AdaptiveRAGRoute | None = None
    started_at: datetime = Field(default_factory=utc_now)


class ToolEvent(StrictModel):
    tool_name: str
    input_hash: str
    output_summary: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    success: bool = True
    duration_ms: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class RunEvent(StrictModel):
    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class RunStreamEvent(StrictModel):
    run_id: UUID
    cursor: int = Field(ge=1)
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    event: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class RunTrajectory(StrictModel):
    context: RunContext
    user_input: str
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)
    snapshot: RunSnapshot | None = None
    status: RunStatus = RunStatus.CREATED
    answer: AnswerResponse | None = None
    tool_events: list[ToolEvent] = Field(default_factory=list)
    feedback_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    feedback_text: str | None = None
    tags: list[str] = Field(default_factory=list)
    completed_at: datetime | None = None


class ConversationSummary(StrictModel):
    session_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    preview: str = Field(default="", max_length=500)
    run_count: int = Field(ge=1)
    last_run_id: UUID
    last_status: RunStatus
    domain_pack: str = Field(min_length=1, max_length=100)
    archived: bool = False
    created_at: datetime
    updated_at: datetime


class ConversationMetadata(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool = False
    context_summary: str = Field(default="", max_length=30_000)
    summarized_run_ids: list[UUID] = Field(default_factory=list, max_length=200)
    context_summary_revision: str | None = Field(default=None, max_length=100)
    context_summary_updated_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class LearningJobResult(StrictModel):
    run_id: UUID
    reflector_revision: str
    reflection_outcome: str
    reflection_summary: str
    memory_ids: list[UUID] = Field(default_factory=list)
    change_set_ids: list[UUID] = Field(default_factory=list)
    skill_candidate_id: UUID | None = None
    skill_candidate_version: str | None = Field(
        default=None,
        pattern=r"^\d+\.\d+\.\d+$",
    )
    observation_ids: list[UUID] = Field(default_factory=list)
    evaluation_id: UUID | None = None
    transition_ids: list[UUID] = Field(default_factory=list)
    harness_experience_ids: list[UUID] = Field(default_factory=list)
    harness_evaluation_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_skill_candidate_version(self) -> Self:
        if self.skill_candidate_version is not None and self.skill_candidate_id is None:
            raise ValueError("A skill candidate version requires a skill candidate id")
        return self


class LearningJob(StrictModel):
    job_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    tenant_id: str = "local"
    project_id: str = "default"
    user_id: str = "local-user"
    run_id: UUID
    trigger: Literal["run_completed", "feedback_received"]
    trajectory: RunTrajectory = Field(exclude=True)
    status: LearningJobStatus = LearningJobStatus.QUEUED
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    available_at: datetime = Field(default_factory=utc_now)
    lease_owner: str | None = Field(default=None, max_length=200, exclude=True)
    lease_token: UUID | None = Field(default=None, exclude=True)
    lease_expires_at: datetime | None = None
    checkpoint: LearningJobCheckpoint | None = Field(default=None, exclude=True)
    result: LearningJobResult | None = None
    reconciliation_status: Literal[
        "not_required",
        "pending",
        "verified",
        "required",
    ] = "not_required"
    reconciliation_error: str | None = Field(
        default=None,
        max_length=1_000,
        exclude=True,
    )
    can_retry: bool = False
    error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,99}$",
    )
    error_message: str | None = Field(default=None, max_length=1_000)
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class LearningJobSubmission(StrictModel):
    job: LearningJob
    coalesced: bool = False


class MemoryCandidate(StrictModel):
    tenant_id: str = "local"
    project_id: str = "default"
    user_id: str | None = "local-user"
    memory_type: MemoryType
    key: str
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    provenance: list[Provenance] = Field(min_length=1)
    expires_at: datetime | None = None


class MemoryRecord(MemoryCandidate):
    memory_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None


class LearningTrajectoryEvaluation(StrictModel):
    run_id: str
    quality_score: float = Field(ge=0.0, le=1.0)
    completion_score: float = Field(ge=0.0, le=1.0)
    tool_success_rate: float = Field(ge=0.0, le=1.0)
    citation_coverage: float = Field(ge=0.0, le=1.0)
    unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    feedback_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class LearningReflectionArtifact(StrictModel):
    artifact_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    trajectory_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evaluation: LearningTrajectoryEvaluation
    outcome: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=2_000)
    strengths: list[str] = Field(default_factory=list, max_length=20)
    weaknesses: list[str] = Field(default_factory=list, max_length=20)
    action_sequence: list[str] = Field(default_factory=list, max_length=100)
    memory_candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=20)
    reflector_revision: str = Field(min_length=1, max_length=200)
    fallback_error: str | None = Field(default=None, max_length=500)
    model_reflection_attempted: bool = False
    trigger_reason: str = Field(default="deterministic_baseline", max_length=200)
    created_at: datetime = Field(default_factory=utc_now)


class LearningJobCheckpoint(StrictModel):
    revision: str = Field(default="learning-workflow-v1", min_length=1, max_length=100)
    stage: Literal[
        "reflection_completed",
        "artifacts_committed",
        "observations_committed",
        "evolution_committed",
        "harness_experience_committed",
    ]
    reflection: LearningReflectionArtifact
    memory_ids: list[UUID] = Field(default_factory=list)
    change_set_ids: list[UUID] = Field(default_factory=list)
    skill_candidate_id: UUID | None = None
    skill_candidate_version: str | None = Field(
        default=None,
        pattern=r"^\d+\.\d+\.\d+$",
    )
    observation_ids: list[UUID] = Field(default_factory=list)
    evaluation_id: UUID | None = None
    transition_ids: list[UUID] = Field(default_factory=list)
    harness_experience_ids: list[UUID] = Field(default_factory=list)
    harness_evaluation_ids: list[UUID] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_skill_candidate_version(self) -> Self:
        if self.skill_candidate_version is not None and self.skill_candidate_id is None:
            raise ValueError("A skill candidate version requires a skill candidate id")
        return self


class SkillStep(StrictModel):
    action: str
    purpose: str
    inputs: dict[str, Any] = Field(default_factory=dict)


class SkillDefinition(StrictModel):
    skill_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = "local"
    project_id: str = "default"
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=10, max_length=500)
    status: SkillStatus = SkillStatus.DRAFT
    trigger_intents: list[str] = Field(default_factory=list)
    trigger_phrases: list[str] = Field(default_factory=list)
    steps: list[SkillStep] = Field(min_length=1)
    allowed_capabilities: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_run_ids: list[UUID] = Field(min_length=1)
    parent_version: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class GovernedSkillActivationRequest(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")


class GovernedSkillActivationResult(StrictModel):
    skill_id: UUID
    name: str
    version: str
    description: str
    steps: list[SkillStep]
    allowed_capabilities: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_run_count: int = Field(ge=1)


class SkillReplayStepResult(StrictModel):
    step_index: int = Field(ge=0)
    action: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    fixture_event_index: int | None = Field(default=None, ge=0)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    success: bool
    error_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,99}$",
    )
    duration_ms: int = Field(default=0, ge=0)


class SkillReplayCaseResult(StrictModel):
    run_id: UUID
    sandbox_revision: str = Field(min_length=1, max_length=200)
    baseline_action_count: int = Field(ge=0)
    candidate_action_count: int = Field(ge=0)
    sequence_similarity: float = Field(ge=0.0, le=1.0)
    tool_success_rate: float = Field(ge=0.0, le=1.0)
    completed: bool
    passed: bool
    steps: list[SkillReplayStepResult] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class SkillEvaluationCase(StrictModel):
    run_id: UUID
    baseline_score: float = Field(ge=0.0, le=1.0)
    candidate_score: float = Field(ge=0.0, le=1.0)
    sequence_similarity: float = Field(ge=0.0, le=1.0)
    tool_success_rate: float = Field(ge=0.0, le=1.0)
    unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    replay: SkillReplayCaseResult | None = None


class SkillEvaluation(StrictModel):
    evaluation_id: UUID = Field(default_factory=uuid4)
    skill_id: UUID
    tenant_id: str = "local"
    project_id: str = "default"
    skill_version: str = Field(default="0.0.0", pattern=r"^\d+\.\d+\.\d+$")
    evaluator_revision: str = Field(default="manual", min_length=1, max_length=200)
    baseline_score: float = Field(ge=0.0, le=1.0)
    candidate_score: float = Field(ge=0.0, le=1.0)
    unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    security_passed: bool
    regression_passed: bool
    case_count: int = Field(default=0, ge=0)
    passed_cases: int = Field(default=0, ge=0)
    cases: list[SkillEvaluationCase] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)


class SkillObservation(StrictModel):
    observation_id: UUID = Field(default_factory=uuid4)
    skill_id: UUID
    tenant_id: str = "local"
    project_id: str = "default"
    skill_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    evaluator_revision: str = Field(default="manual", min_length=1, max_length=200)
    run_id: UUID
    cohort: Literal["shadow", "canary", "active"]
    exposed: bool = False
    activated: bool = False
    simulated: bool = False
    baseline_score: float = Field(ge=0.0, le=1.0)
    candidate_score: float = Field(ge=0.0, le=1.0)
    unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    tool_success_rate: float = Field(ge=0.0, le=1.0)
    passed: bool
    signal_kind: Literal["run_outcome", "explicit_feedback"] = "run_outcome"
    feedback_score: float | None = Field(default=None, ge=-1.0, le=1.0)
    negative_feedback: bool = False
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_feedback_signal(self) -> Self:
        expected_negative = self.feedback_score is not None and self.feedback_score < 0.0
        if self.negative_feedback != expected_negative:
            raise ValueError("Skill observation feedback polarity is inconsistent")
        if self.feedback_score is not None and self.signal_kind != "explicit_feedback":
            raise ValueError("Scored feedback must be an explicit feedback signal")
        return self


class SkillPromotionEvidence(StrictModel):
    evidence_id: UUID
    skill_id: UUID
    tenant_id: str
    project_id: str
    skill_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    cohort: Literal["shadow", "canary", "active"]
    source_observation_ids: list[UUID] = Field(default_factory=list)
    observation_ids: list[UUID] = Field(default_factory=list)
    run_ids: list[UUID] = Field(default_factory=list)
    evaluator_revisions: list[str] = Field(default_factory=list)
    window_started_at: datetime | None = None
    window_ended_at: datetime | None = None
    required_observations: int = Field(ge=1)
    total_observations: int = Field(ge=0)
    evaluated_observations: int = Field(ge=0)
    exposed_observations: int = Field(ge=0)
    activated_observations: int = Field(ge=0)
    average_baseline_score: float = Field(ge=0.0, le=1.0)
    average_candidate_score: float = Field(ge=0.0, le=1.0)
    average_unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    negative_feedback_count: int = Field(ge=0)
    negative_feedback_rate: float = Field(ge=0.0, le=1.0)
    severe_negative_feedback_count: int = Field(ge=0)
    min_quality_score: float = Field(ge=0.0, le=1.0)
    max_score_regression: float = Field(ge=0.0, le=1.0)
    max_failure_rate: float = Field(ge=0.0, le=1.0)
    max_unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    max_negative_feedback_rate: float = Field(ge=0.0, le=1.0)
    severe_negative_feedback_threshold: float = Field(ge=-1.0, le=0.0)
    healthy: bool
    promotion_ready: bool
    recommended_action: Literal[
        "promote",
        "hold",
        "rollback_recommended",
        "rollback",
    ]
    reasons: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_trace_cardinality(self) -> Self:
        if len(self.source_observation_ids) != self.total_observations:
            raise ValueError("Promotion evidence must identify every source observation")
        if len(self.observation_ids) != self.evaluated_observations:
            raise ValueError("Promotion evidence must identify every evaluated observation")
        if len(self.run_ids) != self.evaluated_observations:
            raise ValueError("Promotion evidence must identify every evaluated run")
        if self.window_started_at is None and self.observation_ids:
            raise ValueError("Promotion evidence with observations requires a window")
        if self.window_ended_at is None and self.observation_ids:
            raise ValueError("Promotion evidence with observations requires a window")
        return self


class SkillHealthReport(StrictModel):
    skill_id: UUID
    skill_version: str
    cohort: Literal["shadow", "canary", "active"]
    required_observations: int = Field(ge=1)
    total_observations: int = Field(ge=0)
    evaluated_observations: int = Field(ge=0)
    exposed_observations: int = Field(ge=0)
    activated_observations: int = Field(ge=0)
    average_baseline_score: float = Field(ge=0.0, le=1.0)
    average_candidate_score: float = Field(ge=0.0, le=1.0)
    average_unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    healthy: bool
    promotion_ready: bool
    reasons: list[str] = Field(default_factory=list)
    promotion_evidence: SkillPromotionEvidence
    generated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_promotion_evidence(self) -> Self:
        evidence = self.promotion_evidence
        if (
            evidence.skill_id != self.skill_id
            or evidence.skill_version != self.skill_version
            or evidence.cohort != self.cohort
        ):
            raise ValueError("Skill health promotion evidence does not match its report")
        return self


class PromotionDecision(StrictModel):
    transition_id: UUID | None = None
    promotion_evidence_id: UUID | None = None
    skill_id: UUID
    from_status: SkillStatus
    to_status: SkillStatus
    allowed: bool
    reasons: list[str]
    decided_at: datetime = Field(default_factory=utc_now)


class SkillTransitionEvent(StrictModel):
    transition_id: UUID = Field(default_factory=uuid4)
    skill_id: UUID
    tenant_id: str = "local"
    project_id: str = "default"
    skill_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    transition_type: Literal["promotion", "rollback", "health_gate"]
    from_status: SkillStatus
    to_status: SkillStatus
    allowed: bool
    applied: bool
    reasons: list[str] = Field(min_length=1)
    evaluation_id: UUID | None = None
    promotion_evidence: SkillPromotionEvidence | None = None
    human_approved: bool = False
    learning_job_id: UUID | None = None
    decided_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_applied_decision(self) -> Self:
        if self.applied and not self.allowed:
            raise ValueError("A denied skill transition cannot be applied")
        evidence = self.promotion_evidence
        if evidence is not None and (
            evidence.skill_id != self.skill_id
            or evidence.tenant_id != self.tenant_id
            or evidence.project_id != self.project_id
            or evidence.skill_version != self.skill_version
        ):
            raise ValueError("Skill transition promotion evidence scope mismatch")
        return self


class SkillEvolutionResult(StrictModel):
    skill: SkillDefinition
    evaluation: SkillEvaluation
    transitions: list[PromotionDecision] = Field(default_factory=list)


class SkillEvolutionSnapshot(StrictModel):
    skill: SkillDefinition
    latest_evaluation: SkillEvaluation | None = None
    health: SkillHealthReport | None = None


class LearningChangeSet(StrictModel):
    change_set_id: UUID = Field(default_factory=uuid4)
    target_type: str
    target_id: str
    parent_version: str | None = None
    structured_diff: dict[str, Any]
    source_run_ids: list[UUID] = Field(min_length=1)
    expected_benefits: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    scope: dict[str, str] = Field(default_factory=dict)
    evaluation_report: dict[str, Any] = Field(default_factory=dict)
    rollback_conditions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


LearningJob.model_rebuild()
