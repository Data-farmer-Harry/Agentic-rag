from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from app.domain.contracts import LearningJobRepository
from app.domain.enums import RunStatus
from app.domain.models import (
    LearningJob,
    LearningJobCheckpoint,
    LearningJobResult,
    LearningJobSubmission,
    LearningReflectionArtifact,
    LearningTrajectoryEvaluation,
    RunTrajectory,
    SkillObservation,
)
from app.harness.experience import HarnessExperienceService
from app.harness.health import HarnessPatternHealthMonitor
from app.harness.mining import DeterministicPatternMiner
from app.learning.engine import LearningEngine
from app.learning.evaluator import TrajectoryEvaluation
from app.learning.evolution import SkillEvolutionService
from app.learning.execution import LearningExecutionFence, learning_execution
from app.learning.job_errors import LearningJobLeaseLostError
from app.learning.reflection import ExperienceReflection
from app.learning.safety import LEARNING_GATE_REVISION, assess_automatic_learning

logger = logging.getLogger(__name__)

LearningTrigger = Literal["run_completed", "feedback_received"]
LearningWorkflow = Callable[[RunTrajectory], Awaitable[LearningJobResult]]
LearningCheckpointSaver = Callable[
    [LearningJobCheckpoint],
    Awaitable[LearningJob],
]
LearningStageOperation = Callable[[], Awaitable[LearningJobCheckpoint]]
LearningStageCommitter = Callable[
    [LearningStageOperation],
    Awaitable[LearningJob],
]


class LearningJobTransitionError(ValueError):
    pass


class LearningJobsUnavailableError(RuntimeError):
    pass


