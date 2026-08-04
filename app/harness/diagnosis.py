from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC
from typing import Any

from app.domain.enums import AnswerMode, EvidenceLevel, RunStatus
from app.domain.models import RunTrajectory
from app.harness.models import (
    HarnessCostVector,
    HarnessDiagnosis,
    HarnessDimension,
    HarnessQualityVector,
    HarnessReasonCode,
    HarnessSecuritySignals,
)

DIAGNOSER_REVISION = "deterministic-harness-d1-d6-v1"
EVALUATOR_REVISION = "deterministic-harness-quality-v1"

_REASON_DIMENSION = {
    HarnessReasonCode.CONTEXT_BUDGET_EXHAUSTED: HarnessDimension.CONTEXT,
    HarnessReasonCode.EVIDENCE_TRUNCATED: HarnessDimension.CONTEXT,
    HarnessReasonCode.PRIVATE_SOURCE_UNDERREPRESENTED: HarnessDimension.CONTEXT,
    HarnessReasonCode.PUBLIC_SOURCE_OVERREPRESENTED: HarnessDimension.CONTEXT,
    HarnessReasonCode.RELEVANT_MEMORY_NOT_IN_CAPSULE: HarnessDimension.CONTEXT,
    HarnessReasonCode.IRRELEVANT_CAPSULE_PRESSURE: HarnessDimension.CONTEXT,
    HarnessReasonCode.REQUIRED_RETRIEVAL_NOT_CALLED: HarnessDimension.TOOL,
    HarnessReasonCode.GRAPH_ENTITY_UNRESOLVED: HarnessDimension.TOOL,
    HarnessReasonCode.GRAPH_PATH_MISSING: HarnessDimension.TOOL,
    HarnessReasonCode.RETRIEVAL_LOW_RECALL_SIGNAL: HarnessDimension.TOOL,
    HarnessReasonCode.TOOL_TIMEOUT: HarnessDimension.TOOL,
    HarnessReasonCode.TOOL_CONTRACT_ERROR: HarnessDimension.TOOL,
    HarnessReasonCode.REPEATED_IDENTICAL_TOOL_CALL: HarnessDimension.TOOL,
    HarnessReasonCode.WEB_REQUIRED_BUT_UNAVAILABLE: HarnessDimension.TOOL,
    HarnessReasonCode.ANSWER_INCOMPLETE: HarnessDimension.GENERATION,
    HarnessReasonCode.ANSWER_BUDGET_EXHAUSTED: HarnessDimension.GENERATION,
    HarnessReasonCode.INSTRUCTION_MISALIGNMENT: HarnessDimension.GENERATION,
    HarnessReasonCode.SUPPORTED_EVIDENCE_NOT_SYNTHESIZED: HarnessDimension.GENERATION,
    HarnessReasonCode.UNNECESSARY_VERBOSITY_COST: HarnessDimension.GENERATION,
    HarnessReasonCode.PREMATURE_STOP: HarnessDimension.ORCHESTRATION,
    HarnessReasonCode.MAX_ROUNDS_WITHOUT_NEW_EVIDENCE: HarnessDimension.ORCHESTRATION,
    HarnessReasonCode.SUBQUERY_DRIFT: HarnessDimension.ORCHESTRATION,
    HarnessReasonCode.COMPARE_BRANCH_MISSING: HarnessDimension.ORCHESTRATION,
    HarnessReasonCode.GRAPH_FOLLOWUP_MISSING: HarnessDimension.ORCHESTRATION,
    HarnessReasonCode.BRANCH_BUDGET_WASTED: HarnessDimension.ORCHESTRATION,
    HarnessReasonCode.STALE_MEMORY_SELECTED: HarnessDimension.MEMORY,
    HarnessReasonCode.REVOKED_MEMORY_SELECTED: HarnessDimension.MEMORY,
    HarnessReasonCode.MEMORY_CONFLICT_UNRESOLVED: HarnessDimension.MEMORY,
    HarnessReasonCode.USER_PREFERENCE_OMITTED: HarnessDimension.MEMORY,
    HarnessReasonCode.EPISODIC_OVERFIT: HarnessDimension.MEMORY,
    HarnessReasonCode.MEMORY_SCOPE_MISMATCH: HarnessDimension.MEMORY,
    HarnessReasonCode.CITATION_COVERAGE_BELOW_THRESHOLD: HarnessDimension.OUTPUT,
    HarnessReasonCode.UNSUPPORTED_CLAIM: HarnessDimension.OUTPUT,
    HarnessReasonCode.INVALID_OUTPUT_SCHEMA: HarnessDimension.OUTPUT,
    HarnessReasonCode.PUBLISHER_REJECTED_EVIDENCE: HarnessDimension.OUTPUT,
    HarnessReasonCode.INSUFFICIENT_NOT_DECLARED: HarnessDimension.OUTPUT,
    HarnessReasonCode.COMPARISON_DIMENSION_MISSING: HarnessDimension.OUTPUT,
}

