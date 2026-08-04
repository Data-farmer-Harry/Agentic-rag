from __future__ import annotations

import argparse
import asyncio
import json

from app.bootstrap import build_components
from app.harness.mining import DeterministicPatternMiner


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Mine deterministic Draft harness patterns without model calls."
    )
    value.add_argument("--tenant-id", default="local")
    value.add_argument("--project-id", default="default")
    return value


async def run(args: argparse.Namespace) -> int:
    components = build_components()
    await components.start()
    try:
        miner = DeterministicPatternMiner(
            components.harness_experience_repository,
            components.harness_policy_repository,
            repeated_failure_threshold=(
                components.settings.harness_repeated_failure_threshold
            ),
            min_cluster_size=components.settings.harness_min_cluster_size,
        )
        result = await miner.mine_scope(
            tenant_id=args.tenant_id,
            project_id=args.project_id,
        )
        print(
            json.dumps(
                {
                    "candidates": len(result.candidates),
                    "created": result.created,
                    "unchanged": result.unchanged,
                    "patterns": [
                        {
                            "pattern_id": str(item.pattern_id),
                            "version": item.version,
                            "status": item.status.value,
                            "support_count": item.support_count,
                            "contradiction_count": len(
                                item.contradicting_experience_ids
                            ),
                            "confidence": item.confidence,
                        }
                        for item in result.candidates
                    ],
                    "model_calls": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        await components.close()


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
