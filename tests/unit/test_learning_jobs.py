from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from app.domain.enums import EvidenceLevel, LearningJobStatus, RunStatus, TrustLevel
from app.domain.models import (
    AnswerResponse,
    Claim,
    EvidenceRef,
    LearningJob,
    LearningJobCheckpoint,
    LearningJobResult,
    Provenance,
    RunContext,
    RunTrajectory,
    utc_now,
)
from app.learning.evaluator import TrajectoryEvaluation
from app.learning.job_errors import LearningJobLeaseLostError
from app.learning.jobs import (
    LearningJobService,
    LearningJobTransitionError,
    LearningWorkflowProcessor,
)
from app.learning.reflection import ExperienceReflection


class _MemoryLearningJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[UUID, LearningJob] = {}
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.started = False

    async def enqueue(self, job: LearningJob) -> tuple[LearningJob, bool]:
        existing = next(
            (
                item
                for item in self.jobs.values()
                if item.idempotency_key == job.idempotency_key
            ),
            None,
        )
        if existing is not None:
            return existing, False
        self.jobs[job.job_id] = job
        return job, True

    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningJob | None:
        job = self.jobs.get(job_id)
        if job is None or job.tenant_id != tenant_id or job.project_id != project_id:
            return None
        return job

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 100,
    ) -> list[LearningJob]:
        return [
            item
            for item in reversed(list(self.jobs.values()))
            if item.tenant_id == tenant_id and item.project_id == project_id
        ][:limit]

    async def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> LearningJob | None:
        job = next(
            (
                item
                for item in self.jobs.values()
                if item.status
                in {LearningJobStatus.QUEUED, LearningJobStatus.RETRY_SCHEDULED}
            ),
            None,
        )
        if job is None:
            return None
        claimed = job.model_copy(
            update={
                "status": LearningJobStatus.RUNNING,
                "attempt": job.attempt + 1,
                "lease_owner": worker_id,
                "lease_token": uuid4(),
                "lease_expires_at": utc_now() + timedelta(seconds=lease_seconds),
                "started_at": job.started_at or utc_now(),
                "updated_at": utc_now(),
                "can_retry": False,
            }
        )
        self.jobs[job.job_id] = claimed
        return claimed

    async def renew_lease(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        lease_seconds: int,
    ) -> bool:
        job = self.jobs[job_id]
        return (
            job.status == LearningJobStatus.RUNNING
            and job.lease_owner == worker_id
            and job.lease_token == lease_token
        )

    async def complete(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        result: LearningJobResult,
    ) -> LearningJob:
        job = self._owned(job_id, worker_id, lease_token)
        completed = job.model_copy(
            update={
                "status": LearningJobStatus.SUCCEEDED,
                "result": result,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "completed_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.jobs[job_id] = completed
        return completed

    async def save_checkpoint(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        checkpoint: LearningJobCheckpoint,
    ) -> LearningJob:
        job = self._owned(job_id, worker_id, lease_token)
        updated = job.model_copy(
            update={"checkpoint": checkpoint, "updated_at": utc_now()}
        )
        self.jobs[job_id] = updated
        return updated

    async def commit_stage(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        operation: Callable[[], Awaitable[LearningJobCheckpoint]],
    ) -> LearningJob:
        checkpoint = await operation()
        return await self.save_checkpoint(
            job_id,
            worker_id=worker_id,
            lease_token=lease_token,
            checkpoint=checkpoint,
        )

    async def fail(
        self,
        job_id: UUID,
        *,
        worker_id: str,
        lease_token: UUID,
        error_code: str,
        error_message: str,
        retryable: bool,
        retry_delay_seconds: int,
    ) -> LearningJob:
        job = self._owned(job_id, worker_id, lease_token)
        scheduled = retryable and job.attempt < job.max_attempts
        failed = job.model_copy(
            update={
                "status": (
                    LearningJobStatus.RETRY_SCHEDULED
                    if scheduled
                    else LearningJobStatus.FAILED
                ),
                "available_at": utc_now() + timedelta(seconds=retry_delay_seconds),
                "can_retry": retryable,
                "error_code": error_code,
                "error_message": error_message,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "completed_at": None if scheduled else utc_now(),
                "updated_at": utc_now(),
            }
        )
        self.jobs[job_id] = failed
        return failed

    async def cancel(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningJob | None:
        job = await self.get(job_id, tenant_id=tenant_id, project_id=project_id)
        if job is None or job.status not in {
            LearningJobStatus.QUEUED,
            LearningJobStatus.RETRY_SCHEDULED,
        }:
            return None
        cancelled = job.model_copy(
            update={
                "status": LearningJobStatus.CANCELLED,
                "can_retry": True,
                "completed_at": utc_now(),
            }
        )
        self.jobs[job_id] = cancelled
        return cancelled

    async def retry(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningJob | None:
        job = await self.get(job_id, tenant_id=tenant_id, project_id=project_id)
        if (
            job is None
            or job.status not in {LearningJobStatus.FAILED, LearningJobStatus.CANCELLED}
            or not job.can_retry
        ):
            return None
        retried = job.model_copy(
            update={
                "status": LearningJobStatus.QUEUED,
                "attempt": 0,
                "can_retry": False,
                "error_code": None,
                "error_message": None,
                "completed_at": None,
            }
        )
        self.jobs[job_id] = retried
        return retried

    def _owned(
        self,
        job_id: UUID,
        worker_id: str,
        lease_token: UUID,
    ) -> LearningJob:
        job = self.jobs[job_id]
        if (
            job.status != LearningJobStatus.RUNNING
            or job.lease_owner != worker_id
            or job.lease_token != lease_token
        ):
            raise LearningJobLeaseLostError("stale learning lease")
        return job


class _ScriptedProcessor:
    def __init__(self, outcomes: list[str]) -> None:
        self._outcomes = iter(outcomes)
        self.calls = 0

    async def __call__(self, trajectory: RunTrajectory) -> LearningJobResult:
        self.calls += 1
        outcome = next(self._outcomes)
        if outcome == "retryable":
            raise RuntimeError("temporary model outage")
        if outcome == "permanent":
            raise ValueError("invalid terminal trajectory")
        return _result(trajectory)


class _CheckpointLearning:
    def __init__(self) -> None:
        self.reflect_calls = 0
        self.apply_calls = 0

    async def reflect(self, trajectory: RunTrajectory) -> ExperienceReflection:
        self.reflect_calls += 1
        return ExperienceReflection(
            trajectory=trajectory,
            evaluation=TrajectoryEvaluation(
                run_id=str(trajectory.context.run_id),
                quality_score=1.0,
                completion_score=1.0,
                tool_success_rate=1.0,
                citation_coverage=1.0,
                unsupported_claim_rate=0.0,
                feedback_score=None,
                passed=True,
                reasons=("test_passed",),
            ),
            outcome="success",
            summary="Checkpointed reflection.",
            strengths=("stable",),
            weaknesses=(),
            action_sequence=(),
            memory_candidates=(),
            reflector_revision="checkpoint-test-v1",
        )

    async def apply_reflection(
        self,
        trajectory: RunTrajectory,
        reflection: ExperienceReflection,
    ) -> Any:
        self.apply_calls += 1
        if self.apply_calls == 1:
            raise RuntimeError("crash after reflection checkpoint")
        return SimpleNamespace(
            run_id=trajectory.context.run_id,
            reflection=reflection,
            memories_written=(),
            change_sets=(),
            skill_candidate=None,
        )


class _CheckpointEvolution:
    def __init__(self) -> None:
        self.observe_calls = 0

    async def observe_run(self, trajectory: RunTrajectory) -> list[Any]:
        self.observe_calls += 1
        return []


def _trajectory(
    *,
    project_id: str = "default",
    feedback_score: float | None = None,
) -> RunTrajectory:
    evidence = EvidenceRef(
        text="The enterprise fixture confirms the retrieval boundary.",
        provenance=Provenance(
            source_type="enterprise_fixture",
            source_id="northstar:architecture:system-overview#chunk=0",
            trust=TrustLevel.VERIFIED,
        ),
        metadata={"knowledge_layer": "team_internal"},
    )
    return RunTrajectory(
        context=RunContext(project_id=project_id),
        user_input="Explain agentic retrieval.",
        status=RunStatus.COMPLETED,
        answer=AnswerResponse(
            answer_markdown="The retrieval boundary is source-backed.",
            claims=[
                Claim(
                    text="The retrieval boundary is source-backed.",
                    evidence_ids=[evidence.evidence_id],
                    level=EvidenceLevel.SUPPORTED,
                )
            ],
            citations=[evidence],
            confidence=EvidenceLevel.SUPPORTED,
        ),
        feedback_score=feedback_score,
        completed_at=utc_now(),
    )


def _result(trajectory: RunTrajectory) -> LearningJobResult:
    return LearningJobResult(
        run_id=trajectory.context.run_id,
        reflector_revision="test-reflector-v1",
        reflection_outcome="success",
        reflection_summary="The run completed.",
    )


def _service(
    repository: _MemoryLearningJobRepository,
    processor: _ScriptedProcessor,
) -> LearningJobService:
    return LearningJobService(
        repository,
        processor,
        max_attempts=2,
        lease_seconds=10,
        poll_seconds=0.05,
        retry_base_seconds=1,
        worker_enabled=False,
    )


@pytest.mark.asyncio
async def test_submission_coalesces_snapshot_and_persists_result() -> None:
    repository = _MemoryLearningJobRepository()
    processor = _ScriptedProcessor(["success"])
    service = _service(repository, processor)
    await service.start()
    trajectory = _trajectory()

    first = await service.submit(trajectory, trigger="run_completed")
    duplicate = await service.submit(trajectory, trigger="run_completed")

    assert first.coalesced is False
    assert duplicate.coalesced is True
    assert duplicate.job.job_id == first.job.job_id
    assert "trajectory" not in first.job.model_dump(mode="json")
    assert await service.run_worker_once() is True
    completed = await service.get(first.job.job_id)
    assert completed is not None
    assert completed.status == LearningJobStatus.SUCCEEDED
    assert completed.result is not None
    assert completed.result.run_id == trajectory.context.run_id
    assert processor.calls == 1
    await service.close()


@pytest.mark.asyncio
async def test_feedback_snapshot_gets_a_distinct_job_and_scope_is_enforced() -> None:
    repository = _MemoryLearningJobRepository()
    service = _service(repository, _ScriptedProcessor([]))
    await service.start()
    trajectory = _trajectory(project_id="private")
    completed = await service.submit(trajectory, trigger="run_completed")
    feedback = await service.submit(
        trajectory.model_copy(update={"feedback_score": -1.0}),
        trigger="feedback_received",
    )

    assert feedback.job.job_id != completed.job.job_id
    assert await service.get(completed.job.job_id, project_id="other") is None
    assert len(await service.list_jobs(project_id="private")) == 2
    await service.close()


@pytest.mark.asyncio
async def test_retryable_failure_is_replayed_then_succeeds() -> None:
    repository = _MemoryLearningJobRepository()
    processor = _ScriptedProcessor(["retryable", "success"])
    service = _service(repository, processor)
    await service.start()
    submission = await service.submit(_trajectory(), trigger="run_completed")

    assert await service.run_worker_once() is True
    scheduled = await service.get(submission.job.job_id)
    assert scheduled is not None
    assert scheduled.status == LearningJobStatus.RETRY_SCHEDULED
    assert scheduled.error_code == "learning_workflow_failed"
    assert await service.run_worker_once() is True
    completed = await service.get(submission.job.job_id)
    assert completed is not None
    assert completed.status == LearningJobStatus.SUCCEEDED
    assert completed.attempt == 2
    assert processor.calls == 2
    await service.close()


@pytest.mark.asyncio
async def test_permanent_failure_cannot_be_retried() -> None:
    repository = _MemoryLearningJobRepository()
    service = _service(repository, _ScriptedProcessor(["permanent"]))
    await service.start()
    submission = await service.submit(_trajectory(), trigger="run_completed")

    assert await service.run_worker_once() is True
    failed = await service.get(submission.job.job_id)
    assert failed is not None
    assert failed.status == LearningJobStatus.FAILED
    assert failed.can_retry is False
    with pytest.raises(LearningJobTransitionError, match="Cannot retry"):
        await service.retry(submission.job.job_id)
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_job_can_be_manually_retried() -> None:
    repository = _MemoryLearningJobRepository()
    service = _service(repository, _ScriptedProcessor(["success"]))
    await service.start()
    submission = await service.submit(_trajectory(), trigger="run_completed")

    cancelled = await service.cancel(submission.job.job_id)
    assert cancelled.status == LearningJobStatus.CANCELLED
    retried = await service.retry(submission.job.job_id)
    assert retried.status == LearningJobStatus.QUEUED
    assert retried.attempt == 0
    await service.close()


@pytest.mark.asyncio
async def test_fencing_token_rejects_a_stale_execution() -> None:
    repository = _MemoryLearningJobRepository()
    service = _service(repository, _ScriptedProcessor([]))
    await service.start()
    submission = await service.submit(_trajectory(), trigger="run_completed")
    first = await repository.claim(worker_id="worker", lease_seconds=10)
    assert first is not None
    assert first.lease_token is not None
    replacement_token = uuid4()
    repository.jobs[first.job_id] = first.model_copy(
        update={"lease_token": replacement_token}
    )

    with pytest.raises(LearningJobLeaseLostError, match="stale"):
        await repository.complete(
            submission.job.job_id,
            worker_id="worker",
            lease_token=first.lease_token,
            result=_result(first.trajectory),
        )
    await service.close()


@pytest.mark.asyncio
async def test_durable_workflow_reuses_reflection_checkpoint_after_crash() -> None:
    trajectory = _trajectory()
    learning = _CheckpointLearning()
    evolution = _CheckpointEvolution()
    processor = LearningWorkflowProcessor(
        cast(Any, learning),
        cast(Any, evolution),
        learning_mode="shadow",
    )
    current = LearningJob(
        idempotency_key="a" * 64,
        run_id=trajectory.context.run_id,
        trigger="run_completed",
        trajectory=trajectory,
    )

    async def save_checkpoint(checkpoint: LearningJobCheckpoint) -> LearningJob:
        nonlocal current
        current = current.model_copy(update={"checkpoint": checkpoint})
        return current

    async def commit_stage(
        operation: Callable[[], Awaitable[LearningJobCheckpoint]],
    ) -> LearningJob:
        return await save_checkpoint(await operation())

    with pytest.raises(RuntimeError, match="crash after reflection"):
        await processor.process_job(current, save_checkpoint, commit_stage)

    assert current.checkpoint is not None
    assert current.checkpoint.stage == "reflection_completed"
    result = await processor.process_job(current, save_checkpoint, commit_stage)

    assert learning.reflect_calls == 1
    assert learning.apply_calls == 2
    assert evolution.observe_calls == 1
    final_stage: str = current.checkpoint.stage
    assert final_stage == "evolution_committed"
    assert result.reflector_revision == "checkpoint-test-v1"


@pytest.mark.asyncio
async def test_learning_workflow_short_circuits_non_learnable_run_before_artifacts() -> None:
    safe = _trajectory()
    assert safe.answer is not None
    unsafe = safe.model_copy(
        update={
            "answer": safe.answer.model_copy(
                update={"confidence": EvidenceLevel.INSUFFICIENT}
            )
        }
    )
    learning = _CheckpointLearning()
    evolution = _CheckpointEvolution()
    processor = LearningWorkflowProcessor(
        cast(Any, learning),
        cast(Any, evolution),
        learning_mode="shadow",
    )
    job = LearningJob(
        idempotency_key="b" * 64,
        run_id=unsafe.context.run_id,
        trigger="run_completed",
        trajectory=unsafe,
    )

    direct_result = await processor(unsafe)

    async def unexpected_checkpoint(checkpoint: LearningJobCheckpoint) -> LearningJob:
        del checkpoint
        raise AssertionError("Unsafe runs must not create a learning checkpoint")

    async def unexpected_commit(
        operation: Callable[[], Awaitable[LearningJobCheckpoint]],
    ) -> LearningJob:
        del operation
        raise AssertionError("Unsafe runs must not commit learning artifacts")

    durable_result = await processor.process_job(
        job,
        unexpected_checkpoint,
        unexpected_commit,
    )

    assert direct_result.reflection_outcome == "non_learnable"
    assert durable_result.reflection_outcome == "non_learnable"
    assert "final answer is insufficient" in durable_result.reflection_summary
    assert learning.reflect_calls == 0
    assert learning.apply_calls == 0
    assert evolution.observe_calls == 0
