from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.evaluation import answer_quality_cli
from app.evaluation.answer_quality import (
    AnswerQualityArtifactProvenance,
    AnswerQualityArtifactSet,
    AnswerQualityCase,
    AnswerQualityEvaluator,
    AnswerQualityEvidence,
    AnswerQualityExpectedClaim,
    AnswerQualityGoldenSet,
    AnswerQualityObservedClaim,
    AnswerQualityVariantObservation,
    embedded_fixture_artifacts,
    load_answer_quality_golden_set,
)

_FIXTURE_PATH = Path("examples/evaluation/answer_quality_golden.json")


def test_offline_fixture_is_explicit_and_reports_paired_gains() -> None:
    dataset = load_answer_quality_golden_set(_FIXTURE_PATH)
    report = AnswerQualityEvaluator(dataset).evaluate(embedded_fixture_artifacts(dataset))

    assert dataset.dataset_kind == "offline_fixture"
    assert "not live retrieval" in (dataset.fixture_notice or "")
    assert report.passed is True
    assert report.artifact_provenance.kind == "offline_fixture"
    assert report.gating_variant_total == 2
    assert report.comparison_metrics["graph_rag_vs_vector_only"].pair_count == 1
    assert (
        report.comparison_metrics["graph_rag_vs_vector_only"].mean_citation_coverage_gain
        == 0.5
    )
    assert report.comparison_metrics["self_rag_vs_single_step"].mean_citation_coverage_gain == 0.5


def test_evaluator_fails_closed_for_unsupported_claim_and_invalid_citation() -> None:
    case = _case()
    dataset = _dataset(case)
    artifact = AnswerQualityArtifactSet(
        provenance=AnswerQualityArtifactProvenance(
            kind="external_unverified",
            label="hand-authored regression artifact",
        ),
        answers=[
            AnswerQualityVariantObservation(
                case_id=case.case_id,
                variant="graph_rag",
                answer_markdown="Both claims are true.",
                claim_inventory_complete=True,
                citation_inventory_complete=True,
                claims=[
                    AnswerQualityObservedClaim(
                        claim_id="claim-one",
                        text="Claim one",
                        citation_ids=["evidence-one"],
                    ),
                    AnswerQualityObservedClaim(
                        claim_id="claim-two",
                        text="Claim two",
                        citation_ids=["evidence-one"],
                    ),
                ],
                cited_evidence_ids=["evidence-one"],
            )
        ],
    )

    report = AnswerQualityEvaluator(dataset).evaluate(artifact)
    result = report.variants[0]

    assert report.passed is False
    assert result.passed is False
    assert result.unsupported_claim_ids == ["claim-two"]
    assert "unsupported_claims" in result.failures
    assert "citation_coverage_below_threshold" in result.failures
    assert result.metrics.hallucination_rate == 0.5


def test_evaluator_fails_closed_when_pair_baseline_is_missing() -> None:
    case = _case(required_comparisons=["graph_rag_vs_vector_only"])
    dataset = _dataset(case)
    artifact = AnswerQualityArtifactSet(
        provenance=AnswerQualityArtifactProvenance(
            kind="external_unverified",
            label="candidate-only artifact",
        ),
        answers=[_complete_graph_observation(case.case_id)],
    )

    report = AnswerQualityEvaluator(dataset).evaluate(artifact)

    assert report.passed is False
    assert report.required_pair_failed_count == 1
    assert report.pairs[0].available is False
    assert report.pairs[0].failures == ["invalid_paired_variant_artifact"]


def test_complete_inventory_flags_are_required_and_citation_inventory_must_match() -> None:
    with pytest.raises(ValidationError, match="Input should be True"):
        AnswerQualityVariantObservation(
            case_id="quality-case",
            variant="graph_rag",
            answer_markdown="Answer",
            claim_inventory_complete=False,
            citation_inventory_complete=True,
        )

    case = _case()
    dataset = _dataset(case)
    artifact = AnswerQualityArtifactSet(
        provenance=AnswerQualityArtifactProvenance(
            kind="external_unverified",
            label="missing citation inventory",
        ),
        answers=[
            AnswerQualityVariantObservation(
                case_id=case.case_id,
                variant="graph_rag",
                answer_markdown="Both claims are true.",
                claim_inventory_complete=True,
                citation_inventory_complete=True,
                claims=[
                    AnswerQualityObservedClaim(
                        claim_id="claim-one",
                        text="Claim one",
                        citation_ids=["evidence-one"],
                    ),
                    AnswerQualityObservedClaim(
                        claim_id="claim-two",
                        text="Claim two",
                        citation_ids=["evidence-two"],
                    ),
                ],
                cited_evidence_ids=["evidence-one"],
            )
        ],
    )

    result = AnswerQualityEvaluator(dataset).evaluate(artifact).variants[0]

    assert result.citation_ids_missing_from_inventory == ["evidence-two"]
    assert "citation_ids_missing_from_inventory" in result.failures


