import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.agent.answer_publisher import EvidencePublicationError
from app.agent.hermes_bridge import (
    HermesBridgeError,
    HermesCapabilityBridge,
    HermesNativeSnapshotAudit,
    HermesNativeToolAudit,
    RunBudgetExceeded,
)
from app.agent.hermes_native_learning import HermesNativeRollbackResult
from app.application.run_event_recorder import RunEventRecorder
from app.capabilities import AgentToolRuntime
from app.config import Settings
from app.domain.enums import MemoryType, SkillStatus, TrustLevel
from app.domain.models import (
    EvidenceRef,
    GraphNode,
    GraphRelationship,
    MemoryCandidate,
    MemoryRecord,
    Provenance,
    RetrievalBundle,
    RunContext,
    SkillDefinition,
    SkillStep,
)
from app.graph import InMemoryEvidenceGraph
from app.learning.change_set import JsonLearningChangeSetRepository
from app.memory import JsonMemoryStore
from app.retrieval import InMemoryRetriever, RetrievalPipeline
from app.skills.skill_markdown_repository import SkillMarkdownRepository


class FixtureRetrieval:
    def __init__(self, evidence: EvidenceRef) -> None:
        self.evidence = evidence

    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> RetrievalBundle:
        del context, filters, top_k
        return RetrievalBundle(query=query, evidence=[self.evidence])


def evidence() -> EvidenceRef:
    return EvidenceRef(
        text="HermesGraph is the scoped evidence backend.",
        provenance=Provenance(
            source_type="fixture",
            source_id="doc-1",
            trust=TrustLevel.VERIFIED,
        ),
        metadata={"knowledge_layer": "team_internal"},
    )


def memory_candidate(*, user_id: str | None = "local-user") -> MemoryCandidate:
    return MemoryCandidate(
        user_id=user_id,
        memory_type=MemoryType.POLICY,
        key=f"answer-language:{user_id}",
        summary="Prefer concise Chinese answers.",
        detail={"language": "zh-CN", "style": "concise"},
        confidence=0.95,
        provenance=[
            Provenance(
                source_type="user_feedback",
                source_id=f"memory-{user_id}",
                trust=TrustLevel.USER_ASSERTED,
            )
        ],
    )


@pytest.mark.asyncio
async def test_bridge_hydrates_only_evidence_retrieved_in_the_run() -> None:
    item = evidence()
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(item),
    )
    bridge_id = await bridge.open_run(RunContext())

    result = await bridge.invoke(
        bridge_id,
        "search_knowledge",
        {"query": "HermesGraph"},
    )
    answer_result = await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {
            "answer_markdown": "Supported.",
            "claims": [
                {
                    "text": "HermesGraph is the evidence backend.",
                    "evidence_ids": [str(item.evidence_id)],
                    "level": "supported",
                }
            ],
            "citation_ids": [str(item.evidence_id)],
            "confidence": "supported",
        },
    )
    answer = await bridge.published_answer(bridge_id)

    assert result["success"] is True
    assert answer_result["published"] is True
    assert answer.citations == [item]


@pytest.mark.asyncio
async def test_bridge_rejects_foreign_evidence_and_tools_after_publication() -> None:
    item = evidence()
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(item),
    )
    bridge_id = await bridge.open_run(RunContext())

    with pytest.raises(EvidencePublicationError):
        await bridge.invoke(
            bridge_id,
            "hermesgraph_publish_answer",
            {
                "answer_markdown": "Invented.",
                "citation_ids": [str(uuid4())],
                "confidence": "supported",
            },
        )


@pytest.mark.asyncio
async def test_bridge_publishes_only_memories_allowlisted_for_the_run() -> None:
    context = RunContext(user_id="user-a")
    personal = MemoryRecord(**memory_candidate(user_id="user-a").model_dump())
    shared = MemoryRecord(**memory_candidate(user_id=None).model_dump())
    foreign = MemoryRecord(**memory_candidate(user_id="user-b").model_dump())
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(evidence()),
    )
    bridge_id = await bridge.open_run(
        context,
        allowed_memories=[personal, shared],
    )

    result = await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {
            "answer_markdown": "我会使用简洁中文回答。",
            "response_mode": "conversational",
            "memory_ids": [str(personal.memory_id), str(shared.memory_id)],
            "confidence": "insufficient",
        },
    )
    answer = await bridge.published_answer(bridge_id)

    assert result["memory_count"] == 2
    assert answer.memory_ids == [personal.memory_id, shared.memory_id]
    with pytest.raises(HermesBridgeError, match="active run scope"):
        await bridge.open_run(context, allowed_memories=[foreign])

    unknown_bridge_id = await bridge.open_run(context)
    with pytest.raises(EvidencePublicationError, match="memory outside this run"):
        await bridge.invoke(
            unknown_bridge_id,
            "hermesgraph_publish_answer",
            {
                "answer_markdown": "Invented memory attribution.",
                "memory_ids": [str(uuid4())],
                "confidence": "insufficient",
            },
        )


