from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.contracts import HarnessPolicyRepository
from app.harness.evaluation import DeterministicPatternEvaluator
from app.harness.models import (
    HarnessPattern,
    HarnessPatternEvaluation,
    HarnessPatternEvolutionResult,
    HarnessPatternPromotionEvidence,
    HarnessPatternStatus,
    HarnessPatternTransition,
    canonical_hash,
)


class HarnessPatternEvolutionService:
    """Own Pattern evaluation and append-only effective status transitions."""

    _NEXT_STATUS = {
        HarnessPatternStatus.DRAFT: HarnessPatternStatus.OFFLINE_PASS,
        HarnessPatternStatus.OFFLINE_PASS: HarnessPatternStatus.SHADOW,
        HarnessPatternStatus.SHADOW: HarnessPatternStatus.CANARY,
        HarnessPatternStatus.CANARY: HarnessPatternStatus.ACTIVE,
        HarnessPatternStatus.ACTIVE: HarnessPatternStatus.DEPRECATED,
    }
    _EVALUATED_TARGETS = {
        HarnessPatternStatus.OFFLINE_PASS,
        HarnessPatternStatus.SHADOW,
        HarnessPatternStatus.CANARY,
        HarnessPatternStatus.ACTIVE,
    }
    _HUMAN_TARGETS = {
        HarnessPatternStatus.CANARY,
        HarnessPatternStatus.ACTIVE,
    }
    _ROLLBACK_SOURCES = {
        HarnessPatternStatus.OFFLINE_PASS,
        HarnessPatternStatus.SHADOW,
        HarnessPatternStatus.CANARY,
        HarnessPatternStatus.ACTIVE,
    }

    def __init__(
        self,
        policies: HarnessPolicyRepository,
        evaluator: DeterministicPatternEvaluator,
    ) -> None:
        self._policies = policies
        self._evaluator = evaluator

    async def evaluate_and_stage(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        pattern_version: str | None = None,
    ) -> HarnessPatternEvolutionResult:
        pattern = await self._require_pattern(
            pattern_id,
            tenant_id=tenant_id,
            project_id=project_id,
            pattern_version=pattern_version,
        )
        evaluation = await self._policies.save_pattern_evaluation(
            await self._evaluator.evaluate(pattern)
        )
        promotion_evidence = await self._policies.save_pattern_promotion_evidence(
            _promotion_evidence(pattern, evaluation, self._evaluator)
        )
        transitions: list[HarnessPatternTransition] = []
        current = await self.effective_status(pattern)
        if evaluation.regression_passed:
            while current in {
                HarnessPatternStatus.DRAFT,
                HarnessPatternStatus.OFFLINE_PASS,
            }:
                target = self._NEXT_STATUS[current]
                transition = await self.transition(
                    pattern.pattern_id,
                    target,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    pattern_version=pattern.version,
                    evaluation=evaluation,
                    promotion_evidence=promotion_evidence,
                )
                transitions.append(transition)
                if not transition.applied:
                    break
                current = transition.to_status
        return HarnessPatternEvolutionResult(
            pattern=pattern,
            effective_status=current,
            evaluation=evaluation,
            promotion_evidence=promotion_evidence,
            transitions=transitions,
        )

    async def transition(
        self,
        pattern_id: UUID,
        target_status: HarnessPatternStatus,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        pattern_version: str | None = None,
        evaluation: HarnessPatternEvaluation | None = None,
        promotion_evidence: HarnessPatternPromotionEvidence | None = None,
        human_approved: bool = False,
        actor: Literal["system", "human", "health_monitor"] = "system",
        expected_from_status: HarnessPatternStatus | None = None,
    ) -> HarnessPatternTransition:
        pattern = await self._require_pattern(
            pattern_id,
            tenant_id=tenant_id,
            project_id=project_id,
            pattern_version=pattern_version,
        )
        current = await self.effective_status(pattern)
        resolved_evaluation = evaluation or await self.latest_evaluation(pattern)
        resolved_evidence = (
            promotion_evidence or await self.latest_promotion_evidence(pattern)
        )
        reasons: list[str] = []
        if expected_from_status is not None and expected_from_status != current:
            reasons.append("expected_from_status_mismatch")
        expected = self._NEXT_STATUS.get(current)
        if expected != target_status:
            reasons.append("status_transition_must_be_sequential")
        if target_status == HarnessPatternStatus.ACTIVE and current != HarnessPatternStatus.CANARY:
            reasons.append("direct_active_promotion_forbidden")
        if target_status in self._EVALUATED_TARGETS:
            reasons.extend(_evaluation_failures(pattern, resolved_evaluation))
            reasons.extend(
                _promotion_evidence_failures(
                    pattern,
                    resolved_evaluation,
                    resolved_evidence,
                )
            )
        if (
            target_status in {HarnessPatternStatus.CANARY, HarnessPatternStatus.ACTIVE}
            and resolved_evaluation is not None
            and not resolved_evaluation.consumer_compatible
        ):
            reasons.append("bounded_consumer_incompatible")
        if target_status in self._HUMAN_TARGETS and not human_approved:
            reasons.append("human_approval_required")
        if not reasons:
            reasons.append("promotion_gates_passed")
        allowed = reasons == ["promotion_gates_passed"]
        return await self._save_transition(
            pattern,
            current=current,
            target=target_status,
            transition_type="promotion",
            reasons=reasons,
            allowed=allowed,
            evaluation=resolved_evaluation,
            promotion_evidence=resolved_evidence,
            human_approved=human_approved,
            actor="human" if human_approved else actor,
        )

    async def rollback(
        self,
        pattern_id: UUID,
        *,
        reason: str,
        tenant_id: str = "local",
        project_id: str = "default",
        pattern_version: str | None = None,
        actor: Literal["system", "human", "health_monitor"] = "health_monitor",
        expected_from_status: HarnessPatternStatus | None = None,
    ) -> HarnessPatternTransition:
        pattern = await self._require_pattern(
            pattern_id,
            tenant_id=tenant_id,
            project_id=project_id,
            pattern_version=pattern_version,
        )
        current = await self.effective_status(pattern)
        reasons: list[str] = []
        if expected_from_status is not None and expected_from_status != current:
            reasons.append("expected_from_status_mismatch")
        if current not in self._ROLLBACK_SOURCES:
            reasons.append("status_not_rollback_eligible")
        if not reason.strip():
            reasons.append("rollback_reason_required")
        if not reasons:
            reasons.append(f"rollback:{reason.strip()}")
        allowed = len(reasons) == 1 and reasons[0].startswith("rollback:")
        return await self._save_transition(
            pattern,
            current=current,
            target=HarnessPatternStatus.ROLLED_BACK,
            transition_type="rollback",
            reasons=reasons,
            allowed=allowed,
            evaluation=await self.latest_evaluation(pattern),
            promotion_evidence=await self.latest_promotion_evidence(pattern),
            human_approved=False,
            actor=actor,
        )

    async def effective_status(self, pattern: HarnessPattern) -> HarnessPatternStatus:
        transitions = await self._policies.list_pattern_transitions(
            pattern.pattern_id,
            tenant_id=pattern.tenant_id,
            project_id=pattern.project_id,
            pattern_version=pattern.version,
        )
        applied = [item for item in transitions if item.applied]
        return applied[-1].to_status if applied else pattern.status

    async def latest_evaluation(
        self,
        pattern: HarnessPattern,
    ) -> HarnessPatternEvaluation | None:
        evaluations = await self._policies.list_pattern_evaluations(
            pattern.pattern_id,
            tenant_id=pattern.tenant_id,
            project_id=pattern.project_id,
            pattern_version=pattern.version,
        )
        return evaluations[-1] if evaluations else None

    async def latest_promotion_evidence(
        self,
        pattern: HarnessPattern,
    ) -> HarnessPatternPromotionEvidence | None:
        evidence = await self._policies.list_pattern_promotion_evidence(
            pattern.pattern_id,
            tenant_id=pattern.tenant_id,
            project_id=pattern.project_id,
            pattern_version=pattern.version,
        )
        return evidence[-1] if evidence else None

    async def _save_transition(
        self,
        pattern: HarnessPattern,
        *,
        current: HarnessPatternStatus,
        target: HarnessPatternStatus,
        transition_type: Literal["promotion", "rollback", "health_gate"],
        reasons: list[str],
        allowed: bool,
        evaluation: HarnessPatternEvaluation | None,
        promotion_evidence: HarnessPatternPromotionEvidence | None,
        human_approved: bool,
        actor: Literal["system", "human", "health_monitor"],
    ) -> HarnessPatternTransition:
        evaluation_id = evaluation.evaluation_id if evaluation is not None else None
        identity = (
            f"{pattern.pattern_id}:{pattern.version}:{current.value}:{target.value}:"
            f"{transition_type}:{evaluation_id}:{human_approved}:{actor}:"
            f"{promotion_evidence.evidence_id if promotion_evidence else None}:"
            + ",".join(sorted(reasons))
        )
        transition_id = uuid5(
            NAMESPACE_URL,
            f"hermesgraph:harness-pattern-transition:{identity}",
        )
        existing = await self._policies.list_pattern_transitions(
            pattern.pattern_id,
            tenant_id=pattern.tenant_id,
            project_id=pattern.project_id,
            pattern_version=pattern.version,
        )
        duplicate = next(
            (item for item in existing if item.transition_id == transition_id),
            None,
        )
        if duplicate is not None:
            return duplicate
        payload = {
            "transition_id": transition_id,
            "pattern_id": pattern.pattern_id,
            "pattern_version": pattern.version,
            "tenant_id": pattern.tenant_id,
            "project_id": pattern.project_id,
            "transition_type": transition_type,
            "from_status": current,
            "to_status": target,
            "allowed": allowed,
            "applied": allowed,
            "reasons": reasons,
            "evaluation_id": evaluation_id,
            "evaluation_payload_hash": (
                evaluation.payload_hash if evaluation is not None else None
            ),
            "promotion_evidence_id": (
                promotion_evidence.evidence_id
                if promotion_evidence is not None
                else None
            ),
            "promotion_evidence_payload_hash": (
                promotion_evidence.payload_hash
                if promotion_evidence is not None
                else None
            ),
            "human_approved": human_approved,
            "actor": actor,
            "learning_job_id": None,
            "decided_at": datetime.now(UTC),
        }
        transition = HarnessPatternTransition.model_validate(
            {**payload, "payload_hash": canonical_hash(payload)}
        )
        return await self._policies.save_pattern_transition(transition)

    async def _require_pattern(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        pattern_version: str | None,
    ) -> HarnessPattern:
        pattern = await self._policies.get_pattern(
            pattern_id,
            tenant_id=tenant_id,
            project_id=project_id,
            version=pattern_version,
        )
        if pattern is None:
            raise KeyError(f"Harness pattern not found: {pattern_id}")
        return pattern


