from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from asyncpg import Connection

from app.infra.postgres import PostgresDatabase


@dataclass(frozen=True, slots=True)
class PostgresLearningTransaction:
    database: PostgresDatabase
    connection: Connection


_CURRENT_TRANSACTION: ContextVar[PostgresLearningTransaction | None] = ContextVar(
    "postgres_learning_transaction",
    default=None,
)


def current_postgres_learning_transaction(
    database: PostgresDatabase,
) -> PostgresLearningTransaction | None:
    transaction = _CURRENT_TRANSACTION.get()
    if transaction is None or transaction.database is not database:
        return None
    return transaction


@contextmanager
def postgres_learning_transaction(
    transaction: PostgresLearningTransaction,
) -> Iterator[None]:
    token = _CURRENT_TRANSACTION.set(transaction)
    try:
        yield
    finally:
        _CURRENT_TRANSACTION.reset(token)


__all__ = [
    "PostgresLearningTransaction",
    "current_postgres_learning_transaction",
    "postgres_learning_transaction",
]