def test_evaluator_rejects_unlisted_variants_and_claims_absent_from_answer() -> None:
    case = _case()
    dataset = _dataset(case)
    provenance = AnswerQualityArtifactProvenance(
        kind="external_unverified",
        label="strictness regression artifact",
    )
    with pytest.raises(ValueError, match="variants not declared"):
        AnswerQualityEvaluator(dataset).evaluate(
            AnswerQualityArtifactSet(
                provenance=provenance,
                answers=[
                    AnswerQualityVariantObservation(
                        case_id=case.case_id,
                        variant="self_rag",
                        answer_markdown="Answer",
                        claim_inventory_complete=True,
                        citation_inventory_complete=True,
                    )
                ],
            )
        )

    result = AnswerQualityEvaluator(dataset).evaluate(
        AnswerQualityArtifactSet(
            provenance=provenance,
            answers=[
                AnswerQualityVariantObservation(
                    case_id=case.case_id,
                    variant="graph_rag",
                    answer_markdown="The answer never states the canonical claims.",
                    claim_inventory_complete=True,
                    citation_inventory_complete=True,
                    claims=[
                        AnswerQualityObservedClaim(
                            claim_id="claim-one",
                            text="Claim one",
                            citation_ids=["evidence-one"],
                        ),
                        AnswerQualityObservedClaim(
                            claim_id="claim-two",
                            text="Claim two",
                            citation_ids=["evidence-two"],
                        ),
                    ],
                    cited_evidence_ids=["evidence-one", "evidence-two"],
                )
            ],
        )
    ).variants[0]

    assert result.claims_missing_from_answer_markdown == ["claim-one", "claim-two"]
    assert "claims_missing_from_answer_markdown" in result.failures


def test_pair_fails_closed_when_baseline_inventory_is_not_trustworthy() -> None:
    case = _case(required_comparisons=["graph_rag_vs_vector_only"])
    dataset = _dataset(case)
    artifact = AnswerQualityArtifactSet(
        provenance=AnswerQualityArtifactProvenance(
            kind="external_unverified",
            label="invalid baseline artifact",
        ),
        answers=[
            AnswerQualityVariantObservation(
                case_id=case.case_id,
                variant="vector_only",
                answer_markdown="Claim one",
                claim_inventory_complete=True,
                citation_inventory_complete=True,
                claims=[
                    AnswerQualityObservedClaim(
                        claim_id="claim-one",
                        text="Claim one",
                        citation_ids=["unknown-evidence"],
                    )
                ],
                cited_evidence_ids=["unknown-evidence"],
            ),
            _complete_graph_observation(case.case_id),
        ],
    )

    report = AnswerQualityEvaluator(dataset).evaluate(artifact)

    assert report.passed is False
    assert report.pairs[0].available is False
    assert report.pairs[0].failures == ["invalid_paired_variant_artifact"]


def test_cli_requires_explicit_fixture_opt_in_and_writes_report(tmp_path: Path) -> None:
    output = tmp_path / "answer-quality-report.json"
    args = Namespace(
        dataset=_FIXTURE_PATH,
        answers=None,
        use_embedded_fixture=True,
        output=output,
        report_only=False,
    )

    assert answer_quality_cli._run(args) == 0
    assert '"dataset_kind": "offline_fixture"' in output.read_text(encoding="utf-8")

    missing_artifact_args = Namespace(
        dataset=_FIXTURE_PATH,
        answers=None,
        use_embedded_fixture=False,
        output=output,
        report_only=False,
    )
    with pytest.raises(ValueError, match="provide --answers"):
        answer_quality_cli._run(missing_artifact_args)


def _case(
    *,
    required_comparisons: list[str] | None = None,
) -> AnswerQualityCase:
    return AnswerQualityCase(
        case_id="quality-case",
        query="Check two facts",
        category="unit",
        expected_claims=[
            AnswerQualityExpectedClaim(claim_id="claim-one", text="Claim one"),
            AnswerQualityExpectedClaim(claim_id="claim-two", text="Claim two"),
        ],
        evidence=[
            AnswerQualityEvidence(
                evidence_id="evidence-one",
                source_id="fixture:one",
                text="Evidence for claim one",
                supports_claim_ids=["claim-one"],
            ),
            AnswerQualityEvidence(
                evidence_id="evidence-two",
                source_id="fixture:two",
                text="Evidence for claim two",
                supports_claim_ids=["claim-two"],
            ),
        ],
        required_variants=["graph_rag"],
        required_comparisons=required_comparisons or [],
    )


def _dataset(case: AnswerQualityCase) -> AnswerQualityGoldenSet:
    return AnswerQualityGoldenSet(
        name="unit",
        revision="v1",
        dataset_kind="evaluation_spec",
        cases=[case],
    )


def _complete_graph_observation(case_id: str) -> AnswerQualityVariantObservation:
    return AnswerQualityVariantObservation(
        case_id=case_id,
        variant="graph_rag",
        answer_markdown="Both claims are true.",
        claim_inventory_complete=True,
        citation_inventory_complete=True,
        claims=[
            AnswerQualityObservedClaim(
                claim_id="claim-one",
                text="Claim one",
                citation_ids=["evidence-one"],
            ),
            AnswerQualityObservedClaim(
                claim_id="claim-two",
                text="Claim two",
                citation_ids=["evidence-two"],
            ),
        ],
        cited_evidence_ids=["evidence-one", "evidence-two"],
    )
