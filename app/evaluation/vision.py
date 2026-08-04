from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from app.domain.contracts import VisionAnalyzerPort
from app.domain.models import (
    NormalizedBoundingBox,
    StrictModel,
    VisionAnalysis,
    VisualRegionDraft,
    utc_now,
)
from app.evaluation.graph_extraction import (
    OpenAIUsageAccumulator,
    TokenPricing,
    TokenUsage,
)

VisionCategory = Literal[
    "text",
    "diagram",
    "chart",
    "table",
    "code",
    "interface",
    "object",
    "other",
]


class ExpectedVisionRegion(StrictModel):
    region_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{1,99}$")
    accepted_categories: list[VisionCategory] = Field(min_length=1, max_length=8)
    required_term_groups: list[list[str]] = Field(default_factory=list, max_length=20)
    required_visible_text: list[str] = Field(default_factory=list, max_length=20)
    bounding_box: NormalizedBoundingBox | None = None
    minimum_iou: float = Field(default=0.20, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_terms(self) -> Self:
        _validate_term_groups(self.required_term_groups)
        if any(not item.strip() for item in self.required_visible_text):
            raise ValueError("required visible-text snippets cannot be blank")
        return self


class VisionGoldenCase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,99}$")
    description: str = Field(min_length=1, max_length=500)
    asset_path: str = Field(min_length=1, max_length=1_000)
    asset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_id: str | None = Field(default=None, min_length=1, max_length=300)
    source_title: str | None = Field(default=None, min_length=1, max_length=500)
    source_uri: str | None = Field(default=None, min_length=1, max_length=2_000)
    category: Literal[
        "document",
        "diagram",
        "chart",
        "table",
        "interface",
        "scan",
        "multi_region",
        "security",
        "negative",
    ]
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    tags: list[str] = Field(default_factory=list, max_length=20)
    required_pass: bool = False
    required_title_term_groups: list[list[str]] = Field(default_factory=list, max_length=20)
    required_summary_term_groups: list[list[str]] = Field(default_factory=list, max_length=30)
    required_visible_text: list[str] = Field(default_factory=list, max_length=30)
    required_warning_term_groups: list[list[str]] = Field(default_factory=list, max_length=10)
    forbidden_title_terms: list[str] = Field(default_factory=list, max_length=20)
    forbidden_summary_terms: list[str] = Field(default_factory=list, max_length=20)
    forbidden_visible_text_terms: list[str] = Field(default_factory=list, max_length=20)
    expected_regions: list[ExpectedVisionRegion] = Field(default_factory=list, max_length=50)
    max_regions: int = Field(default=12, ge=0, le=50)

    @model_validator(mode="after")
    def validate_case_contract(self) -> Self:
        asset = Path(self.asset_path)
        if asset.is_absolute() or ".." in asset.parts:
            raise ValueError("vision asset path must be relative and cannot traverse parents")
        if asset.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("vision asset must be PNG, JPEG, or WebP")
        source_fields = (self.source_id, self.source_title, self.source_uri)
        if any(source_fields) and not all(source_fields):
            raise ValueError("source_id, source_title, and source_uri must be provided together")
        for groups in (
            self.required_title_term_groups,
            self.required_summary_term_groups,
            self.required_warning_term_groups,
        ):
            _validate_term_groups(groups)
        for terms in (
            self.required_visible_text,
            self.forbidden_title_terms,
            self.forbidden_summary_terms,
            self.forbidden_visible_text_terms,
        ):
            if any(not item.strip() for item in terms):
                raise ValueError("vision expected and forbidden terms cannot be blank")
        region_ids = [region.region_id for region in self.expected_regions]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("expected vision region IDs must be unique per case")
        if len(self.expected_regions) > self.max_regions:
            raise ValueError("expected regions cannot exceed the case region budget")
        return self


