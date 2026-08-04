from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar
from uuid import UUID, uuid4

from app.domain.models import SkillEvaluation, SkillObservation, StrictModel

_RecordT = TypeVar("_RecordT", SkillEvaluation, SkillObservation)


class SkillEvaluationStoreError(RuntimeError):
    pass


class JsonSkillEvaluationRepository:
    _FORMAT_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def save(self, evaluation: SkillEvaluation) -> SkillEvaluation:
        async with self._lock:
            records = self._read_all()
            records[str(evaluation.evaluation_id)] = evaluation
            self._write_all(records)
        return evaluation

    async def list_for_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> Sequence[SkillEvaluation]:
        async with self._lock:
            records = list(self._read_all().values())
        matched = [
            item
            for item in records
            if item.skill_id == skill_id
            and item.tenant_id == tenant_id
            and item.project_id == project_id
        ]
        if skill_version is not None:
            matched = [item for item in matched if item.skill_version == skill_version]
        return sorted(
            matched,
            key=lambda item: (item.generated_at, str(item.evaluation_id)),
        )

    async def latest(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
    ) -> SkillEvaluation | None:
        records = await self.list_for_skill(
            skill_id,
            tenant_id=tenant_id,
            project_id=project_id,
            skill_version=skill_version,
        )
        return records[-1] if records else None

    async def list_all(self) -> Sequence[SkillEvaluation]:
        async with self._lock:
            records = list(self._read_all().values())
        return sorted(
            records,
            key=lambda item: (item.generated_at, str(item.evaluation_id)),
        )

    def _read_all(self) -> dict[str, SkillEvaluation]:
        return _read_records(
            self._path,
            SkillEvaluation,
            lambda item: item.evaluation_id,
            error_message="skill evaluation",
        )

    def _write_all(self, records: dict[str, SkillEvaluation]) -> None:
        ordered = sorted(
            records.values(),
            key=lambda item: (item.generated_at, str(item.evaluation_id)),
        )
        _write_records(self._path, self._FORMAT_VERSION, ordered)


class JsonSkillObservationRepository:
    _FORMAT_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def save(self, observation: SkillObservation) -> SkillObservation:
        async with self._lock:
            records = self._read_all()
            existing = records.get(str(observation.observation_id))
            if existing is not None and _semantic_payload(existing) != _semantic_payload(
                observation
            ):
                raise ValueError(
                    f"Skill observation {observation.observation_id} has different content"
                )
            if existing is not None:
                return existing
            records[str(observation.observation_id)] = observation
            self._write_all(records)
        return observation

    async def list_for_skill(
        self,
        skill_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        skill_version: str | None = None,
        cohort: str | None = None,
    ) -> Sequence[SkillObservation]:
        async with self._lock:
            records = list(self._read_all().values())
        matched = [
            item
            for item in records
            if item.skill_id == skill_id
            and item.tenant_id == tenant_id
            and item.project_id == project_id
        ]
        if skill_version is not None:
            matched = [item for item in matched if item.skill_version == skill_version]
        if cohort is not None:
            matched = [item for item in matched if item.cohort == cohort]
        return sorted(
            matched,
            key=lambda item: (item.created_at, str(item.observation_id)),
        )

    async def list_all(self) -> Sequence[SkillObservation]:
        async with self._lock:
            records = list(self._read_all().values())
        return sorted(
            records,
            key=lambda item: (item.created_at, str(item.observation_id)),
        )

    def _read_all(self) -> dict[str, SkillObservation]:
        return _read_records(
            self._path,
            SkillObservation,
            lambda item: item.observation_id,
            error_message="skill observation",
        )

    def _write_all(self, records: dict[str, SkillObservation]) -> None:
        ordered = sorted(
            records.values(),
            key=lambda item: (item.created_at, str(item.observation_id)),
        )
        _write_records(self._path, self._FORMAT_VERSION, ordered)


def _read_records(
    path: Path,
    model: type[_RecordT],
    identity: Callable[[_RecordT], UUID],
    *,
    error_message: str,
) -> dict[str, _RecordT]:
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        raw_records = document["records"]
        if not isinstance(raw_records, list):
            raise TypeError("records must be a list")
        records = [model.model_validate(item) for item in raw_records]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SkillEvaluationStoreError(f"Invalid {error_message} store at {path}") from exc
    return {str(identity(item)): item for item in records}


def _write_records(path: Path, version: int, records: Sequence[StrictModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": version,
        "records": [item.model_dump(mode="json") for item in records],
    }
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _semantic_payload(observation: SkillObservation) -> dict[str, object]:
    return observation.model_dump(mode="json", exclude={"created_at"})


__all__ = [
    "JsonSkillEvaluationRepository",
    "JsonSkillObservationRepository",
    "SkillEvaluationStoreError",
]
