from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from uuid import uuid4

from app.domain.enums import RunStatus
from app.domain.models import LearningJob, RunContext, RunTrajectory, utc_now
from app.infra.postgres import PostgresDatabase
from app.infra.postgres_learning_jobs import PostgresLearningJobRepository


async def check(
    dsn: str,
    *,
    count: int,
    timeout_seconds: float,
    keep: bool,
) -> dict[str, object]:
    database = PostgresDatabase(dsn, min_pool_size=1, max_pool_size=2)
    repository = PostgresLearningJobRepository(database, manage_database=True)
    await repository.start()
    project_id = f"worker-smoke-{uuid4().hex[:12]}"
    try:
        job_ids = []
        for index in range(count):
            trajectory = RunTrajectory(
                context=RunContext(
                    project_id=project_id,
                    user_id="worker-smoke",
                ),
                user_input=f"Deterministic durable worker smoke item {index}.",
                status=RunStatus.COMPLETED,
                completed_at=utc_now(),
            )
            idempotency_key = hashlib.sha256(
                f"{project_id}:{trajectory.context.run_id}".encode()
            ).hexdigest()
            job = LearningJob(
                idempotency_key=idempotency_key,
                project_id=project_id,
                user_id="worker-smoke",
                run_id=trajectory.context.run_id,
                trigger="run_completed",
                trajectory=trajectory,
            )
            queued, _ = await repository.enqueue(job)
            job_ids.append(queued.job_id)

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            jobs = list(
                await repository.list_scoped(
                    tenant_id="local",
                    project_id=project_id,
                    limit=max(100, count),
                )
            )
            if len(jobs) == count and all(
                item.status.value in {"succeeded", "failed", "cancelled"}
                for item in jobs
            ):
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Learning worker smoke jobs did not finish in time")
            await asyncio.sleep(0.1)

        return {
            "project_id": project_id,
            "count": len(jobs),
            "statuses": sorted(item.status.value for item in jobs),
            "attempts": sorted(item.attempt for item in jobs),
            "checkpoint_stages": sorted(
                item.checkpoint.stage if item.checkpoint is not None else "missing"
                for item in jobs
            ),
            "job_ids": sorted(str(item) for item in job_ids),
        }
    finally:
        if not keep:
            async with database.pool.acquire() as connection, connection.transaction():
                for table in (
                    "learning_change_sets",
                    "learning_skill_transitions",
                    "learning_skill_observations",
                    "learning_skill_evaluations",
                    "learning_skills",
                    "learning_memories",
                    "learning_jobs",
                ):
                    await connection.execute(
                        f"DELETE FROM {table} WHERE project_id = $1",
                        project_id,
                    )
        await repository.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if not 2 <= args.count <= 100:
        raise SystemExit("--count must be between 2 and 100")
    result = asyncio.run(
        check(
            args.dsn,
            count=args.count,
            timeout_seconds=args.timeout_seconds,
            keep=args.keep,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