class VisionGoldenSet(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=100)
    cases: list[VisionGoldenCase] = Field(min_length=1, max_length=1_000)

    @classmethod
    def load(cls, path: Path) -> VisionGoldenSet:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    @model_validator(mode="after")
    def validate_unique_case_ids(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("vision golden-set case IDs must be unique")
        return self


class VisionEvalThresholds(StrictModel):
    minimum_success_rate: float = Field(default=0.95, ge=0.0, le=1.0)
    minimum_case_pass_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    minimum_title_term_recall: float = Field(default=0.85, ge=0.0, le=1.0)
    minimum_summary_term_recall: float = Field(default=0.80, ge=0.0, le=1.0)
    minimum_ocr_recall: float = Field(default=0.85, ge=0.0, le=1.0)
    minimum_region_recall: float = Field(default=0.80, ge=0.0, le=1.0)
    minimum_region_category_accuracy: float = Field(default=0.90, ge=0.0, le=1.0)
    minimum_region_text_recall: float = Field(default=0.80, ge=0.0, le=1.0)
    minimum_bbox_accuracy: float = Field(default=0.70, ge=0.0, le=1.0)
    minimum_forbidden_content_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)


class VisionEvalCounts(StrictModel):
    title_terms_correct: int = Field(default=0, ge=0)
    title_terms_checked: int = Field(default=0, ge=0)
    summary_terms_correct: int = Field(default=0, ge=0)
    summary_terms_checked: int = Field(default=0, ge=0)
    ocr_snippets_correct: int = Field(default=0, ge=0)
    ocr_snippets_checked: int = Field(default=0, ge=0)
    warning_terms_correct: int = Field(default=0, ge=0)
    warning_terms_checked: int = Field(default=0, ge=0)
    regions_correct: int = Field(default=0, ge=0)
    regions_checked: int = Field(default=0, ge=0)
    region_categories_correct: int = Field(default=0, ge=0)
    region_categories_checked: int = Field(default=0, ge=0)
    region_text_terms_correct: int = Field(default=0, ge=0)
    region_text_terms_checked: int = Field(default=0, ge=0)
    bounding_boxes_correct: int = Field(default=0, ge=0)
    bounding_boxes_checked: int = Field(default=0, ge=0)
    forbidden_terms_absent: int = Field(default=0, ge=0)
    forbidden_terms_checked: int = Field(default=0, ge=0)
    region_budget_violations: int = Field(default=0, ge=0)


class VisionEvalMetrics(StrictModel):
    success_rate: float = Field(ge=0.0, le=1.0)
    case_pass_rate: float = Field(ge=0.0, le=1.0)
    title_term_recall: float = Field(ge=0.0, le=1.0)
    summary_term_recall: float = Field(ge=0.0, le=1.0)
    ocr_recall: float = Field(ge=0.0, le=1.0)
    warning_term_recall: float = Field(ge=0.0, le=1.0)
    region_recall: float = Field(ge=0.0, le=1.0)
    region_category_accuracy: float = Field(ge=0.0, le=1.0)
    region_text_recall: float = Field(ge=0.0, le=1.0)
    bbox_accuracy: float = Field(ge=0.0, le=1.0)
    forbidden_content_accuracy: float = Field(ge=0.0, le=1.0)
    latency_p50_ms: float = Field(ge=0.0)
    latency_p95_ms: float = Field(ge=0.0)


class VisionCaseResult(StrictModel):
    case_id: str
    source_id: str | None = None
    passed: bool
    duration_ms: float = Field(ge=0.0)
    attempt_count: int = Field(default=1, ge=0)
    attempt_errors: list[str] = Field(default_factory=list)
    error_type: str | None = None
    error_message: str | None = None
    reasons: list[str] = Field(default_factory=list)
    counts: VisionEvalCounts
    usage: TokenUsage = Field(default_factory=TokenUsage)
    analysis: VisionAnalysis | None = None
    region_matches: dict[str, int | None] = Field(default_factory=dict)
    region_ious: dict[str, float | None] = Field(default_factory=dict)


