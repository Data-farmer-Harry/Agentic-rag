import pytest

from app.agent.offline_runtime import OfflineAgentRuntime
from app.application.run_event_recorder import RunEventRecorder
from app.domain.enums import EvidenceLevel, TrustLevel
from app.domain.models import EvidenceRef, Provenance, RetrievalBundle, RunContext


class FixtureRetrieval:
    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: dict | None = None,
        top_k: int = 10,
    ) -> RetrievalBundle:
        return RetrievalBundle(
            query=query,
            evidence=[
                EvidenceRef(
                    text="HermesGraph keeps one online agent loop.",
                    provenance=Provenance(
                        source_type="fixture",
                        source_id="intent",
                        trust=TrustLevel.VERIFIED,
                    ),
                )
            ],
        )


@pytest.mark.asyncio
async def test_offline_runtime_produces_valid_supported_answer() -> None:
    recorder = RunEventRecorder()
    runtime = OfflineAgentRuntime(FixtureRetrieval(), event_recorder=recorder)
    context = RunContext()
    answer = await runtime.run("What is the runtime boundary?", context)

    assert answer.confidence == EvidenceLevel.SUPPORTED
    assert len(answer.citations) == 1
    assert answer.claims[0].evidence_ids == [answer.citations[0].evidence_id]
    events = await recorder.drain_tools(context.run_id)
    assert [event.tool_name for event in events] == ["search_knowledge"]
