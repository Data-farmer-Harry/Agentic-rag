from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, cast
from uuid import UUID

from asyncpg import Connection, Record

from app.infra.postgres import PostgresDatabase, PostgresMigration
from app.personal.models import PersonalEvent, PersonalRecordEnvelope, PersonalRecordType
from app.personal.repository import (
    PersonalRepositoryError,
    PersonalVersionConflict,
)

PERSONAL_CONTROL_MIGRATIONS = (
    PostgresMigration(
        version=11,
        name="personal_control_plane",
        statement="""
CREATE TABLE IF NOT EXISTS personal_records (
    record_type text NOT NULL CHECK (
        record_type IN (
            'task', 'plan', 'plan_step', 'checklist_item',
            'note', 'persona', 'day_archive', 'emotion_override'
        )
    ),
    record_id uuid NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    user_id text NOT NULL,
    parent_id uuid,
    record_key text,
    status text,
    record_date date,
    version integer NOT NULL CHECK (version >= 1),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (record_type, record_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_personal_records_scope_key
ON personal_records(record_type, tenant_id, project_id, user_id, record_key)
WHERE record_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_personal_records_scope_type
ON personal_records(tenant_id, project_id, user_id, record_type, updated_at DESC);

CREATE INDEX IF NOT EXISTS ix_personal_records_parent
ON personal_records(tenant_id, project_id, user_id, record_type, parent_id)
WHERE parent_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_personal_records_date
ON personal_records(tenant_id, project_id, user_id, record_type, record_date)
WHERE record_date IS NOT NULL;

CREATE TABLE IF NOT EXISTS personal_events (
    event_id uuid PRIMARY KEY,
    record_type text NOT NULL,
    record_id uuid NOT NULL,
    tenant_id text NOT NULL,
    project_id text NOT NULL,
    user_id text NOT NULL,
    event_type text NOT NULL,
    version integer NOT NULL CHECK (version >= 1),
    actor_id text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_personal_events_record
ON personal_events(record_type, record_id, version);

CREATE INDEX IF NOT EXISTS ix_personal_events_scope
ON personal_events(tenant_id, project_id, user_id, created_at DESC);
""",
    ),
    PostgresMigration(
        version=15,
        name="personal_reminder_state",
        statement="""
ALTER TABLE personal_records
DROP CONSTRAINT IF EXISTS personal_records_record_type_check;

ALTER TABLE personal_records
ADD CONSTRAINT personal_records_record_type_check CHECK (
    record_type IN (
        'task', 'plan', 'plan_step', 'checklist_item',
        'note', 'persona', 'day_archive', 'emotion_override',
        'reminder_state'
    )
);
""",
    ),
)


class PostgresPersonalRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def save(
        self,
        envelope: PersonalRecordEnvelope,
        *,
        expected_version: int | None,
        event_type: str,
        actor_id: str,
    ) -> PersonalRecordEnvelope:
        async with self._connection() as connection:
            existing = await connection.fetchrow(
                """
SELECT version
FROM personal_records
WHERE record_type = $1 AND record_id = $2
FOR UPDATE
""",
                envelope.record_type.value,
                envelope.record_id,
            )
            actual = cast(int, existing["version"]) if existing is not None else 0
            if expected_version is not None and actual != expected_version:
                raise PersonalVersionConflict(
                    f"Expected version {expected_version}, found {actual}"
                )
            if actual == 0 and envelope.version != 1:
                raise PersonalVersionConflict("A new personal record must start at version 1")
            if actual > 0 and envelope.version != actual + 1:
                raise PersonalVersionConflict("The next record version must increment by one")
            try:
                await connection.execute(
                    """
INSERT INTO personal_records (
    record_type, record_id, tenant_id, project_id, user_id,
    parent_id, record_key, status, record_date, version, payload,
    created_at, updated_at
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, $13)
ON CONFLICT (record_type, record_id) DO UPDATE SET
    parent_id = EXCLUDED.parent_id,
    record_key = EXCLUDED.record_key,
    status = EXCLUDED.status,
    record_date = EXCLUDED.record_date,
    version = EXCLUDED.version,
    payload = EXCLUDED.payload,
    updated_at = EXCLUDED.updated_at
""",
                    envelope.record_type.value,
                    envelope.record_id,
                    envelope.tenant_id,
                    envelope.project_id,
                    envelope.user_id,
                    envelope.parent_id,
                    envelope.record_key,
                    envelope.status,
                    envelope.record_date,
                    envelope.version,
                    json.dumps(envelope.payload, ensure_ascii=False, default=str),
                    envelope.created_at,
                    envelope.updated_at,
                )
            except Exception as exc:
                if "ux_personal_records_scope_key" in str(exc):
                    raise PersonalRepositoryError("Personal record key already exists") from exc
                raise
            event = PersonalEvent(
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
            await connection.execute(
                """
INSERT INTO personal_events (
    event_id, record_type, record_id, tenant_id, project_id, user_id,
    event_type, version, actor_id, payload, created_at
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
""",
                event.event_id,
                event.record_type.value,
                event.record_id,
                event.tenant_id,
                event.project_id,
                event.user_id,
                event.event_type,
                event.version,
                event.actor_id,
                json.dumps(event.payload, ensure_ascii=False, default=str),
                event.created_at,
            )
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
        row = await self._database.pool.fetchrow(
            """
SELECT *
FROM personal_records
WHERE record_type = $1 AND record_id = $2
  AND tenant_id = $3 AND project_id = $4 AND user_id = $5
""",
            record_type.value,
            record_id,
            tenant_id,
            project_id,
            user_id,
        )
        return self._from_row(row) if row is not None else None

    async def get_by_key(
        self,
        record_type: PersonalRecordType,
        record_key: str,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> PersonalRecordEnvelope | None:
        row = await self._database.pool.fetchrow(
            """
SELECT *
FROM personal_records
WHERE record_type = $1 AND record_key = $2
  AND tenant_id = $3 AND project_id = $4 AND user_id = $5
""",
            record_type.value,
            record_key,
            tenant_id,
            project_id,
            user_id,
        )
        return self._from_row(row) if row is not None else None

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
        rows = await self._database.pool.fetch(
            """
SELECT *
FROM personal_records
WHERE record_type = $1
  AND tenant_id = $2 AND project_id = $3 AND user_id = $4
  AND ($5::uuid IS NULL OR parent_id = $5)
  AND ($6::text IS NULL OR status = $6)
  AND ($7::date IS NULL OR record_date >= $7)
  AND ($8::date IS NULL OR record_date <= $8)
ORDER BY updated_at DESC, record_id DESC
LIMIT $9
""",
            record_type.value,
            tenant_id,
            project_id,
            user_id,
            parent_id,
            status,
            date_from,
            date_to,
            limit,
        )
        return [self._from_row(row) for row in rows]

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Connection]:
        async with self._database.pool.acquire() as connection, connection.transaction():
            yield connection

    @staticmethod
    def _from_row(row: Record) -> PersonalRecordEnvelope:
        raw_payload: Any = row["payload"]
        if isinstance(raw_payload, str):
            raw_payload = json.loads(raw_payload)
        if isinstance(raw_payload, dict) and set(raw_payload) == {"payload"}:
            raw_payload = raw_payload["payload"]
        return PersonalRecordEnvelope(
            record_type=PersonalRecordType(cast(str, row["record_type"])),
            record_id=cast(UUID, row["record_id"]),
            tenant_id=cast(str, row["tenant_id"]),
            project_id=cast(str, row["project_id"]),
            user_id=cast(str, row["user_id"]),
            parent_id=cast(UUID | None, row["parent_id"]),
            record_key=cast(str | None, row["record_key"]),
            status=cast(str | None, row["status"]),
            record_date=cast(date | None, row["record_date"]),
            version=cast(int, row["version"]),
            payload=cast(dict[str, Any], raw_payload),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


__all__ = ["PERSONAL_CONTROL_MIGRATIONS", "PostgresPersonalRepository"]
