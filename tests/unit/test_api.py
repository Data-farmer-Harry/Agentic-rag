import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from app.agent.adaptive_rag_router import AdaptiveRAGRouterError
from app.agent.hermes_bridge import HermesCapabilityBridge
from app.agent.hermes_runtime import HermesRunTimeoutError
from app.api.app import create_app
from app.application.run_event_recorder import RunEventRecorder
from app.application.run_service import RunService
from app.bootstrap import build_components
from app.config import Settings
from app.domain.enums import EvidenceLevel, IngestionJobStatus, LearningJobStatus, RunStatus
from app.domain.models import (
    AnswerResponse,
    IngestionJob,
    IngestionJobSubmission,
    LearningJob,
    RetrievalBundle,
    RunContext,
    RunTrajectory,
)
from app.infra.local_repositories import JsonlTrajectoryRepository


class StubRuntime:
    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        return AnswerResponse(answer_markdown=user_input, confidence=EvidenceLevel.INSUFFICIENT)


class BusyRuntime:
    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        del user_input, context
        raise RuntimeError("HTTP 429 model_cooldown with private provider details")


class TimeoutRuntime:
    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        del user_input, context
        raise HermesRunTimeoutError("Hermes run timed out after 90 seconds private-run-id")


class UnavailableAdaptiveRouterRuntime(StubRuntime):
    async def prepare_route(self, user_input: str, context: RunContext) -> None:
        del user_input, context
        raise AdaptiveRAGRouterError("private upstream routing failure")


class EmptyRetrieval:
    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: dict[str, object] | None = None,
        top_k: int = 10,
    ) -> RetrievalBundle:
        del context, filters, top_k
        return RetrievalBundle(query=query)


class _AsyncIngestionWorkspaceStub:
    max_upload_bytes = 10_000

    def __init__(self) -> None:
        self.jobs: dict[UUID, IngestionJob] = {}

    async def submit_ingestion_job(self, **kwargs):  # type: ignore[no-untyped-def]
        content = kwargs["content"]
        job = IngestionJob(
            tenant_id="local",
            project_id=kwargs["project_id"],
            user_id=kwargs["user_id"],
            filename=kwargs["filename"],
            media_type=kwargs["media_type"],
            byte_size=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            staging_key="private/staging-key.upload",
            lease_owner="private-worker-id",
        )
        self.jobs[job.job_id] = job
        return IngestionJobSubmission(job=job)

    async def list_ingestion_jobs(
        self, *, tenant_id: str, project_id: str, limit: int
    ) -> list[IngestionJob]:
        assert tenant_id == "local"
        return [item for item in self.jobs.values() if item.project_id == project_id][:limit]

    async def get_ingestion_job(
        self, job_id: UUID, *, tenant_id: str, project_id: str
    ) -> IngestionJob | None:
        assert tenant_id == "local"
        job = self.jobs.get(job_id)
        return job if job is not None and job.project_id == project_id else None

    async def cancel_ingestion_job(
        self, job_id: UUID, *, tenant_id: str, project_id: str
    ) -> IngestionJob:
        job = await self.get_ingestion_job(
            job_id, tenant_id=tenant_id, project_id=project_id
        )
        if job is None:
            raise KeyError("Ingestion job not found")
        cancelled = job.model_copy(
            update={
                "status": IngestionJobStatus.CANCELLED,
                "can_retry": True,
            }
        )
        self.jobs[job_id] = cancelled
        return cancelled

    async def retry_ingestion_job(
        self, job_id: UUID, *, tenant_id: str, project_id: str
    ) -> IngestionJob:
        job = await self.get_ingestion_job(
            job_id, tenant_id=tenant_id, project_id=project_id
        )
        if job is None:
            raise KeyError("Ingestion job not found")
        retried = job.model_copy(
            update={
                "status": IngestionJobStatus.QUEUED,
                "can_retry": False,
            }
        )
        self.jobs[job_id] = retried
        return retried


