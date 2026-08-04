from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.app import create_app
from app.application.run_service import RunService
from app.config import Settings
from app.domain.enums import EvidenceLevel, RunStatus
from app.domain.models import AnswerResponse, RunContext, RunTrajectory
from app.infra.local_repositories import JsonlTrajectoryRepository

TOKEN = "test-bearer-token-with-at-least-32-characters"


class _EchoRuntime:
    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        return AnswerResponse(
            answer_markdown=user_input,
            confidence=EvidenceLevel.INSUFFICIENT,
        )


def _settings(tmp_path: Path, *, role: str = "owner") -> Settings:
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        runtime_mode="offline",
        api_auth_mode="bearer",
        api_bearer_token=TOKEN,
        api_tenant_id="tenant-a",
        api_user_id="user-a",
        api_allowed_projects=["project-a"],
        api_identity_role=role,
    )


def _service(tmp_path: Path, settings: Settings) -> tuple[RunService, JsonlTrajectoryRepository]:
    repository = JsonlTrajectoryRepository(tmp_path / "runs.jsonl")
    service = RunService(
        runtime=_EchoRuntime(),
        trajectories=repository,
        settings=settings,
    )
    return service, repository


@pytest.mark.asyncio
async def test_bearer_auth_requires_exact_token_and_exposes_bound_identity(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service, _ = _service(tmp_path, settings)
    transport = ASGITransport(app=create_app(service, settings=settings))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/v1/auth/me")
        wrong = await client.get(
            "/v1/auth/me",
            headers={"Authorization": "Bearer wrong-token"},
        )
        authenticated = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json()["code"] == "authentication_required"
    assert wrong.status_code == 401
    assert authenticated.json() == {
        "auth_mode": "bearer",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "role": "owner",
        "allowed_projects": ["project-a"],
    }


@pytest.mark.asyncio
async def test_server_binds_run_scope_and_rejects_project_or_user_impersonation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    service, repository = _service(tmp_path, settings)
    transport = ASGITransport(app=create_app(service, settings=settings))
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/projects/project-a/runs",
            headers=headers,
            json={"input": "bound identity"},
        )
        wrong_project = await client.post(
            "/v1/projects/project-b/runs",
            headers=headers,
            json={"input": "forbidden project"},
        )
        wrong_query_user = await client.get(
            "/v1/projects/project-a/runs/00000000-0000-0000-0000-000000000001/events",
            headers=headers,
            params={"user_id": "user-b"},
        )
        wrong_body_user = await client.post(
            "/v1/projects/project-a/runs",
            headers=headers,
            json={"input": "forbidden user", "user_id": "user-b"},
        )
        foreign_run = RunTrajectory(
            context=RunContext(
                tenant_id="tenant-a",
                project_id="project-a",
                user_id="user-b",
            ),
            user_input="private run",
            answer=AnswerResponse(
                answer_markdown="private answer",
                confidence=EvidenceLevel.SUPPORTED,
            ),
            status=RunStatus.COMPLETED,
        )
        await repository.save(foreign_run)
        foreign_feedback = await client.post(
            f"/v1/runs/{foreign_run.context.run_id}/feedback",
            headers=headers,
            json={"score": 1},
        )

    saved = await repository.list_recent(tenant_id="tenant-a", project_id="project-a")
    bound_run = next(item for item in saved if item.user_input == "bound identity")
    assert created.status_code == 200
    assert bound_run.context.tenant_id == "tenant-a"
    assert bound_run.context.project_id == "project-a"
    assert bound_run.context.user_id == "user-a"
    assert wrong_project.status_code == 403
    assert wrong_project.json()["code"] == "project_scope_forbidden"
    assert wrong_query_user.status_code == 403
    assert wrong_query_user.json()["code"] == "user_scope_forbidden"
    assert wrong_body_user.status_code == 403
    assert foreign_feedback.status_code == 404


@pytest.mark.asyncio
async def test_api_roles_restrict_mutation_and_owner_control_plane_actions(
    tmp_path: Path,
) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    viewer_settings = _settings(tmp_path / "viewer", role="viewer")
    viewer_service, _ = _service(tmp_path / "viewer", viewer_settings)
    viewer_app = create_app(viewer_service, settings=viewer_settings)
    member_settings = _settings(tmp_path / "member", role="member")
    member_service, _ = _service(tmp_path / "member", member_settings)
    member_app = create_app(member_service, settings=member_settings)

    async with AsyncClient(
        transport=ASGITransport(app=viewer_app), base_url="http://test"
    ) as client:
        viewer_write = await client.post(
            "/v1/projects/project-a/runs",
            headers=headers,
            json={"input": "write"},
        )
    async with AsyncClient(
        transport=ASGITransport(app=member_app), base_url="http://test"
    ) as client:
        member_run = await client.post(
            "/v1/projects/project-a/runs",
            headers=headers,
            json={"input": "ordinary work"},
        )
        member_review = await client.post(
            "/v1/projects/project-a/graph/candidates/entities/"
            "00000000-0000-0000-0000-000000000001/review",
            headers=headers,
            json={"target_status": "approved"},
        )

    assert viewer_write.status_code == 403
    assert viewer_write.json()["code"] == "role_forbidden"
    assert member_run.status_code == 200
    assert member_review.status_code == 403
    assert member_review.json()["code"] == "role_forbidden"


def test_production_rejects_anonymous_api_mode(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="production require API_AUTH_MODE=bearer"):
        Settings(app_env="production", data_dir=tmp_path, runtime_mode="offline")
