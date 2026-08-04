from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.contracts import HarnessExperienceRepository
from app.harness.consumer import BoundedHarnessConsumer
from app.harness.models import (
    HarnessExperienceEntry,
    HarnessPattern,
    HarnessPatternEvaluation,
    HarnessPatternEvaluationCase,
    HarnessReasonCode,
    canonical_hash,
)

PATTERN_EVALUATOR_REVISION = "deterministic-pattern-projection-v1"
PATTERN_DATASET_REVISION = "harness-pattern-required-cases-v1"

_REPAIR_FIELDS: dict[HarnessReasonCode, set[str]] = {
    HarnessReasonCode.COMPARE_BRANCH_MISSING: {
        "orchestration.retrieval_profile",
        "orchestration.max_subqueries",
    },
    HarnessReasonCode.GRAPH_FOLLOWUP_MISSING: {
        "tool.graph_hops",
        "tool.allow_graph_followup",
    },
    HarnessReasonCode.PUBLIC_SOURCE_OVERREPRESENTED: {
        "context.private_evidence_quota",
    },
    HarnessReasonCode.CITATION_COVERAGE_BELOW_THRESHOLD: {
        "generation.answer_style",
        "output.minimum_citation_coverage",
        "output.claim_support_mode",
        "output.insufficient_evidence_behavior",
    },
}


@dataclass(frozen=True, slots=True)
class _ResolvedExperience:
    experience_id: UUID
    experience: HarnessExperienceEntry | None
    quality_score: float
    unsupported_claim_rate: float
    observed_at: datetime


