import argparse
import asyncio

from app.bootstrap import build_components


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a HermesGraph task")
    parser.add_argument("task", help="Task or question to run")
    parser.add_argument("--domain-pack", default="general")
    parser.add_argument("--session-id", default="cli")
    return parser


async def _run() -> None:
    args = _parser().parse_args()
    components = build_components()
    trajectory = await components.run_service.run(
        args.task,
        session_id=args.session_id,
        domain_pack=args.domain_pack,
    )
    if trajectory.answer is None:
        raise RuntimeError("Run completed without an answer")
    print(trajectory.answer.model_dump_json(indent=2, exclude_none=True))


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
