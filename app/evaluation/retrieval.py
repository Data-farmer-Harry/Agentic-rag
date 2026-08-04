from __future__ import annotations

from math import ceil
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.contracts import RetrievalPort
from app.domain.models import RunContext
from app.retrieval.agentic import PlannerUsage


class RetrievalFixtureDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=100_000)
    title: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RetrievalGoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=2_000)
    category: str = Field(default="fact_lookup", min_length=1, max_length=100)
    difficulty: str = Field(default="medium", min_length=1, max_length=100)
    tenant_id: str = "local"
    project_id: str = "default"
    expected_source_ids: list[str] = Field(default_factory=list)
    forbidden_source_ids: list[str] = Field(default_factory=list)
    expected_intent: str | None = None
    expected_second_round: bool | None = None
    expect_empty: bool = False
    top_k: int = Field(default=10, ge=1, le=50)
    minimum_recall_at_k: float = Field(default=1.0, ge=0.0, le=1.0)
    minimum_reciprocal_rank: float = Field(default=0.0, ge=0.0, le=1.0)
    required_case: bool = False

    @model_validator(mode="after")
    def validate_source_constraints(self) -> Self:
        expected = [source_root(item) for item in self.expected_source_ids]
        forbidden = [source_root(item) for item in self.forbidden_source_ids]
        if any(not item.strip() for item in [*expected, *forbidden]):
            raise ValueError("retrieval source IDs must not be empty")
        if len(expected) != len(set(expected)):
            raise ValueError("expected source IDs must be unique")
        if len(forbidden) != len(set(forbidden)):
            raise ValueError("forbidden source IDs must be unique")
        overlap = sorted(set(expected).intersection(forbidden))
        if overlap:
            raise ValueError(f"source IDs cannot be both expected and forbidden: {overlap}")
        if self.expect_empty and expected:
            raise ValueError("empty retrieval cases cannot require sources")
        return self


class RetrievalGoldenSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    revision: str
    documents: list[RetrievalFixtureDocument]
    cases: list[RetrievalGoldenCase]
    required_case_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case_contract(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("retrieval golden case IDs must be unique")
        if len(self.required_case_ids) != len(set(self.required_case_ids)):
            raise ValueError("required retrieval case IDs must be unique")
        unknown = sorted(set(self.required_case_ids).difference(case_ids))
        if unknown:
            raise ValueError(f"required retrieval case IDs are unknown: {unknown}")
        per_case_required = {case.case_id for case in self.cases if case.required_case}
        declared_required = set(self.required_case_ids)
        if per_case_required and declared_required and per_case_required != declared_required:
            raise ValueError("per-case and golden-set required case IDs must agree")
        return self


class RetrievalCaseMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recall_at_k: float = Field(ge=0.0, le=1.0)
    reciprocal_rank: float = Field(ge=0.0, le=1.0)
    retrieved_count: int = Field(ge=0)
    expected_hit_count: int = Field(ge=0)
    forbidden_hit_count: int = Field(ge=0)
    round_count: int = Field(ge=0)
    second_round_new_evidence: int = Field(ge=0)
    executed_query_count: int = Field(ge=0)
    distinct_source_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)


class RetrievalCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    metrics: RetrievalCaseMetrics
    stop_reason: str | None = None
    planner_revision: str | None = None
    planner_fallback_error: str | None = None
    planned_intent: str | None = None
    planned_subqueries: list[str] = Field(default_factory=list)
    planned_fallback_queries: list[str] = Field(default_factory=list)
    planned_required_terms: list[str] = Field(default_factory=list)


class RetrievalSliceMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    mean_recall_at_k: float = Field(ge=0.0, le=1.0)
    mean_reciprocal_rank: float = Field(ge=0.0, le=1.0)
    mean_distinct_source_count: float = Field(ge=0.0)
    p95_duration_ms: int = Field(ge=0)


class RetrievalPlannerUsageMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    usage_reported_request_count: int = Field(ge=0)


class RetrievalEvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_name: str
    dataset_revision: str
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    required_case_ids: list[str] = Field(default_factory=list)
    required_failed_case_ids: list[str] = Field(default_factory=list)
    required_gate_passed: bool = True
    mean_recall_at_k: float = Field(ge=0.0, le=1.0)
    mean_reciprocal_rank: float = Field(ge=0.0, le=1.0)
    second_round_case_count: int = Field(ge=0)
    planner_fallback_count: int = Field(ge=0)
    mean_duration_ms: float = Field(ge=0.0)
    p95_duration_ms: int = Field(ge=0)
    category_metrics: dict[str, RetrievalSliceMetrics]
    difficulty_metrics: dict[str, RetrievalSliceMetrics]
    planner_usage: RetrievalPlannerUsageMetrics | None = None
    cases: list[RetrievalCaseResult]


class AgenticRetrievalEvaluator:
    def __init__(self, retrieval: RetrievalPort) -> None:
        self._retrieval = retrieval

    async def run(self, dataset: RetrievalGoldenSet) -> RetrievalEvalReport:
        planner_usage_before = await _planner_usage_snapshot(self._retrieval)
        results: list[RetrievalCaseResult] = []
        for case in dataset.cases:
            started_at = perf_counter()
            bundle = await self._retrieval.retrieve(
                case.query,
                RunContext(
                    tenant_id=case.tenant_id,
                    project_id=case.project_id,
                    session_id=f"retrieval-eval:{case.case_id}",
                ),
                top_k=case.top_k,
            )
            duration_ms = max(round((perf_counter() - started_at) * 1_000), 0)
            retrieved = [source_root(item.provenance.source_id) for item in bundle.evidence]
            expected = {source_root(item) for item in case.expected_source_ids}
            hits = expected.intersection(retrieved)
            recall = len(hits) / len(expected) if expected else 1.0
            first_expected_rank = next(
                (
                    index
                    for index, source_id in enumerate(retrieved, start=1)
                    if source_id in expected
                ),
                None,
            )
            reciprocal_rank = 1.0 / first_expected_rank if first_expected_rank else 0.0
            if not expected:
                reciprocal_rank = 1.0 if not case.expect_empty or not retrieved else 0.0
            forbidden_hits = {source_root(item) for item in case.forbidden_source_ids}.intersection(
                retrieved
            )
            rounds = bundle.trace.get("rounds", [])
            second_round = len(rounds) >= 2
            reasons: list[str] = []
            if recall < case.minimum_recall_at_k:
                reasons.append("recall_at_k_below_threshold")
            if reciprocal_rank < case.minimum_reciprocal_rank:
                reasons.append("reciprocal_rank_below_threshold")
            if forbidden_hits:
                reasons.append("forbidden_source_retrieved")
            if case.expect_empty and retrieved:
                reasons.append("expected_empty_retrieval")
            plan_trace = bundle.trace.get("plan", {})
            intent = plan_trace.get("intent")
            if case.expected_intent is not None and not _intent_matches(
                case.expected_intent,
                intent,
            ):
                reasons.append("unexpected_retrieval_intent")
            if (
                case.expected_second_round is not None
                and second_round != case.expected_second_round
            ):
                reasons.append("unexpected_round_count")
            results.append(
                RetrievalCaseResult(
                    case_id=case.case_id,
                    passed=not reasons,
                    reasons=reasons,
                    metrics=RetrievalCaseMetrics(
                        recall_at_k=recall,
                        reciprocal_rank=reciprocal_rank,
                        retrieved_count=len(retrieved),
                        expected_hit_count=len(hits),
                        forbidden_hit_count=len(forbidden_hits),
                        round_count=len(rounds),
                        second_round_new_evidence=(
                            int(rounds[1].get("new_evidence_count", 0)) if len(rounds) >= 2 else 0
                        ),
                        executed_query_count=int(bundle.trace.get("executed_query_count", 0)),
                        distinct_source_count=len(set(retrieved)),
                        duration_ms=duration_ms,
                    ),
                    stop_reason=bundle.trace.get("stop_reason"),
                    planner_revision=bundle.trace.get("planner_revision"),
                    planner_fallback_error=bundle.trace.get("planner_fallback_error"),
                    planned_intent=intent if isinstance(intent, str) else None,
                    planned_subqueries=_string_list(plan_trace.get("subqueries")),
                    planned_fallback_queries=_string_list(plan_trace.get("fallback_queries")),
                    planned_required_terms=_string_list(plan_trace.get("required_terms")),
                )
            )
        passed = sum(item.passed for item in results)
        total = len(results)
        required_case_ids = set(dataset.required_case_ids) or {
            case.case_id for case in dataset.cases if case.required_case
        }
        required_failed_case_ids = [
            item.case_id
            for item in results
            if item.case_id in required_case_ids and not item.passed
        ]
        durations = sorted(item.metrics.duration_ms for item in results)
        p95_index = max(ceil(0.95 * len(durations)) - 1, 0)
        planner_usage_after = await _planner_usage_snapshot(self._retrieval)
        return RetrievalEvalReport(
            dataset_name=dataset.name,
            dataset_revision=dataset.revision,
            total=total,
            passed=passed,
            required_case_ids=sorted(required_case_ids),
            required_failed_case_ids=required_failed_case_ids,
            required_gate_passed=not required_failed_case_ids,
            mean_recall_at_k=(
                sum(item.metrics.recall_at_k for item in results) / total if total else 1.0
            ),
            mean_reciprocal_rank=(
                sum(item.metrics.reciprocal_rank for item in results) / total if total else 1.0
            ),
            second_round_case_count=sum(item.metrics.round_count >= 2 for item in results),
            planner_fallback_count=sum(item.planner_fallback_error is not None for item in results),
            mean_duration_ms=(sum(durations) / total if total else 0.0),
            p95_duration_ms=(durations[p95_index] if durations else 0),
            category_metrics=_aggregate_slices(
                dataset.cases,
                results,
                field="category",
            ),
            difficulty_metrics=_aggregate_slices(
                dataset.cases,
                results,
                field="difficulty",
            ),
            planner_usage=_planner_usage_metrics(
                planner_usage_before,
                planner_usage_after,
            ),
            cases=results,
        )


