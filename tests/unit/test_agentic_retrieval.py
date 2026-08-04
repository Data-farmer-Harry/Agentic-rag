import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.domain.enums import TrustLevel
from app.domain.models import EvidenceRef, Provenance, RetrievalBundle, RunContext
from app.retrieval.agentic import (
    AgenticRetrievalController,
    DeterministicQueryPlanner,
    OpenAIStructuredQueryPlanner,
    QueryPlanDraft,
    RetrievalGap,
    assess_retrieval_gap,
)


@pytest.mark.parametrize(
    ("query", "expected_intent"),
    [
        ("What architecture does Personal Singularity propose?", "lookup"),
        ("Which paper combines owner autonomy and Personal Singularity?", "lookup"),
        ("How does UNIBROWSE align image-to-text and text-to-image flows?", "lookup"),
        ("What does my uploaded MemOps image abstract say?", "visual_lookup"),
        ("What did I upload about memory evaluation?", "personal_recall"),
    ],
)
def test_deterministic_planner_requires_explicit_personal_or_visual_context(
    query: str,
    expected_intent: str,
) -> None:
    plan = asyncio.run(DeterministicQueryPlanner().plan(query))

    assert plan.intent == expected_intent


def test_deterministic_planner_decomposes_with_comparisons() -> None:
    plan = asyncio.run(
        DeterministicQueryPlanner().plan(
            "Compare EvoGraph dynamic editing with RAGU typed consolidation"
        )
    )

    assert plan.subqueries == [
        "EvoGraph dynamic editing",
        "RAGU typed consolidation",
    ]
    assert plan.minimum_evidence == 2
    assert plan.minimum_distinct_sources == 2


def evidence(source_id: str, text: str, *, visual: bool = False) -> EvidenceRef:
    metadata: dict[str, Any] = {"tenant_id": "tenant-a", "project_id": "project-a"}
    if visual:
        metadata.update({"modality": "image", "visual_region_id": "region-01"})
    return EvidenceRef(
        text=text,
        provenance=Provenance(
            source_type="fixture",
            source_id=source_id,
            trust=TrustLevel.VERIFIED,
        ),
        metadata=metadata,
    )


class FixturePlanner:
    revision = "fixture-planner-v1"

    def __init__(self, plan: QueryPlanDraft) -> None:
        self._plan = plan

    async def plan(self, query: str) -> QueryPlanDraft:
        del query
        return self._plan


