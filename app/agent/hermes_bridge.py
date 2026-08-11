from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.answer_publisher import AnswerPublisher, EvidencePublicationError
from app.agent.hermes_native_learning import (
    HermesNativeAdminClient,
    HermesNativeAdminPort,
    HermesNativeLearningConflict,
    HermesNativeLearningUnavailable,
)
from app.application.run_event_recorder import RunEventRecorder
from app.config import Settings
from app.domain.contracts import (
    ComputerWorkspacePort,
    GraphRetrievalToolPort,
    GraphSearchPort,
    LearningChangeSetRepository,
    MemoryRepository,
    RetrievalPort,
    SkillRepository,
    WebSearchPort,
)
from app.domain.enums import RunStatus, SkillStatus
from app.domain.models import (
    AgentAnswerDraft,
    AnswerResponse,
    EvidenceRef,
    GovernedSkillActivationRequest,
    GovernedSkillActivationResult,
    GraphEntityCompareRequest,
    GraphEntityResolveRequest,
    GraphPath,
    GraphRAGRequest,
    GraphSearchRequest,
    LearningChangeSet,
    MemoryRecord,
    RunContext,
    RunTrajectory,
    ToolEvent,
    WebSearchRequest,
    WorkspaceFileReadRequest,
    WorkspaceListRequest,
    WorkspaceSearchRequest,
)
from app.learning.safety import AutomaticLearningDecision, assess_automatic_learning
from app.personal.service import PersonalControlService


class RunBudgetExceeded(RuntimeError):
    pass


@dataclass
class RunBudget:
    max_tool_calls: int
    tool_calls: int = 0
    fingerprints: set[str] = field(default_factory=set)

    def consume(self, fingerprint: str) -> None:
        if fingerprint in self.fingerprints:
            raise RunBudgetExceeded("Repeated tool call blocked")
        if self.tool_calls >= self.max_tool_calls:
            raise RunBudgetExceeded("Tool call budget exhausted")
        self.fingerprints.add(fingerprint)
        self.tool_calls += 1


class HermesBridgeError(RuntimeError):
    pass


class HermesBridgeRunNotFoundError(HermesBridgeError):
    pass


class HermesAnswerNotPublishedError(HermesBridgeError):
    pass


class HermesNativeSnapshotAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    target_kind: Literal["memory", "skill"]
    target_id: str = Field(min_length=1, max_length=255)
    before_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    applied: bool
    rollback_supported: bool
    reason: str | None = Field(default=None, max_length=500)


class HermesNativeToolAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=128)
    args: dict[str, Any] = Field(default_factory=dict)
    result: str = Field(default="", max_length=2_000)
    status: str | None = Field(default=None, max_length=64)
    error_type: str | None = Field(default=None, max_length=128)
    applied: bool | None = None
    snapshot: HermesNativeSnapshotAudit | None = None


