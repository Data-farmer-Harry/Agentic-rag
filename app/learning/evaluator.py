from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import EvidenceLevel, RunStatus
from app.domain.models import RunTrajectory


@dataclass(frozen=True, slots=True)
class TrajectoryEvaluation:
    run_id: str
    quality_score: float
    completion_score: float
    tool_success_rate: float
    citation_coverage: float
    unsupported_claim_rate: float
    feedback_score: float | None
    passed: bool
    reasons: tuple[str, ...]


class DeterministicExperienceEvaluator:
    """Transparent offline baseline; no model calls or learned weights."""

    def __init__(
        self,
        *,
        pass_threshold: float = 0.65,
        max_unsupported_claim_rate: float = 0.2,
    ) -> None:
        if not 0.0 <= pass_threshold <= 1.0:
            raise ValueError("pass_threshold must be between 0 and 1")
        if not 0.0 <= max_unsupported_claim_rate <= 1.0:
            raise ValueError("max_unsupported_claim_rate must be between 0 and 1")
        self._pass_threshold = pass_threshold
        self._max_unsupported_claim_rate = max_unsupported_claim_rate

    def evaluate(self, trajectory: RunTrajectory) -> TrajectoryEvaluation:
        answer = trajectory.answer
        completed = trajectory.status == RunStatus.COMPLETED and answer is not None
        completion_score = 1.0 if completed else 0.0
        tool_success_rate = (
            sum(event.success for event in trajectory.tool_events) / len(trajectory.tool_events)
            if trajectory.tool_events
            else 1.0
        )

        claims = answer.claims if answer is not None else []
        if claims:
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
        quality_score = round(
            0.35 * completion_score
            + 0.20 * tool_success_rate
            + 0.30 * citation_coverage
            + 0.15 * normalized_feedback,
            6,
        )
        reasons: list[str] = []
        if not completed:
            reasons.append("run_not_completed")
        if tool_success_rate < 1.0:
            reasons.append("tool_failures")
        if citation_coverage < 0.9:
            reasons.append("low_citation_coverage")
        if unsupported_claim_rate > self._max_unsupported_claim_rate:
            reasons.append("unsupported_claims")
        if trajectory.feedback_score is not None and trajectory.feedback_score < 0.0:
            reasons.append("negative_feedback")
        passed = (
            completed
            and quality_score >= self._pass_threshold
            and unsupported_claim_rate <= self._max_unsupported_claim_rate
            and (trajectory.feedback_score is None or trajectory.feedback_score >= 0.0)
        )
        if passed:
            reasons.append("deterministic_baseline_passed")
        return TrajectoryEvaluation(
            run_id=str(trajectory.context.run_id),
            quality_score=quality_score,
            completion_score=completion_score,
            tool_success_rate=round(tool_success_rate, 6),
            citation_coverage=round(citation_coverage, 6),
            unsupported_claim_rate=round(unsupported_claim_rate, 6),
            feedback_score=trajectory.feedback_score,
            passed=passed,
            reasons=tuple(reasons),
        )
