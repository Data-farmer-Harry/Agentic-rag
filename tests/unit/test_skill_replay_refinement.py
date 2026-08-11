from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest

from app.domain.enums import RunStatus, SkillStatus
from app.domain.models import RunContext, RunTrajectory, SkillDefinition, SkillStep, ToolEvent
from app.learning.refinement import SkillRefiner
from app.learning.skill_replay import FrozenCapabilitySkillSandbox
from app.skills.skill_markdown_repository import SkillMarkdownRepository


def _trajectory(
    *actions: str,
    failed_action: str | None = None,
) -> RunTrajectory:
    return RunTrajectory(
        context=RunContext(project_id="replay-tests"),
        user_input="Research and verify the requested technical claim",
        status=RunStatus.COMPLETED,
        tool_events=[
            ToolEvent(
                tool_name=action,
                input_hash=f"fixture-input-{index}",
                output_summary=(
                    "private fixture output that must never enter the replay report"
                ),
                detail={"private_token": "not-for-persistence"},
                success=action != failed_action,
            )
            for index, action in enumerate(actions)
        ],
    )


def _skill(
    *actions: str,
    status: SkillStatus = SkillStatus.DRAFT,
    version: str = "0.1.0",
    skill_id: UUID | None = None,
    source_run_ids: list[UUID] | None = None,
) -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id or uuid4(),
        project_id="replay-tests",
        name="research_and_verify",
        version=version,
        description="Research a technical claim and verify the supporting evidence.",
        status=status,
        trigger_intents=["research"],
        trigger_phrases=["research and verify"],
        steps=[
            SkillStep(action=action, purpose=f"Execute bounded {action}")
            for action in actions
        ],
        allowed_capabilities=list(dict.fromkeys(actions)),
        constraints={"max_tool_calls": len(actions)},
        source_run_ids=source_run_ids or [UUID(int=1), UUID(int=2)],
    )


@pytest.mark.asyncio
async def test_frozen_skill_replay_executes_exact_fixture_without_raw_outputs() -> None:
    trajectory = _trajectory("search_knowledge", "fetch_evidence", "verify_evidence")
    candidate = _skill("search_knowledge", "fetch_evidence", "verify_evidence")

    report = await FrozenCapabilitySkillSandbox().replay(candidate, trajectory)

    assert report.passed
    assert report.reasons == ["sandbox_replay_passed"]
    assert [item.fixture_event_index for item in report.steps] == [0, 1, 2]
    assert all(
        item.output_hash is not None and len(item.output_hash) == 64
        for item in report.steps
    )
    serialized = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    assert "private fixture output" not in serialized
    assert "not-for-persistence" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate_actions", "fixture_actions", "failed_action", "expected_reason"),
    [
        (
            ("fetch_evidence", "search_knowledge"),
            ("search_knowledge", "fetch_evidence"),
            None,
            "fixture_action_mismatch",
        ),
        (
            ("search_knowledge",),
            ("search_knowledge",),
            "search_knowledge",
            "fixture_tool_failed",
        ),
    ],
)
async def test_frozen_skill_replay_rejects_divergence_and_failed_tools(
    candidate_actions: tuple[str, ...],
    fixture_actions: tuple[str, ...],
    failed_action: str | None,
    expected_reason: str,
) -> None:
    report = await FrozenCapabilitySkillSandbox().replay(
        _skill(*candidate_actions),
        _trajectory(*fixture_actions, failed_action=failed_action),
    )

    assert not report.passed
    assert expected_reason in report.reasons


@pytest.mark.asyncio
async def test_frozen_skill_replay_enforces_step_budget_before_execution() -> None:
    report = await FrozenCapabilitySkillSandbox(max_steps=1).replay(
        _skill("search_knowledge", "verify_evidence"),
        _trajectory("search_knowledge", "verify_evidence"),
    )

    assert not report.completed
    assert report.steps == []
    assert report.reasons == ["sandbox_step_budget_exceeded"]


def test_skill_refiner_assigns_semver_from_semantic_change() -> None:
    parent = _skill(
        "search_knowledge",
        "verify_evidence",
        status=SkillStatus.SHADOW,
    )
    parent_payload = parent.model_dump(mode="json")
    refiner = SkillRefiner(min_new_source_runs=2)

    patch = refiner.refine(
        parent,
        _skill(
            "search_knowledge",
            "verify_evidence",
            source_run_ids=[UUID(int=index) for index in (1, 2, 3, 4)],
        ),
    )
    minor = refiner.refine(
        parent,
        _skill(
            "search_knowledge",
            "verify_evidence",
            "summarize_evidence",
            source_run_ids=[UUID(int=index) for index in (1, 2, 3)],
        ),
    )
    major = refiner.refine(
        parent,
        _skill(
            "search_knowledge",
            source_run_ids=[UUID(int=index) for index in (1, 2, 3)],
        ),
    )

    assert patch.change_level == "patch"
    assert patch.candidate is not None and patch.candidate.version == "0.1.1"
    assert minor.change_level == "minor"
    assert minor.candidate is not None and minor.candidate.version == "0.2.0"
    assert major.change_level == "major"
    assert major.candidate is not None and major.candidate.version == "1.0.0"
    assert major.semantic_diff["removed_actions"] == ["verify_evidence"]
    for decision in (patch, minor, major):
        assert decision.candidate is not None
        assert decision.candidate.skill_id == parent.skill_id
        assert decision.candidate.parent_version == parent.version
        assert decision.candidate.status == SkillStatus.DRAFT
    assert parent.model_dump(mode="json") == parent_payload


def test_skill_refiner_requires_observable_parent_and_new_evidence() -> None:
    draft = _skill("search_knowledge")
    shadow = draft.model_copy(update={"status": SkillStatus.SHADOW})

    draft_decision = SkillRefiner().refine(draft, _skill("search_knowledge"))
    insufficient = SkillRefiner().refine(shadow, _skill("search_knowledge"))

    assert draft_decision.candidate is None
    assert draft_decision.reasons == ("parent_not_promotion_ready_for_refinement",)
    assert insufficient.candidate is None
    assert insufficient.change_level == "none"
    assert insufficient.reasons[0].startswith("insufficient_new_refinement_evidence")


@pytest.mark.asyncio
async def test_skill_repository_preserves_versions_and_resolves_exact_version(
    tmp_path,
) -> None:
    repository = SkillMarkdownRepository(tmp_path / "skills")
    parent = _skill("search_knowledge", status=SkillStatus.SHADOW)
    child = _skill(
        "search_knowledge",
        "verify_evidence",
        skill_id=parent.skill_id,
        version="0.2.0",
        source_run_ids=[UUID(int=index) for index in (1, 2, 3)],
    ).model_copy(update={"parent_version": parent.version})
    await repository.save(parent)
    await repository.save(child)

    latest = await repository.get(
        parent.skill_id,
        project_id="replay-tests",
    )
    exact_parent = await repository.get(
        parent.skill_id,
        project_id="replay-tests",
        version="0.1.0",
    )

    assert latest == child
    assert exact_parent == parent
    assert [item.version for item in await repository.list_all()] == ["0.1.0", "0.2.0"]
