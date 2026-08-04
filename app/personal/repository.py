from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from app.domain.models import utc_now
from app.personal.models import PersonalEvent, PersonalRecordEnvelope, PersonalRecordType


class PersonalRepositoryError(RuntimeError):
    pass


class PersonalVersionConflict(PersonalRepositoryError):
    pass


class PersonalRepository(Protocol):
    async def save(
        self,
        envelope: PersonalRecordEnvelope,
        *,
        expected_version: int | None,
        event_type: str,
        actor_id: str,
    ) -> PersonalRecordEnvelope: ...

    async def get(
        self,
        record_type: PersonalRecordType,
        record_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> PersonalRecordEnvelope | None: ...

    async def get_by_key(
        self,
        record_type: PersonalRecordType,
        record_key: str,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> PersonalRecordEnvelope | None: ...

    async def list_records(
        self,
        record_type: PersonalRecordType,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
        parent_id: UUID | None = None,
        status: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 500,
    ) -> Sequence[PersonalRecordEnvelope]: ...


class JsonPersonalRepository:
    _FORMAT_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def save(
        self,
        envelope: PersonalRecordEnvelope,
        *,
        expected_version: int | None,
        event_type: str,
        actor_id: str,
    ) -> PersonalRecordEnvelope:
        async with self._lock:
            records, events = self._read()
            key = self._identity(envelope.record_type, envelope.record_id)
            existing = records.get(key)
            if expected_version is not None:
                actual = existing.version if existing is not None else 0
                if actual != expected_version:
                    raise PersonalVersionConflict(
                        f"Expected version {expected_version}, found {actual}"
                    )
            if existing is not None and envelope.version != existing.version + 1:
                raise PersonalVersionConflict("The next record version must increment by one")
            if existing is None and envelope.version != 1:
                raise PersonalVersionConflict("A new personal record must start at version 1")
            if envelope.record_key is not None:
                duplicate = next(
                    (
                        item
                        for item in records.values()
                        if item.record_type == envelope.record_type
                        and item.tenant_id == envelope.tenant_id
                        and item.project_id == envelope.project_id
                        and item.user_id == envelope.user_id
                        and item.record_key == envelope.record_key
                        and item.record_id != envelope.record_id
                    ),
                    None,
                )
                if duplicate is not None:
                    raise PersonalRepositoryError("Personal record key already exists")
            records[key] = envelope
            events.append(
                PersonalEvent(
                    record_type=envelope.record_type,
                    record_id=envelope.record_id,
                    tenant_id=envelope.tenant_id,
                    project_id=envelope.project_id,
                    user_id=envelope.user_id,
                    event_type=event_type,
                    version=envelope.version,
                    actor_id=actor_id,
                    payload=envelope.payload,
                )
            )
            self._write(records, events)
            return envelope

    async def get(
        self,
        record_type: PersonalRecordType,
        record_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> PersonalRecordEnvelope | None:
        async with self._lock:
            records, _ = self._read()
        item = records.get(self._identity(record_type, record_id))
        if item is None or not self._in_scope(item, tenant_id, project_id, user_id):
            return None
        return item

    async def get_by_key(
        self,
        record_type: PersonalRecordType,
        record_key: str,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> PersonalRecordEnvelope | None:
        async with self._lock:
            records, _ = self._read()
        return next(
            (
                item
                for item in records.values()
                if item.record_type == record_type
                and item.record_key == record_key
                and self._in_scope(item, tenant_id, project_id, user_id)
            ),
            None,
        )

    async def list_records(
        self,
        record_type: PersonalRecordType,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
        parent_id: UUID | None = None,
        status: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 500,
    ) -> Sequence[PersonalRecordEnvelope]:
        if not 1 <= limit <= 2_000:
            raise ValueError("limit must be between 1 and 2000")
        async with self._lock:
            records, _ = self._read()
        selected = [
            item
            for item in records.values()
            if item.record_type == record_type
            and self._in_scope(item, tenant_id, project_id, user_id)
            and (parent_id is None or item.parent_id == parent_id)
            and (status is None or item.status == status)
            and (
                date_from is None
                or (item.record_date is not None and item.record_date >= date_from)
            )
            and (date_to is None or (item.record_date is not None and item.record_date <= date_to))
        ]
        selected.sort(key=lambda item: (item.updated_at, str(item.record_id)), reverse=True)
        return selected[:limit]

    @staticmethod
    def _identity(record_type: PersonalRecordType, record_id: UUID) -> str:
        return f"{record_type.value}:{record_id}"

    @staticmethod
    def _in_scope(
        item: PersonalRecordEnvelope,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> bool:
        return (
            item.tenant_id == tenant_id
            and item.project_id == project_id
            and item.user_id == user_id
        )

    def _read(self) -> tuple[dict[str, PersonalRecordEnvelope], list[PersonalEvent]]:
        if not self._path.exists():
            return {}, []
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            records = [
                PersonalRecordEnvelope.model_validate(item)
                for item in document.get("records", [])
            ]
            events = [PersonalEvent.model_validate(item) for item in document.get("events", [])]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PersonalRepositoryError(f"Invalid personal store at {self._path}") from exc
        return (
            {self._identity(item.record_type, item.record_id): item for item in records},
            events,
        )

    def _write(
        self,
        records: dict[str, PersonalRecordEnvelope],
        events: list[PersonalEvent],
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": self._FORMAT_VERSION,
            "records": [
                item.model_dump(mode="json", exclude_none=True)
                for item in sorted(
                    records.values(),
                    key=lambda value: (
                        value.tenant_id,
                        value.project_id,
                        value.user_id,
                        value.record_type.value,
                        str(value.record_id),
                    ),
                )
            ],
            "events": [
                item.model_dump(mode="json", exclude_none=True)
                for item in events[-20_000:]
            ],
            "updated_at": utc_now().isoformat(),
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
