from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from time import perf_counter
from typing import Any

from app.domain.enums import SkillStatus
from app.domain.models import (
    RunTrajectory,
    SkillDefinition,
    SkillReplayCaseResult,
    SkillReplayStepResult,
    ToolEvent,
)
from app.skills.registry import (
    SkillActivationRegistry,
    SkillExecutionError,
    SkillExecutionRegistry,
)

_CONTROL_ACTIONS = {"activate_governed_skill", "activate_skill"}


class FrozenCapabilitySkillSandbox:
    """Executes declarative Skill steps against immutable trajectory fixtures."""

    revision = "frozen-capability-sandbox-v1"

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        max_steps: int = 20,
        max_fixture_output_bytes: int = 100_000,
    ) -> None:
        if not 0.05 <= timeout_seconds <= 300:
            raise ValueError("Skill replay timeout must be between 0.05 and 300 seconds")
        if not 1 <= max_steps <= 100:
            raise ValueError("Skill replay max_steps must be between 1 and 100")
        if not 1_000 <= max_fixture_output_bytes <= 2_000_000:
            raise ValueError(
                "Skill replay max_fixture_output_bytes must be between 1000 and 2000000"
            )
        self._timeout_seconds = timeout_seconds
        self._max_steps = max_steps
        self._max_fixture_output_bytes = max_fixture_output_bytes

    async def replay(
        self,
        skill: SkillDefinition,
        trajectory: RunTrajectory,
    ) -> SkillReplayCaseResult:
        if (
            trajectory.context.tenant_id != skill.tenant_id
            or trajectory.context.project_id != skill.project_id
        ):
            raise ValueError("Skill replay trajectory is outside the skill scope")
        fixture = [
            event for event in trajectory.tool_events if event.tool_name not in _CONTROL_ACTIONS
        ]
        if len(skill.steps) > self._max_steps:
            return self._failed_without_execution(
                skill,
                trajectory,
                fixture,
                "sandbox_step_budget_exceeded",
            )

        execution = SkillExecutionRegistry()
        cursor = 0
        step_reports: list[SkillReplayStepResult] = []
        failure_code: str | None = None

        for action in sorted({step.action for step in skill.steps}):

            async def handler(
                inputs: Mapping[str, Any],
                state: Mapping[str, Any],
                *,
                expected_action: str = action,
            ) -> dict[str, object]:
                nonlocal cursor, failure_code
                started = perf_counter()
                step_index = len(step_reports)
                input_hash = _stable_hash(
                    {
                        "inputs": dict(inputs),
                        "previous_output": state.get("previous_output"),
                    }
                )
                fixture_index = cursor if cursor < len(fixture) else None
                event = fixture[cursor] if cursor < len(fixture) else None
                cursor += 1
                error_code: str | None = None
                output_hash: str | None = None
                success = False
                if event is None:
                    error_code = "fixture_exhausted"
                elif event.tool_name != expected_action:
                    error_code = "fixture_action_mismatch"
                elif not event.success:
                    error_code = "fixture_tool_failed"
                else:
                    output_hash = _event_output_hash(
                        event,
                        max_bytes=self._max_fixture_output_bytes,
                    )
                    success = True
                step_reports.append(
                    SkillReplayStepResult(
                        step_index=step_index,
                        action=expected_action,
                        fixture_event_index=fixture_index,
                        input_hash=input_hash,
                        output_hash=output_hash,
                        success=success,
                        error_code=error_code,
                        duration_ms=max(0, round((perf_counter() - started) * 1_000)),
                    )
                )
                if error_code is not None:
                    failure_code = error_code
                    raise SkillExecutionError(error_code)
                return {
                    "fixture_event_index": fixture_index,
                    "output_hash": output_hash,
                    "success": True,
                }

            execution.register_action(action, handler)

        replay_skill = skill.model_copy(update={"status": SkillStatus.OFFLINE_PASS})
        activated = SkillActivationRegistry().activate(
            replay_skill,
            set(replay_skill.allowed_capabilities),
            offline_replay=True,
        )
        completed = False
        try:
            await asyncio.wait_for(
                execution.execute(
                    activated,
                    initial_state={
                        "run_id": str(trajectory.context.run_id),
                        "user_input_hash": hashlib.sha256(
                            trajectory.user_input.encode("utf-8")
                        ).hexdigest(),
                    },
                ),
                timeout=self._timeout_seconds,
            )
            completed = True
        except TimeoutError:
            failure_code = "sandbox_timeout"
        except SkillExecutionError:
            failure_code = failure_code or "sandbox_execution_failed"

        expected_actions = [step.action for step in skill.steps]
        fixture_actions = [event.tool_name for event in fixture]
        similarity = _sequence_similarity(expected_actions, fixture_actions)
        success_rate = (
            sum(item.success for item in step_reports) / len(step_reports)
            if step_reports
            else 0.0
        )
        reasons: list[str] = []
        if failure_code is not None:
            reasons.append(failure_code)
        if similarity < 1.0:
            reasons.append("sandbox_sequence_diverged")
        if cursor < len(fixture):
            reasons.append("sandbox_fixture_events_remaining")
        if success_rate < 1.0:
            reasons.append("sandbox_tool_failure")
        passed = completed and not reasons
        if passed:
            reasons.append("sandbox_replay_passed")
        return SkillReplayCaseResult(
            run_id=trajectory.context.run_id,
            sandbox_revision=self.revision,
            baseline_action_count=len(fixture),
            candidate_action_count=len(skill.steps),
            sequence_similarity=similarity,
            tool_success_rate=round(success_rate, 6),
            completed=completed,
            passed=passed,
            steps=step_reports,
            reasons=sorted(set(reasons)),
        )

    def _failed_without_execution(
        self,
        skill: SkillDefinition,
        trajectory: RunTrajectory,
        fixture: list[ToolEvent],
        reason: str,
    ) -> SkillReplayCaseResult:
        return SkillReplayCaseResult(
            run_id=trajectory.context.run_id,
            sandbox_revision=self.revision,
            baseline_action_count=len(fixture),
            candidate_action_count=len(skill.steps),
            sequence_similarity=_sequence_similarity(
                [step.action for step in skill.steps],
                [event.tool_name for event in fixture],
            ),
            tool_success_rate=0.0,
            completed=False,
            passed=False,
            reasons=[reason],
        )


def _event_output_hash(event: ToolEvent, *, max_bytes: int) -> str:
    payload = {
        "output_summary": event.output_summary,
        "detail": event.detail,
        "success": event.success,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        encoded = encoded[:max_bytes]
    return hashlib.sha256(encoded).hexdigest()


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _sequence_similarity(left: list[str], right: list[str]) -> float:
    if not left and not right:
        return 1.0
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
    distance = previous[-1] / max(len(left), len(right), 1)
    return round(1.0 - distance, 6)


__all__ = ["FrozenCapabilitySkillSandbox"]
