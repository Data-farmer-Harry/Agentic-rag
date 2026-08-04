from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from app.application.run_service import RunService
from app.config import Settings
from app.domain.enums import AnswerMode, EvidenceLevel, RunStatus
from app.domain.models import AnswerResponse, Claim, EvidenceRef, Provenance, RunContext
from app.harness.consumer import BoundedHarnessConsumer
from app.harness.models import (
    HarnessConfigDelta,
    HarnessOverlayMode,
    HarnessToolConfig,
    RunHarnessOverlay,
    canonical_hash,
)
from app.infra.local_repositories import JsonlTrajectoryRepository


class StubRuntime:
    def __init__(self) -> None:
        self.contexts: list[RunContext] = []

    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        self.contexts.append(context)
        return AnswerResponse(
            answer_markdown=f"Handled: {user_input}",
            confidence=EvidenceLevel.INSUFFICIENT,
        )


class ConversationalRuntime:
    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        del user_input, context
        return AnswerResponse(
            answer_markdown="你好！",
            response_mode=AnswerMode.CONVERSATIONAL,
        )


class SafeGroundedRuntime:
    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        evidence = EvidenceRef(
            text="The Atlas service map is verified by the team architecture document.",
            provenance=Provenance(
                source_type="enterprise_fixture",
                source_id="northstar:architecture:system-overview#chunk=0",
                trust="verified",
            ),
            metadata={"knowledge_layer": "team_internal"},
        )
        return AnswerResponse(
            answer_markdown=f"Handled safely: {user_input}",
            claims=[
                Claim(
                    text="The Atlas service map is verified.",
                    evidence_ids=[evidence.evidence_id],
                    level=EvidenceLevel.SUPPORTED,
                )
            ],
            citations=[evidence],
            confidence=EvidenceLevel.SUPPORTED,
        )


class FailingRuntime:
    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        del user_input, context
        raise RuntimeError("fixture runtime failure")


class StubOverlaySelector:
    def __init__(self, mode: HarnessOverlayMode) -> None:
        self._mode = mode

    async def select(
        self,
        *,
        context: RunContext,
        query: str,
        baseline_policy_versions: dict[str, str],
    ) -> RunHarnessOverlay:
        del query
        payload = {
            "overlay_id": uuid5(NAMESPACE_URL, f"overlay:{context.run_id}"),
            "run_id": context.run_id,
            "tenant_id": context.tenant_id,
            "project_id": context.project_id,
            "baseline_policy_versions": baseline_policy_versions,
            "selected_pattern_versions": [
                "00000000-0000-0000-0000-000000000001@1.0.0"
            ],
            "positive_experience_ids": [],
            "negative_experience_ids": [],
            "effective_delta": HarnessConfigDelta(
                tool=HarnessToolConfig(graph_hops=1)
            ),
            "clamped_fields": [],
            "rejected_conflicts": [],
            "selection_trace_codes": ["test"],
            "selector_revision": "test-selector",
            "experience_bank_revision": "test-experiences",
            "pattern_bank_revision": "test-patterns",
            "mode": self._mode,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "expires_at": None,
        }
        return RunHarnessOverlay.model_validate(
            {**payload, "payload_hash": canonical_hash(payload)}
        )


@pytest.mark.asyncio
async def test_run_service_persists_snapshot_and_result(tmp_path: Path) -> None:
    repository = JsonlTrajectoryRepository(tmp_path / "runs.jsonl")

    async def skill_versions(context: RunContext) -> dict[str, str]:
        assert context.skill_versions == {}
        return {"evidence_compare": "1.2.0"}

    service = RunService(
        runtime=StubRuntime(),
        trajectories=repository,
        settings=Settings(app_env="test", data_dir=tmp_path),
        skill_version_provider=skill_versions,
    )

    completed = await service.run("test task")
    loaded = await repository.get(completed.context.run_id)

    assert completed.status == RunStatus.COMPLETED
    assert completed.snapshot is not None
    assert completed.context.skill_versions == {"evidence_compare": "1.2.0"}
    assert completed.snapshot.skill_versions == completed.context.skill_versions
    assert loaded == completed


@pytest.mark.asyncio
async def test_run_service_uses_preallocated_run_id_for_live_event_subscription(
    tmp_path: Path,
) -> None:
    runtime = StubRuntime()
    service = RunService(
        runtime=runtime,
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
    )
    run_id = uuid4()

    completed = await service.run("stream this task", run_id=run_id)

    assert completed.context.run_id == run_id
    assert runtime.contexts[0].run_id == run_id


