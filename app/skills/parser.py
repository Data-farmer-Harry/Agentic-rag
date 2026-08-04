from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.nodes import MappingNode, Node, SequenceNode

from app.domain.models import SkillDefinition


class SkillMarkdownError(ValueError):
    """Raised when a SKILL.md document is unsafe or does not match the schema."""


_ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
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
}
_KNOWN_FIELDS = {
    "allowed_capabilities",
    "constraints",
    "created_at",
    "description",
    "evaluation",
    "name",
    "parent_version",
    "preconditions",
    "promotion",
    "skill_id",
    "source_run_ids",
    "status",
    "steps",
    "tenant_id",
    "project_id",
    "trigger",
    "trigger_intents",
    "trigger_phrases",
    "version",
}


def _front_matter(document: str) -> str:
    normalized = document.lstrip("\ufeff")
    lines = normalized.splitlines()
    if not lines or lines[0].strip() != "---":
        return normalized
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in {"---", "..."}:
            return "\n".join(lines[1:index])
    raise SkillMarkdownError("SKILL.md front matter is not terminated")


def _reject_duplicate_keys(node: Node | None) -> None:
    if node is None:
        return
    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = str(getattr(key_node, "value", ""))
            if key in seen:
                raise SkillMarkdownError(f"Duplicate YAML key: {key}")
            seen.add(key)
            _reject_duplicate_keys(value_node)
    elif isinstance(node, SequenceNode):
        for item in node.value:
            _reject_duplicate_keys(item)


def _validate_action(action: Any) -> str:
    if not isinstance(action, str) or not _ACTION_PATTERN.fullmatch(action):
        raise SkillMarkdownError(f"Invalid declarative action: {action!r}")
    parts = set(re.split(r"[_.-]", action.casefold()))
    if parts & _BLOCKED_ACTION_PARTS:
        raise SkillMarkdownError(f"Executable action is forbidden: {action}")
    return action


def _normalize_step(raw_step: Any) -> dict[str, Any]:
    if not isinstance(raw_step, Mapping):
        raise SkillMarkdownError("Each skill step must be a YAML mapping")
    step = dict(raw_step)
    action = _validate_action(step.pop("action", None))
    purpose_value = step.pop("purpose", None)
    purpose = str(purpose_value).strip() if purpose_value is not None else f"Run {action}"
    if not purpose:
        raise SkillMarkdownError("Skill step purpose cannot be empty")

    raw_inputs = step.pop("inputs", {})
    if raw_inputs is None:
        inputs: dict[str, Any] = {}
    elif isinstance(raw_inputs, Mapping):
        inputs = dict(raw_inputs)
    else:
        raise SkillMarkdownError("Skill step inputs must be a mapping")
    inputs.update(step)
    return {"action": action, "purpose": purpose, "inputs": inputs}


class SkillMarkdownCodec:
    """Safe YAML codec for declarative, non-executable SKILL.md assets."""

    def __init__(self, *, max_document_chars: int = 100_000) -> None:
        if max_document_chars < 1_000:
            raise ValueError("max_document_chars must be at least 1000")
        self._max_document_chars = max_document_chars

    def loads(self, document: str) -> SkillDefinition:
        if len(document) > self._max_document_chars:
            raise SkillMarkdownError("SKILL.md exceeds the configured size limit")
        payload = _front_matter(document)
        try:
            node = yaml.compose(payload, Loader=yaml.SafeLoader)
            _reject_duplicate_keys(node)
            loaded = yaml.safe_load(payload)
        except yaml.YAMLError as exc:
            raise SkillMarkdownError("Invalid SKILL.md YAML") from exc
        if not isinstance(loaded, Mapping):
            raise SkillMarkdownError("SKILL.md YAML root must be a mapping")
        data = dict(loaded)
        unknown = sorted(set(data) - _KNOWN_FIELDS)
        if unknown:
            raise SkillMarkdownError(f"Unknown SKILL.md fields: {', '.join(map(str, unknown))}")

        trigger = data.pop("trigger", {}) or {}
        if not isinstance(trigger, Mapping):
            raise SkillMarkdownError("trigger must be a mapping")
        unknown_trigger = sorted(set(trigger) - {"entities", "intents", "phrases"})
        if unknown_trigger:
            raise SkillMarkdownError(
                f"Unknown trigger fields: {', '.join(map(str, unknown_trigger))}"
            )

        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            raise SkillMarkdownError("steps must be a non-empty list")
        data["steps"] = [_normalize_step(step) for step in raw_steps]
        data["trigger_intents"] = data.pop("trigger_intents", trigger.get("intents", []))
        data["trigger_phrases"] = data.pop("trigger_phrases", trigger.get("phrases", []))

        constraints = data.pop("constraints", {}) or {}
        if not isinstance(constraints, Mapping):
            raise SkillMarkdownError("constraints must be a mapping")
        normalized_constraints = dict(constraints)
        for field in ("preconditions", "evaluation", "promotion"):
            value = data.pop(field, None)
            if value is not None:
                normalized_constraints[field] = value
        if trigger.get("entities"):
            normalized_constraints["trigger_entities"] = trigger["entities"]
        data["constraints"] = normalized_constraints

        capabilities = data.get("allowed_capabilities")
        if capabilities is None:
            data["allowed_capabilities"] = sorted({step["action"] for step in data["steps"]})
        elif not isinstance(capabilities, list) or not all(
            isinstance(item, str) and _ACTION_PATTERN.fullmatch(item) for item in capabilities
        ):
            raise SkillMarkdownError("allowed_capabilities must contain declarative names")

        try:
            return SkillDefinition.model_validate(data)
        except ValidationError as exc:
            raise SkillMarkdownError(f"SKILL.md schema validation failed: {exc}") from exc

    def dumps(self, skill: SkillDefinition) -> str:
        document: dict[str, Any] = {
            "skill_id": str(skill.skill_id),
            "tenant_id": skill.tenant_id,
            "project_id": skill.project_id,
            "name": skill.name,
            "version": skill.version,
            "description": skill.description,
            "status": skill.status.value,
            "trigger": {
                "intents": skill.trigger_intents,
                "phrases": skill.trigger_phrases,
            },
            "steps": [
                {
                    "action": step.action,
                    "purpose": step.purpose,
                    "inputs": step.inputs,
                }
                for step in skill.steps
            ],
            "allowed_capabilities": skill.allowed_capabilities,
            "constraints": skill.constraints,
            "source_run_ids": [str(run_id) for run_id in skill.source_run_ids],
            "created_at": skill.created_at.isoformat(),
        }
        if skill.parent_version is not None:
            document["parent_version"] = skill.parent_version
        payload = yaml.safe_dump(
            document,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=100,
        ).rstrip()
        return f"---\n{payload}\n---\n\n# {skill.name}\n\n{skill.description}\n"


_DEFAULT_CODEC = SkillMarkdownCodec()


def parse_skill_markdown(document: str) -> SkillDefinition:
    return _DEFAULT_CODEC.loads(document)


def serialize_skill_markdown(skill: SkillDefinition) -> str:
    return _DEFAULT_CODEC.dumps(skill)