class _LearningWorkspaceStub:
    def __init__(self) -> None:
        trajectory = RunTrajectory(
            context=RunContext(project_id="default"),
            user_input="learn this run",
            status=RunStatus.COMPLETED,
        )
        job = LearningJob(
            idempotency_key="a" * 64,
            run_id=trajectory.context.run_id,
            trigger="run_completed",
            trajectory=trajectory,
            lease_owner="private-worker",
            lease_token=UUID("00000000-0000-0000-0000-000000000001"),
        )
        self.jobs = {job.job_id: job}

    async def list_learning_jobs(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int,
    ) -> list[LearningJob]:
        assert tenant_id == "local"
        return [item for item in self.jobs.values() if item.project_id == project_id][:limit]

    async def get_learning_job(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningJob | None:
        assert tenant_id == "local"
        job = self.jobs.get(job_id)
        return job if job is not None and job.project_id == project_id else None

    async def cancel_learning_job(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningJob:
        job = await self.get_learning_job(
            job_id, tenant_id=tenant_id, project_id=project_id
        )
        if job is None:
            raise KeyError("Learning job not found")
        cancelled = job.model_copy(
            update={"status": LearningJobStatus.CANCELLED, "can_retry": True}
        )
        self.jobs[job_id] = cancelled
        return cancelled

    async def retry_learning_job(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> LearningJob:
        job = await self.get_learning_job(
            job_id, tenant_id=tenant_id, project_id=project_id
        )
        if job is None:
            raise KeyError("Learning job not found")
        retried = job.model_copy(update={"status": LearningJobStatus.QUEUED, "can_retry": False})
        self.jobs[job_id] = retried
        return retried


class _UnavailableGraphWorkspace:
    async def graph_search(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("graph backend is unavailable")

    async def resolve_graph_entities(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("graph backend is unavailable")

    async def retrieve_evidence_subgraph(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("graph backend is unavailable")

    async def compare_graph_entities(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("graph backend is unavailable")


@pytest.mark.asyncio
async def test_health_and_run(tmp_path: Path) -> None:
    service = RunService(
        runtime=StubRuntime(),
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
    )
    transport = ASGITransport(app=create_app(service))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/health")
        response = await client.post(
            "/v1/projects/default/runs",
            json={"input": "hello", "domain_pack": "general"},
        )

    assert health.json()["status"] == "ok"
    assert response.status_code == 200
    assert response.json()["answer"]["answer_markdown"] == "hello"


@pytest.mark.asyncio
async def test_adaptive_router_failure_is_a_public_retryable_503(tmp_path: Path) -> None:
    service = RunService(
        runtime=UnavailableAdaptiveRouterRuntime(),
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
    )
    transport = ASGITransport(app=create_app(service))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/projects/default/runs/start",
            json={
                "input": "哈哈你好",
                "idempotency_key": "adaptive-router-unavailable-1234",
            },
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "模型路由服务暂时不可用，请稍后重试。"}


@pytest.mark.asyncio
async def test_sync_run_timeout_is_a_stable_public_504(tmp_path: Path) -> None:
    service = RunService(
        runtime=TimeoutRuntime(),
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
    )
    transport = ASGITransport(app=create_app(service))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/projects/default/runs",
            json={"input": "复杂任务"},
        )

    assert response.status_code == 504
    assert response.json() == {
        "detail": {
            "code": "provider_timeout",
            "message": "模型响应超时，请重新发送；复杂任务也可以稍后再试。",
            "retryable": True,
        }
    }
    assert "private-run-id" not in response.text


@pytest.mark.asyncio
async def test_start_run_is_idempotent_and_events_resume_after_cursor(tmp_path: Path) -> None:
    repository = JsonlTrajectoryRepository(tmp_path / "runs.jsonl")
    recorder = RunEventRecorder(tmp_path / "events.jsonl")
    service = RunService(
        runtime=StubRuntime(),
        trajectories=repository,
        settings=Settings(app_env="test", data_dir=tmp_path),
        event_recorder=recorder,
    )
    transport = ASGITransport(app=create_app(service))
    body = {
        "input": "resume this",
        "session_id": "session-resume",
        "idempotency_key": "request-resume-1234",
    }
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/v1/projects/default/runs/start", json=body)
        repeated = await client.post("/v1/projects/default/runs/start", json=body)
        conflict = await client.post(
            "/v1/projects/default/runs/start",
            json={**body, "input": "different request"},
        )
        run_id = first.json()["run_id"]
        async with client.stream(
            "GET",
            f"/v1/projects/default/runs/{run_id}/events/stream",
        ) as response:
            stream_text = "".join([chunk async for chunk in response.aiter_text()])
        all_events = await client.get(f"/v1/projects/default/runs/{run_id}/events")
        cursor = all_events.json()[1]["cursor"]
        resumed = await client.get(
            f"/v1/projects/default/runs/{run_id}/events",
            params={"after_cursor": cursor},
        )
        hidden = await client.get(
            f"/v1/projects/default/runs/{run_id}/events",
            params={"user_id": "another-user"},
        )

    saved = await repository.list_recent()
    assert first.status_code == 202
    assert first.json()["coalesced"] is False
    assert repeated.json()["run_id"] == run_id
    assert repeated.json()["coalesced"] is True
    assert conflict.status_code == 409
    assert len(saved) == 1
    assert "id: 1" in stream_text
    assert "event: run.completed" in stream_text
    assert all(item["cursor"] > cursor for item in resumed.json())
    assert resumed.json()[-1]["event"] == "run.completed"
    assert hidden.status_code == 403


@pytest.mark.asyncio
async def test_stream_returns_actionable_public_error_without_provider_details(
    tmp_path: Path,
) -> None:
    service = RunService(
        runtime=BusyRuntime(),
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
    )
    transport = ASGITransport(app=create_app(service))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/projects/default/runs/stream",
            json={"input": "hello"},
        ) as response:
            stream_text = "".join([chunk async for chunk in response.aiter_text()])

    assert response.status_code == 200
    assert '"code":"provider_busy"' in stream_text
    assert '"retryable":true' in stream_text
    assert "模型服务当前繁忙，请稍后重试。" in stream_text
    assert "model_cooldown" not in stream_text
    assert "private provider details" not in stream_text


@pytest.mark.asyncio
async def test_internal_hermes_bridge_requires_bearer_auth(tmp_path: Path) -> None:
    service = RunService(
        runtime=StubRuntime(),
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
    )
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=EmptyRetrieval(),
    )
    bridge_id = await bridge.open_run(RunContext())
    transport = ASGITransport(app=create_app(service, hermes_bridge=bridge))
    path = f"/internal/hermes/runs/{bridge_id}/tools/search_knowledge"

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        bridge_health = await client.get(
            "/internal/hermes/health",
            headers={"Authorization": "Bearer bridge-secret"},
        )
        unauthorized = await client.post(path, json={"query": "test"})
        authorized = await client.post(
            path,
            headers={"Authorization": "Bearer bridge-secret"},
            json={"query": "test"},
        )

    assert unauthorized.status_code == 401
    assert bridge_health.json() == {
        "status": "ok",
        "service": "hermesgraph-bridge",
    }
    assert authorized.status_code == 200
    assert authorized.json()["success"] is True


@pytest.mark.asyncio
async def test_hermes_native_learning_audit_and_accept_api(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        runtime_mode="hermes",
        hermes_api_key="api-secret",
        hermes_bridge_token="bridge-secret",
        hermes_native_admin_token="native-admin-secret",
        embedding_provider="deterministic",
    )
    components = build_components(settings)
    components.hermes_native_learning_service._admin = None
    assert components.hermes_bridge is not None
    bridge_id = await components.hermes_bridge.open_run(RunContext(project_id="default"))
    ingested = await components.ingestion_service.ingest(
        filename="safe-native-learning.md",
        content=b"SAFE-NATIVE-LEARNING-101 is trusted project knowledge.",
        media_type="text/markdown",
        user_id="local-user",
    )
    retrieved = await components.hermes_bridge.invoke(
        bridge_id,
        "search_knowledge",
        {"query": "SAFE-NATIVE-LEARNING-101"},
    )
    citation_id = retrieved["result"]["evidence"][0]["evidence_id"]
    await components.hermes_bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {
            "answer_markdown": "The safe learning marker is documented.",
            "citation_ids": [citation_id],
            "confidence": "supported",
        },
    )
    await components.hermes_bridge.complete(bridge_id)
    assert ingested.document.chunk_count > 0
    transport = ASGITransport(
        app=create_app(
            components.run_service,
            components.workspace_service,
            hermes_bridge=components.hermes_bridge,
        )
    )
    event = {
        "tool_name": "memory",
        "args": {
            "action": "add",
            "target": "memory",
            "content": {"redacted": True, "length": 12, "sha256": "a" * 64},
        },
        "result": '{"success": true}',
        "status": "ok",
        "applied": True,
        "snapshot": {
            "snapshot_id": "1" * 32,
            "target_kind": "memory",
            "target_id": "memory",
            "before_hash": "2" * 64,
            "after_hash": "3" * 64,
            "applied": True,
            "rollback_supported": True,
            "reason": None,
        },
    }

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        recorded = await client.post(
            f"/internal/hermes/runs/{bridge_id}/events",
            headers={"Authorization": "Bearer bridge-secret"},
            json=event,
        )
        audits = await client.get("/v1/projects/default/hermes/native-learning")
        change_set_id = audits.json()[0]["change_set_id"]
        accepted = await client.post(
            f"/v1/projects/default/hermes/native-learning/{change_set_id}/review",
            json={
                "decision": "accept",
                "reason": "checked",
            },
        )
        hidden = await client.get("/v1/projects/other/hermes/native-learning")

    assert recorded.status_code == 200
    assert audits.status_code == 200
    assert audits.json()[0]["rollback_supported"] is True
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"
    assert hidden.json() == []
    await components.close()


@pytest.mark.asyncio
async def test_async_ingestion_job_api_is_scoped_and_hides_internal_fields(
    tmp_path: Path,
) -> None:
    service = RunService(
        runtime=StubRuntime(),
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
    )
    workspace = _AsyncIngestionWorkspaceStub()
    transport = ASGITransport(app=create_app(service, workspace))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        submitted = await client.post(
            "/v1/projects/default/ingestion-jobs",
            files={"file": ("queued.md", b"queued knowledge", "text/markdown")},
        )
        job_id = submitted.json()["job"]["job_id"]
        listed = await client.get("/v1/projects/default/ingestion-jobs")
        hidden = await client.get(f"/v1/projects/other/ingestion-jobs/{job_id}")
        cancelled = await client.delete(f"/v1/projects/default/ingestion-jobs/{job_id}")
        retried = await client.post(f"/v1/projects/default/ingestion-jobs/{job_id}/retry")

    assert submitted.status_code == 202
    assert submitted.json()["coalesced"] is False
    assert "staging_key" not in submitted.text
    assert "lease_owner" not in submitted.text
    assert listed.status_code == 200
    assert listed.json()[0]["user_id"] == "local-user"
    assert hidden.status_code == 404
    assert cancelled.json()["status"] == "cancelled"
    assert retried.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_learning_job_api_is_scoped_and_hides_snapshot_and_lease(
    tmp_path: Path,
) -> None:
    service = RunService(
        runtime=StubRuntime(),
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
    )
    workspace = _LearningWorkspaceStub()
    job_id = next(iter(workspace.jobs))
    transport = ASGITransport(app=create_app(service, workspace))  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/v1/projects/default/learning-jobs")
        hidden = await client.get(f"/v1/projects/other/learning-jobs/{job_id}")
        cancelled = await client.delete(f"/v1/projects/default/learning-jobs/{job_id}")
        retried = await client.post(f"/v1/projects/default/learning-jobs/{job_id}/retry")

    assert listed.status_code == 200
    assert "trajectory" not in listed.text
    assert "lease_owner" not in listed.text
    assert "lease_token" not in listed.text
    assert hidden.status_code == 404
    assert cancelled.json()["status"] == "cancelled"
    assert retried.json()["status"] == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/v1/projects/default/graph/search",
            {"entities": ["Alpha Service"], "template": "neighbors"},
        ),
        (
            "/v1/projects/default/graph/entities/resolve",
            {"mentions": ["Alpha Service"]},
        ),
        (
            "/v1/projects/default/graph/retrieve",
            {"query": "Alpha Service dependencies"},
        ),
        (
            "/v1/projects/default/graph/compare",
            {"left_entity": "Alpha Service", "right_entity": "Beta Service"},
        ),
    ],
)
async def test_graph_api_maps_backend_runtime_errors_to_service_unavailable(
    tmp_path: Path,
    path: str,
    payload: dict[str, object],
) -> None:
    service = RunService(
        runtime=StubRuntime(),
        trajectories=JsonlTrajectoryRepository(tmp_path / "runs.jsonl"),
        settings=Settings(app_env="test", data_dir=tmp_path),
    )
    transport = ASGITransport(
        app=create_app(service, _UnavailableGraphWorkspace())  # type: ignore[arg-type]
    )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(path, json=payload)

    assert response.status_code == 503
    assert response.json()["detail"] == "Graph service unavailable"


