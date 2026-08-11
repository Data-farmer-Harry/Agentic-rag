from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.domain.enums import SkillStatus
from app.domain.models import RunContext, SkillDefinition, SkillStep


class SkillActivationError(ValueError):
    pass


class SkillExecutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SkillMatch:
    skill: SkillDefinition
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActivatedSkill:
    skill: SkillDefinition
    available_capabilities: frozenset[str]
    offline_replay: bool = False


@dataclass(frozen=True, slots=True)
class SkillStepResult:
    action: str
    output: Any


@dataclass(frozen=True, slots=True)
class SkillExecutionResult:
    skill_id: str
    version: str
    steps: tuple[SkillStepResult, ...]

    @property
    def final_output(self) -> Any:
        return self.steps[-1].output if self.steps else None


SkillActionHandler = Callable[
    [Mapping[str, Any], Mapping[str, Any]],
    Any | Awaitable[Any],
]


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[\w-]+", text, flags=re.UNICODE)}


def _validate_registry_action(action: str) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", action):
        raise ValueError(f"Invalid action name: {action}")
    parts = set(re.split(r"[_.-]", action.casefold()))
    if parts & {
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
    }:
        raise ValueError(f"Executable action cannot be registered: {action}")


def skill_is_eligible(
    skill: SkillDefinition,
    context: RunContext,
    *,
    canary_percent: int,
) -> bool:
    """Choose canary traffic deterministically so a run is replayable."""
    if not 0 <= canary_percent <= 100:
        raise ValueError("canary_percent must be between 0 and 100")
    if skill.status == SkillStatus.ACTIVE:
        return True
    if skill.status != SkillStatus.CANARY:
        return False
    bucket = int(
        hashlib.sha256(f"{context.run_id}:{skill.skill_id}".encode()).hexdigest()[:8],
        16,
    ) % 100
    return bucket < canary_percent


class SkillDiscoveryRegistry:
    def __init__(self, skills: Sequence[SkillDefinition] = ()) -> None:
        self._skills: dict[tuple[str, str], SkillDefinition] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: SkillDefinition) -> None:
        key = (skill.name, skill.version)
        existing = self._skills.get(key)
        if existing is not None and existing.skill_id != skill.skill_id:
            raise ValueError(f"Duplicate skill version: {skill.name}@{skill.version}")
        self._skills[key] = skill

    def unregister(self, name: str, version: str) -> bool:
        return self._skills.pop((name, version), None) is not None

    def discover(
        self,
        query: str,
        *,
        intent: str | None = None,
        statuses: set[SkillStatus] | None = None,
        limit: int = 10,
    ) -> tuple[SkillMatch, ...]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        query_text = query.casefold()
        query_tokens = _tokens(query)
        matches: list[SkillMatch] = []
        for skill in self._skills.values():
            if statuses is not None and skill.status not in statuses:
                continue
            score = 0.0
            reasons: list[str] = []
            if intent is not None and intent.casefold() in {
                item.casefold() for item in skill.trigger_intents
            }:
                score += 3.0
                reasons.append("intent")
            phrase_hits = [
                phrase for phrase in skill.trigger_phrases if phrase.casefold() in query_text
            ]
            if phrase_hits:
                score += min(4.0, 2.0 * len(phrase_hits))
                reasons.append("phrase")
            skill_tokens = _tokens(
                f"{skill.name.replace('_', ' ')} {skill.description} "
                + " ".join(skill.trigger_intents)
            )
            if query_tokens:
                overlap = len(query_tokens & skill_tokens) / len(query_tokens)
                if overlap:
                    score += overlap
                    reasons.append("tokens")
            if score > 0.0 or (not query.strip() and intent is None):
                matches.append(SkillMatch(skill=skill, score=score, reasons=tuple(reasons)))
        matches.sort(
            key=lambda match: (
                match.score,
                self._version_key(match.skill.version),
                match.skill.name,
            ),
            reverse=True,
        )
        return tuple(matches[:limit])

    @staticmethod
    def _version_key(version: str) -> tuple[int, int, int]:
        major, minor, patch = version.split(".")
        return (int(major), int(minor), int(patch))


class SkillActivationRegistry:
    _RUNTIME_STATUSES = {SkillStatus.CANARY, SkillStatus.ACTIVE}
    _REPLAY_STATUSES = {SkillStatus.OFFLINE_PASS, SkillStatus.SHADOW}

    def activate(
        self,
        skill: SkillDefinition,
        available_capabilities: Sequence[str] | set[str] | frozenset[str],
        *,
        offline_replay: bool = False,
    ) -> ActivatedSkill:
        eligible = set(self._RUNTIME_STATUSES)
        if offline_replay:
            eligible.update(self._REPLAY_STATUSES)
        if skill.status not in eligible:
            raise SkillActivationError(
                f"Skill status {skill.status.value} is not eligible for activation"
            )
        available = frozenset(available_capabilities)
        missing = sorted(set(skill.allowed_capabilities) - available)
        if missing:
            raise SkillActivationError("Missing capabilities: " + ", ".join(missing))
        return ActivatedSkill(
            skill=skill,
            available_capabilities=available,
            offline_replay=offline_replay,
        )


