import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agent.conversation_router import ConversationTurn
from app.agent.hermes_bridge import (
    HermesAnswerNotPublishedError,
    HermesCapabilityBridge,
    HermesNativeToolAudit,
)
from app.agent.hermes_runtime import HermesAgentRuntime
from app.bootstrap import build_components
from app.config import Settings
from app.domain.enums import EvidenceLevel
from app.domain.models import RetrievalBundle, RunContext


class EmptyRetrieval:
    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> RetrievalBundle:
        del context, filters, top_k
        return RetrievalBundle(query=query)


def settings() -> Settings:
    return Settings(
        app_env="test",
        runtime_mode="hermes",
        hermes_api_url="http://hermes.test",
        hermes_api_key="api-secret",
        hermes_bridge_token="bridge-secret",
        hermes_native_admin_token="native-admin-secret",
    )


@pytest.mark.asyncio
async def test_runtime_correlates_task_and_requires_published_artifact() -> None:
    resolved = settings()
    bridge = HermesCapabilityBridge(settings=resolved, retrieval=EmptyRetrieval())
    observed: dict[str, Any] = {}
    requested_paths: list[str] = []
    release_terminal = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/runs" and request.method == "POST":
            body = __import__("json").loads(request.content)
            observed.update(body)
            observed["session_key"] = request.headers["X-Hermes-Session-Key"]
            await bridge.invoke(
                body["session_id"],
                "hermesgraph_publish_answer",
                {
                    "answer_markdown": "No connected evidence.",
                    "confidence": "insufficient",
                },
            )
            return httpx.Response(202, json={"run_id": "run_123", "status": "started"})
        if request.url.path == "/v1/runs/run_123/events":
            await release_terminal.wait()
            return httpx.Response(
                200,
                text='data: {"event":"run.completed","run_id":"run_123"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        if request.url.path == "/v1/runs/run_123":
            return httpx.Response(200, json={"run_id": "run_123", "status": "completed"})
        if request.url.path == "/v1/runs/run_123/stop":
            return httpx.Response(200, json={"status": "stopping"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(
        base_url="http://hermes.test",
        transport=httpx.MockTransport(handler),
    )
    async def capsule_provider(context: RunContext, query: str) -> str:
        assert context.session_id == "personal"
        assert query == "hello"
        return '<skill_index>[{"name":"learned_lookup","version":"1.0.0"}]</skill_index>'

    async def history_provider(
        context: RunContext,
    ) -> tuple[ConversationTurn, ...]:
        assert context.session_id == "personal"
        return (
            ConversationTurn(
                user_input="My preferred language is Chinese.",
                assistant_answer="我会记住这个偏好。",
            ),
        )

    runtime = HermesAgentRuntime(
        settings=resolved,
        bridge=bridge,
        capsule_provider=capsule_provider,
        history_provider=history_provider,
        client=client,
    )

    await runtime.start()
    answer = await runtime.run("hello", RunContext(session_id="personal"))
    assert "/v1/runs/run_123/stop" not in requested_paths
    assert "/v1/runs/run_123" not in requested_paths
    release_terminal.set()
    for _ in range(100):
        if "/v1/runs/run_123" in requested_paths:
            break
        await asyncio.sleep(0.01)
    await bridge.audit_native_tool(
        observed["session_id"],
        HermesNativeToolAudit(
            tool_name="hermes_background_review_completed",
            status="ok",
        ),
    )
    await runtime.close()
    await client.aclose()

    assert answer.confidence == EvidenceLevel.INSUFFICIENT
    assert observed["session_id"].startswith("hg_")
    assert observed["session_key"].startswith("hermesgraph-")
    assert observed["input"] == "hello"
    assert "hermesgraph_publish_answer` exactly once" in observed["instructions"]
    assert "learned_lookup" in observed["instructions"]
    assert observed["conversation_history"] == [
        {"role": "user", "content": "My preferred language is Chinese."},
        {"role": "assistant", "content": "我会记住这个偏好。"},
    ]
    assert "/v1/runs/run_123" in requested_paths
    assert "/v1/runs/run_123/stop" not in requested_paths


@pytest.mark.asyncio
async def test_runtime_fails_closed_when_hermes_does_not_publish() -> None:
    resolved = settings()
    bridge = HermesCapabilityBridge(settings=resolved, retrieval=EmptyRetrieval())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/runs" and request.method == "POST":
            return httpx.Response(202, json={"run_id": "run_unpublished"})
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                text='data: {"event":"run.completed"}\n\n',
                headers={"content-type": "text/event-stream"},
            )
        if request.url.path == "/v1/runs/run_unpublished":
            return httpx.Response(200, json={"status": "completed"})
        if request.url.path.endswith("/stop"):
            return httpx.Response(200, json={"status": "stopping"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(
        base_url="http://hermes.test",
        transport=httpx.MockTransport(handler),
    )
    runtime = HermesAgentRuntime(settings=resolved, bridge=bridge, client=client)

    with pytest.raises(HermesAnswerNotPublishedError):
        await runtime.run("hello", RunContext())
    await client.aclose()


@pytest.mark.asyncio
async def test_bootstrap_selects_hermes_as_the_only_online_runtime(
    tmp_path: Path,
) -> None:
    hermes_components = build_components(
        Settings(
            app_env="test",
            data_dir=tmp_path / "hermes",
            runtime_mode="hermes",
            hermes_api_key="api-secret",
            hermes_bridge_token="bridge-secret",
            hermes_native_admin_token="native-admin-secret",
            embedding_provider="deterministic",
        )
    )
    assert hermes_components.hermes_bridge is not None
    assert any(
        isinstance(resource, HermesAgentRuntime)
        for resource in hermes_components.lifecycle_resources
    )
    await hermes_components.close()
