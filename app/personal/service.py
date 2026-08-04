from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.contracts import MemoryRepository
from app.domain.enums import MemoryType, TrustLevel
from app.domain.models import MemoryCandidate, Provenance, RunContext, utc_now
from app.infra.local_repositories import JsonlTrajectoryRepository
from app.personal.models import (
    ChecklistItem,
    ChecklistItemCreate,
    ChecklistItemPatch,
    DayArchive,
    DayArchivePatch,
    EmotionOverride,
    EmotionOverrideRequest,
    EmotionSnapshot,
    EmotionState,
    MemoryCorrectionRequest,
    MemoryCorrectionResult,
    Note,
    NoteUpsert,
    PersonalRecordEnvelope,
    PersonalRecordType,
    PersonaProfile,
    PersonaUpdate,
    Plan,
    PlanCreate,
    PlanPatch,
    PlanStatus,
    PlanStep,
    PlanStepCreate,
    PlanStepPatch,
    ReminderKind,
    ReminderSnoozeRequest,
    ScopedRecord,
    StepStatus,
    Task,
    TaskCreate,
    TaskPatch,
    TaskReminder,
    TaskReminderFeed,
    TaskReminderState,
    TaskStatus,
)
from app.personal.repository import PersonalRepository

_FORGET_PATTERNS = (
    re.compile(r"^(?:请)?(?:忘记|删除|撤销|移除|不要再记得)\s*(?:关于|掉)?\s*(.+)$", re.I),
    re.compile(
        r"^(?:please\s+)?(?:forget|remove|delete)\s+"
        r"(?:the\s+memory\s+)?(?:about\s+)?(.+)$",
        re.I,
    ),
)
_REPLACE_PATTERNS = (
    re.compile(r"^(?:把)?(.+?)\s*(?:改成|更正为|修改为)\s*(.+)$", re.I),
    re.compile(r"^不是\s*(.+?)[，,]\s*(?:而是|是)\s*(.+)$", re.I),
    re.compile(r"^(?:replace|correct)\s+(.+?)\s+(?:with|to)\s+(.+)$", re.I),
)
_EMOTION_META: dict[EmotionState, tuple[str, float, float, str]] = {
    EmotionState.CALM: ("平静", 0.25, 0.35, "保持温和、清晰和不过度主动的表达。"),
    EmotionState.FOCUSED: ("专注", 0.20, 0.72, "回答更紧凑，突出当前行动和下一步。"),
    EmotionState.CURIOUS: ("好奇", 0.45, 0.62, "允许提出一个真正有帮助的澄清或探索方向。"),
    EmotionState.SUPPORTIVE: ("支持", 0.38, 0.48, "承认阻塞，帮助拆小问题，不制造压力。"),
    EmotionState.CELEBRATING: ("振奋", 0.78, 0.75, "简短认可进展，并把注意力带回下一步。"),
    EmotionState.REFLECTIVE: ("回顾", 0.30, 0.30, "连接近期经验与未完成事项，保持克制。"),
    EmotionState.RESTING: ("安静", 0.10, 0.15, "降低主动性和信息密度，避免不必要打扰。"),
}


class PersonalControlError(RuntimeError):
    pass


class PersonalRecordNotFound(PersonalControlError):
    pass


