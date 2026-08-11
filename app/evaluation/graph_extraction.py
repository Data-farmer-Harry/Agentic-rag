from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, model_validator

from app.domain.contracts import EntityRelationExtractorPort
from app.domain.enums import GraphCandidateStatus
from app.domain.models import (
    GraphEntityCandidate,
    GraphExtractionBatch,
    GraphRelationCandidate,
    KnowledgeChunk,
    KnowledgeDocument,
    StrictModel,
    utc_now,
)
from app.graph.graph_identity import normalized_entity_key

ChunkText = Annotated[str, Field(min_length=1, max_length=20_000)]


class ExpectedGraphEntity(StrictModel):
    canonical_name: str = Field(min_length=2, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    accepted_types: list[str] = Field(min_length=1, max_length=12)
    evidence_chunk_indexes: list[int] = Field(min_length=1, max_length=50)


class ExpectedGraphRelation(StrictModel):
    source_name: str = Field(min_length=2, max_length=300)
    source_aliases: list[str] = Field(default_factory=list, max_length=20)
    target_name: str = Field(min_length=2, max_length=300)
    target_aliases: list[str] = Field(default_factory=list, max_length=20)
    accepted_relation_types: list[str] = Field(min_length=1, max_length=12)
    evidence_chunk_indexes: list[int] = Field(min_length=1, max_length=50)


class GraphExtractionGoldenCase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,99}$")
    description: str = Field(min_length=1, max_length=500)
    source_id: str | None = Field(default=None, min_length=1, max_length=300)
    source_title: str | None = Field(default=None, min_length=1, max_length=500)
    source_uri: str | None = Field(default=None, min_length=1, max_length=2_000)
    domain_pack: str = "general"
    category: Literal[
        "architecture",
        "method",
        "evaluation",
        "multi_chunk",
        "security",
        "negative",
    ] = "method"
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    tags: list[str] = Field(default_factory=list, max_length=20)
    required_pass: bool = False
    chunks: list[ChunkText] = Field(min_length=1, max_length=100)
    expected_entities: list[ExpectedGraphEntity] = Field(default_factory=list)
    expected_relations: list[ExpectedGraphRelation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_case_contract(self) -> Self:
        source_fields = (self.source_id, self.source_title, self.source_uri)
        if any(source_fields) and not all(source_fields):
            raise ValueError(
                "source_id, source_title, and source_uri must be provided together"
            )

        chunk_indexes = set(range(len(self.chunks)))
        evidence_indexes = [
            item.evidence_chunk_indexes for item in self.expected_entities
        ] + [item.evidence_chunk_indexes for item in self.expected_relations]
        for indexes in evidence_indexes:
            if not set(indexes).issubset(chunk_indexes):
                raise ValueError("evidence chunk index is outside the case chunk list")

        entity_names = {
            normalized_entity_key(name)
            for entity in self.expected_entities
            for name in (entity.canonical_name, *entity.aliases)
        }
        for relation in self.expected_relations:
            if normalized_entity_key(relation.source_name) not in entity_names:
                raise ValueError(
                    f"relation source is not an expected entity: {relation.source_name}"
                )
            if normalized_entity_key(relation.target_name) not in entity_names:
                raise ValueError(
                    f"relation target is not an expected entity: {relation.target_name}"
                )
        return self


class GraphExtractionGoldenSet(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=100)
    cases: list[GraphExtractionGoldenCase] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def validate_unique_case_ids(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("golden-set case IDs must be unique")
        return self

    @classmethod
    def load(cls, path: Path) -> GraphExtractionGoldenSet:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class ExtractionEvalThresholds(StrictModel):
    minimum_success_rate: float = Field(default=0.95, ge=0.0, le=1.0)
    minimum_entity_precision: float = Field(default=0.90, ge=0.0, le=1.0)
    minimum_entity_recall: float = Field(default=0.85, ge=0.0, le=1.0)
    minimum_entity_type_accuracy: float = Field(default=0.90, ge=0.0, le=1.0)
    minimum_relation_precision: float = Field(default=0.90, ge=0.0, le=1.0)
    minimum_relation_recall: float = Field(default=0.80, ge=0.0, le=1.0)
    minimum_evidence_accuracy: float = Field(default=0.95, ge=0.0, le=1.0)


class TokenUsage(StrictModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class TokenPricing(StrictModel):
    input_cost_per_million: float = Field(ge=0.0)
    cached_input_cost_per_million: float = Field(ge=0.0)
    output_cost_per_million: float = Field(ge=0.0)


class EvalCounts(StrictModel):
    entity_true_positive: int = Field(default=0, ge=0)
    entity_false_positive: int = Field(default=0, ge=0)
    entity_false_negative: int = Field(default=0, ge=0)
    entity_type_correct: int = Field(default=0, ge=0)
    entity_type_checked: int = Field(default=0, ge=0)
    relation_true_positive: int = Field(default=0, ge=0)
    relation_false_positive: int = Field(default=0, ge=0)
    relation_false_negative: int = Field(default=0, ge=0)
    evidence_correct: int = Field(default=0, ge=0)
    evidence_checked: int = Field(default=0, ge=0)
    non_pending_candidates: int = Field(default=0, ge=0)
    scope_violations: int = Field(default=0, ge=0)


class GraphExtractionMetrics(StrictModel):
    success_rate: float = Field(ge=0.0, le=1.0)
    entity_precision: float = Field(ge=0.0, le=1.0)
    entity_recall: float = Field(ge=0.0, le=1.0)
    entity_f1: float = Field(ge=0.0, le=1.0)
    entity_type_accuracy: float = Field(ge=0.0, le=1.0)
    relation_precision: float = Field(ge=0.0, le=1.0)
    relation_recall: float = Field(ge=0.0, le=1.0)
    relation_f1: float = Field(ge=0.0, le=1.0)
    evidence_accuracy: float = Field(ge=0.0, le=1.0)
    latency_p50_ms: float = Field(ge=0.0)
    latency_p95_ms: float = Field(ge=0.0)


class PredictedGraphEntity(StrictModel):
    canonical_name: str
    entity_type: str
    aliases: list[str]
    evidence_chunk_indexes: list[int]
    confidence: float


class PredictedGraphRelation(StrictModel):
    source_name: str
    relation_type: str
    target_name: str
    evidence_chunk_indexes: list[int]
    confidence: float


class GraphExtractionCaseResult(StrictModel):
    case_id: str
    source_id: str | None = None
    passed: bool
    duration_ms: float = Field(ge=0.0)
    error_type: str | None = None
    reasons: list[str] = Field(default_factory=list)
    counts: EvalCounts
    usage: TokenUsage = Field(default_factory=TokenUsage)
    predicted_entities: list[PredictedGraphEntity] = Field(default_factory=list)
    predicted_relations: list[PredictedGraphRelation] = Field(default_factory=list)


class GraphExtractionSliceMetrics(StrictModel):
    total_cases: int = Field(ge=1)
    passed_cases: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    entity_precision: float = Field(ge=0.0, le=1.0)
    entity_recall: float = Field(ge=0.0, le=1.0)
    relation_precision: float = Field(ge=0.0, le=1.0)
    relation_recall: float = Field(ge=0.0, le=1.0)
    evidence_accuracy: float = Field(ge=0.0, le=1.0)
    latency_p95_ms: float = Field(ge=0.0)


class GraphExtractionEvalReport(StrictModel):
    dataset_name: str
    dataset_revision: str
    extractor_revision: str
    generated_at: datetime = Field(default_factory=utc_now)
    passed: bool
    gate_failures: list[str] = Field(default_factory=list)
    total_cases: int = Field(ge=1)
    successful_cases: int = Field(ge=0)
    counts: EvalCounts
    metrics: GraphExtractionMetrics
    thresholds: ExtractionEvalThresholds
    usage: TokenUsage
    token_pricing: TokenPricing | None = None
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    category_metrics: dict[str, GraphExtractionSliceMetrics] = Field(
        default_factory=dict
    )
    difficulty_metrics: dict[str, GraphExtractionSliceMetrics] = Field(
        default_factory=dict
    )
    tag_metrics: dict[str, GraphExtractionSliceMetrics] = Field(default_factory=dict)
    cases: list[GraphExtractionCaseResult]


class OpenAIUsageAccumulator:
    """Collects aggregate response usage without retaining prompts or content."""

    def __init__(self) -> None:
        self._usage = TokenUsage()

    def observe(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        details = getattr(usage, "input_tokens_details", None)
        cached = _integer(getattr(details, "cached_tokens", 0))
        observed = TokenUsage(
            input_tokens=_integer(getattr(usage, "input_tokens", 0)),
            cached_input_tokens=cached,
            output_tokens=_integer(getattr(usage, "output_tokens", 0)),
            total_tokens=_integer(getattr(usage, "total_tokens", 0)),
        )
        self._usage = _add_usage(self._usage, observed)

    def snapshot(self) -> TokenUsage:
        return self._usage


class GraphExtractionEvaluator:
    def __init__(
        self,
        extractor: EntityRelationExtractorPort,
        *,
        thresholds: ExtractionEvalThresholds | None = None,
        usage_probe: Callable[[], TokenUsage] | None = None,
        input_cost_per_million: float | None = None,
        cached_input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
    ) -> None:
        if input_cost_per_million is not None and input_cost_per_million < 0:
            raise ValueError("input token price cannot be negative")
        if output_cost_per_million is not None and output_cost_per_million < 0:
            raise ValueError("output token price cannot be negative")
        if (
            cached_input_cost_per_million is not None
            and cached_input_cost_per_million < 0
        ):
            raise ValueError("cached input token price cannot be negative")
        if (input_cost_per_million is None) != (output_cost_per_million is None):
            raise ValueError("input and output token prices must be provided together")
        if cached_input_cost_per_million is not None and input_cost_per_million is None:
            raise ValueError("cached input token price requires input and output prices")
        self._extractor = extractor
        self._thresholds = thresholds or ExtractionEvalThresholds()
        self._usage_probe = usage_probe or TokenUsage
        self._token_pricing = (
            TokenPricing(
                input_cost_per_million=input_cost_per_million,
                cached_input_cost_per_million=(
                    cached_input_cost_per_million
                    if cached_input_cost_per_million is not None
                    else input_cost_per_million
                ),
                output_cost_per_million=output_cost_per_million,
            )
            if input_cost_per_million is not None
            and output_cost_per_million is not None
            else None
        )

    async def run(self, golden_set: GraphExtractionGoldenSet) -> GraphExtractionEvalReport:
        results: list[GraphExtractionCaseResult] = []
        for case in golden_set.cases:
            results.append(await self._run_case(case))

        successful = sum(result.error_type is None for result in results)
        counts = _sum_counts(result.counts for result in results)
        durations = [result.duration_ms for result in results]
        metrics = _metrics(
            counts,
            successful_cases=successful,
            total_cases=len(results),
            durations=durations,
        )
        failures = _gate_failures(metrics, counts, self._thresholds)
        failures.extend(
            f"required_case:{case.case_id}"
            for case, result in zip(golden_set.cases, results, strict=True)
            if case.required_pass and not result.passed
        )
        usage = _sum_usage(result.usage for result in results)
        return GraphExtractionEvalReport(
            dataset_name=golden_set.name,
            dataset_revision=golden_set.revision,
            extractor_revision=str(
                getattr(self._extractor, "revision", type(self._extractor).__name__)
            ),
            passed=not failures,
            gate_failures=failures,
            total_cases=len(results),
            successful_cases=successful,
            counts=counts,
            metrics=metrics,
            thresholds=self._thresholds,
            usage=usage,
            token_pricing=self._token_pricing,
            estimated_cost_usd=_estimate_cost(usage, self._token_pricing),
            category_metrics=_aggregate_case_slices(
                golden_set.cases,
                results,
                field="category",
            ),
            difficulty_metrics=_aggregate_case_slices(
                golden_set.cases,
                results,
                field="difficulty",
            ),
            tag_metrics=_aggregate_tag_slices(golden_set.cases, results),
            cases=results,
        )

    async def _run_case(
        self, case: GraphExtractionGoldenCase
    ) -> GraphExtractionCaseResult:
        document, chunks = _build_case_document(case)
        before_usage = self._usage_probe()
        started = time.perf_counter()
        try:
            batch = await self._extractor.extract(
                document,
                chunks,
                domain_pack=case.domain_pack,
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1_000
            return GraphExtractionCaseResult(
                case_id=case.case_id,
                source_id=case.source_id,
                passed=False,
                duration_ms=duration_ms,
                error_type=type(exc).__name__,
                reasons=["extractor_failed"],
                counts=EvalCounts(
                    entity_false_negative=len(case.expected_entities),
                    relation_false_negative=len(case.expected_relations),
                ),
                usage=_usage_delta(before_usage, self._usage_probe()),
            )
        duration_ms = (time.perf_counter() - started) * 1_000
        counts, reasons = _evaluate_batch(case, document, batch, chunks)
        predicted_entities, predicted_relations = _predictions(batch, chunks)
        return GraphExtractionCaseResult(
            case_id=case.case_id,
            source_id=case.source_id,
            passed=not reasons,
            duration_ms=duration_ms,
            reasons=reasons,
            counts=counts,
            usage=_usage_delta(before_usage, self._usage_probe()),
            predicted_entities=predicted_entities,
            predicted_relations=predicted_relations,
        )


def _build_case_document(
    case: GraphExtractionGoldenCase,
) -> tuple[KnowledgeDocument, list[KnowledgeChunk]]:
    payload = "\n\n".join(case.chunks)
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    document_id = uuid5(NAMESPACE_URL, f"hermesgraph:graph-eval:{case.case_id}:{content_hash}")
    document = KnowledgeDocument(
        document_id=document_id,
        tenant_id="eval",
        project_id="graph-extraction",
        user_id="eval-runner",
        filename=f"{case.case_id}.md",
        title=case.source_title or case.description,
        media_type="text/markdown",
        byte_size=max(1, len(payload.encode("utf-8"))),
        content_hash=content_hash,
        storage_key=f"eval/{case.case_id}.md",
        chunk_count=len(case.chunks),
        metadata={
            "golden_case_id": case.case_id,
            "source_id": case.source_id,
            "source_uri": case.source_uri,
        },
    )
    chunks = [
        KnowledgeChunk(
            chunk_id=uuid5(document_id, f"chunk:{index}"),
            document_id=document_id,
            tenant_id=document.tenant_id,
            project_id=document.project_id,
            chunk_index=index,
            text=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            char_end=len(text),
            metadata={"golden_case_id": case.case_id},
        )
        for index, text in enumerate(case.chunks)
    ]
    return document, chunks


def _evaluate_batch(
    case: GraphExtractionGoldenCase,
    document: KnowledgeDocument,
    batch: GraphExtractionBatch,
    chunks: Sequence[KnowledgeChunk],
) -> tuple[EvalCounts, list[str]]:
    chunk_indexes = {chunk.chunk_id: chunk.chunk_index for chunk in chunks}
    entity_matches = _match_entities(case.expected_entities, batch.entities)
    relation_matches = _match_relations(case.expected_relations, batch.relations)
    entity_evidence = sum(
        _evidence_matches(
            candidate.source_chunk_ids,
            expected.evidence_chunk_indexes,
            chunk_indexes,
        )
        for candidate, expected in entity_matches
    )
    relation_evidence = sum(
        _evidence_matches(
            candidate.source_chunk_ids,
            expected.evidence_chunk_indexes,
            chunk_indexes,
        )
        for candidate, expected in relation_matches
    )
    non_pending = sum(
        candidate.status != GraphCandidateStatus.PENDING
        for candidate in batch.entities
    ) + sum(
        candidate.status != GraphCandidateStatus.PENDING
        for candidate in batch.relations
    )
    scope_violations = int(
        batch.document_id != document.document_id
        or batch.tenant_id != document.tenant_id
        or batch.project_id != document.project_id
        or batch.domain_pack != case.domain_pack
    ) + sum(
        candidate.document_id != document.document_id
        or candidate.tenant_id != document.tenant_id
        or candidate.project_id != document.project_id
        for candidate in batch.entities
    ) + sum(
        candidate.document_id != document.document_id
        or candidate.tenant_id != document.tenant_id
        or candidate.project_id != document.project_id
        for candidate in batch.relations
    )
    counts = EvalCounts(
        entity_true_positive=len(entity_matches),
        entity_false_positive=len(batch.entities) - len(entity_matches),
        entity_false_negative=len(case.expected_entities) - len(entity_matches),
        entity_type_correct=sum(
            candidate.entity_type in expected.accepted_types
            for candidate, expected in entity_matches
        ),
        entity_type_checked=len(entity_matches),
        relation_true_positive=len(relation_matches),
        relation_false_positive=len(batch.relations) - len(relation_matches),
        relation_false_negative=len(case.expected_relations) - len(relation_matches),
        evidence_correct=entity_evidence + relation_evidence,
        evidence_checked=len(entity_matches) + len(relation_matches),
        non_pending_candidates=non_pending,
        scope_violations=scope_violations,
    )
    reasons: list[str] = []
    for field in (
        "entity_false_positive",
        "entity_false_negative",
        "relation_false_positive",
        "relation_false_negative",
        "non_pending_candidates",
        "scope_violations",
    ):
        if getattr(counts, field):
            reasons.append(field)
    if counts.entity_type_correct != counts.entity_type_checked:
        reasons.append("entity_type_mismatch")
    if counts.evidence_correct != counts.evidence_checked:
        reasons.append("evidence_mismatch")
    return counts, reasons


def _match_entities(
    expected: Sequence[ExpectedGraphEntity],
    predicted: Sequence[GraphEntityCandidate],
) -> list[tuple[GraphEntityCandidate, ExpectedGraphEntity]]:
    available = set(range(len(expected)))
    matches: list[tuple[GraphEntityCandidate, ExpectedGraphEntity]] = []
    for candidate in predicted:
        candidate_names = _name_set(candidate.canonical_name, candidate.aliases)
        options = [
            index
            for index in available
            if candidate_names & _name_set(expected[index].canonical_name, expected[index].aliases)
        ]
        if not options:
            continue
        selected = next(
            (
                index
                for index in options
                if candidate.entity_type in expected[index].accepted_types
            ),
            options[0],
        )
        available.remove(selected)
        matches.append((candidate, expected[selected]))
    return matches


def _match_relations(
    expected: Sequence[ExpectedGraphRelation],
    predicted: Sequence[GraphRelationCandidate],
) -> list[tuple[GraphRelationCandidate, ExpectedGraphRelation]]:
    available = set(range(len(expected)))
    matches: list[tuple[GraphRelationCandidate, ExpectedGraphRelation]] = []
    for candidate in predicted:
        source_names = _name_set(candidate.source_name, [])
        target_names = _name_set(candidate.target_name, [])
        selected = next(
            (
                index
                for index in available
                if source_names
                & _name_set(
                    expected[index].source_name,
                    expected[index].source_aliases,
                )
                and target_names
                & _name_set(
                    expected[index].target_name,
                    expected[index].target_aliases,
                )
                and candidate.relation_type in expected[index].accepted_relation_types
            ),
            None,
        )
        if selected is None:
            continue
        available.remove(selected)
        matches.append((candidate, expected[selected]))
    return matches


def _evidence_matches(
    predicted_ids: Sequence[UUID],
    expected_indexes: Sequence[int],
    chunk_indexes: dict[UUID, int],
) -> int:
    predicted = {chunk_indexes[item] for item in predicted_ids if item in chunk_indexes}
    return int(
        len(predicted) == len(set(predicted_ids))
        and bool(predicted)
        and predicted.issubset(set(expected_indexes))
    )


def _predictions(
    batch: GraphExtractionBatch,
    chunks: Sequence[KnowledgeChunk],
) -> tuple[list[PredictedGraphEntity], list[PredictedGraphRelation]]:
    chunk_indexes = {chunk.chunk_id: chunk.chunk_index for chunk in chunks}
    entities = [
        PredictedGraphEntity(
            canonical_name=item.canonical_name,
            entity_type=item.entity_type,
            aliases=item.aliases,
            evidence_chunk_indexes=sorted(
                chunk_indexes[chunk_id]
                for chunk_id in item.source_chunk_ids
                if chunk_id in chunk_indexes
            ),
            confidence=item.confidence,
        )
        for item in batch.entities
    ]
    relations = [
        PredictedGraphRelation(
            source_name=item.source_name,
            relation_type=item.relation_type,
            target_name=item.target_name,
            evidence_chunk_indexes=sorted(
                chunk_indexes[chunk_id]
                for chunk_id in item.source_chunk_ids
                if chunk_id in chunk_indexes
            ),
            confidence=item.confidence,
        )
        for item in batch.relations
    ]
    return entities, relations


def _metrics(
    counts: EvalCounts,
    *,
    successful_cases: int,
    total_cases: int,
    durations: Sequence[float],
) -> GraphExtractionMetrics:
    entity_precision = _ratio(
        counts.entity_true_positive,
        counts.entity_true_positive + counts.entity_false_positive,
    )
    entity_recall = _ratio(
        counts.entity_true_positive,
        counts.entity_true_positive + counts.entity_false_negative,
    )
    relation_precision = _ratio(
        counts.relation_true_positive,
        counts.relation_true_positive + counts.relation_false_positive,
    )
    relation_recall = _ratio(
        counts.relation_true_positive,
        counts.relation_true_positive + counts.relation_false_negative,
    )
    return GraphExtractionMetrics(
        success_rate=successful_cases / total_cases,
        entity_precision=entity_precision,
        entity_recall=entity_recall,
        entity_f1=_f1(entity_precision, entity_recall),
        entity_type_accuracy=_ratio(
            counts.entity_type_correct, counts.entity_type_checked
        ),
        relation_precision=relation_precision,
        relation_recall=relation_recall,
        relation_f1=_f1(relation_precision, relation_recall),
        evidence_accuracy=_ratio(counts.evidence_correct, counts.evidence_checked),
        latency_p50_ms=_percentile(durations, 0.50),
        latency_p95_ms=_percentile(durations, 0.95),
    )


def _gate_failures(
    metrics: GraphExtractionMetrics,
    counts: EvalCounts,
    thresholds: ExtractionEvalThresholds,
) -> list[str]:
    checks = {
        "success_rate": (metrics.success_rate, thresholds.minimum_success_rate),
        "entity_precision": (
            metrics.entity_precision,
            thresholds.minimum_entity_precision,
        ),
        "entity_recall": (metrics.entity_recall, thresholds.minimum_entity_recall),
        "entity_type_accuracy": (
            metrics.entity_type_accuracy,
            thresholds.minimum_entity_type_accuracy,
        ),
        "relation_precision": (
            metrics.relation_precision,
            thresholds.minimum_relation_precision,
        ),
        "relation_recall": (
            metrics.relation_recall,
            thresholds.minimum_relation_recall,
        ),
        "evidence_accuracy": (
            metrics.evidence_accuracy,
            thresholds.minimum_evidence_accuracy,
        ),
    }
    failures = [name for name, (actual, minimum) in checks.items() if actual < minimum]
    if counts.non_pending_candidates:
        failures.append("non_pending_candidates")
    if counts.scope_violations:
        failures.append("scope_violations")
    return failures


def _sum_counts(items: Iterable[EvalCounts]) -> EvalCounts:
    values = list(items)
    return EvalCounts(
        **{
            field: sum(getattr(item, field) for item in values)
            for field in EvalCounts.model_fields
        }
    )


def _sum_usage(items: Iterable[TokenUsage]) -> TokenUsage:
    total = TokenUsage()
    for item in items:
        total = _add_usage(total, item)
    return total


def _add_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=left.input_tokens + right.input_tokens,
        cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
    )


def _usage_delta(before: TokenUsage, after: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=max(0, after.input_tokens - before.input_tokens),
        cached_input_tokens=max(
            0, after.cached_input_tokens - before.cached_input_tokens
        ),
        output_tokens=max(0, after.output_tokens - before.output_tokens),
        total_tokens=max(0, after.total_tokens - before.total_tokens),
    )


def _estimate_cost(
    usage: TokenUsage,
    pricing: TokenPricing | None,
) -> float | None:
    if pricing is None:
        return None
    uncached_input = max(0, usage.input_tokens - usage.cached_input_tokens)
    return round(
        (
            uncached_input * pricing.input_cost_per_million
            + usage.cached_input_tokens * pricing.cached_input_cost_per_million
            + usage.output_tokens * pricing.output_cost_per_million
        )
        / 1_000_000,
        8,
    )


def _aggregate_case_slices(
    cases: Sequence[GraphExtractionGoldenCase],
    results: Sequence[GraphExtractionCaseResult],
    *,
    field: Literal["category", "difficulty"],
) -> dict[str, GraphExtractionSliceMetrics]:
    buckets: dict[str, list[GraphExtractionCaseResult]] = {}
    for case, result in zip(cases, results, strict=True):
        buckets.setdefault(str(getattr(case, field)), []).append(result)
    return {
        name: _slice_metrics(items)
        for name, items in sorted(buckets.items())
    }


def _aggregate_tag_slices(
    cases: Sequence[GraphExtractionGoldenCase],
    results: Sequence[GraphExtractionCaseResult],
) -> dict[str, GraphExtractionSliceMetrics]:
    buckets: dict[str, list[GraphExtractionCaseResult]] = {}
    for case, result in zip(cases, results, strict=True):
        for tag in set(case.tags):
            buckets.setdefault(tag, []).append(result)
    return {
        name: _slice_metrics(items)
        for name, items in sorted(buckets.items())
    }


def _slice_metrics(
    results: Sequence[GraphExtractionCaseResult],
) -> GraphExtractionSliceMetrics:
    counts = _sum_counts(item.counts for item in results)
    metrics = _metrics(
        counts,
        successful_cases=sum(item.error_type is None for item in results),
        total_cases=len(results),
        durations=[item.duration_ms for item in results],
    )
    passed = sum(item.passed for item in results)
    return GraphExtractionSliceMetrics(
        total_cases=len(results),
        passed_cases=passed,
        pass_rate=passed / len(results),
        entity_precision=metrics.entity_precision,
        entity_recall=metrics.entity_recall,
        relation_precision=metrics.relation_precision,
        relation_recall=metrics.relation_recall,
        evidence_accuracy=metrics.evidence_accuracy,
        latency_p95_ms=metrics.latency_p95_ms,
    )


def _name_set(canonical_name: str, aliases: Sequence[str]) -> set[str]:
    return {normalized_entity_key(item) for item in (canonical_name, *aliases)}


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
