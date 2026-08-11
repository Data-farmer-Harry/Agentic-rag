from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.contracts import AgentRuntime
from app.domain.enums import EvidenceLevel
from app.domain.models import AnswerResponse, RunContext


class AnswerMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_count: int = Field(ge=0)
    cited_claim_count: int = Field(ge=0)
    citation_coverage: float = Field(ge=0.0, le=1.0)
    unsupported_claim_rate: float = Field(ge=0.0, le=1.0)
    foreign_citation_count: int = Field(ge=0)


def evaluate_answer(answer: AnswerResponse) -> AnswerMetrics:
    claim_count = len(answer.claims)
    cited_claims = sum(bool(claim.evidence_ids) for claim in answer.claims)
    unsupported = sum(
        claim.level == EvidenceLevel.INSUFFICIENT or not claim.evidence_ids
        for claim in answer.claims
    )
    citation_ids = {citation.evidence_id for citation in answer.citations}
    referenced_ids = {
        evidence_id for claim in answer.claims for evidence_id in claim.evidence_ids
    }
    return AnswerMetrics(
        claim_count=claim_count,
        cited_claim_count=cited_claims,
        citation_coverage=(cited_claims / claim_count if claim_count else 1.0),
        unsupported_claim_rate=(unsupported / claim_count if claim_count else 0.0),
        foreign_citation_count=len(referenced_ids - citation_ids),
    )


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    domain_pack: str = "general"
    input: str
    expected_terms: list[str] = Field(default_factory=list)
    forbidden_terms: list[str] = Field(default_factory=list)
    minimum_citation_coverage: float = Field(default=0.0, ge=0.0, le=1.0)


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    metrics: AnswerMetrics


class ReplayReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int
    passed: int
    score: float = Field(ge=0.0, le=1.0)
    cases: list[CaseResult]


class ReplayRunner:
    def __init__(self, runtime: AgentRuntime) -> None:
        self._runtime = runtime

    async def run(self, cases: list[GoldenCase]) -> ReplayReport:
        results: list[CaseResult] = []
        for case in cases:
            answer = await self._runtime.run(
                case.input,
                RunContext(domain_pack=case.domain_pack, session_id=f"replay:{case.case_id}"),
            )
            metrics = evaluate_answer(answer)
            normalized = answer.answer_markdown.casefold()
            reasons: list[str] = []
            missing = [term for term in case.expected_terms if term.casefold() not in normalized]
            present_forbidden = [
                term for term in case.forbidden_terms if term.casefold() in normalized
            ]
            if missing:
                reasons.append(f"missing expected terms: {missing}")
            if present_forbidden:
                reasons.append(f"present forbidden terms: {present_forbidden}")
            if metrics.citation_coverage < case.minimum_citation_coverage:
                reasons.append("citation coverage below threshold")
            checks = 3
            score = (
                int(not missing)
                + int(not present_forbidden)
                + int(metrics.citation_coverage >= case.minimum_citation_coverage)
            ) / checks
            results.append(
                CaseResult(
                    case_id=case.case_id,
                    passed=not reasons,
                    score=score,
                    reasons=reasons,
                    metrics=metrics,
                )
            )
        passed = sum(result.passed for result in results)
        return ReplayReport(
            total=len(results),
            passed=passed,
            score=(sum(result.score for result in results) / len(results) if results else 1.0),
            cases=results,
        )
