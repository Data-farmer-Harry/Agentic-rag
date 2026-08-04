from __future__ import annotations

import hmac
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qsl, unquote, urlencode

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings

IdentityRole = Literal["viewer", "member", "owner"]


@dataclass(frozen=True)
class RequestIdentity:
    tenant_id: str
    user_id: str
    role: IdentityRole
    allowed_projects: frozenset[str]
    auth_mode: Literal["local", "bearer"]

    def can_access_project(self, project_id: str) -> bool:
        return not self.allowed_projects or project_id in self.allowed_projects


_identity_context: ContextVar[RequestIdentity | None] = ContextVar(
    "hermesgraph_api_identity",
    default=None,
)
_PROJECT_PATH = re.compile(r"^/v1/projects/([^/]+)(?:/|$)")


class ApiAuthenticator:
    def __init__(self, settings: Settings) -> None:
        self._mode = settings.api_auth_mode
        self._token = (
            settings.api_bearer_token.get_secret_value()
            if settings.api_bearer_token is not None
            else None
        )
        self.identity = RequestIdentity(
            tenant_id=settings.api_tenant_id,
            user_id=settings.api_user_id,
            role=settings.api_identity_role,
            allowed_projects=frozenset(settings.api_allowed_projects),
            auth_mode=settings.api_auth_mode,
        )

    @property
    def mode(self) -> Literal["local", "bearer"]:
        return self._mode

    def authenticate(self, authorization: str) -> RequestIdentity | None:
        if self._mode == "local":
            return self.identity
        scheme, separator, supplied = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not supplied.strip():
            return None
        assert self._token is not None
        if not hmac.compare_digest(supplied.strip(), self._token):
            return None
        return self.identity


class ApiIdentityMiddleware:
    def __init__(self, app: ASGIApp, authenticator: ApiAuthenticator) -> None:
        self._app = app
        self._authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/v1"):
            await self._app(scope, receive, send)
            return
        if str(scope.get("method", "GET")).upper() == "OPTIONS":
            await self._app(scope, receive, send)
            return
        headers = {
            key.lower(): value
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get(b"authorization", b"").decode(
            "latin-1",
            errors="ignore",
        )
        identity = self._authenticator.authenticate(authorization)
        if identity is None:
            await self._reject(
                scope,
                receive,
                send,
                status_code=401,
                code="authentication_required",
                detail="A valid bearer token is required",
                authenticate=True,
            )
            return
        path = str(scope.get("path", ""))
        project_match = _PROJECT_PATH.match(path)
        path_project = unquote(project_match.group(1)) if project_match is not None else None
        query_pairs = parse_qsl(
            bytes(scope.get("query_string", b"")).decode("utf-8", errors="ignore"),
            keep_blank_values=True,
        )
        query_projects = [value for key, value in query_pairs if key == "project_id"]
        requested_projects = [value for value in [path_project, *query_projects] if value]
        if any(not identity.can_access_project(project_id) for project_id in requested_projects):
            await self._reject(
                scope,
                receive,
                send,
                status_code=403,
                code="project_scope_forbidden",
                detail="The authenticated identity cannot access this project",
            )
            return
        supplied_users = [value for key, value in query_pairs if key == "user_id"]
        if any(value != identity.user_id for value in supplied_users):
            await self._reject(
                scope,
                receive,
                send,
                status_code=403,
                code="user_scope_forbidden",
                detail="The requested user does not match the authenticated identity",
            )
            return
        if identity.role == "viewer" and str(scope.get("method", "GET")).upper() not in {
            "GET",
            "HEAD",
        }:
            await self._forbid_role(scope, receive, send)
            return
        if identity.role != "owner" and self._requires_owner(
            str(scope.get("method", "GET")).upper(),
            path,
        ):
            await self._forbid_role(scope, receive, send)
            return
        if not supplied_users:
            query_pairs.append(("user_id", identity.user_id))
        scoped = dict(scope)
        scoped["query_string"] = urlencode(query_pairs, doseq=True).encode("utf-8")
        state = dict(scoped.get("state", {}))
        state["api_identity"] = identity
        scoped["state"] = state
        context_token: Token[RequestIdentity | None] = _identity_context.set(identity)
        try:
            await self._app(scoped, receive, send)
        finally:
            _identity_context.reset(context_token)

    async def _forbid_role(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._reject(
            scope,
            receive,
            send,
            status_code=403,
            code="role_forbidden",
            detail="The authenticated role cannot perform this action",
        )

    @staticmethod
    def _requires_owner(method: str, path: str) -> bool:
        if (
            path == "/v1/workspace/overview"
            or "/fixtures/enterprise/" in path
            or "/enterprise-fixture/" in path
        ):
            return True
        if "/graph/candidates/" in path or "/hermes/native-learning/" in path:
            return method != "GET"
        if "/transitions" in path or path.endswith(("/transition", "/evaluate")):
            return method != "GET"
        if method == "DELETE" and any(
            segment in path for segment in ("/documents/", "/memories/")
        ):
            return True
        if method != "GET" and any(
            segment in path
            for segment in ("/learning-jobs/", "/ingestion-jobs/")
        ):
            return True
        return False

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: str,
        detail: str,
        authenticate: bool = False,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"detail": detail, "code": code},
            headers={"WWW-Authenticate": "Bearer"} if authenticate else None,
        )
        await response(scope, receive, send)


def current_request_identity() -> RequestIdentity:
    identity = _identity_context.get()
    if identity is None:
        raise RuntimeError("API identity is unavailable outside an authenticated request")
    return identity


def bind_user_id(supplied: str | None) -> str:
    identity = current_request_identity()
    if supplied is not None and supplied != identity.user_id:
        raise HTTPException(
            status_code=403,
            detail="The requested user does not match the authenticated identity",
        )
    return identity.user_id


def bind_reviewer_id(supplied: str | None) -> str:
    return bind_user_id(supplied)