def _evaluation_failures(
    pattern: HarnessPattern,
    evaluation: HarnessPatternEvaluation | None,
) -> list[str]:
    if evaluation is None:
        return ["evaluation_required"]
    reasons: list[str] = []
    if (
        evaluation.pattern_id != pattern.pattern_id
        or evaluation.pattern_version != pattern.version
    ):
        reasons.append("evaluation_pattern_mismatch")
    if (
        evaluation.tenant_id != pattern.tenant_id
        or evaluation.project_id != pattern.project_id
    ):
        reasons.append("evaluation_scope_mismatch")
    if evaluation.pattern_payload_hash != pattern.payload_hash:
        reasons.append("evaluation_definition_hash_mismatch")
    if not evaluation.security_passed:
        reasons.append("security_evaluation_failed")
    if not evaluation.evidence_integrity_passed:
        reasons.append("evidence_integrity_failed")
    if not evaluation.required_cases_passed:
        reasons.append("required_case_failed")
    if not evaluation.regression_passed:
        reasons.append("regression_evaluation_failed")
    return reasons


def _promotion_evidence(
    pattern: HarnessPattern,
    evaluation: HarnessPatternEvaluation,
    evaluator: DeterministicPatternEvaluator,
) -> HarnessPatternPromotionEvidence:
    offline_ready = (
        evaluation.security_passed
        and evaluation.evidence_integrity_passed
        and evaluation.required_cases_passed
        and evaluation.regression_passed
    )
    action = "promote_shadow" if offline_ready else "hold"
    reasons = [
        (
            "offline_promotion_gates_passed"
            if offline_ready
            else "offline_promotion_gate_failed"
        ),
        *evaluation.reasons,
    ]
    identity = (
        f"{pattern.pattern_id}:{pattern.version}:{evaluation.evaluation_id}:"
        f"{evaluation.payload_hash}:{evaluator.min_support_cases}:"
        f"{evaluator.max_score_regression}"
    )
    payload = {
        "evidence_id": uuid5(
            NAMESPACE_URL,
            f"hermesgraph:harness-pattern-promotion-evidence:{identity}",
        ),
        "pattern_id": pattern.pattern_id,
        "pattern_version": pattern.version,
        "tenant_id": pattern.tenant_id,
        "project_id": pattern.project_id,
        "evaluation_id": evaluation.evaluation_id,
        "evaluation_payload_hash": evaluation.payload_hash,
        "evaluator_revision": evaluation.evaluator_revision,
        "dataset_revision": evaluation.dataset_revision,
        "supporting_experience_ids": pattern.supporting_experience_ids,
        "contradicting_experience_ids": pattern.contradicting_experience_ids,
        "required_case_ids": evaluation.required_case_ids,
        "failed_required_case_ids": evaluation.failed_required_case_ids,
        "min_support_cases": evaluator.min_support_cases,
        "max_score_regression": evaluator.max_score_regression,
        "baseline_quality_score": evaluation.baseline_quality_score,
        "candidate_quality_score": evaluation.candidate_quality_score,
        "estimated_quality_lift": evaluation.estimated_quality_lift,
        "security_passed": evaluation.security_passed,
        "evidence_integrity_passed": evaluation.evidence_integrity_passed,
        "required_cases_passed": evaluation.required_cases_passed,
        "regression_passed": evaluation.regression_passed,
        "offline_ready": offline_ready,
        "consumer_compatible": evaluation.consumer_compatible,
        "recommended_action": action,
        "reasons": reasons,
        "generated_at": evaluation.generated_at,
    }
    return HarnessPatternPromotionEvidence.model_validate(
        {**payload, "payload_hash": canonical_hash(payload)}
    )


def _promotion_evidence_failures(
    pattern: HarnessPattern,
    evaluation: HarnessPatternEvaluation | None,
    evidence: HarnessPatternPromotionEvidence | None,
) -> list[str]:
    if evidence is None:
        return ["promotion_evidence_required"]
    reasons: list[str] = []
    if (
        evidence.pattern_id != pattern.pattern_id
        or evidence.pattern_version != pattern.version
        or evidence.tenant_id != pattern.tenant_id
        or evidence.project_id != pattern.project_id
    ):
        reasons.append("promotion_evidence_scope_mismatch")
    if evaluation is None or (
        evidence.evaluation_id != evaluation.evaluation_id
        or evidence.evaluation_payload_hash != evaluation.payload_hash
    ):
        reasons.append("promotion_evidence_evaluation_mismatch")
    if not evidence.offline_ready:
        reasons.append("promotion_evidence_not_ready")
    return reasons


__all__ = ["HarnessPatternEvolutionService"]