class VisionSliceMetrics(StrictModel):
    total_cases: int = Field(ge=1)
    passed_cases: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    ocr_recall: float = Field(ge=0.0, le=1.0)
    region_recall: float = Field(ge=0.0, le=1.0)
    bbox_accuracy: float = Field(ge=0.0, le=1.0)
    forbidden_content_accuracy: float = Field(ge=0.0, le=1.0)
    latency_p95_ms: float = Field(ge=0.0)


class VisionEvalReport(StrictModel):
    dataset_name: str
    dataset_revision: str
    analyzer_revision: str
    generated_at: datetime = Field(default_factory=utc_now)
    passed: bool
    gate_failures: list[str] = Field(default_factory=list)
    total_cases: int = Field(ge=1)
    successful_cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    model_attempts: int = Field(ge=0)
    recovered_cases: int = Field(ge=0)
    counts: VisionEvalCounts
    metrics: VisionEvalMetrics
    thresholds: VisionEvalThresholds
    usage: TokenUsage
    token_pricing: TokenPricing | None = None
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    category_metrics: dict[str, VisionSliceMetrics] = Field(default_factory=dict)
    difficulty_metrics: dict[str, VisionSliceMetrics] = Field(default_factory=dict)
    tag_metrics: dict[str, VisionSliceMetrics] = Field(default_factory=dict)
    cases: list[VisionCaseResult]


