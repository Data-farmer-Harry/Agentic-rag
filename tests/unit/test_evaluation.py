import pytest

from app.domain.enums import EvidenceLevel
from app.domain.models import AnswerResponse, RunContext
from app.evaluation.replay import GoldenCase, ReplayRunner


class StaticRuntime:
    async def run(self, user_input: str, context: RunContext) -> AnswerResponse:
        return AnswerResponse(
            answer_markdown="One root Runner controls the online agent loop.",
            confidence=EvidenceLevel.INSUFFICIENT,
        )


@pytest.mark.asyncio
async def test_replay_runner_returns_machine_readable_gate() -> None:
    report = await ReplayRunner(StaticRuntime()).run(
        [
            GoldenCase(
                case_id="runtime-boundary",
                input="Which component owns the loop?",
                expected_terms=["root Runner"],
                forbidden_terms=["LangChain agent"],
            )
        ]
    )

    assert report.passed == 1
    assert report.score == 1.0