class PersonalControlService:
    def __init__(
        self,
        repository: PersonalRepository,
        *,
        memories: MemoryRepository,
        trajectories: JsonlTrajectoryRepository,
    ) -> None:
        self._repository = repository
        self._memories = memories
        self._trajectories = trajectories

    async def create_task(
        self,
        command: TaskCreate,
        context: RunContext,
    ) -> Task:
        now = utc_now()
        task = Task(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            title=command.title,
            description=command.description,
            priority=command.priority,
            due_at=command.due_at,
            tags=command.tags,
            created_at=now,
            updated_at=now,
        )
        return Task.model_validate(
            await self._persist(
                task,
                PersonalRecordType.TASK,
                task.task_id,
                status=task.status.value,
                expected_version=None,
                event_type="task.created",
                actor_id=context.user_id,
            )
        )

    async def list_tasks(
        self,
        context: RunContext,
        *,
        status: TaskStatus | None = None,
        include_archived: bool = False,
    ) -> list[Task]:
        rows = await self._repository.list_records(
            PersonalRecordType.TASK,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            status=status.value if status is not None else None,
        )
        tasks = [Task.model_validate(item.payload) for item in rows]
        if not include_archived:
            tasks = [item for item in tasks if item.status != TaskStatus.ARCHIVED]
        return sorted(
            tasks,
            key=lambda item: (
                item.status in {TaskStatus.COMPLETED, TaskStatus.ARCHIVED},
                item.due_at or datetime.max.replace(tzinfo=UTC),
                -item.priority,
                item.created_at,
            ),
        )

    async def get_task(self, task_id: UUID, context: RunContext) -> Task:
        return Task.model_validate(
            (await self._require(PersonalRecordType.TASK, task_id, context)).payload
        )

    async def update_task(
        self,
        task_id: UUID,
        patch: TaskPatch,
        context: RunContext,
    ) -> Task:
        current = await self.get_task(task_id, context)
        values = patch.model_dump(exclude_unset=True, exclude={"expected_version"})
        status = cast(TaskStatus | None, values.get("status"))
        if status == TaskStatus.COMPLETED and current.completed_at is None:
            values["completed_at"] = utc_now()
        elif status is not None and status != TaskStatus.COMPLETED:
            values["completed_at"] = None
        updated = current.model_copy(
            update={
                **values,
                "version": current.version + 1,
                "updated_at": utc_now(),
            }
        )
        return Task.model_validate(
            await self._persist(
                updated,
                PersonalRecordType.TASK,
                updated.task_id,
                status=updated.status.value,
                expected_version=patch.expected_version or current.version,
                event_type="task.updated",
                actor_id=context.user_id,
            )
        )

    async def list_task_reminders(
        self,
        context: RunContext,
        *,
        now: datetime | None = None,
    ) -> TaskReminderFeed:
        observed_at = self._aware_datetime(now or utc_now(), UTC)
        persona = await self.get_persona(context)
        timezone = self._timezone(persona.timezone)
        tasks = await self.list_tasks(context, include_archived=False)
        state_rows = await self._repository.list_records(
            PersonalRecordType.REMINDER_STATE,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            limit=2_000,
        )
        states = {
            state.task_id: state
            for state in (
                TaskReminderState.model_validate(item.payload) for item in state_rows
            )
        }
        reminders: list[TaskReminder] = []
        for task in tasks:
            if task.status in {TaskStatus.COMPLETED, TaskStatus.ARCHIVED}:
                continue
            if task.due_at is None:
                continue
            due_at = self._aware_datetime(task.due_at, timezone)
            kind = self._reminder_kind(due_at, observed_at, timezone)
            if kind is None:
                continue
            state = states.get(task.task_id)
            same_due_state = (
                state
                if state is not None
                and self._aware_datetime(state.due_at, timezone) == due_at
                else None
            )
            if (
                same_due_state is not None
                and same_due_state.snoozed_until is not None
                and self._aware_datetime(same_due_state.snoozed_until, timezone) > observed_at
            ):
                continue
            current_state = (
                same_due_state
                if same_due_state is not None and same_due_state.kind == kind
                else None
            )
            reminders.append(
                TaskReminder(
                    task_id=task.task_id,
                    title=task.title,
                    kind=kind,
                    due_at=due_at,
                    priority=task.priority,
                    unread=current_state is None or current_state.read_at is None,
                    snoozed_until=(
                        current_state.snoozed_until if current_state is not None else None
                    ),
                )
            )
        kind_order = {
            ReminderKind.OVERDUE: 0,
            ReminderKind.DUE_SOON: 1,
            ReminderKind.TODAY: 2,
        }
        reminders.sort(
            key=lambda item: (kind_order[item.kind], item.due_at, -item.priority)
        )
        return TaskReminderFeed(
            items=reminders,
            unread_count=sum(item.unread for item in reminders),
            timezone=persona.timezone,
            generated_at=observed_at,
        )

    async def mark_task_reminder_read(
        self,
        task_id: UUID,
        context: RunContext,
    ) -> TaskReminderFeed:
        await self._write_reminder_state(task_id, context, read=True)
        return await self.list_task_reminders(context)

    async def mark_all_task_reminders_read(
        self,
        context: RunContext,
    ) -> TaskReminderFeed:
        feed = await self.list_task_reminders(context)
        for reminder in feed.items:
            if reminder.unread:
                await self._write_reminder_state(reminder.task_id, context, read=True)
        return await self.list_task_reminders(context)

    async def snooze_task_reminder(
        self,
        task_id: UUID,
        command: ReminderSnoozeRequest,
        context: RunContext,
    ) -> TaskReminderFeed:
        await self._write_reminder_state(
            task_id,
            context,
            read=True,
            snoozed_until=utc_now() + timedelta(minutes=command.duration_minutes),
        )
        return await self.list_task_reminders(context)

    async def create_plan(self, command: PlanCreate, context: RunContext) -> Plan:
        if command.task_id is not None:
            await self.get_task(command.task_id, context)
        now = utc_now()
        plan = Plan(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            task_id=command.task_id,
            title=command.title,
            objective=command.objective,
            target_date=command.target_date,
            created_at=now,
            updated_at=now,
        )
        return Plan.model_validate(
            await self._persist(
                plan,
                PersonalRecordType.PLAN,
                plan.plan_id,
                parent_id=plan.task_id,
                status=plan.status.value,
                expected_version=None,
                event_type="plan.created",
                actor_id=context.user_id,
            )
        )

    async def list_plans(
        self,
        context: RunContext,
        *,
        task_id: UUID | None = None,
        include_archived: bool = False,
    ) -> list[Plan]:
        rows = await self._repository.list_records(
            PersonalRecordType.PLAN,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            parent_id=task_id,
        )
        plans = [Plan.model_validate(item.payload) for item in rows]
        if not include_archived:
            plans = [item for item in plans if item.status != PlanStatus.ARCHIVED]
        return sorted(plans, key=lambda item: (item.created_at, str(item.plan_id)), reverse=True)

    async def get_plan(self, plan_id: UUID, context: RunContext) -> Plan:
        return Plan.model_validate(
            (await self._require(PersonalRecordType.PLAN, plan_id, context)).payload
        )

    async def update_plan(
        self,
        plan_id: UUID,
        patch: PlanPatch,
        context: RunContext,
    ) -> Plan:
        current = await self.get_plan(plan_id, context)
        values = patch.model_dump(exclude_unset=True, exclude={"expected_version"})
        updated = current.model_copy(
            update={**values, "version": current.version + 1, "updated_at": utc_now()}
        )
        return Plan.model_validate(
            await self._persist(
                updated,
                PersonalRecordType.PLAN,
                updated.plan_id,
                parent_id=updated.task_id,
                status=updated.status.value,
                expected_version=patch.expected_version or current.version,
                event_type="plan.updated",
                actor_id=context.user_id,
            )
        )

    async def create_plan_step(
        self,
        plan_id: UUID,
        command: PlanStepCreate,
        context: RunContext,
    ) -> PlanStep:
        await self.get_plan(plan_id, context)
        current_steps = await self.list_plan_steps(plan_id, context)
        now = utc_now()
        step = PlanStep(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            plan_id=plan_id,
            title=command.title,
            detail=command.detail,
            position=command.position if command.position is not None else len(current_steps),
            due_at=command.due_at,
            created_at=now,
            updated_at=now,
        )
        return PlanStep.model_validate(
            await self._persist(
                step,
                PersonalRecordType.PLAN_STEP,
                step.step_id,
                parent_id=plan_id,
                status=step.status.value,
                expected_version=None,
                event_type="plan_step.created",
                actor_id=context.user_id,
            )
        )

    async def list_plan_steps(self, plan_id: UUID, context: RunContext) -> list[PlanStep]:
        await self.get_plan(plan_id, context)
        rows = await self._repository.list_records(
            PersonalRecordType.PLAN_STEP,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            parent_id=plan_id,
        )
        return sorted(
            [PlanStep.model_validate(item.payload) for item in rows],
            key=lambda item: (item.position, item.created_at),
        )

    async def update_plan_step(
        self,
        step_id: UUID,
        patch: PlanStepPatch,
        context: RunContext,
    ) -> PlanStep:
        current = PlanStep.model_validate(
            (await self._require(PersonalRecordType.PLAN_STEP, step_id, context)).payload
        )
        values = patch.model_dump(exclude_unset=True, exclude={"expected_version"})
        status = cast(StepStatus | None, values.get("status"))
        if status == StepStatus.COMPLETED and current.completed_at is None:
            values["completed_at"] = utc_now()
        elif status is not None and status != StepStatus.COMPLETED:
            values["completed_at"] = None
        updated = current.model_copy(
            update={**values, "version": current.version + 1, "updated_at": utc_now()}
        )
        return PlanStep.model_validate(
            await self._persist(
                updated,
                PersonalRecordType.PLAN_STEP,
                updated.step_id,
                parent_id=updated.plan_id,
                status=updated.status.value,
                expected_version=patch.expected_version or current.version,
                event_type="plan_step.updated",
                actor_id=context.user_id,
            )
        )

    async def create_checklist_item(
        self,
        command: ChecklistItemCreate,
        context: RunContext,
    ) -> ChecklistItem:
        parent_id = command.task_id or command.step_id
        if command.task_id is not None:
            await self.get_task(command.task_id, context)
        elif command.step_id is not None:
            await self._require(PersonalRecordType.PLAN_STEP, command.step_id, context)
        current = await self.list_checklist(
            context,
            task_id=command.task_id,
            step_id=command.step_id,
        )
        now = utc_now()
        item = ChecklistItem(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            task_id=command.task_id,
            step_id=command.step_id,
            label=command.label,
            position=command.position if command.position is not None else len(current),
            created_at=now,
            updated_at=now,
        )
        return ChecklistItem.model_validate(
            await self._persist(
                item,
                PersonalRecordType.CHECKLIST_ITEM,
                item.item_id,
                parent_id=parent_id,
                status="checked" if item.checked else "open",
                expected_version=None,
                event_type="checklist_item.created",
                actor_id=context.user_id,
            )
        )

    async def list_checklist(
        self,
        context: RunContext,
        *,
        task_id: UUID | None = None,
        step_id: UUID | None = None,
    ) -> list[ChecklistItem]:
        parent_id = task_id or step_id
        if parent_id is None:
            return []
        rows = await self._repository.list_records(
            PersonalRecordType.CHECKLIST_ITEM,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            parent_id=parent_id,
        )
        items = [ChecklistItem.model_validate(item.payload) for item in rows]
        return sorted(items, key=lambda item: (item.position, item.created_at))

    async def update_checklist_item(
        self,
        item_id: UUID,
        patch: ChecklistItemPatch,
        context: RunContext,
    ) -> ChecklistItem:
        current = ChecklistItem.model_validate(
            (await self._require(PersonalRecordType.CHECKLIST_ITEM, item_id, context)).payload
        )
        values = patch.model_dump(exclude_unset=True, exclude={"expected_version"})
        if values.get("checked") is True:
            values["checked_at"] = utc_now()
        elif values.get("checked") is False:
            values["checked_at"] = None
        updated = current.model_copy(
            update={**values, "version": current.version + 1, "updated_at": utc_now()}
        )
        return ChecklistItem.model_validate(
            await self._persist(
                updated,
                PersonalRecordType.CHECKLIST_ITEM,
                updated.item_id,
                parent_id=updated.task_id or updated.step_id,
                status="checked" if updated.checked else "open",
                expected_version=patch.expected_version or current.version,
                event_type="checklist_item.updated",
                actor_id=context.user_id,
            )
        )

    async def upsert_note(self, command: NoteUpsert, context: RunContext) -> Note:
        if command.task_id is not None:
            await self.get_task(command.task_id, context)
        if command.plan_id is not None:
            await self.get_plan(command.plan_id, context)
        existing: Note | None = None
        if command.note_id is not None:
            existing = Note.model_validate(
                (await self._require(PersonalRecordType.NOTE, command.note_id, context)).payload
            )
        now = utc_now()
        note = Note(
            note_id=existing.note_id if existing is not None else command.note_id or uuid4(),
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            kind=command.kind,
            title=command.title,
            content=command.content,
            task_id=command.task_id,
            plan_id=command.plan_id,
            note_date=command.note_date,
            version=(existing.version + 1) if existing is not None else 1,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        return Note.model_validate(
            await self._persist(
                note,
                PersonalRecordType.NOTE,
                note.note_id,
                parent_id=note.task_id or note.plan_id,
                status=note.kind.value,
                record_date=note.note_date,
                expected_version=(
                    command.expected_version or existing.version
                    if existing is not None
                    else None
                ),
                event_type="note.updated" if existing is not None else "note.created",
                actor_id=context.user_id,
            )
        )

    async def list_notes(
        self,
        context: RunContext,
        *,
        task_id: UUID | None = None,
        plan_id: UUID | None = None,
        note_date: date | None = None,
    ) -> list[Note]:
        rows = await self._repository.list_records(
            PersonalRecordType.NOTE,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            parent_id=task_id or plan_id,
            date_from=note_date,
            date_to=note_date,
        )
        notes = [Note.model_validate(item.payload) for item in rows]
        if note_date is not None:
            notes = [item for item in notes if item.note_date == note_date]
        return sorted(notes, key=lambda item: item.updated_at, reverse=True)

    async def get_persona(self, context: RunContext) -> PersonaProfile:
        existing = await self._repository.get_by_key(
            PersonalRecordType.PERSONA,
            "primary",
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
        )
        if existing is not None:
            return PersonaProfile.model_validate(existing.payload)
        now = utc_now()
        persona = PersonaProfile(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            created_at=now,
            updated_at=now,
        )
        return PersonaProfile.model_validate(
            await self._persist(
                persona,
                PersonalRecordType.PERSONA,
                persona.persona_id,
                record_key="primary",
                status="new",
                expected_version=None,
                event_type="persona.created",
                actor_id=context.user_id,
            )
        )

    async def update_persona(
        self,
        patch: PersonaUpdate,
        context: RunContext,
    ) -> PersonaProfile:
        current = await self.get_persona(context)
        values = patch.model_dump(
            exclude_unset=True,
            exclude={"expected_version", "complete_onboarding", "reset_onboarding"},
        )
        if patch.complete_onboarding:
            values["onboarding_completed_at"] = utc_now()
        if patch.reset_onboarding:
            values["onboarding_completed_at"] = None
        if "timezone" in values:
            self._timezone(str(values["timezone"]))
        updated = current.model_copy(
            update={**values, "version": current.version + 1, "updated_at": utc_now()}
        )
        return PersonaProfile.model_validate(
            await self._persist(
                updated,
                PersonalRecordType.PERSONA,
                updated.persona_id,
                record_key="primary",
                status="ready" if updated.onboarding_completed_at is not None else "new",
                expected_version=patch.expected_version or current.version,
                event_type="persona.updated",
                actor_id=context.user_id,
            )
        )

    async def list_day_archives(
        self,
        context: RunContext,
        *,
        date_from: date,
        date_to: date,
    ) -> list[DayArchive]:
        rows = await self._repository.list_records(
            PersonalRecordType.DAY_ARCHIVE,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            date_from=date_from,
            date_to=date_to,
        )
        return sorted(
            [DayArchive.model_validate(item.payload) for item in rows],
            key=lambda item: item.archive_date,
        )

    async def get_day_archive(
        self,
        archive_date: date,
        context: RunContext,
    ) -> DayArchive | None:
        row = await self._repository.get_by_key(
            PersonalRecordType.DAY_ARCHIVE,
            archive_date.isoformat(),
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
        )
        return DayArchive.model_validate(row.payload) if row is not None else None

    async def seal_day(
        self,
        archive_date: date,
        context: RunContext,
        *,
        force: bool = False,
    ) -> DayArchive:
        existing = await self.get_day_archive(archive_date, context)
        if existing is not None and existing.sealed_at is not None and not force:
            return existing
        persona = await self.get_persona(context)
        timezone = self._timezone(persona.timezone)
        runs = [
            run
            for run in await self._trajectories.list_recent(
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                limit=200,
            )
            if run.context.user_id == context.user_id
            and run.context.started_at.astimezone(timezone).date() == archive_date
        ]
        tasks = await self.list_tasks(context, include_archived=False)
        notes = await self.list_notes(context, note_date=archive_date)
        completed = [
            item
            for item in tasks
            if item.completed_at is not None
            and item.completed_at.astimezone(timezone).date() == archive_date
        ]
        open_tasks = [
            item.title
            for item in tasks
            if item.status
            not in {TaskStatus.COMPLETED, TaskStatus.ARCHIVED}
        ][:20]
        highlights = [self._clip(run.user_input, 240) for run in reversed(runs[-10:])]
        highlights.extend(
            self._clip(
                f"记录：{item.title}" + (f" - {item.content}" if item.content else ""),
                240,
            )
            for item in notes[:10]
        )
        decisions = [f"完成任务：{item.title}" for item in completed]
        emotion = await self.current_emotion(context)
        display_name = persona.user_display_name or "你"
        summary = (
            f"{archive_date.isoformat()} 共完成 {len(runs)} 次 Agent 对话、"
            f"{len(completed)} 个任务，留下 {len(notes)} 条笔记；"
            f"仍有 {len(open_tasks)} 个开放事项。"
        )
        diary = (
            f"今天我陪 {display_name} 处理了 {len(runs)} 次对话。"
            f"我们完成了 {len(completed)} 个任务，"
            f"留下了 {len(notes)} 条当天记录，"
            f"当前最需要继续照看的开放事项有 {len(open_tasks)} 个。"
        )
        now = utc_now()
        archive = DayArchive(
            archive_id=existing.archive_id if existing is not None else uuid4(),
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            archive_date=archive_date,
            summary=summary,
            diary=diary,
            highlights=highlights,
            decisions=decisions,
            open_loops=open_tasks,
            emotion_state=emotion.state,
            run_ids=[run.context.run_id for run in runs],
            sealed_at=now,
            version=(existing.version + 1) if existing is not None else 1,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        return DayArchive.model_validate(
            await self._persist(
                archive,
                PersonalRecordType.DAY_ARCHIVE,
                archive.archive_id,
                record_key=archive_date.isoformat(),
                status="sealed",
                record_date=archive_date,
                expected_version=existing.version if existing is not None else None,
                event_type="day_archive.sealed",
                actor_id=context.user_id,
            )
        )

    async def update_day_archive(
        self,
        archive_date: date,
        patch: DayArchivePatch,
        context: RunContext,
    ) -> DayArchive:
        current = await self.get_day_archive(archive_date, context)
        if current is None:
            current = await self.seal_day(archive_date, context)
        values = patch.model_dump(exclude_unset=True, exclude={"expected_version"})
        updated = current.model_copy(
            update={**values, "version": current.version + 1, "updated_at": utc_now()}
        )
        return DayArchive.model_validate(
            await self._persist(
                updated,
                PersonalRecordType.DAY_ARCHIVE,
                updated.archive_id,
                record_key=archive_date.isoformat(),
                status="sealed" if updated.sealed_at is not None else "draft",
                record_date=archive_date,
                expected_version=patch.expected_version or current.version,
                event_type="day_archive.updated",
                actor_id=context.user_id,
            )
        )

    async def current_emotion(
        self,
        context: RunContext,
        *,
        now: datetime | None = None,
    ) -> EmotionSnapshot:
        observed_at = now or utc_now()
        override = await self._emotion_override(context)
        if override is not None and override.expires_at > observed_at:
            return self._emotion_snapshot(
                override.state,
                ["user_override"],
                observed_at,
                overridden=True,
            )
        persona = await self.get_persona(context)
        local_now = observed_at.astimezone(self._timezone(persona.timezone))
        tasks = await self.list_tasks(context)
        recent_runs = [
            run
            for run in await self._trajectories.list_recent(
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                limit=20,
            )
            if run.context.user_id == context.user_id
        ]
        if local_now.hour >= 23 or local_now.hour < 6:
            return self._emotion_snapshot(
                EmotionState.RESTING, ["quiet_hours"], observed_at
            )
        just_completed = any(
            item.completed_at is not None
            and observed_at - item.completed_at.astimezone(UTC) <= timedelta(hours=4)
            for item in tasks
        )
        if just_completed:
            return self._emotion_snapshot(
                EmotionState.CELEBRATING, ["recent_task_completion"], observed_at
            )
        overdue = any(
            item.due_at is not None
            and item.due_at < observed_at
            and item.status not in {TaskStatus.COMPLETED, TaskStatus.ARCHIVED}
            for item in tasks
        )
        if overdue:
            return self._emotion_snapshot(
                EmotionState.SUPPORTIVE, ["overdue_open_task"], observed_at
            )
        if any(item.status == TaskStatus.IN_PROGRESS for item in tasks):
            return self._emotion_snapshot(
                EmotionState.FOCUSED, ["active_task"], observed_at
            )
        if recent_runs and observed_at - recent_runs[0].context.started_at <= timedelta(minutes=20):
            return self._emotion_snapshot(
                EmotionState.CURIOUS, ["recent_conversation"], observed_at
            )
        yesterday = local_now.date() - timedelta(days=1)
        if await self.get_day_archive(yesterday, context) is not None:
            return self._emotion_snapshot(
                EmotionState.REFLECTIVE, ["recent_day_archive"], observed_at
            )
        return self._emotion_snapshot(EmotionState.CALM, ["default"], observed_at)

    async def set_emotion_override(
        self,
        command: EmotionOverrideRequest,
        context: RunContext,
    ) -> EmotionSnapshot:
        current = await self._emotion_override(context)
        now = utc_now()
        override = EmotionOverride(
            override_id=current.override_id if current is not None else uuid4(),
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            state=command.state,
            note=command.note,
            expires_at=now + timedelta(minutes=command.duration_minutes),
            version=(current.version + 1) if current is not None else 1,
            created_at=current.created_at if current is not None else now,
            updated_at=now,
        )
        await self._persist(
            override,
            PersonalRecordType.EMOTION_OVERRIDE,
            override.override_id,
            record_key="current",
            status=override.state.value,
            expected_version=current.version if current is not None else None,
            event_type="emotion.override_set",
            actor_id=context.user_id,
        )
        return await self.current_emotion(context)

    async def clear_emotion_override(self, context: RunContext) -> EmotionSnapshot:
        current = await self._emotion_override(context)
        if current is not None and current.expires_at > utc_now():
            cleared = current.model_copy(
                update={
                    "expires_at": utc_now(),
                    "version": current.version + 1,
                    "updated_at": utc_now(),
                }
            )
            await self._persist(
                cleared,
                PersonalRecordType.EMOTION_OVERRIDE,
                cleared.override_id,
                record_key="current",
                status="expired",
                expected_version=current.version,
                event_type="emotion.override_cleared",
                actor_id=context.user_id,
            )
        return await self.current_emotion(context)

    async def correct_memory(
        self,
        command: MemoryCorrectionRequest,
        context: RunContext,
    ) -> MemoryCorrectionResult:
        action, query, replacement = self._parse_correction(command.request)
        if action == "unknown" or not query:
            return MemoryCorrectionResult(
                status="invalid",
                action="unknown",
                message=(
                    "请明确说“忘记关于 X 的记忆”或“把 X 更正为 Y”。"
                ),
            )
        matches = list(
            await self._memories.search(
                query,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                user_id=context.user_id,
                limit=20,
            )
        )
        selected = [
            item for item in matches if item.memory_id in set(command.confirm_memory_ids)
        ]
        if matches and not selected:
            if len(matches) > 1:
                return MemoryCorrectionResult(
                    status="needs_confirmation",
                    action=cast(Any, action),
                    query=query,
                    replacement=replacement,
                    candidates=matches,
                    message="找到多条相关记忆，请确认要修订的 memory_id。",
                )
            selected = matches
        revoked: list[UUID] = []
        for item in selected:
            if await self._memories.revoke(
                item.memory_id,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                user_id=context.user_id,
            ):
                revoked.append(item.memory_id)
        if action == "forget":
            if not matches:
                return MemoryCorrectionResult(
                    status="no_match",
                    action="forget",
                    query=query,
                    message="没有找到可撤回的相关记忆。",
                )
            return MemoryCorrectionResult(
                status="applied",
                action="forget",
                query=query,
                candidates=matches,
                revoked_memory_ids=revoked,
                message=f"已撤回 {len(revoked)} 条记忆。",
            )
        new_record = await self._memories.upsert(
            MemoryCandidate(
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                user_id=context.user_id,
                memory_type=MemoryType.SEMANTIC,
                key=(
                    "user_correction:"
                    + hashlib.sha256(replacement.encode()).hexdigest()[:24]
                ),
                summary=replacement,
                detail={
                    "correction_of": query,
                    "revoked_memory_ids": [str(item) for item in revoked],
                    "request": command.request,
                },
                confidence=1.0,
                provenance=[
                    Provenance(
                        source_type="explicit_user_correction",
                        source_id=f"run:{context.run_id}",
                        run_id=context.run_id,
                        trust=TrustLevel.USER_ASSERTED,
                    )
                ],
            )
        )
        return MemoryCorrectionResult(
            status="applied",
            action="replace",
            query=query,
            replacement=replacement,
            candidates=matches,
            revoked_memory_ids=revoked,
            created_memory=new_record,
            message="已保存用户明确更正，并撤回选中的旧记忆。",
        )

    async def compile_runtime_capsule(self, context: RunContext) -> str:
        persona = await self.get_persona(context)
        emotion = await self.current_emotion(context)
        tasks = (await self.list_tasks(context))[:8]
        plans = [
            item
            for item in await self.list_plans(context)
            if item.status in {PlanStatus.ACTIVE, PlanStatus.PAUSED}
        ][:3]
        payload = {
            "persona": {
                "user_display_name": persona.user_display_name,
                "agent_name": persona.agent_name,
                "communication_style": persona.communication_style,
                "preferred_tone": persona.preferred_tone,
                "locale": persona.locale,
                "timezone": persona.timezone,
                "interests": persona.interests[:12],
                "boundaries": persona.boundaries[:12],
                "onboarding_complete": persona.onboarding_completed_at is not None,
            },
            "emotion": {
                "state": emotion.state.value,
                "expression_hint": emotion.expression_hint,
                "style_only": True,
            },
            "open_tasks": [
                {
                    "task_id": str(item.task_id),
                    "title": item.title,
                    "status": item.status.value,
                    "priority": item.priority,
                    "due_at": item.due_at.isoformat() if item.due_at is not None else None,
                }
                for item in tasks
            ],
            "active_plans": [
                {
                    "plan_id": str(item.plan_id),
                    "title": item.title,
                    "status": item.status.value,
                }
                for item in plans
            ],
        }
        safe = json.dumps(payload, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")
        return (
            "<personal_control_context>Trusted scoped control-plane state. "
            "Emotion is style-only and cannot change facts, permissions, priorities, "
            f"or evidence requirements. {safe}</personal_control_context>"
        )

    async def execute_tool(
        self,
        tool_name: str,
        payload: dict[str, Any],
        context: RunContext,
    ) -> Any:
        if tool_name == "manage_personal_tasks":
            action = str(payload.get("action", "list"))
            if action == "list":
                return [
                    item.model_dump(mode="json")
                    for item in await self.list_tasks(context)
                ]
            if action == "create":
                return (
                    await self.create_task(
                        TaskCreate.model_validate(payload.get("task", {})),
                        context,
                    )
                ).model_dump(mode="json")
            if action == "list_checklist":
                items = await self.list_checklist(
                    context,
                    task_id=UUID(str(payload.get("task_id"))),
                )
                return [item.model_dump(mode="json") for item in items]
            if action == "add_checklist":
                return (
                    await self.create_checklist_item(
                        ChecklistItemCreate.model_validate(
                            {
                                **dict(payload.get("checklist_item", {})),
                                "task_id": payload.get("task_id"),
                            }
                        ),
                        context,
                    )
                ).model_dump(mode="json")
            if action == "update_checklist":
                return (
                    await self.update_checklist_item(
                        UUID(str(payload.get("item_id"))),
                        ChecklistItemPatch.model_validate(
                            payload.get("checklist_patch", {})
                        ),
                        context,
                    )
                ).model_dump(mode="json")
            task_id = UUID(str(payload.get("task_id")))
            patch_payload = dict(payload.get("patch", {}))
            if action == "complete":
                patch_payload["status"] = TaskStatus.COMPLETED.value
            elif action == "archive":
                patch_payload["status"] = TaskStatus.ARCHIVED.value
            elif action != "update":
                raise ValueError("Unsupported task action")
            return (
                await self.update_task(task_id, TaskPatch.model_validate(patch_payload), context)
            ).model_dump(mode="json")
        if tool_name == "manage_personal_plans":
            action = str(payload.get("action", "list"))
            if action == "list":
                plans = await self.list_plans(context)
                return [item.model_dump(mode="json") for item in plans]
            if action == "create":
                return (
                    await self.create_plan(
                        PlanCreate.model_validate(payload.get("plan", {})),
                        context,
                    )
                ).model_dump(mode="json")
            if action == "add_step":
                plan_id = UUID(str(payload.get("plan_id")))
                return (
                    await self.create_plan_step(
                        plan_id,
                        PlanStepCreate.model_validate(payload.get("step", {})),
                        context,
                    )
                ).model_dump(mode="json")
            if action == "update_step":
                return (
                    await self.update_plan_step(
                        UUID(str(payload.get("step_id"))),
                        PlanStepPatch.model_validate(payload.get("patch", {})),
                        context,
                    )
                ).model_dump(mode="json")
            plan_id = UUID(str(payload.get("plan_id")))
            patch_payload = dict(payload.get("patch", {}))
            transitions = {
                "activate": PlanStatus.ACTIVE,
                "pause": PlanStatus.PAUSED,
                "complete": PlanStatus.COMPLETED,
                "archive": PlanStatus.ARCHIVED,
            }
            if action in transitions:
                patch_payload["status"] = transitions[action].value
            elif action != "update":
                raise ValueError("Unsupported plan action")
            return (
                await self.update_plan(plan_id, PlanPatch.model_validate(patch_payload), context)
            ).model_dump(mode="json")
        if tool_name == "manage_personal_notes":
            action = str(payload.get("action", "list"))
            if action == "list":
                notes = await self.list_notes(
                    context,
                    task_id=self._optional_uuid(payload.get("task_id")),
                    plan_id=self._optional_uuid(payload.get("plan_id")),
                    note_date=self._optional_date(payload.get("note_date")),
                )
                return [item.model_dump(mode="json") for item in notes]
            if action == "upsert":
                return (
                    await self.upsert_note(
                        NoteUpsert.model_validate(payload.get("note", {})),
                        context,
                    )
                ).model_dump(mode="json")
            raise ValueError("Unsupported note action")
        if tool_name == "correct_personal_memory":
            return (
                await self.correct_memory(MemoryCorrectionRequest.model_validate(payload), context)
            ).model_dump(mode="json")
        if tool_name == "manage_personal_profile":
            action = str(payload.get("action", "get"))
            if action == "get":
                persona = await self.get_persona(context)
                emotion = await self.current_emotion(context)
                return {
                    "persona": persona.model_dump(mode="json"),
                    "emotion": emotion.model_dump(mode="json"),
                }
            if action == "update":
                return (
                    await self.update_persona(
                        PersonaUpdate.model_validate(payload.get("persona", {})),
                        context,
                    )
                ).model_dump(mode="json")
            if action == "set_emotion":
                return (
                    await self.set_emotion_override(
                        EmotionOverrideRequest.model_validate(
                            payload.get("emotion", {})
                        ),
                        context,
                    )
                ).model_dump(mode="json")
            if action == "clear_emotion":
                return (
                    await self.clear_emotion_override(context)
                ).model_dump(mode="json")
            raise ValueError("Unsupported personal profile action")
        if tool_name == "manage_personal_journal":
            action = str(payload.get("action", "list"))
            if action == "list":
                date_from = date.fromisoformat(str(payload.get("date_from")))
                date_to = date.fromisoformat(str(payload.get("date_to")))
                archives = await self.list_day_archives(
                    context,
                    date_from=date_from,
                    date_to=date_to,
                )
                return [item.model_dump(mode="json") for item in archives]
            archive_date = date.fromisoformat(str(payload.get("archive_date")))
            if action == "get":
                archive = await self.get_day_archive(archive_date, context)
                return archive.model_dump(mode="json") if archive is not None else None
            if action == "seal":
                return (
                    await self.seal_day(
                        archive_date,
                        context,
                        force=bool(payload.get("force", False)),
                    )
                ).model_dump(mode="json")
            if action == "update":
                return (
                    await self.update_day_archive(
                        archive_date,
                        DayArchivePatch.model_validate(payload.get("patch", {})),
                        context,
                    )
                ).model_dump(mode="json")
            raise ValueError("Unsupported personal journal action")
        raise ValueError(f"Unsupported personal tool: {tool_name}")

    async def _write_reminder_state(
        self,
        task_id: UUID,
        context: RunContext,
        *,
        read: bool,
        snoozed_until: datetime | None = None,
    ) -> TaskReminderState:
        task = await self.get_task(task_id, context)
        if task.due_at is None:
            raise PersonalControlError("Task has no due date")
        if task.status in {TaskStatus.COMPLETED, TaskStatus.ARCHIVED}:
            raise PersonalControlError("Completed or archived tasks cannot be reminded")
        persona = await self.get_persona(context)
        timezone = self._timezone(persona.timezone)
        due_at = self._aware_datetime(task.due_at, timezone)
        now = utc_now()
        kind = self._reminder_kind(due_at, now, timezone)
        if kind is None:
            raise PersonalControlError("Task is outside the reminder window")
        existing_row = await self._repository.get_by_key(
            PersonalRecordType.REMINDER_STATE,
            f"task:{task_id}",
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
        )
        existing = (
            TaskReminderState.model_validate(existing_row.payload)
            if existing_row is not None
            else None
        )
        state = TaskReminderState(
            reminder_state_id=(
                existing.reminder_state_id if existing is not None else uuid4()
            ),
            task_id=task.task_id,
            due_at=due_at,
            kind=kind,
            read_at=now if read else None,
            snoozed_until=snoozed_until,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
            version=(existing.version + 1) if existing is not None else 1,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
        )
        return TaskReminderState.model_validate(
            await self._persist(
                state,
                PersonalRecordType.REMINDER_STATE,
                state.reminder_state_id,
                parent_id=task.task_id,
                record_key=f"task:{task.task_id}",
                status="snoozed" if snoozed_until is not None else "read",
                expected_version=existing.version if existing is not None else None,
                event_type=(
                    "task_reminder.snoozed"
                    if snoozed_until is not None
                    else "task_reminder.read"
                ),
                actor_id=context.user_id,
            )
        )

    @staticmethod
    def _reminder_kind(
        due_at: datetime,
        observed_at: datetime,
        timezone: ZoneInfo,
    ) -> ReminderKind | None:
        if due_at < observed_at:
            return ReminderKind.OVERDUE
        if due_at <= observed_at + timedelta(hours=2):
            return ReminderKind.DUE_SOON
        if due_at.astimezone(timezone).date() == observed_at.astimezone(timezone).date():
            return ReminderKind.TODAY
        if due_at <= observed_at + timedelta(hours=24):
            return ReminderKind.DUE_SOON
        return None

    @staticmethod
    def _aware_datetime(value: datetime, default_timezone: tzinfo) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=default_timezone)
        return value

    async def _emotion_override(self, context: RunContext) -> EmotionOverride | None:
        row = await self._repository.get_by_key(
            PersonalRecordType.EMOTION_OVERRIDE,
            "current",
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
        )
        return EmotionOverride.model_validate(row.payload) if row is not None else None

    async def _persist(
        self,
        record: ScopedRecord,
        record_type: PersonalRecordType,
        record_id: UUID,
        *,
        parent_id: UUID | None = None,
        record_key: str | None = None,
        status: str | None = None,
        record_date: date | None = None,
        expected_version: int | None,
        event_type: str,
        actor_id: str,
    ) -> dict[str, Any]:
        envelope = PersonalRecordEnvelope(
            record_type=record_type,
            record_id=record_id,
            tenant_id=record.tenant_id,
            project_id=record.project_id,
            user_id=record.user_id,
            parent_id=parent_id,
            record_key=record_key,
            status=status,
            record_date=record_date,
            version=record.version,
            payload=record.model_dump(mode="json", exclude_none=True),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        saved = await self._repository.save(
            envelope,
            expected_version=expected_version,
            event_type=event_type,
            actor_id=actor_id,
        )
        return saved.payload

    async def _require(
        self,
        record_type: PersonalRecordType,
        record_id: UUID,
        context: RunContext,
    ) -> PersonalRecordEnvelope:
        item = await self._repository.get(
            record_type,
            record_id,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            user_id=context.user_id,
        )
        if item is None:
            raise PersonalRecordNotFound(f"{record_type.value} not found")
        return item

    @staticmethod
    def _timezone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {name}") from exc

    @staticmethod
    def _emotion_snapshot(
        state: EmotionState,
        reasons: list[str],
        updated_at: datetime,
        *,
        overridden: bool = False,
    ) -> EmotionSnapshot:
        label, valence, energy, hint = _EMOTION_META[state]
        return EmotionSnapshot(
            state=state,
            label=label,
            valence=valence,
            energy=energy,
            expression_hint=hint,
            reason_codes=reasons,
            overridden=overridden,
            updated_at=updated_at,
        )

    @staticmethod
    def _parse_correction(text: str) -> tuple[str, str, str]:
        normalized = " ".join(text.strip().split())
        for pattern in _FORGET_PATTERNS:
            match = pattern.match(normalized)
            if match:
                return "forget", PersonalControlService._clean_phrase(match.group(1)), ""
        for pattern in _REPLACE_PATTERNS:
            match = pattern.match(normalized)
            if match:
                return (
                    "replace",
                    PersonalControlService._clean_phrase(match.group(1)),
                    PersonalControlService._clean_phrase(match.group(2)),
                )
        return "unknown", "", ""

    @staticmethod
    def _clean_phrase(value: str) -> str:
        return value.strip(" \t\r\n。.,，;；:：\"'“”‘’")

    @staticmethod
    def _clip(value: str, limit: int) -> str:
        normalized = " ".join(value.split())
        return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"

    @staticmethod
    def _optional_uuid(value: Any) -> UUID | None:
        return UUID(str(value)) if value not in {None, ""} else None

    @staticmethod
    def _optional_date(value: Any) -> date | None:
        return date.fromisoformat(str(value)) if value not in {None, ""} else None


__all__ = [
    "PersonalControlError",
    "PersonalControlService",
    "PersonalRecordNotFound",
]
