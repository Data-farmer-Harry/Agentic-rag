from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.domain.models import MemoryCandidate, MemoryRecord, utc_now


class MemoryStoreError(RuntimeError):
    """Raised when the on-disk memory document is malformed."""


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[\w-]+", text, flags=re.UNICODE)}


def _is_expired(record: MemoryRecord, now: datetime) -> bool:
    expires_at = record.expires_at
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= now


class JsonMemoryStore:
    """A small, atomic JSON implementation of ``MemoryRepository``.

    Records are keyed by tenant, project, user, memory type, and logical key.
    Upserting that tuple retains identity and creation time, which makes retries idempotent.
    """

    _FORMAT_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def upsert(self, candidate: MemoryCandidate) -> MemoryRecord:
        async with self._lock:
            records = self._read_all()
            existing = next(
                (
                    record
                    for record in records.values()
                    if record.tenant_id == candidate.tenant_id
                    and record.project_id == candidate.project_id
                    and record.user_id == candidate.user_id
                    and record.memory_type == candidate.memory_type
                    and record.key == candidate.key
                ),
                None,
            )
            now = utc_now()
            if existing is not None and existing.revoked_at is not None:
                return existing
            if existing is not None and _candidate_matches(existing, candidate):
                return existing
            values = candidate.model_dump()
            if existing is None:
                record = MemoryRecord.model_validate(
                    {
                        **values,
                        "created_at": now,
                        "updated_at": now,
                    }
                )
            else:
                record = MemoryRecord.model_validate(
                    {
                        **values,
                        "memory_id": existing.memory_id,
                        "created_at": existing.created_at,
                        "updated_at": now,
                        "revoked_at": existing.revoked_at,
                    }
                )
            records[str(record.memory_id)] = record
            self._write_all(records)
            return record

    async def search(
        self,
        query: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = "local-user",
        limit: int = 10,
    ) -> Sequence[MemoryRecord]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0:
            return []

        async with self._lock:
            records = list(self._read_all().values())
        now = datetime.now(UTC)
        active = [
            record
            for record in records
            if record.tenant_id == tenant_id
            and record.project_id == project_id
            and record.user_id in {None, user_id}
            and record.revoked_at is None
            and not _is_expired(record, now)
        ]
        query_tokens = _tokens(query)

        def rank(record: MemoryRecord) -> tuple[float, float, datetime, str]:
            searchable = " ".join(
                [record.key, record.summary, json.dumps(record.detail, ensure_ascii=False)]
            )
            record_tokens = _tokens(searchable)
            overlap = len(query_tokens & record_tokens) / len(query_tokens) if query_tokens else 0.0
            exact = 1.0 if query and query.casefold() in searchable.casefold() else 0.0
            return (overlap, exact, record.updated_at, str(record.memory_id))

        if query_tokens or query.strip():
            active = [record for record in active if rank(record)[0] > 0.0 or rank(record)[1] > 0.0]
        active.sort(key=rank, reverse=True)
        return active[:limit]

    async def revoke(
        self,
        memory_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = "local-user",
    ) -> bool:
        async with self._lock:
            records = self._read_all()
            record = records.get(str(memory_id))
            if (
                record is None
                or record.tenant_id != tenant_id
                or record.project_id != project_id
                or record.user_id not in {None, user_id}
                or record.revoked_at is not None
            ):
                return False
            records[str(memory_id)] = record.model_copy(
                update={"revoked_at": utc_now(), "updated_at": utc_now()}
            )
            self._write_all(records)
            return True

    async def list_all(self, *, include_revoked: bool = False) -> Sequence[MemoryRecord]:
        async with self._lock:
            records = list(self._read_all().values())
        if not include_revoked:
            records = [record for record in records if record.revoked_at is None]
        records.sort(key=lambda record: (record.created_at, str(record.memory_id)))
        return records

    async def list_scoped(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str | None = "local-user",
        include_revoked: bool = False,
    ) -> Sequence[MemoryRecord]:
        records = [
            record
            for record in await self.list_all(include_revoked=include_revoked)
            if record.tenant_id == tenant_id
            and record.project_id == project_id
            and record.user_id in {None, user_id}
        ]
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def _read_all(self) -> dict[str, MemoryRecord]:
        if not self._path.exists():
            return {}
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
            raw_records = document if isinstance(document, list) else document["records"]
            if not isinstance(raw_records, list):
                raise TypeError("records must be a list")
            records = [MemoryRecord.model_validate(item) for item in raw_records]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MemoryStoreError(f"Invalid memory store at {self._path}") from exc
        return {str(record.memory_id): record for record in records}

    def _write_all(self, records: dict[str, MemoryRecord]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(
            records.values(),
            key=lambda record: (
                record.tenant_id,
                record.project_id,
                record.user_id or "",
                record.memory_type.value,
                record.key,
                str(record.memory_id),
            ),
        )
        document = {
            "version": self._FORMAT_VERSION,
            "records": [record.model_dump(mode="json", exclude_none=True) for record in ordered],
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


def _candidate_matches(
    existing: MemoryRecord,
    candidate: MemoryCandidate,
) -> bool:
    values = {
        field: getattr(existing, field)
        for field in MemoryCandidate.model_fields
    }
    return MemoryCandidate.model_validate(values) == candidate
