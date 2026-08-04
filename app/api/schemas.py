from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import GraphCandidateStatus, MemoryType, SkillStatus
from app.domain.models import AnswerResponse
from app.harness.models import HarnessPatternStatus


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1, max_length=50_000)
    session_id: str = Field(default="default", min_length=1, max_length=200)
    user_id: str | None = Field(default=None, min_length=1, max_length=200)
    domain_pack: str | None = Field(default=None, min_length=1, max_length=100)
    idempotency_key: str | None = Field(
        default=None,
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
    )


class RunResponse(BaseModel):
    run_id: str
    status: Literal["completed"]
    answer: AnswerResponse


class RunStartResponse(BaseModel):
    run_id: str
    status: Literal["running", "completed", "failed", "cancelled"]
    idempotency_key: str
    coalesced: bool


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=-1.0, le=1.0)
    text: str | None = Field(default=None, max_length=10_000)


class CreateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2_000)
    memory_type: MemoryType = MemoryType.SEMANTIC
    source_session_id: str | None = Field(default=None, max_length=200)


class UpdateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    archived: bool | None = None


class SkillTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_status: SkillStatus
    human_approved: bool = False


class HarnessPatternTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_status: HarnessPatternStatus
    human_approved: bool = False
    expected_from_status: HarnessPatternStatus | None = None
    reason: str = Field(default="", max_length=2_000)


class GraphCandidateReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_status: GraphCandidateStatus
    reviewer_id: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2_000)


class HermesNativeLearningReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "rollback"]
    reviewer_id: str | None = Field(default=None, min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2_000)


class EnterpriseFixtureStartRequest(BaseModel):
    """Body accepted by the stable workbench fixture-import endpoint."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = False