class VisionEvaluator:
    def __init__(
        self,
        analyzer: VisionAnalyzerPort,
        *,
        asset_root: Path,
        thresholds: VisionEvalThresholds | None = None,
        usage_probe: Callable[[], TokenUsage] | None = None,
        input_cost_per_million: float | None = None,
        cached_input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
        max_asset_bytes: int = 20_000_000,
        max_case_attempts: int = 1,
        retry_base_seconds: float = 1.0,
    ) -> None:
        if max_asset_bytes < 1:
            raise ValueError("max asset bytes must be positive")
        if (input_cost_per_million is None) != (output_cost_per_million is None):
            raise ValueError("input and output token prices must be provided together")
        if cached_input_cost_per_million is not None and input_cost_per_million is None:
            raise ValueError("cached input price requires input and output prices")
        prices = (
            input_cost_per_million,
            cached_input_cost_per_million,
            output_cost_per_million,
        )
        if any(item is not None and item < 0 for item in prices):
            raise ValueError("token prices cannot be negative")
        if not 1 <= max_case_attempts <= 5:
            raise ValueError("Vision max case attempts must be between 1 and 5")
        if not 0.0 <= retry_base_seconds <= 30.0:
            raise ValueError("Vision retry base seconds must be between 0 and 30")
        self._analyzer = analyzer
        self._asset_root = asset_root.resolve()
        self._thresholds = thresholds or VisionEvalThresholds()
        self._usage_probe = usage_probe or TokenUsage
        self._max_asset_bytes = max_asset_bytes
        self._max_case_attempts = max_case_attempts
        self._retry_base_seconds = retry_base_seconds
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
            if input_cost_per_million is not None and output_cost_per_million is not None
            else None
        )

    async def run(self, golden_set: VisionGoldenSet) -> VisionEvalReport:
        results: list[VisionCaseResult] = []
        for case in golden_set.cases:
            results.append(await self._run_case(case))
        counts = _sum_counts(result.counts for result in results)
        successful = sum(result.error_type is None for result in results)
        passed_cases = sum(result.passed for result in results)
        metrics = _metrics(
            counts,
            successful_cases=successful,
            passed_cases=passed_cases,
            total_cases=len(results),
            durations=[result.duration_ms for result in results],
        )
        gate_failures = _gate_failures(metrics, counts, self._thresholds)
        gate_failures.extend(
            f"required_case:{case.case_id}"
            for case, result in zip(golden_set.cases, results, strict=True)
            if case.required_pass and not result.passed
        )
        usage = _sum_usage(result.usage for result in results)
        return VisionEvalReport(
            dataset_name=golden_set.name,
            dataset_revision=golden_set.revision,
            analyzer_revision=str(
                getattr(self._analyzer, "revision", type(self._analyzer).__name__)
            ),
            passed=not gate_failures,
            gate_failures=gate_failures,
            total_cases=len(results),
            successful_cases=successful,
            passed_cases=passed_cases,
            model_attempts=sum(result.attempt_count for result in results),
            recovered_cases=sum(
                result.error_type is None and result.attempt_count > 1 for result in results
            ),
            counts=counts,
            metrics=metrics,
            thresholds=self._thresholds,
            usage=usage,
            token_pricing=self._token_pricing,
            estimated_cost_usd=_estimate_cost(usage, self._token_pricing),
            category_metrics=_aggregate_case_slices(golden_set.cases, results, field="category"),
            difficulty_metrics=_aggregate_case_slices(
                golden_set.cases, results, field="difficulty"
            ),
            tag_metrics=_aggregate_tag_slices(golden_set.cases, results),
            cases=results,
        )

    async def _run_case(self, case: VisionGoldenCase) -> VisionCaseResult:
        before_usage = self._usage_probe()
        started = time.perf_counter()
        try:
            asset_path = self._resolve_asset_path(case.asset_path)
            size = asset_path.stat().st_size
            if size > self._max_asset_bytes:
                raise ValueError("vision evaluation asset exceeds byte budget")
            content = await asyncio.to_thread(asset_path.read_bytes)
            if hashlib.sha256(content).hexdigest() != case.asset_sha256:
                raise ValueError("vision evaluation asset hash does not match golden set")
        except Exception as exc:
            duration_ms = (time.perf_counter() - started) * 1_000
            return VisionCaseResult(
                case_id=case.case_id,
                source_id=case.source_id,
                passed=False,
                duration_ms=duration_ms,
                attempt_count=0,
                error_type=type(exc).__name__,
                error_message=str(exc)[:1_000],
                reasons=["analysis_failed"],
                counts=_failure_counts(case),
                usage=_usage_delta(before_usage, self._usage_probe()),
            )
        attempt_errors: list[str] = []
        analysis: VisionAnalysis | None = None
        last_error: Exception | None = None
        attempt_count = 0
        for attempt_count in range(1, self._max_case_attempts + 1):
            try:
                analysis = await self._analyzer.analyze(
                    content,
                    media_type=_media_type(asset_path),
                    filename=asset_path.name,
                )
                break
            except Exception as exc:
                last_error = exc
                attempt_errors.append(f"{type(exc).__name__}: {str(exc)[:500]}")
                if attempt_count >= self._max_case_attempts or not _is_retryable(exc):
                    break
                delay = min(
                    30.0,
                    self._retry_base_seconds * (2 ** (attempt_count - 1)),
                )
                if delay:
                    await asyncio.sleep(delay)
        if analysis is None:
            if last_error is None:
                raise RuntimeError("Vision evaluation exhausted attempts without a result")
            duration_ms = (time.perf_counter() - started) * 1_000
            return VisionCaseResult(
                case_id=case.case_id,
                source_id=case.source_id,
                passed=False,
                duration_ms=duration_ms,
                attempt_count=attempt_count,
                attempt_errors=attempt_errors,
                error_type=type(last_error).__name__,
                error_message=str(last_error)[:1_000],
                reasons=["analysis_failed"],
                counts=_failure_counts(case),
                usage=_usage_delta(before_usage, self._usage_probe()),
            )
        duration_ms = (time.perf_counter() - started) * 1_000
        counts, reasons, matches, ious = _evaluate_analysis(case, analysis)
        return VisionCaseResult(
            case_id=case.case_id,
            source_id=case.source_id,
            passed=not reasons,
            duration_ms=duration_ms,
            attempt_count=attempt_count,
            attempt_errors=attempt_errors,
            reasons=reasons,
            counts=counts,
            usage=_usage_delta(before_usage, self._usage_probe()),
            analysis=analysis,
            region_matches=matches,
            region_ious=ious,
        )

    def _resolve_asset_path(self, relative_path: str) -> Path:
        path = (self._asset_root / relative_path).resolve()
        if path != self._asset_root and self._asset_root not in path.parents:
            raise ValueError("vision asset resolved outside the dataset directory")
        if not path.is_file():
            raise FileNotFoundError(f"vision evaluation asset not found: {relative_path}")
        return path


