from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LearningExecutionFence:
    job_id: UUID
    worker_id: str
    lease_token: UUID


_CURRENT_FENCE: ContextVar[LearningExecutionFence | None] = ContextVar(
    "hermesgraph_learning_execution_fence",
    default=None,
)


def current_learning_fence() -> LearningExecutionFence | None:
    return _CURRENT_FENCE.get()


@contextmanager
def learning_execution(fence: LearningExecutionFence) -> Iterator[None]:
    token = _CURRENT_FENCE.set(fence)
    try:
        yield
    finally:
        _CURRENT_FENCE.reset(token)


__all__ = [
    "LearningExecutionFence",
    "current_learning_fence",
    "learning_execution",
]
