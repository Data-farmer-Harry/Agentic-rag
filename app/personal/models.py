from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.domain.models import MemoryRecord, StrictModel, utc_now


class TaskStatus(StrEnum):
    INBOX = "inbox"
    PLANNED = "planned"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class PlanStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class StepStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class NoteKind(StrEnum):
    GENERAL = "general"
    TASK = "task"
    DAILY = "daily"


class EmotionState(StrEnum):
    CALM = "calm"
    FOCUSED = "focused"
    CURIOUS = "curious"
    SUPPORTIVE = "supportive"
    CELEBRATING = "celebrating"
    REFLECTIVE = "reflective"
    RESTING = "resting"


class ReminderKind(StrEnum):
    OVERDUE = "overdue"
    DUE_SOON = "due_soon"
    TODAY = "today"


class PersonalRecordType(StrEnum):
    TASK = "task"
    PLAN = "plan"
    PLAN_STEP = "plan_step"
    CHECKLIST_ITEM = "checklist_item"
    NOTE = "note"
    PERSONA = "persona"
    DAY_ARCHIVE = "day_archive"
    EMOTION_OVERRIDE = "emotion_override"
    REMINDER_STATE = "reminder_state"


class ScopedRecord(StrictModel):
    tenant_id: str = Field(default="local", min_length=1, max_length=200)
    project_id: str = Field(default="default", min_length=1, max_length=200)
    user_id: str = Field(default="local-user", min_length=1, max_length=200)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Task(ScopedRecord):
    task_id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=10_000)
    status: TaskStatus = TaskStatus.INBOX
    priority: int = Field(default=3, ge=1, le=5)
    due_at: datetime | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    completed_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("Task title cannot be blank")
        return normalized

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip()[:64] for value in values if value.strip()))


class TaskCreate(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=10_000)
    priority: int = Field(default=3, ge=1, le=5)
    due_at: datetime | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)


class TaskPatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10_000)
    status: TaskStatus | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    due_at: datetime | None = None
    tags: list[str] | None = Field(default=None, max_length=20)
    expected_version: int | None = Field(default=None, ge=1)


class TaskReminderState(ScopedRecord):
    reminder_state_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    due_at: datetime
    kind: ReminderKind
    read_at: datetime | None = None
    snoozed_until: datetime | None = None


class TaskReminder(StrictModel):
    task_id: UUID
    title: str
    kind: ReminderKind
    due_at: datetime
    priority: int = Field(ge=1, le=5)
    unread: bool = True
    snoozed_until: datetime | None = None


class TaskReminderFeed(StrictModel):
    items: list[TaskReminder] = Field(default_factory=list)
    unread_count: int = Field(ge=0)
    timezone: str
    generated_at: datetime = Field(default_factory=utc_now)


class ReminderSnoozeRequest(StrictModel):
    duration_minutes: int = Field(default=60, ge=5, le=10_080)


class Plan(ScopedRecord):
    plan_id: UUID = Field(default_factory=uuid4)
    task_id: UUID | None = None
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(default="", max_length=10_000)
    status: PlanStatus = PlanStatus.DRAFT
    target_date: date | None = None


class PlanCreate(StrictModel):
    task_id: UUID | None = None
    title: str = Field(min_length=1, max_length=300)
    objective: str = Field(default="", max_length=10_000)
    target_date: date | None = None


class PlanPatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    objective: str | None = Field(default=None, max_length=10_000)
    status: PlanStatus | None = None
    target_date: date | None = None
    expected_version: int | None = Field(default=None, ge=1)


class PlanStep(ScopedRecord):
    step_id: UUID = Field(default_factory=uuid4)
    plan_id: UUID
    title: str = Field(min_length=1, max_length=300)
    detail: str = Field(default="", max_length=10_000)
    position: int = Field(default=0, ge=0)
    status: StepStatus = StepStatus.TODO
    due_at: datetime | None = None
    completed_at: datetime | None = None