@dataclass(slots=True)
class _RunState:
    context: RunContext
    budget: RunBudget
    allowed_evidence: dict[str, EvidenceRef] = field(default_factory=dict)
    allowed_memories: dict[str, MemoryRecord] = field(default_factory=dict)
    graph_paths: dict[str, GraphPath] = field(default_factory=dict)
    retrieval_calls: int = 0
    graph_calls: int = 0
    web_search_calls: int = 0
    computer_calls: int = 0
    personal_calls: int = 0
    skill_activations: int = 0
    published_answer: AnswerResponse | None = None
    published_event: asyncio.Event = field(default_factory=asyncio.Event)
    native_review_completed_event: asyncio.Event = field(default_factory=asyncio.Event)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    published_at: datetime | None = None
    completed_at: datetime | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class HermesCapabilityBridge:
    """Run-scoped capability and publication boundary for the Hermes sidecar."""

    def __init__(
        self,
        *,
        settings: Settings,
        retrieval: RetrievalPort,
        graph_search: GraphSearchPort | None = None,
        graph_tools: GraphRetrievalToolPort | None = None,
        web_search: WebSearchPort | None = None,
        workspace: ComputerWorkspacePort | None = None,
        memory_repository: MemoryRepository | None = None,
        skill_repository: SkillRepository | None = None,
        publisher: AnswerPublisher | None = None,
        event_recorder: RunEventRecorder | None = None,
        change_set_repository: LearningChangeSetRepository | None = None,
        personal: PersonalControlService | None = None,
        native_admin: HermesNativeAdminPort | None = None,
        retention_seconds: int = 600,
    ) -> None:
        self._settings = settings
        self._retrieval = retrieval
        self._graph_search = graph_search
        self._graph_tools = graph_tools
        self._web_search = web_search
        self._workspace = workspace
        self._memory_repository = memory_repository
        self._skill_repository = skill_repository
        self._publisher = publisher or AnswerPublisher()
        self._event_recorder = event_recorder
        self._change_sets = change_set_repository
        self._personal = personal
        self._native_admin = native_admin
        self._retention = timedelta(seconds=retention_seconds)
        self._runs: dict[str, _RunState] = {}
        self._lock = asyncio.Lock()

    async def open_run(
        self,
        context: RunContext,
        *,
        allowed_memories: Sequence[MemoryRecord] = (),
    ) -> str:
        bridge_id = f"hg_{context.run_id.hex}_{secrets.token_urlsafe(12)}"
        scoped_memories: dict[str, MemoryRecord] = {}
        for memory in allowed_memories:
            self._validate_memory_scope(memory, context)
            if memory.revoked_at is None:
                scoped_memories[str(memory.memory_id)] = memory
        async with self._lock:
            self._prune_locked()
            self._runs[bridge_id] = _RunState(
                context=context,
                budget=RunBudget(max_tool_calls=self._settings.max_tool_calls),
                allowed_memories=scoped_memories,
            )
        return bridge_id

    def is_authorized(self, supplied_token: str) -> bool:
        configured = self._settings.hermes_bridge_token
        return configured is not None and hmac.compare_digest(
            supplied_token,
            configured.get_secret_value(),
        )

    async def invoke(
        self,
        bridge_id: str,
        tool_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        state = await self._state(bridge_id)
        if tool_name == "hermesgraph_publish_answer":
            return await self._publish(state, payload)

        payload = self._normalize_policy_payload(state.context, tool_name, payload)
        fingerprint = hashlib.sha256(
            json.dumps(
                {"tool": tool_name, "payload": payload},
                sort_keys=True,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        started = perf_counter()
        try:
            async with state.lock:
                if state.published_answer is not None:
                    raise HermesBridgeError("No tools may run after answer publication")
                state.budget.consume(fingerprint)
                self._consume_per_tool_budget(state, tool_name)
            result, evidence, graph_paths = await self._execute(state, tool_name, payload)
            async with state.lock:
                for item in evidence:
                    state.allowed_evidence[str(item.evidence_id)] = item
                for path in graph_paths:
                    self._validate_graph_path_scope(path, state.context)
                    state.graph_paths[self._graph_path_identity(path)] = path
        except Exception as exc:
            await self._record_tool(
                state.context,
                ToolEvent(
                    tool_name=tool_name,
                    input_hash=fingerprint,
                    output_summary=type(exc).__name__,
                    success=False,
                    duration_ms=int((perf_counter() - started) * 1_000),
                ),
            )
            raise

        await self._record_tool(
            state.context,
            ToolEvent(
                tool_name=tool_name,
                input_hash=fingerprint,
                output_summary=self._result_summary(tool_name, result, len(evidence)),
                duration_ms=int((perf_counter() - started) * 1_000),
            ),
        )
        return {"success": True, "result": result}

    async def audit_native_tool(
        self,
        bridge_id: str,
        audit: HermesNativeToolAudit,
    ) -> dict[str, bool]:
        state = await self._state(bridge_id)
        if audit.tool_name == "hermes_background_review_completed":
            state.native_review_completed_event.set()
            return {"accepted": True}
        payload = json.dumps(audit.args, sort_keys=True, default=str, ensure_ascii=False)
        success = self._native_audit_succeeded(audit)
        applied = audit.applied if audit.applied is not None else success
        async with state.lock:
            run_events_closed = state.published_at is not None
        native_mutation = audit.tool_name in {"memory", "skill_manage"}
        if success and applied and native_mutation:
            decision = await self._native_learning_decision(state)
            if not decision.allowed:
                rollback = await self._rollback_unsafe_native_write(audit)
                if not run_events_closed:
                    await self._record_tool(
                        state.context,
                        ToolEvent(
                            tool_name=f"hermes.{audit.tool_name}",
                            input_hash=hashlib.sha256(payload.encode()).hexdigest(),
                            output_summary="native_learning_blocked",
                            detail={
                                "runtime": "hermes",
                                "native_tool": audit.tool_name,
                                "native_learning_blocked": True,
                                "learning_gate_reasons": list(decision.reasons),
                                "rollback_state": rollback["state"],
                            },
                            success=False,
                        ),
                    )
                await self._record_blocked_native_change(
                    state,
                    audit,
                    decision,
                    rollback,
                )
                return {"accepted": False}
        if not run_events_closed:
            await self._record_tool(
                state.context,
                ToolEvent(
                    tool_name=f"hermes.{audit.tool_name}",
                    input_hash=hashlib.sha256(payload.encode()).hexdigest(),
                    output_summary=audit.result[:500],
                    detail={
                        "runtime": "hermes",
                        "native_tool": audit.tool_name,
                        "arguments": payload[:8_000],
                        "applied": applied,
                        "snapshot_id": (
                            audit.snapshot.snapshot_id if audit.snapshot is not None else None
                        ),
                    },
                    success=success,
                ),
            )
        if success and applied and native_mutation:
            await self._record_native_change(state, audit)
        return {"accepted": True}

    async def published_answer(self, bridge_id: str) -> AnswerResponse:
        state = await self._state(bridge_id)
        async with state.lock:
            answer = state.published_answer
        if answer is None:
            raise HermesAnswerNotPublishedError(
                "Hermes completed without calling hermesgraph_publish_answer"
            )
        return answer

    async def complete(self, bridge_id: str) -> None:
        state = await self._state(bridge_id)
        async with state.lock:
            state.completed_at = datetime.now(UTC)

    async def wait_for_published_answer(self, bridge_id: str) -> AnswerResponse:
        state = await self._state(bridge_id)
        await state.published_event.wait()
        return await self.published_answer(bridge_id)

    async def wait_for_native_review_completion(self, bridge_id: str) -> None:
        state = await self._state(bridge_id)
        await state.native_review_completed_event.wait()

    async def discard(self, bridge_id: str) -> None:
        async with self._lock:
            self._runs.pop(bridge_id, None)

    async def _publish(
        self,
        state: _RunState,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with state.lock:
            if state.published_answer is not None:
                answer = state.published_answer
                return {
                    "success": True,
                    "published": True,
                    "duplicate_ignored": True,
                    "answer_unchanged": True,
                    "citation_count": len(answer.citations),
                    "graph_path_count": len(answer.graph_paths),
                    "memory_count": len(answer.memory_ids),
                    "confidence": answer.confidence.value,
                    "message": (
                        "The final answer was already published and remains unchanged. "
                        "Do not call any more tools; finish this turn with a plain-text "
                        "response now."
                    ),
                }
            draft = AgentAnswerDraft.model_validate(payload)
            unknown_memories = {
                str(memory_id)
                for memory_id in draft.memory_ids
                if str(memory_id) not in state.allowed_memories
            }
            if unknown_memories:
                raise EvidencePublicationError(
                    "Answer referred to memory outside this run: "
                    f"{unknown_memories}"
                )
            answer = self._publisher.publish(
                draft,
                allowed_evidence=list(state.allowed_evidence.values()),
                graph_paths=list(state.graph_paths.values()),
            )
            answer = answer.model_copy(
                update={"memory_ids": list(dict.fromkeys(draft.memory_ids))}
            )
            state.published_answer = answer
            state.published_at = datetime.now(UTC)
        await self._record_tool(
            state.context,
            ToolEvent(
                tool_name="hermesgraph_publish_answer",
                input_hash=hashlib.sha256(draft.model_dump_json().encode()).hexdigest(),
                output_summary=(
                    f"claims={len(answer.claims)},citations={len(answer.citations)},"
                    f"graph_paths={len(answer.graph_paths)},memories={len(answer.memory_ids)},"
                    f"confidence={answer.confidence.value}"
                ),
            ),
        )
        state.published_event.set()
        return {
            "success": True,
            "published": True,
            "citation_count": len(answer.citations),
            "graph_path_count": len(answer.graph_paths),
            "memory_count": len(answer.memory_ids),
            "confidence": answer.confidence.value,
        }

    async def _execute(
        self,
        state: _RunState,
        tool_name: str,
        payload: dict[str, Any],
    ) -> tuple[Any, list[EvidenceRef], list[GraphPath]]:
        context = state.context
        if tool_name == "search_knowledge":
            query = self._safe_query(payload.get("query"))
            top_k = int(payload.get("top_k", 10))
            if not 1 <= top_k <= 50:
                raise ValueError("top_k must be between 1 and 50")
            bundle = await self._retrieval.retrieve(query, context, top_k=top_k)
            return bundle.model_dump(mode="json", exclude_none=True), list(bundle.evidence), []

        if tool_name == "search_graph":
            if self._graph_search is None:
                raise HermesBridgeError("Graph search is not configured")
            graph_request = GraphSearchRequest.model_validate(payload)
            graph_result = await self._graph_search.search_graph(graph_request, context)
            paths = list(graph_result.paths)
            return (
                graph_result.model_dump(mode="json", exclude_none=True),
                _graph_tool_evidence(graph_result.evidence, paths),
                paths,
            )

        if tool_name == "resolve_graph_entities":
            if self._graph_tools is None:
                raise HermesBridgeError("Graph retrieval tools are not configured")
            resolve_request = GraphEntityResolveRequest.model_validate(payload)
            resolve_result = await self._graph_tools.resolve_graph_entities(
                resolve_request, context
            )
            return (
                resolve_result.model_dump(mode="json", exclude_none=True),
                list(resolve_result.evidence),
                [],
            )

        if tool_name == "retrieve_evidence_subgraph":
            if self._graph_tools is None:
                raise HermesBridgeError("Graph retrieval tools are not configured")
            graph_rag_request = GraphRAGRequest.model_validate(payload)
            graph_rag_result = await self._graph_tools.retrieve_evidence_subgraph(
                graph_rag_request, context
            )
            paths = list(graph_rag_result.graph_paths)
            return (
                graph_rag_result.model_dump(mode="json", exclude_none=True),
                _graph_tool_evidence(graph_rag_result.evidence, paths),
                paths,
            )

        if tool_name == "compare_graph_entities":
            if self._graph_tools is None:
                raise HermesBridgeError("Graph retrieval tools are not configured")
            compare_request = GraphEntityCompareRequest.model_validate(payload)
            compare_result = await self._graph_tools.compare_graph_entities(
                compare_request, context
            )
            paths = list(compare_result.connecting_paths)
            return (
                compare_result.model_dump(mode="json", exclude_none=True),
                _graph_tool_evidence(compare_result.evidence, paths),
                paths,
            )

        if tool_name == "search_web":
            if self._web_search is None:
                raise HermesBridgeError("Web search is not configured")
            request_payload = dict(payload)
            request_payload.setdefault("max_results", self._settings.web_search_max_results)
            web_request = WebSearchRequest.model_validate(request_payload)
            if web_request.max_results > self._settings.web_search_max_results:
                raise ValueError("max_results exceeds the deployment web search budget")
            web_result = await self._web_search.search_web(web_request, context)
            return (
                web_result.model_dump(mode="json", exclude_none=True),
                list(web_result.evidence),
                [],
            )

        if tool_name == "list_workspace_files":
            if self._workspace is None:
                raise HermesBridgeError("Computer workspace tools are not configured")
            list_result = await self._workspace.list_workspace_files(
                WorkspaceListRequest.model_validate(payload),
                context,
            )
            return list_result.model_dump(mode="json", exclude_none=True), [], []

        if tool_name == "read_workspace_file":
            if self._workspace is None:
                raise HermesBridgeError("Computer workspace tools are not configured")
            read_result = await self._workspace.read_workspace_file(
                WorkspaceFileReadRequest.model_validate(payload),
                context,
            )
            return (
                read_result.model_dump(mode="json", exclude_none=True),
                list(read_result.evidence),
                [],
            )

        if tool_name == "search_workspace_files":
            if self._workspace is None:
                raise HermesBridgeError("Computer workspace tools are not configured")
            search_result = await self._workspace.search_workspace_files(
                WorkspaceSearchRequest.model_validate(payload),
                context,
            )
            return (
                search_result.model_dump(mode="json", exclude_none=True),
                list(search_result.evidence),
                [],
            )

        if tool_name == "recall_project_memory":
            if self._memory_repository is None:
                raise HermesBridgeError("Project memory is not configured")
            query = self._safe_query(payload.get("query"))
            limit = int(payload.get("limit", 5))
            if not 1 <= limit <= 20:
                raise ValueError("limit must be between 1 and 20")
            records = await self._memory_repository.search(
                query,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                user_id=context.user_id,
                limit=limit,
            )
            async with state.lock:
                for record in records:
                    self._validate_memory_scope(record, context)
                    if record.revoked_at is None:
                        state.allowed_memories[str(record.memory_id)] = record
            return (
                [item.model_dump(mode="json", exclude_none=True) for item in records],
                [],
                [],
            )

        if tool_name in {
            "manage_personal_tasks",
            "manage_personal_plans",
            "manage_personal_notes",
            "correct_personal_memory",
            "manage_personal_profile",
            "manage_personal_journal",
        }:
            if self._personal is None:
                raise HermesBridgeError("Personal control tools are not configured")
            return await self._personal.execute_tool(tool_name, payload, context), [], []

        if tool_name == "activate_governed_skill":
            if self._skill_repository is None:
                raise HermesBridgeError("Governed skills are not configured")
            request = GovernedSkillActivationRequest.model_validate(payload)
            pinned_version = context.skill_versions.get(request.name)
            if pinned_version is None:
                raise HermesBridgeError("The requested skill is not pinned for this run")
            if request.version is not None and request.version != pinned_version:
                raise HermesBridgeError("The requested skill version differs from the run snapshot")
            skill = await self._skill_repository.get_by_name(
                request.name,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                version=pinned_version,
            )
            if skill is None:
                raise HermesBridgeError("The pinned governed skill is unavailable")
            if skill.status not in {SkillStatus.CANARY, SkillStatus.ACTIVE}:
                raise HermesBridgeError("The pinned governed skill is not runtime eligible")
            result = GovernedSkillActivationResult(
                skill_id=skill.skill_id,
                name=skill.name,
                version=skill.version,
                description=skill.description,
                steps=skill.steps,
                allowed_capabilities=skill.allowed_capabilities,
                constraints=skill.constraints,
                source_run_count=len(skill.source_run_ids),
            )
            return result.model_dump(mode="json", exclude_none=True), [], []

        raise HermesBridgeError(f"Unsupported HermesGraph tool: {tool_name}")

    @staticmethod
    def _normalize_policy_payload(
        context: RunContext,
        tool_name: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        policy = context.execution_policy
        if policy is None or not policy.behavior_applied:
            return payload
        request: GraphSearchRequest | GraphRAGRequest | GraphEntityCompareRequest
        if tool_name == "search_graph":
            request = GraphSearchRequest.model_validate(payload)
        elif tool_name == "retrieve_evidence_subgraph":
            request = GraphRAGRequest.model_validate(payload)
        elif tool_name == "compare_graph_entities":
            request = GraphEntityCompareRequest.model_validate(payload)
        else:
            return payload
        if (
            policy.graph_hop_cap is not None
            and request.max_hops > policy.graph_hop_cap
        ):
            request = request.model_copy(
                update={"max_hops": policy.graph_hop_cap}
            )
        return request.model_dump(mode="json")

    def _consume_per_tool_budget(self, state: _RunState, tool_name: str) -> None:
        if tool_name == "search_knowledge":
            if state.retrieval_calls >= self._settings.max_retrieval_tool_calls:
                raise RunBudgetExceeded("Per-run retrieval call budget exhausted")
            state.retrieval_calls += 1
        elif tool_name in {
            "search_graph",
            "resolve_graph_entities",
            "retrieve_evidence_subgraph",
            "compare_graph_entities",
        }:
            if state.graph_calls >= self._settings.max_graph_tool_calls:
                raise RunBudgetExceeded("Per-run graph tool call budget exhausted")
            state.graph_calls += 1
        elif tool_name == "search_web":
            if state.web_search_calls >= self._settings.max_web_search_tool_calls:
                raise RunBudgetExceeded("Per-run web search call budget exhausted")
            state.web_search_calls += 1
        elif tool_name in {
            "list_workspace_files",
            "read_workspace_file",
            "search_workspace_files",
        }:
            if state.computer_calls >= self._settings.max_computer_tool_calls:
                raise RunBudgetExceeded("Per-run computer workspace call budget exhausted")
            state.computer_calls += 1
        elif tool_name in {
            "manage_personal_tasks",
            "manage_personal_plans",
            "manage_personal_notes",
            "correct_personal_memory",
            "manage_personal_profile",
            "manage_personal_journal",
        }:
            if state.personal_calls >= self._settings.max_personal_tool_calls:
                raise RunBudgetExceeded("Per-run personal control tool budget exhausted")
            state.personal_calls += 1
        elif tool_name == "activate_governed_skill":
            if state.skill_activations >= self._settings.max_skill_activations:
                raise RunBudgetExceeded("Per-run governed skill activation budget exhausted")
            state.skill_activations += 1

    async def _state(self, bridge_id: str) -> _RunState:
        async with self._lock:
            state = self._runs.get(bridge_id)
        if state is None:
            raise HermesBridgeRunNotFoundError("Unknown or expired Hermes bridge run")
        return state

    def _prune_locked(self) -> None:
        cutoff = datetime.now(UTC) - self._retention
        expired = [
            bridge_id
            for bridge_id, state in self._runs.items()
            if (state.completed_at or state.created_at) < cutoff
        ]
        for bridge_id in expired:
            del self._runs[bridge_id]

    @staticmethod
    def _validate_graph_path_scope(path: GraphPath, context: RunContext) -> None:
        if not path.nodes or not path.relationships:
            raise HermesBridgeError("Graph tool returned an incomplete graph path")
        node_out_of_scope = any(
            node.tenant_id != context.tenant_id or node.project_id != context.project_id
            for node in path.nodes
        )
        relationship_out_of_scope = any(
            relationship.tenant_id != context.tenant_id
            or relationship.project_id != context.project_id
            for relationship in path.relationships
        )
        if node_out_of_scope or relationship_out_of_scope:
            raise HermesBridgeError("Graph tool returned a path outside the active scope")

    @staticmethod
    def _graph_path_identity(path: GraphPath) -> str:
        payload = path.model_dump(mode="json", exclude_none=True)
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    async def _record_tool(self, context: RunContext, event: ToolEvent) -> None:
        if self._event_recorder is not None:
            await self._event_recorder.record_tool(context.run_id, event)

    async def _native_learning_decision(
        self,
        state: _RunState,
    ) -> AutomaticLearningDecision:
        """Evaluate only the immutable answer that Hermes actually published."""

        async with state.lock:
            answer = state.published_answer
            run_completed = state.completed_at is not None
        return assess_automatic_learning(
            RunTrajectory(
                context=state.context,
                user_input="",
                status=(
                    RunStatus.COMPLETED
                    if answer is not None and run_completed
                    else RunStatus.RUNNING
                ),
                answer=answer,
            )
        )

    async def _rollback_unsafe_native_write(
        self,
        audit: HermesNativeToolAudit,
    ) -> dict[str, str | bool]:
        """Compensate a post-hook native write when a snapshot makes it possible."""

        snapshot = audit.snapshot
        if (
            snapshot is None
            or not snapshot.rollback_supported
            or not snapshot.after_hash
        ):
            return {
                "attempted": False,
                "state": "rollback_unavailable",
                "reason": "snapshot_rollback_unavailable",
            }
        admin = self._native_admin
        temporary_admin: HermesNativeAdminClient | None = None
        if admin is None:
            if self._settings.hermes_native_admin_token is None:
                return {
                    "attempted": False,
                    "state": "rollback_unavailable",
                    "reason": "native_admin_not_configured",
                }
            try:
                temporary_admin = HermesNativeAdminClient(self._settings)
            except ValueError:
                return {
                    "attempted": False,
                    "state": "rollback_unavailable",
                    "reason": "native_admin_not_configured",
                }
            admin = temporary_admin
        try:
            result = await admin.rollback(
                snapshot.snapshot_id,
                expected_after_hash=snapshot.after_hash,
            )
            return {
                "attempted": True,
                "state": "rolled_back" if result.success else "rollback_failed",
                "reason": "compensating_rollback_completed"
                if result.success
                else "compensating_rollback_rejected",
            }
        except HermesNativeLearningConflict:
            return {
                "attempted": True,
                "state": "rollback_failed",
                "reason": "native_snapshot_conflict",
            }
        except HermesNativeLearningUnavailable:
            return {
                "attempted": True,
                "state": "rollback_failed",
                "reason": "native_admin_unavailable",
            }
        finally:
            if temporary_admin is not None:
                await temporary_admin.close()

    async def _record_blocked_native_change(
        self,
        state: _RunState,
        audit: HermesNativeToolAudit,
        decision: AutomaticLearningDecision,
        rollback: dict[str, str | bool],
    ) -> None:
        """Keep a non-learnable audit without treating the native write as an asset."""

        if self._change_sets is None:
            return
        context = state.context
        target_id = self._native_target_id(audit)
        await self._change_sets.save(
            LearningChangeSet(
                target_type="hermes_native_learning_blocked_write",
                target_id=target_id,
                structured_diff={
                    "runtime": "hermes",
                    "state": "native_write_blocked",
                    "tool": audit.tool_name,
                    "target": target_id,
                    "native_applied_reported": bool(
                        audit.applied if audit.applied is not None else True
                    ),
                    "learning_gate_reasons": list(decision.reasons),
                    "rollback": rollback,
                    "snapshot": (
                        audit.snapshot.model_dump(mode="json")
                        if audit.snapshot is not None
                        else None
                    ),
                },
                source_run_ids=[context.run_id],
                expected_benefits=[],
                risks=[
                    "Unsafe final evidence must not produce a native Hermes learning asset",
                    "A compensating rollback can fail if the native state drifted",
                ],
                scope={
                    "tenant_id": context.tenant_id,
                    "project_id": context.project_id,
                    "user_id": context.user_id,
                },
                evaluation_report={
                    "status": "non_learnable",
                    "runtime": "hermes",
                    "learning_gate_reasons": list(decision.reasons),
                    "rollback_state": rollback["state"],
                },
                rollback_conditions=[
                    "The native snapshot after_hash must still match before rollback",
                ],
            )
        )

    async def _record_native_change(
        self,
        state: _RunState,
        audit: HermesNativeToolAudit,
    ) -> None:
        if self._change_sets is None:
            return
        context = state.context
        target_type = (
            "hermes_native_memory" if audit.tool_name == "memory" else "hermes_native_skill"
        )
        target_id = self._native_target_id(audit)
        await self._change_sets.save(
            LearningChangeSet(
                target_type=target_type,
                target_id=target_id[:255],
                structured_diff={
                    "runtime": "hermes",
                    "state": "native_applied",
                    "tool": audit.tool_name,
                    "arguments": audit.args,
                    "result": audit.result,
                    "audit_error_type": audit.error_type,
                    "snapshot": (
                        audit.snapshot.model_dump(mode="json")
                        if audit.snapshot is not None
                        else None
                    ),
                },
                source_run_ids=[context.run_id],
                expected_benefits=["Preserve a reusable Hermes learning artifact"],
                risks=["Native Hermes learning was applied before HermesGraph evaluation"],
                scope={
                    "tenant_id": context.tenant_id,
                    "project_id": context.project_id,
                    "user_id": context.user_id,
                },
                evaluation_report={
                    "status": "requires_audit",
                    "runtime": "hermes",
                    "native_applied": True,
                    "rollback_supported": bool(
                        audit.snapshot is not None and audit.snapshot.rollback_supported
                    ),
                },
                rollback_conditions=[
                    "User correction",
                    "Unsupported memory",
                    "Skill evaluation regression",
                    "Current native artifact hash must still match the audited after_hash",
                ],
            )
        )

    @staticmethod
    def _native_target_id(audit: HermesNativeToolAudit) -> str:
        return str(
            audit.args.get("name")
            or audit.args.get("id")
            or audit.args.get("action")
            or audit.tool_name
        )[:255]

    @staticmethod
    def _native_audit_succeeded(audit: HermesNativeToolAudit) -> bool:
        if audit.status not in {None, "", "ok"}:
            return False
        try:
            parsed = json.loads(audit.result)
        except json.JSONDecodeError:
            result_lower = audit.result.lower()
            return not any(marker in result_lower for marker in ('"error"', "error:", "failed"))
        if isinstance(parsed, dict):
            return parsed.get("success") is not False and not parsed.get("error")
        return True

    @staticmethod
    def _safe_query(value: Any) -> str:
        query = str(value or "")
        if not query.strip() or len(query) > 2_000 or "\x00" in query:
            raise ValueError("query must contain 1-2000 safe text characters")
        return query

    @staticmethod
    def _validate_memory_scope(memory: MemoryRecord, context: RunContext) -> None:
        if (
            memory.tenant_id != context.tenant_id
            or memory.project_id != context.project_id
            or memory.user_id not in {None, context.user_id}
        ):
            raise HermesBridgeError("Memory does not belong to the active run scope")

    @staticmethod
    def _result_summary(tool_name: str, result: Any, evidence_count: int) -> str:
        if tool_name == "recall_project_memory":
            return f"memory_count={len(result)}"
        if tool_name in {
            "manage_personal_tasks",
            "manage_personal_plans",
            "manage_personal_notes",
            "correct_personal_memory",
            "manage_personal_profile",
            "manage_personal_journal",
        }:
            return "personal_control_operation=completed"
        if tool_name == "list_workspace_files":
            return f"entry_count={len(result.get('entries', []))}"
        if tool_name == "read_workspace_file":
            return (
                f"lines={result.get('start_line', 0)}-{result.get('end_line', 0)},"
                f"evidence_count={evidence_count}"
            )
        if tool_name == "search_workspace_files":
            return (
                f"match_count={len(result.get('matches', []))},"
                f"evidence_count={evidence_count}"
            )
        if tool_name == "activate_governed_skill":
            return (
                f"skill={result.get('name', '')}@{result.get('version', '')},"
                f"steps={len(result.get('steps', []))}"
            )
        return f"evidence_count={evidence_count}"


def _graph_tool_evidence(
    top_level: list[EvidenceRef],
    paths: list[GraphPath],
) -> list[EvidenceRef]:
    """Retain graph-path evidence in the current run allowlist before publishing."""

    evidence: dict[str, EvidenceRef] = {
        str(item.evidence_id): item for item in top_level
    }
    for path in paths:
        for item in path.evidence:
            evidence[str(item.evidence_id)] = item
        for relationship in path.relationships:
            for item in relationship.evidence:
                evidence[str(item.evidence_id)] = item
    return list(evidence.values())
