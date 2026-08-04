from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.contracts import AgentRuntime
from app.domain.models import RunContext
from app.evaluation.metrics import AnswerMetrics, evaluate_answer


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