@dataclass(frozen=True, slots=True)
class _RegisteredAction:
    handler: SkillActionHandler
    required_capability: str


class SkillExecutionRegistry:
    def __init__(self) -> None:
        self._actions: dict[str, _RegisteredAction] = {}

    def register_action(
        self,
        action: str,
        handler: SkillActionHandler,
        *,
        required_capability: str | None = None,
    ) -> None:
        _validate_registry_action(action)
        if not callable(handler):
            raise TypeError("handler must be callable")
        capability = required_capability or action
        _validate_registry_action(capability)
        self._actions[action] = _RegisteredAction(handler, capability)

    async def execute(
        self,
        activated: ActivatedSkill,
        *,
        initial_state: Mapping[str, Any] | None = None,
    ) -> SkillExecutionResult:
        skill = activated.skill
        configured_limit = skill.constraints.get("max_tool_calls", len(skill.steps))
        if not isinstance(configured_limit, int) or isinstance(configured_limit, bool):
            raise SkillExecutionError("max_tool_calls must be an integer")
        if configured_limit < len(skill.steps):
            raise SkillExecutionError("Skill exceeds its max_tool_calls constraint")

        state: dict[str, Any] = dict(initial_state or {})
        results: list[SkillStepResult] = []
        for index, step in enumerate(skill.steps):
            output = await self._execute_step(step, activated, state)
            self._ensure_json_value(output)
            result = SkillStepResult(action=step.action, output=output)
            results.append(result)
            state["step_index"] = index
            state["previous_output"] = output
            state["outputs"] = tuple(item.output for item in results)
        return SkillExecutionResult(
            skill_id=str(skill.skill_id),
            version=skill.version,
            steps=tuple(results),
        )

    async def _execute_step(
        self,
        step: SkillStep,
        activated: ActivatedSkill,
        state: dict[str, Any],
    ) -> Any:
        registered = self._actions.get(step.action)
        if registered is None:
            raise SkillExecutionError(f"Action is not registered: {step.action}")
        if registered.required_capability not in activated.available_capabilities:
            raise SkillExecutionError(f"Action lacks capability: {registered.required_capability}")
        if (
            activated.skill.allowed_capabilities
            and registered.required_capability not in activated.skill.allowed_capabilities
        ):
            raise SkillExecutionError(
                f"Action is outside the skill capability declaration: {step.action}"
            )
        try:
            value = registered.handler(dict(step.inputs), MappingProxyType(state))
            return await value if inspect.isawaitable(value) else value
        except SkillExecutionError:
            raise
        except Exception as exc:
            raise SkillExecutionError(f"Action failed: {step.action}") from exc

    @staticmethod
    def _ensure_json_value(value: Any) -> None:
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise SkillExecutionError("Action output must be JSON serializable") from exc


class SkillRegistry:
    """Facade preserving discovery, activation, and execution as separate gates."""

    def __init__(
        self,
        discovery: SkillDiscoveryRegistry | None = None,
        activation: SkillActivationRegistry | None = None,
        execution: SkillExecutionRegistry | None = None,
    ) -> None:
        self.discovery = discovery or SkillDiscoveryRegistry()
        self.activation = activation or SkillActivationRegistry()
        self.execution = execution or SkillExecutionRegistry()

    async def run(
        self,
        query: str,
        *,
        available_capabilities: Sequence[str] | set[str] | frozenset[str],
        intent: str | None = None,
        initial_state: Mapping[str, Any] | None = None,
    ) -> SkillExecutionResult | None:
        matches = self.discovery.discover(
            query,
            intent=intent,
            statuses={SkillStatus.CANARY, SkillStatus.ACTIVE},
            limit=1,
        )
        if not matches:
            return None
        activated = self.activation.activate(matches[0].skill, available_capabilities)
        return await self.execution.execute(activated, initial_state=initial_state)

    async def replay(
        self,
        query: str,
        *,
        available_capabilities: Sequence[str] | set[str] | frozenset[str],
        intent: str | None = None,
        initial_state: Mapping[str, Any] | None = None,
    ) -> SkillExecutionResult | None:
        matches = self.discovery.discover(
            query,
            intent=intent,
            statuses={SkillStatus.OFFLINE_PASS, SkillStatus.SHADOW},
            limit=1,
        )
        if not matches:
            return None
        activated = self.activation.activate(
            matches[0].skill,
            available_capabilities,
            offline_replay=True,
        )
        return await self.execution.execute(activated, initial_state=initial_state)
