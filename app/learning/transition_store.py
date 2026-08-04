from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

from app.domain.models import SkillTransitionEvent


class SkillTransitionStoreError(RuntimeError):
    pass


class JsonSkillTransitionRepository:
    _FORMAT_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def save(self, transition: SkillTransitionEvent) -> SkillTransitionEvent:
        async with self._lock:
            records = self._read_all()
            existing = records.get(str(transition.transition_id))
            if existing is not None and _semantic_payload(existing) != _semantic_payload(
                transition
            ):
                raise ValueError(
                    f"Skill transition {transition.transition_id} has different content"
                )
            if existing is None:
                records[str(transition.transition_id)] = transition
                self._write_all(records)
                return transition
            return existing

    async def list_for_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillTransitionEvent]:
        return [
            item
            for item in await self.list_all()
            if item.skill_id == skill_id
            and item.tenant_id == tenant_id
            and item.project_id == project_id
            and (skill_version is None or item.skill_version == skill_version)
        ]

    async def list_all(self) -> Sequence[SkillTransitionEvent]:
        async with self._lock:
            records = list(self._read_all().values())
        return sorted(
            records,
            key=lambda item: (item.decided_at, str(item.transition_id)),
        )

    def _read_all(self) -> dict[str, SkillTransitionEvent]:
        if not self._path.exists():
            return {}
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            raw_records = document["records"]
            if not isinstance(raw_records, list):
                raise TypeError("records must be a list")
            records = [
                SkillTransitionEvent.model_validate(item)
                for item in raw_records
            ]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SkillTransitionStoreError(
                f"Invalid skill transition store at {self._path}"
            ) from exc
        return {str(item.transition_id): item for item in records}

    def _write_all(self, records: dict[str, SkillTransitionEvent]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(
            records.values(),
            key=lambda item: (item.decided_at, str(item.transition_id)),
        )
        document = {
            "version": self._FORMAT_VERSION,
            "records": [item.model_dump(mode="json") for item in ordered],
        }
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)


def _semantic_payload(transition: SkillTransitionEvent) -> dict[str, object]:
    return transition.model_dump(mode="json", exclude={"decided_at"})


__all__ = [
    "JsonSkillTransitionRepository",
    "SkillTransitionStoreError",
]