@pytest.mark.asyncio
async def test_streaming_workspace_and_graph_endpoints(tmp_path: Path) -> None:
    components = build_components(
        Settings(
            app_env="test",
            data_dir=tmp_path,
            runtime_mode="offline",
            learning_mode="observe",
            skill_min_similar_runs=3,
            skill_min_successful_runs=2,
        )
    )
    transport = ASGITransport(app=create_app(components.run_service, components.workspace_service))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream(
            "POST",
            "/v1/projects/default/runs/stream",
            json={"input": "LangChain Integration Runtime", "domain_pack": "general"},
        ) as response:
            stream_text = "".join([chunk async for chunk in response.aiter_text()])

        overview = await client.get("/v1/workspace/overview")
        runs = await client.get("/v1/projects/default/runs")
        memories = await client.get("/v1/projects/default/memories")
        changes = await client.get("/v1/projects/default/learning-changes")
        graph = await client.post(
            "/v1/projects/default/graph/search",
            json={"entities": ["LangChain"], "template": "neighbors"},
        )
        resolved_graph_entities = await client.post(
            "/v1/projects/default/graph/entities/resolve",
            json={"mentions": ["LangChain Integration Runtime"]},
        )
        evidence_subgraph = await client.post(
            "/v1/projects/default/graph/retrieve",
            json={
                "query": "How does LangChain Integration Runtime orchestrate retrieval?",
                "seed_entities": ["LangChain Integration Runtime"],
                "max_hops": 2,
            },
        )
        graph_comparison = await client.post(
            "/v1/projects/default/graph/compare",
            json={
                "left_entity": "LangChain Integration Runtime",
                "right_entity": "Knowledge Retrieval",
            },
        )

    assert response.status_code == 200
    assert "event: run.accepted" in stream_text
    assert '"phase":"executing"' in stream_text
    assert "event: answer.delta" in stream_text
    assert "event: evidence.added" in stream_text
    assert "event: run.completed" in stream_text
    assert stream_text.index("event: tool.completed") < stream_text.index("event: answer.delta")
    assert overview.json()["counts"]["runs"] == 1
    assert len(runs.json()) == 1
    assert len(memories.json()) == 1
    assert len(changes.json()) == 1
    assert graph.status_code == 200
    assert graph.json()["paths"]
    assert resolved_graph_entities.status_code == 200
    assert resolved_graph_entities.json()["matches"][0]["node"]["node_id"] == (
        "integration_runtime"
    )
    assert evidence_subgraph.status_code == 200
    assert evidence_subgraph.json()["graph_paths"]
    assert evidence_subgraph.json()["trace"]["strategy"] == ("vector_graph_evidence_fusion")
    assert graph_comparison.status_code == 200
    assert graph_comparison.json()["trace"]["connected"] is True


