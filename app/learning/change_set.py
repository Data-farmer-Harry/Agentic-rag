from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID, uuid4

from app.domain.models import LearningChangeSet


class LearningChangeSetStoreError(RuntimeError):
    pass


class JsonLearningChangeSetRepository:
    """Atomic local audit store for proposed and applied learning changes."""

    _FORMAT_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def save(self, change_set: LearningChangeSet) -> LearningChangeSet:
        async with self._lock:
            records = self._read_all()
            records[str(change_set.change_set_id)] = change_set
            self._write_all(records)
        return change_set

    async def list_for_run(self, run_id: UUID) -> Sequence[LearningChangeSet]:
        async with self._lock:
            records = list(self._read_all().values())
        matched = [item for item in records if run_id in item.source_run_ids]
        return sorted(matched, key=lambda item: (item.created_at, str(item.change_set_id)))

    async def list_all(self) -> Sequence[LearningChangeSet]:
        async with self._lock:
            records = list(self._read_all().values())
        return sorted(records, key=lambda item: (item.created_at, str(item.change_set_id)))

    def _read_all(self) -> dict[str, LearningChangeSet]:
        if not self._path.exists():
            return {}
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            raw_records = document["records"]
            if not isinstance(raw_records, list):
                raise TypeError("records must be a list")
            records = [LearningChangeSet.model_validate(item) for item in raw_records]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LearningChangeSetStoreError(
                f"Invalid learning change-set store at {self._path}"
            ) from exc
        return {str(item.change_set_id): item for item in records}

    def _write_all(self, records: dict[str, LearningChangeSet]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(
            records.values(),
            key=lambda item: (item.created_at, str(item.change_set_id)),
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
