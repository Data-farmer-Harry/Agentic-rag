from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from app.harness.models import (
    HarnessExperienceEntry,
    HarnessExperienceEvaluation,
    HarnessPattern,
    HarnessPatternEvaluation,
    HarnessPatternPromotionEvidence,
    HarnessPatternStatus,
    HarnessPatternTransition,
    RunHarnessOverlay,
)


class HarnessExperienceConflictError(ValueError):
    pass


class JsonHarnessExperienceRepository:
    """Append-only local store with immutable identity conflict checks."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def save(self, experience: HarnessExperienceEntry) -> HarnessExperienceEntry:
        async with self._lock:
            experiences, _ = self._read_all()
            existing = experiences.get(experience.experience_id)
            if existing is not None:
                if existing.payload_hash != experience.payload_hash:
                    raise HarnessExperienceConflictError(
                        f"Experience {experience.experience_id} already has different content"
                    )
                return existing
            self._append("experience", experience.model_dump(mode="json"))
            return experience

    async def get(
        self,
        experience_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> HarnessExperienceEntry | None:
        experiences, _ = self._read_all()
        item = experiences.get(experience_id)
        if item is None or item.tenant_id != tenant_id or item.project_id != project_id:
            return None
        return item

    async def list_scoped(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 100,
        learnable: bool | None = None,
        success: bool | None = None,
    ) -> Sequence[HarnessExperienceEntry]:
        _validate_limit(limit)
        experiences, _ = self._read_all()
        selected = [
            item
            for item in experiences.values()
            if item.tenant_id == tenant_id
            and item.project_id == project_id
            and (learnable is None or item.diagnosis.learnable == learnable)
            and (success is None or item.diagnosis.success == success)
        ]
        return sorted(
            selected,
            key=lambda item: (item.created_at, str(item.experience_id)),
            reverse=True,
        )[:limit]

    async def list_for_run(
        self,
        run_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> Sequence[HarnessExperienceEntry]:
        items = await self.list_scoped(
            tenant_id=tenant_id,
            project_id=project_id,
            limit=500,
        )
        return [item for item in items if item.run_id == run_id]

    async def save_evaluation(
        self,
        evaluation: HarnessExperienceEvaluation,
    ) -> HarnessExperienceEvaluation:
        async with self._lock:
            _, evaluations = self._read_all()
            existing = evaluations.get(evaluation.evaluation_id)
            if existing is not None:
                if existing.payload_hash != evaluation.payload_hash:
                    raise HarnessExperienceConflictError(
                        f"Evaluation {evaluation.evaluation_id} already has different content"
                    )
                return existing
            self._append("evaluation", evaluation.model_dump(mode="json"))
            return evaluation

    async def get_evaluation(
        self,
        evaluation_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> HarnessExperienceEvaluation | None:
        _, evaluations = self._read_all()
        item = evaluations.get(evaluation_id)
        if item is None or item.tenant_id != tenant_id or item.project_id != project_id:
            return None
        return item

    async def list_evaluations(
        self,
        experience_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> Sequence[HarnessExperienceEvaluation]:
        _, evaluations = self._read_all()
        selected = [
            item
            for item in evaluations.values()
            if item.experience_id == experience_id
            and item.tenant_id == tenant_id
            and item.project_id == project_id
        ]
        signal_order = {"run_outcome": 0, "explicit_feedback": 1}
        return sorted(
            selected,
            key=lambda item: (
                item.created_at,
                signal_order[item.signal_kind],
                str(item.evaluation_id),
            ),
        )

    def _append(self, kind: str, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        envelope = {"kind": kind, "payload": payload}
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )

    def _read_all(
        self,
    ) -> tuple[
        dict[UUID, HarnessExperienceEntry],
        dict[UUID, HarnessExperienceEvaluation],
    ]:
        experiences: dict[UUID, HarnessExperienceEntry] = {}
        evaluations: dict[UUID, HarnessExperienceEvaluation] = {}
        if not self._path.exists():
            return experiences, evaluations
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            envelope = json.loads(line)
            if envelope.get("kind") == "experience":
                experience = HarnessExperienceEntry.model_validate(envelope["payload"])
                experiences[experience.experience_id] = experience
            elif envelope.get("kind") == "evaluation":
                evaluation = HarnessExperienceEvaluation.model_validate(
                    envelope["payload"]
                )
                evaluations[evaluation.evaluation_id] = evaluation
        return experiences, evaluations


class JsonHarnessPolicyRepository:
    """Append-only Pattern/Overlay store for local and offline operation."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    async def save_pattern(self, pattern: HarnessPattern) -> HarnessPattern:
        async with self._lock:
            patterns, _, _, _, _ = self._read_all()
            key = (pattern.pattern_id, pattern.version)
            existing = patterns.get(key)
            if existing is not None:
                if existing.payload_hash != pattern.payload_hash:
                    raise HarnessExperienceConflictError(
                        f"Pattern {pattern.pattern_id}@{pattern.version} "
                        "already has different content"
                    )
                return existing
            self._append("pattern", pattern.model_dump(mode="json"))
            return pattern

    async def get_pattern(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        version: str | None = None,
    ) -> HarnessPattern | None:
        patterns, _, _, _, _ = self._read_all()
        candidates = [
            item
            for (identity, item_version), item in patterns.items()
            if identity == pattern_id
            and (version is None or item_version == version)
            and item.tenant_id == tenant_id
            and item.project_id == project_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: _semver(item.version))

    async def list_patterns(
        self,
        *,
        tenant_id: str,
        project_id: str,
        limit: int = 100,
        status: HarnessPatternStatus | None = None,
    ) -> Sequence[HarnessPattern]:
        _validate_limit(limit)
        patterns, _, _, _, _ = self._read_all()
        selected = [
            item
            for item in patterns.values()
            if item.tenant_id == tenant_id
            and item.project_id == project_id
            and (status is None or item.status == status)
        ]
        return sorted(
            selected,
            key=lambda item: (
                item.created_at,
                _semver(item.version),
                str(item.pattern_id),
            ),
            reverse=True,
        )[:limit]

    async def save_pattern_evaluation(
        self,
        evaluation: HarnessPatternEvaluation,
    ) -> HarnessPatternEvaluation:
        async with self._lock:
            patterns, _, evaluations, _, _ = self._read_all()
            pattern = patterns.get((evaluation.pattern_id, evaluation.pattern_version))
            if pattern is None:
                raise ValueError("Pattern evaluation references a missing pattern")
            if (
                pattern.tenant_id != evaluation.tenant_id
                or pattern.project_id != evaluation.project_id
                or pattern.payload_hash != evaluation.pattern_payload_hash
            ):
                raise ValueError("Pattern evaluation scope or definition hash mismatch")
            existing = evaluations.get(evaluation.evaluation_id)
            if existing is not None:
                if existing.payload_hash != evaluation.payload_hash:
                    raise HarnessExperienceConflictError(
                        f"Pattern evaluation {evaluation.evaluation_id} "
                        "already has different content"
                    )
                return existing
            self._append("pattern_evaluation", evaluation.model_dump(mode="json"))
            return evaluation

    async def list_pattern_evaluations(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternEvaluation]:
        _, _, evaluations, _, _ = self._read_all()
        selected = [
            item
            for item in evaluations.values()
            if item.pattern_id == pattern_id
            and item.tenant_id == tenant_id
            and item.project_id == project_id
            and (pattern_version is None or item.pattern_version == pattern_version)
        ]
        return sorted(
            selected,
            key=lambda item: (item.generated_at, str(item.evaluation_id)),
        )

    async def save_pattern_promotion_evidence(
        self,
        evidence: HarnessPatternPromotionEvidence,
    ) -> HarnessPatternPromotionEvidence:
        async with self._lock:
            patterns, _, evaluations, promotion_evidence, _ = self._read_all()
            pattern = patterns.get((evidence.pattern_id, evidence.pattern_version))
            evaluation = evaluations.get(evidence.evaluation_id)
            if pattern is None or evaluation is None:
                raise ValueError(
                    "Pattern promotion evidence references a missing parent"
                )
            if (
                pattern.tenant_id != evidence.tenant_id
                or pattern.project_id != evidence.project_id
                or evaluation.pattern_id != evidence.pattern_id
                or evaluation.pattern_version != evidence.pattern_version
                or evaluation.payload_hash != evidence.evaluation_payload_hash
            ):
                raise ValueError("Pattern promotion evidence scope or hash mismatch")
            existing = promotion_evidence.get(evidence.evidence_id)
            if existing is not None:
                if existing.payload_hash != evidence.payload_hash:
                    raise HarnessExperienceConflictError(
                        f"Pattern promotion evidence {evidence.evidence_id} "
                        "already has different content"
                    )
                return existing
            self._append(
                "pattern_promotion_evidence",
                evidence.model_dump(mode="json"),
            )
            return evidence

    async def list_pattern_promotion_evidence(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternPromotionEvidence]:
        _, _, _, evidence, _ = self._read_all()
        selected = [
            item
            for item in evidence.values()
            if item.pattern_id == pattern_id
            and item.tenant_id == tenant_id
            and item.project_id == project_id
            and (pattern_version is None or item.pattern_version == pattern_version)
        ]
        return sorted(
            selected,
            key=lambda item: (item.generated_at, str(item.evidence_id)),
        )

    async def save_pattern_transition(
        self,
        transition: HarnessPatternTransition,
    ) -> HarnessPatternTransition:
        async with self._lock:
            patterns, _, evaluations, promotion_evidence, transitions = (
                self._read_all()
            )
            pattern = patterns.get((transition.pattern_id, transition.pattern_version))
            if pattern is None:
                raise ValueError("Pattern transition references a missing pattern")
            if (
                pattern.tenant_id != transition.tenant_id
                or pattern.project_id != transition.project_id
            ):
                raise ValueError("Pattern transition scope mismatch")
            if transition.evaluation_id is not None:
                evaluation = evaluations.get(transition.evaluation_id)
                if evaluation is None:
                    raise ValueError("Pattern transition references a missing evaluation")
                if (
                    evaluation.pattern_id != transition.pattern_id
                    or evaluation.pattern_version != transition.pattern_version
                    or evaluation.payload_hash != transition.evaluation_payload_hash
                    or evaluation.tenant_id != transition.tenant_id
                    or evaluation.project_id != transition.project_id
                ):
                    raise ValueError("Pattern transition evaluation mismatch")
            if transition.promotion_evidence_id is not None:
                evidence = promotion_evidence.get(transition.promotion_evidence_id)
                if evidence is None:
                    raise ValueError(
                        "Pattern transition references missing promotion evidence"
                    )
                if (
                    evidence.pattern_id != transition.pattern_id
                    or evidence.pattern_version != transition.pattern_version
                    or evidence.payload_hash
                    != transition.promotion_evidence_payload_hash
                    or evidence.tenant_id != transition.tenant_id
                    or evidence.project_id != transition.project_id
                ):
                    raise ValueError("Pattern transition promotion evidence mismatch")
            existing = transitions.get(transition.transition_id)
            if existing is not None:
                if existing.payload_hash != transition.payload_hash:
                    raise HarnessExperienceConflictError(
                        f"Pattern transition {transition.transition_id} "
                        "already has different content"
                    )
                return existing
            if transition.applied:
                current = _effective_status(
                    pattern,
                    [
                        item
                        for item in transitions.values()
                        if item.pattern_id == transition.pattern_id
                        and item.pattern_version == transition.pattern_version
                        and item.tenant_id == transition.tenant_id
                        and item.project_id == transition.project_id
                    ],
                )
                if current != transition.from_status:
                    raise HarnessExperienceConflictError(
                        "Pattern transition is stale for the current effective status"
                    )
            self._append("pattern_transition", transition.model_dump(mode="json"))
            return transition

    async def list_pattern_transitions(
        self,
        pattern_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
        pattern_version: str | None = None,
    ) -> Sequence[HarnessPatternTransition]:
        _, _, _, _, transitions = self._read_all()
        selected = [
            item
            for item in transitions.values()
            if item.pattern_id == pattern_id
            and item.tenant_id == tenant_id
            and item.project_id == project_id
            and (pattern_version is None or item.pattern_version == pattern_version)
        ]
        return sorted(
            selected,
            key=lambda item: (item.decided_at, str(item.transition_id)),
        )

    async def save_overlay(self, overlay: RunHarnessOverlay) -> RunHarnessOverlay:
        async with self._lock:
            _, overlays, _, _, _ = self._read_all()
            existing = overlays.get(overlay.run_id)
            if existing is not None:
                if existing.payload_hash != overlay.payload_hash:
                    raise HarnessExperienceConflictError(
                        f"Overlay for run {overlay.run_id} already has different content"
                    )
                return existing
            self._append("overlay", overlay.model_dump(mode="json"))
            return overlay

    async def get_overlay(
        self,
        run_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> RunHarnessOverlay | None:
        _, overlays, _, _, _ = self._read_all()
        item = overlays.get(run_id)
        if item is None or item.tenant_id != tenant_id or item.project_id != project_id:
            return None
        return item

    def _append(self, kind: str, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"kind": kind, "payload": payload},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )

    def _read_all(
        self,
    ) -> tuple[
        dict[tuple[UUID, str], HarnessPattern],
        dict[UUID, RunHarnessOverlay],
        dict[UUID, HarnessPatternEvaluation],
        dict[UUID, HarnessPatternPromotionEvidence],
        dict[UUID, HarnessPatternTransition],
    ]:
        patterns: dict[tuple[UUID, str], HarnessPattern] = {}
        overlays: dict[UUID, RunHarnessOverlay] = {}
        evaluations: dict[UUID, HarnessPatternEvaluation] = {}
        promotion_evidence: dict[UUID, HarnessPatternPromotionEvidence] = {}
        transitions: dict[UUID, HarnessPatternTransition] = {}
        if not self._path.exists():
            return patterns, overlays, evaluations, promotion_evidence, transitions
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            envelope = json.loads(line)
            if envelope.get("kind") == "pattern":
                pattern = HarnessPattern.model_validate(envelope["payload"])
                patterns[(pattern.pattern_id, pattern.version)] = pattern
            elif envelope.get("kind") == "overlay":
                overlay = RunHarnessOverlay.model_validate(envelope["payload"])
                overlays[overlay.run_id] = overlay
            elif envelope.get("kind") == "pattern_evaluation":
                evaluation = HarnessPatternEvaluation.model_validate(
                    envelope["payload"]
                )
                evaluations[evaluation.evaluation_id] = evaluation
            elif envelope.get("kind") == "pattern_transition":
                transition = HarnessPatternTransition.model_validate(
                    envelope["payload"]
                )
                transitions[transition.transition_id] = transition
            elif envelope.get("kind") == "pattern_promotion_evidence":
                evidence = HarnessPatternPromotionEvidence.model_validate(
                    envelope["payload"]
                )
                promotion_evidence[evidence.evidence_id] = evidence
        return patterns, overlays, evaluations, promotion_evidence, transitions


def _semver(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")


def _effective_status(
    pattern: HarnessPattern,
    transitions: Sequence[HarnessPatternTransition],
) -> HarnessPatternStatus:
    applied = [item for item in transitions if item.applied]
    if not applied:
        return pattern.status
    return max(
        applied,
        key=lambda item: (item.decided_at, str(item.transition_id)),
    ).to_status
