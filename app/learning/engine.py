from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.domain.contracts import (
    LearningChangeSetRepository,
    MemoryRepository,
    SkillRepository,
    SkillTransitionRepository,
    TrajectoryRepository,
)
from app.domain.enums import RunStatus, SkillStatus
from app.domain.models import (
    LearningChangeSet,
    MemoryCandidate,
    MemoryRecord,
    PromotionDecision,
    RunTrajectory,
    SkillDefinition,
    SkillEvaluation,
    SkillPromotionEvidence,
    SkillTransitionEvent,
)
from app.learning.evaluator import DeterministicExperienceEvaluator
from app.learning.execution import current_learning_fence
from app.learning.promotion import PromotionResult, PromotionStateMachine
from app.learning.refinement import SkillRefinementDecision, SkillRefiner
from app.learning.reflection import (
    DeterministicExperienceReflector,
    ExperienceReflection,
    ExperienceReflector,
)
from app.learning.safety import (
    LEARNING_GATE_REVISION,
    AutomaticLearningDecision,
    annotate_trajectory_for_automatic_learning,
)
from app.learning.skill_miner import RepeatedTrajectorySkillMiner, SkillMiningDecision
from app.memory.memory_write_gate import MemoryWriteDecision, MemoryWriteGate


@dataclass(frozen=True, slots=True)
class MemoryRejection:
    candidate: MemoryCandidate
    decision: MemoryWriteDecision


@dataclass(frozen=True, slots=True)
class LearningOutcome:
    run_id: UUID
    reflection: ExperienceReflection
    memories_written: tuple[MemoryRecord, ...]
    memories_rejected: tuple[MemoryRejection, ...]
    mining: SkillMiningDecision
    skill_candidate: SkillDefinition | None
    refinement: SkillRefinementDecision | None
    change_sets: tuple[LearningChangeSet, ...]


