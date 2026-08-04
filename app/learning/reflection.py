from __future__ import annotations

import hashlib
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from app.domain.enums import MemoryType, TrustLevel
from app.domain.models import MemoryCandidate, Provenance, RunTrajectory
from app.learning.evaluator import DeterministicExperienceEvaluator, TrajectoryEvaluation


@dataclass(frozen=True, slots=True)
class ExperienceReflection:
    trajectory: RunTrajectory
    evaluation: TrajectoryEvaluation
    outcome: str
    summary: str
    strengths: tuple[str, ...]
    weaknesses: tuple[str, ...]
    action_sequence: tuple[str, ...]
    memory_candidates: tuple[MemoryCandidate, ...]
    reflector_revision: str = "deterministic-experience-reflector-v1"
    fallback_error: str | None = None
    model_reflection_attempted: bool = False
    trigger_reason: str = "deterministic_baseline"


class ExperienceReflector(Protocol):
    def reflect(
        self,
        trajectory: RunTrajectory,
    ) -> ExperienceReflection | Awaitable[ExperienceReflection]: ...


class DeterministicExperienceReflector:
    """Turns a trajectory into auditable candidates without mutating stores."""

    revision = "deterministic-experience-reflector-v1"

    def __init__(self, evaluator: DeterministicExperienceEvaluator | None = None) -> None:
        self._evaluator = evaluator or DeterministicExperienceEvaluator()

    def reflect(self, trajectory: RunTrajectory) -> ExperienceReflection:
        evaluation = self._evaluator.evaluate(trajectory)
        if evaluation.passed:
            outcome = "success"
        elif evaluation.completion_score > 0.0:
            outcome = "partial"
        else:
            outcome = "failure"

        strengths: list[str] = []
        weaknesses: list[str] = []
        if evaluation.completion_score == 1.0:
            strengths.append("run_completed")
        else:
            weaknesses.append("run_incomplete")
        if evaluation.tool_success_rate == 1.0:
            strengths.append("tools_succeeded")
        else:
            weaknesses.append("tool_failures_observed")
        if evaluation.citation_coverage >= 0.9:
            strengths.append("citation_coverage_met")
        else:
            weaknesses.append("citation_coverage_below_target")
        if evaluation.feedback_score is not None:
            (strengths if evaluation.feedback_score >= 0.0 else weaknesses).append(
                "non_negative_feedback" if evaluation.feedback_score >= 0.0 else "negative_feedback"
            )

        run_id = trajectory.context.run_id
        content_hash = hashlib.sha256(
            trajectory.model_dump_json(exclude_none=True).encode("utf-8")
        ).hexdigest()
        base_time = trajectory.completed_at or trajectory.context.started_at
        candidate = MemoryCandidate(
            tenant_id=trajectory.context.tenant_id,
            project_id=trajectory.context.project_id,
            user_id=trajectory.context.user_id,
            memory_type=MemoryType.EPISODIC,
            key=f"run:{run_id}",
            summary=(
                f"Run {run_id} had outcome {outcome} with deterministic quality "
                f"{evaluation.quality_score:.3f}."
            ),
            detail={
                "user_input": trajectory.user_input,
                "status": trajectory.status.value,
                "outcome": outcome,
                "quality_score": evaluation.quality_score,
                "citation_coverage": evaluation.citation_coverage,
                "unsupported_claim_rate": evaluation.unsupported_claim_rate,
                "tool_sequence": [event.tool_name for event in trajectory.tool_events],
                "tool_success": [event.success for event in trajectory.tool_events],
                "tags": sorted(set(trajectory.tags)),
            },
            confidence=max(0.6, min(0.95, round(0.55 + 0.4 * evaluation.quality_score, 6))),
            provenance=[
                Provenance(
                    source_type="run_trajectory",
                    source_id=str(run_id),
                    run_id=run_id,
                    content_hash=content_hash,
                    trust=TrustLevel.OBSERVED,
                    observed_at=base_time,
                )
            ],
            expires_at=base_time + timedelta(days=180),
        )
        return ExperienceReflection(
            trajectory=trajectory,
            evaluation=evaluation,
            outcome=outcome,
            summary=candidate.summary,
            strengths=tuple(strengths),
            weaknesses=tuple(weaknesses),
            action_sequence=tuple(event.tool_name for event in trajectory.tool_events),
            memory_candidates=(candidate,),
            reflector_revision=self.revision,
        )
