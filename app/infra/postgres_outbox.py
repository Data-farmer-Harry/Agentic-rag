from __future__ import annotations

import json
from uuid import UUID

from asyncpg import Connection, Record

from app.domain.models import OutboxEvent
from app.infra.outbox_errors import OutboxLeaseLostError
from app.infra.postgres import PostgresDatabase


async def insert_outbox_event(connection: Connection, event: OutboxEvent) -> None:
    await connection.execute(
        """
INSERT INTO outbox_events (
    event_id, aggregate_type, aggregate_id, event_type, payload, status,
    attempt, max_attempts, available_at, lease_owner, lease_expires_at,
    published_at, error, created_at, updated_at
) VALUES (
    $1, $2, $3, $4, $5::jsonb, $6,
    $7, $8, $9, $10, $11,
    $12, $13, $14, $15
)
ON CONFLICT (event_id) DO NOTHING
""",
        event.event_id,
        event.aggregate_type,
        event.aggregate_id,
        event.event_type,
        json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
        event.status.value,
        event.attempt,
        event.max_attempts,
        event.available_at,
        event.lease_owner,
        event.lease_expires_at,
        event.published_at,
        event.error,
        event.created_at,
        event.updated_at,
    )


class PostgresOutboxRepository:
    def __init__(self, database: PostgresDatabase) -> None:
        self._database = database

    async def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> OutboxEvent | None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if not 10 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        async with self._database.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
UPDATE outbox_events
SET status = 'dead_letter',
    error = COALESCE(error, 'Outbox lease expired at the maximum attempt limit.'),
    lease_owner = NULL,
    lease_expires_at = NULL,
    updated_at = now()
WHERE status = 'processing'
  AND lease_expires_at < now()
  AND attempt >= max_attempts
"""
            )
            record = await connection.fetchrow(
                """
WITH candidate AS (
    SELECT event_id
    FROM outbox_events
    WHERE (
        status = 'pending' AND available_at <= now()
    ) OR (
        status = 'processing'
        AND lease_expires_at < now()
        AND attempt < max_attempts
    )
    ORDER BY available_at ASC, created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE outbox_events AS event
SET status = 'processing',
    attempt = event.attempt + 1,
    lease_owner = $1,
    lease_expires_at = now() + ($2 * interval '1 second'),
    error = NULL,
    updated_at = now()
FROM candidate
WHERE event.event_id = candidate.event_id
RETURNING event.*
""",
                worker_id,
                lease_seconds,
            )
        return _event_from_record(record) if record is not None else None

    async def renew_lease(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        result = await self._database.pool.execute(
            """
UPDATE outbox_events
SET lease_expires_at = now() + ($3 * interval '1 second'), updated_at = now()
WHERE event_id = $1 AND status = 'processing' AND lease_owner = $2
""",
            event_id,
            worker_id,
            lease_seconds,
        )
        return result == "UPDATE 1"

    async def mark_published(
        self,
        event_id: UUID,
        *,
        worker_id: str,
    ) -> OutboxEvent:
        record = await self._database.pool.fetchrow(
            """
UPDATE outbox_events
SET status = 'published', published_at = now(),
    lease_owner = NULL, lease_expires_at = NULL, error = NULL, updated_at = now()
WHERE event_id = $1 AND status = 'processing' AND lease_owner = $2
RETURNING *
""",
            event_id,
            worker_id,
        )
        return _leased_event(record)

    async def fail(
        self,
        event_id: UUID,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: int,
    ) -> OutboxEvent:
        record = await self._database.pool.fetchrow(
            """
UPDATE outbox_events
SET status = CASE WHEN attempt < max_attempts THEN 'pending' ELSE 'dead_letter' END,
    available_at = CASE
        WHEN attempt < max_attempts THEN now() + ($4 * interval '1 second')
        ELSE available_at
    END,
    lease_owner = NULL,
    lease_expires_at = NULL,
    error = $3,
    updated_at = now()
WHERE event_id = $1 AND status = 'processing' AND lease_owner = $2
RETURNING *
""",
            event_id,
            worker_id,
            error[:1_000],
            max(1, retry_delay_seconds),
        )
        return _leased_event(record)

    async def count_unpublished(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> int:
        record = await self._database.pool.fetchrow(
            """
SELECT count(*) AS count
FROM outbox_events
WHERE status IN ('pending', 'processing')
  AND payload ->> 'tenant_id' = $1
  AND payload ->> 'project_id' = $2
""",
            tenant_id,
            project_id,
        )
        return int(record["count"]) if record is not None else 0


def _leased_event(record: Record | None) -> OutboxEvent:
    if record is None:
        raise OutboxLeaseLostError("The outbox event lease is no longer owned")
    return _event_from_record(record)


def _event_from_record(record: Record) -> OutboxEvent:
    payload = dict(record)
    if isinstance(payload.get("payload"), str):
        payload["payload"] = json.loads(payload["payload"])
    return OutboxEvent.model_validate(payload)