@pytest.mark.asyncio
async def test_conversation_api_groups_sessions_and_restores_chronological_runs(
    tmp_path: Path,
) -> None:
    components = build_components(
        Settings(
            app_env="test",
            data_dir=tmp_path,
            runtime_mode="offline",
            learning_mode="disabled",
        )
    )
    base_time = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
    runs = [
        RunTrajectory(
            context=RunContext(
                session_id="alpha",
                user_id="local-user",
                started_at=base_time,
            ),
            user_input="Alpha first question",
            answer=AnswerResponse(
                answer_markdown="Alpha first answer",
                confidence=EvidenceLevel.SUPPORTED,
            ),
            status=RunStatus.COMPLETED,
            completed_at=base_time + timedelta(seconds=1),
        ),
        RunTrajectory(
            context=RunContext(
                session_id="alpha",
                user_id="local-user",
                started_at=base_time + timedelta(minutes=1),
            ),
            user_input="Alpha follow-up",
            answer=AnswerResponse(
                answer_markdown="Alpha latest answer",
                confidence=EvidenceLevel.SUPPORTED,
            ),
            status=RunStatus.COMPLETED,
            completed_at=base_time + timedelta(minutes=1, seconds=1),
        ),
        RunTrajectory(
            context=RunContext(
                session_id="beta",
                user_id="local-user",
                started_at=base_time + timedelta(minutes=2),
            ),
            user_input=(
                "Beta only question\n\n<attachments>\n- architecture.txt\n</attachments>"
            ),
            status=RunStatus.CANCELLED,
            completed_at=base_time + timedelta(minutes=2, seconds=1),
        ),
        RunTrajectory(
            context=RunContext(
                session_id="alpha",
                user_id="other-user",
                started_at=base_time + timedelta(minutes=3),
            ),
            user_input="Other user's private turn",
            answer=AnswerResponse(
                answer_markdown="Private answer",
                confidence=EvidenceLevel.SUPPORTED,
            ),
            status=RunStatus.COMPLETED,
            completed_at=base_time + timedelta(minutes=3, seconds=1),
        ),
    ]
    for run in runs:
        await components.trajectory_repository.save(run)

    transport = ASGITransport(
        app=create_app(components.run_service, components.workspace_service)
    )
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        conversations = await client.get(
            "/v1/projects/default/conversations",
            params={"user_id": "local-user"},
        )
        alpha_runs = await client.get(
            "/v1/projects/default/conversations/alpha/runs",
            params={"user_id": "local-user"},
        )
        other_runs = await client.get(
            "/v1/projects/default/conversations/alpha/runs",
            params={"user_id": "other-user"},
        )
        renamed = await client.patch(
            "/v1/projects/default/conversations/alpha",
            params={"user_id": "local-user"},
            json={"title": "Architecture notes"},
        )
        archived = await client.patch(
            "/v1/projects/default/conversations/alpha",
            params={"user_id": "local-user"},
            json={"archived": True},
        )
        active_conversations = await client.get(
            "/v1/projects/default/conversations",
            params={"user_id": "local-user"},
        )
        all_conversations = await client.get(
            "/v1/projects/default/conversations",
            params={"user_id": "local-user", "include_archived": True},
        )
        other_conversations = await client.get(
            "/v1/projects/default/conversations",
            params={"user_id": "other-user", "include_archived": True},
        )
        restored = await client.patch(
            "/v1/projects/default/conversations/alpha",
            params={"user_id": "local-user"},
            json={"archived": False},
        )
        missing_conversation = await client.patch(
            "/v1/projects/default/conversations/missing",
            params={"user_id": "local-user"},
            json={"title": "Missing"},
        )
        remembered = await client.post(
            "/v1/projects/default/memories",
            params={"user_id": "local-user"},
            json={
                "summary": "I prefer concise technical answers.",
                "memory_type": "semantic",
                "source_session_id": "alpha",
            },
        )
        remembered_again = await client.post(
            "/v1/projects/default/memories",
            params={"user_id": "local-user"},
            json={
                "summary": "I prefer concise technical answers.",
                "memory_type": "semantic",
                "source_session_id": "alpha",
            },
        )
        local_memories = await client.get(
            "/v1/projects/default/memories",
            params={"user_id": "local-user"},
        )
        hidden_memories = await client.get(
            "/v1/projects/default/memories",
            params={"user_id": "other-user"},
        )

    assert conversations.status_code == 200
    summaries = conversations.json()
    assert [item["session_id"] for item in summaries] == ["beta", "alpha"]
    alpha = next(item for item in summaries if item["session_id"] == "alpha")
    beta = next(item for item in summaries if item["session_id"] == "beta")
    assert alpha["title"] == "Alpha first question"
    assert alpha["preview"] == "Alpha latest answer"
    assert alpha["run_count"] == 2
    assert beta["title"] == "Beta only question"
    assert beta["preview"] == "Beta only question"
    assert [item["user_input"] for item in alpha_runs.json()] == [
        "Alpha first question",
        "Alpha follow-up",
    ]
    assert other_runs.status_code == 403
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Architecture notes"
    assert archived.json()["archived"] is True
    assert [item["session_id"] for item in active_conversations.json()] == ["beta"]
    archived_alpha = next(
        item for item in all_conversations.json() if item["session_id"] == "alpha"
    )
    assert archived_alpha["title"] == "Architecture notes"
    assert archived_alpha["archived"] is True
    assert other_conversations.status_code == 403
    assert restored.json()["archived"] is False
    assert missing_conversation.status_code == 404
    assert remembered.status_code == 200
    assert remembered.json()["memory_id"] == remembered_again.json()["memory_id"]
    assert remembered.json()["confidence"] == 1.0
    assert remembered.json()["provenance"][0]["trust"] == "user_asserted"
    assert [item["summary"] for item in local_memories.json()] == [
        "I prefer concise technical answers."
    ]
    assert hidden_memories.status_code == 403
    await components.close()


