from pathlib import Path

import pytest

from app.bootstrap import build_components
from app.config import Settings
from app.domain.enums import SkillStatus


@pytest.mark.asyncio
async def test_three_repeated_runs_create_only_a_draft_skill(tmp_path: Path) -> None:
    components = build_components(
        Settings(
            app_env="test",
            data_dir=tmp_path,
            runtime_mode="offline",
            learning_mode="observe",
        )
    )

    for index in range(3):
        trajectory = await components.run_service.run(
            "Hermes Agent Loop and LangChain integration",
            session_id=f"learning-{index}",
        )
        assert trajectory.tool_events[0].tool_name == "search_knowledge"

    drafts = await components.skill_repository.list_by_status("draft")
    active = await components.skill_repository.list_by_status("active")

    assert len(drafts) == 1
    assert drafts[0].status == SkillStatus.DRAFT
    assert drafts[0].allowed_capabilities == ["search_knowledge"]
    assert active == []


@pytest.mark.asyncio
async def test_shadow_mode_automatically_evaluates_new_skill(tmp_path: Path) -> None:
    components = build_components(
        Settings(
            app_env="test",
            data_dir=tmp_path,
            runtime_mode="offline",
            learning_mode="shadow",
        )
    )

    for index in range(3):
        await components.run_service.run(
            "Hermes Agent Loop and LangChain integration",
            session_id=f"auto-stage-{index}",
        )

    shadows = await components.skill_repository.list_by_status("shadow")
    snapshots = await components.skill_evolution_service.snapshots(shadows)

    assert len(shadows) == 1
    assert snapshots[0].latest_evaluation is not None
    assert snapshots[0].latest_evaluation.regression_passed
    assert snapshots[0].health is not None
    assert snapshots[0].health.evaluated_observations == 0
