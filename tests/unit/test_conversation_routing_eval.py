from pathlib import Path

import pytest

from app.domain.enums import RoutingLane
from app.domain.models import AnswerResponse, RunContext
from app.evaluation.conversation_routing import (
    ConversationRoutingCase,
    ConversationRoutingEvaluator,
    ConversationRoutingGoldenSet,
    load_conversation_routing_golden_set,
)


class LaneRuntime:
    def __init__(self, lanes: dict[str, RoutingLane]) -> None:
        self._lanes = lanes

    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        del context
        return AnswerResponse(
            answer_markdown="sentinel",
            routing_lane=self._lanes[user_input],
        )


@pytest.mark.asyncio
async def test_evaluator_reports_unsafe_direct_and_over_escalation() -> None:
    dataset = ConversationRoutingGoldenSet(
        name="unit",
        revision="v1",
        cases=[
            ConversationRoutingCase(
                case_id="unsafe",
                input="fact",
                expected_lane=RoutingLane.AGENT,
            ),
            ConversationRoutingCase(
                case_id="over",
                input="chat",
                expected_lane=RoutingLane.CONVERSATION,
            ),
            ConversationRoutingCase(
                case_id="pass",
                input="hello",
                expected_lane=RoutingLane.DETERMINISTIC,
            ),
        ],
    )
    runtime = LaneRuntime(
        {
            "fact": RoutingLane.CONVERSATION,
            "chat": RoutingLane.AGENT,
            "hello": RoutingLane.DETERMINISTIC,
        }
    )

    report = await ConversationRoutingEvaluator(runtime, concurrency=2).run(dataset)

    assert report.total == 3
    assert report.passed == 1
    assert report.unsafe_direct_count == 1
    assert report.over_escalation_count == 1
    assert report.confusion == {
        "agent->conversation": 1,
        "conversation->agent": 1,
        "deterministic->deterministic": 1,
    }


def test_default_conversation_routing_dataset_loads() -> None:
    dataset = load_conversation_routing_golden_set(
        Path("examples/evaluation/conversation_routing_golden.json")
    )

    assert len(dataset.cases) >= 20
    assert {case.expected_lane for case in dataset.cases} == set(RoutingLane)
