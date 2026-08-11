from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent.hermes_bridge import HermesCapabilityBridge, RunBudgetExceeded
from app.api.app import create_app
from app.bootstrap import build_components
from app.config import Settings
from app.domain.enums import MemoryType, TrustLevel
from app.domain.models import MemoryCandidate, Provenance, RunContext, utc_now
from app.infra.local_repositories import JsonlTrajectoryRepository
from app.memory.json_memory_repository import JsonMemoryStore
from app.personal.models import (
    ChecklistItemCreate,
    ChecklistItemPatch,
    EmotionOverrideRequest,
    EmotionState,
    MemoryCorrectionRequest,
    NoteUpsert,
    PersonaUpdate,
    PlanCreate,
    PlanStepCreate,
    PlanStepPatch,
    ReminderSnoozeRequest,
    StepStatus,
    TaskCreate,
    TaskPatch,
    TaskStatus,
)
from app.personal.repository import JsonPersonalRepository, PersonalVersionConflict
from app.personal.service import PersonalControlService
from app.retrieval.in_memory_retriever import InMemoryRetriever


def personal_service(tmp_path: Path) -> PersonalControlService:
    return PersonalControlService(
        JsonPersonalRepository(tmp_path / "personal.json"),
        memories=JsonMemoryStore(tmp_path / "memory.json"),
        trajectories=JsonlTrajectoryRepository(tmp_path / "trajectories.jsonl"),
    )


@pytest.mark.asyncio
async def test_personal_control_vertical_slice_and_scope(tmp_path: Path) -> None:
    service = personal_service(tmp_path)
    context = RunContext(project_id="project-a", user_id="harry")

    task = await service.create_task(
        TaskCreate(title="实现个人控制平面", priority=1, tags=["agent"]),
        context,
    )
    task = await service.update_task(
        task.task_id,
        TaskPatch(
            status=TaskStatus.IN_PROGRESS,
            expected_version=task.version,
        ),
        context,
    )
    plan = await service.create_plan(
        PlanCreate(task_id=task.task_id, title="端到端实现"),
        context,
    )
    step = await service.create_plan_step(
        plan.plan_id,
        PlanStepCreate(title="完成后端"),
        context,
    )
    step = await service.update_plan_step(
        step.step_id,
        PlanStepPatch(
            status=StepStatus.COMPLETED,
            expected_version=step.version,
        ),
        context,
    )
    item = await service.create_checklist_item(
        ChecklistItemCreate(task_id=task.task_id, label="运行测试"),
        context,
    )
    item = await service.update_checklist_item(
        item.item_id,
        ChecklistItemPatch(checked=True, expected_version=item.version),
        context,
    )
    note = await service.upsert_note(
        NoteUpsert(
            kind="task",
            title="实现判断",
            content="Persona 和 emotion 只进入运行时胶囊。",
            task_id=task.task_id,
        ),
        context,
    )
    daily_note = await service.upsert_note(
        NoteUpsert(
            kind="daily",
            title="当天判断",
            content="快速记录已经进入日历回顾。",
            note_date=date.today(),
        ),
        context,
    )
    persona = await service.update_persona(
        PersonaUpdate(
            user_display_name="Harry",
            interests=["agent", "knowledge graph"],
            complete_onboarding=True,
            expected_version=1,
        ),
        context,
    )
    emotion = await service.set_emotion_override(
        EmotionOverrideRequest(
            state=EmotionState.FOCUSED,
            duration_minutes=60,
        ),
        context,
    )
    archive = await service.seal_day(date.today(), context)
    capsule = await service.compile_runtime_capsule(context)
    tool_checklist = await service.execute_tool(
        "manage_personal_tasks",
        {"action": "list_checklist", "task_id": str(task.task_id)},
        context,
    )
    tool_profile = await service.execute_tool(
        "manage_personal_profile",
        {"action": "get"},
        context,
    )
    tool_journal = await service.execute_tool(
        "manage_personal_journal",
        {"action": "get", "archive_date": date.today().isoformat()},
        context,
    )

    assert task.status == TaskStatus.IN_PROGRESS
    assert step.status == StepStatus.COMPLETED
    assert item.checked is True
    assert note.task_id == task.task_id
    assert daily_note.note_date == date.today()
    assert persona.onboarding_completed_at is not None
    assert emotion.state == EmotionState.FOCUSED
    assert archive.archive_date == date.today()
    assert "1 条笔记" in archive.summary
    assert any("当天判断" in item for item in archive.highlights)
    assert "实现个人控制平面" in archive.open_loops
    assert '"style_only": true' in capsule
    assert "实现个人控制平面" in capsule
    assert tool_checklist[0]["checked"] is True
    assert tool_profile["emotion"]["state"] == "focused"
    assert tool_journal["archive_date"] == date.today().isoformat()
    assert await service.list_tasks(
        RunContext(project_id="project-b", user_id="harry")
    ) == []