def _evaluate_analysis(
    case: VisionGoldenCase,
    analysis: VisionAnalysis,
) -> tuple[VisionEvalCounts, list[str], dict[str, int | None], dict[str, float | None]]:
    title_correct = _count_term_groups(analysis.title, case.required_title_term_groups)
    summary_correct = _count_term_groups(analysis.summary, case.required_summary_term_groups)
    ocr_correct = sum(
        _contains(analysis.visible_text, snippet) for snippet in case.required_visible_text
    )
    warnings_text = " ".join(analysis.warnings)
    warning_correct = _count_term_groups(warnings_text, case.required_warning_term_groups)
    forbidden_checks = [
        *(not _contains(analysis.title, term) for term in case.forbidden_title_terms),
        *(not _contains(analysis.summary, term) for term in case.forbidden_summary_terms),
        *(not _contains(analysis.visible_text, term) for term in case.forbidden_visible_text_terms),
    ]
    region_counts, matches, ious = _evaluate_regions(case.expected_regions, analysis.regions)
    counts = VisionEvalCounts(
        title_terms_correct=title_correct,
        title_terms_checked=len(case.required_title_term_groups),
        summary_terms_correct=summary_correct,
        summary_terms_checked=len(case.required_summary_term_groups),
        ocr_snippets_correct=ocr_correct,
        ocr_snippets_checked=len(case.required_visible_text),
        warning_terms_correct=warning_correct,
        warning_terms_checked=len(case.required_warning_term_groups),
        forbidden_terms_absent=sum(forbidden_checks),
        forbidden_terms_checked=len(forbidden_checks),
        region_budget_violations=int(len(analysis.regions) > case.max_regions),
        **region_counts,
    )
    reasons: list[str] = []
    checks = {
        "title_term_mismatch": (
            counts.title_terms_correct,
            counts.title_terms_checked,
        ),
        "summary_term_mismatch": (
            counts.summary_terms_correct,
            counts.summary_terms_checked,
        ),
        "ocr_mismatch": (counts.ocr_snippets_correct, counts.ocr_snippets_checked),
        "warning_mismatch": (
            counts.warning_terms_correct,
            counts.warning_terms_checked,
        ),
        "region_mismatch": (counts.regions_correct, counts.regions_checked),
        "region_category_mismatch": (
            counts.region_categories_correct,
            counts.region_categories_checked,
        ),
        "region_text_mismatch": (
            counts.region_text_terms_correct,
            counts.region_text_terms_checked,
        ),
        "bounding_box_mismatch": (
            counts.bounding_boxes_correct,
            counts.bounding_boxes_checked,
        ),
        "forbidden_content": (
            counts.forbidden_terms_absent,
            counts.forbidden_terms_checked,
        ),
    }
    reasons.extend(reason for reason, (correct, checked) in checks.items() if correct != checked)
    if counts.region_budget_violations:
        reasons.append("region_budget_violation")
    return counts, reasons, matches, ious


