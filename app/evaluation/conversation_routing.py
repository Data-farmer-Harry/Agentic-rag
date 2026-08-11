from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field

from app.agent.adaptive_rag_router import ConversationHistoryProvider, ConversationTurn
from app.domain.contracts import AgentRuntime
from app.domain.enums import EvidenceLevel, RoutingLane
from app.domain.models import AnswerResponse, RunContext


class ConversationRoutingTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    user_input: str = Field(min_length=1, max_length=50_000)
    assistant_answer: str = Field(min_length=1, max_length=50_000)


class ConversationRoutingCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=200)
    input: str = Field(min_length=1, max_length=50_000)
    expected_lane: RoutingLane
    history: list[ConversationRoutingTurn] = Field(default_factory=list, max_length=50)
    domain_pack: str = "general"


class ConversationRoutingGoldenSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    revision: str
    cases: list[ConversationRoutingCase] = Field(min_length=1)


class ConversationRoutingCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    expected_lane: RoutingLane
    actual_lane: RoutingLane
    passed: bool
    duration_ms: int = Field(ge=0)


class ConversationRoutingReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_name: str
    dataset_revision: str
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    unsafe_direct_count: int = Field(ge=0)
    over_escalation_count: int = Field(ge=0)
    mean_duration_ms: float = Field(ge=0.0)
    p95_duration_ms: int = Field(ge=0)
    confusion: dict[str, int]
    cases: list[ConversationRoutingCaseResult]


class EvaluationAgentRuntime(AgentRuntime):
    """A no-tool sentinel used to measure routing without running the full Agent."""

    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        del user_input, context
        return AnswerResponse(
            answer_markdown="evaluation-agent-sentinel",
            routing_lane=RoutingLane.AGENT,
            confidence=EvidenceLevel.INSUFFICIENT,
        )


class EvaluationConversationHistory:
    def __init__(self, dataset: ConversationRoutingGoldenSet) -> None:
        self._turns = {
            f"routing-eval:{case.case_id}": tuple(
                ConversationTurn(
                    user_input=turn.user_input,
                    assistant_answer=turn.assistant_answer,
                )
                for turn in case.history
            )
            for case in dataset.cases
        }

    async def __call__(self, context: RunContext) -> Sequence[ConversationTurn]:
        return self._turns.get(context.session_id, ())

    def provider(self) -> ConversationHistoryProvider:
        return self


class ConversationRoutingEvaluator:
    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        concurrency: int = 4,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        self._runtime = runtime
        self._concurrency = concurrency

    async def run(
        self,
        dataset: ConversationRoutingGoldenSet,
    ) -> ConversationRoutingReport:
        semaphore = asyncio.Semaphore(self._concurrency)

        async def evaluate(
            case: ConversationRoutingCase,
        ) -> ConversationRoutingCaseResult:
            async with semaphore:
                started_at = perf_counter()
                answer = await self._runtime.run(
                    case.input,
                    RunContext(
                        session_id=f"routing-eval:{case.case_id}",
                        domain_pack=case.domain_pack,
                    ),
                )
                duration_ms = max(round((perf_counter() - started_at) * 1_000), 0)
                actual_lane = answer.routing_lane or RoutingLane.AGENT
                return ConversationRoutingCaseResult(
                    case_id=case.case_id,
                    expected_lane=case.expected_lane,
                    actual_lane=actual_lane,
                    passed=actual_lane == case.expected_lane,
                    duration_ms=duration_ms,
                )

        results = list(await asyncio.gather(*(evaluate(case) for case in dataset.cases)))
        durations = sorted(item.duration_ms for item in results)
        passed = sum(item.passed for item in results)
        unsafe_direct_count = sum(
            item.expected_lane == RoutingLane.AGENT
            and item.actual_lane != RoutingLane.AGENT
            for item in results
        )
        over_escalation_count = sum(
            item.expected_lane != RoutingLane.AGENT
            and item.actual_lane == RoutingLane.AGENT
            for item in results
        )
        confusion = Counter(
            f"{item.expected_lane.value}->{item.actual_lane.value}" for item in results
        )
        return ConversationRoutingReport(
            dataset_name=dataset.name,
            dataset_revision=dataset.revision,
            total=len(results),
            passed=passed,
            pass_rate=passed / len(results) if results else 1.0,
            unsafe_direct_count=unsafe_direct_count,
            over_escalation_count=over_escalation_count,
            mean_duration_ms=(
                sum(item.duration_ms for item in results) / len(results) if results else 0.0
            ),
            p95_duration_ms=_percentile_95(durations),
            confusion=dict(sorted(confusion.items())),
            cases=results,
        )


def load_conversation_routing_golden_set(
    path: Path,
) -> ConversationRoutingGoldenSet:
    return ConversationRoutingGoldenSet.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _percentile_95(values: Sequence[int]) -> int:
    if not values:
        return 0
    index = max(0, min(len(values) - 1, (95 * len(values) + 99) // 100 - 1))
    return values[index]


__all__ = [
    "ConversationRoutingCase",
    "ConversationRoutingCaseResult",
    "ConversationRoutingEvaluator",
    "ConversationRoutingGoldenSet",
    "ConversationRoutingReport",
    "ConversationRoutingTurn",
    "EvaluationAgentRuntime",
    "EvaluationConversationHistory",
    "load_conversation_routing_golden_set",
]