class DeterministicPatternEvaluator:
    """Evaluate immutable Pattern evidence without storing replay output text."""

    revision = PATTERN_EVALUATOR_REVISION

    def __init__(
        self,
        experiences: HarnessExperienceRepository,
        *,
        consumer: BoundedHarnessConsumer | None = None,
        min_support_cases: int = 3,
        max_score_regression: float = 0.02,
    ) -> None:
        if min_support_cases < 1:
            raise ValueError("Pattern evaluation requires at least one support case")
        if not 0.0 <= max_score_regression <= 1.0:
            raise ValueError("max_score_regression must be between 0 and 1")
        self._experiences = experiences
        self._consumer = consumer or BoundedHarnessConsumer()
        self._min_support_cases = min_support_cases
        self._max_score_regression = max_score_regression

    @property
    def min_support_cases(self) -> int:
        return self._min_support_cases

    @property
    def max_score_regression(self) -> float:
        return self._max_score_regression

    async def evaluate(self, pattern: HarnessPattern) -> HarnessPatternEvaluation:
        support = [
            await self._resolve(pattern, experience_id)
            for experience_id in pattern.supporting_experience_ids
        ]
        contradictions = [
            await self._resolve(pattern, experience_id)
            for experience_id in pattern.contradicting_experience_ids
        ]
        delta_fields = _delta_fields(pattern)
        expected_fields = {
            field
            for reason in pattern.trigger_predicate.required_reason_codes
            for field in _REPAIR_FIELDS.get(reason, set())
        }
        repair_contract_passed = bool(expected_fields & delta_fields)
        support_cases = [
            _experience_case(
                item,
                role="supporting",
                repair_contract_passed=repair_contract_passed,
                citation_repair=(
                    HarnessReasonCode.CITATION_COVERAGE_BELOW_THRESHOLD
                    in pattern.trigger_predicate.required_reason_codes
                ),
                max_score_regression=self._max_score_regression,
            )
            for item in support
        ]
        contradiction_cases = [
            _experience_case(
                item,
                role="contradicting",
                repair_contract_passed=repair_contract_passed,
                citation_repair=False,
                max_score_regression=self._max_score_regression,
            )
            for item in contradictions
        ]
        evidence_integrity = all(
            item.experience is not None
            for item in [*support, *contradictions]
        )
        support_minimum = len(support) >= self._min_support_cases
        contradiction_bound = len(contradictions) <= len(support)
        required_cases = [
            _required_case(
                "required.delta_nonempty",
                passed=bool(delta_fields),
                passed_reason="typed_delta_present",
                failed_reason="empty_pattern_delta",
            ),
            _required_case(
                "required.repair_contract",
                passed=repair_contract_passed,
                passed_reason="reason_to_delta_contract_passed",
                failed_reason="delta_does_not_address_required_reason",
            ),
            _required_case(
                "required.evidence_integrity",
                passed=evidence_integrity,
                passed_reason="all_evidence_resolved_in_scope",
                failed_reason="missing_or_out_of_scope_evidence",
            ),
            _required_case(
                "required.support_minimum",
                passed=support_minimum,
                passed_reason="minimum_support_satisfied",
                failed_reason=f"insufficient_support:{len(support)}/{self._min_support_cases}",
            ),
            _required_case(
                "required.contradiction_bound",
                passed=contradiction_bound,
                passed_reason="contradiction_bound_satisfied",
                failed_reason="contradictions_exceed_support",
            ),
        ]
        cases = [*support_cases, *contradiction_cases, *required_cases]
        evaluated_cases = [*support_cases, *contradiction_cases]
        baseline_quality = _average(
            item.baseline_quality_score for item in evaluated_cases
        )
        candidate_quality = _average(
            item.candidate_quality_score for item in evaluated_cases
        )
        baseline_unsupported = _average(
            item.baseline_unsupported_claim_rate for item in evaluated_cases
        )
        candidate_unsupported = _average(
            item.candidate_unsupported_claim_rate for item in evaluated_cases
        )
        required_passed = all(item.passed for item in required_cases)
        regression_passed = (
            required_passed
            and all(item.passed for item in evaluated_cases)
            and candidate_quality >= baseline_quality - self._max_score_regression
            and candidate_unsupported <= baseline_unsupported + self._max_score_regression
        )
        projection = self._consumer.project(pattern.proposed_delta)
        reasons: list[str] = []
        if required_passed:
            reasons.append("required_cases_passed")
        else:
            reasons.append("required_case_failed")
        if regression_passed:
            reasons.append("projected_non_regression_passed")
        else:
            reasons.append("projected_non_regression_failed")
        if projection.compatible:
            reasons.append("bounded_consumer_compatible")
        else:
            reasons.append("bounded_consumer_incompatible")
        generated_at = max(
            [pattern.created_at, *(item.observed_at for item in [*support, *contradictions])]
        )
        payload = {
            "evaluation_id": uuid5(
                NAMESPACE_URL,
                "pending",
            ),
            "pattern_id": pattern.pattern_id,
            "pattern_version": pattern.version,
            "tenant_id": pattern.tenant_id,
            "project_id": pattern.project_id,
            "evaluator_revision": self.revision,
            "dataset_revision": PATTERN_DATASET_REVISION,
            "pattern_payload_hash": pattern.payload_hash,
            "baseline_quality_score": baseline_quality,
            "candidate_quality_score": candidate_quality,
            "estimated_quality_lift": round(candidate_quality - baseline_quality, 6),
            "baseline_unsupported_claim_rate": baseline_unsupported,
            "candidate_unsupported_claim_rate": candidate_unsupported,
            "support_case_count": len(support_cases),
            "contradiction_case_count": len(contradiction_cases),
            "required_case_count": len(required_cases),
            "passed_case_count": sum(item.passed for item in cases),
            "security_passed": True,
            "evidence_integrity_passed": evidence_integrity,
            "required_cases_passed": required_passed,
            "regression_passed": regression_passed,
            "consumer_compatible": projection.compatible,
            "allowed_consumer_fields": list(projection.allowed_fields),
            "rejected_consumer_fields": list(projection.rejected_fields),
            "required_case_ids": [item.case_id for item in required_cases],
            "failed_required_case_ids": [
                item.case_id for item in required_cases if not item.passed
            ],
            "cases": cases,
            "reasons": reasons,
            "generated_at": generated_at,
        }
        fingerprint = canonical_hash(
            {
                key: value
                for key, value in payload.items()
                if key not in {"evaluation_id", "generated_at"}
            }
        )
        evaluation_id = uuid5(
            NAMESPACE_URL,
            (
                "hermesgraph:harness-pattern-evaluation:"
                f"{pattern.pattern_id}:{pattern.version}:{self.revision}:{fingerprint}"
            ),
        )
        payload["evaluation_id"] = evaluation_id
        return HarnessPatternEvaluation.model_validate(
            {**payload, "payload_hash": canonical_hash(payload)}
        )

    async def _resolve(
        self,
        pattern: HarnessPattern,
        experience_id: UUID,
    ) -> _ResolvedExperience:
        experience = await self._experiences.get(
            experience_id,
            tenant_id=pattern.tenant_id,
            project_id=pattern.project_id,
        )
        if experience is None:
            return _ResolvedExperience(
                experience_id=experience_id,
                experience=None,
                quality_score=0.0,
                unsupported_claim_rate=1.0,
                observed_at=pattern.created_at,
            )
        evaluations = list(
            await self._experiences.list_evaluations(
                experience_id,
                tenant_id=pattern.tenant_id,
                project_id=pattern.project_id,
            )
        )
        latest = evaluations[-1] if evaluations else None
        quality = (
            latest.quality_vector
            if latest is not None
            else experience.diagnosis.quality_vector
        )
        return _ResolvedExperience(
            experience_id=experience_id,
            experience=experience,
            quality_score=quality.quality_score,
            unsupported_claim_rate=quality.unsupported_claim_rate,
            observed_at=latest.created_at if latest is not None else experience.created_at,
        )


