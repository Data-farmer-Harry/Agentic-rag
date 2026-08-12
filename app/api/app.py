from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.agent.adaptive_rag_router import AdaptiveRAGRouterError
from app.agent.hermes_bridge import (
    HermesBridgeError,
    HermesBridgeRunNotFoundError,
    HermesCapabilityBridge,
    HermesNativeToolAudit,
)
from app.agent.hermes_native_learning import (
    HermesNativeLearningConflict,
    HermesNativeLearningUnavailable,
)
from app.agent.hermes_runtime import HermesRuntimeError, HermesRunTimeoutError
from app.api.auth import (
    ApiAuthenticator,
    ApiIdentityMiddleware,
    bind_reviewer_id,
    bind_user_id,
    current_request_identity,
)
from app.api.personal_router import build_personal_router
from app.api.schemas import (
    CreateMemoryRequest,
    EnterpriseFixtureStartRequest,
    FeedbackRequest,
    GraphCandidateReviewRequest,
    HarnessPatternTransitionRequest,
    HermesNativeLearningReviewRequest,
    RunRequest,
    RunResponse,
    RunStartResponse,
    SkillTransitionRequest,
    UpdateConversationRequest,
)
from app.application.run_service import RunService, answer_from_trajectory
from app.application.run_stream import RunStreamCoordinator, public_run_error
from app.application.workspace_service import WorkspaceService
from app.config import Settings
from app.demo.enterprise_fixture import EnterpriseFixtureError
from app.domain.enums import GraphCandidateStatus, SkillStatus
from app.domain.models import (
    GraphEntityCompareRequest,
    GraphEntityResolveRequest,
    GraphRAGRequest,
    GraphSearchRequest,
    KnowledgeSource,
)
from app.graph.graph_candidate_service import GraphCandidateReviewError
from app.harness.models import HarnessPatternStatus
from app.knowledge.ingestion_job_errors import IngestionJobRepositoryError
from app.knowledge.ingestion_jobs import (
    IngestionJobsUnavailableError,
    IngestionJobTransitionError,
    IngestionStagingError,
)
from app.knowledge.knowledge_ingestion import KnowledgeIndexError, KnowledgeIngestionError
from app.learning.job_errors import LearningJobRepositoryError
from app.learning.jobs import (
    LearningJobsUnavailableError,
    LearningJobTransitionError,
)
from app.personal.service import PersonalControlService


def _sse(event: str, data: Any, *, event_id: int | None = None) -> str:
    return (
        (f"id: {event_id}\n" if event_id is not None else "")
        + f"event: {event}\n"
        f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str)}\n\n"
    )