class LearningWorkflowProcessor:
    """Runs the replay-safe learning and skill-evolution stages."""

    def __init__(
        self,
        learning: LearningEngine,
        skill_evolution: SkillEvolutionService,
        *,
        learning_mode: str,
        harness_experiences: HarnessExperienceService | None = None,
        harness_pattern_miner: DeterministicPatternMiner | None = None,
        harness_health_monitor: HarnessPatternHealthMonitor | None = None,
    ) -> None:
        self._learning = learning
        self._skill_evolution = skill_evolution
        self._learning_mode = learning_mode
        self._harness_experiences = harness_experiences
        self._harness_pattern_miner = harness_pattern_miner
        self._harness_health_monitor = harness_health_monitor

    async def __call__(self, trajectory: RunTrajectory) -> LearningJobResult:
        decision = assess_automatic_learning(trajectory)
        if not decision.allowed:
            return _nonlearnable_result(trajectory, decision.audit_summary)
        errors: list[Exception] = []
        outcome = None
        observations: tuple[SkillObservation, ...] = ()
        evolution = None
        try:
            outcome = await self._learning.process_completed_run(trajectory)
        except Exception as exc:
            errors.append(exc)
        try:
            observations = tuple(await self._skill_evolution.observe_run(trajectory))
        except Exception as exc:
            errors.append(exc)
        if (
            outcome is not None
            and outcome.skill_candidate is not None
            and self._learning_mode in {"shadow", "canary", "active"}
        ):
            try:
                evolution = await self._skill_evolution.evaluate_and_stage(
                    outcome.skill_candidate.skill_id,
                    tenant_id=trajectory.context.tenant_id,
                    project_id=trajectory.context.project_id,
                    skill_version=outcome.skill_candidate.version,
                )
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[0]
        if outcome is None:
            raise RuntimeError("Learning workflow completed without an outcome")
        harness_experience_ids: list[UUID] = []
        harness_evaluation_ids: list[UUID] = []
        if self._harness_experiences is not None:
            collected = await self._harness_experiences.collect(
                trajectory,
                trigger=(
                    "feedback_received"
                    if trajectory.feedback_score is not None
                    else "run_completed"
                ),
                native_change_set_ids=[item.change_set_id for item in outcome.change_sets],
            )
            harness_experience_ids.append(collected.experience.experience_id)
            harness_evaluation_ids.append(collected.evaluation.evaluation_id)
            if self._harness_pattern_miner is not None:
                await self._harness_pattern_miner.mine_scope(
                    tenant_id=trajectory.context.tenant_id,
                    project_id=trajectory.context.project_id,
                )
            if self._harness_health_monitor is not None:
                await self._harness_health_monitor.monitor_scope(
                    tenant_id=trajectory.context.tenant_id,
                    project_id=trajectory.context.project_id,
                )
        return LearningJobResult(
            run_id=trajectory.context.run_id,
            reflector_revision=outcome.reflection.reflector_revision,
            reflection_outcome=outcome.reflection.outcome,
            reflection_summary=outcome.reflection.summary,
            memory_ids=[item.memory_id for item in outcome.memories_written],
            change_set_ids=[item.change_set_id for item in outcome.change_sets],
            skill_candidate_id=(
                outcome.skill_candidate.skill_id
                if outcome.skill_candidate is not None
                else None
            ),
            skill_candidate_version=(
                outcome.skill_candidate.version
                if outcome.skill_candidate is not None
                else None
            ),
            observation_ids=[item.observation_id for item in observations],
            evaluation_id=(
                evolution.evaluation.evaluation_id if evolution is not None else None
            ),
            harness_experience_ids=harness_experience_ids,
            harness_evaluation_ids=harness_evaluation_ids,
        )

    async def process_job(
        self,
        job: LearningJob,
        save_checkpoint: LearningCheckpointSaver,
        commit_stage: LearningStageCommitter,
    ) -> LearningJobResult:
        decision = assess_automatic_learning(job.trajectory)
        if not decision.allowed:
            return _nonlearnable_result(job.trajectory, decision.audit_summary)
        checkpoint = job.checkpoint
        if checkpoint is None:
            reflection = await self._learning.reflect(job.trajectory)
            checkpoint = LearningJobCheckpoint(
                stage="reflection_completed",
                reflection=_to_reflection_artifact(job.trajectory, reflection),
            )
            job = await save_checkpoint(checkpoint)
            checkpoint = job.checkpoint
            if checkpoint is None:
                raise RuntimeError("Learning reflection checkpoint was not persisted")
        _validate_checkpoint(job.trajectory, checkpoint)

        if checkpoint.stage == "reflection_completed":
            stage_checkpoint = checkpoint
            stage_job = job

            async def commit_artifacts() -> LearningJobCheckpoint:
                reflection = _from_reflection_artifact(
                    stage_job.trajectory,
                    stage_checkpoint.reflection,
                )
                outcome = await self._learning.apply_reflection(
                    stage_job.trajectory,
                    reflection,
                )
                return LearningJobCheckpoint(
                    stage="artifacts_committed",
                    reflection=stage_checkpoint.reflection,
                    memory_ids=[item.memory_id for item in outcome.memories_written],
                    change_set_ids=[item.change_set_id for item in outcome.change_sets],
                    skill_candidate_id=(
                        outcome.skill_candidate.skill_id
                        if outcome.skill_candidate is not None
                        else None
                    ),
                    skill_candidate_version=(
                        outcome.skill_candidate.version
                        if outcome.skill_candidate is not None
                        else None
                    ),
                    updated_at=_checkpoint_time(stage_job.trajectory),
                )

            job = await commit_stage(commit_artifacts)
            checkpoint = _require_checkpoint(job)

        if checkpoint.stage == "artifacts_committed":
            stage_checkpoint = checkpoint
            stage_job = job

            async def commit_observations() -> LearningJobCheckpoint:
                observations = tuple(
                    await self._skill_evolution.observe_run(stage_job.trajectory)
                )
                return stage_checkpoint.model_copy(
                    update={
                        "stage": "observations_committed",
                        "observation_ids": [
                            item.observation_id for item in observations
                        ],
                        "updated_at": _checkpoint_time(stage_job.trajectory),
                    }
                )

            job = await commit_stage(commit_observations)
            checkpoint = _require_checkpoint(job)

        if checkpoint.stage == "observations_committed":
            stage_checkpoint = checkpoint
            stage_job = job

            async def commit_evolution() -> LearningJobCheckpoint:
                evaluation_id = stage_checkpoint.evaluation_id
                transition_ids = list(stage_checkpoint.transition_ids)
                if (
                    stage_checkpoint.skill_candidate_id is not None
                    and self._learning_mode in {"shadow", "canary", "active"}
                ):
                    evolution = await self._skill_evolution.evaluate_and_stage(
                        stage_checkpoint.skill_candidate_id,
                        tenant_id=stage_job.tenant_id,
                        project_id=stage_job.project_id,
                        skill_version=stage_checkpoint.skill_candidate_version,
                    )
                    evaluation_id = evolution.evaluation.evaluation_id
                    transition_ids = [
                        item.transition_id
                        for item in evolution.transitions
                        if item.transition_id is not None
                    ]
                return stage_checkpoint.model_copy(
                    update={
                        "stage": "evolution_committed",
                        "evaluation_id": evaluation_id,
                        "transition_ids": transition_ids,
                        "updated_at": _checkpoint_time(stage_job.trajectory),
                    }
                )

            job = await commit_stage(commit_evolution)
            checkpoint = _require_checkpoint(job)

        if (
            checkpoint.stage == "evolution_committed"
            and self._harness_experiences is not None
        ):
            harness_experiences = self._harness_experiences
            stage_checkpoint = checkpoint
            stage_job = job

            async def commit_harness_experience() -> LearningJobCheckpoint:
                collected = await harness_experiences.collect(
                    stage_job.trajectory,
                    trigger=stage_job.trigger,
                    native_change_set_ids=list(stage_checkpoint.change_set_ids),
                )
                if self._harness_pattern_miner is not None:
                    await self._harness_pattern_miner.mine_scope(
                        tenant_id=stage_job.tenant_id,
                        project_id=stage_job.project_id,
                    )
                if self._harness_health_monitor is not None:
                    await self._harness_health_monitor.monitor_scope(
                        tenant_id=stage_job.tenant_id,
                        project_id=stage_job.project_id,
                    )
                return stage_checkpoint.model_copy(
                    update={
                        "stage": "harness_experience_committed",
                        "harness_experience_ids": [
                            collected.experience.experience_id
                        ],
                        "harness_evaluation_ids": [
                            collected.evaluation.evaluation_id
                        ],
                        "updated_at": _checkpoint_time(stage_job.trajectory),
                    }
                )

            job = await commit_stage(commit_harness_experience)
            checkpoint = _require_checkpoint(job)

        valid_final_stages = {"evolution_committed"}
        if self._harness_experiences is not None:
            valid_final_stages = {"harness_experience_committed"}
        if checkpoint.stage not in valid_final_stages:
            raise RuntimeError(f"Unsupported learning checkpoint stage: {checkpoint.stage}")
        return _result_from_checkpoint(job.trajectory, checkpoint)


