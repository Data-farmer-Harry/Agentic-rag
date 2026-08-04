from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.bootstrap import build_components
from app.config import get_settings


async def _run() -> int:
    configured = get_settings()
    with TemporaryDirectory(prefix="hermesgraph-reflection-") as directory:
        data_dir = Path(directory)
        settings = configured.model_copy(
            update={
                "app_env": "test",
                "data_dir": data_dir,
                "runtime_mode": "offline",
                "learning_reflector_mode": "openai",
                "learning_reflection_model": (
                    configured.learning_reflection_model or configured.openai_model
                ),
                "learning_reflection_trigger_mode": "all",
                "retrieval_backend": "local",
                "graph_backend": "local",
                "graph_extractor_mode": "rule",
                "knowledge_repository_backend": "local",
                "ingestion_mode": "sync",
                "vision_enabled": False,
            }
        )
        components = build_components(settings)
        try:
            trajectory = await components.run_service.run(
                "Compare deterministic and model-assisted reflection for an Agent run.",
                session_id="reflection-probe",
            )
            changes = await components.change_set_repository.list_all()
            memories = await components.memory_store.list_scoped(
                tenant_id="local",
                project_id="default",
                user_id="local-user",
            )
            evaluation = changes[-1].evaluation_report if changes else {}
            revision = str(evaluation.get("reflector_revision", ""))
            fallback_error = evaluation.get("reflection_fallback_error")
            live = revision.startswith("openai-experience-reflection") and not fallback_error
            print(
                json.dumps(
                    {
                        "status": "live_structured" if live else "deterministic_fallback",
                        "model": settings.learning_reflection_model,
                        "reflector_revision": revision,
                        "fallback_error": fallback_error,
                        "memory_types": sorted(item.memory_type.value for item in memories),
                        "learning_failed": "learning_postprocess_failed" in trajectory.tags,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if live else 1
        finally:
            await components.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
