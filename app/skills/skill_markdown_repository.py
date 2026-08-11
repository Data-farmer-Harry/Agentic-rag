from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

from app.domain.enums import SkillStatus
from app.domain.models import SkillDefinition
from app.skills.skill_markdown_codec import SkillMarkdownCodec


class SkillMarkdownRepository:
    """Filesystem skill repository using one versioned SKILL.md per asset."""

    def __init__(self, root: Path, codec: SkillMarkdownCodec | None = None) -> None:
        self._root = root
        self._codec = codec or SkillMarkdownCodec()
        self._lock = asyncio.Lock()

    async def save(self, skill: SkillDefinition) -> SkillDefinition:
        path = self._path_for(skill)
        async with self._lock:
            if path.exists():
                existing = self._codec.loads(path.read_text(encoding="utf-8"))
                if existing.skill_id != skill.skill_id:
                    raise ValueError(
                        f"Skill {skill.name}@{skill.version} already has a different identity"
                    )
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_text(self._codec.dumps(skill), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return skill

    async def list_by_status(
        self,
        status: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> Sequence[SkillDefinition]:
        requested = SkillStatus(status)
        return [
            skill
            for skill in await self.list_all()
            if skill.status == requested
            and skill.tenant_id == tenant_id
            and skill.project_id == project_id
        ]

    async def get(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        version: str | None = None,
    ) -> SkillDefinition | None:
        matches = [
            skill
            for skill in await self.list_all()
            if skill.skill_id == skill_id
            and skill.tenant_id == tenant_id
            and skill.project_id == project_id
            and (version is None or skill.version == version)
        ]
        return matches[-1] if matches else None

    async def list_all(self) -> Sequence[SkillDefinition]:
        async with self._lock:
            paths = sorted(self._root.glob("*/*/*/SKILL.md"))
            skills = [self._codec.loads(path.read_text(encoding="utf-8")) for path in paths]
        return sorted(skills, key=lambda skill: (skill.name, self._version_key(skill.version)))

    async def get_by_name(
        self,
        name: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        version: str | None = None,
    ) -> SkillDefinition | None:
        matches = [
            skill
            for skill in await self.list_all()
            if skill.name == name
            and skill.tenant_id == tenant_id
            and skill.project_id == project_id
        ]
        if version is not None:
            return next((skill for skill in matches if skill.version == version), None)
        return matches[-1] if matches else None

    def _path_for(self, skill: SkillDefinition) -> Path:
        scope = hashlib.sha256(f"{skill.tenant_id}\x00{skill.project_id}".encode()).hexdigest()[:24]
        return self._root / scope / skill.name / skill.version / "SKILL.md"

    @staticmethod
    def _version_key(version: str) -> tuple[int, int, int]:
        major, minor, patch = version.split(".")
        return (int(major), int(minor), int(patch))