def _evaluate_regions(
    expected: Sequence[ExpectedVisionRegion],
    predicted: Sequence[VisualRegionDraft],
) -> tuple[dict[str, int], dict[str, int | None], dict[str, float | None]]:
    available = set(range(len(predicted)))
    matches: dict[str, int | None] = {}
    ious: dict[str, float | None] = {}
    region_correct = 0
    category_correct = 0
    text_correct = 0
    text_checked = 0
    bbox_correct = 0
    bbox_checked = 0
    for item in expected:
        selected = _select_region(item, predicted, available)
        matches[item.region_id] = selected
        category_ok = False
        all_text_ok = False
        if selected is None:
            text_checked += len(item.required_term_groups) + len(item.required_visible_text)
            if item.bounding_box is not None:
                bbox_checked += 1
            ious[item.region_id] = None
            continue
        available.remove(selected)
        candidate = predicted[selected]
        category_ok = candidate.category in item.accepted_categories
        category_correct += int(category_ok)
        combined = " ".join([candidate.label, candidate.description, candidate.visible_text])
        term_hits = _count_term_groups(combined, item.required_term_groups)
        visible_hits = sum(
            _contains(candidate.visible_text, snippet) for snippet in item.required_visible_text
        )
        checked = len(item.required_term_groups) + len(item.required_visible_text)
        correct = term_hits + visible_hits
        text_checked += checked
        text_correct += correct
        all_text_ok = correct == checked
        if item.bounding_box is not None:
            bbox_checked += 1
            iou = (
                _bbox_iou(item.bounding_box, candidate.bounding_box)
                if candidate.bounding_box is not None
                else 0.0
            )
            ious[item.region_id] = iou
            bbox_correct += int(iou >= item.minimum_iou)
        else:
            ious[item.region_id] = None
        region_correct += int(category_ok and all_text_ok)
    return (
        {
            "regions_correct": region_correct,
            "regions_checked": len(expected),
            "region_categories_correct": category_correct,
            "region_categories_checked": len(expected),
            "region_text_terms_correct": text_correct,
            "region_text_terms_checked": text_checked,
            "bounding_boxes_correct": bbox_correct,
            "bounding_boxes_checked": bbox_checked,
        },
        matches,
        ious,
    )


def _select_region(
    expected: ExpectedVisionRegion,
    predicted: Sequence[VisualRegionDraft],
    available: set[int],
) -> int | None:
    best_index: int | None = None
    best_score = -1.0
    for index in available:
        candidate = predicted[index]
        combined = " ".join([candidate.label, candidate.description, candidate.visible_text])
        term_hits = _count_term_groups(combined, expected.required_term_groups)
        visible_hits = sum(
            _contains(candidate.visible_text, snippet) for snippet in expected.required_visible_text
        )
        category_bonus = int(candidate.category in expected.accepted_categories)
        iou = (
            _bbox_iou(expected.bounding_box, candidate.bounding_box)
            if expected.bounding_box is not None and candidate.bounding_box is not None
            else 0.0
        )
        score = term_hits * 3 + visible_hits * 3 + category_bonus * 2 + iou
        if score > best_score:
            best_index = index
            best_score = score
    return best_index if best_score > 0 else None


def _failure_counts(case: VisionGoldenCase) -> VisionEvalCounts:
    region_text_checked = sum(
        len(region.required_term_groups) + len(region.required_visible_text)
        for region in case.expected_regions
    )
    return VisionEvalCounts(
        title_terms_checked=len(case.required_title_term_groups),
        summary_terms_checked=len(case.required_summary_term_groups),
        ocr_snippets_checked=len(case.required_visible_text),
        warning_terms_checked=len(case.required_warning_term_groups),
        regions_checked=len(case.expected_regions),
        region_categories_checked=len(case.expected_regions),
        region_text_terms_checked=region_text_checked,
        bounding_boxes_checked=sum(
            region.bounding_box is not None for region in case.expected_regions
        ),
        forbidden_terms_checked=(
            len(case.forbidden_title_terms)
            + len(case.forbidden_summary_terms)
            + len(case.forbidden_visible_text_terms)
        ),
    )


