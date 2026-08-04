from __future__ import annotations

from collections.abc import Awaitable
from datetime import date
from typing import Annotated, TypeVar
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from app.api.auth import bind_user_id, current_request_identity
from app.domain.models import RunContext
from app.personal.models import (
    ChecklistItemCreate,
    ChecklistItemPatch,
    DayArchivePatch,
    EmotionOverrideRequest,
    MemoryCorrectionRequest,
    NoteUpsert,
    PersonaUpdate,
    PlanCreate,
    PlanPatch,
    PlanStepCreate,
    PlanStepPatch,
    ReminderSnoozeRequest,
    TaskCreate,
    TaskPatch,
    TaskStatus,
)
from app.personal.repository import (
    PersonalRepositoryError,
    PersonalVersionConflict,
)
from app.personal.service import (
    PersonalControlError,
    PersonalControlService,
    PersonalRecordNotFound,
)

T = TypeVar("T")


def build_personal_router(service: PersonalControlService) -> APIRouter:
    router = APIRouter(prefix="/v1/projects/{project_id}/personal", tags=["Personal"])

    def context(project_id: str, user_id: str) -> RunContext:
        identity = current_request_identity()
        return RunContext(
            tenant_id=identity.tenant_id,
            project_id=project_id,
            user_id=bind_user_id(user_id),
        )

    @router.get("/tasks")
    async def list_tasks(
        project_id: str,
        user_id: str = "local-user",
        task_status: Annotated[TaskStatus | None, Query(alias="status")] = None,
        include_archived: bool = False,
    ) -> object:
        return await service.list_tasks(
            context(project_id, user_id),
            status=task_status,
            include_archived=include_archived,
        )

    @router.post("/tasks", status_code=status.HTTP_201_CREATED)
    async def create_task(
        project_id: str,
        command: TaskCreate,
        user_id: str = "local-user",
    ) -> object:
        return await service.create_task(command, context(project_id, user_id))

    @router.get("/tasks/{task_id}")
    async def get_task(
        project_id: str,
        task_id: UUID,
        user_id: str = "local-user",
    ) -> object:
        return await _not_found(service.get_task(task_id, context(project_id, user_id)))

    @router.patch("/tasks/{task_id}")
    async def update_task(
        project_id: str,
        task_id: UUID,
        patch: TaskPatch,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(service.update_task(task_id, patch, context(project_id, user_id)))

    @router.delete("/tasks/{task_id}")
    async def archive_task(
        project_id: str,
        task_id: UUID,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.update_task(
                task_id,
                TaskPatch(status=TaskStatus.ARCHIVED),
                context(project_id, user_id),
            )
        )

    @router.get("/reminders")
    async def list_reminders(
        project_id: str,
        user_id: str = "local-user",
    ) -> object:
        return await service.list_task_reminders(context(project_id, user_id))

    @router.put("/reminders/{task_id}/read")
    async def mark_reminder_read(
        project_id: str,
        task_id: UUID,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.mark_task_reminder_read(task_id, context(project_id, user_id))
        )

    @router.put("/reminders/read-all")
    async def mark_all_reminders_read(
        project_id: str,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.mark_all_task_reminders_read(context(project_id, user_id))
        )

    @router.put("/reminders/{task_id}/snooze")
    async def snooze_reminder(
        project_id: str,
        task_id: UUID,
        command: ReminderSnoozeRequest,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.snooze_task_reminder(
                task_id,
                command,
                context(project_id, user_id),
            )
        )

    @router.get("/plans")
    async def list_plans(
        project_id: str,
        user_id: str = "local-user",
        task_id: UUID | None = None,
        include_archived: bool = False,
    ) -> object:
        return await service.list_plans(
            context(project_id, user_id),
            task_id=task_id,
            include_archived=include_archived,
        )

    @router.post("/plans", status_code=status.HTTP_201_CREATED)
    async def create_plan(
        project_id: str,
        command: PlanCreate,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(service.create_plan(command, context(project_id, user_id)))

    @router.patch("/plans/{plan_id}")
    async def update_plan(
        project_id: str,
        plan_id: UUID,
        patch: PlanPatch,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(service.update_plan(plan_id, patch, context(project_id, user_id)))

    @router.get("/plans/{plan_id}/steps")
    async def list_plan_steps(
        project_id: str,
        plan_id: UUID,
        user_id: str = "local-user",
    ) -> object:
        return await _not_found(
            service.list_plan_steps(plan_id, context(project_id, user_id))
        )

    @router.post("/plans/{plan_id}/steps", status_code=status.HTTP_201_CREATED)
    async def create_plan_step(
        project_id: str,
        plan_id: UUID,
        command: PlanStepCreate,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.create_plan_step(plan_id, command, context(project_id, user_id))
        )

    @router.patch("/plan-steps/{step_id}")
    async def update_plan_step(
        project_id: str,
        step_id: UUID,
        patch: PlanStepPatch,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.update_plan_step(step_id, patch, context(project_id, user_id))
        )

    @router.get("/checklist")
    async def list_checklist(
        project_id: str,
        user_id: str = "local-user",
        task_id: UUID | None = None,
        step_id: UUID | None = None,
    ) -> object:
        return await service.list_checklist(
            context(project_id, user_id),
            task_id=task_id,
            step_id=step_id,
        )

    @router.post("/checklist", status_code=status.HTTP_201_CREATED)
    async def create_checklist_item(
        project_id: str,
        command: ChecklistItemCreate,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.create_checklist_item(command, context(project_id, user_id))
        )

    @router.patch("/checklist/{item_id}")
    async def update_checklist_item(
        project_id: str,
        item_id: UUID,
        patch: ChecklistItemPatch,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.update_checklist_item(item_id, patch, context(project_id, user_id))
        )

    @router.get("/notes")
    async def list_notes(
        project_id: str,
        user_id: str = "local-user",
        task_id: UUID | None = None,
        plan_id: UUID | None = None,
        note_date: date | None = None,
    ) -> object:
        return await service.list_notes(
            context(project_id, user_id),
            task_id=task_id,
            plan_id=plan_id,
            note_date=note_date,
        )

    @router.post("/notes")
    async def upsert_note(
        project_id: str,
        command: NoteUpsert,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(service.upsert_note(command, context(project_id, user_id)))

    @router.get("/persona")
    async def get_persona(
        project_id: str,
        user_id: str = "local-user",
    ) -> object:
        return await service.get_persona(context(project_id, user_id))

    @router.put("/persona")
    async def update_persona(
        project_id: str,
        patch: PersonaUpdate,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(service.update_persona(patch, context(project_id, user_id)))

    @router.get("/days")
    async def list_days(
        project_id: str,
        date_from: date,
        date_to: date,
        user_id: str = "local-user",
    ) -> object:
        if (date_to - date_from).days > 366:
            raise HTTPException(status_code=422, detail="Calendar range cannot exceed 366 days")
        return await service.list_day_archives(
            context(project_id, user_id),
            date_from=date_from,
            date_to=date_to,
        )

    @router.get("/days/{archive_date}")
    async def get_day(
        project_id: str,
        archive_date: date,
        user_id: str = "local-user",
    ) -> object:
        archive = await service.get_day_archive(
            archive_date,
            context(project_id, user_id),
        )
        if archive is None:
            raise HTTPException(status_code=404, detail="Day archive not found")
        return archive

    @router.post("/days/{archive_date}/seal")
    async def seal_day(
        project_id: str,
        archive_date: date,
        user_id: str = "local-user",
        force: bool = False,
    ) -> object:
        return await _mutation(
            service.seal_day(
                archive_date,
                context(project_id, user_id),
                force=force,
            )
        )

    @router.put("/days/{archive_date}")
    async def update_day(
        project_id: str,
        archive_date: date,
        patch: DayArchivePatch,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.update_day_archive(
                archive_date,
                patch,
                context(project_id, user_id),
            )
        )

    @router.get("/emotion")
    async def get_emotion(
        project_id: str,
        user_id: str = "local-user",
    ) -> object:
        return await service.current_emotion(context(project_id, user_id))

    @router.put("/emotion/override")
    async def set_emotion(
        project_id: str,
        command: EmotionOverrideRequest,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.set_emotion_override(command, context(project_id, user_id))
        )

    @router.delete("/emotion/override")
    async def clear_emotion(
        project_id: str,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.clear_emotion_override(context(project_id, user_id))
        )

    @router.post("/memory-corrections")
    async def correct_memory(
        project_id: str,
        command: MemoryCorrectionRequest,
        user_id: str = "local-user",
    ) -> object:
        return await _mutation(
            service.correct_memory(command, context(project_id, user_id))
        )

    return router


async def _not_found(awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except PersonalRecordNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _mutation(awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except PersonalRecordNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PersonalVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PersonalRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PersonalControlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
