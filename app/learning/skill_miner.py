from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from app.domain.enums import SkillStatus
from app.domain.models import RunTrajectory, SkillDefinition, SkillStep
from app.learning.evaluator import DeterministicExperienceEvaluator

_BLOCKED_ACTION_PARTS = {
    "bash",
    "cypher",
    "eval",
    "exec",
    "javascript",
    "powershell",
    "python",
    "shell",
    "sql",
    "subprocess",
    "write",
}
_STOP_WORDS = {
    "about",
    "and",
    "compare",
    "find",
    "for",
    "from",
    "how",
    "method",
    "methods",
    "please",
    "research",
    "the",
    "this",
    "with",
}
_CONTROL_ACTIONS = {"activate_governed_skill", "activate_skill"}


@dataclass(frozen=True, slots=True)
class SkillMiningDecision:
    candidate: SkillDefinition | None
    reasons: tuple[str, ...]
    repeated_runs: int
    successful_runs: int
    average_sequence_distance: float | None


def _sequence(trajectory: RunTrajectory) -> tuple[str, ...]:
    return tuple(
        event.tool_name
        for event in trajectory.tool_events
        if event.tool_name not in _CONTROL_ACTIONS
    )


def _normalized_edit_distance(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    if not left and not right:
        return 0.0
    previous = list(range(len(right) + 1))
    for left_index, left_item in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_item in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_item != right_item),
                )
            )
        previous = current
    return previous[-1] / max(len(left), len(right), 1)