def _metrics(
    counts: VisionEvalCounts,
    *,
    successful_cases: int,
    passed_cases: int,
    total_cases: int,
    durations: Sequence[float],
) -> VisionEvalMetrics:
    return VisionEvalMetrics(
        success_rate=successful_cases / total_cases,
        case_pass_rate=passed_cases / total_cases,
        title_term_recall=_ratio(counts.title_terms_correct, counts.title_terms_checked),
        summary_term_recall=_ratio(counts.summary_terms_correct, counts.summary_terms_checked),
        ocr_recall=_ratio(counts.ocr_snippets_correct, counts.ocr_snippets_checked),
        warning_term_recall=_ratio(counts.warning_terms_correct, counts.warning_terms_checked),
        region_recall=_ratio(counts.regions_correct, counts.regions_checked),
        region_category_accuracy=_ratio(
            counts.region_categories_correct, counts.region_categories_checked
        ),
        region_text_recall=_ratio(
            counts.region_text_terms_correct, counts.region_text_terms_checked
        ),
        bbox_accuracy=_ratio(counts.bounding_boxes_correct, counts.bounding_boxes_checked),
        forbidden_content_accuracy=_ratio(
            counts.forbidden_terms_absent, counts.forbidden_terms_checked
        ),
        latency_p50_ms=_percentile(durations, 0.50),
        latency_p95_ms=_percentile(durations, 0.95),
    )


def _gate_failures(
    metrics: VisionEvalMetrics,
    counts: VisionEvalCounts,
    thresholds: VisionEvalThresholds,
) -> list[str]:
    checks = {
        "success_rate": (metrics.success_rate, thresholds.minimum_success_rate),
        "case_pass_rate": (
            metrics.case_pass_rate,
            thresholds.minimum_case_pass_rate,
        ),
        "title_term_recall": (
            metrics.title_term_recall,
            thresholds.minimum_title_term_recall,
        ),
        "summary_term_recall": (
            metrics.summary_term_recall,
            thresholds.minimum_summary_term_recall,
        ),
        "ocr_recall": (metrics.ocr_recall, thresholds.minimum_ocr_recall),
        "region_recall": (
            metrics.region_recall,
            thresholds.minimum_region_recall,
        ),
        "region_category_accuracy": (
            metrics.region_category_accuracy,
            thresholds.minimum_region_category_accuracy,
        ),
        "region_text_recall": (
            metrics.region_text_recall,
            thresholds.minimum_region_text_recall,
        ),
        "bbox_accuracy": (
            metrics.bbox_accuracy,
            thresholds.minimum_bbox_accuracy,
        ),
        "forbidden_content_accuracy": (
            metrics.forbidden_content_accuracy,
            thresholds.minimum_forbidden_content_accuracy,
        ),
    }
    failures = [name for name, (actual, minimum) in checks.items() if actual < minimum]
    if counts.region_budget_violations:
        failures.append("region_budget_violations")
    return failures


def _aggregate_case_slices(
    cases: Sequence[VisionGoldenCase],
    results: Sequence[VisionCaseResult],
    *,
    field: Literal["category", "difficulty"],
) -> dict[str, VisionSliceMetrics]:
    buckets: dict[str, list[VisionCaseResult]] = {}
    for case, result in zip(cases, results, strict=True):
        buckets.setdefault(str(getattr(case, field)), []).append(result)
    return {name: _slice_metrics(items) for name, items in sorted(buckets.items())}


def _aggregate_tag_slices(
    cases: Sequence[VisionGoldenCase],
    results: Sequence[VisionCaseResult],
) -> dict[str, VisionSliceMetrics]:
    buckets: dict[str, list[VisionCaseResult]] = {}
    for case, result in zip(cases, results, strict=True):
        for tag in set(case.tags):
            buckets.setdefault(tag, []).append(result)
    return {name: _slice_metrics(items) for name, items in sorted(buckets.items())}