@pytest.mark.asyncio
async def test_personal_records_enforce_optimistic_versioning(tmp_path: Path) -> None:
    service = personal_service(tmp_path)
    context = RunContext()
    task = await service.create_task(TaskCreate(title="版本测试"), context)

    with pytest.raises(PersonalVersionConflict):
        await service.update_task(
            task.task_id,
            TaskPatch(title="冲突写入", expected_version=99),
            context,
        )


@pytest.mark.asyncio
async def test_task_reminders_project_due_tasks_and_reset_after_reschedule(
    tmp_path: Path,
) -> None:
    service = personal_service(tmp_path)
    context = RunContext(project_id="reminders", user_id="harry")
    now = datetime(2026, 8, 2, 1, 0, tzinfo=UTC)
    overdue = await service.create_task(
        TaskCreate(title="已经逾期", due_at=now - timedelta(minutes=30), priority=1),
        context,
    )
    due_soon = await service.create_task(
        TaskCreate(title="马上到期", due_at=now + timedelta(minutes=30), priority=2),
        context,
    )
    later = await service.create_task(
        TaskCreate(title="今天稍后", due_at=now + timedelta(hours=4), priority=3),
        context,
    )
    await service.create_task(
        TaskCreate(title="暂不提醒", due_at=now + timedelta(days=3)),
        context,
    )
    completed = await service.create_task(
        TaskCreate(title="已完成", due_at=now - timedelta(hours=1)),
        context,
    )
    await service.update_task(
        completed.task_id,
        TaskPatch(status=TaskStatus.COMPLETED, expected_version=completed.version),
        context,
    )

    initial = await service.list_task_reminders(context, now=now)
    assert [item.task_id for item in initial.items] == [
        overdue.task_id,
        due_soon.task_id,
        later.task_id,
    ]
    assert [item.kind.value for item in initial.items] == [
        "overdue",
        "due_soon",
        "today",
    ]
    assert initial.unread_count == 3
    assert initial.timezone == "Asia/Shanghai"

    read = await service.mark_task_reminder_read(overdue.task_id, context)
    assert next(item for item in read.items if item.task_id == overdue.task_id).unread is False

    snoozed = await service.snooze_task_reminder(
        due_soon.task_id,
        ReminderSnoozeRequest(duration_minutes=60),
        context,
    )
    assert all(item.task_id != due_soon.task_id for item in snoozed.items)

    changed = await service.update_task(
        overdue.task_id,
        TaskPatch(
            due_at=now + timedelta(minutes=45),
            expected_version=overdue.version,
        ),
        context,
    )
    refreshed = await service.list_task_reminders(context, now=now)
    changed_reminder = next(
        item for item in refreshed.items if item.task_id == changed.task_id
    )
    assert changed_reminder.kind.value == "due_soon"
    assert changed_reminder.unread is True