@pytest.mark.asyncio
async def test_skill_evolution_api_uses_system_evaluation_and_health_gate(
    tmp_path: Path,
) -> None:
    components = build_components(
        Settings(
            app_env="test",
            data_dir=tmp_path,
            runtime_mode="offline",
            learning_mode="observe",
        )
    )
    transport = ASGITransport(app=create_app(components.run_service, components.workspace_service))
    query = "Hermes Agent Loop and LangChain integration"

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for index in range(3):
            response = await client.post(
                "/v1/projects/default/runs",
                json={"input": query, "session_id": f"mine-{index}"},
            )
            assert response.status_code == 200

        skills = await client.get("/v1/projects/default/skills")
        draft = next(item for item in skills.json() if item["status"] == "draft")
        skill_id = draft["skill_id"]
        skill_version = draft["version"]
        evaluated = await client.post(f"/v1/projects/default/skills/{skill_id}/evaluate")
        forged = await client.post(
            f"/v1/projects/default/skills/{skill_id}/transition",
            json={
                "target_status": "canary",
                "human_approved": True,
                "evaluation": {"security_passed": True},
            },
        )
        for index in range(3):
            shadow_run = await client.post(
                "/v1/projects/default/runs",
                json={"input": query, "session_id": f"shadow-{index}"},
            )
            assert shadow_run.status_code == 200
        evolution = await client.get("/v1/projects/default/skill-evolution")
        snapshots = evolution.json()
        snapshot = next(
            item
            for item in snapshots
            if item["skill"]["skill_id"] == skill_id and item["skill"]["version"] == skill_version
        )
        refinement_snapshot = next(
            item
            for item in snapshots
            if item["skill"]["skill_id"] == skill_id
            and item["skill"]["parent_version"] == skill_version
        )
        learning_changes = await client.get("/v1/projects/default/learning-changes")
        denied = await client.post(
            f"/v1/projects/default/skills/{skill_id}/transition",
            params={"skill_version": skill_version},
            json={"target_status": "canary", "human_approved": False},
        )
        approved = await client.post(
            f"/v1/projects/default/skills/{skill_id}/transition",
            params={"skill_version": skill_version},
            json={"target_status": "canary", "human_approved": True},
        )
        reports = await client.get(f"/v1/projects/default/skills/{skill_id}/evaluations")
        versioned_reports = await client.get(
            f"/v1/projects/default/skills/{skill_id}/evaluations",
            params={"skill_version": skill_version},
        )
        missing_version = await client.get(
            f"/v1/projects/default/skills/{skill_id}/evaluations",
            params={"skill_version": "99.0.0"},
        )
        transitions = await client.get(
            f"/v1/projects/default/skills/{skill_id}/transitions",
            params={"skill_version": skill_version},
        )

    assert evaluated.status_code == 200
    assert evaluated.json()["skill"]["status"] == "shadow"
    assert evaluated.json()["evaluation"]["evaluator_revision"] == (
        "counterfactual-skill-replay-v2"
    )
    assert forged.status_code == 422
    assert evolution.status_code == 200
    assert snapshot["health"]["promotion_ready"] is True
    assert snapshot["health"]["evaluated_observations"] >= 3
    promotion_evidence = snapshot["health"]["promotion_evidence"]
    assert promotion_evidence["tenant_id"] == "local"
    assert promotion_evidence["project_id"] == "default"
    assert promotion_evidence["skill_version"] == skill_version
    assert promotion_evidence["recommended_action"] == "promote"
    assert len(promotion_evidence["observation_ids"]) >= 3
    assert len(promotion_evidence["run_ids"]) >= 3
    assert refinement_snapshot["skill"]["status"] == "draft"
    refinement_change = next(
        item
        for item in learning_changes.json()
        if item["structured_diff"].get("operation") == "create_refinement_draft"
    )
    assert refinement_change["parent_version"] == skill_version
    assert refinement_change["structured_diff"]["change_level"] == "patch"
    assert "semantic_diff" in refinement_change["structured_diff"]
    assert denied.json()["allowed"] is False
    assert "human_approval_required" in denied.json()["reasons"]
    assert approved.json()["allowed"] is True
    assert approved.json()["promotion_evidence_id"] == promotion_evidence["evidence_id"]
    assert reports.json()[0]["skill_id"] == skill_id
    assert versioned_reports.json() == reports.json()
    assert missing_version.status_code == 404
    assert transitions.status_code == 200
    assert any(
        item["allowed"] is False and "human_approval_required" in item["reasons"]
        for item in transitions.json()
    )
    assert any(item["to_status"] == "canary" for item in transitions.json())
    applied_canary = next(
        item for item in transitions.json() if item["to_status"] == "canary" and item["applied"]
    )
    assert (
        applied_canary["promotion_evidence"]["evidence_id"] == (promotion_evidence["evidence_id"])
    )

    recent = await components.trajectory_repository.list_recent(limit=3)
    assert all("learning_postprocess_failed" not in item.tags for item in recent)


