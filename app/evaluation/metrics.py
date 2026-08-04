from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import EvidenceLevel
from app.domain.models import AnswerResponse


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
    referenced_ids = {evidence_id for claim in answer.claims for evidence_id in claim.evidence_ids}
    return AnswerMetrics(
        claim_count=claim_count,
        cited_claim_count=cited_claims,
        citation_coverage=(cited_claims / claim_count if claim_count else 1.0),
        unsupported_claim_rate=(unsupported / claim_count if claim_count else 0.0),
        foreign_citation_count=len(referenced_ids - citation_ids),
    )
