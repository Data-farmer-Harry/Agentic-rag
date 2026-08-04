from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.enums import AnswerMode, EvidenceLevel, RunStatus
from app.domain.models import (
    AnswerResponse,
    Claim,
    RunContext,
    RunTrajectory,
    ToolEvent,
)
from app.harness.diagnosis import diagnose_trajectory
from app.harness.experience import (
    HarnessExperienceService,
    assemble_evaluation,
    assemble_experience,
)
from app.harness.models import (
    HarnessConfigDelta,
    HarnessOrchestrationConfig,
    canonical_hash,
)
from app.harness.repository import (
    HarnessExperienceConflictError,
    JsonHarnessExperienceRepository,
)


def _trajectory(
    *,
    user_input: str = "Compare GraphRAG and vector retrieval.",
    status: RunStatus = RunStatus.COMPLETED,
    feedback_score: float | None = None,
    conversational: bool = False,
) -> RunTrajectory:
    started = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)
    answer = (
        AnswerResponse(
            answer_markdown="They use different evidence structures.",
            response_mode=(
                AnswerMode.CONVERSATIONAL if conversational else AnswerMode.GROUNDED
            ),
            claims=(
                []
                if conversational
                else [
                    Claim(
                        text="GraphRAG uses graph evidence.",
                        evidence_ids=[],
                        level=EvidenceLevel.INFERRED,
                    )
                ]
            ),
            confidence=(
                EvidenceLevel.INFERRED
                if conversational
                else EvidenceLevel.INSUFFICIENT
            ),
            limitations=[] if conversational else ["No grounded evidence was found."],
        )
        if status == RunStatus.COMPLETED
        else None
    )
    return RunTrajectory(
        context=RunContext(
            tenant_id="tenant-a",
            project_id="project-a",
            started_at=started,
        ),
        user_input=user_input,
        status=status,
        answer=answer,
        feedback_score=feedback_score,
        tool_events=[
            ToolEvent(
                tool_name="search_knowledge",
                input_hash="query-hash",
                output_summary="must never enter the experience bank",
                detail={"result_count": 0},
                success=True,
                duration_ms=12,
            )
        ],
        completed_at=started + timedelta(seconds=2),
    )


def test_harness_delta_rejects_unknown_fields_and_caps() -> None:
    with pytest.raises(ValidationError):
        HarnessConfigDelta.model_validate(
            {"orchestration": {"max_subqueries": 99}}
        )
    with pytest.raises(ValidationError):
        HarnessOrchestrationConfig.model_validate({"arbitrary_tool": True})


def test_experience_is_stable_and_does_not_store_raw_query_or_tool_output() -> None:
    trajectory = _trajectory(user_input="PRIVATE exact question about GraphRAG")

    first = assemble_experience(trajectory)
    second = assemble_experience(trajectory)
    serialized = first.model_dump_json()

    assert first == second
    assert first.experience_id == second.experience_id
    assert first.payload_hash == canonical_hash(first)
    assert "PRIVATE exact question" not in serialized
    assert "must never enter" not in serialized
    assert "search_knowledge" not in serialized
    assert first.tool_sequence_summary[0].call_count == 1


def test_conversational_answer_is_not_penalized_for_missing_citations() -> None:
    experience = assemble_experience(
        _trajectory(user_input="你好", conversational=True)
    )

    assert experience.diagnosis.success is True
    assert experience.diagnosis.quality_vector.citation_coverage == 1.0
    assert experience.diagnosis.reason_codes == []


def test_provider_failure_is_unlearnable_and_not_forced_into_d1_d6() -> None:
    trajectory = _trajectory(status=RunStatus.FAILED).model_copy(
        update={
            "tool_events": [
                ToolEvent(
                    tool_name="search_knowledge",
                    input_hash="same",
                    success=False,
                    detail={"error_code": "upstream_error", "message": "Bad Gateway"},
                )
            ]
        }
    )

    diagnosis = diagnose_trajectory(trajectory)

    assert diagnosis.learnable is False
    assert "provider_failure" in diagnosis.reason_codes


@pytest.mark.asyncio
async def test_local_repository_is_idempotent_scoped_and_keeps_feedback_separate(
    tmp_path: Path,
) -> None:
    repository = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    service = HarnessExperienceService(repository)
    trajectory = _trajectory()

    first = await service.collect(trajectory, trigger="run_completed")
    repeated = await service.collect(trajectory, trigger="run_completed")
    feedback = await service.collect(
        trajectory.model_copy(update={"feedback_score": -1.0}),
        trigger="feedback_received",
    )

    assert first.experience_created is True
    assert repeated.experience_created is False
    assert feedback.experience.experience_id == first.experience.experience_id
    assert feedback.evaluation.evaluation_id != first.evaluation.evaluation_id
    assert len(
        await repository.list_evaluations(
            first.experience.experience_id,
            tenant_id="tenant-a",
            project_id="project-a",
        )
    ) == 2
    assert (
        await repository.get(
            first.experience.experience_id,
            tenant_id="tenant-b",
            project_id="project-a",
        )
        is None
    )


@pytest.mark.asyncio
async def test_local_repository_rejects_same_identity_with_different_payload(
    tmp_path: Path,
) -> None:
    repository = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    experience = assemble_experience(_trajectory())
    await repository.save(experience)
    changed_payload = experience.model_dump(mode="python", exclude={"payload_hash"})
    changed_payload["task_fingerprint"] = "f" * 64
    changed = experience.__class__.model_validate(
        {**changed_payload, "payload_hash": canonical_hash(changed_payload)}
    )

    with pytest.raises(HarnessExperienceConflictError):
        await repository.save(changed)


def test_feedback_evaluation_does_not_change_main_experience() -> None:
    trajectory = _trajectory()
    experience = assemble_experience(trajectory)
    evaluation = assemble_evaluation(
        trajectory.model_copy(update={"feedback_score": -0.5}),
        experience,
        trigger="feedback_received",
        native_change_set_ids=[],
    )

    assert experience.reward_vector.feedback_score is None
    assert evaluation.reward_vector.feedback_score == -0.5
    assert evaluation.reward_vector.passed is False