@pytest.mark.asyncio
async def test_document_api_drives_retrieval_and_archive(tmp_path: Path) -> None:
    components = build_components(
        Settings(app_env="test", data_dir=tmp_path, runtime_mode="offline")
    )
    transport = ASGITransport(app=create_app(components.run_service, components.workspace_service))
    content = b"The NEBULA-CROWN-7429 protocol requires two independent approvals."

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        uploaded = await client.post(
            "/v1/projects/default/documents",
            files={"file": ("protocol.md", content, "text/markdown")},
        )
        duplicate = await client.post(
            "/v1/projects/default/documents",
            files={"file": ("protocol-copy.md", content, "text/markdown")},
        )
        documents = await client.get("/v1/projects/default/documents")
        document_id = uploaded.json()["document"]["document_id"]
        detail = await client.get(f"/v1/projects/default/documents/{document_id}")
        retained_content = await client.get(f"/v1/projects/default/documents/{document_id}/content")
        candidates = await client.get("/v1/projects/default/graph/candidates")
        limited_candidates = await client.get("/v1/projects/default/graph/candidates?limit=1")
        relation_id = candidates.json()["relations"][0]["candidate_id"]
        reviewed = await client.post(
            f"/v1/projects/default/graph/candidates/relations/{relation_id}/review",
            json={
                "target_status": "approved",
                "reason": "Verified in the uploaded sentence.",
            },
        )
        run = await client.post(
            "/v1/projects/default/runs",
            json={"input": "What does NEBULA-CROWN-7429 require?", "domain_pack": "general"},
        )
        archived = await client.delete(f"/v1/projects/default/documents/{document_id}")
        archived_candidates = await client.get(
            "/v1/projects/default/graph/candidates?status=archived"
        )
        after_archive = await client.post(
            "/v1/projects/default/runs",
            json={"input": "What does NEBULA-CROWN-7429 require?", "domain_pack": "general"},
        )

    assert uploaded.status_code == 200
    assert all(
        len(limited_candidates.json()[candidate_type]) <= 1
        for candidate_type in ("entities", "relations", "resolutions")
    )
    assert limited_candidates.headers["x-graph-entity-total"] == str(
        len(candidates.json()["entities"])
    )
    assert uploaded.json()["deduplicated"] is False
    assert uploaded.json()["document"]["user_id"] == "local-user"
    assert duplicate.json()["deduplicated"] is True
    assert len(documents.json()) == 1
    assert detail.json()["filename"] == "protocol.md"
    assert retained_content.content == content
    assert retained_content.headers["content-type"].startswith("text/markdown")
    assert retained_content.headers["x-content-type-options"] == "nosniff"
    assert candidates.json()["entities"]
    assert candidates.json()["relations"]
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    citations = run.json()["answer"]["citations"]
    assert any(item["provenance"]["source_type"] == "uploaded_document" for item in citations)
    assert archived.json()["archived"] is True
    assert archived_candidates.json()["relations"][0]["status"] == "archived"
    assert all(
        item["provenance"]["source_type"] != "uploaded_document"
        for item in after_archive.json()["answer"]["citations"]
    )


