from __future__ import annotations

import argparse
import asyncio
import json

from app.bootstrap import build_components
from app.domain.enums import RunStatus
from app.harness.experience import assemble_evaluation, assemble_experience


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Backfill observe-only harness experiences from terminal trajectories."
    )
    value.add_argument("--tenant-id", default="local")
    value.add_argument("--project-id", default="default")
    value.add_argument("--limit", type=int, default=500)
    value.add_argument("--dry-run", action="store_true")
    return value


async def run(args: argparse.Namespace) -> int:
    if not 1 <= args.limit <= 10_000:
        raise ValueError("--limit must be between 1 and 10000")
    components = build_components()
    await components.start()
    try:
        trajectories = await components.trajectory_repository.list_recent(
            tenant_id=args.tenant_id,
            project_id=args.project_id,
            limit=args.limit,
        )
        terminal = [
            item
            for item in trajectories
            if item.status
            in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
        ]
        created = 0
        evaluations_created = 0
        conflicts = 0
        unlearnable = 0
        for index, trajectory in enumerate(terminal, start=1):
            try:
                if args.dry_run:
                    experience = assemble_experience(trajectory)
                    assemble_evaluation(
                        trajectory,
                        experience,
                        trigger=(
                            "feedback_received"
                            if trajectory.feedback_score is not None
                            else "run_completed"
                        ),
                        native_change_set_ids=[],
                    )
                    created += 1
                elif components.harness_experience_service is not None:
                    outcome = await components.harness_experience_service.collect(
                        trajectory,
                        trigger=(
                            "feedback_received"
                            if trajectory.feedback_score is not None
                            else "run_completed"
                        ),
                    )
                    created += int(outcome.experience_created)
                    evaluations_created += int(outcome.evaluation_created)
                    unlearnable += int(not outcome.experience.diagnosis.learnable)
            except ValueError:
                conflicts += 1
            if index % 25 == 0 or index == len(terminal):
                print(
                    f"\rHarness {index}/{len(terminal)} created={created} "
                    f"evaluations={evaluations_created} conflicts={conflicts}",
                    end="",
                    flush=True,
                )
        if terminal:
            print()
        print(
            json.dumps(
                {
                    "trajectories_selected": len(terminal),
                    "experiences_created": created,
                    "evaluations_created": evaluations_created,
                    "unlearnable": unlearnable,
                    "conflicts": conflicts,
                    "dry_run": args.dry_run,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if conflicts else 0
    finally:
        await components.close()


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
