from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.contracts import HarnessExperienceRepository, HarnessPolicyRepository
from app.harness.evolution import HarnessPatternEvolutionService
from app.harness.models import HarnessExperienceEntry, HarnessPattern, HarnessPatternStatus


@dataclass(frozen=True, slots=True)
class HarnessPatternHealth:
    pattern: HarnessPattern
    effective_status: HarnessPatternStatus
    applied_count: int
    control_count: int
    applied_quality: float
    control_quality: float
    quality_lift: float
    applied_failure_rate: float
    control_failure_rate: float
    failure_rate_delta: float
    applied_negative_feedback_rate: float
    control_negative_feedback_rate: float
    negative_feedback_rate_delta: float
    severe_negative_feedback: bool
    sufficient: bool
    healthy: bool
    reasons: tuple[str, ...]


class HarnessPatternHealthMonitor:
    """Evaluate observed treatment/control outcomes and fail closed on regressions."""

    def __init__(
        self,
        experiences: HarnessExperienceRepository,
        policies: HarnessPolicyRepository,
        evolution: HarnessPatternEvolutionService,
        *,
        min_applied_cases: int = 5,
        min_control_cases: int = 5,
        max_quality_regression: float = 0.05,
        max_failure_rate_regression: float = 0.10,
        max_negative_feedback_rate_regression: float = 0.10,
        severe_negative_feedback_threshold: float = -0.8,
    ) -> None:
        self._experiences = experiences
        self._policies = policies
        self._evolution = evolution
        self._min_applied = min_applied_cases
        self._min_control = min_control_cases
        self._max_quality_regression = max_quality_regression
        self._max_failure_regression = max_failure_rate_regression
        self._max_feedback_regression = max_negative_feedback_rate_regression
        self._severe_feedback = severe_negative_feedback_threshold

    async def evaluate(self, pattern: HarnessPattern) -> HarnessPatternHealth:
        status = await self._evolution.effective_status(pattern)
        values = await self._experiences.list_scoped(
            tenant_id=pattern.tenant_id,
            project_id=pattern.project_id,
            limit=500,
            learnable=True,
        )
        matching = [
            item
            for item in values
            if item.created_at >= pattern.created_at
            and experience_matches_pattern(pattern, item)
        ]
        key = f"{pattern.pattern_id}@{pattern.version}"
        applied = [item for item in matching if key in item.applied_pattern_versions]
        control = [item for item in matching if key not in item.applied_pattern_versions]
        applied_metrics = await self._metrics(applied)
        control_metrics = await self._metrics(control)
        sufficient = len(applied) >= self._min_applied and len(control) >= self._min_control
        quality_lift = applied_metrics[0] - control_metrics[0]
        failure_delta = applied_metrics[1] - control_metrics[1]
        feedback_delta = applied_metrics[2] - control_metrics[2]
        reasons: list[str] = []
        if not sufficient:
            reasons.append(
                f"insufficient_health_sample:applied={len(applied)}/{self._min_applied},"
                f"control={len(control)}/{self._min_control}"
            )
        if sufficient and quality_lift < -self._max_quality_regression:
            reasons.append(f"quality_regression:{quality_lift:.6f}")
        if sufficient and failure_delta > self._max_failure_regression:
            reasons.append(f"failure_rate_regression:{failure_delta:.6f}")
        if sufficient and feedback_delta > self._max_feedback_regression:
            reasons.append(f"negative_feedback_regression:{feedback_delta:.6f}")
        if applied_metrics[3]:
            reasons.append("severe_negative_feedback_observed")
        healthy = sufficient and not reasons
        if healthy:
            reasons.append("observed_health_gate_passed")
        return HarnessPatternHealth(
            pattern=pattern,
            effective_status=status,
            applied_count=len(applied),
            control_count=len(control),
            applied_quality=round(applied_metrics[0], 6),
            control_quality=round(control_metrics[0], 6),
            quality_lift=round(quality_lift, 6),
            applied_failure_rate=round(applied_metrics[1], 6),
            control_failure_rate=round(control_metrics[1], 6),
            failure_rate_delta=round(failure_delta, 6),
            applied_negative_feedback_rate=round(applied_metrics[2], 6),
            control_negative_feedback_rate=round(control_metrics[2], 6),
            negative_feedback_rate_delta=round(feedback_delta, 6),
            severe_negative_feedback=applied_metrics[3],
            sufficient=sufficient,
            healthy=healthy,
            reasons=tuple(reasons),
        )

    async def monitor_scope(self, *, tenant_id: str, project_id: str) -> list[HarnessPatternHealth]:
        patterns = await self._policies.list_patterns(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=500,
        )
        latest = _latest(patterns)
        reports: list[HarnessPatternHealth] = []
        for pattern in latest:
            status = await self._evolution.effective_status(pattern)
            if status not in {HarnessPatternStatus.CANARY, HarnessPatternStatus.ACTIVE}:
                continue
            report = await self.evaluate(pattern)
            reports.append(report)
            should_rollback = report.severe_negative_feedback or (
                report.sufficient and not report.healthy
            )
            if should_rollback:
                await self._evolution.record_health_gate(
                    pattern.pattern_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    pattern_version=pattern.version,
                    reasons=list(report.reasons),
                )
                await self._evolution.rollback(
                    pattern.pattern_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    pattern_version=pattern.version,
                    reason=";".join(report.reasons),
                )
        return reports

    async def _metrics(
        self,
        values: list[HarnessExperienceEntry],
    ) -> tuple[float, float, float, bool]:
        if not values:
            return 0.0, 0.0, 0.0, False
        quality: list[float] = []
        failed = 0
        negative = 0
        explicit = 0
        severe = False
        for item in values:
            evaluations = await self._experiences.list_evaluations(
                item.experience_id,
                tenant_id=item.tenant_id,
                project_id=item.project_id,
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
            failed += int(not passed)
            feedback = [
                value.reward_vector.feedback_score
                for value in evaluations
                if value.signal_kind == "explicit_feedback"
                and value.reward_vector.feedback_score is not None
            ]
            if feedback:
                explicit += 1
                score = feedback[-1]
                negative += int(score < 0)
                severe = severe or score <= self._severe_feedback
        return (
            sum(quality) / len(quality),
            failed / len(values),
            negative / explicit if explicit else 0.0,
            severe,
        )


def experience_matches_pattern(
    pattern: HarnessPattern,
    experience: HarnessExperienceEntry,
) -> bool:
    features = experience.case_features
    predicate = pattern.trigger_predicate
    primary = next((item for item in features.intents if item != "social"), "lookup")
    if features.domain_pack != predicate.domain_pack or primary != predicate.primary_intent:
        return False
    for field in ("personal_knowledge", "visual", "graph_relations"):
        expected = getattr(predicate, field)
        if expected is not None and getattr(features, field) != expected:
            return False
    return predicate.language is None or predicate.language == features.language


def _latest(patterns: Sequence[HarnessPattern]) -> list[HarnessPattern]:
    values: dict[str, HarnessPattern] = {}
    for pattern in patterns:
        key = str(pattern.pattern_id)
        current = values.get(key)
        if current is None or tuple(map(int, pattern.version.split("."))) > tuple(
            map(int, current.version.split("."))
        ):
            values[key] = pattern
    return list(values.values())