@pytest.mark.asyncio
async def test_entity_resolution_review_api(tmp_path: Path) -> None:
    components = build_components(
        Settings(app_env="test", data_dir=tmp_path, runtime_mode="offline")
    )
    transport = ASGITransport(app=create_app(components.run_service, components.workspace_service))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/v1/projects/default/documents",
            files={
                "file": (
                    "architecture.md",
                    b"HERMES-CORE-91 uses Qdrant for retrieval.",
                    "text/markdown",
                )
            },
        )
        await client.post(
            "/v1/projects/default/documents",
            files={
                "file": (
                    "operations.md",
                    b"HERMES-CORE-91 supports reviewed operations.",
                    "text/markdown",
                )
            },
        )
        candidates = await client.get("/v1/projects/default/graph/candidates")
        resolution = next(
            item
            for item in candidates.json()["resolutions"]
            if {item["left_name"], item["right_name"]} == {"HERMES-CORE-91"}
        )
        reviewed = await client.post(
            "/v1/projects/default/graph/candidates/resolutions/"
            f"{resolution['candidate_id']}/review",
            json={
                "target_status": "approved",
                "reason": "Both documents use the same stable identifier.",
            },
        )
        overview = await client.get("/v1/workspace/overview")
        first_document_id = first.json()["document"]["document_id"]
        await client.delete(f"/v1/projects/default/documents/{first_document_id}")
        archived = await client.get("/v1/projects/default/graph/candidates?status=archived")

    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "approved"
    assert reviewed.json()["match_strategy"] == "exact_identifier"
    assert overview.json()["counts"]["graph_resolution_candidates"] >= 1
    assert any(
        item["candidate_id"] == resolution["candidate_id"]
        for item in archived.json()["resolutions"]
    )