class PlanStepCreate(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    detail: str = Field(default="", max_length=10_000)
    position: int | None = Field(default=None, ge=0)
    due_at: datetime | None = None


class PlanStepPatch(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    detail: str | None = Field(default=None, max_length=10_000)
    position: int | None = Field(default=None, ge=0)
    status: StepStatus | None = None
    due_at: datetime | None = None
    expected_version: int | None = Field(default=None, ge=1)


class ChecklistItem(ScopedRecord):
    item_id: UUID = Field(default_factory=uuid4)
    task_id: UUID | None = None
    step_id: UUID | None = None
    label: str = Field(min_length=1, max_length=500)
    checked: bool = False
    position: int = Field(default=0, ge=0)
    checked_at: datetime | None = None

    @model_validator(mode="after")
    def validate_parent(self) -> ChecklistItem:
        if (self.task_id is None) == (self.step_id is None):
            raise ValueError("Checklist item must belong to exactly one task or plan step")
        return self


class ChecklistItemCreate(StrictModel):
    task_id: UUID | None = None
    step_id: UUID | None = None
    label: str = Field(min_length=1, max_length=500)
    position: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_parent(self) -> ChecklistItemCreate:
        if (self.task_id is None) == (self.step_id is None):
            raise ValueError("Checklist item must belong to exactly one task or plan step")
        return self


class ChecklistItemPatch(StrictModel):
    label: str | None = Field(default=None, min_length=1, max_length=500)
    checked: bool | None = None
    position: int | None = Field(default=None, ge=0)
    expected_version: int | None = Field(default=None, ge=1)


class Note(ScopedRecord):
    note_id: UUID = Field(default_factory=uuid4)
    kind: NoteKind = NoteKind.GENERAL
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(default="", max_length=100_000)
    task_id: UUID | None = None
    plan_id: UUID | None = None
    note_date: date | None = None


class NoteUpsert(StrictModel):
    note_id: UUID | None = None
    kind: NoteKind = NoteKind.GENERAL
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(default="", max_length=100_000)
    task_id: UUID | None = None
    plan_id: UUID | None = None
    note_date: date | None = None
    expected_version: int | None = Field(default=None, ge=1)


class PersonaProfile(ScopedRecord):
    persona_id: UUID = Field(default_factory=uuid4)
    user_display_name: str = Field(default="", max_length=100)
    agent_name: str = Field(default="HermesGraph", min_length=1, max_length=100)
    self_description: str = Field(default="", max_length=5_000)
    communication_style: str = Field(default="clear and collaborative", max_length=500)
    preferred_tone: str = Field(default="warm", max_length=100)
    locale: str = Field(default="zh-CN", max_length=32)
    timezone: str = Field(default="Asia/Shanghai", max_length=100)
    interests: list[str] = Field(default_factory=list, max_length=50)
    boundaries: list[str] = Field(default_factory=list, max_length=50)
    onboarding_completed_at: datetime | None = None


class PersonaUpdate(StrictModel):
    user_display_name: str | None = Field(default=None, max_length=100)
    agent_name: str | None = Field(default=None, min_length=1, max_length=100)
    self_description: str | None = Field(default=None, max_length=5_000)
    communication_style: str | None = Field(default=None, max_length=500)
    preferred_tone: str | None = Field(default=None, max_length=100)
    locale: str | None = Field(default=None, max_length=32)
    timezone: str | None = Field(default=None, max_length=100)
    interests: list[str] | None = Field(default=None, max_length=50)
    boundaries: list[str] | None = Field(default=None, max_length=50)
    complete_onboarding: bool = False
    reset_onboarding: bool = False
    expected_version: int | None = Field(default=None, ge=1)


class DayArchive(ScopedRecord):
    archive_id: UUID = Field(default_factory=uuid4)
    archive_date: date
    summary: str = Field(default="", max_length=20_000)
    diary: str = Field(default="", max_length=50_000)
    highlights: list[str] = Field(default_factory=list, max_length=50)
    decisions: list[str] = Field(default_factory=list, max_length=50)
    open_loops: list[str] = Field(default_factory=list, max_length=50)
    emotion_state: EmotionState = EmotionState.CALM
    run_ids: list[UUID] = Field(default_factory=list, max_length=500)
    sealed_at: datetime | None = None


class DayArchivePatch(StrictModel):
    summary: str | None = Field(default=None, max_length=20_000)
    diary: str | None = Field(default=None, max_length=50_000)
    highlights: list[str] | None = Field(default=None, max_length=50)
    decisions: list[str] | None = Field(default=None, max_length=50)
    open_loops: list[str] | None = Field(default=None, max_length=50)
    expected_version: int | None = Field(default=None, ge=1)


class EmotionOverride(ScopedRecord):
    override_id: UUID = Field(default_factory=uuid4)
    state: EmotionState
    note: str = Field(default="", max_length=500)
    expires_at: datetime


class EmotionOverrideRequest(StrictModel):
    state: EmotionState
    note: str = Field(default="", max_length=500)
    duration_minutes: int = Field(default=120, ge=5, le=1_440)


class EmotionSnapshot(StrictModel):
    state: EmotionState
    label: str
    valence: float = Field(ge=-1.0, le=1.0)
    energy: float = Field(ge=0.0, le=1.0)
    expression_hint: str = Field(max_length=500)
    reason_codes: list[str] = Field(default_factory=list, max_length=20)
    overridden: bool = False
    updated_at: datetime = Field(default_factory=utc_now)


class MemoryCorrectionRequest(StrictModel):
    request: str = Field(min_length=1, max_length=2_000)
    confirm_memory_ids: list[UUID] = Field(default_factory=list, max_length=20)


class MemoryCorrectionResult(StrictModel):
    status: Literal["applied", "needs_confirmation", "no_match", "invalid"]
    action: Literal["forget", "replace", "unknown"]
    query: str = Field(default="", max_length=1_000)
    replacement: str = Field(default="", max_length=2_000)
    candidates: list[MemoryRecord] = Field(default_factory=list, max_length=20)
    revoked_memory_ids: list[UUID] = Field(default_factory=list, max_length=20)
    created_memory: MemoryRecord | None = None
    message: str = Field(max_length=2_000)


class PersonalRecordEnvelope(StrictModel):
    record_type: PersonalRecordType
    record_id: UUID
    tenant_id: str
    project_id: str
    user_id: str
    parent_id: UUID | None = None
    record_key: str | None = None
    status: str | None = None
    record_date: date | None = None
    version: int = Field(ge=1)
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class PersonalEvent(StrictModel):
    event_id: UUID = Field(default_factory=uuid4)
    record_type: PersonalRecordType
    record_id: UUID
    tenant_id: str
    project_id: str
    user_id: str
    event_type: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    actor_id: str = Field(default="local-user", min_length=1, max_length=200)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
