from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from time import perf_counter
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.contracts import KnowledgeRepository
from app.domain.enums import DocumentStatus
from app.domain.models import KnowledgeChunk, KnowledgeDocument, utc_now
from app.evaluation.retrieval import RetrievalEvalReport, RetrievalGoldenSet
from app.retrieval.embedding_providers import EmbeddingUsage


class KnowledgeIndexPort(Protocol):
    async def index_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> None: ...


class CalibrationScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)


class EmbeddingUsageMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_count: int = Field(ge=0)
    input_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    usage_reported_request_count: int = Field(ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)


class EmbeddingIndexFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    tenant_id: str
    project_id: str
    error: str


class EmbeddingIndexingOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    discovered_document_count: int = Field(ge=0)
    discovered_chunk_count: int = Field(ge=0)
    indexed_document_count: int = Field(ge=0)
    indexed_chunk_count: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    failures: list[EmbeddingIndexFailure] = Field(default_factory=list)


class RetrievalBaselineDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_dataset_revision: str
    candidate_dataset_revision: str
    passed_delta: int
    mean_recall_at_k_delta: float
    mean_reciprocal_rank_delta: float
    mean_duration_ms_delta: float
    p95_duration_ms_delta: int
    newly_failed_case_ids: list[str] = Field(default_factory=list)
    newly_passing_case_ids: list[str] = Field(default_factory=list)
    recall_regression_case_ids: list[str] = Field(default_factory=list)
    reciprocal_rank_regression_case_ids: list[str] = Field(default_factory=list)


class EmbeddingCalibrationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_revision: str = "embedding-calibration-v1"
    created_at: datetime = Field(default_factory=utc_now)
    target_collection: str
    active_collection: str
    embedding_revision: str
    planner_mode: str
    scopes: list[CalibrationScope]
    indexing: EmbeddingIndexingOutcome
    indexing_usage: EmbeddingUsageMetrics
    retrieval_usage: EmbeddingUsageMetrics
    evaluation: RetrievalEvalReport | None = None
    baseline_diff: RetrievalBaselineDiff | None = None
    gate_failures: list[str] = Field(default_factory=list)
    error: str | None = None
    passed: bool


def scopes_from_dataset(dataset: RetrievalGoldenSet) -> list[CalibrationScope]:
    return [
        CalibrationScope(tenant_id=tenant_id, project_id=project_id)
        for tenant_id, project_id in sorted(
            {(case.tenant_id, case.project_id) for case in dataset.cases}
        )
    ]


async def index_calibration_scopes(
    repository: KnowledgeRepository,
    index: KnowledgeIndexPort,
    scopes: Sequence[CalibrationScope],
    *,
    fail_fast: bool = True,
) -> EmbeddingIndexingOutcome:
    started_at = perf_counter()
    discovered_documents = 0
    discovered_chunks = 0
    indexed_documents = 0
    indexed_chunks = 0
    failures: list[EmbeddingIndexFailure] = []

    for scope in scopes:
        documents = await repository.list_documents(
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
        )
        for document in documents:
            if document.status != DocumentStatus.ACTIVE:
                continue
            chunks = list(
                await repository.list_chunks(
                    document.document_id,
                    tenant_id=scope.tenant_id,
                    project_id=scope.project_id,
                )
            )
            discovered_documents += 1
            discovered_chunks += len(chunks)
            try:
                if not chunks:
                    raise RuntimeError("Active document has no persisted chunks")
                await index.index_document(document, chunks)
            except Exception as exc:
                failures.append(
                    EmbeddingIndexFailure(
                        document_id=document.document_id,
                        tenant_id=scope.tenant_id,
                        project_id=scope.project_id,
                        error=f"{type(exc).__name__}: {exc}"[:1_000],
                    )
                )
                if fail_fast:
                    return _indexing_outcome(
                        started_at,
                        discovered_documents,
                        discovered_chunks,
                        indexed_documents,
                        indexed_chunks,
                        failures,
                    )
                continue
            indexed_documents += 1
            indexed_chunks += len(chunks)

    return _indexing_outcome(
        started_at,
        discovered_documents,
        discovered_chunks,
        indexed_documents,
        indexed_chunks,
        failures,
    )


def compare_retrieval_reports(
    baseline: RetrievalEvalReport,
    candidate: RetrievalEvalReport,
) -> RetrievalBaselineDiff:
    if (
        baseline.dataset_name != candidate.dataset_name
        or baseline.dataset_revision != candidate.dataset_revision
    ):
        raise ValueError("Retrieval reports must use the same dataset name and revision")
    baseline_cases = {item.case_id: item for item in baseline.cases}
    candidate_cases = {item.case_id: item for item in candidate.cases}
    if baseline_cases.keys() != candidate_cases.keys():
        raise ValueError("Retrieval reports must contain the same case IDs")

    case_ids = sorted(baseline_cases)
    return RetrievalBaselineDiff(
        baseline_dataset_revision=baseline.dataset_revision,
        candidate_dataset_revision=candidate.dataset_revision,
        passed_delta=candidate.passed - baseline.passed,
        mean_recall_at_k_delta=(
            candidate.mean_recall_at_k - baseline.mean_recall_at_k
        ),
        mean_reciprocal_rank_delta=(
            candidate.mean_reciprocal_rank - baseline.mean_reciprocal_rank
        ),
        mean_duration_ms_delta=(
            candidate.mean_duration_ms - baseline.mean_duration_ms
        ),
        p95_duration_ms_delta=candidate.p95_duration_ms - baseline.p95_duration_ms,
        newly_failed_case_ids=[
            case_id
            for case_id in case_ids
            if baseline_cases[case_id].passed and not candidate_cases[case_id].passed
        ],
        newly_passing_case_ids=[
            case_id
            for case_id in case_ids
            if not baseline_cases[case_id].passed and candidate_cases[case_id].passed
        ],
        recall_regression_case_ids=[
            case_id
            for case_id in case_ids
            if (
                candidate_cases[case_id].metrics.recall_at_k
                < baseline_cases[case_id].metrics.recall_at_k
            )
        ],
        reciprocal_rank_regression_case_ids=[
            case_id
            for case_id in case_ids
            if (
                candidate_cases[case_id].metrics.reciprocal_rank
                < baseline_cases[case_id].metrics.reciprocal_rank
            )
        ],
    )


def usage_metrics(
    current: EmbeddingUsage,
    previous: EmbeddingUsage,
    *,
    price_per_million_input_tokens: float | None,
) -> EmbeddingUsageMetrics:
    delta = current.delta(previous)
    estimated_cost: float | None = None
    if (
        price_per_million_input_tokens is not None
        and delta.request_count == delta.usage_reported_request_count
    ):
        estimated_cost = (
            delta.input_tokens / 1_000_000 * price_per_million_input_tokens
        )
    return EmbeddingUsageMetrics(
        request_count=delta.request_count,
        input_count=delta.input_count,
        input_tokens=delta.input_tokens,
        usage_reported_request_count=delta.usage_reported_request_count,
        estimated_cost_usd=estimated_cost,
    )


def _indexing_outcome(
    started_at: float,
    discovered_documents: int,
    discovered_chunks: int,
    indexed_documents: int,
    indexed_chunks: int,
    failures: list[EmbeddingIndexFailure],
) -> EmbeddingIndexingOutcome:
    return EmbeddingIndexingOutcome(
        discovered_document_count=discovered_documents,
        discovered_chunk_count=discovered_chunks,
        indexed_document_count=indexed_documents,
        indexed_chunk_count=indexed_chunks,
        duration_ms=max(round((perf_counter() - started_at) * 1_000), 0),
        failures=failures,
    )
