from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from app.domain.models import ConversationMetadata, RunTrajectory


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in text.split() if token.strip()}


class JsonlTrajectoryRepository:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def save(self, trajectory: RunTrajectory) -> None:
        async with self._lock:
            records = await self._read_all()
            records[str(trajectory.context.run_id)] = trajectory
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = "\n".join(
                item.model_dump_json(exclude_none=True) for item in records.values()
            )
            self._path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")

    async def get(self, run_id: UUID) -> RunTrajectory | None:
        return (await self._read_all()).get(str(run_id))

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> RunTrajectory | None:
        matches = [
            item
            for item in (await self._read_all()).values()
            if item.idempotency_key == idempotency_key
            and item.context.tenant_id == tenant_id
            and item.context.project_id == project_id
            and item.context.user_id == user_id
        ]
        if not matches:
            return None
        matches.sort(
            key=lambda item: (item.context.started_at, str(item.context.run_id)),
            reverse=True,
        )
        return matches[0]

    async def find_similar(
        self,
        trajectory: RunTrajectory,
        *,
        limit: int = 20,
    ) -> Sequence[RunTrajectory]:
        query_tokens = _tokens(trajectory.user_input)
        candidates: list[tuple[float, RunTrajectory]] = []
        for candidate in (await self._read_all()).values():
            if candidate.context.run_id == trajectory.context.run_id:
                continue
            if (
                candidate.context.tenant_id != trajectory.context.tenant_id
                or candidate.context.project_id != trajectory.context.project_id
                or candidate.context.domain_pack != trajectory.context.domain_pack
            ):
                continue
            candidate_tokens = _tokens(candidate.user_input)
            union = query_tokens | candidate_tokens
            score = len(query_tokens & candidate_tokens) / len(union) if union else 0.0
            if score > 0:
                candidates.append((score, candidate))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [item for _, item in candidates[:limit]]

    async def list_recent(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        limit: int = 50,
    ) -> Sequence[RunTrajectory]:
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        records = [
            item
            for item in (await self._read_all()).values()
            if item.context.tenant_id == tenant_id
            and item.context.project_id == project_id
        ]
        records.sort(
            key=lambda item: (item.context.started_at, str(item.context.run_id)),
            reverse=True,
        )
        return records[:limit]

    async def list_session(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        session_id: str = "default",
        limit: int = 50,
    ) -> Sequence[RunTrajectory]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        records = [
            item
            for item in (await self._read_all()).values()
            if item.context.tenant_id == tenant_id
            and item.context.project_id == project_id
            and item.context.user_id == user_id
            and item.context.session_id == session_id
        ]
        records.sort(
            key=lambda item: (item.context.started_at, str(item.context.run_id)),
            reverse=True,
        )
        return records[:limit]

    async def _read_all(self) -> dict[str, RunTrajectory]:
        if not self._path.exists():
            return {}
        records: dict[str, RunTrajectory] = {}
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = RunTrajectory.model_validate(json.loads(line))
            records[str(item.context.run_id)] = item
        return records


class JsonlConversationRepository:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    @staticmethod
    def _key(metadata: ConversationMetadata) -> str:
        return "\x1f".join(
            (
                metadata.tenant_id,
                metadata.project_id,
                metadata.user_id,
                metadata.session_id,
            )
        )

    async def save(self, metadata: ConversationMetadata) -> None:
        async with self._lock:
            records = await self._read_all()
            records[self._key(metadata)] = metadata
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = "\n".join(
                item.model_dump_json(exclude_none=True) for item in records.values()
            )
            self._path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")

    async def get(
        self,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
        session_id: str,
    ) -> ConversationMetadata | None:
        probe = ConversationMetadata(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            session_id=session_id,
        )
        return (await self._read_all()).get(self._key(probe))

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
    ) -> Sequence[ConversationMetadata]:
        records = [
            item
            for item in (await self._read_all()).values()
            if item.tenant_id == tenant_id
            and item.project_id == project_id
            and item.user_id == user_id
        ]
        records.sort(key=lambda item: (item.updated_at, item.session_id), reverse=True)
        return records

    async def _read_all(self) -> dict[str, ConversationMetadata]:
        if not self._path.exists():
            return {}
        records: dict[str, ConversationMetadata] = {}
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = ConversationMetadata.model_validate(json.loads(line))
            records[self._key(item)] = item
        return records