class LearningEngine:
    """Offline trace -> reflection -> gated memory -> draft skill pipeline."""

    _TERMINAL_STATUSES = {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }

    def __init__(
        self,
        trajectory_repository: TrajectoryRepository,
        memory_repository: MemoryRepository,
        skill_repository: SkillRepository,
        *,
        memory_gate: MemoryWriteGate | None = None,
        reflector: ExperienceReflector | None = None,
        skill_miner: RepeatedTrajectorySkillMiner | None = None,
        promotion: PromotionStateMachine | None = None,
        change_set_repository: LearningChangeSetRepository | None = None,
        transition_repository: SkillTransitionRepository | None = None,
        skill_refiner: SkillRefiner | None = None,
    ) -> None:
        self._trajectories = trajectory_repository
        self._memories = memory_repository
        self._skills = skill_repository
        self._memory_gate = memory_gate or MemoryWriteGate()
        self._reflector = reflector or DeterministicExperienceReflector()
        self._skill_miner = skill_miner or RepeatedTrajectorySkillMiner()
        self._promotion = promotion or PromotionStateMachine()
        self._change_sets = change_set_repository
        self._transitions = transition_repository
        self._skill_refiner = skill_refiner or SkillRefiner()

    async def learn(self, trajectory: RunTrajectory) -> LearningOutcome:
        reflection = await self.reflect(trajectory)
        return await self.apply_reflection(reflection.trajectory, reflection)

    async def reflect(self, trajectory: RunTrajectory) -> ExperienceReflection:
        if trajectory.status not in self._TERMINAL_STATUSES:
            raise ValueError("LearningEngine accepts only terminal trajectories")
        audited, decision = annotate_trajectory_for_automatic_learning(trajectory)
        await self._trajectories.save(audited)
        if not decision.allowed:
            return self._nonlearnable_reflection(audited, decision)
        reflection_result = self._reflector.reflect(audited)
        return (
            await reflection_result if inspect.isawaitable(reflection_result) else reflection_result
        )

    async def apply_reflection(
        self,
        trajectory: RunTrajectory,
        reflection: ExperienceReflection,
    ) -> LearningOutcome:
        if trajectory.status not in self._TERMINAL_STATUSES:
            raise ValueError("LearningEngine accepts only terminal trajectories")
        if reflection.trajectory.context.run_id != trajectory.context.run_id:
            raise ValueError("Reflection trajectory does not match the learning run")
        audited, decision = annotate_trajectory_for_automatic_learning(trajectory)
        await self._trajectories.save(audited)
        if not decision.allowed:
            return self._nonlearnable_outcome(audited, decision)
        written: list[MemoryRecord] = []
        rejected: list[MemoryRejection] = []
        for memory_candidate in reflection.memory_candidates:
            memory_decision = self._memory_gate.evaluate(memory_candidate)
            if memory_decision.allowed:
                written.append(await self._memory_gate.write(self._memories, memory_candidate))
            else:
                rejected.append(
                    MemoryRejection(candidate=memory_candidate, decision=memory_decision)
                )

        similar = await self._trajectories.find_similar(audited, limit=20)
        mining = self._skill_miner.analyze(self._deduplicate([audited, *similar]))
        skill_candidate = mining.candidate
        refinement: SkillRefinementDecision | None = None
        if skill_candidate is not None:
            if skill_candidate.status != SkillStatus.DRAFT:
                raise RuntimeError("Mined skills must remain in draft status")
            existing = await self._skills.get_by_name(
                skill_candidate.name,
                tenant_id=skill_candidate.tenant_id,
                project_id=skill_candidate.project_id,
            )
            if existing is None:
                skill_candidate = await self._skills.save(skill_candidate)
            else:
                refinement = self._skill_refiner.refine(existing, skill_candidate)
                if refinement.candidate is None:
                    skill_candidate = None
                else:
                    existing_version = await self._skills.get_by_name(
                        refinement.candidate.name,
                        tenant_id=refinement.candidate.tenant_id,
                        project_id=refinement.candidate.project_id,
                        version=refinement.candidate.version,
                    )
                    skill_candidate = (
                        existing_version
                        if existing_version is not None
                        else await self._skills.save(refinement.candidate)
                    )
        change_sets = self._build_change_sets(
            audited,
            memories=written,
            skill=skill_candidate,
            reflection=reflection,
            refinement=refinement,
        )
        if self._change_sets is not None:
            for change_set in change_sets:
                await self._change_sets.save(change_set)
        return LearningOutcome(
            run_id=audited.context.run_id,
            reflection=reflection,
            memories_written=tuple(written),
            memories_rejected=tuple(rejected),
            mining=mining,
            skill_candidate=skill_candidate,
            refinement=refinement,
            change_sets=change_sets,
        )

    async def process_completed_run(self, trajectory: RunTrajectory) -> LearningOutcome:
        return await self.learn(trajectory)

    @staticmethod
    def _nonlearnable_reflection(
        trajectory: RunTrajectory,
        decision: AutomaticLearningDecision,
    ) -> ExperienceReflection:
        evaluation = DeterministicExperienceEvaluator().evaluate(trajectory)
        return ExperienceReflection(
            trajectory=trajectory,
            evaluation=evaluation,
            outcome="non_learnable",
            summary=decision.audit_summary,
            strengths=(),
            weaknesses=("automatic_learning_blocked", *decision.reasons),
            action_sequence=tuple(event.tool_name for event in trajectory.tool_events),
            memory_candidates=(),
            reflector_revision=LEARNING_GATE_REVISION,
            trigger_reason="non_learnable_citation_provenance_gate",
        )

    @classmethod
    def _nonlearnable_outcome(
        cls,
        trajectory: RunTrajectory,
        decision: AutomaticLearningDecision,
    ) -> LearningOutcome:
        reflection = cls._nonlearnable_reflection(trajectory, decision)
        return LearningOutcome(
            run_id=trajectory.context.run_id,
            reflection=reflection,
            memories_written=(),
            memories_rejected=(),
            mining=SkillMiningDecision(
                candidate=None,
                reasons=("automatic_learning_blocked", *decision.reasons),
                repeated_runs=0,
                successful_runs=0,
                average_sequence_distance=None,
            ),
            skill_candidate=None,
            refinement=None,
            change_sets=(),
        )

    async def transition_skill(
        self,
        skill_id: UUID,
        to_status: SkillStatus,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
        evaluation: SkillEvaluation | None = None,
        promotion_evidence: SkillPromotionEvidence | None = None,
        human_approved: bool = False,
    ) -> PromotionDecision:
        skill = await self._require_skill(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )
        result = self._promotion.transition(
            skill,
            to_status,
            evaluation=evaluation,
            human_approved=human_approved,
        )
        decision = result.decision.model_copy(
            update={
                "promotion_evidence_id": (
                    promotion_evidence.evidence_id if promotion_evidence is not None else None
                )
            }
        )
        applied = False
        if decision.allowed:
            await self._skills.save(result.skill)
            applied = True
        event = await self.record_transition_decision(
            skill,
            decision,
            transition_type="promotion",
            applied=applied,
            evaluation=evaluation,
            promotion_evidence=promotion_evidence,
            human_approved=human_approved,
        )
        return decision.model_copy(
            update={"transition_id": (event.transition_id if event is not None else None)}
        )

    async def rollback_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
        reason: str,
        promotion_evidence: SkillPromotionEvidence | None = None,
    ) -> PromotionDecision:
        skill = await self._require_skill(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )
        result: PromotionResult = self._promotion.rollback(skill, reason=reason)
        decision = result.decision.model_copy(
            update={
                "promotion_evidence_id": (
                    promotion_evidence.evidence_id if promotion_evidence is not None else None
                )
            }
        )
        applied = False
        if decision.allowed:
            await self._skills.save(result.skill)
            applied = True
        event = await self.record_transition_decision(
            skill,
            decision,
            transition_type="rollback",
            applied=applied,
            promotion_evidence=promotion_evidence,
        )
        return decision.model_copy(
            update={"transition_id": (event.transition_id if event is not None else None)}
        )

    async def record_transition_decision(
        self,
        skill: SkillDefinition,
        decision: PromotionDecision,
        *,
        transition_type: Literal["promotion", "rollback", "health_gate"],
        applied: bool,
        evaluation: SkillEvaluation | None = None,
        promotion_evidence: SkillPromotionEvidence | None = None,
        human_approved: bool = False,
    ) -> SkillTransitionEvent | None:
        if self._transitions is None:
            return None
        fence = current_learning_fence()
        if decision.transition_id is not None:
            transition_id = decision.transition_id
        elif fence is not None:
            transition_id = uuid5(
                NAMESPACE_URL,
                (
                    "hermesgraph:skill-transition:"
                    f"{fence.job_id}:{skill.skill_id}:{skill.version}:"
                    f"{decision.from_status.value}:{decision.to_status.value}:"
                    f"{transition_type}"
                ),
            )
        else:
            transition_id = uuid4()
        event = SkillTransitionEvent(
            transition_id=transition_id,
            skill_id=skill.skill_id,
            tenant_id=skill.tenant_id,
            project_id=skill.project_id,
            skill_version=skill.version,
            transition_type=transition_type,
            from_status=decision.from_status,
            to_status=decision.to_status,
            allowed=decision.allowed,
            applied=applied,
            reasons=decision.reasons,
            evaluation_id=(evaluation.evaluation_id if evaluation is not None else None),
            promotion_evidence=promotion_evidence,
            human_approved=human_approved,
            learning_job_id=fence.job_id if fence is not None else None,
            decided_at=decision.decided_at,
        )
        return await self._transitions.save(event)

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

    @staticmethod
    def _deduplicate(trajectories: Sequence[RunTrajectory]) -> tuple[RunTrajectory, ...]:
        by_id = {trajectory.context.run_id: trajectory for trajectory in trajectories}
        return tuple(by_id.values())

    @staticmethod
    def _build_change_sets(
        trajectory: RunTrajectory,
        *,
        memories: Sequence[MemoryRecord],
        skill: SkillDefinition | None,
        reflection: ExperienceReflection,
        refinement: SkillRefinementDecision | None,
    ) -> tuple[LearningChangeSet, ...]:
        changes: list[LearningChangeSet] = []
        scope = {
            "tenant_id": trajectory.context.tenant_id,
            "project_id": trajectory.context.project_id,
            "user_id": trajectory.context.user_id,
        }
        evaluation = {
            "passed": reflection.evaluation.passed,
            "quality_score": reflection.evaluation.quality_score,
            "reasons": list(reflection.evaluation.reasons),
            "reflector_revision": reflection.reflector_revision,
            "reflection_fallback_error": reflection.fallback_error,
            "model_reflection_attempted": reflection.model_reflection_attempted,
            "reflection_trigger_reason": reflection.trigger_reason,
        }
        for memory in memories:
            source_run_ids = sorted(
                {item.run_id for item in memory.provenance if item.run_id is not None},
                key=str,
            )
            memory_diff = {
                "operation": "upsert",
                "memory_type": memory.memory_type.value,
                "key": memory.key,
                "summary": memory.summary,
                "confidence": memory.confidence,
            }
            mutation_hash = _stable_hash(
                {
                    "structured_diff": memory_diff,
                    "source_run_ids": [str(item) for item in source_run_ids],
                    "evaluation": evaluation,
                }
            )
            identity = f"memory:{memory.memory_id}:{mutation_hash}"
            changes.append(
                LearningChangeSet(
                    change_set_id=uuid5(NAMESPACE_URL, f"hermesgraph:{identity}"),
                    target_type="memory_record",
                    target_id=str(memory.memory_id),
                    structured_diff=memory_diff,
                    source_run_ids=source_run_ids,
                    expected_benefits=["Recall verified experience in later runs"],
                    risks=["Stale or over-generalized memory may bias future prompts"],
                    scope=scope,
                    evaluation_report=evaluation,
                    rollback_conditions=[
                        "Contradicting verified evidence is observed",
                        "User revokes or corrects the remembered information",
                    ],
                    created_at=memory.updated_at,
                )
            )
        if skill is not None:
            skill_diff: dict[str, object] = {
                "operation": (
                    "create_refinement_draft"
                    if skill.parent_version is not None
                    else "create_draft"
                ),
                "name": skill.name,
                "version": skill.version,
                "status": skill.status.value,
                "steps": [step.model_dump(mode="json") for step in skill.steps],
                "allowed_capabilities": skill.allowed_capabilities,
            }
            if refinement is not None:
                skill_diff.update(
                    {
                        "change_level": refinement.change_level,
                        "refinement_reasons": list(refinement.reasons),
                        "semantic_diff": refinement.semantic_diff,
                    }
                )
            mutation_hash = _stable_hash(
                {
                    "structured_diff": skill_diff,
                    "source_run_ids": [str(item) for item in skill.source_run_ids],
                    "evaluation": evaluation,
                }
            )
            identity = f"skill:{skill.skill_id}:{skill.version}:{mutation_hash}"
            changes.append(
                LearningChangeSet(
                    change_set_id=uuid5(NAMESPACE_URL, f"hermesgraph:{identity}"),
                    target_type="skill_definition",
                    target_id=str(skill.skill_id),
                    parent_version=skill.parent_version,
                    structured_diff=skill_diff,
                    source_run_ids=skill.source_run_ids,
                    expected_benefits=["Reuse a repeated successful action sequence"],
                    risks=[
                        "The repeated pattern may not generalize beyond its source tasks",
                        "A capability contract may change before promotion",
                    ],
                    scope={
                        "tenant_id": skill.tenant_id,
                        "project_id": skill.project_id,
                    },
                    evaluation_report=evaluation,
                    rollback_conditions=[
                        "Offline evaluation regresses against baseline",
                        "Unsupported-claim or security thresholds are exceeded",
                    ],
                    created_at=skill.created_at,
                )
            )
        return tuple(changes)


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
