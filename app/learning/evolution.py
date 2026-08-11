from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.contracts import (
    LearningChangeSetRepository,
    SkillEvaluationRepository,
    SkillObservationRepository,
    SkillRepository,
    SkillTransitionRepository,
)
from app.domain.enums import SkillStatus
from app.domain.models import (
    LearningChangeSet,
    PromotionDecision,
    RunTrajectory,
    SkillDefinition,
    SkillEvaluation,
    SkillEvolutionResult,
    SkillEvolutionSnapshot,
    SkillHealthReport,
    SkillObservation,
    SkillPromotionEvidence,
    SkillTransitionEvent,
)
from app.learning.engine import LearningEngine
from app.learning.safety import assess_automatic_learning
from app.learning.skill_evaluator import DeterministicSkillEvaluator
from app.skills.skill_registry import SkillDiscoveryRegistry


class SkillEvolutionService:
    """Owns system-generated evaluation, staged promotion, and health rollback."""

    _AUTO_STAGE_STATUSES = {
        SkillStatus.DRAFT,
        SkillStatus.SECURITY_REVIEW,
        SkillStatus.OFFLINE_PASS,
    }

    def __init__(
        self,
        *,
        learning_engine: LearningEngine,
        skills: SkillRepository,
        evaluator: DeterministicSkillEvaluator,
        evaluations: SkillEvaluationRepository,
        observations: SkillObservationRepository,
        transitions: SkillTransitionRepository | None = None,
        change_sets: LearningChangeSetRepository | None = None,
        min_shadow_observations: int = 3,
        min_canary_observations: int = 5,
        min_quality_score: float = 0.65,
        max_score_regression: float = 0.02,
        max_failure_rate: float = 0.20,
        max_unsupported_claim_rate: float = 0.05,
        max_negative_feedback_rate: float = 0.0,
        severe_negative_feedback_threshold: float = -0.5,
    ) -> None:
        if min_shadow_observations < 1 or min_canary_observations < 1:
            raise ValueError("Skill health observation minimums must be positive")
        for name, value in (
            ("min_quality_score", min_quality_score),
            ("max_score_regression", max_score_regression),
            ("max_failure_rate", max_failure_rate),
            ("max_unsupported_claim_rate", max_unsupported_claim_rate),
            ("max_negative_feedback_rate", max_negative_feedback_rate),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if not -1.0 <= severe_negative_feedback_threshold <= 0.0:
            raise ValueError("severe_negative_feedback_threshold must be between -1 and 0")
        self._learning = learning_engine
        self._skills = skills
        self._evaluator = evaluator
        self._evaluations = evaluations
        self._observations = observations
        self._transitions = transitions
        self._change_sets = change_sets
        self._min_shadow_observations = min_shadow_observations
        self._min_canary_observations = min_canary_observations
        self._min_quality_score = min_quality_score
        self._max_score_regression = max_score_regression
        self._max_failure_rate = max_failure_rate
        self._max_unsupported_claim_rate = max_unsupported_claim_rate
        self._max_negative_feedback_rate = max_negative_feedback_rate
        self._severe_negative_feedback_threshold = severe_negative_feedback_threshold

    async def evaluate_and_stage(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> SkillEvolutionResult:
        skill = await self._require_skill(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )
        evaluation = await self._evaluations.save(await self._evaluator.evaluate(skill))
        await self._record_evaluation(skill, evaluation)
        transitions: list[PromotionDecision] = []
        current = skill
        if evaluation.security_passed and evaluation.regression_passed:
            while current.status in self._AUTO_STAGE_STATUSES:
                target = {
                    SkillStatus.DRAFT: SkillStatus.SECURITY_REVIEW,
                    SkillStatus.SECURITY_REVIEW: SkillStatus.OFFLINE_PASS,
                    SkillStatus.OFFLINE_PASS: SkillStatus.SHADOW,
                }[current.status]
                decision = await self._learning.transition_skill(
                    current.skill_id,
                    target,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    skill_version=current.version,
                    evaluation=evaluation,
                )
                transitions.append(decision)
                if not decision.allowed:
                    break
                current = await self._require_skill(
                    current.skill_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    skill_version=current.version,
                )
        return SkillEvolutionResult(
            skill=current,
            evaluation=evaluation,
            transitions=transitions,
        )

    async def transition_skill(
        self,
        skill_id: UUID,
        target_status: SkillStatus,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
        human_approved: bool = False,
    ) -> PromotionDecision:
        skill = await self._require_skill(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )
        evaluation = await self._evaluations.latest(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill.version,
        )
        if target_status in {SkillStatus.CANARY, SkillStatus.ACTIVE}:
            cohort = "shadow" if target_status == SkillStatus.CANARY else "canary"
            health = await self.health(skill, cohort=cohort)
            promotion_evidence = health.promotion_evidence
            if not health.promotion_ready:
                decision = PromotionDecision(
                    transition_id=uuid5(
                        NAMESPACE_URL,
                        (
                            "hermesgraph:health-gate:"
                            f"{promotion_evidence.evidence_id}:{target_status.value}"
                        ),
                    ),
                    promotion_evidence_id=promotion_evidence.evidence_id,
                    skill_id=skill.skill_id,
                    from_status=skill.status,
                    to_status=target_status,
                    allowed=False,
                    reasons=["health_gate_not_ready", *health.reasons],
                )
                event = await self._learning.record_transition_decision(
                    skill,
                    decision,
                    transition_type="health_gate",
                    applied=False,
                    evaluation=evaluation,
                    promotion_evidence=promotion_evidence,
                    human_approved=human_approved,
                )
                return decision.model_copy(
                    update={"transition_id": (event.transition_id if event is not None else None)}
                )
        else:
            promotion_evidence = None
        return await self._learning.transition_skill(
            skill_id,
            target_status,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill.version,
            evaluation=evaluation,
            promotion_evidence=promotion_evidence,
            human_approved=human_approved,
        )

    async def observe_run(self, trajectory: RunTrajectory) -> Sequence[SkillObservation]:
        if not assess_automatic_learning(trajectory).allowed:
            # Observations feed health gates and can mine/promote behavior.  They
            # are automatic learning assets, so unsafe final evidence must not
            # enter this path even when a caller bypasses LearningWorkflowProcessor.
            return ()
        scoped = [
            skill
            for status in (SkillStatus.SHADOW, SkillStatus.CANARY, SkillStatus.ACTIVE)
            for skill in await self._skills.list_by_status(
                status.value,
                tenant_id=trajectory.context.tenant_id,
                project_id=trajectory.context.project_id,
            )
        ]
        observations: list[SkillObservation] = []
        for skill in scoped:
            if skill.status == SkillStatus.SHADOW:
                matches = SkillDiscoveryRegistry([skill]).discover(
                    trajectory.user_input,
                    statuses={SkillStatus.SHADOW},
                    limit=1,
                )
                if not matches:
                    continue
                exposed = False
                activated = False
                simulated = True
            else:
                exposed = trajectory.context.skill_versions.get(skill.name) == skill.version
                if not exposed:
                    continue
                activated = any(
                    event.tool_name in {"activate_governed_skill", "activate_skill"}
                    and f"{skill.name}@{skill.version}" in event.output_summary
                    for event in trajectory.tool_events
                )
                simulated = False

            case = await self._evaluator.evaluate_case(skill, trajectory)
            reasons = list(case.reasons)
            if exposed and not activated:
                reasons.append("skill_exposed_but_not_activated")
            signal_kind = (
                "explicit_feedback"
                if trajectory.feedback_score is not None
                or bool((trajectory.feedback_text or "").strip())
                else "run_outcome"
            )
            negative_feedback = (
                trajectory.feedback_score is not None and trajectory.feedback_score < 0.0
            )
            if negative_feedback:
                reasons.append("explicit_negative_feedback")
            input_fingerprint = _observation_input_fingerprint(
                trajectory=trajectory,
                case_payload=case.model_dump(mode="json"),
                exposed=exposed,
                activated=activated,
                simulated=simulated,
            )
            observation = SkillObservation(
                observation_id=uuid5(
                    NAMESPACE_URL,
                    (
                        "hermesgraph:skill-observation:"
                        f"{skill.skill_id}:{skill.version}:"
                        f"{trajectory.context.run_id}:{skill.status.value}:"
                        f"{self._evaluator.revision}:{input_fingerprint}"
                    ),
                ),
                skill_id=skill.skill_id,
                tenant_id=skill.tenant_id,
                project_id=skill.project_id,
                skill_version=skill.version,
                evaluator_revision=self._evaluator.revision,
                run_id=trajectory.context.run_id,
                cohort=skill.status.value,
                exposed=exposed,
                activated=activated,
                simulated=simulated,
                baseline_score=case.baseline_score,
                candidate_score=case.candidate_score,
                unsupported_claim_rate=case.unsupported_claim_rate,
                tool_success_rate=case.tool_success_rate,
                passed=case.passed,
                signal_kind=signal_kind,
                feedback_score=trajectory.feedback_score,
                negative_feedback=negative_feedback,
                reasons=sorted(set(reasons)),
                created_at=(
                    datetime.now(UTC)
                    if signal_kind == "explicit_feedback"
                    else trajectory.completed_at or trajectory.context.started_at
                ),
            )
            observations.append(await self._observations.save(observation))
            if skill.status in {SkillStatus.CANARY, SkillStatus.ACTIVE} and activated:
                await self._govern_live_skill(skill)
        return observations

    async def health(
        self,
        skill: SkillDefinition,
        *,
        cohort: str | None = None,
    ) -> SkillHealthReport:
        resolved_cohort = cohort or (
            skill.status.value
            if skill.status in {SkillStatus.SHADOW, SkillStatus.CANARY, SkillStatus.ACTIVE}
            else "shadow"
        )
        if resolved_cohort not in {"shadow", "canary", "active"}:
            raise ValueError("Skill health cohort must be shadow, canary, or active")
        observations = list(
            await self._observations.list_for_skill(
                skill.skill_id,
                tenant_id=skill.tenant_id,
                project_id=skill.project_id,
                skill_version=skill.version,
                cohort=resolved_cohort,
            )
        )
        eligible = (
            observations
            if resolved_cohort == "shadow"
            else [item for item in observations if item.activated]
        )
        evaluated = _effective_observation_per_run(eligible)
        baseline = _average(item.baseline_score for item in evaluated)
        candidate = _average(item.candidate_score for item in evaluated)
        unsupported = _average(item.unsupported_claim_rate for item in evaluated)
        failure_rate = (
            sum(not item.passed for item in evaluated) / len(evaluated) if evaluated else 0.0
        )
        required = (
            self._min_shadow_observations
            if resolved_cohort == "shadow"
            else self._min_canary_observations
        )
        negative_feedback_count = sum(item.negative_feedback for item in evaluated)
        negative_feedback_rate = negative_feedback_count / len(evaluated) if evaluated else 0.0
        severe_negative_feedback_count = sum(
            item.feedback_score is not None
            and item.feedback_score <= self._severe_negative_feedback_threshold
            for item in evaluated
        )
        metric_reasons: list[str] = []
        if evaluated and candidate < self._min_quality_score:
            metric_reasons.append("candidate_quality_below_minimum")
        if evaluated and candidate < baseline - self._max_score_regression:
            metric_reasons.append("candidate_score_regressed")
        if unsupported > self._max_unsupported_claim_rate:
            metric_reasons.append("unsupported_claim_rate_too_high")
        if failure_rate > self._max_failure_rate:
            metric_reasons.append("failure_rate_too_high")
        if negative_feedback_rate > self._max_negative_feedback_rate:
            metric_reasons.append("negative_feedback_rate_too_high")
        if severe_negative_feedback_count:
            metric_reasons.append("severe_negative_feedback_observed")
        reasons = list(metric_reasons)
        if len(evaluated) < required:
            reasons.append(f"insufficient_observations:{len(evaluated)}/{required}")
        healthy = bool(evaluated) and not metric_reasons
        promotion_ready = healthy and len(evaluated) >= required
        recommended_action = _recommended_action(
            cohort=resolved_cohort,
            promotion_ready=promotion_ready,
            unhealthy=bool(metric_reasons),
            enough_observations=len(evaluated) >= required,
            negative_feedback_count=negative_feedback_count,
            severe_negative_feedback_count=severe_negative_feedback_count,
        )
        generated_at = max(item.created_at for item in evaluated) if evaluated else skill.created_at
        window_started_at = min(item.created_at for item in evaluated) if evaluated else None
        window_ended_at = max(item.created_at for item in evaluated) if evaluated else None
        evidence_payload = {
            "skill_id": str(skill.skill_id),
            "tenant_id": skill.tenant_id,
            "project_id": skill.project_id,
            "skill_version": skill.version,
            "cohort": resolved_cohort,
            "source_observation_ids": [str(item.observation_id) for item in observations],
            "observation_ids": [str(item.observation_id) for item in evaluated],
            "required_observations": required,
            "min_quality_score": self._min_quality_score,
            "max_score_regression": self._max_score_regression,
            "max_failure_rate": self._max_failure_rate,
            "max_unsupported_claim_rate": self._max_unsupported_claim_rate,
            "max_negative_feedback_rate": self._max_negative_feedback_rate,
            "severe_negative_feedback_threshold": (self._severe_negative_feedback_threshold),
            "reasons": reasons,
            "recommended_action": recommended_action,
        }
        promotion_evidence = SkillPromotionEvidence(
            evidence_id=uuid5(
                NAMESPACE_URL,
                "hermesgraph:skill-promotion-evidence:"
                + hashlib.sha256(
                    json.dumps(
                        evidence_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            ),
            skill_id=skill.skill_id,
            tenant_id=skill.tenant_id,
            project_id=skill.project_id,
            skill_version=skill.version,
            cohort=resolved_cohort,
            source_observation_ids=[item.observation_id for item in observations],
            observation_ids=[item.observation_id for item in evaluated],
            run_ids=[item.run_id for item in evaluated],
            evaluator_revisions=sorted({item.evaluator_revision for item in evaluated}),
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
            required_observations=required,
            total_observations=len(observations),
            evaluated_observations=len(evaluated),
            exposed_observations=sum(item.exposed for item in observations),
            activated_observations=sum(item.activated for item in observations),
            average_baseline_score=baseline,
            average_candidate_score=candidate,
            average_unsupported_claim_rate=unsupported,
            failure_rate=round(failure_rate, 6),
            negative_feedback_count=negative_feedback_count,
            negative_feedback_rate=round(negative_feedback_rate, 6),
            severe_negative_feedback_count=severe_negative_feedback_count,
            min_quality_score=self._min_quality_score,
            max_score_regression=self._max_score_regression,
            max_failure_rate=self._max_failure_rate,
            max_unsupported_claim_rate=self._max_unsupported_claim_rate,
            max_negative_feedback_rate=self._max_negative_feedback_rate,
            severe_negative_feedback_threshold=(self._severe_negative_feedback_threshold),
            healthy=healthy,
            promotion_ready=promotion_ready,
            recommended_action=recommended_action,
            reasons=reasons,
            generated_at=generated_at,
        )
        return SkillHealthReport(
            skill_id=skill.skill_id,
            skill_version=skill.version,
            cohort=resolved_cohort,
            required_observations=required,
            total_observations=len(observations),
            evaluated_observations=len(evaluated),
            exposed_observations=sum(item.exposed for item in observations),
            activated_observations=sum(item.activated for item in observations),
            average_baseline_score=baseline,
            average_candidate_score=candidate,
            average_unsupported_claim_rate=unsupported,
            failure_rate=round(failure_rate, 6),
            healthy=healthy,
            promotion_ready=promotion_ready,
            reasons=reasons,
            promotion_evidence=promotion_evidence,
            generated_at=generated_at,
        )

    async def snapshots(
        self,
        skills: Sequence[SkillDefinition],
    ) -> Sequence[SkillEvolutionSnapshot]:
        snapshots: list[SkillEvolutionSnapshot] = []
        for skill in skills:
            latest = await self._evaluations.latest(
                skill.skill_id,
                tenant_id=skill.tenant_id,
                project_id=skill.project_id,
                skill_version=skill.version,
            )
            health = (
                await self.health(skill)
                if skill.status in {SkillStatus.SHADOW, SkillStatus.CANARY, SkillStatus.ACTIVE}
                else None
            )
            snapshots.append(
                SkillEvolutionSnapshot(
                    skill=skill,
                    latest_evaluation=latest,
                    health=health,
                )
            )
        return snapshots

    async def list_evaluations(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillEvaluation]:
        await self._require_skill(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )
        return await self._evaluations.list_for_skill(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )

    async def list_transitions(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillTransitionEvent]:
        await self._require_skill(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )
        if self._transitions is None:
            return []
        return await self._transitions.list_for_skill(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )

    async def _govern_live_skill(self, skill: SkillDefinition) -> None:
        health = await self.health(skill, cohort=skill.status.value)
        evidence = health.promotion_evidence
        if evidence.recommended_action in {"hold", "promote"}:
            return
        if evidence.recommended_action == "rollback_recommended":
            decision = PromotionDecision(
                transition_id=uuid5(
                    NAMESPACE_URL,
                    (f"hermesgraph:rollback-recommendation:{evidence.evidence_id}"),
                ),
                promotion_evidence_id=evidence.evidence_id,
                skill_id=skill.skill_id,
                from_status=skill.status,
                to_status=SkillStatus.ROLLED_BACK,
                allowed=False,
                reasons=["rollback_recommended", *health.reasons],
                decided_at=evidence.generated_at,
            )
            await self._learning.record_transition_decision(
                skill,
                decision,
                transition_type="health_gate",
                applied=False,
                promotion_evidence=evidence,
            )
            await self._record_governance_change(
                skill,
                evidence,
                operation="rollback_recommendation",
                applied=False,
            )
            return

        reason = f"automatic_health_gate:{evidence.evidence_id}:" + ",".join(health.reasons)
        decision = await self._learning.rollback_skill(
            skill.skill_id,
            tenant_id=skill.tenant_id,
            project_id=skill.project_id,
            skill_version=skill.version,
            reason=reason,
            promotion_evidence=evidence,
        )
        if decision.allowed:
            await self._record_governance_change(
                skill,
                evidence,
                operation="automatic_rollback",
                applied=True,
                reason=reason,
            )

    async def _record_governance_change(
        self,
        skill: SkillDefinition,
        evidence: SkillPromotionEvidence,
        *,
        operation: str,
        applied: bool,
        reason: str | None = None,
    ) -> None:
        if self._change_sets is None or not evidence.run_ids:
            return
        await self._change_sets.save(
            LearningChangeSet(
                change_set_id=uuid5(
                    NAMESPACE_URL,
                    f"hermesgraph:{operation}:{evidence.evidence_id}",
                ),
                target_type="skill_definition",
                target_id=str(skill.skill_id),
                parent_version=skill.version,
                structured_diff={
                    "operation": operation,
                    "from_status": skill.status.value,
                    "to_status": (SkillStatus.ROLLED_BACK.value if applied else skill.status.value),
                    "applied": applied,
                    "promotion_evidence_id": str(evidence.evidence_id),
                    "reason": reason or ",".join(evidence.reasons),
                },
                source_run_ids=evidence.run_ids,
                expected_benefits=[
                    (
                        "Stop a regressing skill from receiving more traffic"
                        if applied
                        else "Surface deterministic rollback evidence for review"
                    )
                ],
                risks=[
                    (
                        "Automatic rollback may react to a severe single feedback signal"
                        if applied
                        else "A recommendation does not remove live traffic by itself"
                    )
                ],
                scope={
                    "tenant_id": skill.tenant_id,
                    "project_id": skill.project_id,
                    "skill_version": skill.version,
                },
                evaluation_report=evidence.model_dump(mode="json"),
                rollback_conditions=[],
                created_at=evidence.generated_at,
            )
        )

    async def _record_evaluation(
        self,
        skill: SkillDefinition,
        evaluation: SkillEvaluation,
    ) -> None:
        if self._change_sets is None:
            return
        await self._change_sets.save(
            LearningChangeSet(
                change_set_id=uuid5(
                    NAMESPACE_URL,
                    f"hermesgraph:skill-evaluation:{evaluation.evaluation_id}",
                ),
                target_type="skill_evaluation",
                target_id=str(evaluation.evaluation_id),
                parent_version=skill.version,
                structured_diff={
                    "operation": "offline_replay_evaluation",
                    "skill_id": str(skill.skill_id),
                    "skill_version": skill.version,
                    "security_passed": evaluation.security_passed,
                    "regression_passed": evaluation.regression_passed,
                },
                source_run_ids=skill.source_run_ids,
                expected_benefits=["Gate skill promotion with reproducible source-run replay"],
                risks=["Historical source runs may not represent open-world traffic"],
                scope={
                    "tenant_id": skill.tenant_id,
                    "project_id": skill.project_id,
                },
                evaluation_report=evaluation.model_dump(mode="json"),
                rollback_conditions=["A newer evaluation invalidates this report"],
            )
        )

    async def _require_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        skill_version: str | None = None,
    ) -> SkillDefinition:
        skill = await self._skills.get(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            version=skill_version,
        )
        if skill is None:
            raise KeyError(f"Skill not found: {skill_id}")
        return skill


def _observation_input_fingerprint(
    *,
    trajectory: RunTrajectory,
    case_payload: dict[str, object],
    exposed: bool,
    activated: bool,
    simulated: bool,
) -> str:
    feedback_text = (trajectory.feedback_text or "").strip()
    payload = {
        "run_id": str(trajectory.context.run_id),
        "status": trajectory.status.value,
        "case": case_payload,
        "feedback_score": trajectory.feedback_score,
        "feedback_text_hash": (
            hashlib.sha256(feedback_text.encode("utf-8")).hexdigest() if feedback_text else None
        ),
        "exposed": exposed,
        "activated": activated,
        "simulated": simulated,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _effective_observation_per_run(
    observations: Sequence[SkillObservation],
) -> list[SkillObservation]:
    selected: dict[UUID, SkillObservation] = {}
    for observation in observations:
        current = selected.get(observation.run_id)
        if current is None or _observation_supersedes(observation, current):
            selected[observation.run_id] = observation
    return sorted(
        selected.values(),
        key=lambda item: (item.created_at, str(item.observation_id)),
    )


def _observation_supersedes(
    candidate: SkillObservation,
    current: SkillObservation,
) -> bool:
    if candidate.signal_kind != current.signal_kind:
        return candidate.signal_kind == "explicit_feedback"
    if candidate.signal_kind == "explicit_feedback" and candidate.created_at != current.created_at:
        return candidate.created_at > current.created_at
    return _observation_risk_key(candidate) < _observation_risk_key(current)


def _observation_risk_key(
    observation: SkillObservation,
) -> tuple[bool, bool, float, float, float, float, str]:
    return (
        observation.passed,
        not observation.negative_feedback,
        observation.feedback_score if observation.feedback_score is not None else 1.0,
        observation.candidate_score,
        observation.tool_success_rate,
        -observation.unsupported_claim_rate,
        str(observation.observation_id),
    )


def _recommended_action(
    *,
    cohort: str,
    promotion_ready: bool,
    unhealthy: bool,
    enough_observations: bool,
    negative_feedback_count: int,
    severe_negative_feedback_count: int,
) -> Literal["promote", "hold", "rollback_recommended", "rollback"]:
    live = cohort in {"canary", "active"}
    if live and severe_negative_feedback_count:
        return "rollback"
    if live and unhealthy and enough_observations:
        return "rollback"
    if live and negative_feedback_count:
        return "rollback_recommended"
    if cohort in {"shadow", "canary"} and promotion_ready:
        return "promote"
    return "hold"


def _average(values: Iterable[float]) -> float:
    resolved = list(values)
    return round(sum(resolved) / len(resolved), 6) if resolved else 0.0


__all__ = ["SkillEvolutionService"]
