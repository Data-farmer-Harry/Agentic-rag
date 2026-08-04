import asyncio

from app.domain.enums import TrustLevel
from app.domain.models import EvidenceRef, Provenance, RetrievalBundle, RunContext
from app.evaluation.retrieval import (
    AgenticRetrievalEvaluator,
    RetrievalGoldenCase,
    RetrievalGoldenSet,
    source_root,
)
from app.retrieval.agentic import PlannerUsage


class FixtureController:
    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: dict[str, object] | None = None,
        top_k: int = 10,
    ) -> RetrievalBundle:
        del query, filters, top_k
        good = EvidenceRef(
            text="Expected evidence",
            provenance=Provenance(
                source_type="fixture",
                source_id="expected",
                trust=TrustLevel.VERIFIED,
            ),
            metadata={"tenant_id": context.tenant_id, "project_id": context.project_id},
        )
        return RetrievalBundle(
            query="fixture",
            evidence=[good],
            trace={
                "planner_revision": "fixture",
                "plan": {"intent": "lookup"},
                "rounds": [
                    {"round": 1, "new_evidence_count": 0},
                    {"round": 2, "new_evidence_count": 1},
                ],
                "executed_query_count": 2,
                "stop_reason": "coverage_satisfied",
            },
        )


def test_agentic_retrieval_evaluator_scores_sources_rounds_and_intent() -> None:
    async def scenario() -> None:
        dataset = RetrievalGoldenSet(
            name="fixture",
            revision="v1",
            documents=[],
            cases=[
                RetrievalGoldenCase(
                    case_id="case",
                    query="find expected",
                    category="paraphrase",
                    difficulty="hard",
                    expected_source_ids=["expected"],
                    forbidden_source_ids=["foreign"],
                    expected_intent="lookup",
                    expected_second_round=True,
                    minimum_reciprocal_rank=1.0,
                )
            ],
        )

        report = await AgenticRetrievalEvaluator(FixtureController()).run(dataset)

        assert report.total == report.passed == 1
        assert report.mean_recall_at_k == 1.0
        assert report.mean_reciprocal_rank == 1.0
        assert report.second_round_case_count == 1
        assert report.cases[0].metrics.second_round_new_evidence == 1
        assert report.cases[0].metrics.distinct_source_count == 1
        assert report.cases[0].metrics.duration_ms >= 0
        assert report.cases[0].stop_reason == "coverage_satisfied"
        assert report.cases[0].planned_intent == "lookup"
        assert report.mean_duration_ms >= 0
        assert report.p95_duration_ms >= 0
        assert report.category_metrics["paraphrase"].pass_rate == 1.0
        assert report.category_metrics["paraphrase"].mean_recall_at_k == 1.0
        assert report.difficulty_metrics["hard"].total == 1

    asyncio.run(scenario())


def test_source_root_removes_only_chunk_locator() -> None:
    assert source_root("arxiv:2607.12893#chunk=17") == "arxiv:2607.12893"
    assert source_root("uploaded-document") == "uploaded-document"


def test_evaluator_reports_openai_planner_usage_delta() -> None:
    class UsageController(FixtureController):
        def __init__(self) -> None:
            self.requests = 0

        async def retrieve(
            self,
            query: str,
            context: RunContext,
            *,
            filters: dict[str, object] | None = None,
            top_k: int = 10,
        ) -> RetrievalBundle:
            self.requests += 1
            return await super().retrieve(
                query,
                context,
                filters=filters,
                top_k=top_k,
            )

        async def planner_usage_snapshot(self) -> PlannerUsage:
            return PlannerUsage(
                request_count=self.requests,
                input_tokens=self.requests * 100,
                output_tokens=self.requests * 20,
                total_tokens=self.requests * 120,
                usage_reported_request_count=self.requests,
            )

    async def scenario() -> None:
        dataset = RetrievalGoldenSet(
            name="usage",
            revision="v1",
            documents=[],
            cases=[
                RetrievalGoldenCase(
                    case_id="case",
                    query="find expected",
                    expected_source_ids=["expected"],
                )
            ],
        )

        report = await AgenticRetrievalEvaluator(UsageController()).run(dataset)

        assert report.planner_usage is not None
        assert report.planner_usage.request_count == 1
        assert report.planner_usage.input_tokens == 100
        assert report.planner_usage.output_tokens == 20
        assert report.planner_usage.total_tokens == 120

    asyncio.run(scenario())


def test_forbidden_only_case_does_not_lower_mrr_without_a_target() -> None:
    class NonTargetController:
        async def retrieve(
            self,
            query: str,
            context: RunContext,
            *,
            filters: dict[str, object] | None = None,
            top_k: int = 10,
        ) -> RetrievalBundle:
            del query, filters, top_k
            return RetrievalBundle(
                query="scope",
                evidence=[
                    EvidenceRef(
                        text="Allowed same-scope evidence",
                        provenance=Provenance(
                            source_type="fixture",
                            source_id="allowed#chunk=1",
                        ),
                        metadata={
                            "tenant_id": context.tenant_id,
                            "project_id": context.project_id,
                        },
                    )
                ],
                trace={"plan": {"intent": "lookup"}, "rounds": []},
            )

    async def scenario() -> None:
        dataset = RetrievalGoldenSet(
            name="scope",
            revision="v1",
            documents=[],
            cases=[
                RetrievalGoldenCase(
                    case_id="scope",
                    query="scope only",
                    expected_source_ids=[],
                    forbidden_source_ids=["foreign"],
                )
            ],
        )

        report = await AgenticRetrievalEvaluator(NonTargetController()).run(dataset)

        assert report.passed == 1
        assert report.mean_reciprocal_rank == 1.0

    asyncio.run(scenario())
