from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain.enums import TrustLevel
from app.domain.models import (
    EvidenceRef,
    Provenance,
    RunContext,
    WebSearchResult,
    WebSearchSource,
)
from app.evaluation.web_search import (
    OpenAIWebSearchEvaluationBackend,
    WebSearchEvalThresholds,
    WebSearchEvaluator,
    WebSearchGoldenCase,
    WebSearchGoldenSet,
)
from app.evaluation.web_search_cli import _select_cases, _write_atomic


class _ResultBackend:
    revision = "web-search-test-backend-v1"

    def __init__(self, *, private_url: bool = False, fail_once: bool = False) -> None:
        self.private_url = private_url
        self.fail_once = fail_once
        self.calls = 0

    async def search_case(
        self,
        case: WebSearchGoldenCase,
        context: RunContext,
    ) -> WebSearchResult:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise ConnectionError("temporary upstream disconnect")
        urls = (
            ["http://127.0.0.1/private"]
            if self.private_url
            else [
                "https://platform.openai.com/docs/guides/tools-web-search",
                "https://openai.com/index/new-tools-for-building-agents/",
            ]
        )
        text = "The Responses API web search tool returns native URL citations."
        evidence = [
            EvidenceRef(
                text=text,
                title=f"Source {index}",
                provenance=Provenance(
                    source_type="web_search",
                    source_id=url,
                    run_id=context.run_id,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    locator={"uri": url},
                    trust=TrustLevel.UNTRUSTED,
                ),
            )
            for index, url in enumerate(urls)
        ]
        return WebSearchResult(
            query=case.query,
            summary=text,
            evidence=evidence,
            sources=[
                WebSearchSource(url=url, title=f"Source {index}")
                for index, url in enumerate(urls)
            ],
            trace={
                "stop_reason": "cited_sources",
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 10},
                    "output_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )


class _EmptyBackend:
    revision = "web-search-empty-backend-v1"

    async def search_case(
        self,
        case: WebSearchGoldenCase,
        context: RunContext,
    ) -> WebSearchResult:
        del context
        return WebSearchResult(query=case.query)


def _live_case(*, required_pass: bool = False) -> WebSearchGoldenCase:
    return WebSearchGoldenCase(
        case_id="openai_docs_fixture",
        description="Official sources with native URL citations",
        query="How does current OpenAI web search cite sources?",
        category="citation",
        difficulty="medium",
        tags=["official", "fixture"],
        required_pass=required_pass,
        allowed_domains=["openai.com"],
        primary_domains=["openai.com"],
        required_term_groups=[["Responses API"], ["web search"], ["citation"]],
        minimum_source_count=2,
        minimum_distinct_domains=1,
        freshness_required=True,
    )


@pytest.mark.asyncio
async def test_contract_suite_is_offline_passes_and_redacts_queries() -> None:
    fixture = WebSearchGoldenSet.load(
        Path("examples/evaluation/web_search_golden.json")
    )
    contract = fixture.model_copy(
        update={
            "revision": f"{fixture.revision}+contract-test",
            "cases": [
                case for case in fixture.cases if case.execution_mode != "live"
            ],
        }
    )
    settings = Settings(web_search_mode="disabled")
    backend = OpenAIWebSearchEvaluationBackend(
        settings,
        contract_only=True,
    )

    report = await WebSearchEvaluator(backend).run(contract)

    assert report.passed is True
    assert report.total_cases == 6
    assert report.passed_cases == 6
    assert report.live_case_count == 0
    assert report.metrics.policy_accuracy == 1.0
    assert report.metrics.resilience_accuracy == 1.0
    assert report.metrics.citation_coverage == 1.0
    payload = report.model_dump_json()
    for case in contract.cases:
        assert case.query not in payload
        assert hashlib.sha256(" ".join(case.query.split()).encode()).hexdigest() in payload


@pytest.mark.asyncio
async def test_evaluator_scores_citations_sources_slices_usage_and_cost() -> None:
    golden = WebSearchGoldenSet(
        name="web search fixture",
        revision="v1",
        cases=[_live_case()],
    )

    report = await WebSearchEvaluator(
        _ResultBackend(),
        input_cost_per_million=2.0,
        cached_input_cost_per_million=1.0,
        output_cost_per_million=10.0,
    ).run(golden)

    assert report.passed is True
    assert report.metrics.case_pass_rate == 1.0
    assert report.metrics.provider_only_success_rate == 1.0
    assert report.metrics.citation_coverage == 1.0
    assert report.metrics.source_precision == 1.0
    assert report.metrics.primary_source_rate == 1.0
    assert report.metrics.term_recall == 1.0
    assert report.metrics.freshness_accuracy == 1.0
    assert report.category_metrics["citation"].pass_rate == 1.0
    assert report.difficulty_metrics["medium"].citation_coverage == 1.0
    assert report.tag_metrics["official"].source_precision == 1.0
    assert report.usage.total_tokens == 120
    assert report.estimated_cost_usd == 0.00039
    assert report.cases[0].source_domains == ["openai.com", "platform.openai.com"]


@pytest.mark.asyncio
async def test_private_source_fails_case_and_reduces_source_precision() -> None:
    report = await WebSearchEvaluator(_ResultBackend(private_url=True)).run(
        WebSearchGoldenSet(
            name="private source fixture",
            revision="v1",
            cases=[_live_case()],
        )
    )

    assert report.passed is False
    assert report.metrics.source_precision == 0.0
    assert report.cases[0].passed is False
    assert "invalid_or_private_source_url" in report.cases[0].reasons
    assert "citation_contract_violation" in report.cases[0].reasons


@pytest.mark.asyncio
async def test_evaluator_retries_transient_live_failure_once() -> None:
    backend = _ResultBackend(fail_once=True)
    report = await WebSearchEvaluator(
        backend,
        max_case_attempts=2,
        retry_base_seconds=0.0,
    ).run(
        WebSearchGoldenSet(name="retry fixture", revision="v1", cases=[_live_case()])
    )

    assert report.passed is True
    assert backend.calls == 2
    assert report.cases[0].attempt_count == 2
    assert report.cases[0].attempt_error_codes == ["connection_error"]


@pytest.mark.asyncio
async def test_required_case_blocks_a_zero_threshold_gate() -> None:
    zero = WebSearchEvalThresholds(
        minimum_case_pass_rate=0.0,
        minimum_provider_only_success_rate=0.0,
        minimum_citation_coverage=0.0,
        minimum_source_precision=0.0,
        minimum_primary_source_rate=0.0,
        minimum_term_recall=0.0,
        minimum_freshness_accuracy=0.0,
        minimum_policy_accuracy=0.0,
        minimum_resilience_accuracy=0.0,
    )
    report = await WebSearchEvaluator(_EmptyBackend(), thresholds=zero).run(
        WebSearchGoldenSet(
            name="required fixture",
            revision="v1",
            cases=[_live_case(required_pass=True)],
        )
    )

    assert report.passed is False
    assert report.gate_failures == ["required_case:openai_docs_fixture"]


def test_web_search_golden_contract_and_repository_coverage() -> None:
    with pytest.raises(ValidationError, match="bare DNS"):
        WebSearchGoldenCase(
            case_id="invalid_domain",
            description="Invalid domain",
            query="query",
            category="domain_policy",
            allowed_domains=["http://localhost:8000"],
        )
    with pytest.raises(ValidationError, match="cannot overlap"):
        WebSearchGoldenCase(
            case_id="overlap_domain",
            description="Overlapping policy",
            query="query",
            category="domain_policy",
            allowed_domains=["example.com"],
            forbidden_domains=["example.com"],
        )
    duplicate = _live_case()
    with pytest.raises(ValidationError, match="case IDs"):
        WebSearchGoldenSet(
            name="duplicates",
            revision="v1",
            cases=[duplicate, duplicate],
        )

    fixture = WebSearchGoldenSet.load(
        Path("examples/evaluation/web_search_golden.json")
    )
    assert fixture.revision == "2026-07-19-v1"
    assert len(fixture.cases) == 13
    assert sum(case.execution_mode == "live" for case in fixture.cases) == 7
    assert {case.category for case in fixture.cases} == {
        "freshness",
        "primary_source",
        "citation",
        "domain_policy",
        "security",
        "no_citation",
        "conflict",
        "resilience",
        "multilingual",
    }
    assert {case.execution_mode for case in fixture.cases if case.required_pass} == {
        "policy_probe",
        "fixture_timeout",
        "fixture_5xx",
        "fixture_no_citation",
        "fixture_private_url_filtered",
        "fixture_untrusted_injection",
    }


def test_cli_selection_is_versioned_and_atomic_report_has_no_temp_file(
    tmp_path: Path,
) -> None:
    fixture = WebSearchGoldenSet.load(
        Path("examples/evaluation/web_search_golden.json")
    )
    contract = _select_cases(fixture, [], "contract")
    assert len(contract.cases) == 6
    assert contract.revision.startswith("2026-07-19-v1+subset.")
    assert all(case.execution_mode != "live" for case in contract.cases)
    with pytest.raises(ValueError, match="empty"):
        _select_cases(fixture, ["provider_timeout_contract"], "live")
    with pytest.raises(ValueError, match="Unknown"):
        _select_cases(fixture, ["missing_case"], "all")

    output = tmp_path / "nested" / "report.json"
    _write_atomic(output, '{"passed":true}\n')
    assert output.read_text() == '{"passed":true}\n'
    assert list(output.parent.glob("*.tmp")) == []