@pytest.mark.asyncio
async def test_bridge_allows_memory_returned_by_recall_tool(tmp_path: Path) -> None:
    store = JsonMemoryStore(tmp_path / "memories.json")
    memory = await store.upsert(memory_candidate())
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(evidence()),
        memory_repository=store,
    )
    bridge_id = await bridge.open_run(RunContext())

    recalled = await bridge.invoke(
        bridge_id,
        "recall_project_memory",
        {"query": "concise Chinese", "limit": 5},
    )
    await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {
            "answer_markdown": "我会简洁作答。",
            "response_mode": "conversational",
            "memory_ids": [str(memory.memory_id)],
            "confidence": "insufficient",
        },
    )
    answer = await bridge.published_answer(bridge_id)

    assert recalled["result"][0]["memory_id"] == str(memory.memory_id)
    assert answer.memory_ids == [memory.memory_id]

    await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {"answer_markdown": "Insufficient.", "confidence": "insufficient"},
    )
    with pytest.raises(HermesBridgeError, match="after answer publication"):
        await bridge.invoke(
            bridge_id,
            "search_knowledge",
            {"query": "too late"},
        )


@pytest.mark.asyncio
async def test_bridge_treats_repeated_answer_publication_as_idempotent() -> None:
    recorder = RunEventRecorder()
    context = RunContext()
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(evidence()),
        event_recorder=recorder,
    )
    bridge_id = await bridge.open_run(context)

    first = await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {"answer_markdown": "First and immutable.", "confidence": "insufficient"},
    )
    duplicate = await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {"answer_markdown": "A conflicting replacement.", "confidence": "supported"},
    )
    answer = await bridge.published_answer(bridge_id)
    events = await recorder.drain_tools(context.run_id)

    assert first["published"] is True
    assert duplicate["duplicate_ignored"] is True
    assert duplicate["answer_unchanged"] is True
    assert answer.answer_markdown == "First and immutable."
    assert [event.tool_name for event in events] == ["hermesgraph_publish_answer"]


@pytest.mark.asyncio
async def test_bridge_blocks_repeated_calls_and_audits_native_learning_tools(
    tmp_path: Path,
) -> None:
    item = evidence()
    recorder = RunEventRecorder()
    change_sets = JsonLearningChangeSetRepository(tmp_path / "changes.json")
    context = RunContext()
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(item),
        event_recorder=recorder,
        change_set_repository=change_sets,
    )
    bridge_id = await bridge.open_run(context)
    await bridge.invoke(bridge_id, "search_knowledge", {"query": "same"})

    with pytest.raises(RunBudgetExceeded, match="Repeated tool call"):
        await bridge.invoke(bridge_id, "search_knowledge", {"query": "same"})

    audit_result = await bridge.audit_native_tool(
        bridge_id,
        HermesNativeToolAudit(
            tool_name="skill_manage",
            args={"action": "create", "name": "fixture"},
            result='{"success": true}',
        ),
    )
    events = await recorder.drain_tools(context.run_id)
    native_changes = await change_sets.list_for_run(context.run_id)

    assert [event.tool_name for event in events] == [
        "search_knowledge",
        "search_knowledge",
        "hermes.skill_manage",
    ]
    assert events[1].success is False
    assert events[2].detail["runtime"] == "hermes"
    assert events[2].success is False
    assert audit_result == {"accepted": False}
    assert native_changes[0].target_type == "hermes_native_learning_blocked_write"
    assert native_changes[0].evaluation_report["status"] == "non_learnable"
    assert "run_not_completed" in native_changes[0].evaluation_report[
        "learning_gate_reasons"
    ]


