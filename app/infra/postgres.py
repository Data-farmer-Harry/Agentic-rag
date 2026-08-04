from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import asyncpg
from asyncpg import Pool

_MIGRATION_LOCK_ID = 3_849_172_605_811_043_727


class PostgresDatabaseError(RuntimeError):
    pass


class PostgresMigrationError(PostgresDatabaseError):
    pass


@dataclass(frozen=True, slots=True)
class PostgresMigration:
    version: int
    name: str
    statement: str

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Migration version must be positive")
        if not self.name.strip():
            raise ValueError("Migration name is required")
        if not self.statement.strip():
            raise ValueError("Migration statement is required")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.statement.encode()).hexdigest()


class PostgresDatabase:
    """Shared asyncpg pool and checksum-verified migration runner."""

    def __init__(
        self,
        dsn: str,
        *,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        command_timeout_seconds: int = 30,
    ) -> None:
        if not dsn.strip():
            raise ValueError("A Postgres DSN is required")
        if not 1 <= min_pool_size <= max_pool_size <= 50:
            raise ValueError("Invalid Postgres pool bounds")
        self._dsn = normalize_postgres_dsn(dsn.strip())
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._command_timeout_seconds = command_timeout_seconds
        self._pool: Pool | None = None

    @property
    def pool(self) -> Pool:
        if self._pool is None:
            raise PostgresDatabaseError("Postgres database is not started")
        return self._pool

    async def start(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_pool_size,
            max_size=self._max_pool_size,
            command_timeout=self._command_timeout_seconds,
        )
        self._pool = pool

    async def close(self) -> None:
        if self._pool is None:
            return
        pool, self._pool = self._pool, None
        await pool.close()

    async def migrate(self, migrations: Sequence[PostgresMigration]) -> None:
        ordered = sorted(migrations, key=lambda item: item.version)
        versions = [item.version for item in ordered]
        if len(versions) != len(set(versions)):
            raise PostgresMigrationError("Migration versions must be unique")
        pool = self.pool
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
CREATE TABLE IF NOT EXISTS hermesgraph_schema_migrations (
    version integer PRIMARY KEY,
    name text,
    checksum char(64),
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""
            )
            await connection.execute(
                "ALTER TABLE hermesgraph_schema_migrations "
                "ADD COLUMN IF NOT EXISTS name text"
            )
            await connection.execute(
                "ALTER TABLE hermesgraph_schema_migrations "
                "ADD COLUMN IF NOT EXISTS checksum char(64)"
            )
            await connection.execute(
                "SELECT pg_advisory_xact_lock($1)", _MIGRATION_LOCK_ID
            )
            rows = await connection.fetch(
                "SELECT version, name, checksum FROM hermesgraph_schema_migrations"
            )
            applied = {cast(int, row["version"]): row for row in rows}
            for migration in ordered:
                existing = applied.get(migration.version)
                if existing is not None:
                    checksum = cast(str | None, existing["checksum"])
                    if checksum is not None and checksum != migration.checksum:
                        raise PostgresMigrationError(
                            f"Migration {migration.version} checksum does not match"
                        )
                    if checksum is None:
                        await connection.execute(
                            """
UPDATE hermesgraph_schema_migrations
SET name = $2, checksum = $3
WHERE version = $1
""",
                            migration.version,
                            migration.name,
                            migration.checksum,
                        )
                    continue
                await connection.execute(migration.statement)
                await connection.execute(
                    """
INSERT INTO hermesgraph_schema_migrations (version, name, checksum)
VALUES ($1, $2, $3)
""",
                    migration.version,
                    migration.name,
                    migration.checksum,
                )


class PostgresRuntimeResource:
    def __init__(
        self,
        database: PostgresDatabase,
        migrations: Sequence[PostgresMigration],
    ) -> None:
        self._database = database
        self._migrations = tuple(migrations)

    async def start(self) -> None:
        await self._database.start()
        try:
            await self._database.migrate(self._migrations)
        except BaseException:
            await self._database.close()
            raise

    async def close(self) -> None:
        await self._database.close()


def normalize_postgres_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql+asyncpg://"):
        return f"postgresql://{dsn.removeprefix('postgresql+asyncpg://')}"
    return dsn
