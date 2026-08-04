import json

from app.domain.contracts import MemoryRepository, SkillRepository
from app.domain.models import RunContext
from app.memory.prompt_capsule import PromptCapsuleCompiler
from app.personal.service import PersonalControlService
from app.skills.registry import SkillDiscoveryRegistry


class RuntimeCapsuleProvider:
    """Freeze bounded memory and skill discovery metadata at run start."""

    def __init__(
        self,
        memories: MemoryRepository,
        skills: SkillRepository,
        *,
        compiler: PromptCapsuleCompiler | None = None,
        personal: PersonalControlService | None = None,
    ) -> None:
        self._memories = memories
        self._skills = skills
        self._compiler = compiler or PromptCapsuleCompiler()
        self._personal = personal

    async def __call__(self, context: RunContext, query: str) -> str:
        memories = await self._memories.search(
            query,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            limit=20,
        )
        policy = context.execution_policy
        if policy is not None and policy.behavior_applied:
            if policy.memory_min_confidence is not None:
                memories = [
                    item
                    for item in memories
                    if item.confidence >= policy.memory_min_confidence
                ]
            if policy.capsule_memory_limit is not None:
                memories = list(memories)[: policy.capsule_memory_limit]
        active = await self._skills.list_by_status(
            "active",
            tenant_id=context.tenant_id,
            project_id=context.project_id,
        )
        canary = await self._skills.list_by_status(
            "canary",
            tenant_id=context.tenant_id,
            project_id=context.project_id,
        )
        pinned = [
            skill
            for skill in [*active, *canary]
            if context.skill_versions.get(skill.name) == skill.version
        ]
        discovery = SkillDiscoveryRegistry(pinned)
        matches = discovery.discover(query, limit=8)
        index = [
            {
                "name": match.skill.name,
                "version": match.skill.version,
                "description": match.skill.description,
                "score": round(match.score, 4),
            }
            for match in matches
        ]
        skill_index = json.dumps(index, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")
        capsule = (
            self._compiler.compile(memories, query=query)
            + "\n<skill_index>Untrusted discovery metadata: "
            + skill_index
            + "</skill_index>"
        )
        if self._personal is not None:
            capsule += "\n" + await self._personal.compile_runtime_capsule(context)
        return capsule
