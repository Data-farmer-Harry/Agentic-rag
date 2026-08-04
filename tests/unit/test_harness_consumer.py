import asyncio
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.context.capsule import RuntimeCapsuleProvider
from app.domain.enums import MemoryType, TrustLevel
from app.domain.models import (
    GraphSearchRequest,
    GraphSearchResult,
    MemoryRecord,
    Provenance,
    RetrievalBundle,
    RunContext,
)
from app.harness.consumer import BoundedHarnessConsumer
from app.harness.models import (
    HarnessConfigDelta,
    HarnessContextConfig,
    HarnessMemoryConfig,
    HarnessOrchestrationConfig,
    HarnessOverlayMode,
    HarnessToolConfig,
    RunHarnessOverlay,
    canonical_hash,
)
from app.integration.runtime import IntegrationRuntime
from app.retrieval.agentic import AgenticRetrievalController, QueryPlanDraft


class StubMemoryRepository:
    def __init__(self, records: list[MemoryRecord]) -> None:
        self._records = records

    async def search(self, *args, **kwargs) -> list[MemoryRecord]:
        del args, kwargs
        return list(self._records)


class StubSkillRepository:
    async def list_by_status(self, *args, **kwargs) -> list[object]:
        del args, kwargs
        return []


class FixedPlanner:
    revision = "fixed-planner-v1"

    async def plan(self, query: str) -> QueryPlanDraft:
        del query
        return QueryPlanDraft(
            intent="synthesis",
            subqueries=["one", "two", "three", "four"],
            fallback_queries=[],
            required_terms=["missing"],
        )


class RecordingRetrieval:
    def __init__(self) -> None:
        self.calls: dict[str, list[str]] = {}

    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters=None,
        top_k: int = 10,
    ) -> RetrievalBundle:
        del filters, top_k
        self.calls.setdefault(str(context.run_id), []).append(query)
        await asyncio.sleep(0)
        return RetrievalBundle(query=query)


class RecordingGraph:
    def __init__(self) -> None:
        self.requests: list[GraphSearchRequest] = []

    async def search_graph(
        self,
        request: GraphSearchRequest,
        context: RunContext,
    ) -> GraphSearchResult:
        del context
        self.requests.append(request)
        return GraphSearchResult()


def _context_with_policy(
    delta: HarnessConfigDelta,
    *,
    applied: bool,
) -> RunContext:
    context = RunContext(project_id="consumer")
    mode = HarnessOverlayMode.ACTIVE if applied else HarnessOverlayMode.SHADOW
    payload = {
        "overlay_id": uuid5(NAMESPACE_URL, f"consumer:{context.run_id}:{mode.value}"),
        "run_id": context.run_id,
        "tenant_id": context.tenant_id,
        "project_id": context.project_id,
        "baseline_policy_versions": {"harness": "baseline-v1"},
        "selected_pattern_versions": [
            "00000000-0000-0000-0000-000000000001@1.0.0"
        ],
        "positive_experience_ids": [],
        "negative_experience_ids": [],
        "effective_delta": delta,
        "clamped_fields": [],
        "rejected_conflicts": [],
        "selection_trace_codes": ["test"],
        "selector_revision": "test-selector",
        "experience_bank_revision": "test-experiences",
        "pattern_bank_revision": "test-patterns",
        "mode": mode,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "expires_at": None,
    }
    overlay = RunHarnessOverlay.model_validate(
        {**payload, "payload_hash": canonical_hash(payload)}
    )
    policy = BoundedHarnessConsumer().resolve_policy(
        context=context,
        overlay=overlay,
        apply_requested=applied,
    )
    return context.model_copy(update={"execution_policy": policy})


def _memory(key: str, confidence: float) -> MemoryRecord:
    return MemoryRecord(
        memory_type=MemoryType.SEMANTIC,
        key=key,
        summary=f"summary-{key}",
        confidence=confidence,
        provenance=[
            Provenance(
                source_type="test",
                source_id=key,
                trust=TrustLevel.OBSERVED,
            )
        ],
    )


@pytest.mark.asyncio
async def test_capsule_consumer_is_noop_in_shadow_and_bounded_when_active() -> None:
    provider = RuntimeCapsuleProvider(
        StubMemoryRepository([_memory("high", 0.9), _memory("low", 0.7)]),
        StubSkillRepository(),  # type: ignore[arg-type]
    )
    baseline = RunContext(project_id="consumer")
    shadow = _context_with_policy(
        HarnessConfigDelta(
            context=HarnessContextConfig(capsule_memory_limit=1),
            memory=HarnessMemoryConfig(memory_min_confidence=0.8),
        ),
        applied=False,
    )
    active = _context_with_policy(
        HarnessConfigDelta(
            context=HarnessContextConfig(capsule_memory_limit=1),
            memory=HarnessMemoryConfig(memory_min_confidence=0.8),
        ),
        applied=True,
    )

    baseline_capsule = await provider(baseline, "memory")
    shadow_capsule = await provider(shadow, "memory")
    active_capsule = await provider(active, "memory")

    assert shadow_capsule == baseline_capsule
    assert "summary-high" in active_capsule
    assert "summary-low" not in active_capsule


@pytest.mark.asyncio
async def test_retrieval_consumer_is_run_local_under_concurrency() -> None:
    retrieval = RecordingRetrieval()
    controller = AgenticRetrievalController(
        retrieval,
        planner=FixedPlanner(),
        max_rounds=2,
        max_subqueries=4,
    )
    baseline = RunContext(project_id="consumer")
    active = _context_with_policy(
        HarnessConfigDelta(
            orchestration=HarnessOrchestrationConfig(
                retrieval_profile="lookup",
                max_subqueries=1,
                max_retrieval_rounds=1,
            )
        ),
        applied=True,
    )

    active_result, baseline_result = await asyncio.gather(
        controller.retrieve("query", active),
        controller.retrieve("query", baseline),
    )

    assert retrieval.calls[str(active.run_id)] == ["one"]
    assert retrieval.calls[str(baseline.run_id)][:4] == [
        "one",
        "two",
        "three",
        "four",
    ]
    assert len(retrieval.calls[str(baseline.run_id)]) == 5
    assert active_result.trace["effective_max_subqueries"] == 1
    assert active_result.trace["plan"]["intent"] == "lookup"
    assert baseline_result.trace["effective_max_subqueries"] == 4


@pytest.mark.asyncio
async def test_integration_runtime_clamps_graph_hops_only_for_applied_policy() -> None:
    graph = RecordingGraph()
    runtime = IntegrationRuntime(RecordingRetrieval(), graph=graph)
    active = _context_with_policy(
        HarnessConfigDelta(tool=HarnessToolConfig(graph_hops=1)),
        applied=True,
    )
    shadow = _context_with_policy(
        HarnessConfigDelta(tool=HarnessToolConfig(graph_hops=1)),
        applied=False,
    )

    await runtime.search_graph(
        GraphSearchRequest(entities=["GraphRAG"], template="paths", max_hops=3),
        active,
    )
    await runtime.search_graph(
        GraphSearchRequest(entities=["GraphRAG"], template="paths", max_hops=3),
        shadow,
    )

    assert [item.max_hops for item in graph.requests] == [1, 3]