class LearningJobService:
    def __init__(
        self,
        repository: LearningJobRepository,
        processor: LearningWorkflow,
        *,
        max_attempts: int = 3,
        lease_seconds: int = 300,
        poll_seconds: float = 1.0,
        retry_base_seconds: int = 5,
        worker_enabled: bool = True,
    ) -> None:
        if not 1 <= max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if not 10 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 10 and 3600")
        if not 0.05 <= poll_seconds <= 30:
            raise ValueError("poll_seconds must be between 0.05 and 30")
        if not 1 <= retry_base_seconds <= 3_600:
            raise ValueError("retry_base_seconds must be between 1 and 3600")
        self._repository = repository
        self._max_attempts = max_attempts
        self._worker = _LearningWorker(
            repository,
            processor,
            lease_seconds=lease_seconds,
            poll_seconds=poll_seconds,
            retry_base_seconds=retry_base_seconds,
        )
        self._worker_enabled = worker_enabled
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        await self._repository.start()
        try:
            if self._worker_enabled:
                await self._worker.start()
        except BaseException:
            await self._repository.close()
            raise
        self._started = True

    async def close(self) -> None:
        if not self._started:
            return
        if self._worker_enabled:
            await self._worker.close()
        await self._repository.close()
        self._started = False

    async def submit(
        self,
        trajectory: RunTrajectory,
        *,
        trigger: LearningTrigger,
    ) -> LearningJobSubmission:
        if trajectory.status not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            raise ValueError("Learning jobs accept only terminal trajectories")
        idempotency_key = _idempotency_key(trajectory, trigger)
        job = LearningJob(
            idempotency_key=idempotency_key,
            tenant_id=trajectory.context.tenant_id,
            project_id=trajectory.context.project_id,
            user_id=trajectory.context.user_id,
            run_id=trajectory.context.run_id,
            trigger=trigger,
            trajectory=trajectory,
            max_attempts=self._max_attempts,
        )
        queued, created = await self._repository.enqueue(job)
        self._worker.wake()
        return LearningJobSubmission(job=queued, coalesced=not created)

    async def process_inline(
        self,
        trajectory: RunTrajectory,
        *,
        trigger: LearningTrigger,
    ) -> LearningJobSubmission:
        submission = await self.submit(trajectory, trigger=trigger)
        if submission.job.status.value in {"queued", "retry_scheduled"}:
            await self.run_worker_once()
            refreshed = await self.get(
                submission.job.job_id,
                tenant_id=submission.job.tenant_id,
                project_id=submission.job.project_id,
            )
            if refreshed is not None:
                return submission.model_copy(update={"job": refreshed})
        return submission

    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> LearningJob | None:
        return await self._repository.get(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    async def list_jobs(
        self,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        limit: int = 100,
    ) -> list[LearningJob]:
        return list(
            await self._repository.list_scoped(
                tenant_id=tenant_id,
                project_id=project_id,
                limit=limit,
            )
        )

    async def cancel(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> LearningJob:
        cancelled = await self._repository.cancel(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if cancelled is not None:
            return cancelled
        current = await self.get(job_id, tenant_id=tenant_id, project_id=project_id)
        if current is None:
            raise KeyError("Learning job not found")
        raise LearningJobTransitionError(
            f"Cannot cancel a learning job in {current.status.value} status"
        )

    async def retry(
        self,
        job_id: UUID,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
    ) -> LearningJob:
        retried = await self._repository.retry(
            job_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if retried is not None:
            self._worker.wake()
            return retried
        current = await self.get(job_id, tenant_id=tenant_id, project_id=project_id)
        if current is None:
            raise KeyError("Learning job not found")
        raise LearningJobTransitionError(
            f"Cannot retry a learning job in {current.status.value} status"
        )

    async def run_worker_once(self) -> bool:
        return await self._worker.run_once()


class _LearningWorker:
    def __init__(
        self,
        repository: LearningJobRepository,
        processor: LearningWorkflow,
        *,
        lease_seconds: int,
        poll_seconds: float,
        retry_base_seconds: int,
    ) -> None:
        self._repository = repository
        self._processor = processor
        self._lease_seconds = lease_seconds
        self._poll_seconds = poll_seconds
        self._retry_base_seconds = retry_base_seconds
        self._worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:12]}"
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="learning-worker")

    async def close(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._wake.set()
        task, self._task = self._task, None
        try:
            await asyncio.wait_for(task, timeout=min(self._lease_seconds, 30))
        except TimeoutError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def wake(self) -> None:
        self._wake.set()

    async def run_once(self) -> bool:
        job = await self._repository.claim(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if job is None:
            return False
        logger.info(
            "Learning worker %s claimed job %s at attempt %s",
            self._worker_id,
            job.job_id,
            job.attempt,
        )
        await self._process(job)
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("Learning worker claim loop failed")
                processed = False
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    async def _process(self, job: LearningJob) -> None:
        if job.lease_token is None:
            raise LearningJobLeaseLostError("Claimed learning job has no fencing token")
        heartbeat = asyncio.create_task(
            self._heartbeat(job.job_id, job.lease_token),
            name=f"learning-heartbeat:{job.job_id}",
        )
        try:
            with learning_execution(
                LearningExecutionFence(
                    job_id=job.job_id,
                    worker_id=self._worker_id,
                    lease_token=job.lease_token,
                )
            ):
                result = await self._process_claimed_job(job)
            await self._repository.complete(
                job.job_id,
                worker_id=self._worker_id,
                lease_token=job.lease_token,
                result=result,
            )
            logger.info(
                "Learning worker %s completed job %s",
                self._worker_id,
                job.job_id,
            )
        except LearningJobLeaseLostError:
            logger.warning("Lease lost while finalizing learning job %s", job.job_id)
        except (ValueError, KeyError) as exc:
            await self._record_failure(
                job,
                error_code="invalid_learning_input",
                error_message=str(exc),
                retryable=False,
            )
        except Exception:
            logger.exception("Unexpected learning job failure: %s", job.job_id)
            await self._record_failure(
                job,
                error_code="learning_workflow_failed",
                error_message="The learning worker encountered a retryable internal error.",
                retryable=True,
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _process_claimed_job(self, job: LearningJob) -> LearningJobResult:
        durable_processor = getattr(self._processor, "process_job", None)
        if not callable(durable_processor):
            return await self._processor(job.trajectory)
        lease_token = job.lease_token
        if lease_token is None:
            raise LearningJobLeaseLostError("Claimed learning job has no fencing token")

        async def save_checkpoint(
            checkpoint: LearningJobCheckpoint,
        ) -> LearningJob:
            return await self._repository.save_checkpoint(
                job.job_id,
                worker_id=self._worker_id,
                lease_token=lease_token,
                checkpoint=checkpoint,
            )

        async def commit_stage(
            operation: LearningStageOperation,
        ) -> LearningJob:
            return await self._repository.commit_stage(
                job.job_id,
                worker_id=self._worker_id,
                lease_token=lease_token,
                operation=operation,
            )

        result = cast(
            Awaitable[LearningJobResult],
            durable_processor(job, save_checkpoint, commit_stage),
        )
        return await result

    async def _record_failure(
        self,
        job: LearningJob,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        if job.lease_token is None:
            raise LearningJobLeaseLostError("Learning job has no fencing token")
        retry_delay = min(
            3_600,
            self._retry_base_seconds * (2 ** max(0, job.attempt - 1)),
        )
        try:
            await self._repository.fail(
                job.job_id,
                worker_id=self._worker_id,
                lease_token=job.lease_token,
                error_code=error_code,
                error_message=error_message,
                retryable=retryable,
                retry_delay_seconds=retry_delay,
            )
        except LearningJobLeaseLostError:
            logger.warning("Lease lost while failing learning job %s", job.job_id)

    async def _heartbeat(self, job_id: UUID, lease_token: UUID) -> None:
        interval = max(3.0, self._lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._repository.renew_lease(
                    job_id,
                    worker_id=self._worker_id,
                    lease_token=lease_token,
                    lease_seconds=self._lease_seconds,
                )
            except Exception:
                logger.exception("Unable to renew learning lease for %s", job_id)
                return
            if not renewed:
                return


def _nonlearnable_result(
    trajectory: RunTrajectory,
    summary: str,
) -> LearningJobResult:
    """Return a durable job audit without creating learning artifacts."""

    return LearningJobResult(
        run_id=trajectory.context.run_id,
        reflector_revision=LEARNING_GATE_REVISION,
        reflection_outcome="non_learnable",
        reflection_summary=summary,
    )


def _idempotency_key(trajectory: RunTrajectory, trigger: LearningTrigger) -> str:
    payload = json.dumps(
        {
            "trigger": trigger,
            "trajectory": trajectory.model_dump(mode="json", exclude_none=True),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _trajectory_hash(trajectory: RunTrajectory) -> str:
    payload = json.dumps(
        trajectory.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _to_reflection_artifact(
    trajectory: RunTrajectory,
    reflection: ExperienceReflection,
) -> LearningReflectionArtifact:
    trajectory_hash = _trajectory_hash(trajectory)
    evaluation = LearningTrajectoryEvaluation(
        run_id=reflection.evaluation.run_id,
        quality_score=reflection.evaluation.quality_score,
        completion_score=reflection.evaluation.completion_score,
        tool_success_rate=reflection.evaluation.tool_success_rate,
        citation_coverage=reflection.evaluation.citation_coverage,
        unsupported_claim_rate=reflection.evaluation.unsupported_claim_rate,
        feedback_score=reflection.evaluation.feedback_score,
        passed=reflection.evaluation.passed,
        reasons=list(reflection.evaluation.reasons),
    )
    return LearningReflectionArtifact(
        artifact_id=uuid5(
            NAMESPACE_URL,
            (
                "hermesgraph:learning-reflection:"
                f"{trajectory.context.run_id}:{trajectory_hash}:"
                f"{reflection.reflector_revision}"
            ),
        ),
        run_id=trajectory.context.run_id,
        trajectory_hash=trajectory_hash,
        evaluation=evaluation,
        outcome=reflection.outcome,
        summary=reflection.summary,
        strengths=list(reflection.strengths),
        weaknesses=list(reflection.weaknesses),
        action_sequence=list(reflection.action_sequence),
        memory_candidates=list(reflection.memory_candidates),
        reflector_revision=reflection.reflector_revision,
        fallback_error=(
            reflection.fallback_error[:500]
            if reflection.fallback_error is not None
            else None
        ),
        model_reflection_attempted=reflection.model_reflection_attempted,
        trigger_reason=reflection.trigger_reason[:200],
        created_at=_checkpoint_time(trajectory),
    )


def _from_reflection_artifact(
    trajectory: RunTrajectory,
    artifact: LearningReflectionArtifact,
) -> ExperienceReflection:
    evaluation = TrajectoryEvaluation(
        run_id=artifact.evaluation.run_id,
        quality_score=artifact.evaluation.quality_score,
        completion_score=artifact.evaluation.completion_score,
        tool_success_rate=artifact.evaluation.tool_success_rate,
        citation_coverage=artifact.evaluation.citation_coverage,
        unsupported_claim_rate=artifact.evaluation.unsupported_claim_rate,
        feedback_score=artifact.evaluation.feedback_score,
        passed=artifact.evaluation.passed,
        reasons=tuple(artifact.evaluation.reasons),
    )
    return ExperienceReflection(
        trajectory=trajectory,
        evaluation=evaluation,
        outcome=artifact.outcome,
        summary=artifact.summary,
        strengths=tuple(artifact.strengths),
        weaknesses=tuple(artifact.weaknesses),
        action_sequence=tuple(artifact.action_sequence),
        memory_candidates=tuple(artifact.memory_candidates),
        reflector_revision=artifact.reflector_revision,
        fallback_error=artifact.fallback_error,
        model_reflection_attempted=artifact.model_reflection_attempted,
        trigger_reason=artifact.trigger_reason,
    )


def _validate_checkpoint(
    trajectory: RunTrajectory,
    checkpoint: LearningJobCheckpoint,
) -> None:
    if checkpoint.revision != "learning-workflow-v1":
        raise ValueError(
            f"Unsupported learning checkpoint revision: {checkpoint.revision}"
        )
    if checkpoint.reflection.run_id != trajectory.context.run_id:
        raise ValueError("Learning checkpoint belongs to another run")
    if checkpoint.reflection.trajectory_hash != _trajectory_hash(trajectory):
        raise ValueError("Learning checkpoint trajectory hash does not match")


def _require_checkpoint(job: LearningJob) -> LearningJobCheckpoint:
    if job.checkpoint is None:
        raise RuntimeError("Learning checkpoint was not returned after persistence")
    return job.checkpoint


def _result_from_checkpoint(
    trajectory: RunTrajectory,
    checkpoint: LearningJobCheckpoint,
) -> LearningJobResult:
    reflection = checkpoint.reflection
    return LearningJobResult(
        run_id=trajectory.context.run_id,
        reflector_revision=reflection.reflector_revision,
        reflection_outcome=reflection.outcome,
        reflection_summary=reflection.summary,
        memory_ids=checkpoint.memory_ids,
        change_set_ids=checkpoint.change_set_ids,
        skill_candidate_id=checkpoint.skill_candidate_id,
        skill_candidate_version=checkpoint.skill_candidate_version,
        observation_ids=checkpoint.observation_ids,
        evaluation_id=checkpoint.evaluation_id,
        transition_ids=checkpoint.transition_ids,
        harness_experience_ids=checkpoint.harness_experience_ids,
        harness_evaluation_ids=checkpoint.harness_evaluation_ids,
    )


def _checkpoint_time(trajectory: RunTrajectory) -> datetime:
    return trajectory.completed_at or trajectory.context.started_at


__all__ = [
    "LearningJobService",
    "LearningJobsUnavailableError",
    "LearningJobTransitionError",
    "LearningTrigger",
    "LearningWorkflowProcessor",
]
