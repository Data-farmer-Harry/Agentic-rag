from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.enums import GraphCandidateStatus
from app.domain.models import GraphEntityCandidate, GraphExtractionBatch
from app.evaluation.graph_extraction import (
    ExpectedGraphEntity,
    ExpectedGraphRelation,
    ExtractionEvalThresholds,
    GraphExtractionEvaluator,
    GraphExtractionGoldenCase,
    GraphExtractionGoldenSet,
    OpenAIUsageAccumulator,
)
from app.graph.extraction import RuleBasedEntityRelationExtractor


def _perfect_golden_set() -> GraphExtractionGoldenSet:
    return GraphExtractionGoldenSet(
        name="Evaluator contract fixture",
        revision="v1",
        cases=[
            GraphExtractionGoldenCase(
                case_id="perfect_explicit_relation",
                description="One exact rule relation",
                source_id="arxiv:fixture",
                source_title="Fixture paper",
                source_uri="https://arxiv.org/abs/fixture",
                category="architecture",
                difficulty="easy",
                tags=["natural_arxiv", "explicit_relation"],
                chunks=["HermesGraph uses Qdrant."],
                expected_entities=[
                    ExpectedGraphEntity(
                        canonical_name="HermesGraph",
                        accepted_types=["Concept"],
                        evidence_chunk_indexes=[0],
                    ),
                    ExpectedGraphEntity(
                        canonical_name="Qdrant",
                        accepted_types=["Concept"],
                        evidence_chunk_indexes=[0],
                    ),
                ],
                expected_relations=[
                    ExpectedGraphRelation(
                        source_name="HermesGraph",
                        target_name="Qdrant",
                        accepted_relation_types=["uses"],
                        evidence_chunk_indexes=[0],
                    )
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_graph_extraction_evaluator_passes_exact_pending_candidates() -> None:
    report = await GraphExtractionEvaluator(
        RuleBasedEntityRelationExtractor()
    ).run(_perfect_golden_set())

    assert report.passed is True
    assert report.gate_failures == []
    assert report.metrics.entity_f1 == 1.0
    assert report.metrics.relation_f1 == 1.0
    assert report.metrics.evidence_accuracy == 1.0
    assert report.cases[0].passed is True
    assert report.cases[0].source_id == "arxiv:fixture"
    assert report.category_metrics["architecture"].pass_rate == 1.0
    assert report.difficulty_metrics["easy"].total_cases == 1
    assert report.tag_metrics["natural_arxiv"].relation_recall == 1.0


class _FailingExtractor:
    revision = "failing-v1"

    async def extract(self, document, chunks, *, domain_pack="general"):  # type: ignore[no-untyped-def]
        del document, chunks, domain_pack
        raise RuntimeError("synthetic failure")


@pytest.mark.asyncio
async def test_extractor_failure_counts_expected_items_as_false_negatives() -> None:
    report = await GraphExtractionEvaluator(_FailingExtractor()).run(  # type: ignore[arg-type]
        _perfect_golden_set()
    )

    assert report.passed is False
    assert report.metrics.success_rate == 0.0
    assert report.metrics.entity_recall == 0.0
    assert report.metrics.relation_recall == 0.0
    assert report.counts.entity_false_negative == 2
    assert report.counts.relation_false_negative == 1
    assert report.cases[0].error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_required_case_blocks_gate_even_when_numeric_thresholds_are_zero() -> None:
    golden = _perfect_golden_set()
    golden = golden.model_copy(
        update={
            "cases": [golden.cases[0].model_copy(update={"required_pass": True})]
        }
    )
    report = await GraphExtractionEvaluator(
        _FailingExtractor(),  # type: ignore[arg-type]
        thresholds=ExtractionEvalThresholds(
            minimum_success_rate=0.0,
            minimum_entity_precision=0.0,
            minimum_entity_recall=0.0,
            minimum_entity_type_accuracy=0.0,
            minimum_relation_precision=0.0,
            minimum_relation_recall=0.0,
            minimum_evidence_accuracy=0.0,
        ),
    ).run(golden)

    assert report.gate_failures == ["required_case:perfect_explicit_relation"]


class _UnsafeExtractor:
    revision = "unsafe-v1"

    async def extract(self, document, chunks, *, domain_pack="general"):  # type: ignore[no-untyped-def]
        candidate = GraphEntityCandidate(
            document_id=document.document_id,
            tenant_id=document.tenant_id,
            project_id="wrong-project",
            canonical_name="HermesGraph",
            entity_type="Concept",
            source_chunk_ids=[chunks[0].chunk_id],
            confidence=1.0,
            extractor_revision=self.revision,
            status=GraphCandidateStatus.APPROVED,
        )
        return GraphExtractionBatch(
            document_id=document.document_id,
            tenant_id=document.tenant_id,
            project_id="wrong-project",
            domain_pack=domain_pack,
            extractor_revision=self.revision,
            entities=[candidate],
        )


@pytest.mark.asyncio
async def test_scope_and_pending_are_hard_gates_even_when_name_matches() -> None:
    case = GraphExtractionGoldenCase(
        case_id="unsafe_scope_candidate",
        description="Scope contract fixture",
        chunks=["HermesGraph"],
        expected_entities=[
            ExpectedGraphEntity(
                canonical_name="HermesGraph",
                accepted_types=["Concept"],
                evidence_chunk_indexes=[0],
            )
        ],
    )
    golden = GraphExtractionGoldenSet(name="unsafe", revision="v1", cases=[case])

    report = await GraphExtractionEvaluator(_UnsafeExtractor()).run(golden)  # type: ignore[arg-type]

    assert report.passed is False
    assert "scope_violations" in report.gate_failures
    assert "non_pending_candidates" in report.gate_failures
    assert report.counts.scope_violations == 2
    assert report.counts.non_pending_candidates == 1


class _UsageRuleExtractor:
    revision = "usage-rule-v1"

    def __init__(self, tracker: OpenAIUsageAccumulator) -> None:
        self._tracker = tracker
        self._rule = RuleBasedEntityRelationExtractor()

    async def extract(self, document, chunks, *, domain_pack="general"):  # type: ignore[no-untyped-def]
        self._tracker.observe(
            SimpleNamespace(
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=10,
                    total_tokens=110,
                    input_tokens_details=SimpleNamespace(cached_tokens=20),
                )
            )
        )
        return await self._rule.extract(document, chunks, domain_pack=domain_pack)


@pytest.mark.asyncio
async def test_usage_and_runtime_pricing_are_frozen_in_report() -> None:
    tracker = OpenAIUsageAccumulator()
    report = await GraphExtractionEvaluator(
        _UsageRuleExtractor(tracker),  # type: ignore[arg-type]
        usage_probe=tracker.snapshot,
        input_cost_per_million=2.0,
        cached_input_cost_per_million=1.0,
        output_cost_per_million=10.0,
    ).run(_perfect_golden_set())

    assert report.usage.input_tokens == 100
    assert report.usage.cached_input_tokens == 20
    assert report.usage.output_tokens == 10
    assert report.token_pricing is not None
    assert report.token_pricing.cached_input_cost_per_million == 1.0
    assert report.estimated_cost_usd == 0.00028


def test_golden_dataset_is_strict_and_repository_fixture_loads() -> None:
    fixture = GraphExtractionGoldenSet.load(
        Path("examples/evaluation/graph_extraction_golden.json")
    )

    assert fixture.revision == "2026-07-15-v1"
    assert len(fixture.cases) == 5
    with pytest.raises(ValidationError):
        GraphExtractionGoldenCase(
            case_id="empty_chunk",
            description="Invalid fixture",
            chunks=[""],
        )


def test_natural_arxiv_golden_dataset_has_frozen_coverage_contract() -> None:
    fixture = GraphExtractionGoldenSet.load(
        Path("examples/evaluation/graph_extraction_arxiv_golden.json")
    )

    assert fixture.revision == "2026-07-16-v1"
    assert len(fixture.cases) == 18
    assert len({case.case_id for case in fixture.cases}) == 18
    assert len({case.source_id for case in fixture.cases}) == 14
    assert sum("natural_arxiv" in case.tags for case in fixture.cases) == 17
    assert {case.case_id for case in fixture.cases if case.required_pass} == {
        "arxiv_natural_negative_no_core_fact",
        "arxiv_prompt_injection_preserves_fact",
    }
    assert {case.category for case in fixture.cases} == {
        "architecture",
        "method",
        "evaluation",
        "multi_chunk",
        "security",
        "negative",
    }
    assert all(
        case.source_id and case.source_title and case.source_uri
        for case in fixture.cases
    )


def test_golden_case_rejects_out_of_range_evidence_and_missing_endpoints() -> None:
    with pytest.raises(ValidationError, match="outside the case chunk list"):
        GraphExtractionGoldenCase(
            case_id="invalid_evidence",
            description="Invalid fixture",
            chunks=["HermesGraph uses Qdrant."],
            expected_entities=[
                ExpectedGraphEntity(
                    canonical_name="HermesGraph",
                    accepted_types=["Concept"],
                    evidence_chunk_indexes=[1],
                )
            ],
        )

    with pytest.raises(ValidationError, match="relation target"):
        GraphExtractionGoldenCase(
            case_id="missing_relation_endpoint",
            description="Invalid fixture",
            chunks=["HermesGraph uses Qdrant."],
            expected_entities=[
                ExpectedGraphEntity(
                    canonical_name="HermesGraph",
                    accepted_types=["Concept"],
                    evidence_chunk_indexes=[0],
                )
            ],
            expected_relations=[
                ExpectedGraphRelation(
                    source_name="HermesGraph",
                    target_name="Qdrant",
                    accepted_relation_types=["uses"],
                    evidence_chunk_indexes=[0],
                )
            ],
        )
