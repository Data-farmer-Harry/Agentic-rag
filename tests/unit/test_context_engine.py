from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.agent.context_engine import ContextEngine, HybridMemorySelector
from app.domain.enums import AnswerMode, MemoryType, RunStatus, TrustLevel
from app.domain.models import (
    AnswerResponse,
    ConversationMetadata,
    MemoryRecord,
    Provenance,
    RunContext,
    RunTrajectory,
)
from app.tokenization import TokenCounter


def _memory(
    key: str,
    summary: str,
    *,
    user_id: str | None = "user-a",
    confidence: float = 0.9,
    updated_at: datetime | None = None,
) -> MemoryRecord:
    return MemoryRecord(
        tenant_id="local",
        project_id="project-a",
        user_id=user_id,
        memory_type=MemoryType.SEMANTIC,
        key=key,
        summary=summary,
        confidence=confidence,
        provenance=[
            Provenance(
                source_type="test",
                source_id=key + summary,
                trust=TrustLevel.USER_ASSERTED,
            )
        ],
        updated_at=updated_at or datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_hybrid_memory_selection_resolves_scope_conflict_and_duplicates() -> None:
    selector = HybridMemorySelector(recency_half_life_days=30)
    old = datetime.now(UTC) - timedelta(days=180)
    shared = _memory("runtime", "Use the legacy runtime", user_id=None, updated_at=old)
    personal = _memory("runtime", "Use the Hermes runtime")
    duplicate = _memory("runtime-copy", "Use the Hermes runtime", confidence=0.8)
    irrelevant = _memory("lunch", "Order noodles")

    result = await selector.select(
        "Which runtime should this agent use?",
        [shared, personal, duplicate, irrelevant],
        user_id="user-a",
    )

    assert personal in result.records
    assert shared not in result.records
    assert duplicate not in result.records
    assert irrelevant not in result.records
    assert result.conflicts == 1
    assert result.duplicates == 1


@pytest.mark.asyncio
async def test_context_engine_persists_old_turn_summary_and_respects_token_budget() -> None:
    context = RunContext(
        tenant_id="local",
        project_id="project-a",
        user_id="user-a",
        session_id="long-session",
    )
    runs = [
        RunTrajectory(
            context=RunContext(
                tenant_id="local",
                project_id="project-a",
                user_id="user-a",
                session_id="long-session",
                started_at=datetime(2026, 1, day, tzinfo=UTC),
            ),
            user_input=f"question {day} " + "detail " * 20,
            answer=AnswerResponse(
                answer_markdown=f"answer {day} " + "result " * 30,
                response_mode=AnswerMode.CONVERSATIONAL,
            ),
            status=RunStatus.COMPLETED,
        )
        for day in range(1, 7)
    ]

    class Trajectories:
        async def list_session(self, **kwargs: object):
            del kwargs
            return list(reversed(runs))

    class Conversations:
        def __init__(self) -> None:
            self.value: ConversationMetadata | None = None

        async def get(self, **kwargs: object):
            del kwargs
            return self.value

        async def save(self, metadata: ConversationMetadata) -> None:
            self.value = metadata

    class Memories:
        async def list_scoped(self, **kwargs: object):
            del kwargs
            return []

    class Skills:
        async def list_by_status(self, *args: object, **kwargs: object):
            del args, kwargs
            return []

    conversations = Conversations()
    engine = ContextEngine(
        Trajectories(),  # type: ignore[arg-type]
        conversations,  # type: ignore[arg-type]
        Memories(),  # type: ignore[arg-type]
        Skills(),  # type: ignore[arg-type]
        max_turns=2,
        total_tokens=700,
        history_tokens=300,
        summary_tokens=120,
        memory_tokens=160,
        skill_tokens=100,
        personal_tokens=100,
    )

    history = await engine.history(context)
    capsule = await engine.capsule(context, "runtime")

    assert history[0].user_input.startswith("Earlier conversation summary")
    assert conversations.value is not None
    assert len(conversations.value.summarized_run_ids) == 4
    assert conversations.value.context_summary_revision is not None
    assert 1 <= capsule.trace.recent_turn_count <= 2
    assert capsule.trace.summarized_turn_count == 4
    assert capsule.trace.used_tokens <= capsule.trace.total_budget_tokens
    history_text = "".join(
        turn.user_input + turn.assistant_answer for turn in history
    )
    assert TokenCounter().count(history_text) <= 300
