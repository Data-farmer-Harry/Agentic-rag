from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import SkillStatus
from app.domain.models import (
    PromotionDecision,
    SkillDefinition,
    SkillEvaluation,
)


@dataclass(frozen=True, slots=True)
class PromotionResult:
    skill: SkillDefinition
    decision: PromotionDecision


class PromotionStateMachine:
    """Explicit promotion gates; no status skipping and no direct activation."""

    _NEXT_STATUS = {
        SkillStatus.DRAFT: SkillStatus.SECURITY_REVIEW,
        SkillStatus.SECURITY_REVIEW: SkillStatus.OFFLINE_PASS,
        SkillStatus.OFFLINE_PASS: SkillStatus.SHADOW,
        SkillStatus.SHADOW: SkillStatus.CANARY,
        SkillStatus.CANARY: SkillStatus.ACTIVE,
        SkillStatus.ACTIVE: SkillStatus.DEPRECATED,
    }
    _EVALUATED_TARGETS = {
        SkillStatus.OFFLINE_PASS,
        SkillStatus.SHADOW,
        SkillStatus.CANARY,
        SkillStatus.ACTIVE,
    }
    _HUMAN_APPROVAL_TARGETS = {SkillStatus.CANARY, SkillStatus.ACTIVE}
    _ROLLBACK_SOURCES = {
        SkillStatus.OFFLINE_PASS,
        SkillStatus.SHADOW,
        SkillStatus.CANARY,
        SkillStatus.ACTIVE,
    }

    def __init__(
        self,
        *,
        max_score_regression: float = 0.02,
        max_unsupported_claim_rate: float = 0.05,
    ) -> None:
        if not 0.0 <= max_score_regression <= 1.0:
            raise ValueError("max_score_regression must be between 0 and 1")
        if not 0.0 <= max_unsupported_claim_rate <= 1.0:
            raise ValueError("max_unsupported_claim_rate must be between 0 and 1")
        self._max_score_regression = max_score_regression
        self._max_unsupported_claim_rate = max_unsupported_claim_rate

    def decide(
        self,
        skill: SkillDefinition,
        to_status: SkillStatus,
        *,
        evaluation: SkillEvaluation | None = None,
        human_approved: bool = False,
    ) -> PromotionDecision:
        reasons: list[str] = []
        expected = self._NEXT_STATUS.get(skill.status)
        if to_status == SkillStatus.ACTIVE and skill.status != SkillStatus.CANARY:
            reasons.append("direct_active_promotion_forbidden")
        if expected != to_status:
            reasons.append("status_transition_must_be_sequential")

        if to_status in self._EVALUATED_TARGETS:
            reasons.extend(self._evaluation_failures(skill, evaluation))
        if to_status in self._HUMAN_APPROVAL_TARGETS and not human_approved:
            reasons.append("human_approval_required")
        if not reasons:
            reasons.append("promotion_gates_passed")
        return PromotionDecision(
            skill_id=skill.skill_id,
            from_status=skill.status,
            to_status=to_status,
            allowed=reasons == ["promotion_gates_passed"],
            reasons=reasons,
        )

    def transition(
        self,
        skill: SkillDefinition,
        to_status: SkillStatus,
        *,
        evaluation: SkillEvaluation | None = None,
        human_approved: bool = False,
    ) -> PromotionResult:
        decision = self.decide(
            skill,
            to_status,
            evaluation=evaluation,
            human_approved=human_approved,
        )
        updated = skill.model_copy(update={"status": to_status}) if decision.allowed else skill
        return PromotionResult(skill=updated, decision=decision)

    def promote(
        self,
        skill: SkillDefinition,
        *,
        evaluation: SkillEvaluation | None = None,
        human_approved: bool = False,
    ) -> PromotionResult:
        target = self._NEXT_STATUS.get(skill.status)
        if target is None:
            decision = PromotionDecision(
                skill_id=skill.skill_id,
                from_status=skill.status,
                to_status=skill.status,
                allowed=False,
                reasons=["no_forward_transition_available"],
            )
            return PromotionResult(skill=skill, decision=decision)
        return self.transition(
            skill,
            target,
            evaluation=evaluation,
            human_approved=human_approved,
        )

    def rollback(self, skill: SkillDefinition, *, reason: str) -> PromotionResult:
        reasons: list[str] = []
        if skill.status not in self._ROLLBACK_SOURCES:
            reasons.append("status_not_rollback_eligible")
        if not reason.strip():
            reasons.append("rollback_reason_required")
        allowed = not reasons
        if allowed:
            reasons.append(f"rollback:{reason.strip()}")
        decision = PromotionDecision(
            skill_id=skill.skill_id,
            from_status=skill.status,
            to_status=SkillStatus.ROLLED_BACK,
            allowed=allowed,
            reasons=reasons,
        )
        updated = skill.model_copy(update={"status": SkillStatus.ROLLED_BACK}) if allowed else skill
        return PromotionResult(skill=updated, decision=decision)

    def _evaluation_failures(
        self,
        skill: SkillDefinition,
        evaluation: SkillEvaluation | None,
    ) -> list[str]:
        if evaluation is None:
            return ["evaluation_required"]
        reasons: list[str] = []
        if evaluation.skill_id != skill.skill_id:
            reasons.append("evaluation_skill_mismatch")
        if evaluation.skill_version != skill.version:
            reasons.append("evaluation_skill_version_mismatch")
        if (
            evaluation.tenant_id != skill.tenant_id
            or evaluation.project_id != skill.project_id
        ):
            reasons.append("evaluation_scope_mismatch")
        if not evaluation.security_passed:
            reasons.append("security_evaluation_failed")
        if not evaluation.regression_passed:
            reasons.append("regression_evaluation_failed")
        if evaluation.candidate_score < evaluation.baseline_score - self._max_score_regression:
            reasons.append("candidate_score_regressed")
        if evaluation.unsupported_claim_rate > self._max_unsupported_claim_rate:
            reasons.append("unsupported_claim_rate_too_high")
        return reasons