class RecordingRetrieval:
    def __init__(self, results: dict[str, list[EvidenceRef]]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.active = 0
        self.max_active = 0

    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> RetrievalBundle:
        self.calls.append((query, dict(filters or {})))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        effective = dict(filters or {}) | {
            "tenant_id": context.tenant_id,
            "project_id": context.project_id,
        }
        return RetrievalBundle(
            query=query,
            evidence=self.results.get(query, [])[:top_k],
            applied_filters=effective,
            trace={"fixture": True},
        )


def test_controller_decomposes_in_parallel_and_stops_on_coverage() -> None:
    async def scenario() -> None:
        planner = FixturePlanner(
            QueryPlanDraft(
                intent="compare",
                subqueries=["Alpha architecture", "Beta architecture"],
                fallback_queries=["Alpha Beta limitations"],
                required_terms=["Alpha", "Beta"],
                minimum_evidence=2,
                minimum_distinct_sources=2,
                recommends_graph_search=True,
            )
        )
        retrieval = RecordingRetrieval(
            {
                "Alpha architecture": [evidence("alpha", "Alpha architecture details")],
                "Beta architecture": [evidence("beta", "Beta architecture details")],
            }
        )
        controller = AgenticRetrievalController(retrieval, planner=planner)

        result = await controller.retrieve(
            "Compare Alpha and Beta",
            RunContext(tenant_id="tenant-a", project_id="project-a"),
        )

        assert retrieval.max_active == 2
        assert {item.provenance.source_id for item in result.evidence} == {"alpha", "beta"}
        assert result.trace["stop_reason"] == "coverage_satisfied"
        assert result.trace["executed_query_count"] == 2
        assert result.trace["recommends_graph_search"] is True
        assert len(result.trace["rounds"]) == 1
        assert result.applied_filters == {
            "tenant_id": "tenant-a",
            "project_id": "project-a",
        }

    asyncio.run(scenario())


def test_gap_assessment_always_returns_a_strict_result() -> None:
    gap = assess_retrieval_gap(
        QueryPlanDraft(
            intent="lookup",
            subqueries=["missing"],
            fallback_queries=[],
            required_terms=["missing"],
        ),
        [],
    )

    assert isinstance(gap, RetrievalGap)
    assert gap.sufficient is False
    assert gap.reasons == [
        "insufficient_evidence_count",
        "insufficient_source_diversity",
        "no_evidence",
    ]


def test_controller_runs_second_round_only_for_a_measured_gap() -> None:
    async def scenario() -> None:
        planner = FixturePlanner(
            QueryPlanDraft(
                intent="synthesis",
                subqueries=["initial evidence"],
                fallback_queries=["missing independent evidence"],
                required_terms=["claim"],
                minimum_evidence=2,
                minimum_distinct_sources=2,
            )
        )
        retrieval = RecordingRetrieval(
            {
                "initial evidence": [evidence("source-a", "The claim is supported once.")],
                "missing independent evidence": [
                    evidence("source-b", "An independent source supports the claim.")
                ],
            }
        )
        controller = AgenticRetrievalController(retrieval, planner=planner)

        result = await controller.retrieve(
            "Synthesize the claim",
            RunContext(tenant_id="tenant-a", project_id="project-a"),
        )

        assert [call[0] for call in retrieval.calls] == [
            "initial evidence",
            "missing independent evidence",
        ]
        assert [gap["sufficient"] for gap in result.trace["gap_assessments"]] == [
            False,
            True,
        ]
        assert result.trace["gap_assessments"][0]["reasons"] == [
            "insufficient_evidence_count",
            "insufficient_source_diversity",
        ]
        assert result.trace["stop_reason"] == "coverage_satisfied"
        assert len(result.trace["rounds"]) == 2

    asyncio.run(scenario())


def test_controller_fails_soft_to_deterministic_planning() -> None:
    class FailingPlanner:
        revision = "failing-planner"

        async def plan(self, query: str) -> QueryPlanDraft:
            del query
            raise TimeoutError("planner unavailable")

    async def scenario() -> None:
        retrieval = RecordingRetrieval(
            {"Find AURORA-42": [evidence("aurora", "AURORA-42 is available.")]}
        )
        controller = AgenticRetrievalController(retrieval, planner=FailingPlanner())

        result = await controller.retrieve(
            "Find AURORA-42",
            RunContext(tenant_id="tenant-a", project_id="project-a"),
        )

        assert result.trace["planner_fallback_error"] == "TimeoutError"
        assert result.trace["planner_revision"] == "deterministic-query-planner-v2"
        assert result.trace["stop_reason"] == "coverage_satisfied"

    asyncio.run(scenario())


def test_controller_enforces_scope_before_planning_or_retrieval() -> None:
    async def scenario() -> None:
        retrieval = RecordingRetrieval({})
        controller = AgenticRetrievalController(retrieval)

        with pytest.raises(ValueError, match="cannot override"):
            await controller.retrieve(
                "scope test",
                RunContext(tenant_id="tenant-a", project_id="project-a"),
                filters={"tenant_id": "tenant-b"},
            )
        assert retrieval.calls == []

    asyncio.run(scenario())


def test_controller_requires_visual_evidence_when_plan_requests_it() -> None:
    async def scenario() -> None:
        planner = FixturePlanner(
            QueryPlanDraft(
                intent="visual_lookup",
                subqueries=["diagram visible text"],
                fallback_queries=["diagram region labels"],
                required_terms=["diagram"],
                requires_visual_evidence=True,
            )
        )
        retrieval = RecordingRetrieval(
            {
                "diagram visible text": [evidence("text-doc", "diagram description")],
                "diagram region labels": [
                    evidence("image-doc", "diagram region labels", visual=True)
                ],
            }
        )
        controller = AgenticRetrievalController(retrieval, planner=planner)

        result = await controller.retrieve(
            "What is visible in the diagram?",
            RunContext(tenant_id="tenant-a", project_id="project-a"),
        )

        assert result.trace["gap_assessments"][0]["reasons"] == [
            "missing_visual_evidence"
        ]
        assert result.trace["gap_assessments"][1]["visual_evidence_count"] == 1
        assert result.trace["stop_reason"] == "coverage_satisfied"

    asyncio.run(scenario())


def test_gap_source_diversity_counts_document_roots_not_chunks() -> None:
    async def scenario() -> None:
        planner = FixturePlanner(
            QueryPlanDraft(
                intent="synthesis",
                subqueries=["same document chunks"],
                fallback_queries=[],
                required_terms=[],
                minimum_distinct_sources=2,
            )
        )
        first = evidence("paper#chunk=1", "First chunk")
        second = evidence("paper#chunk=2", "Second chunk")
        retrieval = RecordingRetrieval({"same document chunks": [first, second]})
        controller = AgenticRetrievalController(retrieval, planner=planner)

        result = await controller.retrieve(
            "Synthesize independent sources",
            RunContext(tenant_id="tenant-a", project_id="project-a"),
        )

        gap = result.trace["gap_assessments"][0]
        assert gap["distinct_source_count"] == 1
        assert gap["reasons"] == ["insufficient_source_diversity"]

    asyncio.run(scenario())


def test_openai_planner_uses_strict_structured_output() -> None:
    class FakeResponses:
        def __init__(self) -> None:
            self.kwargs: dict[str, Any] = {}

        async def parse(self, **kwargs: Any) -> Any:
            self.kwargs = kwargs
            return SimpleNamespace(
                output_parsed=QueryPlanDraft(
                    intent="lookup",
                    subqueries=["HermesGraph architecture"],
                    fallback_queries=[],
                    required_terms=["HermesGraph"],
                ),
                usage=SimpleNamespace(
                    input_tokens=120,
                    output_tokens=30,
                    total_tokens=150,
                ),
            )

    class FakeClient:
        def __init__(self) -> None:
            self.responses = FakeResponses()

    async def scenario() -> None:
        client = FakeClient()
        planner = OpenAIStructuredQueryPlanner(client, model="fixture-model")  # type: ignore[arg-type]

        query = "According to my uploaded HermesGraph note, how is it organized?"
        result = await planner.plan(query)
        comparison = await planner.plan(
            "Compare PalmClaw architecture with MemOps memory lifecycle"
        )

        assert result.intent == "personal_recall"
        assert result.subqueries == [query]
        assert result.fallback_queries == ["HermesGraph architecture"]
        assert comparison.intent == "compare"
        assert comparison.subqueries == [
            "PalmClaw architecture",
            "MemOps memory lifecycle",
        ]
        assert comparison.minimum_evidence == 2
        assert comparison.minimum_distinct_sources == 2
        assert client.responses.kwargs["text_format"] is QueryPlanDraft
        assert client.responses.kwargs["store"] is False
        assert "untrusted" in client.responses.kwargs["input"][0]["content"]
        usage = await planner.usage_snapshot()
        assert usage.request_count == 2
        assert usage.input_tokens == 240
        assert usage.output_tokens == 60
        assert usage.total_tokens == 300
        assert usage.usage_reported_request_count == 2

    asyncio.run(scenario())
