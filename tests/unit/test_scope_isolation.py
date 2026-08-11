from pathlib import Path

import pytest

from app.domain.enums import RunStatus
from app.domain.models import RunContext, RunTrajectory, SkillDefinition, SkillStep
from app.infra.local_repositories import JsonlTrajectoryRepository
from app.skills.skill_markdown_repository import SkillMarkdownRepository


@pytest.mark.asyncio
async def test_similar_trajectory_search_never_crosses_scope(tmp_path: Path) -> None:
    repository = JsonlTrajectoryRepository(tmp_path / "runs.jsonl")
    source = RunTrajectory(
        context=RunContext(tenant_id="tenant-a", project_id="project-a"),
        user_input="compare retrieval approaches",
        status=RunStatus.COMPLETED,
    )
    foreign = RunTrajectory(
        context=RunContext(tenant_id="tenant-b", project_id="project-a"),
        user_input="compare retrieval approaches",
        status=RunStatus.COMPLETED,
    )
    same_scope = RunTrajectory(
        context=RunContext(tenant_id="tenant-a", project_id="project-a"),
        user_input="compare retrieval methods",
        status=RunStatus.COMPLETED,
    )
    await repository.save(foreign)
    await repository.save(same_scope)

    matches = await repository.find_similar(source)

    assert [item.context.run_id for item in matches] == [same_scope.context.run_id]


@pytest.mark.asyncio
async def test_session_history_never_crosses_user_or_session(tmp_path: Path) -> None:
    repository = JsonlTrajectoryRepository(tmp_path / "runs.jsonl")
    matching = RunTrajectory(
        context=RunContext(user_id="user-a", session_id="session-a"),
        user_input="matching",
        status=RunStatus.COMPLETED,
    )
    other_user = RunTrajectory(
        context=RunContext(user_id="user-b", session_id="session-a"),
        user_input="other user",
        status=RunStatus.COMPLETED,
    )
    other_session = RunTrajectory(
        context=RunContext(user_id="user-a", session_id="session-b"),
        user_input="other session",
        status=RunStatus.COMPLETED,
    )
    for item in (matching, other_user, other_session):
        await repository.save(item)

    history = await repository.list_session(
        user_id="user-a",
        session_id="session-a",
    )

    assert [item.context.run_id for item in history] == [matching.context.run_id]


@pytest.mark.asyncio
async def test_trajectory_backfill_can_read_more_than_online_history_limit(
    tmp_path: Path,
) -> None:
    repository = JsonlTrajectoryRepository(tmp_path / "runs.jsonl")

    assert await repository.list_recent(limit=10_000) == []
    with pytest.raises(ValueError, match="10000"):
        await repository.list_recent(limit=10_001)
    with pytest.raises(ValueError, match="200"):
        await repository.list_session(limit=201)


@pytest.mark.asyncio
async def test_skill_repository_scopes_same_name_and_version(tmp_path: Path) -> None:
    repository = SkillMarkdownRepository(tmp_path / "skills")
    source_run = RunContext().run_id

    def scoped_skill(tenant_id: str) -> SkillDefinition:
        return SkillDefinition(
            tenant_id=tenant_id,
            project_id="project-a",
            name="scoped_skill",
            version="0.1.0",
            description="A scoped declarative skill for isolation testing.",
            steps=[SkillStep(action="search_knowledge", purpose="Search scoped knowledge")],
            source_run_ids=[source_run],
        )

    tenant_a = await repository.save(scoped_skill("tenant-a"))
    tenant_b = await repository.save(scoped_skill("tenant-b"))

    assert tenant_a.skill_id != tenant_b.skill_id
    assert (
        await repository.get(
            tenant_a.skill_id,
            tenant_id="tenant-b",
            project_id="project-a",
        )
        is None
    )
    assert len(list((tmp_path / "skills").glob("*/*/*/SKILL.md"))) == 2