def _slice_metrics(results: Sequence[VisionCaseResult]) -> VisionSliceMetrics:
    counts = _sum_counts(result.counts for result in results)
    return VisionSliceMetrics(
        total_cases=len(results),
        passed_cases=sum(result.passed for result in results),
        pass_rate=sum(result.passed for result in results) / len(results),
        ocr_recall=_ratio(counts.ocr_snippets_correct, counts.ocr_snippets_checked),
        region_recall=_ratio(counts.regions_correct, counts.regions_checked),
        bbox_accuracy=_ratio(counts.bounding_boxes_correct, counts.bounding_boxes_checked),
        forbidden_content_accuracy=_ratio(
            counts.forbidden_terms_absent, counts.forbidden_terms_checked
        ),
        latency_p95_ms=_percentile([result.duration_ms for result in results], 0.95),
    )


def _sum_counts(items: Iterable[VisionEvalCounts]) -> VisionEvalCounts:
    values = list(items)
    return VisionEvalCounts(
        **{
            field: sum(getattr(item, field) for item in values)
            for field in VisionEvalCounts.model_fields
        }
    )


def _sum_usage(items: Iterable[TokenUsage]) -> TokenUsage:
    total = TokenUsage()
    for item in items:
        total = TokenUsage(
            input_tokens=total.input_tokens + item.input_tokens,
            cached_input_tokens=(total.cached_input_tokens + item.cached_input_tokens),
            output_tokens=total.output_tokens + item.output_tokens,
            total_tokens=total.total_tokens + item.total_tokens,
        )
    return total


def _usage_delta(before: TokenUsage, after: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=max(0, after.input_tokens - before.input_tokens),
        cached_input_tokens=max(0, after.cached_input_tokens - before.cached_input_tokens),
        output_tokens=max(0, after.output_tokens - before.output_tokens),
        total_tokens=max(0, after.total_tokens - before.total_tokens),
    )


def _estimate_cost(usage: TokenUsage, pricing: TokenPricing | None) -> float | None:
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


def _validate_term_groups(groups: Sequence[Sequence[str]]) -> None:
    if any(not group or any(not term.strip() for term in group) for group in groups):
        raise ValueError("vision term groups must contain non-blank alternatives")


def _count_term_groups(text: str, groups: Sequence[Sequence[str]]) -> int:
    return sum(any(_contains(text, term) for term in group) for group in groups)


def _contains(text: str, term: str) -> bool:
    return _normalize_search(term) in _normalize_search(text)


def _normalize_search(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", normalized, flags=re.UNICODE)


def _bbox_iou(
    expected: NormalizedBoundingBox,
    predicted: NormalizedBoundingBox | None,
) -> float:
    if predicted is None:
        return 0.0
    x_min = max(expected.x_min, predicted.x_min)
    y_min = max(expected.y_min, predicted.y_min)
    x_max = min(expected.x_max, predicted.x_max)
    y_max = min(expected.y_max, predicted.y_max)
    intersection = max(0.0, x_max - x_min) * max(0.0, y_max - y_min)
    expected_area = (expected.x_max - expected.x_min) * (expected.y_max - expected.y_min)
    predicted_area = (predicted.x_max - predicted.x_min) * (predicted.y_max - predicted.y_min)
    union = expected_area + predicted_area - intersection
    return intersection / union if union > 0 else 0.0


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


def _media_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    raise ValueError(f"unsupported vision evaluation asset: {suffix}")


def _is_retryable(exc: Exception) -> bool:
    return isinstance(exc, (ConnectionError, TimeoutError)) or type(exc).__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
    }


__all__ = [
    "ExpectedVisionRegion",
    "OpenAIUsageAccumulator",
    "VisionEvalReport",
    "VisionEvalThresholds",
    "VisionEvaluator",
    "VisionGoldenCase",
    "VisionGoldenSet",
]