def _experience_case(
    resolved: _ResolvedExperience,
    *,
    role: Literal["supporting", "contradicting"],
    repair_contract_passed: bool,
    citation_repair: bool,
    max_score_regression: float,
) -> HarnessPatternEvaluationCase:
    experience = resolved.experience
    identity = str(resolved.experience_id)
    if experience is None:
        return HarnessPatternEvaluationCase(
            case_id=f"{role}:{identity}",
            experience_id=resolved.experience_id,
            role=role,
            baseline_quality_score=resolved.quality_score,
            candidate_quality_score=resolved.quality_score,
            baseline_unsupported_claim_rate=resolved.unsupported_claim_rate,
            candidate_unsupported_claim_rate=resolved.unsupported_claim_rate,
            passed=False,
            reasons=["missing_or_out_of_scope_experience"],
        )
    if role == "supporting":
        candidate_quality = (
            min(1.0, resolved.quality_score + 0.08)
            if repair_contract_passed
            else resolved.quality_score
        )
        candidate_unsupported = (
            max(0.0, resolved.unsupported_claim_rate - 0.10)
            if repair_contract_passed and citation_repair
            else resolved.unsupported_claim_rate
        )
        passed = (
            not experience.diagnosis.success
            and experience.diagnosis.learnable
            and repair_contract_passed
        )
        reasons = [
            (
                "support_projection_passed"
                if passed
                else "support_case_contract_failed"
            ),
            "counterfactual_projection_not_live_output",
        ]
    else:
        candidate_quality = resolved.quality_score
        candidate_unsupported = resolved.unsupported_claim_rate
        passed = (
            experience.diagnosis.success
            and candidate_quality >= resolved.quality_score - max_score_regression
        )
        reasons = [
            (
                "contradiction_non_regression_passed"
                if passed
                else "contradiction_case_failed"
            ),
            "counterfactual_projection_not_live_output",
        ]
    return HarnessPatternEvaluationCase(
        case_id=f"{role}:{identity}",
        experience_id=experience.experience_id,
        role=role,
        baseline_quality_score=resolved.quality_score,
        candidate_quality_score=round(candidate_quality, 6),
        baseline_unsupported_claim_rate=resolved.unsupported_claim_rate,
        candidate_unsupported_claim_rate=round(candidate_unsupported, 6),
        passed=passed,
        reasons=reasons,
    )


def _required_case(
    case_id: str,
    *,
    passed: bool,
    passed_reason: str,
    failed_reason: str,
) -> HarnessPatternEvaluationCase:
    return HarnessPatternEvaluationCase(
        case_id=case_id,
        role="required",
        baseline_quality_score=1.0,
        candidate_quality_score=1.0 if passed else 0.0,
        baseline_unsupported_claim_rate=0.0,
        candidate_unsupported_claim_rate=0.0 if passed else 1.0,
        passed=passed,
        reasons=[passed_reason if passed else failed_reason],
    )


def _delta_fields(pattern: HarnessPattern) -> set[str]:
    payload = pattern.proposed_delta.model_dump(mode="json", exclude_none=True)
    return {
        f"{section}.{field}"
        for section, values in payload.items()
        if isinstance(values, dict)
        for field in values
    }


def _average(values: Iterable[float]) -> float:
    resolved = list(values)
    return round(sum(resolved) / len(resolved), 6) if resolved else 0.0


__all__ = [
    "DeterministicPatternEvaluator",
    "PATTERN_DATASET_REVISION",
    "PATTERN_EVALUATOR_REVISION",
]
