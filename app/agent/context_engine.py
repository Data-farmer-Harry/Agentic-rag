from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID

from app.agent.adaptive_rag_router import ConversationTurn
from app.domain.contracts import (
    ConversationRepository,
    MemoryRepository,
    SkillRepository,
    TrajectoryRepository,
)
from app.domain.enums import MemoryType, RunStatus, TrustLevel
from app.domain.models import (
    ContextTrace,
    ConversationMetadata,
    MemoryRecord,
    RunContext,
    RunTrajectory,
    utc_now,
)
from app.memory.memory_prompt_compiler import PromptCapsuleCompiler
from app.personal.service import PersonalControlService
from app.retrieval.embedding_providers import DenseEmbeddingPort, DeterministicDenseEmbedder
from app.skills.skill_registry import SkillDiscoveryRegistry
from app.tokenization import TokenCounter

_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_\-]+|[\u3400-\u9fff]+", re.UNICODE)
_SUMMARY_REVISION = "deterministic-conversation-summary-v1"
_CONTEXT_REVISION = "context-engine-v2"
_TRUST_SCORE = {
    TrustLevel.UNTRUSTED: 0.0,
    TrustLevel.USER_ASSERTED: 0.75,
    TrustLevel.OBSERVED: 0.85,
    TrustLevel.VERIFIED: 1.0,
}


@dataclass(frozen=True, slots=True)
class MemorySelection:
    records: tuple[MemoryRecord, ...]
    omitted: int
    duplicates: int
    conflicts: int


@dataclass(slots=True)
class _ContextState:
    component_tokens: dict[str, int] = field(default_factory=dict)
    recent_turn_count: int = 0
    summarized_turn_count: int = 0
    summary_revision: str | None = None
    truncated_components: set[str] = field(default_factory=set)
    selected_memory_ids: list[UUID] = field(default_factory=list)
    omitted_memory_count: int = 0
    duplicate_memory_count: int = 0
    conflicting_memory_count: int = 0


class RuntimeCapsule(str):
    """String-compatible runtime context plus exact memory and trace metadata."""

    __slots__ = ("memories", "trace")

    memories: tuple[MemoryRecord, ...]
    trace: ContextTrace

    def __new__(
        cls,
        text: str,
        memories: tuple[MemoryRecord, ...] = (),
        trace: ContextTrace | None = None,
    ) -> RuntimeCapsule:
        instance = str.__new__(cls, text)
        instance.memories = memories
        instance.trace = trace or ContextTrace(total_budget_tokens=0, used_tokens=0)
        return instance

    @property
    def text(self) -> str:
        return str(self)


