import hashlib

from app.domain.enums import SkillStatus
from app.domain.models import RunContext, SkillDefinition


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
