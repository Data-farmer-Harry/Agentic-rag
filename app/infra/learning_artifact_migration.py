from __future__ import annotations

import hashlib
import json
from typing import Any

from app.domain.contracts import (
    LearningChangeSetRepository,
    MemoryRepository,
    SkillEvaluationRepository,
    SkillObservationRepository,
    SkillRepository,
    SkillTransitionRepository,
)
from app.infra.postgres_learning_artifacts import (
    PostgresLearningArtifactRepository,
)


class LegacyLearningArtifactMigrator:
    """Imports frozen filesystem learning artifacts once without dual writes."""

    def __init__(
        self,
        target: PostgresLearningArtifactRepository,
        *,
        memories: MemoryRepository,
        skills: SkillRepository,
        evaluations: SkillEvaluationRepository,
        observations: SkillObservationRepository,
        transitions: SkillTransitionRepository,
        change_sets: LearningChangeSetRepository,
    ) -> None:
        self._target = target
        self._memories = memories
        self._skills = skills
        self._evaluations = evaluations
        self._observations = observations
        self._transitions = transitions
        self._change_sets = change_sets
        self.last_counts: dict[str, int] | None = None

    async def start(self) -> None:
        memories = tuple(await self._memories.list_all(include_revoked=True))
        skills = tuple(await self._skills.list_all())
        evaluations = tuple(await self._evaluations.list_all())
        observations = tuple(await self._observations.list_all())
        transitions = tuple(await self._transitions.list_all())
        change_sets = tuple(await self._change_sets.list_all())
        snapshot: dict[str, list[dict[str, Any]]] = {
            "memories": [item.model_dump(mode="json") for item in memories],
            "skills": [item.model_dump(mode="json") for item in skills],
            "evaluations": [item.model_dump(mode="json") for item in evaluations],
            "observations": [item.model_dump(mode="json") for item in observations],
            "transitions": [item.model_dump(mode="json") for item in transitions],
            "change_sets": [item.model_dump(mode="json") for item in change_sets],
        }
        digest = hashlib.sha256(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.last_counts = await self._target.import_legacy(
            import_key=f"filesystem-learning-v2:{digest}",
            memories=memories,
            skills=skills,
            evaluations=evaluations,
            observations=observations,
            transitions=transitions,
            change_sets=change_sets,
        )

    async def close(self) -> None:
        return None


__all__ = ["LegacyLearningArtifactMigrator"]