def load_retrieval_golden_set(path: Path) -> RetrievalGoldenSet:
    return RetrievalGoldenSet.model_validate_json(path.read_text(encoding="utf-8"))


def source_root(source_id: str) -> str:
    """Normalize chunk-qualified provenance to the retained knowledge source."""
    return source_id.split("#", 1)[0]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _intent_matches(expected: str, actual: Any) -> bool:
    if not isinstance(actual, str):
        return False
    if expected == actual:
        return True
    # Enterprise fixtures retain the product-level "knowledge_query" intent while
    # the bounded retrieval planner reports a more specific retrieval strategy.
    if expected == "knowledge_query":
        return actual in {"lookup", "compare", "synthesis", "visual_lookup"}
    return False


async def _planner_usage_snapshot(retrieval: RetrievalPort) -> PlannerUsage | None:
    snapshot = getattr(retrieval, "planner_usage_snapshot", None)
    if snapshot is None:
        return None
    value = await snapshot()
    return value if isinstance(value, PlannerUsage) else None


def _planner_usage_metrics(
    before: PlannerUsage | None,
    after: PlannerUsage | None,
) -> RetrievalPlannerUsageMetrics | None:
    if before is None or after is None:
        return None
    usage = after.delta(before)
    return RetrievalPlannerUsageMetrics(
        request_count=usage.request_count,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens,
        usage_reported_request_count=usage.usage_reported_request_count,
    )


def _aggregate_slices(
    cases: list[RetrievalGoldenCase],
    results: list[RetrievalCaseResult],
    *,
    field: Literal["category", "difficulty"],
) -> dict[str, RetrievalSliceMetrics]:
    buckets: dict[str, list[RetrievalCaseResult]] = {}
    for case, result in zip(cases, results, strict=True):
        buckets.setdefault(str(getattr(case, field)), []).append(result)
    aggregates: dict[str, RetrievalSliceMetrics] = {}
    for name, items in sorted(buckets.items()):
        durations = sorted(item.metrics.duration_ms for item in items)
        count = len(items)
        aggregates[name] = RetrievalSliceMetrics(
            total=count,
            passed=sum(item.passed for item in items),
            pass_rate=sum(item.passed for item in items) / count,
            mean_recall_at_k=(sum(item.metrics.recall_at_k for item in items) / count),
            mean_reciprocal_rank=(sum(item.metrics.reciprocal_rank for item in items) / count),
            mean_distinct_source_count=(
                sum(item.metrics.distinct_source_count for item in items) / count
            ),
            p95_duration_ms=durations[max(ceil(0.95 * count) - 1, 0)],
        )
    return aggregates