@pytest.mark.asyncio
async def test_run_service_records_conversation_feedback_without_automatic_learning(
    tmp_path: Path,
) -> None:
    repository = JsonlTrajectoryRepository(tmp_path / "runs.jsonl")
    learning_triggers: list[str] = []

    async def learn(trajectory, trigger: str) -> None:
        del trajectory
        learning_triggers.append(trigger)

    service = RunService(
        runtime=ConversationalRuntime(),
        trajectories=repository,
        settings=Settings(app_env="test", data_dir=tmp_path),
        learning_processor=learn,
    )

    completed = await service.run("你好")
    updated = await service.feedback(str(completed.context.run_id), 0.0, "太模板化了")

    assert completed.answer is not None
    assert completed.answer.response_mode == AnswerMode.CONVERSATIONAL
    assert updated.feedback_text == "太模板化了"
    assert "learning_non_learnable" in updated.tags
    assert "learning_gate_reason:response_mode_conversational" in updated.tags
    assert learning_triggers == []


@pytest.mark.asyncio
async def test_run_service_dispatches_only_safe_grounded_learning(
    tmp_path: Path,
) -> None:
    received: list[tuple[str, list[str]]] = []

    async def learn(trajectory, trigger: str) -> None:
        received.append((trigger, trajectory.tags))

    service = RunService(
        runtime=SafeGroundedRuntime(),
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
        learning_processor=learn,
    )

    completed = await service.run("Explain the Atlas service map")
    updated = await service.feedback(str(completed.context.run_id), 0.8, "Useful")

    assert [trigger for trigger, _ in received] == [
        "run_completed",
        "feedback_received",
    ]
    assert "learning_gate:eligible" in completed.tags
    assert "learning_gate:eligible" in updated.tags


@pytest.mark.asyncio
async def test_run_service_failed_run_is_audited_but_never_dispatched_to_learning(
    tmp_path: Path,
) -> None:
    received: list[str] = []

    async def learn(trajectory, trigger: str) -> None:
        del trajectory
        received.append(trigger)

    repository = JsonlTrajectoryRepository(tmp_path / "runs.jsonl")
    service = RunService(
        runtime=FailingRuntime(),
        trajectories=repository,
        settings=Settings(app_env="test", data_dir=tmp_path),
        learning_processor=learn,
    )

    with pytest.raises(RuntimeError, match="fixture runtime failure"):
        await service.run("Break this run")

    runs = await repository.list_session(limit=10)
    assert len(runs) == 1
    assert runs[0].status == RunStatus.FAILED
    assert "learning_non_learnable" in runs[0].tags
    assert "learning_gate_reason:run_status_failed" in runs[0].tags
    assert received == []


@pytest.mark.asyncio
async def test_run_service_cancelled_run_is_audited_but_never_dispatched_to_learning(
    tmp_path: Path,
) -> None:
    received: list[str] = []

    async def learn(trajectory, trigger: str) -> None:
        del trajectory
        received.append(trigger)

    repository = JsonlTrajectoryRepository(tmp_path / "runs.jsonl")
    service = RunService(
        runtime=StubRuntime(),
        trajectories=repository,
        settings=Settings(app_env="test", data_dir=tmp_path),
        learning_processor=learn,
    )
    prepared = await service.prepare_run("Cancel this run")

    cancelled = await service.mark_cancelled(prepared.context.run_id)

    assert cancelled is not None
    assert cancelled.status == RunStatus.CANCELLED
    assert "learning_non_learnable" in cancelled.tags
    assert "learning_gate_reason:run_status_cancelled" in cancelled.tags
    assert received == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_applied"),
    [
        (HarnessOverlayMode.SHADOW, False),
        (HarnessOverlayMode.CANARY, True),
    ],
)
async def test_run_service_freezes_bounded_execution_policy(
    tmp_path: Path,
    mode: HarnessOverlayMode,
    expected_applied: bool,
) -> None:
    repository = JsonlTrajectoryRepository(tmp_path / f"{mode.value}.jsonl")
    runtime = StubRuntime()
    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        harness_overlay_mode=mode.value,
    )
    service = RunService(
        runtime=runtime,
        trajectories=repository,
        settings=settings,
        overlay_selector=StubOverlaySelector(mode),  # type: ignore[arg-type]
        harness_consumer=BoundedHarnessConsumer(),
    )

    completed = await service.run("graph task")

    policy = runtime.contexts[0].execution_policy
    assert policy is not None
    assert policy.behavior_applied is expected_applied
    assert bool(policy.applied_pattern_versions) is expected_applied
    assert completed.snapshot is not None
    assert completed.snapshot.harness_execution_policy_hash == policy.policy_hash
    assert completed.snapshot.harness_execution_policy == policy.model_dump(mode="json")