@pytest.mark.asyncio
async def test_bridge_exposes_graph_rag_tools_with_budget_and_citation_hydration() -> None:
    item = evidence()
    context = RunContext()
    graph = InMemoryEvidenceGraph(
        nodes=[
            GraphNode(
                node_id="hermes",
                tenant_id="local",
                project_id="default",
                label="Runtime",
                name="Hermes Agent",
                properties={"aliases": ["Hermes"]},
            ),
            GraphNode(
                node_id="graph",
                tenant_id="local",
                project_id="default",
                label="Capability",
                name="Knowledge Graph",
            ),
        ],
        relationships=[
            GraphRelationship(
                relationship_id="uses",
                tenant_id="local",
                project_id="default",
                relation_type="uses",
                source_node_id="hermes",
                target_node_id="graph",
                evidence=[item],
            )
        ],
    )
    runtime = AgentToolRuntime(
        RetrievalPipeline({"fixture": InMemoryRetriever([item])}),
        graph=graph,
    )
    bridge = HermesCapabilityBridge(
        settings=Settings(
            app_env="test",
            hermes_bridge_token="bridge-secret",
            max_graph_tool_calls=1,
        ),
        retrieval=runtime,
        graph_search=runtime,
        graph_tools=runtime,
    )
    bridge_id = await bridge.open_run(context)

    result = await bridge.invoke(
        bridge_id,
        "retrieve_evidence_subgraph",
        {
            "query": "How does Hermes use the knowledge graph?",
            "seed_entities": ["Hermes"],
            "max_hops": 2,
        },
    )
    with pytest.raises(RunBudgetExceeded, match="graph tool call budget"):
        await bridge.invoke(
            bridge_id,
            "resolve_graph_entities",
            {"mentions": ["Knowledge Graph"]},
        )
    await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {
            "answer_markdown": "Hermes uses the knowledge graph.",
            "citation_ids": [str(item.evidence_id)],
            "confidence": "supported",
        },
    )
    answer = await bridge.published_answer(bridge_id)

    assert result["result"]["trace"]["strategy"] == "vector_graph_evidence_fusion"
    assert result["result"]["graph_paths"]
    assert answer.citations == [item]


@pytest.mark.asyncio
async def test_unsafe_late_native_learning_is_audited_without_reopening_run_events(
    tmp_path: Path,
) -> None:
    recorder = RunEventRecorder()
    change_sets = JsonLearningChangeSetRepository(tmp_path / "changes.json")
    context = RunContext()
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(evidence()),
        event_recorder=recorder,
        change_set_repository=change_sets,
    )
    bridge_id = await bridge.open_run(context)
    await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {"answer_markdown": "Insufficient.", "confidence": "insufficient"},
    )
    await bridge.published_answer(bridge_id)
    await recorder.drain_tools(context.run_id)

    audit_result = await bridge.audit_native_tool(
        bridge_id,
        HermesNativeToolAudit(
            tool_name="memory",
            args={"action": "add", "content": "A durable preference"},
            result='{"success": true}',
        ),
    )

    assert await recorder.drain_tools(context.run_id) == []
    native_changes = await change_sets.list_for_run(context.run_id)
    assert audit_result == {"accepted": False}
    assert native_changes[0].target_type == "hermes_native_learning_blocked_write"
    assert native_changes[0].structured_diff["state"] == "native_write_blocked"
    assert "answer_confidence_insufficient" in native_changes[0].structured_diff[
        "learning_gate_reasons"
    ]


@pytest.mark.asyncio
async def test_unsafe_native_learning_attempts_snapshot_rollback(tmp_path: Path) -> None:
    class RecordingNativeAdmin:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def rollback(
            self,
            snapshot_id: str,
            *,
            expected_after_hash: str,
        ) -> HermesNativeRollbackResult:
            self.calls.append((snapshot_id, expected_after_hash))
            return HermesNativeRollbackResult(
                success=True,
                snapshot_id=snapshot_id,
                state="rolled_back",
            )

    admin = RecordingNativeAdmin()
    changes = JsonLearningChangeSetRepository(tmp_path / "changes.json")
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(evidence()),
        change_set_repository=changes,
        native_admin=admin,  # type: ignore[arg-type]
    )
    context = RunContext()
    bridge_id = await bridge.open_run(context)
    await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {"answer_markdown": "Insufficient.", "confidence": "insufficient"},
    )
    after_hash = "c" * 64

    result = await bridge.audit_native_tool(
        bridge_id,
        HermesNativeToolAudit(
            tool_name="memory",
            args={"action": "add", "content": "unsafe automatic memory"},
            result='{"success": true}',
            snapshot=HermesNativeSnapshotAudit(
                snapshot_id="a" * 32,
                target_kind="memory",
                target_id="memory",
                before_hash="b" * 64,
                after_hash=after_hash,
                applied=True,
                rollback_supported=True,
            ),
        ),
    )
    blocked = await changes.list_for_run(context.run_id)

    assert result == {"accepted": False}
    assert admin.calls == [("a" * 32, after_hash)]
    assert blocked[0].structured_diff["rollback"]["state"] == "rolled_back"
    assert blocked[0].evaluation_report["status"] == "non_learnable"


