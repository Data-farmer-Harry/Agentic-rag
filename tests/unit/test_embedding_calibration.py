from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

import pytest

from app.domain.enums import DocumentStatus
from app.domain.models import KnowledgeChunk, KnowledgeDocument
from app.evaluation.embedding_calibration import (
    CalibrationScope,
    compare_retrieval_reports,
    index_calibration_scopes,
    scopes_from_dataset,
    usage_metrics,
)
from app.evaluation.retrieval import (
    RetrievalCaseMetrics,
    RetrievalCaseResult,
    RetrievalEvalReport,
    RetrievalGoldenCase,
    RetrievalGoldenSet,
)
from app.retrieval.embedding_providers import EmbeddingUsage


def _document(*, status: DocumentStatus = DocumentStatus.ACTIVE) -> KnowledgeDocument:
    return KnowledgeDocument(
        filename="paper.txt",
        title="Paper",
        media_type="text/plain",
        byte_size=10,
        content_hash="a" * 64,
        storage_key="objects/paper.txt",
        status=status,
        chunk_count=1,
    )


def _chunk(document: KnowledgeDocument) -> KnowledgeChunk:
    return KnowledgeChunk(
        document_id=document.document_id,
        chunk_index=0,
        text="Agentic retrieval evidence",
        content_hash="b" * 64,
        char_end=27,
    )


class _Repository:
    def __init__(self, documents: Sequence[KnowledgeDocument]) -> None:
        self.documents = list(documents)
        self.chunks = {
            item.document_id: [_chunk(item)]
            for item in documents
            if item.status == DocumentStatus.ACTIVE
        }

    async def list_documents(self, **kwargs: Any) -> Sequence[KnowledgeDocument]:
        del kwargs
        return self.documents

    async def list_chunks(self, document_id: UUID, **kwargs: Any) -> Sequence[KnowledgeChunk]:
        del kwargs
        return self.chunks.get(document_id, [])


class _Index:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.indexed: list[UUID] = []

    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None:
        assert chunks
        if self.fail:
            raise RuntimeError("provider unavailable")
        self.indexed.append(document.document_id)


@pytest.mark.asyncio
async def test_isolated_indexing_skips_non_active_documents() -> None:
    active = _document()
    failed = _document(status=DocumentStatus.FAILED)
    repository = _Repository([active, failed])
    index = _Index()

    outcome = await index_calibration_scopes(
        repository,  # type: ignore[arg-type]
        index,
        [CalibrationScope(tenant_id="local", project_id="default")],
    )

    assert index.indexed == [active.document_id]
    assert outcome.discovered_document_count == 1
    assert outcome.discovered_chunk_count == 1
    assert outcome.indexed_document_count == 1
    assert outcome.indexed_chunk_count == 1
    assert outcome.failures == []


@pytest.mark.asyncio
async def test_isolated_indexing_fails_closed_with_document_identity() -> None:
    document = _document()

    outcome = await index_calibration_scopes(
        _Repository([document]),  # type: ignore[arg-type]
        _Index(fail=True),
        [CalibrationScope(tenant_id="local", project_id="default")],
    )

    assert outcome.indexed_document_count == 0
    assert outcome.failures[0].document_id == document.document_id
    assert "provider unavailable" in outcome.failures[0].error


def _report(*, passed: bool, recall: float, reciprocal_rank: float) -> RetrievalEvalReport:
    case = RetrievalCaseResult(
        case_id="case",
        passed=passed,
        reasons=[] if passed else ["recall_at_k_below_threshold"],
        metrics=RetrievalCaseMetrics(
            recall_at_k=recall,
            reciprocal_rank=reciprocal_rank,
            retrieved_count=1,
            expected_hit_count=int(recall > 0),
            forbidden_hit_count=0,
            round_count=1,
            second_round_new_evidence=0,
            executed_query_count=1,
            distinct_source_count=1,
            duration_ms=20,
        ),
    )
    return RetrievalEvalReport(
        dataset_name="retrieval",
        dataset_revision="v1",
        total=1,
        passed=int(passed),
        mean_recall_at_k=recall,
        mean_reciprocal_rank=reciprocal_rank,
        second_round_case_count=0,
        planner_fallback_count=0,
        mean_duration_ms=20,
        p95_duration_ms=20,
        category_metrics={},
        difficulty_metrics={},
        cases=[case],
    )


def test_baseline_diff_names_quality_regressions() -> None:
    diff = compare_retrieval_reports(
        _report(passed=True, recall=1.0, reciprocal_rank=1.0),
        _report(passed=False, recall=0.0, reciprocal_rank=0.0),
    )

    assert diff.passed_delta == -1
    assert diff.newly_failed_case_ids == ["case"]
    assert diff.recall_regression_case_ids == ["case"]
    assert diff.reciprocal_rank_regression_case_ids == ["case"]


def test_usage_metrics_only_estimates_complete_provider_usage() -> None:
    complete = usage_metrics(
        EmbeddingUsage(2, 10, 500, 2),
        EmbeddingUsage(),
        price_per_million_input_tokens=0.02,
    )
    partial = usage_metrics(
        EmbeddingUsage(2, 10, 250, 1),
        EmbeddingUsage(),
        price_per_million_input_tokens=0.02,
    )

    assert complete.estimated_cost_usd == pytest.approx(0.00001)
    assert partial.estimated_cost_usd is None


def test_dataset_scopes_are_deduplicated_and_sorted() -> None:
    dataset = RetrievalGoldenSet(
        name="retrieval",
        revision="v1",
        documents=[],
        cases=[
            RetrievalGoldenCase(case_id="b", query="b", project_id="computer-science"),
            RetrievalGoldenCase(case_id="a", query="a", project_id="default"),
            RetrievalGoldenCase(case_id="c", query="c", project_id="default"),
        ],
    )

    assert scopes_from_dataset(dataset) == [
        CalibrationScope(tenant_id="local", project_id="computer-science"),
        CalibrationScope(tenant_id="local", project_id="default"),
    ]