def _parse_knowledge_source(value: str | None) -> KnowledgeSource | None:
    if value is None or not value.strip():
        return None
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict) or set(payload) - {"title"}:
            raise ValueError("Only an upload title may be supplied")
        return KnowledgeSource(title=payload.get("title"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise KnowledgeIngestionError("Invalid knowledge source metadata") from exc


def create_app(
    run_service: RunService,
    workspace: WorkspaceService | None = None,
    *,
    hermes_bridge: HermesCapabilityBridge | None = None,
    personal: PersonalControlService | None = None,
    settings: Settings | None = None,
    frontend_dist: Path | None = None,
    startup_callback: Callable[[], Awaitable[None]] | None = None,
    shutdown_callback: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    api_authenticator = ApiAuthenticator(resolved_settings)
    run_streams = RunStreamCoordinator(
        run_service,
        run_service.event_recorder,
        workspace,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if startup_callback is not None:
            await startup_callback()
        try:
            yield
        finally:
            await run_streams.close()
            if shutdown_callback is not None:
                await shutdown_callback()

    app = FastAPI(title="HermesGraph API", version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(ApiIdentityMiddleware, authenticator=api_authenticator)
    if personal is not None:
        app.include_router(build_personal_router(personal))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "hermesgraph"}

    @app.get("/v1/auth/me")
    async def authenticated_identity() -> dict[str, object]:
        identity = current_request_identity()
        return {
            "auth_mode": identity.auth_mode,
            "tenant_id": identity.tenant_id,
            "user_id": identity.user_id,
            "role": identity.role,
            "allowed_projects": sorted(identity.allowed_projects) or ["*"],
        }

    if hermes_bridge is not None:

        def require_hermes_bridge_auth(request: Request) -> None:
            authorization = request.headers.get("Authorization", "")
            supplied = authorization.removeprefix("Bearer ").strip()
            if not hermes_bridge.is_authorized(supplied):
                raise HTTPException(status_code=401, detail="Invalid bridge credentials")

        @app.get("/internal/hermes/health")
        async def hermes_bridge_health(request: Request) -> dict[str, str]:
            require_hermes_bridge_auth(request)
            return {"status": "ok", "service": "hermesgraph-bridge"}

        @app.post("/internal/hermes/runs/{bridge_id}/tools/{tool_name}")
        async def invoke_hermes_tool(
            bridge_id: str,
            tool_name: str,
            payload: dict[str, Any],
            request: Request,
        ) -> dict[str, Any]:
            require_hermes_bridge_auth(request)
            try:
                return await hermes_bridge.invoke(bridge_id, tool_name, payload)
            except HermesBridgeRunNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (HermesBridgeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.post("/internal/hermes/runs/{bridge_id}/events")
        async def audit_hermes_event(
            bridge_id: str,
            audit: HermesNativeToolAudit,
            request: Request,
        ) -> dict[str, bool]:
            require_hermes_bridge_auth(request)
            try:
                return await hermes_bridge.audit_native_tool(bridge_id, audit)
            except HermesBridgeRunNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/projects/{project_id}/runs", response_model=RunResponse)
    async def create_run(project_id: str, request: RunRequest) -> RunResponse:
        identity = current_request_identity()
        try:
            trajectory = await run_service.run(
                request.input,
                tenant_id=identity.tenant_id,
                project_id=project_id,
                session_id=request.session_id,
                user_id=bind_user_id(request.user_id),
                domain_pack=request.domain_pack,
            )
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AdaptiveRAGRouterError as exc:
            raise HTTPException(
                status_code=503,
                detail="模型路由服务暂时不可用，请稍后重试。",
            ) from exc
        except HermesRunTimeoutError as exc:
            raise HTTPException(status_code=504, detail=public_run_error(exc)) from exc
        except HermesRuntimeError as exc:
            raise HTTPException(status_code=503, detail=public_run_error(exc)) from exc
        return RunResponse(
            run_id=str(trajectory.context.run_id),
            status="completed",
            answer=answer_from_trajectory(trajectory),
        )

    async def start_stream_run(project_id: str, request_body: RunRequest) -> RunStartResponse:
        identity = current_request_identity()
        try:
            result = await run_streams.start(
                request_body.input,
                idempotency_key=request_body.idempotency_key or str(uuid4()),
                tenant_id=identity.tenant_id,
                project_id=project_id,
                session_id=request_body.session_id,
                user_id=bind_user_id(request_body.user_id),
                domain_pack=request_body.domain_pack,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AdaptiveRAGRouterError as exc:
            raise HTTPException(
                status_code=503,
                detail="模型路由服务暂时不可用，请稍后重试。",
            ) from exc
        return RunStartResponse(
            run_id=str(result.run_id),
            status=result.status.value,
            idempotency_key=result.idempotency_key,
            coalesced=result.coalesced,
        )

    def stream_response(events: AsyncIterator[Any]) -> StreamingResponse:
        async def encoded() -> AsyncIterator[str]:
            async for event in events:
                yield _sse(event.event, event.payload, event_id=event.cursor)

        return StreamingResponse(
            encoded(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/v1/projects/{project_id}/runs/start",
        response_model=RunStartResponse,
        status_code=202,
    )
    async def start_run(project_id: str, request_body: RunRequest) -> RunStartResponse:
        return await start_stream_run(project_id, request_body)

    @app.get("/v1/projects/{project_id}/runs/{run_id}/events")
    async def list_run_events(
        project_id: str,
        run_id: UUID,
        after_cursor: int = Query(default=0, ge=0),
        user_id: str | None = None,
    ) -> Any:
        identity = current_request_identity()
        try:
            return await run_streams.list_events(
                run_id,
                after_cursor=after_cursor,
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @app.get("/v1/projects/{project_id}/runs/{run_id}/events/stream")
    async def resume_run_stream(
        project_id: str,
        run_id: UUID,
        after_cursor: int = Query(default=0, ge=0),
        user_id: str | None = None,
    ) -> StreamingResponse:
        identity = current_request_identity()
        try:
            events = run_streams.stream(
                run_id,
                after_cursor=after_cursor,
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
            )
            await run_streams.list_events(
                run_id,
                after_cursor=after_cursor,
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc
        return stream_response(events)

    @app.delete("/v1/projects/{project_id}/runs/{run_id}")
    async def cancel_run(
        project_id: str,
        run_id: UUID,
        user_id: str | None = None,
    ) -> Any:
        identity = current_request_identity()
        try:
            return await run_streams.cancel(
                run_id,
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @app.post("/v1/projects/{project_id}/runs/stream")
    async def stream_run(
        project_id: str,
        request_body: RunRequest,
    ) -> StreamingResponse:
        started = await start_stream_run(project_id, request_body)
        identity = current_request_identity()
        return stream_response(
            run_streams.stream(
                UUID(started.run_id),
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=bind_user_id(request_body.user_id),
            )
        )

    @app.post("/v1/runs/{run_id}/feedback")
    async def add_feedback(run_id: str, request: FeedbackRequest) -> dict[str, str | float]:
        identity = current_request_identity()
        try:
            trajectory = await run_service.feedback(
                run_id,
                request.score,
                request.text,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                allowed_projects=identity.allowed_projects,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"run_id": str(trajectory.context.run_id), "score": request.score}

    if workspace is not None:

        async def _preview_enterprise_fixture(project_id: str) -> Any:
            identity = current_request_identity()
            try:
                return await workspace.preview_enterprise_fixture(
                    tenant_id=identity.tenant_id,
                    project_id=project_id,
                )
            except EnterpriseFixtureError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        async def _start_enterprise_fixture(project_id: str, *, dry_run: bool) -> Any:
            identity = current_request_identity()
            try:
                return await workspace.start_enterprise_fixture(
                    tenant_id=identity.tenant_id,
                    project_id=project_id,
                    requested_by=identity.user_id,
                    dry_run=dry_run,
                )
            except EnterpriseFixtureError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        async def _enterprise_fixture_status(project_id: str, run_id: UUID) -> Any:
            identity = current_request_identity()
            try:
                status = await workspace.enterprise_fixture_status(
                    run_id,
                    tenant_id=identity.tenant_id,
                    project_id=project_id,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if status is None:
                raise HTTPException(status_code=404, detail="Fixture import run not found")
            return status

        async def _reset_enterprise_fixture(project_id: str) -> Any:
            identity = current_request_identity()
            try:
                return await workspace.reset_enterprise_fixture(
                    tenant_id=identity.tenant_id,
                    project_id=project_id,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        @app.get("/v1/workspace/overview")
        async def workspace_overview(
            project_id: str = "default",
            user_id: str | None = None,
        ) -> dict[str, object]:
            identity = current_request_identity()
            return await workspace.overview(
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
            )

        @app.get("/v1/projects/{project_id}/fixtures/enterprise/preview")
        async def preview_enterprise_fixture(project_id: str) -> Any:
            return await _preview_enterprise_fixture(project_id)

        @app.post(
            "/v1/projects/{project_id}/fixtures/enterprise/start",
            status_code=202,
        )
        async def start_enterprise_fixture(
            project_id: str,
            dry_run: bool = False,
        ) -> Any:
            return await _start_enterprise_fixture(project_id, dry_run=dry_run)

        @app.get("/v1/projects/{project_id}/fixtures/enterprise/status/{run_id}")
        async def enterprise_fixture_status(project_id: str, run_id: UUID) -> Any:
            return await _enterprise_fixture_status(project_id, run_id)

        @app.post("/v1/projects/{project_id}/fixtures/enterprise/reset")
        async def reset_enterprise_fixture(project_id: str) -> Any:
            return await _reset_enterprise_fixture(project_id)

        # The /fixtures/enterprise paths above are the canonical API. These
        # aliases preserve the workbench contract already shipped to the UI.
        @app.post(
            "/v1/projects/{project_id}/enterprise-fixture/runs",
            status_code=202,
        )
        async def start_enterprise_fixture_compatibility(
            project_id: str,
            request: EnterpriseFixtureStartRequest,
        ) -> Any:
            return await _start_enterprise_fixture(project_id, dry_run=request.dry_run)

        @app.get("/v1/projects/{project_id}/enterprise-fixture/runs/{run_id}")
        async def enterprise_fixture_status_compatibility(
            project_id: str,
            run_id: UUID,
        ) -> Any:
            return await _enterprise_fixture_status(project_id, run_id)

        @app.get("/v1/projects/{project_id}/runs")
        async def list_runs(project_id: str, limit: int = Query(default=50, ge=1, le=200)) -> Any:
            identity = current_request_identity()
            return await workspace.list_runs(
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=identity.user_id,
                limit=limit,
            )

        @app.get("/v1/projects/{project_id}/conversations")
        async def list_conversations(
            project_id: str,
            user_id: str | None = None,
            limit: int = Query(default=50, ge=1, le=200),
            include_archived: bool = False,
        ) -> Any:
            identity = current_request_identity()
            return await workspace.list_conversations(
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
                limit=limit,
                include_archived=include_archived,
            )

        @app.patch("/v1/projects/{project_id}/conversations/{session_id}")
        async def update_conversation(
            project_id: str,
            session_id: str,
            request: UpdateConversationRequest,
            user_id: str | None = None,
        ) -> Any:
            identity = current_request_identity()
            try:
                return await workspace.update_conversation(
                    session_id,
                    tenant_id=identity.tenant_id,
                    project_id=project_id,
                    user_id=bind_user_id(user_id),
                    title=request.title,
                    archived=request.archived,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        @app.get("/v1/projects/{project_id}/conversations/{session_id}/runs")
        async def list_conversation_runs(
            project_id: str,
            session_id: str,
            user_id: str | None = None,
            limit: int = Query(default=200, ge=1, le=200),
        ) -> Any:
            identity = current_request_identity()
            return await workspace.list_conversation_runs(
                session_id,
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
                limit=limit,
            )

        @app.get("/v1/projects/{project_id}/runs/{run_id}")
        async def get_run(project_id: str, run_id: UUID) -> Any:
            identity = current_request_identity()
            run = await workspace.get_run(
                run_id,
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=identity.user_id,
            )
            if run is None:
                raise HTTPException(status_code=404, detail="Run not found")
            return run

        @app.get("/v1/projects/{project_id}/memories")
        async def list_memories(
            project_id: str,
            user_id: str | None = None,
            include_revoked: bool = False,
        ) -> Any:
            identity = current_request_identity()
            return await workspace.list_memories(
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
                include_revoked=include_revoked,
            )

        @app.post("/v1/projects/{project_id}/memories")
        async def create_memory(
            project_id: str,
            request: CreateMemoryRequest,
            user_id: str | None = None,
        ) -> Any:
            identity = current_request_identity()
            try:
                return await workspace.remember(
                    request.summary,
                    tenant_id=identity.tenant_id,
                    project_id=project_id,
                    user_id=bind_user_id(user_id),
                    memory_type=request.memory_type,
                    source_session_id=request.source_session_id,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        @app.delete("/v1/projects/{project_id}/memories/{memory_id}")
        async def revoke_memory(
            project_id: str,
            memory_id: UUID,
            user_id: str | None = None,
        ) -> dict[str, object]:
            identity = current_request_identity()
            revoked = await workspace.revoke_memory(
                memory_id,
                tenant_id=identity.tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
            )
            if not revoked:
                raise HTTPException(status_code=404, detail="Memory not found")
            return {"memory_id": str(memory_id), "revoked": True}

        @app.get("/v1/projects/{project_id}/skills")
        async def list_skills(
            project_id: str,
            status: SkillStatus | None = None,
        ) -> Any:
            return await workspace.list_skills(
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                status=status,
            )

        @app.get("/v1/projects/{project_id}/skill-evolution")
        async def list_skill_evolution(project_id: str) -> Any:
            return await workspace.list_skill_evolution(
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
            )

        @app.get("/v1/projects/{project_id}/skills/{skill_id}/evaluations")
        async def list_skill_evaluations(
            project_id: str,
            skill_id: UUID,
            skill_version: str | None = None,
        ) -> Any:
            try:
                return await workspace.list_skill_evaluations(
                    skill_id,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    skill_version=skill_version,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.get("/v1/projects/{project_id}/skills/{skill_id}/transitions")
        async def list_skill_transitions(
            project_id: str,
            skill_id: UUID,
            skill_version: str | None = None,
        ) -> Any:
            try:
                return await workspace.list_skill_transitions(
                    skill_id,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    skill_version=skill_version,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.post("/v1/projects/{project_id}/skills/{skill_id}/evaluate")
        async def evaluate_skill(
            project_id: str,
            skill_id: UUID,
            skill_version: str | None = None,
        ) -> Any:
            try:
                return await workspace.evaluate_skill(
                    skill_id,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    skill_version=skill_version,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.post("/v1/projects/{project_id}/skills/{skill_id}/transition")
        async def transition_skill(
            project_id: str,
            skill_id: UUID,
            transition: SkillTransitionRequest,
            skill_version: str | None = None,
        ) -> Any:
            try:
                return await workspace.transition_skill(
                    skill_id,
                    transition.target_status,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    skill_version=skill_version,
                    human_approved=transition.human_approved,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.get("/v1/projects/{project_id}/learning-changes")
        async def list_learning_changes(project_id: str) -> Any:
            return await workspace.list_change_sets(
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
            )

        @app.get("/v1/projects/{project_id}/hermes/native-learning")
        async def list_hermes_native_learning(project_id: str) -> Any:
            return await workspace.list_hermes_native_learning(
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
            )

        @app.get("/v1/hermes/native-learning/health")
        async def hermes_native_learning_health() -> Any:
            try:
                return await workspace.hermes_native_learning_health()
            except HermesNativeLearningUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        @app.post("/v1/projects/{project_id}/hermes/native-learning/{change_set_id}/review")
        async def review_hermes_native_learning(
            project_id: str,
            change_set_id: UUID,
            review: HermesNativeLearningReviewRequest,
        ) -> Any:
            try:
                return await workspace.review_hermes_native_learning(
                    change_set_id,
                    review.decision,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    reviewer_id=bind_reviewer_id(review.reviewer_id),
                    reason=review.reason,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except HermesNativeLearningConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except HermesNativeLearningUnavailable as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        @app.post("/v1/projects/{project_id}/graph/search")
        async def graph_search(
            project_id: str,
            graph_request: GraphSearchRequest,
            user_id: str | None = None,
        ) -> Any:
            try:
                return await workspace.graph_search(
                    graph_request,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    user_id=bind_user_id(user_id),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail="Graph service unavailable") from exc

        @app.post("/v1/projects/{project_id}/graph/entities/resolve")
        async def resolve_graph_entities(
            project_id: str,
            request: GraphEntityResolveRequest,
            user_id: str | None = None,
        ) -> Any:
            try:
                return await workspace.resolve_graph_entities(
                    request,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    user_id=bind_user_id(user_id),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail="Graph service unavailable") from exc

        @app.post("/v1/projects/{project_id}/graph/retrieve")
        async def retrieve_evidence_subgraph(
            project_id: str,
            request: GraphRAGRequest,
            user_id: str | None = None,
        ) -> Any:
            try:
                return await workspace.retrieve_evidence_subgraph(
                    request,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    user_id=bind_user_id(user_id),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail="Graph service unavailable") from exc

        @app.post("/v1/projects/{project_id}/graph/compare")
        async def compare_graph_entities(
            project_id: str,
            request: GraphEntityCompareRequest,
            user_id: str | None = None,
        ) -> Any:
            try:
                return await workspace.compare_graph_entities(
                    request,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    user_id=bind_user_id(user_id),
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail="Graph service unavailable") from exc

        @app.get("/v1/projects/{project_id}/graph/candidates")
        async def list_graph_candidates(
            project_id: str,
            response: Response,
            document_id: UUID | None = None,
            status: GraphCandidateStatus | None = None,
            limit: int = Query(default=500, ge=1, le=5_000),
        ) -> Any:
            candidates = await workspace.list_graph_candidates(
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                document_id=document_id,
                status=status,
            )
            response.headers["X-Graph-Entity-Total"] = str(len(candidates.entities))
            response.headers["X-Graph-Relation-Total"] = str(len(candidates.relations))
            response.headers["X-Graph-Resolution-Total"] = str(
                len(candidates.resolutions)
            )
            response.headers["X-Graph-Result-Limit"] = str(limit)
            return candidates.model_copy(
                update={
                    "entities": candidates.entities[:limit],
                    "relations": candidates.relations[:limit],
                    "resolutions": candidates.resolutions[:limit],
                }
            )

        @app.post("/v1/projects/{project_id}/graph/candidates/entities/{candidate_id}/review")
        async def review_graph_entity(
            project_id: str,
            candidate_id: UUID,
            review: GraphCandidateReviewRequest,
        ) -> Any:
            try:
                return await workspace.review_graph_entity(
                    candidate_id,
                    review.target_status,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    reviewer_id=bind_reviewer_id(review.reviewer_id),
                    reason=review.reason,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except GraphCandidateReviewError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.post("/v1/projects/{project_id}/graph/candidates/relations/{candidate_id}/review")
        async def review_graph_relation(
            project_id: str,
            candidate_id: UUID,
            review: GraphCandidateReviewRequest,
        ) -> Any:
            try:
                return await workspace.review_graph_relation(
                    candidate_id,
                    review.target_status,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    reviewer_id=bind_reviewer_id(review.reviewer_id),
                    reason=review.reason,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except GraphCandidateReviewError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.post("/v1/projects/{project_id}/graph/candidates/resolutions/{candidate_id}/review")
        async def review_entity_resolution(
            project_id: str,
            candidate_id: UUID,
            review: GraphCandidateReviewRequest,
        ) -> Any:
            try:
                return await workspace.review_entity_resolution(
                    candidate_id,
                    review.target_status,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    reviewer_id=bind_reviewer_id(review.reviewer_id),
                    reason=review.reason,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except GraphCandidateReviewError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @app.get("/v1/capabilities")
        async def list_capabilities() -> Any:
            return workspace.capabilities()

        @app.post("/v1/projects/{project_id}/documents")
        async def upload_document(
            project_id: str,
            file: Annotated[UploadFile, File()],
            user_id: Annotated[str | None, Form()] = None,
            source: Annotated[str | None, Form()] = None,
        ) -> Any:
            try:
                content = await file.read(workspace.max_upload_bytes + 1)
                return await workspace.ingest_document(
                    filename=file.filename or "upload.txt",
                    content=content,
                    media_type=file.content_type,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    user_id=bind_user_id(user_id),
                    source=_parse_knowledge_source(source),
                )
            except KnowledgeIndexError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except KnowledgeIngestionError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            finally:
                await file.close()

        @app.post("/v1/projects/{project_id}/ingestion-jobs", status_code=202)
        async def submit_ingestion_job(
            project_id: str,
            file: Annotated[UploadFile, File()],
            user_id: Annotated[str | None, Form()] = None,
            source: Annotated[str | None, Form()] = None,
        ) -> Any:
            try:
                content = await file.read(workspace.max_upload_bytes + 1)
                return await workspace.submit_ingestion_job(
                    filename=file.filename or "upload.txt",
                    content=content,
                    media_type=file.content_type,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    user_id=bind_user_id(user_id),
                    source=_parse_knowledge_source(source),
                )
            except (KnowledgeIngestionError, IngestionStagingError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except IngestionJobsUnavailableError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except IngestionJobRepositoryError as exc:
                raise HTTPException(
                    status_code=503, detail="Ingestion control plane is unavailable"
                ) from exc
            finally:
                await file.close()

        @app.get("/v1/projects/{project_id}/ingestion-jobs")
        async def list_ingestion_jobs(
            project_id: str,
            limit: int = Query(default=100, ge=1, le=500),
        ) -> Any:
            try:
                return await workspace.list_ingestion_jobs(
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    limit=limit,
                )
            except IngestionJobsUnavailableError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except IngestionJobRepositoryError as exc:
                raise HTTPException(
                    status_code=503, detail="Ingestion control plane is unavailable"
                ) from exc

        @app.get("/v1/projects/{project_id}/ingestion-jobs/{job_id}")
        async def get_ingestion_job(project_id: str, job_id: UUID) -> Any:
            try:
                job = await workspace.get_ingestion_job(
                    job_id,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                )
            except IngestionJobsUnavailableError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except IngestionJobRepositoryError as exc:
                raise HTTPException(
                    status_code=503, detail="Ingestion control plane is unavailable"
                ) from exc
            if job is None:
                raise HTTPException(status_code=404, detail="Ingestion job not found")
            return job

        @app.delete("/v1/projects/{project_id}/ingestion-jobs/{job_id}")
        async def cancel_ingestion_job(project_id: str, job_id: UUID) -> Any:
            try:
                return await workspace.cancel_ingestion_job(
                    job_id,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (IngestionJobTransitionError, IngestionJobsUnavailableError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except IngestionJobRepositoryError as exc:
                raise HTTPException(
                    status_code=503, detail="Ingestion control plane is unavailable"
                ) from exc

        @app.post("/v1/projects/{project_id}/ingestion-jobs/{job_id}/retry")
        async def retry_ingestion_job(project_id: str, job_id: UUID) -> Any:
            try:
                return await workspace.retry_ingestion_job(
                    job_id,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (IngestionJobTransitionError, IngestionJobsUnavailableError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except IngestionJobRepositoryError as exc:
                raise HTTPException(
                    status_code=503, detail="Ingestion control plane is unavailable"
                ) from exc

        @app.get("/v1/projects/{project_id}/learning-jobs")
        async def list_learning_jobs(
            project_id: str,
            limit: int = Query(default=100, ge=1, le=500),
        ) -> Any:
            try:
                return await workspace.list_learning_jobs(
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    limit=limit,
                )
            except LearningJobsUnavailableError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except LearningJobRepositoryError as exc:
                raise HTTPException(
                    status_code=503, detail="Learning control plane is unavailable"
                ) from exc

        @app.get("/v1/projects/{project_id}/harness/experiences")
        async def list_harness_experiences(
            project_id: str,
            limit: int = Query(default=100, ge=1, le=500),
            learnable: bool | None = None,
            success: bool | None = None,
        ) -> Any:
            return await workspace.list_harness_experiences(
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                limit=limit,
                learnable=learnable,
                success=success,
            )

        @app.get("/v1/projects/{project_id}/harness/effectiveness")
        async def get_self_learning_effectiveness(
            project_id: str,
            limit: int = Query(default=500, ge=1, le=500),
            minimum_experiences: int = Query(default=20, ge=1, le=500),
            minimum_feedback: int = Query(default=5, ge=0, le=500),
        ) -> Any:
            return await workspace.self_learning_effectiveness(
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                limit=limit,
                minimum_experiences=minimum_experiences,
                minimum_feedback=minimum_feedback,
            )

        @app.get(
            "/v1/projects/{project_id}/harness/experiences/{experience_id}"
        )
        async def get_harness_experience(
            project_id: str,
            experience_id: UUID,
        ) -> Any:
            experience = await workspace.get_harness_experience(
                experience_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
            )
            if experience is None:
                raise HTTPException(status_code=404, detail="Harness experience not found")
            return experience

        @app.get(
            "/v1/projects/{project_id}/harness/experiences/"
            "{experience_id}/evaluations"
        )
        async def list_harness_experience_evaluations(
            project_id: str,
            experience_id: UUID,
        ) -> Any:
            experience = await workspace.get_harness_experience(
                experience_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
            )
            if experience is None:
                raise HTTPException(status_code=404, detail="Harness experience not found")
            return await workspace.list_harness_experience_evaluations(
                experience_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
            )

        @app.get("/v1/projects/{project_id}/harness/patterns")
        async def list_harness_patterns(
            project_id: str,
            limit: int = Query(default=100, ge=1, le=500),
            status: HarnessPatternStatus | None = None,
        ) -> Any:
            return await workspace.list_harness_patterns(
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                limit=limit,
                status=status,
            )

        @app.get("/v1/projects/{project_id}/harness/patterns/{pattern_id}")
        async def get_harness_pattern(
            project_id: str,
            pattern_id: UUID,
            version: str | None = None,
        ) -> Any:
            pattern = await workspace.get_harness_pattern(
                pattern_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                version=version,
            )
            if pattern is None:
                raise HTTPException(status_code=404, detail="Harness pattern not found")
            return pattern

        @app.post(
            "/v1/projects/{project_id}/harness/patterns/{pattern_id}/evaluate"
        )
        async def evaluate_harness_pattern(
            project_id: str,
            pattern_id: UUID,
            version: str | None = None,
        ) -> Any:
            try:
                return await workspace.evaluate_harness_pattern(
                    pattern_id,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    pattern_version=version,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.post(
            "/v1/projects/{project_id}/harness/patterns/{pattern_id}/transition"
        )
        async def transition_harness_pattern(
            project_id: str,
            pattern_id: UUID,
            transition: HarnessPatternTransitionRequest,
            version: str | None = None,
        ) -> Any:
            try:
                return await workspace.transition_harness_pattern(
                    pattern_id,
                    transition.target_status,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                    pattern_version=version,
                    human_approved=transition.human_approved,
                    expected_from_status=transition.expected_from_status,
                    reason=transition.reason,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        @app.get(
            "/v1/projects/{project_id}/harness/patterns/{pattern_id}/evaluations"
        )
        async def list_harness_pattern_evaluations(
            project_id: str,
            pattern_id: UUID,
            version: str | None = None,
        ) -> Any:
            return await workspace.list_harness_pattern_evaluations(
                pattern_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                pattern_version=version,
            )

        @app.get(
            "/v1/projects/{project_id}/harness/patterns/"
            "{pattern_id}/promotion-evidence"
        )
        async def list_harness_pattern_promotion_evidence(
            project_id: str,
            pattern_id: UUID,
            version: str | None = None,
        ) -> Any:
            return await workspace.list_harness_pattern_promotion_evidence(
                pattern_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                pattern_version=version,
            )

        @app.get(
            "/v1/projects/{project_id}/harness/patterns/{pattern_id}/transitions"
        )
        async def list_harness_pattern_transitions(
            project_id: str,
            pattern_id: UUID,
            version: str | None = None,
        ) -> Any:
            return await workspace.list_harness_pattern_transitions(
                pattern_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                pattern_version=version,
            )

        @app.get("/v1/projects/{project_id}/runs/{run_id}/harness-overlay")
        async def get_run_harness_overlay(
            project_id: str,
            run_id: UUID,
        ) -> Any:
            overlay = await workspace.get_harness_overlay(
                run_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
            )
            if overlay is None:
                raise HTTPException(status_code=404, detail="Harness overlay not found")
            return overlay

        @app.get("/v1/projects/{project_id}/learning-jobs/{job_id}")
        async def get_learning_job(project_id: str, job_id: UUID) -> Any:
            try:
                job = await workspace.get_learning_job(
                    job_id,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                )
            except LearningJobsUnavailableError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except LearningJobRepositoryError as exc:
                raise HTTPException(
                    status_code=503, detail="Learning control plane is unavailable"
                ) from exc
            if job is None:
                raise HTTPException(status_code=404, detail="Learning job not found")
            return job

        @app.delete("/v1/projects/{project_id}/learning-jobs/{job_id}")
        async def cancel_learning_job(project_id: str, job_id: UUID) -> Any:
            try:
                return await workspace.cancel_learning_job(
                    job_id,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (LearningJobTransitionError, LearningJobsUnavailableError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except LearningJobRepositoryError as exc:
                raise HTTPException(
                    status_code=503, detail="Learning control plane is unavailable"
                ) from exc

        @app.post("/v1/projects/{project_id}/learning-jobs/{job_id}/retry")
        async def retry_learning_job(project_id: str, job_id: UUID) -> Any:
            try:
                return await workspace.retry_learning_job(
                    job_id,
                    tenant_id=current_request_identity().tenant_id,
                    project_id=project_id,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except (LearningJobTransitionError, LearningJobsUnavailableError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except LearningJobRepositoryError as exc:
                raise HTTPException(
                    status_code=503, detail="Learning control plane is unavailable"
                ) from exc

        @app.get("/v1/projects/{project_id}/documents")
        async def list_documents(
            project_id: str,
            include_archived: bool = False,
            user_id: str | None = None,
        ) -> Any:
            return await workspace.list_documents(
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
                include_archived=include_archived,
            )

        @app.get("/v1/projects/{project_id}/documents/{document_id}")
        async def get_document(
            project_id: str,
            document_id: UUID,
            user_id: str | None = None,
        ) -> Any:
            document = await workspace.get_document(
                document_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
            )
            if document is None:
                raise HTTPException(status_code=404, detail="Document not found")
            return document

        @app.get("/v1/projects/{project_id}/documents/{document_id}/content")
        async def get_document_content(
            project_id: str,
            document_id: UUID,
            user_id: str | None = None,
        ) -> Response:
            result = await workspace.get_document_content(
                document_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
            )
            if result is None:
                raise HTTPException(status_code=404, detail="Document not found")
            document, content = result
            return Response(
                content=content,
                media_type=document.media_type,
                headers={
                    "Cache-Control": "private, max-age=300",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        @app.delete("/v1/projects/{project_id}/documents/{document_id}")
        async def archive_document(
            project_id: str,
            document_id: UUID,
            user_id: str | None = None,
        ) -> dict[str, object]:
            try:
                archived = await workspace.archive_document(
                    document_id,
                tenant_id=current_request_identity().tenant_id,
                project_id=project_id,
                user_id=bind_user_id(user_id),
                )
            except KnowledgeIndexError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            if not archived:
                raise HTTPException(status_code=404, detail="Document not found")
            return {"document_id": str(document_id), "archived": True}

    resolved_frontend = frontend_dist or (Path(__file__).resolve().parents[2] / "frontend" / "dist")
    if resolved_frontend.exists():
        assets = resolved_frontend / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

        @app.get("/", include_in_schema=False)
        async def frontend_index() -> FileResponse:
            return FileResponse(resolved_frontend / "index.html")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def frontend_fallback(full_path: str) -> FileResponse:
            requested = (resolved_frontend / full_path).resolve()
            if requested.is_relative_to(resolved_frontend.resolve()) and requested.is_file():
                return FileResponse(requested)
            return FileResponse(resolved_frontend / "index.html")

    return app