@pytest.mark.asyncio
async def test_natural_language_memory_correction_requires_disambiguation(
    tmp_path: Path,
) -> None:
    memories = JsonMemoryStore(tmp_path / "memory.json")
    service = PersonalControlService(
        JsonPersonalRepository(tmp_path / "personal.json"),
        memories=memories,
        trajectories=JsonlTrajectoryRepository(tmp_path / "trajectories.jsonl"),
    )
    context = RunContext()
    provenance = [
        Provenance(
            source_type="explicit_user",
            source_id="fixture",
            trust=TrustLevel.USER_ASSERTED,
        )
    ]
    first = await memories.upsert(
        MemoryCandidate(
            memory_type=MemoryType.SEMANTIC,
            key="language-primary",
            summary="我偏好 Java",
            confidence=1,
            provenance=provenance,
        )
    )
    await memories.upsert(
        MemoryCandidate(
            memory_type=MemoryType.SEMANTIC,
            key="language-secondary",
            summary="Java 是我之前常用的语言",
            confidence=1,
            provenance=provenance,
        )
    )

    pending = await service.correct_memory(
        MemoryCorrectionRequest(
            request="把“Java”更正为“我现在主要使用 Python”"
        ),
        context,
    )
    applied = await service.correct_memory(
        MemoryCorrectionRequest(
            request="把“Java”更正为“我现在主要使用 Python”",
            confirm_memory_ids=[first.memory_id],
        ),
        context,
    )

    assert pending.status == "needs_confirmation"
    assert len(pending.candidates) == 2
    assert applied.status == "applied"
    assert applied.revoked_memory_ids == [first.memory_id]
    assert applied.created_memory is not None
    assert applied.created_memory.summary == "我现在主要使用 Python"


@pytest.mark.asyncio
async def test_personal_api_exposes_all_control_plane_surfaces(
    tmp_path: Path,
) -> None:
    components = build_components(Settings(app_env="test", data_dir=tmp_path))
    app = create_app(
        components.run_service,
        components.workspace_service,
        personal=components.personal_service,
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/projects/default/personal/tasks",
            json={"title": "API 任务", "priority": 2},
        )
        task_id = created.json()["task_id"]
        reminder_task = await client.post(
            "/v1/projects/default/personal/tasks",
            json={
                "title": "API 到期提醒",
                "priority": 1,
                "due_at": (utc_now() + timedelta(minutes=30)).isoformat(),
            },
        )
        tasks = await client.get("/v1/projects/default/personal/tasks")
        reminders = await client.get("/v1/projects/default/personal/reminders")
        reminder_read = await client.put(
            f"/v1/projects/default/personal/reminders/{reminder_task.json()['task_id']}/read"
        )
        reminder_snoozed = await client.put(
            f"/v1/projects/default/personal/reminders/{reminder_task.json()['task_id']}/snooze",
            json={"duration_minutes": 60},
        )
        plan = await client.post(
            "/v1/projects/default/personal/plans",
            json={"task_id": task_id, "title": "API 计划"},
        )
        step = await client.post(
            f"/v1/projects/default/personal/plans/{plan.json()['plan_id']}/steps",
            json={"title": "API 步骤"},
        )
        persona = await client.put(
            "/v1/projects/default/personal/persona",
            json={"user_display_name": "Harry", "complete_onboarding": True},
        )
        emotion = await client.put(
            "/v1/projects/default/personal/emotion/override",
            json={"state": "curious", "duration_minutes": 30},
        )
        archive = await client.post(
            f"/v1/projects/default/personal/days/{date.today().isoformat()}/seal"
        )

    assert created.status_code == 201
    assert any(item["title"] == "API 任务" for item in tasks.json())
    assert reminders.json()["items"][0]["title"] == "API 到期提醒"
    assert reminder_read.json()["items"][0]["unread"] is False
    assert reminder_snoozed.json()["items"] == []
    assert plan.status_code == 201
    assert step.status_code == 201
    assert persona.json()["onboarding_completed_at"] is not None
    assert emotion.json()["state"] == "curious"
    assert archive.json()["archive_date"] == date.today().isoformat()


@pytest.mark.asyncio
async def test_hermes_bridge_executes_scoped_personal_tools_with_budget(
    tmp_path: Path,
) -> None:
    service = personal_service(tmp_path)
    context = RunContext(project_id="personal-tools", user_id="harry")
    bridge = HermesCapabilityBridge(
        settings=Settings(
            app_env="test",
            hermes_bridge_token="bridge-secret",
            max_personal_tool_calls=1,
        ),
        retrieval=InMemoryRetriever.from_texts(["fixture"]),
        personal=service,
    )
    bridge_id = await bridge.open_run(context)

    created = await bridge.invoke(
        bridge_id,
        "manage_personal_tasks",
        {
            "action": "create",
            "task": {"title": "由 Hermes 创建"},
        },
    )

    assert created["result"]["title"] == "由 Hermes 创建"
    with pytest.raises(RunBudgetExceeded):
        await bridge.invoke(
            bridge_id,
            "manage_personal_tasks",
            {"action": "list"},
        )