_DIMENSION_PRIORITY = (
    HarnessDimension.OUTPUT,
    HarnessDimension.TOOL,
    HarnessDimension.ORCHESTRATION,
    HarnessDimension.CONTEXT,
    HarnessDimension.MEMORY,
    HarnessDimension.GENERATION,
)


def evaluate_trajectory(trajectory: RunTrajectory) -> HarnessQualityVector:
    answer = trajectory.answer
    completed = trajectory.status == RunStatus.COMPLETED and answer is not None
    completion_score = 1.0 if completed else 0.0
    tool_success_rate = (
        sum(event.success for event in trajectory.tool_events) / len(trajectory.tool_events)
        if trajectory.tool_events
        else 1.0
    )
    claims = answer.claims if answer is not None else []
    conversational = answer is not None and answer.response_mode == AnswerMode.CONVERSATIONAL
    if conversational:
        citation_coverage = 1.0
        unsupported_claim_rate = 0.0
    elif claims:
        supported = sum(
            bool(claim.evidence_ids)
            and claim.level in {EvidenceLevel.VERIFIED, EvidenceLevel.SUPPORTED}
            for claim in claims
        )
        unsupported = sum(
            not claim.evidence_ids
            or claim.level in {EvidenceLevel.INFERRED, EvidenceLevel.INSUFFICIENT}
            for claim in claims
        )
        citation_coverage = supported / len(claims)
        unsupported_claim_rate = unsupported / len(claims)
    elif answer is not None and answer.citations:
        citation_coverage = 1.0
        unsupported_claim_rate = 0.0
    else:
        citation_coverage = 0.0
        unsupported_claim_rate = 1.0 if answer is not None else 0.0
    normalized_feedback = (
        (trajectory.feedback_score + 1.0) / 2.0
        if trajectory.feedback_score is not None
        else 0.5
    )
    quality = (
        0.35 * completion_score
        + 0.20 * tool_success_rate
        + 0.30 * citation_coverage
        + 0.15 * normalized_feedback
    )
    return HarnessQualityVector(
        quality_score=round(quality, 6),
        completion_score=completion_score,
        tool_success_rate=round(tool_success_rate, 6),
        citation_coverage=round(citation_coverage, 6),
        unsupported_claim_rate=round(unsupported_claim_rate, 6),
    )


def diagnose_trajectory(
    trajectory: RunTrajectory,
    *,
    quality: HarnessQualityVector | None = None,
) -> HarnessDiagnosis:
    quality = quality or evaluate_trajectory(trajectory)
    signals = _security_signals(trajectory)
    reasons = _reason_codes(trajectory, quality, signals)
    dimensions = [
        dimension
        for dimension in _DIMENSION_PRIORITY
        if any(_REASON_DIMENSION.get(reason) == dimension for reason in reasons)
    ]
    unlearnable = (
        signals.scope_violation
        or signals.permission_denied
        or signals.secret_detected
        or signals.provider_failure
        or signals.user_cancelled
        or (
            trajectory.status == RunStatus.FAILED
            and not any(reason in _REASON_DIMENSION for reason in reasons)
        )
    )
    passed = (
        trajectory.status == RunStatus.COMPLETED
        and trajectory.answer is not None
        and quality.quality_score >= 0.65
        and quality.unsupported_claim_rate <= 0.2
        and (trajectory.feedback_score is None or trajectory.feedback_score >= 0.0)
        and not any(
            reason
            in {
                HarnessReasonCode.MEMORY_SCOPE_MISMATCH,
                HarnessReasonCode.REVOKED_MEMORY_SELECTED,
                HarnessReasonCode.SECURITY_SCOPE_VIOLATION,
            }
            for reason in reasons
        )
    )
    return HarnessDiagnosis(
        success=passed,
        learnable=not unlearnable,
        primary_dimension=dimensions[0] if dimensions else None,
        secondary_dimensions=dimensions[1:3],
        reason_codes=reasons,
        quality_vector=quality,
        cost_vector=_cost_vector(trajectory),
        security_signals=signals,
        evidence_ids_hash=_evidence_ids_hash(trajectory),
        diagnoser_revision=DIAGNOSER_REVISION,
    )


