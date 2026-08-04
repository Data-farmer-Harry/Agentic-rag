from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from uuid import NAMESPACE_URL, uuid5

from app.domain.contracts import TrajectoryRepository
from app.domain.models import (
    RunTrajectory,
    SkillDefinition,
    SkillEvaluation,
    SkillEvaluationCase,
)
from app.learning.evaluator import DeterministicExperienceEvaluator
from app.learning.skill_replay import FrozenCapabilitySkillSandbox

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
_BLOCKED_INPUT_KEYS = {
    "code",
    "command",
    "executable",
    "script",
    "shell",
    "sql",
}
_INJECTION_PATTERNS = (
    "ignore previous",
    "ignore system",
    "reveal secret",
    "system prompt",
)
class DeterministicSkillEvaluator:
    revision = "counterfactual-skill-replay-v2"

    def __init__(
        self,
        trajectories: TrajectoryRepository,
        *,
        experience_evaluator: DeterministicExperienceEvaluator | None = None,
        sandbox: FrozenCapabilitySkillSandbox | None = None,
        min_cases: int = 2,
        min_sequence_similarity: float = 0.66,
        max_score_regression: float = 0.02,
        max_unsupported_claim_rate: float = 0.05,
    ) -> None:
        if min_cases < 1:
            raise ValueError("Skill evaluation requires at least one case")
        for name, value in (
            ("min_sequence_similarity", min_sequence_similarity),
            ("max_score_regression", max_score_regression),
            ("max_unsupported_claim_rate", max_unsupported_claim_rate),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        self._trajectories = trajectories
        self._experience = experience_evaluator or DeterministicExperienceEvaluator()
        self._sandbox = sandbox or FrozenCapabilitySkillSandbox()
        self._min_cases = min_cases
        self._min_sequence_similarity = min_sequence_similarity
        self._max_score_regression = max_score_regression
        self._max_unsupported_claim_rate = max_unsupported_claim_rate

    async def evaluate(self, skill: SkillDefinition) -> SkillEvaluation:
        security_reasons = _skill_security_reasons(skill)
        cases: list[SkillEvaluationCase] = []
        missing_runs: list[str] = []
        for run_id in skill.source_run_ids:
            trajectory = await self._trajectories.get(run_id)
            if trajectory is None:
                missing_runs.append(str(run_id))
                continue
            if (
                trajectory.context.tenant_id != skill.tenant_id
                or trajectory.context.project_id != skill.project_id
            ):
                missing_runs.append(str(run_id))
                continue
            cases.append(await self.evaluate_case(skill, trajectory))

        baseline_score = _average(item.baseline_score for item in cases)
        candidate_score = _average(item.candidate_score for item in cases)
        unsupported_claim_rate = _average(item.unsupported_claim_rate for item in cases)
        enough_cases = len(cases) >= self._min_cases
        regression_passed = (
            enough_cases
            and not missing_runs
            and all(item.passed for item in cases)
            and candidate_score >= baseline_score - self._max_score_regression
            and unsupported_claim_rate <= self._max_unsupported_claim_rate
        )
        notes = [*security_reasons]
        if not enough_cases:
            notes.append("insufficient_evaluation_cases")
        if missing_runs:
            notes.append("missing_or_out_of_scope_source_runs:" + ",".join(missing_runs))
        if regression_passed:
            notes.append("sandbox_counterfactual_non_regression_passed")
        input_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "skill": skill.model_dump(
                        mode="json",
                        exclude={"status", "created_at"},
                    ),
                    "cases": [item.model_dump(mode="json") for item in cases],
                    "missing_runs": missing_runs,
                    "security_reasons": security_reasons,
                    "evaluator_revision": self.revision,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return SkillEvaluation(
            evaluation_id=uuid5(
                NAMESPACE_URL,
                (
                    "hermesgraph:skill-evaluation:"
                    f"{skill.skill_id}:{skill.version}:{self.revision}:{input_fingerprint}"
                ),
            ),
            skill_id=skill.skill_id,
            tenant_id=skill.tenant_id,
            project_id=skill.project_id,
            skill_version=skill.version,
            evaluator_revision=self.revision,
            baseline_score=baseline_score,
            candidate_score=candidate_score,
            unsupported_claim_rate=unsupported_claim_rate,
            security_passed=not security_reasons,
            regression_passed=regression_passed,
            case_count=len(cases),
            passed_cases=sum(item.passed for item in cases),
            cases=cases,
            notes=notes,
        )

    async def evaluate_case(
        self,
        skill: SkillDefinition,
        trajectory: RunTrajectory,
    ) -> SkillEvaluationCase:
        baseline = self._experience.evaluate(trajectory)
        replay = await self._sandbox.replay(skill, trajectory)
        similarity = replay.sequence_similarity
        candidate_score = round(
            baseline.quality_score
            * (0.6 + 0.2 * similarity + 0.2 * replay.tool_success_rate),
            6,
        )
        reasons: list[str] = []
        if not baseline.passed:
            reasons.append("source_run_failed_baseline")
        if not replay.passed:
            reasons.extend(replay.reasons)
        if similarity < self._min_sequence_similarity:
            reasons.append("action_sequence_mismatch")
        if candidate_score < baseline.quality_score - self._max_score_regression:
            reasons.append("projected_score_regression")
        if baseline.unsupported_claim_rate > self._max_unsupported_claim_rate:
            reasons.append("unsupported_claim_rate_too_high")
        if not reasons:
            reasons.append("case_non_regression_passed")
        return SkillEvaluationCase(
            run_id=trajectory.context.run_id,
            baseline_score=baseline.quality_score,
            candidate_score=candidate_score,
            sequence_similarity=similarity,
            tool_success_rate=replay.tool_success_rate,
            unsupported_claim_rate=baseline.unsupported_claim_rate,
            passed=reasons == ["case_non_regression_passed"],
            reasons=sorted(set(reasons)),
            replay=replay,
        )


def _skill_security_reasons(skill: SkillDefinition) -> list[str]:
    reasons: list[str] = []
    allowed = set(skill.allowed_capabilities)
    if not allowed:
        reasons.append("no_allowed_capabilities")
    for step in skill.steps:
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", step.action):
            reasons.append(f"invalid_action:{step.action}")
            continue
        parts = set(re.split(r"[_.-]", step.action.casefold()))
        if parts & _BLOCKED_ACTION_PARTS:
            reasons.append(f"blocked_action:{step.action}")
        if step.action not in allowed:
            reasons.append(f"undeclared_capability:{step.action}")
        blocked_keys = sorted(
            key.casefold() for key in step.inputs if key.casefold() in _BLOCKED_INPUT_KEYS
        )
        if blocked_keys:
            reasons.append(f"blocked_input_keys:{step.action}:{','.join(blocked_keys)}")
        content = " ".join(
            [step.purpose, json.dumps(step.inputs, ensure_ascii=False, sort_keys=True)]
        ).casefold()
        if any(pattern in content for pattern in _INJECTION_PATTERNS):
            reasons.append(f"instruction_injection_pattern:{step.action}")
    configured_limit = skill.constraints.get("max_tool_calls", len(skill.steps))
    if (
        not isinstance(configured_limit, int)
        or isinstance(configured_limit, bool)
        or configured_limit < len(skill.steps)
    ):
        reasons.append("invalid_tool_call_budget")
    return sorted(set(reasons))


def _average(values: Iterable[float]) -> float:
    resolved = list(values)
    return round(sum(resolved) / len(resolved), 6) if resolved else 0.0


__all__ = ["DeterministicSkillEvaluator"]