@pytest.mark.asyncio
async def test_document_api_rejects_oversized_upload_without_parsing(tmp_path: Path) -> None:
    components = build_components(
        Settings(
            app_env="test",
            data_dir=tmp_path,
            runtime_mode="offline",
            max_upload_bytes=1_024,
        )
    )
    transport = ASGITransport(app=create_app(components.run_service, components.workspace_service))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/projects/default/documents",
            files={"file": ("large.txt", b"x" * 1_025, "text/plain")},
        )

    assert response.status_code == 400
    assert "exceeds" in response.json()["detail"]


@pytest.mark.asyncio
async def test_harness_pattern_governance_routes_are_scoped_and_strict(
    tmp_path: Path,
) -> None:
    components = build_components(
        Settings(
            app_env="test",
            data_dir=tmp_path,
            runtime_mode="offline",
        )
    )
    transport = ASGITransport(
        app=create_app(components.run_service, components.workspace_service)
    )
    missing_id = UUID("00000000-0000-0000-0000-000000000999")

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        evaluation = await client.post(
            f"/v1/projects/default/harness/patterns/{missing_id}/evaluate"
        )
        transition = await client.post(
            f"/v1/projects/default/harness/patterns/{missing_id}/transition",
            json={
                "target_status": "canary",
                "human_approved": True,
                "unexpected": "forbidden",
            },
        )
        evidence = await client.get(
            f"/v1/projects/default/harness/patterns/"
            f"{missing_id}/promotion-evidence"
        )

    assert evaluation.status_code == 404
    assert transition.status_code == 422
    assert evidence.status_code == 200
    assert evidence.json() == []
