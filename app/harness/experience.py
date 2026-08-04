from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.contracts import HarnessExperienceRepository
from app.domain.models import RunTrajectory
from app.harness.diagnosis import (
    DIAGNOSER_REVISION,
    EVALUATOR_REVISION,
    diagnose_trajectory,
    evaluate_trajectory,
)
from app.harness.features import (
    build_case_features,
    canonical_json_hash,
    summarize_tools,
    task_fingerprint,
)
from app.harness.models import (
    HarnessConfigDelta,
    HarnessExperienceEntry,
    HarnessExperienceEvaluation,
    HarnessRewardVector,
    canonical_hash,
)

HarnessTrigger = Literal["run_completed", "feedback_received"]


@dataclass(frozen=True, slots=True)
class HarnessExperienceCollectionResult:
    experience: HarnessExperienceEntry
    evaluation: HarnessExperienceEvaluation
    experience_created: bool
    evaluation_created: bool


class HarnessExperienceService:
    """Observe-only, deterministic experience collection with no runtime mutation."""

    def __init__(self, repository: HarnessExperienceRepository) -> None:
        self._repository = repository

    async def collect(
        self,
        trajectory: RunTrajectory,
        *,
        trigger: HarnessTrigger,
        native_change_set_ids: list[UUID] | None = None,
    ) -> HarnessExperienceCollectionResult:
        experience = assemble_experience(trajectory)
        existing = await self._repository.get(
            experience.experience_id,
            tenant_id=experience.tenant_id,
            project_id=experience.project_id,
        )
        stored = await self._repository.save(experience)
        evaluation = assemble_evaluation(
            trajectory,
            stored,
            trigger=trigger,
            native_change_set_ids=native_change_set_ids or [],
        )
        existing_evaluation = await self._repository.get_evaluation(
            evaluation.evaluation_id,
            tenant_id=evaluation.tenant_id,
            project_id=evaluation.project_id,
        )
        stored_evaluation = await self._repository.save_evaluation(evaluation)
        return HarnessExperienceCollectionResult(
            experience=stored,
            evaluation=stored_evaluation,
            experience_created=existing is None,
            evaluation_created=existing_evaluation is None,
        )


def assemble_experience(trajectory: RunTrajectory) -> HarnessExperienceEntry:
    base = trajectory.model_copy(update={"feedback_score": None, "feedback_text": None})
    features = build_case_features(base)
    quality = evaluate_trajectory(base)
    diagnosis = diagnose_trajectory(base, quality=quality)
    snapshot_payload = (
        base.snapshot.model_dump(mode="json")
        if base.snapshot is not None
        else {
            "model": base.context.model,
            "domain_pack": base.context.domain_pack,
            "skill_versions": base.context.skill_versions,
            "snapshot": "unavailable",
        }
    )
    snapshot_hash = canonical_json_hash(snapshot_payload)
    experience_id = uuid5(
        NAMESPACE_URL,
        f"hermesgraph:harness:{base.context.run_id}:{snapshot_hash}:{DIAGNOSER_REVISION}",
    )
    created_at = base.completed_at or base.context.started_at
    payload = {
        "experience_id": experience_id,
        "tenant_id": base.context.tenant_id,
        "project_id": base.context.project_id,
        "user_id": base.context.user_id,
        "run_id": base.context.run_id,
        "task_fingerprint": task_fingerprint(features),
        "case_features": features,
        "snapshot_hash": snapshot_hash,
        "baseline_policy_versions": (
            dict(base.snapshot.policy_versions) if base.snapshot is not None else {}
        ),
        "overlay_id": (
            base.snapshot.harness_overlay_id if base.snapshot is not None else None
        ),
        "overlay_hash": (
            base.snapshot.harness_overlay_hash if base.snapshot is not None else None
        ),
        "applied_pattern_versions": (
            list(
                base.snapshot.harness_execution_policy.get(
                    "applied_pattern_versions",
                    [],
                )
            )
            if base.snapshot is not None
            else []
        ),
        "config_delta": HarnessConfigDelta(),
        "trajectory_hash": canonical_json_hash(base.model_dump(mode="json")),
        "tool_sequence_summary": summarize_tools(base),
        "diagnosis": diagnosis,
        "reward_vector": HarnessRewardVector(
            passed=diagnosis.success,
            quality_score=quality.quality_score,
            feedback_score=None,
        ),
        "native_change_set_ids": [],
        "created_at": created_at,
    }
    return HarnessExperienceEntry.model_validate(
        {**payload, "payload_hash": canonical_hash(payload)}
    )


def assemble_evaluation(
    trajectory: RunTrajectory,
    experience: HarnessExperienceEntry,
    *,
    trigger: HarnessTrigger,
    native_change_set_ids: list[UUID],
) -> HarnessExperienceEvaluation:
    quality = evaluate_trajectory(trajectory)
    signal_kind = (
        "explicit_feedback"
        if trigger == "feedback_received" and trajectory.feedback_score is not None
        else "run_outcome"
    )
    passed = (
        trajectory.status.value == "completed"
        and trajectory.answer is not None
        and quality.quality_score >= 0.65
        and quality.unsupported_claim_rate <= 0.2
        and (trajectory.feedback_score is None or trajectory.feedback_score >= 0.0)
    )
    identity = (
        f"{experience.experience_id}:{signal_kind}:{EVALUATOR_REVISION}:"
        f"{trajectory.feedback_score if signal_kind == 'explicit_feedback' else 'none'}"
    )
    evaluation_id = uuid5(NAMESPACE_URL, identity)
    payload = {
        "evaluation_id": evaluation_id,
        "experience_id": experience.experience_id,
        "tenant_id": experience.tenant_id,
        "project_id": experience.project_id,
        "run_id": experience.run_id,
        "signal_kind": signal_kind,
        "trigger": trigger,
        "quality_vector": quality,
        "reward_vector": HarnessRewardVector(
            passed=passed,
            quality_score=quality.quality_score,
            feedback_score=trajectory.feedback_score,
        ),
        "native_change_set_ids": sorted(set(native_change_set_ids), key=str)[:100],
        "evaluator_revision": EVALUATOR_REVISION,
        "created_at": trajectory.completed_at or trajectory.context.started_at,
    }
    return HarnessExperienceEvaluation.model_validate(
        {**payload, "payload_hash": canonical_hash(payload)}
    )
