from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from app.domain.contracts import HarnessExperienceRepository, HarnessPolicyRepository
from app.domain.models import StrictModel
from app.harness.health import experience_matches_pattern
from app.harness.models import (
    HarnessExperienceEntry,
    HarnessPattern,
    HarnessPatternEvaluation,
    HarnessPatternStatus,
    HarnessPatternTransition,
)


class LearningPatternEvidence(StrictModel):
    pattern_id: str
    version: str
    effective_status: HarnessPatternStatus
    support_count: int = Field(ge=0)
    contradiction_count: int = Field(ge=0)
    evaluation_count: int = Field(ge=0)
    latest_quality_lift: float | None = Field(default=None, ge=-1.0, le=1.0)
    offline_ready: bool = False
    applied_promotion_count: int = Field(ge=0)
    applied_rollback_count: int = Field(ge=0)
    applied_experience_count: int = Field(ge=0)
    control_experience_count: int = Field(ge=0)
    observed_quality_lift: float | None = Field(default=None, ge=-1.0, le=1.0)
    observed_failure_rate_delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    causal_evidence: bool = False


class SelfLearningEffectReport(StrictModel):
    revision: str = "self-learning-effect-gate-v1"
    generated_at: datetime
    tenant_id: str
    project_id: str
    status: Literal["not_ready", "observing", "validated"]
    passed: bool
    experience_count: int = Field(ge=0)
    evaluated_experience_count: int = Field(ge=0)
    evaluation_coverage: float = Field(ge=0.0, le=1.0)
    learnable_experience_count: int = Field(ge=0)
    successful_experience_count: int = Field(ge=0)
    explicit_feedback_count: int = Field(ge=0)
    pattern_count: int = Field(ge=0)
    status_counts: dict[str, int]
    offline_ready_pattern_count: int = Field(ge=0)
    causally_validated_pattern_count: int = Field(ge=0)
    rollback_evidence_count: int = Field(ge=0)
    reasons: list[str]
    patterns: list[LearningPatternEvidence]