@pytest.mark.asyncio
async def test_safe_grounded_native_learning_remains_auditable(tmp_path: Path) -> None:
    item = evidence()
    changes = JsonLearningChangeSetRepository(tmp_path / "changes.json")
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(item),
        change_set_repository=changes,
    )
    context = RunContext()
    bridge_id = await bridge.open_run(context)
    await bridge.invoke(bridge_id, "search_knowledge", {"query": "HermesGraph"})
    await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {
            "answer_markdown": "Supported.",
            "citation_ids": [str(item.evidence_id)],
            "confidence": "supported",
        },
    )
    await bridge.complete(bridge_id)

    result = await bridge.audit_native_tool(
        bridge_id,
        HermesNativeToolAudit(
            tool_name="memory",
            args={"action": "add", "content": "safe reflected preference"},
            result='{"success": true}',
        ),
    )
    native_changes = await changes.list_for_run(context.run_id)

    assert result == {"accepted": True}
    assert native_changes[0].target_type == "hermes_native_memory"
    assert native_changes[0].evaluation_report["status"] == "requires_audit"


@pytest.mark.asyncio
async def test_native_learning_fails_closed_until_the_bridge_is_completed(
    tmp_path: Path,
) -> None:
    item = evidence()
    changes = JsonLearningChangeSetRepository(tmp_path / "changes.json")
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(item),
        change_set_repository=changes,
    )
    context = RunContext()
    bridge_id = await bridge.open_run(context)
    await bridge.invoke(bridge_id, "search_knowledge", {"query": "HermesGraph"})
    await bridge.invoke(
        bridge_id,
        "hermesgraph_publish_answer",
        {
            "answer_markdown": "Supported.",
            "citation_ids": [str(item.evidence_id)],
            "confidence": "supported",
        },
    )

    result = await bridge.audit_native_tool(
        bridge_id,
        HermesNativeToolAudit(
            tool_name="memory",
            args={"action": "add", "content": "must wait for terminal completion"},
            result='{"success": true}',
        ),
    )
    blocked = await changes.list_for_run(context.run_id)

    assert result == {"accepted": False}
    assert blocked[0].target_type == "hermes_native_learning_blocked_write"
    assert "run_not_completed" in blocked[0].evaluation_report[
        "learning_gate_reasons"
    ]


@pytest.mark.asyncio
async def test_background_review_completion_releases_bridge_waiter() -> None:
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(evidence()),
    )
    bridge_id = await bridge.open_run(RunContext())
    waiter = asyncio.create_task(bridge.wait_for_native_review_completion(bridge_id))

    await bridge.audit_native_tool(
        bridge_id,
        HermesNativeToolAudit(
            tool_name="hermes_background_review_completed",
            status="ok",
        ),
    )

    await waiter


def test_bridge_uses_constant_time_token_boundary() -> None:
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="correct-token"),
        retrieval=FixtureRetrieval(evidence()),
    )

    assert bridge.is_authorized("correct-token") is True
    assert bridge.is_authorized("wrong-token") is False


@pytest.mark.asyncio
async def test_bridge_activates_only_the_exact_governed_skill_pinned_for_the_run(
    tmp_path: Path,
) -> None:
    skills = SkillMarkdownRepository(tmp_path / "skills")
    skill = SkillDefinition(
        name="learned_graph_lookup",
        version="1.2.0",
        description="Retrieve an evidence-backed graph path for relationship questions.",
        status=SkillStatus.ACTIVE,
        trigger_intents=["relationship"],
        steps=[
            SkillStep(
                action="retrieve_evidence_subgraph",
                purpose="Retrieve source passages and an evidence-backed subgraph.",
                inputs={"max_hops": 2},
            )
        ],
        allowed_capabilities=["retrieve_evidence_subgraph"],
        constraints={"max_tool_calls": 1, "requires_evidence_validation": True},
        source_run_ids=[uuid4()],
    )
    await skills.save(skill)
    context = RunContext(skill_versions={skill.name: skill.version})
    recorder = RunEventRecorder()
    bridge = HermesCapabilityBridge(
        settings=Settings(app_env="test", hermes_bridge_token="bridge-secret"),
        retrieval=FixtureRetrieval(evidence()),
        skill_repository=skills,
        event_recorder=recorder,
    )
    bridge_id = await bridge.open_run(context)

    result = await bridge.invoke(
        bridge_id,
        "activate_governed_skill",
        {"name": skill.name, "version": skill.version},
    )
    events = await recorder.drain_tools(context.run_id)

    assert result["result"]["steps"][0]["action"] == "retrieve_evidence_subgraph"
    assert events[0].output_summary == "skill=learned_graph_lookup@1.2.0,steps=1"

    other_context = RunContext(skill_versions={skill.name: skill.version})
    other_bridge_id = await bridge.open_run(other_context)
    with pytest.raises(HermesBridgeError, match="differs from the run snapshot"):
        await bridge.invoke(
            other_bridge_id,
            "activate_governed_skill",
            {"name": skill.name, "version": "1.1.0"},
        )