def _reason_codes(
    trajectory: RunTrajectory,
    quality: HarnessQualityVector,
    signals: HarnessSecuritySignals,
) -> list[HarnessReasonCode]:
    reasons: list[HarnessReasonCode] = []
    if signals.scope_violation:
        reasons.append(HarnessReasonCode.SECURITY_SCOPE_VIOLATION)
    if signals.provider_failure:
        reasons.append(HarnessReasonCode.PROVIDER_FAILURE)
    if signals.permission_denied:
        reasons.append(HarnessReasonCode.PERMISSION_DENIED)
    if signals.user_cancelled:
        reasons.append(HarnessReasonCode.USER_CANCELLED)

    details = [_flatten(event.detail) for event in trajectory.tool_events]
    tags = {tag.casefold() for tag in trajectory.tags}
    if any("truncat" in detail for detail in details) or "evidence_truncated" in tags:
        reasons.append(HarnessReasonCode.EVIDENCE_TRUNCATED)
    if any(not event.success for event in trajectory.tool_events):
        if any("timeout" in detail or "timed out" in detail for detail in details):
            reasons.append(HarnessReasonCode.TOOL_TIMEOUT)
        else:
            reasons.append(HarnessReasonCode.TOOL_CONTRACT_ERROR)
    calls = Counter((event.tool_name, event.input_hash) for event in trajectory.tool_events)
    if any(count > 1 for count in calls.values()):
        reasons.append(HarnessReasonCode.REPEATED_IDENTICAL_TOOL_CALL)
    if "graph_entity_unresolved" in tags:
        reasons.append(HarnessReasonCode.GRAPH_ENTITY_UNRESOLVED)
    if "graph_path_missing" in tags:
        reasons.append(HarnessReasonCode.GRAPH_PATH_MISSING)
    if "max_rounds_without_new_evidence" in tags:
        reasons.append(HarnessReasonCode.MAX_ROUNDS_WITHOUT_NEW_EVIDENCE)
    if "compare_branch_missing" in tags:
        reasons.append(HarnessReasonCode.COMPARE_BRANCH_MISSING)
    if "graph_followup_missing" in tags:
        reasons.append(HarnessReasonCode.GRAPH_FOLLOWUP_MISSING)
    if "memory_scope_mismatch" in tags:
        reasons.append(HarnessReasonCode.MEMORY_SCOPE_MISMATCH)
    if "revoked_memory_selected" in tags:
        reasons.append(HarnessReasonCode.REVOKED_MEMORY_SELECTED)
    if "stale_memory_selected" in tags:
        reasons.append(HarnessReasonCode.STALE_MEMORY_SELECTED)

    answer = trajectory.answer
    conversational = answer is not None and answer.response_mode == AnswerMode.CONVERSATIONAL
    if trajectory.status == RunStatus.COMPLETED and answer is None:
        reasons.append(HarnessReasonCode.ANSWER_INCOMPLETE)
    if not conversational and quality.citation_coverage < 0.9:
        reasons.append(HarnessReasonCode.CITATION_COVERAGE_BELOW_THRESHOLD)
    if not conversational and quality.unsupported_claim_rate > 0.2:
        reasons.append(HarnessReasonCode.UNSUPPORTED_CLAIM)
    if (
        answer is not None
        and answer.confidence == EvidenceLevel.INSUFFICIENT
        and not answer.limitations
        and not conversational
    ):
        reasons.append(HarnessReasonCode.INSUFFICIENT_NOT_DECLARED)
    if trajectory.status == RunStatus.FAILED and not reasons:
        reasons.append(HarnessReasonCode.UNKNOWN_FAILURE)
    return list(dict.fromkeys(reasons))[:20]


def _security_signals(trajectory: RunTrajectory) -> HarnessSecuritySignals:
    values = [
        " ".join(trajectory.tags),
        *(_flatten(event.detail) for event in trajectory.tool_events),
    ]
    text = " ".join(values).casefold()
    return HarnessSecuritySignals(
        scope_violation=any(
            term in text
            for term in (
                "scope_violation",
                "scope mismatch",
                "cross-tenant",
                "cross tenant",
            )
        ),
        permission_denied=any(
            term in text for term in ("permission_denied", "permission denied", "forbidden")
        ),
        secret_detected=any(
            term in text for term in ("secret_detected", "credential_leak", "private_key")
        ),
        provider_failure=any(
            term in text
            for term in (
                "provider_failure",
                "upstream_error",
                "bad gateway",
                "connection reset",
                "unexpected eof",
            )
        ),
        user_cancelled=trajectory.status == RunStatus.CANCELLED,
    )


def _cost_vector(trajectory: RunTrajectory) -> HarnessCostVector:
    elapsed_ms = None
    if trajectory.completed_at is not None:
        start = trajectory.context.started_at
        end = trajectory.completed_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        elapsed_ms = max(0, round((end - start).total_seconds() * 1_000))
    return HarnessCostVector(
        elapsed_ms=elapsed_ms,
        tool_duration_ms=sum(event.duration_ms for event in trajectory.tool_events),
        tool_call_count=len(trajectory.tool_events),
    )


def _evidence_ids_hash(trajectory: RunTrajectory) -> str:
    answer = trajectory.answer
    evidence_ids = sorted(str(item.evidence_id) for item in answer.citations) if answer else []
    encoded = json.dumps(evidence_ids, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _flatten(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).casefold()
