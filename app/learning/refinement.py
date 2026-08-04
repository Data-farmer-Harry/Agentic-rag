from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.domain.enums import SkillStatus
from app.domain.models import SkillDefinition

_REFINABLE_STATUSES = {
    SkillStatus.SHADOW,
    SkillStatus.CANARY,
    SkillStatus.ACTIVE,
    SkillStatus.DEPRECATED,
    SkillStatus.ROLLED_BACK,
}
_VOLATILE_CONSTRAINTS = {
    "average_sequence_distance",
    "mined_from_repeated_runs",
}


@dataclass(frozen=True, slots=True)
class SkillRefinementDecision:
    parent: SkillDefinition
    candidate: SkillDefinition | None
    change_level: Literal["none", "patch", "minor", "major"]
    reasons: tuple[str, ...]
    semantic_diff: dict[str, object]


class SkillRefiner:
    """Creates immutable SemVer descendants from newly mined evidence."""

    revision = "skill-refiner-v1"

    def __init__(self, *, min_new_source_runs: int = 2) -> None:
        if not 1 <= min_new_source_runs <= 100:
            raise ValueError("min_new_source_runs must be between 1 and 100")
        self._min_new_source_runs = min_new_source_runs

    def refine(
        self,
        parent: SkillDefinition,
        proposed: SkillDefinition,
    ) -> SkillRefinementDecision:
        self._validate_lineage(parent, proposed)
        new_sources = sorted(
            set(proposed.source_run_ids) - set(parent.source_run_ids),
            key=str,
        )
        if parent.status not in _REFINABLE_STATUSES:
            return SkillRefinementDecision(
                parent=parent,
                candidate=None,
                change_level="none",
                reasons=("parent_not_promotion_ready_for_refinement",),
                semantic_diff={"new_source_run_count": len(new_sources)},
            )

        parent_actions = [step.action for step in parent.steps]
        proposed_actions = [step.action for step in proposed.steps]
        parent_capabilities = set(parent.allowed_capabilities)
        proposed_capabilities = set(proposed.allowed_capabilities)
        parent_triggers = set(parent.trigger_intents) | set(parent.trigger_phrases)
        proposed_triggers = set(proposed.trigger_intents) | set(proposed.trigger_phrases)
        parent_behavior = _behavior_payload(parent)
        proposed_behavior = _behavior_payload(proposed)
        behavior_changed = parent_behavior != proposed_behavior
        if not behavior_changed and len(new_sources) < self._min_new_source_runs:
            return SkillRefinementDecision(
                parent=parent,
                candidate=None,
                change_level="none",
                reasons=(
                    f"insufficient_new_refinement_evidence:{len(new_sources)}/"
                    f"{self._min_new_source_runs}",
                ),
                semantic_diff={"new_source_run_count": len(new_sources)},
            )

        removed_actions = sorted(set(parent_actions) - set(proposed_actions))
        removed_capabilities = sorted(parent_capabilities - proposed_capabilities)
        removed_triggers = sorted(parent_triggers - proposed_triggers)
        if removed_actions or removed_capabilities or removed_triggers:
            level: Literal["patch", "minor", "major"] = "major"
            reasons = ("breaking_skill_behavior_change",)
        elif behavior_changed:
            level = "minor"
            reasons = ("compatible_skill_behavior_change",)
        else:
            level = "patch"
            reasons = ("expanded_refinement_evidence",)
        version = _next_version(parent.version, level)
        candidate = proposed.model_copy(
            update={
                "skill_id": parent.skill_id,
                "version": version,
                "status": SkillStatus.DRAFT,
                "parent_version": parent.version,
                "source_run_ids": sorted(
                    set(parent.source_run_ids) | set(proposed.source_run_ids),
                    key=str,
                ),
            }
        )
        return SkillRefinementDecision(
            parent=parent,
            candidate=candidate,
            change_level=level,
            reasons=(*reasons, "refinement_remains_draft"),
            semantic_diff={
                "from_version": parent.version,
                "to_version": version,
                "parent_actions": parent_actions,
                "candidate_actions": proposed_actions,
                "added_actions": sorted(set(proposed_actions) - set(parent_actions)),
                "added_capabilities": sorted(
                    proposed_capabilities - parent_capabilities
                ),
                "removed_capabilities": removed_capabilities,
                "removed_actions": removed_actions,
                "added_triggers": sorted(proposed_triggers - parent_triggers),
                "removed_triggers": removed_triggers,
                "description_changed": parent.description != proposed.description,
                "constraints_changed": (
                    _stable_constraints(parent) != _stable_constraints(proposed)
                ),
                "new_source_run_count": len(new_sources),
                "refiner_revision": self.revision,
            },
        )

    @staticmethod
    def _validate_lineage(
        parent: SkillDefinition,
        proposed: SkillDefinition,
    ) -> None:
        if (
            parent.tenant_id != proposed.tenant_id
            or parent.project_id != proposed.project_id
            or parent.name != proposed.name
        ):
            raise ValueError("Skill refinement must preserve scope and name")
        if proposed.status != SkillStatus.DRAFT:
            raise ValueError("A proposed refinement must remain draft")


def _behavior_payload(skill: SkillDefinition) -> dict[str, object]:
    return {
        "trigger_intents": sorted(skill.trigger_intents),
        "trigger_phrases": sorted(skill.trigger_phrases),
        "steps": [step.model_dump(mode="json") for step in skill.steps],
        "allowed_capabilities": sorted(skill.allowed_capabilities),
        "constraints": _stable_constraints(skill),
    }


def _stable_constraints(skill: SkillDefinition) -> dict[str, object]:
    return {
        key: value
        for key, value in sorted(skill.constraints.items())
        if key not in _VOLATILE_CONSTRAINTS
    }


def _next_version(
    version: str,
    level: Literal["patch", "minor", "major"],
) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


__all__ = ["SkillRefinementDecision", "SkillRefiner"]