class SelfLearningEffectEvaluator:
    """Audit observed learning evidence without manufacturing promotion data."""

    def __init__(
        self,
        experiences: HarnessExperienceRepository,
        policies: HarnessPolicyRepository,
        *,
        minimum_experiences: int = 20,
        minimum_evaluation_coverage: float = 0.95,
        minimum_feedback: int = 5,
        minimum_quality_lift: float = 0.0,
    ) -> None:
        self._experiences = experiences
        self._policies = policies
        self._minimum_experiences = minimum_experiences
        self._minimum_evaluation_coverage = minimum_evaluation_coverage
        self._minimum_feedback = minimum_feedback
        self._minimum_quality_lift = minimum_quality_lift

    async def evaluate(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 500,
    ) -> SelfLearningEffectReport:
        experiences = list(
            await self._experiences.list_scoped(
                tenant_id=tenant_id,
                project_id=project_id,
                limit=limit,
            )
        )
        evaluated = 0
        explicit_feedback = 0
        for experience in experiences:
            values = await self._experiences.list_evaluations(
                experience.experience_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            evaluated += int(bool(values))
            explicit_feedback += int(
                any(item.signal_kind == "explicit_feedback" for item in values)
            )

        patterns = list(
            await self._policies.list_patterns(
                tenant_id=tenant_id,
                project_id=project_id,
                limit=limit,
            )
        )
        latest = _latest_patterns(patterns)
        pattern_evidence = [
            await self._pattern_evidence(
                item,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            for item in latest
        ]
        coverage = evaluated / len(experiences) if experiences else 0.0
        reasons: list[str] = []
        if len(experiences) < self._minimum_experiences:
            reasons.append(f"insufficient_experiences:{len(experiences)}/{self._minimum_experiences}")
        if coverage < self._minimum_evaluation_coverage:
            reasons.append(
                f"insufficient_evaluation_coverage:{coverage:.4f}/{self._minimum_evaluation_coverage:.4f}"
            )
        if explicit_feedback < self._minimum_feedback:
            reasons.append(f"insufficient_explicit_feedback:{explicit_feedback}/{self._minimum_feedback}")
        if not pattern_evidence:
            reasons.append("no_real_patterns_mined")
        offline_ready = sum(item.offline_ready for item in pattern_evidence)
        causal = sum(item.causal_evidence for item in pattern_evidence)
        if pattern_evidence and not offline_ready:
            reasons.append("no_pattern_passed_offline_evidence_gate")
        if pattern_evidence and not causal:
            reasons.append("no_pattern_has_observed_post_application_quality_lift")
        if not reasons and causal:
            status: Literal["not_ready", "observing", "validated"] = "validated"
        elif experiences and coverage >= self._minimum_evaluation_coverage:
            status = "observing"
        else:
            status = "not_ready"
        if not reasons:
            reasons.append("real_learning_effect_evidence_passed")
        return SelfLearningEffectReport(
            generated_at=datetime.now(UTC),
            tenant_id=tenant_id,
            project_id=project_id,
            status=status,
            passed=status == "validated",
            experience_count=len(experiences),
            evaluated_experience_count=evaluated,
            evaluation_coverage=round(coverage, 6),
            learnable_experience_count=sum(item.diagnosis.learnable for item in experiences),
            successful_experience_count=sum(item.diagnosis.success for item in experiences),
            explicit_feedback_count=explicit_feedback,
            pattern_count=len(pattern_evidence),
            status_counts=dict(Counter(item.effective_status.value for item in pattern_evidence)),
            offline_ready_pattern_count=offline_ready,
            causally_validated_pattern_count=causal,
            rollback_evidence_count=sum(item.applied_rollback_count for item in pattern_evidence),
            reasons=reasons,
            patterns=pattern_evidence,
        )

    async def _pattern_evidence(
        self,
        pattern: HarnessPattern,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningPatternEvidence:
        evaluations = list(
            await self._policies.list_pattern_evaluations(
                pattern.pattern_id,
                tenant_id=tenant_id,
                project_id=project_id,
                pattern_version=pattern.version,
            )
        )
        promotion = list(
            await self._policies.list_pattern_promotion_evidence(
                pattern.pattern_id,
                tenant_id=tenant_id,
                project_id=project_id,
                pattern_version=pattern.version,
            )
        )
        transitions = list(
            await self._policies.list_pattern_transitions(
                pattern.pattern_id,
                tenant_id=tenant_id,
                project_id=project_id,
                pattern_version=pattern.version,
            )
        )
        latest_evaluation = _latest_evaluation(evaluations)
        offline_ready = any(item.offline_ready for item in promotion)
        applied_promotions = [
            item for item in transitions if item.transition_type == "promotion" and item.applied
        ]
        applied_rollbacks = [
            item for item in transitions if item.transition_type == "rollback" and item.applied
        ]
        applied_experiences, control_experiences = await self._observed_groups(
            pattern,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        applied_quality, applied_failure = await self._outcomes(applied_experiences)
        control_quality, control_failure = await self._outcomes(control_experiences)
        observed_quality_lift = (
            applied_quality - control_quality
            if applied_experiences and control_experiences
            else None
        )
        observed_failure_delta = (
            applied_failure - control_failure
            if applied_experiences and control_experiences
            else None
        )
        online_promotions = [
            item
            for item in applied_promotions
            if item.to_status in {HarnessPatternStatus.CANARY, HarnessPatternStatus.ACTIVE}
        ]
        causal = bool(
            online_promotions
            and len(applied_experiences) >= 5
            and len(control_experiences) >= 5
            and observed_quality_lift is not None
            and observed_quality_lift > self._minimum_quality_lift
            and observed_failure_delta is not None
            and observed_failure_delta <= 0.10
            and not applied_rollbacks
        )
        return LearningPatternEvidence(
            pattern_id=str(pattern.pattern_id),
            version=pattern.version,
            effective_status=_effective_status(pattern, transitions),
            support_count=pattern.support_count,
            contradiction_count=len(pattern.contradicting_experience_ids),
            evaluation_count=len(evaluations),
            latest_quality_lift=(
                latest_evaluation.estimated_quality_lift
                if latest_evaluation is not None
                else None
            ),
            offline_ready=offline_ready,
            applied_promotion_count=len(applied_promotions),
            applied_rollback_count=len(applied_rollbacks),
            applied_experience_count=len(applied_experiences),
            control_experience_count=len(control_experiences),
            observed_quality_lift=(
                round(observed_quality_lift, 6)
                if observed_quality_lift is not None
                else None
            ),
            observed_failure_rate_delta=(
                round(observed_failure_delta, 6)
                if observed_failure_delta is not None
                else None
            ),
            causal_evidence=causal,
        )

    async def _observed_groups(
        self,
        pattern: HarnessPattern,
        *,
        tenant_id: str,
        project_id: str,
    ) -> tuple[list[HarnessExperienceEntry], list[HarnessExperienceEntry]]:
        key = f"{pattern.pattern_id}@{pattern.version}"
        values = await self._experiences.list_scoped(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=500,
        )
        matching = [
            item
            for item in values
            if item.created_at >= pattern.created_at
            and experience_matches_pattern(pattern, item)
        ]
        return (
            [item for item in matching if key in item.applied_pattern_versions],
            [item for item in matching if key not in item.applied_pattern_versions],
        )

    async def _outcomes(
        self,
        values: list[HarnessExperienceEntry],
    ) -> tuple[float, float]:
        if not values:
            return 0.0, 0.0
        quality: list[float] = []
        failures = 0
        for item in values:
            evaluations = list(
                await self._experiences.list_evaluations(
                    item.experience_id,
                    tenant_id=item.tenant_id,
                    project_id=item.project_id,
                )
            )
            latest = (
                max(
                    evaluations,
                    key=lambda value: (
                        value.created_at,
                        value.signal_kind == "explicit_feedback",
                    ),
                )
                if evaluations
                else None
            )
            quality.append(
                latest.quality_vector.quality_score
                if latest is not None
                else item.diagnosis.quality_vector.quality_score
            )
            passed = (
                latest.reward_vector.passed
                if latest is not None
                else item.diagnosis.success
            )
            failures += int(not passed)
        return sum(quality) / len(quality), failures / len(values)


def _latest_patterns(patterns: list[HarnessPattern]) -> list[HarnessPattern]:
    latest: dict[str, HarnessPattern] = {}
    for item in patterns:
        key = str(item.pattern_id)
        current = latest.get(key)
        if current is None or _semver(item.version) > _semver(current.version):
            latest[key] = item
    return sorted(latest.values(), key=lambda item: (item.name, item.version))


def _latest_evaluation(
    values: list[HarnessPatternEvaluation],
) -> HarnessPatternEvaluation | None:
    return max(values, key=lambda item: item.generated_at) if values else None


def _effective_status(
    pattern: HarnessPattern,
    transitions: list[HarnessPatternTransition],
) -> HarnessPatternStatus:
    applied = [item for item in transitions if item.applied]
    return max(applied, key=lambda item: item.decided_at).to_status if applied else pattern.status


def _semver(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)
