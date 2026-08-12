"""Deterministic, fail-closed answer-quality evaluation.

This evaluator intentionally does not call an LLM.  A golden case declares the
claims that matter and the evidence snippets that support them.  Each evaluated
answer supplies a complete, structured claim and citation inventory.  That makes
the following gates reproducible in CI:

* citation-to-claim support;
* coverage of required claims; and
* unsupported-claim (hallucination) rate.

The evaluator also compares paired variants from the same case.  In particular,
``graph_rag`` is compared with ``vector_only`` and ``self_rag`` with
``single_step``.  A pair is incomplete or regresses a configured safety metric
when it fails; a fixture is never represented as a live model result.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from app.domain.models import StrictModel

AnswerQualityVariant = Literal[
    "vector_only",
    "graph_rag",
    "single_step",
    "self_rag",
]
AnswerQualityComparison = Literal[
    "graph_rag_vs_vector_only",
    "self_rag_vs_single_step",
]

_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,99}$"
_SOURCE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9:._/#=-]{0,299}$"
_COMPARISON_VARIANTS: dict[
    AnswerQualityComparison,
    tuple[AnswerQualityVariant, AnswerQualityVariant],
] = {
    "graph_rag_vs_vector_only": ("vector_only", "graph_rag"),
    "self_rag_vs_single_step": ("single_step", "self_rag"),
}
_PAIR_ARTIFACT_INTEGRITY_FAILURES = frozenset(
    {
        "missing_variant_artifact",
        "unknown_claim_ids",
        "claims_missing_from_answer_markdown",
        "unknown_citation_ids",
        "unassigned_citations",
        "citation_ids_missing_from_inventory",
    }
)


class AnswerQualityExpectedClaim(StrictModel):
    """A claim whose correctness can be judged from the fixed evidence set."""

    claim_id: str = Field(pattern=_ID_PATTERN)
    text: str = Field(min_length=1, max_length=10_000)
    required: bool = True

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("claim text must not be empty")
        return normalized


class AnswerQualityEvidence(StrictModel):
    """An immutable annotated evidence snippet from the evaluation corpus."""

    evidence_id: str = Field(pattern=_ID_PATTERN)
    source_id: str = Field(pattern=_SOURCE_ID_PATTERN)
    text: str = Field(min_length=1, max_length=50_000)
    supports_claim_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("evidence text must not be empty")
        return normalized

    @field_validator("supports_claim_ids")
    @classmethod
    def validate_claim_ids(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values]
        if any(not item for item in normalized):
            raise ValueError("evidence support claim IDs must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("evidence support claim IDs must be unique")
        return normalized


class AnswerQualityPairPolicy(StrictModel):
    """Non-regression limits applied to a candidate in a paired comparison."""

    minimum_claim_support_rate_gain: float = Field(default=0.0, ge=-1.0, le=1.0)
    minimum_citation_coverage_gain: float = Field(default=0.0, ge=-1.0, le=1.0)
    minimum_citation_claim_support_rate_gain: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
    )
    minimum_hallucination_rate_reduction: float = Field(default=0.0, ge=-1.0, le=1.0)


class AnswerQualityCase(StrictModel):
    """One answer-quality test case and its fixed evidence annotations."""

    case_id: str = Field(pattern=_ID_PATTERN)
    query: str = Field(min_length=1, max_length=10_000)
    category: str = Field(min_length=1, max_length=100)
    expected_claims: list[AnswerQualityExpectedClaim] = Field(min_length=1, max_length=100)
    evidence: list[AnswerQualityEvidence] = Field(min_length=1, max_length=500)
    required_variants: list[AnswerQualityVariant] = Field(min_length=1, max_length=4)
    required_comparisons: list[AnswerQualityComparison] = Field(default_factory=list, max_length=2)
    minimum_claim_support_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    minimum_citation_coverage: float = Field(default=1.0, ge=0.0, le=1.0)
    minimum_citation_claim_support_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    maximum_hallucination_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    pair_policy: AnswerQualityPairPolicy = Field(default_factory=AnswerQualityPairPolicy)

    @field_validator("query", "category")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("case text must not be empty")
        return normalized

    @field_validator("required_variants")
    @classmethod
    def validate_required_variants(
        cls,
        values: list[AnswerQualityVariant],
    ) -> list[AnswerQualityVariant]:
        if len(values) != len(set(values)):
            raise ValueError("required variants must be unique")
        return values

    @field_validator("required_comparisons")
    @classmethod
    def validate_required_comparisons(
        cls,
        values: list[AnswerQualityComparison],
    ) -> list[AnswerQualityComparison]:
        if len(values) != len(set(values)):
            raise ValueError("required comparisons must be unique")
        return values

    @model_validator(mode="after")
    def validate_cross_contract(self) -> Self:
        claim_ids = [item.claim_id for item in self.expected_claims]
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("expected claim IDs must be unique")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence IDs must be unique")
        known_claim_ids = set(claim_ids)
        unsupported_references = sorted(
            {
                claim_id
                for item in self.evidence
                for claim_id in item.supports_claim_ids
                if claim_id not in known_claim_ids
            }
        )
        if unsupported_references:
            raise ValueError(
                f"evidence references unknown expected claim IDs: {unsupported_references}"
            )
        if not any(item.required for item in self.expected_claims):
            raise ValueError("an answer-quality case must require at least one claim")
        for comparison in self.required_comparisons:
            _, candidate = _COMPARISON_VARIANTS[comparison]
            if candidate not in self.required_variants:
                raise ValueError(
                    f"a required comparison must require its candidate variant: {comparison}"
                )
        return self


class AnswerQualityArtifactProvenance(StrictModel):
    """Origin metadata; offline fixtures can never be mistaken for live output."""

    kind: Literal["offline_fixture", "live_run", "external_unverified"]
    label: str = Field(min_length=1, max_length=300)
    run_id: str | None = Field(default=None, min_length=1, max_length=300)
    run_ids: list[str] = Field(default_factory=list, max_length=10_000)
    model_revision: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("artifact provenance label must not be blank")
        return normalized

    @field_validator("run_id", "model_revision")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("provenance values must not be blank")
        return normalized

    @field_validator("run_ids")
    @classmethod
    def normalize_run_ids(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values]
        if any(not item for item in normalized):
            raise ValueError("live run IDs must not be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("live run IDs must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_origin(self) -> Self:
        values = ([self.run_id] if self.run_id is not None else []) + self.run_ids
        if self.kind == "live_run" and not values:
            raise ValueError("live_run artifacts require at least one run ID")
        if self.kind != "live_run" and values:
            raise ValueError("only live_run artifacts may declare run IDs")
        if len(values) != len(set(values)):
            raise ValueError("live run IDs must be unique")
        return self


class AnswerQualityObservedClaim(StrictModel):
    """One claim declared by the answer producer, with its cited evidence IDs."""

    claim_id: str = Field(pattern=_ID_PATTERN)
    text: str = Field(min_length=1, max_length=10_000)
    answer_quote: str | None = Field(default=None, min_length=1, max_length=10_000)
    citation_ids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("text", "answer_quote")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("observed claim text must not be empty")
        return normalized

    @field_validator("citation_ids")
    @classmethod
    def validate_citation_ids(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values]
        if any(not item for item in normalized):
            raise ValueError("claim citation IDs must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("claim citation IDs must be unique")
        return normalized


class AnswerQualityVariantObservation(StrictModel):
    """A complete claim/citation inventory for one case and implementation variant."""

    case_id: str = Field(pattern=_ID_PATTERN)
    variant: AnswerQualityVariant
    answer_markdown: str = Field(min_length=1, max_length=100_000)
    claim_inventory_complete: Literal[True]
    citation_inventory_complete: Literal[True]
    claims: list[AnswerQualityObservedClaim] = Field(default_factory=list, max_length=500)
    cited_evidence_ids: list[str] = Field(default_factory=list, max_length=1_000)

    @field_validator("answer_markdown")
    @classmethod
    def require_answer_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer markdown must not be blank")
        return value

    @field_validator("cited_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: list[str]) -> list[str]:
        normalized = [item.strip() for item in values]
        if any(not item for item in normalized):
            raise ValueError("cited evidence IDs must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("cited evidence IDs must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_claim_ids(self) -> Self:
        claim_ids = [item.claim_id for item in self.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("observed claim IDs must be unique per variant")
        return self


class AnswerQualityArtifactSet(StrictModel):
    provenance: AnswerQualityArtifactProvenance
    answers: list[AnswerQualityVariantObservation] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def validate_unique_case_variants(self) -> Self:
        keys = [(item.case_id, item.variant) for item in self.answers]
        if len(keys) != len(set(keys)):
            raise ValueError("answer artifacts must be unique by case ID and variant")
        return self

    @classmethod
    def load(cls, path: Path) -> AnswerQualityArtifactSet:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class AnswerQualityGoldenSet(StrictModel):
    """Evaluation spec. Embedded artifacts are explicitly limited to offline fixtures."""

    name: str = Field(min_length=1, max_length=300)
    revision: str = Field(min_length=1, max_length=200)
    dataset_kind: Literal["offline_fixture", "evaluation_spec"]
    fixture_notice: str | None = Field(default=None, min_length=1, max_length=1_000)
    cases: list[AnswerQualityCase] = Field(min_length=1, max_length=10_000)
    fixture_artifacts: AnswerQualityArtifactSet | None = None

    @model_validator(mode="after")
    def validate_dataset_contract(self) -> Self:
        case_ids = [item.case_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("answer-quality case IDs must be unique")
        if self.dataset_kind == "offline_fixture":
            if not self.fixture_notice or not self.fixture_notice.strip():
                raise ValueError("offline fixtures must include a fixture_notice")
            if self.fixture_artifacts is None:
                raise ValueError("offline fixtures must include fixture_artifacts")
            if self.fixture_artifacts.provenance.kind != "offline_fixture":
                raise ValueError(
                    "offline fixture artifacts must declare offline_fixture provenance"
                )
        elif self.fixture_artifacts is not None:
            raise ValueError("evaluation specs must not embed fixture artifacts")
        return self


class AnswerQualityMetrics(StrictModel):
    total_claim_count: int = Field(ge=0)
    expected_claim_count: int = Field(ge=0)
    required_claim_count: int = Field(ge=0)
    supported_claim_count: int = Field(ge=0)
    unsupported_claim_count: int = Field(ge=0)
    unknown_claim_count: int = Field(ge=0)
    supported_required_claim_count: int = Field(ge=0)
    citation_link_count: int = Field(ge=0)
    supported_citation_link_count: int = Field(ge=0)
    cited_evidence_count: int = Field(ge=0)
    unknown_citation_count: int = Field(ge=0)
    unassigned_citation_count: int = Field(ge=0)
    citation_ids_missing_from_inventory_count: int = Field(ge=0)
    claim_support_rate: float = Field(ge=0.0, le=1.0)
    citation_coverage: float = Field(ge=0.0, le=1.0)
    citation_claim_support_rate: float = Field(ge=0.0, le=1.0)
    hallucination_rate: float = Field(ge=0.0, le=1.0)


class AnswerQualityVariantResult(StrictModel):
    case_id: str
    variant: AnswerQualityVariant
    required_for_gate: bool
    passed: bool
    failures: list[str] = Field(default_factory=list)
    missing_required_claim_ids: list[str] = Field(default_factory=list)
    unsupported_claim_ids: list[str] = Field(default_factory=list)
    unknown_claim_ids: list[str] = Field(default_factory=list)
    claims_missing_from_answer_markdown: list[str] = Field(default_factory=list)
    unknown_citation_ids: list[str] = Field(default_factory=list)
    unassigned_citation_ids: list[str] = Field(default_factory=list)
    citation_ids_missing_from_inventory: list[str] = Field(default_factory=list)
    metrics: AnswerQualityMetrics


class AnswerQualityPairMetrics(StrictModel):
    claim_support_rate_gain: float
    citation_coverage_gain: float
    citation_claim_support_rate_gain: float
    hallucination_rate_reduction: float


class AnswerQualityPairResult(StrictModel):
    case_id: str
    comparison: AnswerQualityComparison
    baseline_variant: AnswerQualityVariant
    candidate_variant: AnswerQualityVariant
    available: bool
    passed: bool
    failures: list[str] = Field(default_factory=list)
    metrics: AnswerQualityPairMetrics | None = None


class AnswerQualitySliceMetrics(StrictModel):
    total_gating_variants: int = Field(ge=0)
    passed_gating_variants: int = Field(ge=0)
    gate_pass_rate: float = Field(ge=0.0, le=1.0)
    mean_claim_support_rate: float = Field(ge=0.0, le=1.0)
    mean_citation_coverage: float = Field(ge=0.0, le=1.0)
    mean_citation_claim_support_rate: float = Field(ge=0.0, le=1.0)
    mean_hallucination_rate: float = Field(ge=0.0, le=1.0)


class AnswerQualityComparisonMetrics(StrictModel):
    pair_count: int = Field(ge=0)
    available_pair_count: int = Field(ge=0)
    passed_pair_count: int = Field(ge=0)
    regression_count: int = Field(ge=0)
    mean_claim_support_rate_gain: float = Field(default=0.0)
    mean_citation_coverage_gain: float = Field(default=0.0)
    mean_citation_claim_support_rate_gain: float = Field(default=0.0)
    mean_hallucination_rate_reduction: float = Field(default=0.0)


class AnswerQualityEvalReport(StrictModel):
    dataset_name: str
    dataset_revision: str
    dataset_kind: Literal["offline_fixture", "evaluation_spec"]
    artifact_provenance: AnswerQualityArtifactProvenance
    total_cases: int = Field(ge=0)
    total_variant_results: int = Field(ge=0)
    gating_variant_total: int = Field(ge=0)
    gating_variant_failed_count: int = Field(ge=0)
    non_gating_variant_failed_count: int = Field(ge=0)
    required_pair_total: int = Field(ge=0)
    required_pair_failed_count: int = Field(ge=0)
    passed: bool
    category_metrics: dict[str, AnswerQualitySliceMetrics] = Field(default_factory=dict)
    comparison_metrics: dict[AnswerQualityComparison, AnswerQualityComparisonMetrics]
    variants: list[AnswerQualityVariantResult] = Field(default_factory=list)
    pairs: list[AnswerQualityPairResult] = Field(default_factory=list)


class AnswerQualityEvaluator:
    """Evaluate serialized answer artifacts without model inference or heuristics."""

    def __init__(self, dataset: AnswerQualityGoldenSet) -> None:
        self._dataset = dataset

    def evaluate(self, artifacts: AnswerQualityArtifactSet) -> AnswerQualityEvalReport:
        observations = {(item.case_id, item.variant): item for item in artifacts.answers}
        known_cases = {item.case_id for item in self._dataset.cases}
        unknown_cases = sorted({case_id for case_id, _ in observations}.difference(known_cases))
        if unknown_cases:
            raise ValueError(f"answer artifacts reference unknown cases: {unknown_cases}")
        allowed_variants = {
            (case.case_id, variant)
            for case in self._dataset.cases
            for variant in _case_variants(case)
        }
        unexpected_variants = sorted(set(observations).difference(allowed_variants))
        if unexpected_variants:
            raise ValueError(
                "answer artifacts reference variants not declared by their case: "
                f"{unexpected_variants}"
            )

        variant_results: list[AnswerQualityVariantResult] = []
        pair_results: list[AnswerQualityPairResult] = []
        by_case_variant: dict[tuple[str, AnswerQualityVariant], AnswerQualityVariantResult] = {}
        for case in self._dataset.cases:
            for variant in _case_variants(case):
                result = self._evaluate_variant(
                    case,
                    variant,
                    observations.get((case.case_id, variant)),
                )
                variant_results.append(result)
                by_case_variant[(case.case_id, variant)] = result
            for comparison in case.required_comparisons:
                pair_results.append(
                    self._evaluate_pair(
                        case,
                        comparison,
                        by_case_variant,
                    )
                )

        gating_results = [item for item in variant_results if item.required_for_gate]
        gating_failures = [item for item in gating_results if not item.passed]
        non_gating_failures = [
            item for item in variant_results if not item.required_for_gate and not item.passed
        ]
        pair_failures = [item for item in pair_results if not item.passed]
        return AnswerQualityEvalReport(
            dataset_name=self._dataset.name,
            dataset_revision=self._dataset.revision,
            dataset_kind=self._dataset.dataset_kind,
            artifact_provenance=artifacts.provenance,
            total_cases=len(self._dataset.cases),
            total_variant_results=len(variant_results),
            gating_variant_total=len(gating_results),
            gating_variant_failed_count=len(gating_failures),
            non_gating_variant_failed_count=len(non_gating_failures),
            required_pair_total=len(pair_results),
            required_pair_failed_count=len(pair_failures),
            passed=not gating_failures and not pair_failures,
            category_metrics=_slice_metrics(self._dataset.cases, variant_results),
            comparison_metrics=_comparison_metrics(pair_results),
            variants=variant_results,
            pairs=pair_results,
        )

    def _evaluate_variant(
        self,
        case: AnswerQualityCase,
        variant: AnswerQualityVariant,
        observation: AnswerQualityVariantObservation | None,
    ) -> AnswerQualityVariantResult:
        required_for_gate = variant in case.required_variants
        if observation is None:
            return AnswerQualityVariantResult(
                case_id=case.case_id,
                variant=variant,
                required_for_gate=required_for_gate,
                passed=False,
                failures=["missing_variant_artifact"],
                metrics=_empty_metrics(case),
            )

        expected_claims = {item.claim_id: item for item in case.expected_claims}
        expected_evidence = {item.evidence_id: item for item in case.evidence}
        required_claim_ids = {item.claim_id for item in case.expected_claims if item.required}
        observed_claim_ids = {item.claim_id for item in observation.claims}
        missing_required_claim_ids = sorted(required_claim_ids.difference(observed_claim_ids))
        unknown_claim_ids: list[str] = []
        unsupported_claim_ids: list[str] = []
        claims_missing_from_answer_markdown: list[str] = []
        supported_claim_ids: set[str] = set()
        citation_links: set[tuple[str, str]] = set()
        supported_links: set[tuple[str, str]] = set()
        claim_citation_ids: set[str] = set()
        for claim in observation.claims:
            if claim.claim_id not in expected_claims:
                unknown_claim_ids.append(claim.claim_id)
            answer_text = claim.answer_quote or claim.text
            if _normalized_claim_text(answer_text) not in _normalized_claim_text(
                observation.answer_markdown
            ):
                claims_missing_from_answer_markdown.append(claim.claim_id)
            for evidence_id in claim.citation_ids:
                citation_links.add((claim.claim_id, evidence_id))
                claim_citation_ids.add(evidence_id)
                evidence = expected_evidence.get(evidence_id)
                if evidence is not None and claim.claim_id in evidence.supports_claim_ids:
                    supported_links.add((claim.claim_id, evidence_id))
                    if claim.claim_id in expected_claims:
                        supported_claim_ids.add(claim.claim_id)
            if claim.claim_id not in supported_claim_ids:
                unsupported_claim_ids.append(claim.claim_id)

        cited_evidence_ids = set(observation.cited_evidence_ids)
        unknown_citation_ids = sorted(
            cited_evidence_ids.union(claim_citation_ids).difference(expected_evidence)
        )
        unassigned_citation_ids = sorted(cited_evidence_ids.difference(claim_citation_ids))
        citation_ids_missing_from_inventory = sorted(
            claim_citation_ids.difference(cited_evidence_ids)
        )
        supported_required_claim_ids = required_claim_ids.intersection(supported_claim_ids)
        metrics = AnswerQualityMetrics(
            total_claim_count=len(observation.claims),
            expected_claim_count=len(expected_claims),
            required_claim_count=len(required_claim_ids),
            supported_claim_count=len(supported_claim_ids),
            unsupported_claim_count=len(unsupported_claim_ids),
            unknown_claim_count=len(unknown_claim_ids),
            supported_required_claim_count=len(supported_required_claim_ids),
            citation_link_count=len(citation_links),
            supported_citation_link_count=len(supported_links),
            cited_evidence_count=len(cited_evidence_ids),
            unknown_citation_count=len(unknown_citation_ids),
            unassigned_citation_count=len(unassigned_citation_ids),
            citation_ids_missing_from_inventory_count=len(citation_ids_missing_from_inventory),
            claim_support_rate=_ratio(len(supported_claim_ids), len(observation.claims)),
            citation_coverage=_ratio(
                len(supported_required_claim_ids),
                len(required_claim_ids),
            ),
            citation_claim_support_rate=_ratio(len(supported_links), len(citation_links)),
            hallucination_rate=_ratio(len(unsupported_claim_ids), len(observation.claims)),
        )
        failures: list[str] = []
        if missing_required_claim_ids:
            failures.append("missing_required_claims")
        if unsupported_claim_ids:
            failures.append("unsupported_claims")
        if unknown_claim_ids:
            failures.append("unknown_claim_ids")
        if claims_missing_from_answer_markdown:
            failures.append("claims_missing_from_answer_markdown")
        if unknown_citation_ids:
            failures.append("unknown_citation_ids")
        if unassigned_citation_ids:
            failures.append("unassigned_citations")
        if citation_ids_missing_from_inventory:
            failures.append("citation_ids_missing_from_inventory")
        if metrics.claim_support_rate < case.minimum_claim_support_rate:
            failures.append("claim_support_rate_below_threshold")
        if metrics.citation_coverage < case.minimum_citation_coverage:
            failures.append("citation_coverage_below_threshold")
        if metrics.citation_claim_support_rate < case.minimum_citation_claim_support_rate:
            failures.append("citation_claim_support_rate_below_threshold")
        if metrics.hallucination_rate > case.maximum_hallucination_rate:
            failures.append("hallucination_rate_above_threshold")
        return AnswerQualityVariantResult(
            case_id=case.case_id,
            variant=variant,
            required_for_gate=required_for_gate,
            passed=not failures,
            failures=failures,
            missing_required_claim_ids=missing_required_claim_ids,
            unsupported_claim_ids=sorted(unsupported_claim_ids),
            unknown_claim_ids=sorted(unknown_claim_ids),
            claims_missing_from_answer_markdown=sorted(claims_missing_from_answer_markdown),
            unknown_citation_ids=unknown_citation_ids,
            unassigned_citation_ids=unassigned_citation_ids,
            citation_ids_missing_from_inventory=citation_ids_missing_from_inventory,
            metrics=metrics,
        )

    def _evaluate_pair(
        self,
        case: AnswerQualityCase,
        comparison: AnswerQualityComparison,
        results: dict[tuple[str, AnswerQualityVariant], AnswerQualityVariantResult],
    ) -> AnswerQualityPairResult:
        baseline_variant, candidate_variant = _COMPARISON_VARIANTS[comparison]
        baseline = results[(case.case_id, baseline_variant)]
        candidate = results[(case.case_id, candidate_variant)]
        if _pair_artifact_has_integrity_failure(baseline) or _pair_artifact_has_integrity_failure(
            candidate
        ):
            return AnswerQualityPairResult(
                case_id=case.case_id,
                comparison=comparison,
                baseline_variant=baseline_variant,
                candidate_variant=candidate_variant,
                available=False,
                passed=False,
                failures=["invalid_paired_variant_artifact"],
            )
        metrics = AnswerQualityPairMetrics(
            claim_support_rate_gain=(
                candidate.metrics.claim_support_rate - baseline.metrics.claim_support_rate
            ),
            citation_coverage_gain=(
                candidate.metrics.citation_coverage - baseline.metrics.citation_coverage
            ),
            citation_claim_support_rate_gain=(
                candidate.metrics.citation_claim_support_rate
                - baseline.metrics.citation_claim_support_rate
            ),
            hallucination_rate_reduction=(
                baseline.metrics.hallucination_rate - candidate.metrics.hallucination_rate
            ),
        )
        failures: list[str] = []
        policy = case.pair_policy
        if metrics.claim_support_rate_gain < policy.minimum_claim_support_rate_gain:
            failures.append("claim_support_rate_regression")
        if metrics.citation_coverage_gain < policy.minimum_citation_coverage_gain:
            failures.append("citation_coverage_regression")
        if (
            metrics.citation_claim_support_rate_gain
            < policy.minimum_citation_claim_support_rate_gain
        ):
            failures.append("citation_claim_support_rate_regression")
        if metrics.hallucination_rate_reduction < policy.minimum_hallucination_rate_reduction:
            failures.append("hallucination_rate_regression")
        return AnswerQualityPairResult(
            case_id=case.case_id,
            comparison=comparison,
            baseline_variant=baseline_variant,
            candidate_variant=candidate_variant,
            available=True,
            passed=not failures,
            failures=failures,
            metrics=metrics,
        )


def load_answer_quality_golden_set(path: Path) -> AnswerQualityGoldenSet:
    return AnswerQualityGoldenSet.model_validate_json(path.read_text(encoding="utf-8"))


def embedded_fixture_artifacts(dataset: AnswerQualityGoldenSet) -> AnswerQualityArtifactSet:
    """Return fixture-only observations, rejecting an attempt to imply live output."""

    if dataset.dataset_kind != "offline_fixture" or dataset.fixture_artifacts is None:
        raise ValueError("this dataset does not include offline fixture artifacts")
    if dataset.fixture_artifacts.provenance.kind != "offline_fixture":
        raise ValueError("embedded artifacts must have offline_fixture provenance")
    return dataset.fixture_artifacts


def _case_variants(case: AnswerQualityCase) -> list[AnswerQualityVariant]:
    variants = set(case.required_variants)
    for comparison in case.required_comparisons:
        variants.update(_COMPARISON_VARIANTS[comparison])
    return sorted(variants)


def _empty_metrics(case: AnswerQualityCase) -> AnswerQualityMetrics:
    required_count = sum(item.required for item in case.expected_claims)
    return AnswerQualityMetrics(
        total_claim_count=0,
        expected_claim_count=len(case.expected_claims),
        required_claim_count=required_count,
        supported_claim_count=0,
        unsupported_claim_count=0,
        unknown_claim_count=0,
        supported_required_claim_count=0,
        citation_link_count=0,
        supported_citation_link_count=0,
        cited_evidence_count=0,
        unknown_citation_count=0,
        unassigned_citation_count=0,
        citation_ids_missing_from_inventory_count=0,
        claim_support_rate=0.0,
        citation_coverage=0.0,
        citation_claim_support_rate=0.0,
        hallucination_rate=0.0,
    )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _normalized_claim_text(value: str) -> str:
    return " ".join(value.split())


def _pair_artifact_has_integrity_failure(result: AnswerQualityVariantResult) -> bool:
    return bool(_PAIR_ARTIFACT_INTEGRITY_FAILURES.intersection(result.failures))


def _slice_metrics(
    cases: Sequence[AnswerQualityCase],
    results: Sequence[AnswerQualityVariantResult],
) -> dict[str, AnswerQualitySliceMetrics]:
    categories = {item.case_id: item.category for item in cases}
    grouped: dict[str, list[AnswerQualityVariantResult]] = defaultdict(list)
    for result in results:
        if result.required_for_gate:
            grouped[categories[result.case_id]].append(result)
    return {category: _build_slice_metrics(items) for category, items in sorted(grouped.items())}


def _build_slice_metrics(
    results: Sequence[AnswerQualityVariantResult],
) -> AnswerQualitySliceMetrics:
    total = len(results)
    return AnswerQualitySliceMetrics(
        total_gating_variants=total,
        passed_gating_variants=sum(item.passed for item in results),
        gate_pass_rate=_ratio(sum(item.passed for item in results), total),
        mean_claim_support_rate=_mean(item.metrics.claim_support_rate for item in results),
        mean_citation_coverage=_mean(item.metrics.citation_coverage for item in results),
        mean_citation_claim_support_rate=_mean(
            item.metrics.citation_claim_support_rate for item in results
        ),
        mean_hallucination_rate=_mean(item.metrics.hallucination_rate for item in results),
    )


def _comparison_metrics(
    pairs: Sequence[AnswerQualityPairResult],
) -> dict[AnswerQualityComparison, AnswerQualityComparisonMetrics]:
    grouped: dict[AnswerQualityComparison, list[AnswerQualityPairResult]] = defaultdict(list)
    for pair in pairs:
        grouped[pair.comparison].append(pair)
    return {
        comparison: _build_comparison_metrics(items)
        for comparison, items in sorted(grouped.items())
    }


def _build_comparison_metrics(
    pairs: Sequence[AnswerQualityPairResult],
) -> AnswerQualityComparisonMetrics:
    available = [item for item in pairs if item.available and item.metrics is not None]
    metrics = [item.metrics for item in available if item.metrics is not None]
    return AnswerQualityComparisonMetrics(
        pair_count=len(pairs),
        available_pair_count=len(available),
        passed_pair_count=sum(item.passed for item in pairs),
        regression_count=sum(not item.passed for item in pairs),
        mean_claim_support_rate_gain=_mean(item.claim_support_rate_gain for item in metrics),
        mean_citation_coverage_gain=_mean(item.citation_coverage_gain for item in metrics),
        mean_citation_claim_support_rate_gain=_mean(
            item.citation_claim_support_rate_gain for item in metrics
        ),
        mean_hallucination_rate_reduction=_mean(
            item.hallucination_rate_reduction for item in metrics
        ),
    )


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


__all__ = [
    "AnswerQualityArtifactProvenance",
    "AnswerQualityArtifactSet",
    "AnswerQualityCase",
    "AnswerQualityComparison",
    "AnswerQualityComparisonMetrics",
    "AnswerQualityEvalReport",
    "AnswerQualityEvaluator",
    "AnswerQualityEvidence",
    "AnswerQualityExpectedClaim",
    "AnswerQualityMetrics",
    "AnswerQualityObservedClaim",
    "AnswerQualityPairMetrics",
    "AnswerQualityPairPolicy",
    "AnswerQualityPairResult",
    "AnswerQualitySliceMetrics",
    "AnswerQualityVariant",
    "AnswerQualityVariantObservation",
    "AnswerQualityVariantResult",
    "AnswerQualityGoldenSet",
    "embedded_fixture_artifacts",
    "load_answer_quality_golden_set",
]
