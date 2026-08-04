from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.agent.conversation_router import (
    ConversationRoutedRuntime,
    OpenAIConversationResponder,
)
from app.agent.model_provider import build_model_client
from app.config import get_settings
from app.evaluation.conversation_routing import (
    ConversationRoutingEvaluator,
    EvaluationAgentRuntime,
    EvaluationConversationHistory,
    load_conversation_routing_golden_set,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate direct-conversation versus Hermes Agent routing"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("examples/evaluation/conversation_routing_golden.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--minimum-pass-rate", type=float, default=0.90)
    return parser


async def _run(args: argparse.Namespace) -> int:
    if not 0 <= args.minimum_pass_rate <= 1:
        raise ValueError("minimum pass rate must be between zero and one")
    settings = get_settings()
    dataset = load_conversation_routing_golden_set(args.dataset)
    history = EvaluationConversationHistory(dataset)
    responder = OpenAIConversationResponder(
        build_model_client(
            settings,
            max_retries=1,
            timeout=settings.conversation_fast_path_timeout_seconds,
        ),
        model=settings.conversation_fast_path_model or settings.openai_model,
    )
    runtime = ConversationRoutedRuntime(
        EvaluationAgentRuntime(),
        direct_responder=responder,
        history_provider=history.provider(),
    )
    try:
        report = await ConversationRoutingEvaluator(
            runtime,
            concurrency=args.concurrency,
        ).run(dataset)
    finally:
        await responder.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(report.model_dump_json(indent=2))
    return int(
        report.pass_rate < args.minimum_pass_rate or report.unsafe_direct_count > 0
    )


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