class HybridMemorySelector:
    """Scope-safe BM25+dense memory ranking with decay and conflict resolution."""

    def __init__(
        self,
        *,
        dense: DenseEmbeddingPort | None = None,
        recency_half_life_days: float = 90.0,
    ) -> None:
        self._dense = dense or DeterministicDenseEmbedder(256)
        self._half_life_days = recency_half_life_days
        self._vector_cache: dict[str, list[float]] = {}
        self._cache_lock = asyncio.Lock()

    async def select(
        self,
        query: str,
        records: Sequence[MemoryRecord],
        *,
        user_id: str | None,
        limit: int = 20,
    ) -> MemorySelection:
        now = datetime.now(UTC)
        active = [record for record in records if self._active(record, now)]
        resolved, conflicts = self._resolve_conflicts(active, user_id=user_id)
        unique, duplicates = self._deduplicate(resolved)
        if not unique or limit == 0:
            return MemorySelection((), len(active), duplicates, conflicts)

        texts = [self._searchable(record) for record in unique]
        sparse_scores = self._bm25_scores(query, texts)
        dense_scores = await self._dense_scores(query, texts)
        ranked: list[tuple[float, MemoryRecord]] = []
        for index, record in enumerate(unique):
            trust = min((_TRUST_SCORE[item.trust] for item in record.provenance), default=0.0)
            age_days = max(0.0, (now - self._aware(record.updated_at)).total_seconds() / 86_400)
            recency = math.pow(0.5, age_days / self._half_life_days)
            relevance = 0.58 * dense_scores[index] + 0.27 * sparse_scores[index]
            quality = 0.10 * record.confidence + 0.05 * trust
            score = relevance + quality + 0.05 * recency
            globally_applicable = record.memory_type == MemoryType.POLICY
            if relevance > 0.0 or globally_applicable or not query.strip():
                ranked.append((score, record))
        ranked.sort(
            key=lambda item: (item[0], item[1].updated_at, str(item[1].memory_id)),
            reverse=True,
        )
        selected = tuple(record for _, record in ranked[:limit])
        return MemorySelection(
            selected,
            max(0, len(active) - len(selected)),
            duplicates,
            conflicts,
        )

    async def _dense_scores(self, query: str, texts: Sequence[str]) -> list[float]:
        if not query.strip():
            return [0.0] * len(texts)
        try:
            query_vector = (await self._dense.embed([query]))[0]
            vectors: list[list[float] | None] = [None] * len(texts)
            missing_texts: list[str] = []
            missing_indices: list[int] = []
            async with self._cache_lock:
                for index, text in enumerate(texts):
                    key = sha256(text.encode()).hexdigest()
                    cached = self._vector_cache.get(key)
                    if cached is None:
                        missing_texts.append(text)
                        missing_indices.append(index)
                    else:
                        vectors[index] = cached
            if missing_texts:
                embedded = await self._dense.embed(missing_texts)
                async with self._cache_lock:
                    for index, text, vector in zip(
                        missing_indices, missing_texts, embedded, strict=True
                    ):
                        vectors[index] = vector
                        self._vector_cache[sha256(text.encode()).hexdigest()] = vector
                    if len(self._vector_cache) > 2_000:
                        for key in tuple(self._vector_cache)[:500]:
                            self._vector_cache.pop(key, None)
            return [
                max(0.0, sum(a * b for a, b in zip(query_vector, vector or [], strict=True)))
                for vector in vectors
            ]
        except Exception:
            return [0.0] * len(texts)

    @staticmethod
    def _bm25_scores(query: str, documents: Sequence[str]) -> list[float]:
        query_terms = set(_search_tokens(query))
        if not query_terms or not documents:
            return [0.0] * len(documents)
        tokenized = [_search_tokens(document) for document in documents]
        average_length = sum(len(tokens) for tokens in tokenized) / max(len(tokenized), 1)
        document_frequency = Counter(
            token for tokens in tokenized for token in set(tokens) if token in query_terms
        )
        scores: list[float] = []
        for tokens in tokenized:
            frequencies = Counter(tokens)
            score = 0.0
            for term in query_terms:
                frequency = frequencies[term]
                if not frequency:
                    continue
                frequency_docs = document_frequency[term]
                idf = math.log(
                    1.0
                    + (len(documents) - frequency_docs + 0.5)
                    / (frequency_docs + 0.5)
                )
                normalization = frequency + 1.2 * (
                    0.25 + 0.75 * len(tokens) / max(average_length, 1.0)
                )
                score += idf * frequency * 2.2 / normalization
            scores.append(score)
        peak = max(scores, default=0.0)
        return [score / peak if peak else 0.0 for score in scores]

    @staticmethod
    def _resolve_conflicts(
        records: Sequence[MemoryRecord],
        *,
        user_id: str | None,
    ) -> tuple[list[MemoryRecord], int]:
        groups: dict[tuple[MemoryType, str], list[MemoryRecord]] = {}
        for record in records:
            groups.setdefault((record.memory_type, _normalize(record.key)), []).append(record)
        selected: list[MemoryRecord] = []
        conflicts = 0
        for group in groups.values():
            summaries = {_normalize(record.summary) for record in group}
            if len(summaries) > 1:
                conflicts += len(group) - 1
            group.sort(
                key=lambda record: (
                    record.user_id == user_id,
                    min((_TRUST_SCORE[item.trust] for item in record.provenance), default=0.0),
                    record.confidence,
                    record.updated_at,
                    str(record.memory_id),
                ),
                reverse=True,
            )
            selected.append(group[0])
        return selected, conflicts

    @staticmethod
    def _deduplicate(records: Sequence[MemoryRecord]) -> tuple[list[MemoryRecord], int]:
        seen: set[str] = set()
        selected: list[MemoryRecord] = []
        duplicates = 0
        for record in sorted(
            records,
            key=lambda item: (
                item.confidence,
                min(
                    (_TRUST_SCORE[source.trust] for source in item.provenance),
                    default=0.0,
                ),
                item.updated_at,
            ),
            reverse=True,
        ):
            fingerprint = _normalize(record.summary) + "\x1f" + _normalize(
                json.dumps(record.detail, ensure_ascii=False, sort_keys=True)
            )
            if fingerprint in seen:
                duplicates += 1
                continue
            seen.add(fingerprint)
            selected.append(record)
        return selected, duplicates

    @staticmethod
    def _active(record: MemoryRecord, now: datetime) -> bool:
        return (
            record.revoked_at is None
            and (record.expires_at is None or HybridMemorySelector._aware(record.expires_at) > now)
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @staticmethod
    def _searchable(record: MemoryRecord) -> str:
        return " ".join(
            (
                record.key,
                record.summary,
                json.dumps(record.detail, ensure_ascii=False, sort_keys=True),
            )
        )


class ContextEngine:
    """Assemble and persist bounded conversation, memory, skill, and personal context."""

    def __init__(
        self,
        trajectories: TrajectoryRepository,
        conversations: ConversationRepository,
        memories: MemoryRepository,
        skills: SkillRepository,
        *,
        personal: PersonalControlService | None = None,
        compiler: PromptCapsuleCompiler | None = None,
        dense: DenseEmbeddingPort | None = None,
        max_turns: int = 8,
        total_tokens: int = 8_000,
        history_tokens: int = 3_500,
        summary_tokens: int = 1_200,
        memory_tokens: int = 2_200,
        skill_tokens: int = 700,
        personal_tokens: int = 1_200,
        memory_recency_half_life_days: float = 90.0,
    ) -> None:
        if history_tokens + memory_tokens + skill_tokens + personal_tokens > total_tokens:
            raise ValueError("Context component token budgets exceed total_tokens")
        if summary_tokens > history_tokens:
            raise ValueError("summary_tokens cannot exceed history_tokens")
        self._trajectories = trajectories
        self._conversations = conversations
        self._memories = memories
        self._skills = skills
        self._personal = personal
        self._compiler = compiler or PromptCapsuleCompiler(max_chars=120_000)
        self._tokens = TokenCounter()
        self._memory_selector = HybridMemorySelector(
            dense=dense,
            recency_half_life_days=memory_recency_half_life_days,
        )
        self._max_turns = max_turns
        self._total_tokens = total_tokens
        self._history_tokens = min(history_tokens, total_tokens)
        self._summary_tokens = min(summary_tokens, self._history_tokens)
        self._memory_tokens = memory_tokens
        self._skill_tokens = skill_tokens
        self._personal_tokens = personal_tokens
        self._states: dict[UUID, _ContextState] = {}
        self._history_cache: dict[UUID, tuple[ConversationTurn, ...]] = {}

    async def history(self, context: RunContext) -> Sequence[ConversationTurn]:
        cached = self._history_cache.get(context.run_id)
        if cached is not None:
            return cached
        state = self._state(context.run_id)
        if self._max_turns == 0 or self._history_tokens == 0:
            self._history_cache[context.run_id] = ()
            return ()
        recent = await self._trajectories.list_session(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            session_id=context.session_id,
            limit=200,
        )
        candidates = [
            item
            for item in recent
            if item.context.run_id != context.run_id
            and item.context.tenant_id == context.tenant_id
            and item.context.project_id == context.project_id
            and item.context.user_id == context.user_id
            and item.context.session_id == context.session_id
            and item.status == RunStatus.COMPLETED
            and item.answer is not None
            and item.answer.answer_markdown.strip()
        ]
        recent_runs = candidates[: self._max_turns]
        older_runs = candidates[self._max_turns :]
        summary = await self._persist_summary(context, older_runs)
        turns = self._fit_history(summary, recent_runs, len(older_runs), state)
        self._history_cache[context.run_id] = turns
        return turns

    async def capsule(self, context: RunContext, query: str) -> RuntimeCapsule:
        await self.history(context)
        state = self._state(context.run_id)
        try:
            scoped = await self._memories.list_scoped(
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                user_id=context.user_id,
            )
        except (AttributeError, NotImplementedError):
            scoped = await self._memories.search(
                query,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                user_id=context.user_id,
                limit=20,
            )
        policy = context.execution_policy
        minimum_confidence = (
            policy.memory_min_confidence
            if policy is not None and policy.behavior_applied
            else None
        )
        if minimum_confidence is not None:
            scoped = [record for record in scoped if record.confidence >= minimum_confidence]
        memory_limit = (
            policy.capsule_memory_limit
            if policy is not None
            and policy.behavior_applied
            and policy.capsule_memory_limit is not None
            else 20
        )
        selection = await self._memory_selector.select(
            query,
            scoped,
            user_id=context.user_id,
            limit=memory_limit,
        )
        compiled = self._compiler.compile_result(
            selection.records,
            query=query,
            max_tokens=self._effective_budget(state, self._memory_tokens),
            token_counter=self._tokens.count,
            preserve_order=True,
        )
        state.selected_memory_ids = [record.memory_id for record in compiled.records]
        state.omitted_memory_count = selection.omitted + compiled.omitted
        state.duplicate_memory_count = selection.duplicates
        state.conflicting_memory_count = selection.conflicts
        state.component_tokens["memory"] = self._tokens.count(compiled.text)
        if compiled.omitted:
            state.truncated_components.add("memory")

        active, canary = await asyncio.gather(
            self._skills.list_by_status(
                "active", tenant_id=context.tenant_id, project_id=context.project_id
            ),
            self._skills.list_by_status(
                "canary", tenant_id=context.tenant_id, project_id=context.project_id
            ),
        )
        pinned = [
            skill
            for skill in [*active, *canary]
            if context.skill_versions.get(skill.name) == skill.version
        ]
        matches = SkillDiscoveryRegistry(pinned).discover(query, limit=8)
        skill_payload = [
            {
                "name": match.skill.name,
                "version": match.skill.version,
                "description": match.skill.description,
                "score": round(match.score, 4),
            }
            for match in matches
        ]
        skill_text = self._bounded_skill_index(skill_payload, state)
        personal_text = ""
        if self._personal is not None:
            personal_text = await self._personal.compile_runtime_capsule(context)
            personal_text = self._bounded_text(
                personal_text,
                self._effective_budget(state, self._personal_tokens),
                "personal",
                state,
            )
        parts = [compiled.text, skill_text]
        if personal_text:
            parts.append(personal_text)
        capsule = "\n".join(parts)
        trace = self.trace(context)
        return RuntimeCapsule(capsule, compiled.records, trace)

    def trace(self, context: RunContext) -> ContextTrace:
        state = self._state(context.run_id)
        used = sum(state.component_tokens.values())
        return ContextTrace(
            revision=_CONTEXT_REVISION,
            total_budget_tokens=self._total_tokens,
            used_tokens=used,
            component_tokens=dict(sorted(state.component_tokens.items())),
            selected_memory_ids=state.selected_memory_ids,
            omitted_memory_count=state.omitted_memory_count,
            duplicate_memory_count=state.duplicate_memory_count,
            conflicting_memory_count=state.conflicting_memory_count,
            recent_turn_count=state.recent_turn_count,
            summarized_turn_count=state.summarized_turn_count,
            summary_revision=state.summary_revision,
            truncated_components=sorted(state.truncated_components),
        )

    async def _persist_summary(
        self,
        context: RunContext,
        older_runs: Sequence[RunTrajectory],
    ) -> str:
        metadata = await self._conversations.get(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            session_id=context.session_id,
        )
        run_ids = [item.context.run_id for item in older_runs[:200]]
        if metadata is not None and metadata.summarized_run_ids == run_ids:
            return metadata.context_summary
        summary = self._summarize_runs(older_runs)
        if not older_runs and metadata is None:
            return ""
        updated = ConversationMetadata(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            session_id=context.session_id,
            title=metadata.title if metadata is not None else None,
            archived=metadata.archived if metadata is not None else False,
            context_summary=summary,
            summarized_run_ids=run_ids,
            context_summary_revision=_SUMMARY_REVISION,
            context_summary_updated_at=utc_now(),
            created_at=metadata.created_at if metadata is not None else utc_now(),
            updated_at=utc_now(),
        )
        await self._conversations.save(updated)
        return summary

    def _summarize_runs(self, runs: Sequence[RunTrajectory]) -> str:
        lines: list[str] = []
        used = 0
        per_turn_budget = max(
            24,
            self._summary_tokens // max(1, min(len(runs), 4)),
        )
        for run in runs:
            assert run.answer is not None
            user = self._tokens.truncate(
                " ".join(run.user_input.split()),
                max(8, per_turn_budget // 3),
            )
            answer = self._tokens.truncate(
                " ".join(run.answer.answer_markdown.split()),
                max(12, per_turn_budget * 2 // 3),
            )
            line = self._tokens.truncate(
                f"User: {user}\nAssistant: {answer}",
                per_turn_budget,
            )
            line_tokens = self._tokens.count(line)
            if used + line_tokens > self._summary_tokens:
                continue
            lines.append(line)
            used += line_tokens
        return "\n\n".join(reversed(lines))

    def _fit_history(
        self,
        summary: str,
        recent_runs: Sequence[RunTrajectory],
        summarized_turn_count: int,
        state: _ContextState,
    ) -> tuple[ConversationTurn, ...]:
        summary_turn: ConversationTurn | None = None
        summary_used = 0
        if summary:
            summary_prefix = "Earlier conversation summary (untrusted context):\n"
            summary_ack = "Context noted."
            summary_overhead = self._tokens.count(summary_prefix) + self._tokens.count(
                summary_ack
            )
            summary_budget = min(
                self._summary_tokens,
                max(0, self._history_tokens - summary_overhead),
            )
            bounded_summary = self._tokens.truncate(summary, summary_budget)
            if bounded_summary:
                summary_turn = ConversationTurn(
                    user_input=summary_prefix + bounded_summary,
                    assistant_answer=summary_ack,
                )
                summary_used = self._tokens.count(
                    summary_turn.user_input
                ) + self._tokens.count(summary_turn.assistant_answer)
                if bounded_summary != summary:
                    state.truncated_components.add("summary")
        newest_first: list[ConversationTurn] = []
        used = summary_used
        for run in recent_runs:
            assert run.answer is not None
            user = run.user_input.strip()
            assistant = run.answer.answer_markdown.strip()
            pair_tokens = self._tokens.count(user) + self._tokens.count(assistant)
            remaining = self._history_tokens - used
            if remaining <= 0:
                state.truncated_components.add("history")
                break
            if pair_tokens > remaining:
                if newest_first:
                    state.truncated_components.add("history")
                    break
                user_budget = min(self._tokens.count(user), max(1, remaining // 3))
                user = self._tokens.truncate(user, user_budget)
                assistant = self._tokens.truncate(assistant, max(1, remaining - user_budget))
                pair_tokens = self._tokens.count(user) + self._tokens.count(assistant)
                state.truncated_components.add("history")
            newest_first.append(ConversationTurn(user_input=user, assistant_answer=assistant))
            used += pair_tokens
        turns = list(reversed(newest_first))
        if summary_turn is not None:
            turns.insert(0, summary_turn)
        state.component_tokens["history"] = used
        state.recent_turn_count = len(newest_first)
        state.summarized_turn_count = summarized_turn_count
        if summary:
            state.summary_revision = _SUMMARY_REVISION
        return tuple(turns)

    def _bounded_skill_index(
        self,
        entries: Sequence[dict[str, Any]],
        state: _ContextState,
    ) -> str:
        selected: list[dict[str, Any]] = []
        budget = self._effective_budget(state, self._skill_tokens)
        for entry in entries:
            candidate = self._render_skill_index([*selected, entry])
            if self._tokens.count(candidate) <= budget:
                selected.append(entry)
            else:
                state.truncated_components.add("skills")
        rendered = self._render_skill_index(selected)
        state.component_tokens["skills"] = self._tokens.count(rendered)
        return rendered

    @staticmethod
    def _render_skill_index(entries: Sequence[dict[str, Any]]) -> str:
        payload = json.dumps(entries, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")
        return f"<skill_index>Untrusted discovery metadata: {payload}</skill_index>"

    def _bounded_text(
        self,
        text: str,
        budget: int,
        component: str,
        state: _ContextState,
    ) -> str:
        bounded = self._tokens.truncate(text, budget)
        if bounded != text:
            state.truncated_components.add(component)
        state.component_tokens[component] = self._tokens.count(bounded)
        return bounded

    def _effective_budget(self, state: _ContextState, requested: int) -> int:
        remaining = max(40, self._total_tokens - sum(state.component_tokens.values()))
        return min(requested, remaining)

    def _state(self, run_id: UUID) -> _ContextState:
        state = self._states.get(run_id)
        if state is None:
            if len(self._states) >= 512:
                oldest = next(iter(self._states))
                self._states.pop(oldest, None)
                self._history_cache.pop(oldest, None)
            state = _ContextState()
            self._states[run_id] = state
        return state


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _search_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_PATTERN.finditer(text.casefold()):
        value = match.group(0)
        if value[0].isascii():
            tokens.append(value)
            continue
        characters = list(value)
        tokens.extend(characters)
        tokens.extend(
            characters[index] + characters[index + 1]
            for index in range(len(characters) - 1)
        )
    return tokens
