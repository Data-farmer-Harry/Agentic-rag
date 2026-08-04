"""Declarative skill parsing, persistence, discovery, and safe execution."""

from app.skills.parser import (
    SkillMarkdownCodec,
    SkillMarkdownError,
    parse_skill_markdown,
    serialize_skill_markdown,
)
from app.skills.registry import (
    ActivatedSkill,
    SkillActivationError,
    SkillActivationRegistry,
    SkillDiscoveryRegistry,
    SkillExecutionError,
    SkillExecutionRegistry,
    SkillExecutionResult,
    SkillMatch,
    SkillRegistry,
)
from app.skills.repository import SkillMarkdownRepository
from app.skills.rollout import skill_is_eligible

__all__ = [
    "ActivatedSkill",
    "SkillActivationError",
    "SkillActivationRegistry",
    "SkillDiscoveryRegistry",
    "SkillExecutionError",
    "SkillExecutionRegistry",
    "SkillExecutionResult",
    "SkillMarkdownCodec",
    "SkillMarkdownError",
    "SkillMarkdownRepository",
    "skill_is_eligible",
    "SkillMatch",
    "SkillRegistry",
    "parse_skill_markdown",
    "serialize_skill_markdown",
]