class RepeatedTrajectorySkillMiner:
    """Mines only stable, repeated, successful declarative action sequences."""

    def __init__(
        self,
        *,
        min_similar_runs: int = 3,
        min_successful_runs: int = 2,
        max_sequence_distance: float = 0.34,
        max_quality_spread: float = 0.25,
        allowed_actions: set[str] | frozenset[str] | None = None,
        evaluator: DeterministicExperienceEvaluator | None = None,
    ) -> None:
        if min_similar_runs < 2:
            raise ValueError("min_similar_runs must be at least 2")
        if not 1 <= min_successful_runs <= min_similar_runs:
            raise ValueError("min_successful_runs must be between 1 and min_similar_runs")
        if not 0.0 <= max_sequence_distance <= 1.0:
            raise ValueError("max_sequence_distance must be between 0 and 1")
        if not 0.0 <= max_quality_spread <= 1.0:
            raise ValueError("max_quality_spread must be between 0 and 1")
        self._min_similar_runs = min_similar_runs
        self._min_successful_runs = min_successful_runs
        self._max_sequence_distance = max_sequence_distance
        self._max_quality_spread = max_quality_spread
        self._allowed_actions = frozenset(allowed_actions or ())
        self._evaluator = evaluator or DeterministicExperienceEvaluator()

    def analyze(self, trajectories: Sequence[RunTrajectory]) -> SkillMiningDecision:
        unique = {trajectory.context.run_id: trajectory for trajectory in trajectories}
        runs = sorted(unique.values(), key=lambda item: str(item.context.run_id))
        if len(runs) < self._min_similar_runs:
            return SkillMiningDecision(
                candidate=None,
                reasons=("not_enough_similar_runs",),
                repeated_runs=len(runs),
                successful_runs=0,
                average_sequence_distance=None,
            )

        non_empty = [run for run in runs if _sequence(run)]
        if len(non_empty) < self._min_similar_runs:
            return SkillMiningDecision(
                candidate=None,
                reasons=("not_enough_action_sequences",),
                repeated_runs=len(non_empty),
                successful_runs=0,
                average_sequence_distance=None,
            )

        medoid = min(
            (_sequence(run) for run in non_empty),
            key=lambda sequence: (
                sum(_normalized_edit_distance(sequence, _sequence(run)) for run in non_empty),
                sequence,
            ),
        )
        stable = [
            run
            for run in non_empty
            if _normalized_edit_distance(medoid, _sequence(run)) <= self._max_sequence_distance
        ]
        distances = [_normalized_edit_distance(medoid, _sequence(run)) for run in stable]
        average_distance = round(sum(distances) / len(distances), 6) if distances else None
        if len(stable) < self._min_similar_runs:
            return SkillMiningDecision(
                candidate=None,
                reasons=("action_sequences_not_stable",),
                repeated_runs=len(stable),
                successful_runs=0,
                average_sequence_distance=average_distance,
            )

        evaluated = [(run, self._evaluator.evaluate(run)) for run in stable]
        successful = [(run, evaluation) for run, evaluation in evaluated if evaluation.passed]
        if len(successful) < self._min_successful_runs:
            return SkillMiningDecision(
                candidate=None,
                reasons=("not_enough_successful_runs",),
                repeated_runs=len(stable),
                successful_runs=len(successful),
                average_sequence_distance=average_distance,
            )
        quality_scores = [evaluation.quality_score for _, evaluation in successful]
        if max(quality_scores) - min(quality_scores) > self._max_quality_spread:
            return SkillMiningDecision(
                candidate=None,
                reasons=("quality_not_stable",),
                repeated_runs=len(stable),
                successful_runs=len(successful),
                average_sequence_distance=average_distance,
            )

        unsafe_actions = [action for action in medoid if not self._action_is_allowed(action)]
        if unsafe_actions:
            return SkillMiningDecision(
                candidate=None,
                reasons=("unsafe_or_unapproved_actions",),
                repeated_runs=len(stable),
                successful_runs=len(successful),
                average_sequence_distance=average_distance,
            )

        source_runs = [run for run, _ in successful]
        candidate = self._build_candidate(
            source_runs=source_runs,
            repeated_runs=len(stable),
            sequence=medoid,
            average_distance=average_distance or 0.0,
        )
        return SkillMiningDecision(
            candidate=candidate,
            reasons=("repeated_successful_pattern", "candidate_remains_draft"),
            repeated_runs=len(stable),
            successful_runs=len(successful),
            average_sequence_distance=average_distance,
        )

    def mine(self, trajectories: Sequence[RunTrajectory]) -> SkillDefinition | None:
        return self.analyze(trajectories).candidate

    def is_repeated_pattern(self, trajectories: Sequence[RunTrajectory]) -> bool:
        return self.analyze(trajectories).candidate is not None

    def _action_is_allowed(self, action: str) -> bool:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", action):
            return False
        parts = set(re.split(r"[_.-]", action.casefold()))
        if parts & _BLOCKED_ACTION_PARTS:
            return False
        return action in self._allowed_actions

    def _build_candidate(
        self,
        *,
        source_runs: Sequence[RunTrajectory],
        repeated_runs: int,
        sequence: tuple[str, ...],
        average_distance: float,
    ) -> SkillDefinition:
        source_ids = sorted((run.context.run_id for run in source_runs), key=str)
        trigger_tokens = self._shared_tokens(source_runs)
        signature = "|".join(
            [
                source_runs[0].context.tenant_id,
                source_runs[0].context.project_id,
                *trigger_tokens,
                "::actions::",
                *sequence,
            ]
        )
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        name = self._candidate_name(source_runs, digest)
        created_at: datetime = max(
            run.completed_at or run.context.started_at for run in source_runs
        )
        phrases = sorted({run.user_input.strip()[:160] for run in source_runs if run.user_input})[
            :5
        ]
        return SkillDefinition(
            skill_id=uuid5(NAMESPACE_URL, f"hermesgraph:skill:{digest}"),
            tenant_id=source_runs[0].context.tenant_id,
            project_id=source_runs[0].context.project_id,
            name=name,
            version="0.1.0",
            description=(
                f"Execute a stable {len(sequence)}-step workflow mined from "
                f"{repeated_runs} similar trajectories."
            ),
            status=SkillStatus.DRAFT,
            trigger_intents=trigger_tokens,
            trigger_phrases=phrases,
            steps=[
                SkillStep(
                    action=action,
                    purpose=f"Execute approved action {action}",
                    inputs={},
                )
                for action in sequence
            ],
            allowed_capabilities=sorted(set(sequence)),
            constraints={
                "max_tool_calls": len(sequence),
                "mined_from_repeated_runs": repeated_runs,
                "average_sequence_distance": average_distance,
                "no_arbitrary_code": True,
                "requires_evidence_validation": True,
            },
            source_run_ids=source_ids,
            created_at=created_at,
        )

    def _candidate_name(self, runs: Sequence[RunTrajectory], digest: str) -> str:
        tokens = self._shared_tokens(runs)
        suffix = "_".join(tokens[:4]) if tokens else f"workflow_{digest[:8]}"
        return f"learned_{suffix}"[:64].rstrip("_")

    @staticmethod
    def _shared_tokens(runs: Sequence[RunTrajectory]) -> list[str]:
        token_sets = [
            {
                token.casefold()
                for token in re.findall(r"[a-z][a-z0-9]{2,}", run.user_input.casefold())
                if token.casefold() not in _STOP_WORDS
            }
            for run in runs
        ]
        shared = set.intersection(*token_sets) if token_sets else set()
        return sorted(shared)[:8]
