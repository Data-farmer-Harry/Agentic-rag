from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from typing import Any

import httpx

from app.agent.adaptive_rag_router import ConversationHistoryProvider, ConversationTurn
from app.agent.context_engine import RuntimeCapsule
from app.agent.hermes_bridge import HermesCapabilityBridge
from app.agent.instructions import load_hermes_instructions
from app.config import Settings
from app.domain.models import AdaptiveRAGRoute, AnswerResponse, RunContext
from app.domain_packs.registry import DomainPackRegistry
from app.retrieval.knowledge_query_router import (
    KnowledgeQueryRouteDecision,
    adaptive_route_instruction,
    route_knowledge_query,
)


class HermesRuntimeError(RuntimeError):
    pass


ContextCapsuleProvider = Callable[
    [RunContext, str],
    Awaitable[str | RuntimeCapsule],
]
logger = logging.getLogger(__name__)


class HermesAgentRuntime:
    """AgentRuntime adapter for a pinned Hermes Agent API sidecar."""

    def __init__(
        self,
        *,
        settings: Settings,
        bridge: HermesCapabilityBridge,
        domain_packs: DomainPackRegistry | None = None,
        capsule_provider: ContextCapsuleProvider | None = None,
        history_provider: ConversationHistoryProvider | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._bridge = bridge
        self._domain_packs = domain_packs or DomainPackRegistry()
        self._capsule_provider = capsule_provider
        self._history_provider = history_provider
        self._background_finalizers: set[asyncio.Task[None]] = set()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.hermes_api_url.rstrip("/"),
            timeout=httpx.Timeout(settings.agent_timeout_seconds, connect=10.0),
        )

    async def start(self) -> None:
        response = await self._client.get("/health", headers=self._auth_headers())
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") not in {"ok", "healthy"}:
            raise HermesRuntimeError(f"Hermes sidecar is unhealthy: {payload}")

    async def close(self) -> None:
        if self._background_finalizers:
            done, pending = await asyncio.wait(
                self._background_finalizers,
                timeout=self._settings.hermes_shutdown_grace_seconds,
            )
            del done
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self._owns_client:
            await self._client.aclose()

    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        history: Sequence[ConversationTurn] = ()
        if self._history_provider is not None:
            try:
                history = await self._history_provider(context)
            except Exception:
                logger.warning("Hermes conversation history could not be loaded")
        raw_capsule = (
            await self._capsule_provider(context, user_input)
            if self._capsule_provider is not None
            else ""
        )
        capsule = raw_capsule.text if isinstance(raw_capsule, RuntimeCapsule) else raw_capsule
        capsule_memories = raw_capsule.memories if isinstance(raw_capsule, RuntimeCapsule) else ()
        context_trace = raw_capsule.trace if isinstance(raw_capsule, RuntimeCapsule) else None
        bridge_id = await self._bridge.open_run(
            context,
            allowed_memories=capsule_memories,
        )
        retrieval_route = context.adaptive_rag_route or route_knowledge_query(user_input)
        run_id: str | None = None
        try:
            response = await self._client.post(
                "/v1/runs",
                headers={
                    **self._auth_headers(),
                    "X-Hermes-Session-Key": self._memory_scope(context),
                },
                json={
                    "input": user_input,
                    "instructions": self._instructions(
                        context,
                        capsule,
                        retrieval_route,
                    ),
                    "session_id": bridge_id,
                    "model": self._settings.openai_model,
                    "conversation_history": self._serialize_history(history),
                },
            )
            response.raise_for_status()
            payload = response.json()
            run_id = str(payload.get("run_id") or "")
            if not run_id:
                raise HermesRuntimeError("Hermes did not return a run_id")

            answer = await self._wait_for_answer_or_terminal(run_id, bridge_id)
            return answer.model_copy(update={"context_trace": context_trace})
        except asyncio.CancelledError:
            if run_id:
                await self._stop_run(run_id)
            await self._bridge.discard(bridge_id)
            raise
        except Exception:
            if run_id:
                await self._stop_run(run_id)
            await self._bridge.discard(bridge_id)
            raise

    async def _wait_for_answer_or_terminal(
        self,
        run_id: str,
        bridge_id: str,
    ) -> AnswerResponse:
        published_task = asyncio.create_task(
            self._bridge.wait_for_published_answer(bridge_id)
        )
        active_terminal_task = asyncio.create_task(
            self._wait_for_terminal_event(run_id)
        )
        terminal_task: asyncio.Task[None] | None = active_terminal_task
        wait_tasks: set[asyncio.Future[Any]] = {
            published_task,
            active_terminal_task,
        }
        try:
            done, _ = await asyncio.wait(
                wait_tasks,
                timeout=self._settings.agent_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError
            if published_task in done:
                answer = published_task.result()
                assert terminal_task is not None
                self._track_finalizer(
                    self._finish_published_run(run_id, bridge_id, terminal_task)
                )
                terminal_task = None
                return answer

            assert terminal_task is not None
            terminal_task.result()
            status = await self._get_status(run_id)
            if status.get("status") != "completed":
                error = status.get("error") or status.get("status") or "unknown"
                raise HermesRuntimeError(f"Hermes run {run_id} failed: {error}")
            await self._bridge.complete(bridge_id)
            answer = await self._bridge.published_answer(bridge_id)
            await self._bridge.discard(bridge_id)
            return answer
        finally:
            if not published_task.done():
                published_task.cancel()
            remaining: list[asyncio.Future[Any]] = [published_task]
            if terminal_task is not None:
                if not terminal_task.done():
                    terminal_task.cancel()
                remaining.append(terminal_task)
            await asyncio.gather(*remaining, return_exceptions=True)

    def _track_finalizer(
        self,
        coroutine: Coroutine[Any, Any, None],
    ) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coroutine)
        self._background_finalizers.add(task)
        task.add_done_callback(self._background_finalizers.discard)

    async def _finish_published_run(
        self,
        run_id: str,
        bridge_id: str,
        terminal_task: asyncio.Task[None],
    ) -> None:
        try:
            await asyncio.wait_for(
                terminal_task,
                timeout=self._settings.hermes_post_publish_timeout_seconds,
            )
            status = await self._get_status(run_id)
            if status.get("status") != "completed":
                logger.warning(
                    "Hermes post-publication finalizer ended with status %s",
                    status.get("status") or "unknown",
                )
            await self._bridge.complete(bridge_id)
            try:
                await asyncio.wait_for(
                    self._bridge.wait_for_native_review_completion(bridge_id),
                    timeout=self._settings.hermes_native_review_timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "Hermes native Memory/Skill review completion was not observed "
                    "within %.1fs",
                    self._settings.hermes_native_review_timeout_seconds,
                )
        except asyncio.CancelledError:
            await self._stop_run(run_id)
            raise
        except TimeoutError:
            logger.warning("Hermes post-publication finalizer timed out")
            await self._stop_run(run_id)
        except Exception as exc:
            logger.warning(
                "Hermes post-publication finalizer failed: %s",
                type(exc).__name__,
            )
        finally:
            await self._bridge.discard(bridge_id)

    async def _wait_for_terminal_event(self, run_id: str) -> None:
        terminal = {"run.completed", "run.failed", "run.cancelled"}
        try:
            async for event in self._stream_events(run_id):
                event_name = str(event.get("event") or "")
                if event_name == "approval.request":
                    await self._deny_approval(run_id)
                if event_name in terminal:
                    return
        except (httpx.HTTPError, json.JSONDecodeError):
            await self._poll_until_terminal(run_id)

    async def _stream_events(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        async with self._client.stream(
            "GET",
            f"/v1/runs/{run_id}/events",
            headers=self._auth_headers(),
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[5:].strip())
                if isinstance(payload, dict):
                    yield payload

    async def _poll_until_terminal(self, run_id: str) -> None:
        while True:
            status = await self._get_status(run_id)
            if status.get("status") in {"completed", "failed", "cancelled"}:
                return
            if status.get("status") == "waiting_for_approval":
                await self._deny_approval(run_id)
            await asyncio.sleep(self._settings.hermes_poll_interval_seconds)

    async def _get_status(self, run_id: str) -> dict[str, Any]:
        response = await self._client.get(
            f"/v1/runs/{run_id}",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise HermesRuntimeError("Hermes returned an invalid run status")
        return payload

    async def _deny_approval(self, run_id: str) -> None:
        response = await self._client.post(
            f"/v1/runs/{run_id}/approval",
            headers=self._auth_headers(),
            json={"decision": "deny"},
        )
        if response.status_code not in {200, 404, 409}:
            response.raise_for_status()

    async def _stop_run(self, run_id: str) -> None:
        try:
            response = await self._client.post(
                f"/v1/runs/{run_id}/stop",
                headers=self._auth_headers(),
            )
            if response.status_code not in {200, 404, 409}:
                response.raise_for_status()
        except httpx.HTTPError:
            pass

    def _auth_headers(self) -> dict[str, str]:
        key = self._settings.hermes_api_key
        if key is None:
            raise HermesRuntimeError("HERMES_API_KEY is required")
        return {"Authorization": f"Bearer {key.get_secret_value()}"}

    def _memory_scope(self, context: RunContext) -> str:
        token = self._settings.hermes_bridge_token
        if token is None:
            raise HermesRuntimeError("HERMES_BRIDGE_TOKEN is required")
        value = ":".join(
            (context.tenant_id, context.project_id, context.user_id, context.session_id)
        )
        digest = hmac.new(
            token.get_secret_value().encode(),
            value.encode(),
            hashlib.sha256,
        ).hexdigest()
        return f"hermesgraph-{digest}"

    @staticmethod
    def _serialize_history(
        history: Sequence[ConversationTurn],
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for turn in history:
            messages.append({"role": "user", "content": turn.user_input})
            messages.append({"role": "assistant", "content": turn.assistant_answer})
        return messages

    def _instructions(
        self,
        context: RunContext,
        capsule: str = "",
        retrieval_route: AdaptiveRAGRoute | KnowledgeQueryRouteDecision | None = None,
    ) -> str:
        domain_context = self._domain_packs.get(context.domain_pack).system_context()
        instructions = load_hermes_instructions(domain_context, capsule)
        if retrieval_route is None:
            return instructions
        if isinstance(retrieval_route, AdaptiveRAGRoute):
            return f"{instructions}\n\n{adaptive_route_instruction(retrieval_route)}"
        return f"{instructions}\n\n{retrieval_route.as_instruction()}"
