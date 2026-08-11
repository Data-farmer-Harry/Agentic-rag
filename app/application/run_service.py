from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from app.agent.answer_publisher import AnswerPublisher
from app.application.run_event_recorder import RunEventRecorder
from app.config import Settings
from app.domain.contracts import AgentRuntime, TrajectoryRepository
from app.domain.enums import RunStatus
from app.domain.models import AnswerResponse, RunContext, RunSnapshot, RunTrajectory, ToolEvent
from app.domain_packs.registry import DomainPackRegistry
from app.harness.consumer import BoundedHarnessConsumer
from app.harness.selector import HarnessOverlaySelector
from app.knowledge.knowledge_visibility import WorkspaceProfileResolver
from app.learning.safety import annotate_trajectory_for_automatic_learning

LearningTrigger = Literal["run_completed", "feedback_received"]
LearningProcessor = Callable[[RunTrajectory, LearningTrigger], Awaitable[None]]
SkillVersionProvider = Callable[[RunContext], Awaitable[Mapping[str, str]]]


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class RunService:
    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        trajectories: TrajectoryRepository,
        settings: Settings,
        domain_packs: DomainPackRegistry | None = None,
        learning_processor: LearningProcessor | None = None,
        event_recorder: RunEventRecorder | None = None,
        skill_version_provider: SkillVersionProvider | None = None,
        overlay_selector: HarnessOverlaySelector | None = None,
        harness_consumer: BoundedHarnessConsumer | None = None,
        workspace_profiles: WorkspaceProfileResolver | None = None,
    ) -> None:
        self._runtime = runtime
        self._trajectories = trajectories
        self._settings = settings
        self._domain_packs = domain_packs or DomainPackRegistry()
        self._answer_publisher = AnswerPublisher()
        self._learning_processor = learning_processor
        self._event_recorder = event_recorder
        self._skill_version_provider = skill_version_provider
        self._overlay_selector = overlay_selector
        self._harness_consumer = harness_consumer
        self._workspace_profiles = workspace_profiles

    def resolve_domain_pack(
        self,
        domain_pack: str | None,
        *,
        tenant_id: str,
        project_id: str,
    ) -> str:
        if domain_pack is not None:
            return domain_pack
        if self._workspace_profiles is None:
            return "general"
        return self._workspace_profiles.resolve(
            tenant_id=tenant_id,
            project_id=project_id,
        ).default_domain_pack

    async def run(
        self,
        user_input: str,
        *,
        run_id: UUID | None = None,
        idempotency_key: str | None = None,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        session_id: str = "default",
        domain_pack: str | None = None,
    ) -> RunTrajectory:
        trajectory = await self.prepare_run(
            user_input,
            run_id=run_id,
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            session_id=session_id,
            domain_pack=domain_pack,
        )
        return await self.execute_run(trajectory)

    async def prepare_run(
        self,
        user_input: str,
        *,
        run_id: UUID | None = None,
        idempotency_key: str | None = None,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
        session_id: str = "default",
        domain_pack: str | None = None,
    ) -> RunTrajectory:
        profile = (
            self._workspace_profiles.resolve(
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if self._workspace_profiles is not None
            else None
        )
        resolved_domain_pack = self.resolve_domain_pack(
            domain_pack,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        pack = self._domain_packs.get(resolved_domain_pack)
        context = RunContext(
            **({"run_id": run_id} if run_id is not None else {}),
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            session_id=session_id,
            domain_pack=resolved_domain_pack,
            model=self._settings.openai_model,
            enabled_knowledge_layers=(
                profile.enabled_knowledge_layers if profile is not None else None
            ),
            workspace_mode=profile.workspace_mode if profile is not None else None,
        )
        if self._skill_version_provider is not None:
            skill_versions = dict(await self._skill_version_provider(context))
            context = context.model_copy(update={"skill_versions": skill_versions})
        prepare_route = getattr(self._runtime, "prepare_route", None)
        if callable(prepare_route):
            adaptive_route = await prepare_route(user_input, context)
            if adaptive_route is not None:
                context = context.model_copy(
                    update={"adaptive_rag_route": adaptive_route}
                )
        snapshot = RunSnapshot(
            model=self._settings.openai_model,
            prompt_hash=_stable_hash(pack.system_context()),
            domain_pack=resolved_domain_pack,
            domain_pack_version=pack.manifest().version,
            skill_versions=context.skill_versions,
            policy_versions={"harness": "baseline-v1"},
            corpus_snapshot="local",
            component_versions={
                "core": "0.1.0",
                "conversation_router": "adaptive-rag-v1",
                "context_engine": "context-engine-v2",
            },
            config_hash=_stable_hash(
                {
                    "max_agent_turns": self._settings.max_agent_turns,
                    "max_tool_calls": self._settings.max_tool_calls,
                    "conversation_fast_path_enabled": (
                        self._settings.conversation_fast_path_enabled
                    ),
                    "conversation_fast_path_model": (
                        self._settings.adaptive_rag_router_model
                        or self._settings.conversation_fast_path_model
                        or self._settings.openai_model
                    ),
                    "adaptive_rag_router_enabled": (
                        self._settings.adaptive_rag_router_enabled
                    ),
                    "adaptive_rag_router_timeout_seconds": (
                        self._settings.adaptive_rag_router_timeout_seconds
                    ),
                    "conversation_history_turns": self._settings.conversation_history_turns,
                    "context_total_tokens": self._settings.context_total_tokens,
                    "context_history_tokens": self._settings.context_history_tokens,
                    "context_summary_tokens": self._settings.context_summary_tokens,
                    "context_memory_tokens": self._settings.context_memory_tokens,
                    "context_skill_tokens": self._settings.context_skill_tokens,
                    "context_personal_tokens": self._settings.context_personal_tokens,
                    "learning_mode": self._settings.learning_mode,
                    "harness_overlay_mode": self._settings.harness_overlay_mode,
                    "harness_bounded_consumer_enabled": (
                        self._settings.harness_bounded_consumer_enabled
                    ),
                    "harness_canary_percentage": (
                        self._settings.harness_canary_percentage
                    ),
                    "harness_max_capsule_memories": (
                        self._settings.harness_max_capsule_memories
                    ),
                    "harness_max_graph_hops": self._settings.harness_max_graph_hops,
                    "harness_max_subqueries": self._settings.harness_max_subqueries,
                    "harness_max_retrieval_rounds": (
                        self._settings.harness_max_retrieval_rounds
                    ),
                }
            ),
        )
        if self._overlay_selector is not None:
            try:
                overlay = await self._overlay_selector.select(
                    context=context,
                    query=user_input,
                    baseline_policy_versions=snapshot.policy_versions,
                )
            except Exception:
                overlay = None
            if overlay is not None:
                execution_policy = (
                    self._harness_consumer.resolve_policy(
                        context=context,
                        overlay=overlay,
                        apply_requested=(
                            self._settings.harness_bounded_consumer_enabled
                            and overlay.mode.value in {"canary", "active"}
                        ),
                    )
                    if self._harness_consumer is not None
                    else None
                )
                if execution_policy is not None:
                    context = context.model_copy(
                        update={"execution_policy": execution_policy}
                    )
                snapshot = snapshot.model_copy(
                    update={
                        "harness_overlay_id": overlay.overlay_id,
                        "harness_overlay_hash": overlay.payload_hash,
                        "harness_pattern_versions": overlay.selected_pattern_versions,
                        "harness_overlay_mode": overlay.mode.value,
                        "harness_execution_policy": (
                            execution_policy.model_dump(mode="json")
                            if execution_policy is not None
                            else {}
                        ),
                        "harness_execution_policy_hash": (
                            execution_policy.policy_hash
                            if execution_policy is not None
                            else None
                        ),
                    }
                )
        trajectory = RunTrajectory(
            context=context,
            user_input=user_input,
            idempotency_key=idempotency_key,
            snapshot=snapshot,
            status=RunStatus.RUNNING,
        )
        await self._trajectories.save(trajectory)
        return trajectory

    async def execute_run(self, trajectory: RunTrajectory) -> RunTrajectory:
        context = trajectory.context
        try:
            answer = await self._runtime.run(trajectory.user_input, context)
            answer = self._answer_publisher.hydrate_view(answer)
        except asyncio.CancelledError:
            tool_events = (
                await self._event_recorder.drain_tools(context.run_id)
                if self._event_recorder is not None
                else []
            )
            cancelled = trajectory.model_copy(
                update={
                    "status": RunStatus.CANCELLED,
                    "tool_events": tool_events,
                    "completed_at": datetime.now(UTC),
                }
            )
            await self._process_automatic_learning(cancelled, "run_completed")
            raise
        except Exception:
            tool_events = (
                await self._event_recorder.drain_tools(context.run_id)
                if self._event_recorder is not None
                else []
            )
            failed = trajectory.model_copy(
                update={
                    "status": RunStatus.FAILED,
                    "tool_events": tool_events,
                    "completed_at": datetime.now(UTC),
                }
            )
            await self._process_automatic_learning(failed, "run_completed")
            raise

        tool_events = (
            await self._event_recorder.drain_tools(context.run_id)
            if self._event_recorder is not None
            else []
        )
        completed = trajectory.model_copy(
            update={
                "status": RunStatus.COMPLETED,
                "answer": answer,
                "tool_events": tool_events,
                "completed_at": datetime.now(UTC),
            }
        )
        return await self._process_automatic_learning(completed, "run_completed")

    @property
    def event_recorder(self) -> RunEventRecorder:
        if self._event_recorder is None:
            self._event_recorder = RunEventRecorder()
        return self._event_recorder

    async def get_run(self, run_id: UUID) -> RunTrajectory | None:
        return await self._trajectories.get(run_id)

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
        *,
        tenant_id: str = "local",
        project_id: str = "default",
        user_id: str = "local-user",
    ) -> RunTrajectory | None:
        return await self._trajectories.get_by_idempotency_key(
            idempotency_key,
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
        )

    async def mark_interrupted(self, run_id: UUID) -> RunTrajectory | None:
        trajectory = await self._trajectories.get(run_id)
        if trajectory is None or trajectory.status != RunStatus.RUNNING:
            return trajectory
        interrupted = trajectory.model_copy(
            update={
                "status": RunStatus.FAILED,
                "completed_at": datetime.now(UTC),
                "tags": list(dict.fromkeys([*trajectory.tags, "run_interrupted"])),
            }
        )
        return await self._process_automatic_learning(interrupted, "run_completed")

    async def mark_cancelled(self, run_id: UUID) -> RunTrajectory | None:
        trajectory = await self._trajectories.get(run_id)
        if trajectory is None or trajectory.status != RunStatus.RUNNING:
            return trajectory
        cancelled = trajectory.model_copy(
            update={
                "status": RunStatus.CANCELLED,
                "completed_at": datetime.now(UTC),
            }
        )
        return await self._process_automatic_learning(cancelled, "run_completed")

    async def subscribe_tool_events(
        self,
        run_id: UUID,
    ) -> asyncio.Queue[ToolEvent] | None:
        if self._event_recorder is None:
            return None
        return await self._event_recorder.subscribe_tools(run_id)

    async def unsubscribe_tool_events(
        self,
        run_id: UUID,
        queue: asyncio.Queue[ToolEvent] | None,
    ) -> None:
        if self._event_recorder is None or queue is None:
            return
        await self._event_recorder.unsubscribe_tools(run_id, queue)

    async def feedback(
        self,
        run_id: str,
        score: float,
        text: str | None = None,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        allowed_projects: frozenset[str] = frozenset(),
    ) -> RunTrajectory:
        trajectory = await self._trajectories.get(UUID(run_id))
        if (
            trajectory is None
            or (tenant_id is not None and trajectory.context.tenant_id != tenant_id)
            or (user_id is not None and trajectory.context.user_id != user_id)
            or (
                allowed_projects
                and trajectory.context.project_id not in allowed_projects
            )
        ):
            raise KeyError(f"Unknown run: {run_id}")
        updated = trajectory.model_copy(update={"feedback_score": score, "feedback_text": text})
        return await self._process_automatic_learning(updated, "feedback_received")

    async def _process_automatic_learning(
        self,
        trajectory: RunTrajectory,
        trigger: LearningTrigger,
    ) -> RunTrajectory:
        """Persist a deterministic learning audit and dispatch only eligible work.

        Explicit user operations, including user-authored memory writes, use their
        dedicated write gates and do not pass through this automatic dispatcher.
        """

        audited, decision = annotate_trajectory_for_automatic_learning(trajectory)
        await self._trajectories.save(audited)
        if (
            not decision.allowed
            or self._learning_processor is None
            or self._settings.learning_mode == "disabled"
        ):
            return audited
        try:
            await self._learning_processor(audited, trigger)
        except Exception:
            failure_tag = (
                "learning_feedback_failed"
                if trigger == "feedback_received"
                else "learning_postprocess_failed"
            )
            audited = audited.model_copy(
                update={"tags": list(dict.fromkeys([*audited.tags, failure_tag]))}
            )
            await self._trajectories.save(audited)
        return audited


def answer_from_trajectory(trajectory: RunTrajectory) -> AnswerResponse:
    if trajectory.answer is None:
        raise ValueError("Trajectory has no answer")
    return trajectory.answer
