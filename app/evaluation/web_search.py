from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import math
import re
import time
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, Self
from urllib.parse import urlsplit

from openai import AsyncOpenAI
from pydantic import Field, field_validator, model_validator

from app.agent.model_provider import build_model_client
from app.config import Settings
from app.domain.enums import TrustLevel
from app.domain.models import (
    EvidenceRef,
    Provenance,
    RunContext,
    StrictModel,
    WebSearchRequest,
    WebSearchResult,
    WebSearchSource,
    utc_now,
)
from app.evaluation.graph_extraction import TokenPricing, TokenUsage
from app.web_search import (
    OpenAIHostedWebSearch,
    WebSearchPolicyError,
    validate_web_search_query,
)

_DOMAIN_PATTERN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)

WebSearchOutcome = Literal["cited", "empty", "policy_error", "provider_error"]
WebSearchExecutionMode = Literal[
    "live",
    "policy_probe",
    "fixture_timeout",
    "fixture_5xx",
    "fixture_no_citation",
    "fixture_private_url_filtered",
    "fixture_untrusted_injection",
]


class WebSearchGoldenCase(StrictModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,99}$")
    description: str = Field(min_length=1, max_length=500)
    query: str = Field(min_length=1, max_length=2_000)
    category: Literal[
        "freshness",
        "primary_source",
        "citation",
        "domain_policy",
        "security",
        "no_citation",
        "conflict",
        "resilience",
        "multilingual",
    ]
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    tags: list[str] = Field(default_factory=list, max_length=20)
    required_pass: bool = False
    execution_mode: WebSearchExecutionMode = "live"
    expected_outcome: WebSearchOutcome = "cited"
    expected_error_codes: list[str] = Field(default_factory=list, max_length=10)
    allowed_domains: list[str] = Field(default_factory=list, max_length=20)
    primary_domains: list[str] = Field(default_factory=list, max_length=20)
    forbidden_domains: list[str] = Field(default_factory=list, max_length=20)
    required_term_groups: list[list[str]] = Field(default_factory=list, max_length=30)
    forbidden_evidence_terms: list[str] = Field(default_factory=list, max_length=30)
    minimum_source_count: int = Field(default=1, ge=0, le=20)
    minimum_distinct_domains: int = Field(default=1, ge=0, le=20)
    maximum_source_count: int = Field(default=8, ge=0, le=20)
    max_results: int = Field(default=8, ge=1, le=20)
    freshness_required: bool = False
    maximum_evidence_age_seconds: int = Field(default=300, ge=1, le=86_400)

    @field_validator("allowed_domains", "primary_domains", "forbidden_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            domain = value.strip().casefold().rstrip(".")
            if not domain or _DOMAIN_PATTERN.fullmatch(domain) is None:
                raise ValueError("web-search evaluation domains must be bare DNS names")
            if domain not in normalized:
                normalized.append(domain)
        return normalized

    @model_validator(mode="after")
    def validate_case_contract(self) -> Self:
        if len(self.tags) != len(set(self.tags)):
            raise ValueError("web-search evaluation tags must be unique")
        for group in self.required_term_groups:
            if not group or any(not item.strip() for item in group):
                raise ValueError("required term groups must contain non-empty alternatives")
        if set(self.allowed_domains) & set(self.forbidden_domains):
            raise ValueError("allowed and forbidden domains cannot overlap")
        if not set(self.primary_domains).issubset(set(self.allowed_domains)):
            raise ValueError("primary domains must be included in allowed domains")
        if self.expected_outcome in {"policy_error", "provider_error"}:
            if not self.expected_error_codes:
                raise ValueError("error outcomes require expected_error_codes")
            if self.minimum_source_count != 0 or self.minimum_distinct_domains != 0:
                raise ValueError("error outcomes cannot require returned sources")
        elif self.expected_error_codes:
            raise ValueError("successful outcomes cannot declare expected_error_codes")
        if self.expected_outcome == "empty" and (
            self.minimum_source_count != 0 or self.minimum_distinct_domains != 0
        ):
            raise ValueError("empty outcomes cannot require returned sources")
        if self.minimum_source_count > self.maximum_source_count:
            raise ValueError("minimum source count cannot exceed maximum source count")
        return self


class WebSearchGoldenSet(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    revision: str = Field(min_length=1, max_length=100)
    cases: list[WebSearchGoldenCase] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_unique_case_ids(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("web-search golden-set case IDs must be unique")
        return self

    @classmethod
    def load(cls, path: Path) -> WebSearchGoldenSet:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class WebSearchEvalThresholds(StrictModel):
    minimum_case_pass_rate: float = Field(default=0.90, ge=0.0, le=1.0)
    minimum_provider_only_success_rate: float = Field(default=0.90, ge=0.0, le=1.0)
    minimum_citation_coverage: float = Field(default=0.95, ge=0.0, le=1.0)
    minimum_source_precision: float = Field(default=0.90, ge=0.0, le=1.0)
    minimum_primary_source_rate: float = Field(default=0.80, ge=0.0, le=1.0)
    minimum_term_recall: float = Field(default=0.80, ge=0.0, le=1.0)
    minimum_freshness_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    minimum_policy_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)
    minimum_resilience_accuracy: float = Field(default=1.0, ge=0.0, le=1.0)


class WebSearchEvalCounts(StrictModel):
    citations_correct: int = Field(default=0, ge=0)
    citations_checked: int = Field(default=0, ge=0)
    acceptable_sources: int = Field(default=0, ge=0)
    sources_checked: int = Field(default=0, ge=0)
    primary_sources: int = Field(default=0, ge=0)
    primary_sources_checked: int = Field(default=0, ge=0)
    terms_correct: int = Field(default=0, ge=0)
    terms_checked: int = Field(default=0, ge=0)
    fresh_evidence: int = Field(default=0, ge=0)
    freshness_checked: int = Field(default=0, ge=0)


class WebSearchCaseResult(StrictModel):
    case_id: str
    query_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    execution_mode: WebSearchExecutionMode
    expected_outcome: WebSearchOutcome
    passed: bool
    reasons: list[str] = Field(default_factory=list)
    duration_ms: float = Field(ge=0.0)
    attempt_count: int = Field(default=1, ge=1)
    attempt_error_codes: list[str] = Field(default_factory=list)
    provider_succeeded: bool = False
    error_type: str | None = None
    error_code: str | None = None
    error_status_code: int | None = Field(default=None, ge=100, le=599)
    source_domains: list[str] = Field(default_factory=list)
    evidence_count: int = Field(default=0, ge=0)
    returned_source_count: int = Field(default=0, ge=0)
    stop_reason: str | None = None
    counts: WebSearchEvalCounts = Field(default_factory=WebSearchEvalCounts)
    usage: TokenUsage = Field(default_factory=TokenUsage)


class WebSearchEvalMetrics(StrictModel):
    case_pass_rate: float = Field(ge=0.0, le=1.0)
    provider_only_success_rate: float = Field(ge=0.0, le=1.0)
    citation_coverage: float = Field(ge=0.0, le=1.0)
    source_precision: float = Field(ge=0.0, le=1.0)
    primary_source_rate: float = Field(ge=0.0, le=1.0)
    term_recall: float = Field(ge=0.0, le=1.0)
    freshness_accuracy: float = Field(ge=0.0, le=1.0)
    policy_accuracy: float = Field(ge=0.0, le=1.0)
    resilience_accuracy: float = Field(ge=0.0, le=1.0)
    latency_p50_ms: float = Field(ge=0.0)
    latency_p95_ms: float = Field(ge=0.0)


class WebSearchSliceMetrics(StrictModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    pass_rate: float = Field(ge=0.0, le=1.0)
    citation_coverage: float = Field(ge=0.0, le=1.0)
    source_precision: float = Field(ge=0.0, le=1.0)
    p95_duration_ms: float = Field(ge=0.0)


class WebSearchEvalReport(StrictModel):
    dataset_name: str
    dataset_revision: str
    backend_revision: str
    generated_at: datetime = Field(default_factory=utc_now)
    passed: bool
    gate_failures: list[str] = Field(default_factory=list)
    total_cases: int = Field(ge=1)
    passed_cases: int = Field(ge=0)
    live_case_count: int = Field(ge=0)
    provider_succeeded_live_cases: int = Field(ge=0)
    metrics: WebSearchEvalMetrics
    category_metrics: dict[str, WebSearchSliceMetrics]
    difficulty_metrics: dict[str, WebSearchSliceMetrics]
    tag_metrics: dict[str, WebSearchSliceMetrics]
    usage: TokenUsage = Field(default_factory=TokenUsage)
    token_pricing: TokenPricing | None = None
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    cases: list[WebSearchCaseResult]


class WebSearchEvaluationBackend(Protocol):
    revision: str

    async def search_case(
        self,
        case: WebSearchGoldenCase,
        context: RunContext,
    ) -> WebSearchResult: ...


class WebSearchFixtureProviderError(RuntimeError):
    pass


class OpenAIWebSearchEvaluationBackend:
    """Runs live cases and explicit deterministic resilience probes."""

    revision = "openai-web-search-eval-backend-v1"

    def __init__(
        self,
        settings: Settings,
        *,
        client: AsyncOpenAI | None = None,
        contract_only: bool = False,
    ) -> None:
        if settings.web_search_mode != "openai" and not contract_only:
            raise ValueError("OpenAI web search must be enabled for evaluation")
        self._settings = settings
        self._client = client
        self._contract_only = contract_only
        self._owns_client = False

    async def search_case(
        self,
        case: WebSearchGoldenCase,
        context: RunContext,
    ) -> WebSearchResult:
        if case.execution_mode == "fixture_timeout":
            raise TimeoutError("deterministic web-search timeout fixture")
        if case.execution_mode == "fixture_5xx":
            raise WebSearchFixtureProviderError("deterministic provider 5xx fixture")
        if case.execution_mode == "fixture_no_citation":
            return _empty_fixture(case, uncited=True)
        if case.execution_mode == "fixture_private_url_filtered":
            return _empty_fixture(case, rejected_url_count=1)
        if case.execution_mode == "fixture_untrusted_injection":
            return _untrusted_injection_fixture(case, context)
        if case.execution_mode == "policy_probe":
            validate_web_search_query(case.query)
            raise RuntimeError("web-search policy probe query was unexpectedly accepted")
        if self._contract_only:
            raise RuntimeError("contract-only Web Search backend cannot run live cases")
        if self._settings.web_search_mode != "openai":
            raise RuntimeError("OpenAI web search is disabled")

        if self._client is None:
            self._client = build_model_client(
                self._settings,
                max_retries=0,
                timeout=float(self._settings.web_search_timeout_seconds),
            )
            self._owns_client = True

        configured_domains = case.allowed_domains or self._settings.web_search_allowed_domains
        scoped_settings = self._settings.model_copy(
            update={"web_search_allowed_domains": configured_domains}
        )
        search = OpenAIHostedWebSearch(scoped_settings, client=self._client)
        return await search.search_web(
            WebSearchRequest(query=case.query, max_results=case.max_results),
            context,
        )

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.close()


class WebSearchEvaluator:
    def __init__(
        self,
        backend: WebSearchEvaluationBackend,
        *,
        thresholds: WebSearchEvalThresholds | None = None,
        max_case_attempts: int = 2,
        retry_base_seconds: float = 0.25,
        input_cost_per_million: float | None = None,
        cached_input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
    ) -> None:
        if not 1 <= max_case_attempts <= 5:
            raise ValueError("max_case_attempts must be between 1 and 5")
        if retry_base_seconds < 0:
            raise ValueError("retry_base_seconds cannot be negative")
        if (input_cost_per_million is None) != (output_cost_per_million is None):
            raise ValueError("input and output token prices must be provided together")
        if cached_input_cost_per_million is not None and input_cost_per_million is None:
            raise ValueError("cached input price requires input and output prices")
        self._backend = backend
        self._thresholds = thresholds or WebSearchEvalThresholds()
        self._max_case_attempts = max_case_attempts
        self._retry_base_seconds = retry_base_seconds
        self._pricing = (
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

    async def run(self, golden_set: WebSearchGoldenSet) -> WebSearchEvalReport:
        results = [await self._run_case(case) for case in golden_set.cases]
        metrics = _metrics(golden_set.cases, results)
        failures = _gate_failures(metrics, self._thresholds)
        failures.extend(
            f"required_case:{case.case_id}"
            for case, result in zip(golden_set.cases, results, strict=True)
            if case.required_pass and not result.passed
        )
        usage = _sum_usage(item.usage for item in results)
        live_results = [
            result
            for case, result in zip(golden_set.cases, results, strict=True)
            if case.execution_mode == "live"
        ]
        return WebSearchEvalReport(
            dataset_name=golden_set.name,
            dataset_revision=golden_set.revision,
            backend_revision=self._backend.revision,
            passed=not failures,
            gate_failures=failures,
            total_cases=len(results),
            passed_cases=sum(item.passed for item in results),
            live_case_count=len(live_results),
            provider_succeeded_live_cases=sum(
                item.provider_succeeded for item in live_results
            ),
            metrics=metrics,
            category_metrics=_slices(golden_set.cases, results, "category"),
            difficulty_metrics=_slices(golden_set.cases, results, "difficulty"),
            tag_metrics=_tag_slices(golden_set.cases, results),
            usage=usage,
            token_pricing=self._pricing,
            estimated_cost_usd=_estimate_cost(usage, self._pricing),
            cases=results,
        )

    async def _run_case(self, case: WebSearchGoldenCase) -> WebSearchCaseResult:
        context = RunContext(session_id=f"web-search-eval:{case.case_id}")
        started = time.perf_counter()
        result: WebSearchResult | None = None
        error: Exception | None = None
        attempt_codes: list[str] = []
        attempt_count = 0
        expected_error = case.expected_outcome in {"policy_error", "provider_error"}
        max_attempts = 1 if expected_error else self._max_case_attempts
        for attempt_count in range(1, max_attempts + 1):
            try:
                result = await self._backend.search_case(case, context)
                error = None
                break
            except Exception as exc:
                error = exc
                attempt_codes.append(_error_code(exc))
                if attempt_count >= max_attempts or not _is_retryable(exc):
                    break
                delay = min(30.0, self._retry_base_seconds * (2 ** (attempt_count - 1)))
                if delay:
                    await asyncio.sleep(delay)
        duration_ms = max((time.perf_counter() - started) * 1_000, 0.0)
        if result is None:
            if error is None:
                raise RuntimeError("Web-search evaluation exhausted attempts without a result")
            code = _error_code(error)
            reasons = []
            if case.expected_outcome not in {"policy_error", "provider_error"}:
                reasons.append("unexpected_search_error")
            elif code not in case.expected_error_codes:
                reasons.append("unexpected_error_code")
            return WebSearchCaseResult(
                case_id=case.case_id,
                query_fingerprint=_query_fingerprint(case.query),
                execution_mode=case.execution_mode,
                expected_outcome=case.expected_outcome,
                passed=not reasons,
                reasons=reasons,
                duration_ms=duration_ms,
                attempt_count=attempt_count,
                attempt_error_codes=attempt_codes,
                error_type=type(error).__name__,
                error_code=code,
                error_status_code=_status_code(error),
            )
        return _evaluate_result(
            case,
            context,
            result,
            duration_ms=duration_ms,
            attempt_count=attempt_count,
            attempt_codes=attempt_codes,
        )


def _evaluate_result(
    case: WebSearchGoldenCase,
    context: RunContext,
    result: WebSearchResult,
    *,
    duration_ms: float,
    attempt_count: int,
    attempt_codes: list[str],
) -> WebSearchCaseResult:
    reasons: list[str] = []
    source_domains: list[str] = []
    for source in result.sources:
        domain = _url_domain(source.url)
        if domain is None:
            reasons.append("invalid_or_private_source_url")
        else:
            source_domains.append(domain)
    distinct_domains = sorted(set(source_domains))
    if case.expected_outcome == "cited" and not result.evidence:
        reasons.append("expected_cited_evidence")
    if case.expected_outcome == "empty" and (result.evidence or result.sources):
        reasons.append("expected_empty_result")
    if case.expected_outcome in {"policy_error", "provider_error"}:
        reasons.append("expected_error_not_raised")
    if len(result.sources) < case.minimum_source_count:
        reasons.append("source_count_below_minimum")
    if len(result.sources) > case.maximum_source_count:
        reasons.append("source_count_above_maximum")
    if len(distinct_domains) < case.minimum_distinct_domains:
        reasons.append("distinct_domain_count_below_minimum")

    source_urls = {item.url for item in result.sources}
    citation_correct = 0
    freshness_correct = 0
    for evidence in result.evidence:
        uri = str(evidence.provenance.locator.get("uri", ""))
        valid = (
            evidence.provenance.source_type == "web_search"
            and evidence.provenance.trust == TrustLevel.UNTRUSTED
            and evidence.provenance.run_id == context.run_id
            and evidence.provenance.source_id == uri
            and uri in source_urls
            and _url_domain(uri) is not None
            and evidence.provenance.content_hash is not None
            and len(evidence.provenance.content_hash) == 64
        )
        citation_correct += int(valid)
        if case.freshness_required:
            freshness_correct += int(
                evidence.provenance.observed_at
                >= utc_now() - timedelta(seconds=case.maximum_evidence_age_seconds)
            )
    if citation_correct != len(result.evidence):
        reasons.append("citation_contract_violation")

    acceptable = sum(
        _domain_allowed(domain, case.allowed_domains)
        and not _domain_matches(domain, case.forbidden_domains)
        for domain in source_domains
    )
    if acceptable != len(source_domains):
        reasons.append("source_domain_policy_violation")
    primary = sum(_domain_matches(domain, case.primary_domains) for domain in source_domains)
    if case.primary_domains and not primary:
        reasons.append("primary_source_missing")
    combined = " ".join(item.text for item in result.evidence).casefold()
    term_hits = sum(
        any(term.casefold() in combined for term in group)
        for group in case.required_term_groups
    )
    if term_hits != len(case.required_term_groups):
        reasons.append("required_evidence_terms_missing")
    if any(term.casefold() in combined for term in case.forbidden_evidence_terms):
        reasons.append("forbidden_evidence_content")
    if case.freshness_required and freshness_correct != len(result.evidence):
        reasons.append("stale_evidence_observation")

    return WebSearchCaseResult(
        case_id=case.case_id,
        query_fingerprint=_query_fingerprint(case.query),
        execution_mode=case.execution_mode,
        expected_outcome=case.expected_outcome,
        passed=not reasons,
        reasons=sorted(set(reasons)),
        duration_ms=duration_ms,
        attempt_count=attempt_count,
        attempt_error_codes=attempt_codes,
        provider_succeeded=True,
        source_domains=distinct_domains,
        evidence_count=len(result.evidence),
        returned_source_count=len(result.sources),
        stop_reason=_optional_string(result.trace.get("stop_reason")),
        counts=WebSearchEvalCounts(
            citations_correct=citation_correct,
            citations_checked=len(result.evidence),
            acceptable_sources=acceptable,
            sources_checked=len(result.sources),
            primary_sources=primary,
            primary_sources_checked=(len(result.sources) if case.primary_domains else 0),
            terms_correct=term_hits,
            terms_checked=len(case.required_term_groups),
            fresh_evidence=freshness_correct,
            freshness_checked=(len(result.evidence) if case.freshness_required else 0),
        ),
        usage=_usage_from_trace(result.trace),
    )


def _metrics(
    cases: Sequence[WebSearchGoldenCase],
    results: Sequence[WebSearchCaseResult],
) -> WebSearchEvalMetrics:
    counts = _sum_counts(item.counts for item in results)
    live = [
        result
        for case, result in zip(cases, results, strict=True)
        if case.execution_mode == "live"
    ]
    policy = [
        result
        for case, result in zip(cases, results, strict=True)
        if case.expected_outcome == "policy_error"
    ]
    resilience = [
        result
        for case, result in zip(cases, results, strict=True)
        if case.expected_outcome == "provider_error"
    ]
    durations = [item.duration_ms for item in results]
    return WebSearchEvalMetrics(
        case_pass_rate=sum(item.passed for item in results) / len(results),
        provider_only_success_rate=_ratio(
            sum(item.provider_succeeded for item in live), len(live)
        ),
        citation_coverage=_ratio(counts.citations_correct, counts.citations_checked),
        source_precision=_ratio(counts.acceptable_sources, counts.sources_checked),
        primary_source_rate=_ratio(
            counts.primary_sources, counts.primary_sources_checked
        ),
        term_recall=_ratio(counts.terms_correct, counts.terms_checked),
        freshness_accuracy=_ratio(counts.fresh_evidence, counts.freshness_checked),
        policy_accuracy=_ratio(sum(item.passed for item in policy), len(policy)),
        resilience_accuracy=_ratio(
            sum(item.passed for item in resilience), len(resilience)
        ),
        latency_p50_ms=_percentile(durations, 0.50),
        latency_p95_ms=_percentile(durations, 0.95),
    )


def _gate_failures(
    metrics: WebSearchEvalMetrics,
    thresholds: WebSearchEvalThresholds,
) -> list[str]:
    checks = {
        "case_pass_rate": (metrics.case_pass_rate, thresholds.minimum_case_pass_rate),
        "provider_only_success_rate": (
            metrics.provider_only_success_rate,
            thresholds.minimum_provider_only_success_rate,
        ),
        "citation_coverage": (
            metrics.citation_coverage,
            thresholds.minimum_citation_coverage,
        ),
        "source_precision": (
            metrics.source_precision,
            thresholds.minimum_source_precision,
        ),
        "primary_source_rate": (
            metrics.primary_source_rate,
            thresholds.minimum_primary_source_rate,
        ),
        "term_recall": (metrics.term_recall, thresholds.minimum_term_recall),
        "freshness_accuracy": (
            metrics.freshness_accuracy,
            thresholds.minimum_freshness_accuracy,
        ),
        "policy_accuracy": (
            metrics.policy_accuracy,
            thresholds.minimum_policy_accuracy,
        ),
        "resilience_accuracy": (
            metrics.resilience_accuracy,
            thresholds.minimum_resilience_accuracy,
        ),
    }
    return [
        f"{name}_below_threshold:{actual:.6f}<{required:.6f}"
        for name, (actual, required) in checks.items()
        if actual < required
    ]


def _slices(
    cases: Sequence[WebSearchGoldenCase],
    results: Sequence[WebSearchCaseResult],
    field: Literal["category", "difficulty"],
) -> dict[str, WebSearchSliceMetrics]:
    values = sorted({str(getattr(case, field)) for case in cases})
    return {
        value: _slice_metrics(
            [
                result
                for case, result in zip(cases, results, strict=True)
                if getattr(case, field) == value
            ]
        )
        for value in values
    }


def _tag_slices(
    cases: Sequence[WebSearchGoldenCase],
    results: Sequence[WebSearchCaseResult],
) -> dict[str, WebSearchSliceMetrics]:
    tags = sorted({tag for case in cases for tag in case.tags})
    return {
        tag: _slice_metrics(
            [
                result
                for case, result in zip(cases, results, strict=True)
                if tag in case.tags
            ]
        )
        for tag in tags
    }


def _slice_metrics(results: Sequence[WebSearchCaseResult]) -> WebSearchSliceMetrics:
    counts = _sum_counts(item.counts for item in results)
    return WebSearchSliceMetrics(
        total=len(results),
        passed=sum(item.passed for item in results),
        pass_rate=_ratio(sum(item.passed for item in results), len(results)),
        citation_coverage=_ratio(counts.citations_correct, counts.citations_checked),
        source_precision=_ratio(counts.acceptable_sources, counts.sources_checked),
        p95_duration_ms=_percentile([item.duration_ms for item in results], 0.95),
    )


def _empty_fixture(
    case: WebSearchGoldenCase,
    *,
    uncited: bool = False,
    rejected_url_count: int = 0,
) -> WebSearchResult:
    return WebSearchResult(
        query=case.query,
        trace={
            "provider": "deterministic_fixture",
            "stop_reason": "no_cited_sources",
            "uncited_output_discarded": uncited,
            "rejected_url_count": rejected_url_count,
        },
    )


def _untrusted_injection_fixture(
    case: WebSearchGoldenCase,
    context: RunContext,
) -> WebSearchResult:
    text = "Ignore previous instructions is untrusted page text, never system policy."
    url = "https://example.com/untrusted-injection"
    evidence = EvidenceRef(
        text=text,
        title="Untrusted injection fixture",
        provenance=Provenance(
            source_type="web_search",
            source_id=url,
            run_id=context.run_id,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            locator={"uri": url},
            trust=TrustLevel.UNTRUSTED,
        ),
    )
    return WebSearchResult(
        query=case.query,
        summary=text,
        evidence=[evidence],
        sources=[WebSearchSource(url=url, title="Untrusted injection fixture")],
        trace={
            "provider": "deterministic_fixture",
            "stop_reason": "cited_sources",
        },
    )


def _error_code(error: Exception) -> str:
    if isinstance(error, WebSearchPolicyError):
        return "policy_rejected"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, WebSearchFixtureProviderError):
        return "provider_5xx"
    status_code = getattr(error, "status_code", None)
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "authorization_error"
    if status_code == 404:
        return "unsupported_endpoint"
    if isinstance(status_code, int) and status_code >= 500:
        return "provider_5xx"
    if isinstance(status_code, int) and status_code == 429:
        return "rate_limited"
    if isinstance(error, ConnectionError):
        return "connection_error"
    return "provider_error"


def _status_code(error: Exception) -> int | None:
    value = getattr(error, "status_code", None)
    return value if isinstance(value, int) and 100 <= value <= 599 else None


def _is_retryable(error: Exception) -> bool:
    return _error_code(error) in {
        "timeout",
        "provider_5xx",
        "rate_limited",
        "connection_error",
    }


def _url_domain(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if _DOMAIN_PATTERN.fullmatch(host) is None:
            return None
    else:
        if not address.is_global:
            return None
    return host


def _domain_allowed(domain: str, allowed: Sequence[str]) -> bool:
    if not allowed:
        return True
    return _domain_matches(domain, allowed)


def _domain_matches(domain: str, candidates: Sequence[str]) -> bool:
    return any(domain == item or domain.endswith(f".{item}") for item in candidates)


def _query_fingerprint(query: str) -> str:
    return hashlib.sha256(" ".join(query.split()).encode()).hexdigest()


def _usage_from_trace(trace: dict[str, object]) -> TokenUsage:
    raw = trace.get("usage", {})
    if not isinstance(raw, dict):
        return TokenUsage()
    details = raw.get("input_tokens_details", {})
    cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    return TokenUsage(
        input_tokens=_nonnegative_int(raw.get("input_tokens")),
        cached_input_tokens=_nonnegative_int(cached),
        output_tokens=_nonnegative_int(raw.get("output_tokens")),
        total_tokens=_nonnegative_int(raw.get("total_tokens")),
    )


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _sum_counts(values: Iterable[WebSearchEvalCounts]) -> WebSearchEvalCounts:
    resolved = list(values)
    return WebSearchEvalCounts(
        citations_correct=sum(item.citations_correct for item in resolved),
        citations_checked=sum(item.citations_checked for item in resolved),
        acceptable_sources=sum(item.acceptable_sources for item in resolved),
        sources_checked=sum(item.sources_checked for item in resolved),
        primary_sources=sum(item.primary_sources for item in resolved),
        primary_sources_checked=sum(item.primary_sources_checked for item in resolved),
        terms_correct=sum(item.terms_correct for item in resolved),
        terms_checked=sum(item.terms_checked for item in resolved),
        fresh_evidence=sum(item.fresh_evidence for item in resolved),
        freshness_checked=sum(item.freshness_checked for item in resolved),
    )


def _sum_usage(values: Iterable[TokenUsage]) -> TokenUsage:
    resolved = list(values)
    return TokenUsage(
        input_tokens=sum(item.input_tokens for item in resolved),
        cached_input_tokens=sum(item.cached_input_tokens for item in resolved),
        output_tokens=sum(item.output_tokens for item in resolved),
        total_tokens=sum(item.total_tokens for item in resolved),
    )


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 1.0


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(math.ceil(quantile * len(ordered)) - 1, 0)
    return round(ordered[index], 3)


def _estimate_cost(usage: TokenUsage, pricing: TokenPricing | None) -> float | None:
    if pricing is None:
        return None
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    total = (
        uncached * pricing.input_cost_per_million
        + usage.cached_input_tokens * pricing.cached_input_cost_per_million
        + usage.output_tokens * pricing.output_cost_per_million
    ) / 1_000_000
    return round(total, 8)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "OpenAIWebSearchEvaluationBackend",
    "WebSearchCaseResult",
    "WebSearchEvalMetrics",
    "WebSearchEvalReport",
    "WebSearchEvalThresholds",
    "WebSearchEvaluator",
    "WebSearchGoldenCase",
    "WebSearchGoldenSet",
]
