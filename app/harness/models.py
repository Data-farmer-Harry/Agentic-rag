from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from app.domain.models import StrictModel, utc_now


def canonical_hash(value: Any) -> str:
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json", exclude={"payload_hash"})
    else:
        payload = value
    encoded = json.dumps(
        to_jsonable_python(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class HarnessDimension(StrEnum):
    CONTEXT = "context"
    TOOL = "tool"
    GENERATION = "generation"
    ORCHESTRATION = "orchestration"
    MEMORY = "memory"
    OUTPUT = "output"


class HarnessPatternStatus(StrEnum):
    DRAFT = "draft"
    OFFLINE_PASS = "offline_pass"
    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVE = "active"
    ROLLED_BACK = "rolled_back"
    DEPRECATED = "deprecated"


class HarnessOverlayMode(StrEnum):
    DISABLED = "disabled"
    OBSERVE = "observe"
    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVE = "active"


class HarnessReasonCode(StrEnum):
    CONTEXT_BUDGET_EXHAUSTED = "context_budget_exhausted"
    EVIDENCE_TRUNCATED = "evidence_truncated"
    PRIVATE_SOURCE_UNDERREPRESENTED = "private_source_underrepresented"
    PUBLIC_SOURCE_OVERREPRESENTED = "public_source_overrepresented"
    RELEVANT_MEMORY_NOT_IN_CAPSULE = "relevant_memory_not_in_capsule"
    IRRELEVANT_CAPSULE_PRESSURE = "irrelevant_capsule_pressure"

    REQUIRED_RETRIEVAL_NOT_CALLED = "required_retrieval_not_called"
    GRAPH_ENTITY_UNRESOLVED = "graph_entity_unresolved"
    GRAPH_PATH_MISSING = "graph_path_missing"
    RETRIEVAL_LOW_RECALL_SIGNAL = "retrieval_low_recall_signal"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_CONTRACT_ERROR = "tool_contract_error"
    REPEATED_IDENTICAL_TOOL_CALL = "repeated_identical_tool_call"
    WEB_REQUIRED_BUT_UNAVAILABLE = "web_required_but_unavailable"

    ANSWER_INCOMPLETE = "answer_incomplete"
    ANSWER_BUDGET_EXHAUSTED = "answer_budget_exhausted"
    INSTRUCTION_MISALIGNMENT = "instruction_misalignment"
    SUPPORTED_EVIDENCE_NOT_SYNTHESIZED = "supported_evidence_not_synthesized"
    UNNECESSARY_VERBOSITY_COST = "unnecessary_verbosity_cost"

    PREMATURE_STOP = "premature_stop"
    MAX_ROUNDS_WITHOUT_NEW_EVIDENCE = "max_rounds_without_new_evidence"
    SUBQUERY_DRIFT = "subquery_drift"
    COMPARE_BRANCH_MISSING = "compare_branch_missing"
    GRAPH_FOLLOWUP_MISSING = "graph_followup_missing"
    BRANCH_BUDGET_WASTED = "branch_budget_wasted"

    STALE_MEMORY_SELECTED = "stale_memory_selected"
    REVOKED_MEMORY_SELECTED = "revoked_memory_selected"
    MEMORY_CONFLICT_UNRESOLVED = "memory_conflict_unresolved"
    USER_PREFERENCE_OMITTED = "user_preference_omitted"
    EPISODIC_OVERFIT = "episodic_overfit"
    MEMORY_SCOPE_MISMATCH = "memory_scope_mismatch"

    CITATION_COVERAGE_BELOW_THRESHOLD = "citation_coverage_below_threshold"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    INVALID_OUTPUT_SCHEMA = "invalid_output_schema"
    PUBLISHER_REJECTED_EVIDENCE = "publisher_rejected_evidence"
    INSUFFICIENT_NOT_DECLARED = "insufficient_not_declared"
    COMPARISON_DIMENSION_MISSING = "comparison_dimension_missing"

    SECURITY_SCOPE_VIOLATION = "security_scope_violation"
    PROVIDER_FAILURE = "provider_failure"
    PERMISSION_DENIED = "permission_denied"
    USER_CANCELLED = "user_cancelled"
    UNKNOWN_FAILURE = "unknown_failure"


class HarnessContextConfig(StrictModel):
    max_capsule_tokens: int | None = Field(default=None, ge=256, le=32_000)
    private_evidence_quota: int | None = Field(default=None, ge=0, le=20)
    capsule_memory_limit: int | None = Field(default=None, ge=0, le=20)


class HarnessToolConfig(StrictModel):
    graph_hops: int | None = Field(default=None, ge=1, le=3)
    source_diversity_limit: int | None = Field(default=None, ge=1, le=10)
    allow_graph_followup: bool | None = None


class HarnessGenerationConfig(StrictModel):
    answer_style: Literal["concise", "balanced", "detailed"] | None = None
    comparison_dimension_limit: int | None = Field(default=None, ge=2, le=12)


class HarnessOrchestrationConfig(StrictModel):
    retrieval_profile: Literal[
        "lookup",
        "compare",
        "synthesis",
        "personal_recall",
        "visual_lookup",
    ] | None = None
    max_subqueries: int | None = Field(default=None, ge=1, le=4)
    max_retrieval_rounds: int | None = Field(default=None, ge=1, le=2)


class HarnessMemoryConfig(StrictModel):
    memory_type_quota: dict[
        Literal["episodic", "semantic", "procedural", "policy"],
        int,
    ] = Field(default_factory=dict)
    memory_min_confidence: float | None = Field(default=None, ge=0.6, le=1.0)

    @model_validator(mode="after")
    def validate_quotas(self) -> Self:
        if any(value < 0 or value > 20 for value in self.memory_type_quota.values()):
            raise ValueError("Memory quotas must be between 0 and 20")
        return self


class HarnessOutputConfig(StrictModel):
    minimum_citation_coverage: float | None = Field(default=None, ge=0.9, le=1.0)
    claim_support_mode: Literal["supported", "verified"] | None = None
    insufficient_evidence_behavior: Literal[
        "abstain",
        "ask_clarification",
        "retrieve_again",
    ] | None = None


class HarnessConfigDelta(StrictModel):
    context: HarnessContextConfig | None = None
    tool: HarnessToolConfig | None = None
    generation: HarnessGenerationConfig | None = None
    orchestration: HarnessOrchestrationConfig | None = None
    memory: HarnessMemoryConfig | None = None
    output: HarnessOutputConfig | None = None


class CaseFeatures(StrictModel):
    query_token_hashes: list[str] = Field(default_factory=list, max_length=128)
    language: Literal["zh", "en", "mixed", "unknown"] = "unknown"
    character_count: int = Field(ge=0, le=100_000)
    code_block_count: int = Field(ge=0, le=100)
    url_count: int = Field(ge=0, le=100)
    intents: list[
        Literal["lookup", "compare", "research", "debug", "summarize", "social"]
    ] = Field(default_factory=list, max_length=6)
    personal_knowledge: bool = False
    visual: bool = False
    graph_relations: bool = False
    temporal: bool = False
    code: bool = False
    tenant_id: str
    project_id: str
    domain_pack: str
    corpus_snapshot: str
    active_skill_versions: dict[str, str] = Field(default_factory=dict)
    policy_versions: dict[str, str] = Field(default_factory=dict)
    capability_allowlist_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    baseline_harness_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class HarnessQualityVector(StrictModel):
    quality_score: float = Field(ge=0.0, le=1.0)
    completion_score: float = Field(ge=0.0, le=1.0)
    tool_success_rate: float = Field(ge=0.0, le=1.0)
    citation_coverage: float = Field(ge=0.0, le=1.0)
    unsupported_claim_rate: float = Field(ge=0.0, le=1.0)


class HarnessCostVector(StrictModel):
    elapsed_ms: int | None = Field(default=None, ge=0)
    tool_duration_ms: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    token_usage_available: bool = False
    monetary_cost_usd: float | None = Field(default=None, ge=0.0)
    monetary_cost_available: bool = False

    @model_validator(mode="after")
    def validate_availability(self) -> Self:
        if self.token_usage_available != (
            self.input_tokens is not None and self.output_tokens is not None
        ):
            raise ValueError("Token availability must match token values")
        if self.monetary_cost_available != (self.monetary_cost_usd is not None):
            raise ValueError("Cost availability must match monetary cost")
        return self


class HarnessSecuritySignals(StrictModel):
    scope_violation: bool = False
    permission_denied: bool = False
    secret_detected: bool = False
    provider_failure: bool = False
    user_cancelled: bool = False


class HarnessDiagnosis(StrictModel):
    success: bool
    learnable: bool
    primary_dimension: HarnessDimension | None = None
    secondary_dimensions: list[HarnessDimension] = Field(default_factory=list, max_length=2)
    reason_codes: list[HarnessReasonCode] = Field(default_factory=list, max_length=20)
    quality_vector: HarnessQualityVector
    cost_vector: HarnessCostVector
    security_signals: HarnessSecuritySignals
    evidence_ids_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    diagnoser_revision: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_dimensions(self) -> Self:
        if self.primary_dimension in self.secondary_dimensions:
            raise ValueError("Primary diagnosis cannot also be secondary")
        if len(self.secondary_dimensions) != len(set(self.secondary_dimensions)):
            raise ValueError("Secondary diagnosis dimensions must be unique")
        return self


class HarnessToolSummary(StrictModel):
    tool_name_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    call_count: int = Field(ge=1)
    success_count: int = Field(ge=0)
    total_duration_ms: int = Field(ge=0)


class HarnessRewardVector(StrictModel):
    passed: bool
    quality_score: float = Field(ge=0.0, le=1.0)
    feedback_score: float | None = Field(default=None, ge=-1.0, le=1.0)


class HarnessExperienceEntry(StrictModel):
    experience_id: UUID
    tenant_id: str
    project_id: str
    user_id: str
    run_id: UUID
    task_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    case_features: CaseFeatures
    snapshot_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    baseline_policy_versions: dict[str, str] = Field(default_factory=dict)
    overlay_id: UUID | None = None
    overlay_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    applied_pattern_versions: list[str] = Field(default_factory=list, max_length=20)
    config_delta: HarnessConfigDelta = Field(default_factory=HarnessConfigDelta)
    trajectory_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    tool_sequence_summary: list[HarnessToolSummary] = Field(
        default_factory=list,
        max_length=50,
    )
    diagnosis: HarnessDiagnosis
    reward_vector: HarnessRewardVector
    native_change_set_ids: list[UUID] = Field(default_factory=list, max_length=100)
    created_at: datetime = Field(default_factory=utc_now)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.case_features.tenant_id != self.tenant_id:
            raise ValueError("Experience tenant scope does not match case features")
        if self.case_features.project_id != self.project_id:
            raise ValueError("Experience project scope does not match case features")
        if canonical_hash(self) != self.payload_hash:
            raise ValueError("Experience payload hash does not match its content")
        return self


class HarnessExperienceEvaluation(StrictModel):
    evaluation_id: UUID
    experience_id: UUID
    tenant_id: str
    project_id: str
    run_id: UUID
    signal_kind: Literal["run_outcome", "explicit_feedback"]
    trigger: Literal["run_completed", "feedback_received"]
    quality_vector: HarnessQualityVector
    reward_vector: HarnessRewardVector
    native_change_set_ids: list[UUID] = Field(default_factory=list, max_length=100)
    evaluator_revision: str = Field(min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.signal_kind == "explicit_feedback" and self.reward_vector.feedback_score is None:
            raise ValueError("Explicit feedback evaluations require a feedback score")
        if canonical_hash(self) != self.payload_hash:
            raise ValueError("Experience evaluation hash does not match its content")
        return self


class HarnessTriggerPredicate(StrictModel):
    domain_pack: str = Field(min_length=1, max_length=100)
    primary_intent: Literal[
        "lookup",
        "compare",
        "research",
        "debug",
        "summarize",
    ]
    language: Literal["zh", "en", "mixed", "unknown"] | None = None
    personal_knowledge: bool | None = None
    visual: bool | None = None
    graph_relations: bool | None = None
    required_reason_codes: list[HarnessReasonCode] = Field(
        default_factory=list,
        min_length=1,
        max_length=5,
    )


class HarnessPattern(StrictModel):
    pattern_id: UUID
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    parent_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")
    tenant_id: str
    project_id: str
    name: str = Field(min_length=3, max_length=200)
    trigger_predicate: HarnessTriggerPredicate
    dimensions: list[HarnessDimension] = Field(min_length=1, max_length=6)
    proposed_delta: HarnessConfigDelta
    supporting_experience_ids: list[UUID] = Field(default_factory=list)
    contradicting_experience_ids: list[UUID] = Field(default_factory=list)
    support_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    estimated_quality_lift: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    status: HarnessPatternStatus = HarnessPatternStatus.DRAFT
    miner_revision: str = Field(min_length=1, max_length=200)
    evaluator_revision: str = Field(min_length=1, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if canonical_hash(self) != self.payload_hash:
            raise ValueError("Harness pattern hash does not match its content")
        return self


class HarnessPatternEvaluationCase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{2,199}$")
    experience_id: UUID | None = None
    role: Literal["supporting", "contradicting", "required"]
    baseline_quality_score: float = Field(ge=0.0, le=1.0)
    candidate_quality_score: float = Field(ge=0.0, le=1.0)
    baseline_unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    candidate_unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    passed: bool
    reasons: list[str] = Field(default_factory=list, min_length=1, max_length=20)


class HarnessPatternEvaluation(StrictModel):
    evaluation_id: UUID
    pattern_id: UUID
    pattern_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    tenant_id: str
    project_id: str
    evaluator_revision: str = Field(min_length=1, max_length=200)
    dataset_revision: str = Field(min_length=1, max_length=200)
    pattern_payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    baseline_quality_score: float = Field(ge=0.0, le=1.0)
    candidate_quality_score: float = Field(ge=0.0, le=1.0)
    estimated_quality_lift: float = Field(ge=-1.0, le=1.0)
    baseline_unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    candidate_unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    support_case_count: int = Field(ge=0)
    contradiction_case_count: int = Field(ge=0)
    required_case_count: int = Field(ge=1)
    passed_case_count: int = Field(ge=0)
    security_passed: bool
    evidence_integrity_passed: bool
    required_cases_passed: bool
    regression_passed: bool
    consumer_compatible: bool
    allowed_consumer_fields: list[str] = Field(default_factory=list, max_length=50)
    rejected_consumer_fields: list[str] = Field(default_factory=list, max_length=50)
    required_case_ids: list[str] = Field(default_factory=list, min_length=1)
    failed_required_case_ids: list[str] = Field(default_factory=list)
    cases: list[HarnessPatternEvaluationCase] = Field(
        default_factory=list,
        max_length=1_100,
    )
    reasons: list[str] = Field(default_factory=list, min_length=1, max_length=100)
    generated_at: datetime = Field(default_factory=utc_now)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        roles = [item.role for item in self.cases]
        if roles.count("supporting") != self.support_case_count:
            raise ValueError("Pattern evaluation support case count is inconsistent")
        if roles.count("contradicting") != self.contradiction_case_count:
            raise ValueError("Pattern evaluation contradiction case count is inconsistent")
        if roles.count("required") != self.required_case_count:
            raise ValueError("Pattern evaluation required case count is inconsistent")
        if sum(item.passed for item in self.cases) != self.passed_case_count:
            raise ValueError("Pattern evaluation passed case count is inconsistent")
        required = [item.case_id for item in self.cases if item.role == "required"]
        failed_required = [
            item.case_id
            for item in self.cases
            if item.role == "required" and not item.passed
        ]
        if self.required_case_ids != required:
            raise ValueError("Pattern evaluation required case IDs are inconsistent")
        if self.failed_required_case_ids != failed_required:
            raise ValueError("Pattern evaluation failed required case IDs are inconsistent")
        if self.required_cases_passed != (not failed_required):
            raise ValueError("Pattern evaluation required-case gate is inconsistent")
        if canonical_hash(self) != self.payload_hash:
            raise ValueError("Harness pattern evaluation hash does not match its content")
        return self


class HarnessPatternPromotionEvidence(StrictModel):
    evidence_id: UUID
    pattern_id: UUID
    pattern_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    tenant_id: str
    project_id: str
    evaluation_id: UUID
    evaluation_payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evaluator_revision: str = Field(min_length=1, max_length=200)
    dataset_revision: str = Field(min_length=1, max_length=200)
    supporting_experience_ids: list[UUID] = Field(default_factory=list)
    contradicting_experience_ids: list[UUID] = Field(default_factory=list)
    required_case_ids: list[str] = Field(default_factory=list, min_length=1)
    failed_required_case_ids: list[str] = Field(default_factory=list)
    min_support_cases: int = Field(ge=1)
    max_score_regression: float = Field(ge=0.0, le=1.0)
    baseline_quality_score: float = Field(ge=0.0, le=1.0)
    candidate_quality_score: float = Field(ge=0.0, le=1.0)
    estimated_quality_lift: float = Field(ge=-1.0, le=1.0)
    security_passed: bool
    evidence_integrity_passed: bool
    required_cases_passed: bool
    regression_passed: bool
    offline_ready: bool
    consumer_compatible: bool
    recommended_action: Literal["promote_shadow", "hold"]
    reasons: list[str] = Field(default_factory=list, min_length=1, max_length=100)
    generated_at: datetime = Field(default_factory=utc_now)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.offline_ready != (
            self.security_passed
            and self.evidence_integrity_passed
            and self.required_cases_passed
            and self.regression_passed
            and not self.failed_required_case_ids
            and self.recommended_action == "promote_shadow"
        ):
            raise ValueError("Pattern promotion evidence readiness is inconsistent")
        if canonical_hash(self) != self.payload_hash:
            raise ValueError("Harness promotion evidence hash does not match its content")
        return self


class HarnessPatternTransition(StrictModel):
    transition_id: UUID
    pattern_id: UUID
    pattern_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    tenant_id: str
    project_id: str
    transition_type: Literal["promotion", "rollback", "health_gate"]
    from_status: HarnessPatternStatus
    to_status: HarnessPatternStatus
    allowed: bool
    applied: bool
    reasons: list[str] = Field(min_length=1, max_length=100)
    evaluation_id: UUID | None = None
    evaluation_payload_hash: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    promotion_evidence_id: UUID | None = None
    promotion_evidence_payload_hash: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    human_approved: bool = False
    actor: Literal["system", "human", "health_monitor"] = "system"
    learning_job_id: UUID | None = None
    decided_at: datetime = Field(default_factory=utc_now)
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.applied and not self.allowed:
            raise ValueError("A denied Harness pattern transition cannot be applied")
        if (self.evaluation_id is None) != (self.evaluation_payload_hash is None):
            raise ValueError("Pattern transition evaluation identity and hash must coexist")
        if (self.promotion_evidence_id is None) != (
            self.promotion_evidence_payload_hash is None
        ):
            raise ValueError(
                "Pattern transition promotion evidence identity and hash must coexist"
            )
        if canonical_hash(self) != self.payload_hash:
            raise ValueError("Harness pattern transition hash does not match its content")
        return self


class HarnessPatternEvolutionResult(StrictModel):
    pattern: HarnessPattern
    effective_status: HarnessPatternStatus
    evaluation: HarnessPatternEvaluation
    promotion_evidence: HarnessPatternPromotionEvidence
    transitions: list[HarnessPatternTransition] = Field(default_factory=list)


class RunHarnessOverlay(StrictModel):
    overlay_id: UUID
    run_id: UUID
    tenant_id: str
    project_id: str
    baseline_policy_versions: dict[str, str] = Field(default_factory=dict)
    selected_pattern_versions: list[str] = Field(default_factory=list, max_length=3)
    positive_experience_ids: list[UUID] = Field(default_factory=list, max_length=3)
    negative_experience_ids: list[UUID] = Field(default_factory=list, max_length=3)
    effective_delta: HarnessConfigDelta
    clamped_fields: list[str] = Field(default_factory=list, max_length=50)
    rejected_conflicts: list[str] = Field(default_factory=list, max_length=50)
    selection_trace_codes: list[str] = Field(default_factory=list, max_length=50)
    selector_revision: str = Field(min_length=1, max_length=200)
    experience_bank_revision: str = Field(min_length=1, max_length=200)
    pattern_bank_revision: str = Field(min_length=1, max_length=200)
    mode: HarnessOverlayMode
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    payload_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if canonical_hash(self) != self.payload_hash:
            raise ValueError("Harness overlay hash does not match its content")
        return self
