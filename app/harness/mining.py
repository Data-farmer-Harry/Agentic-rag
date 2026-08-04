from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain.contracts import HarnessExperienceRepository, HarnessPolicyRepository
from app.harness.features import canonical_json_hash
from app.harness.models import (
    HarnessConfigDelta,
    HarnessContextConfig,
    HarnessDimension,
    HarnessExperienceEntry,
    HarnessGenerationConfig,
    HarnessOrchestrationConfig,
    HarnessOutputConfig,
    HarnessPattern,
    HarnessPatternStatus,
    HarnessReasonCode,
    HarnessToolConfig,
    HarnessTriggerPredicate,
    canonical_hash,
)

PATTERN_MINER_REVISION = "deterministic-pattern-miner-v1"
PATTERN_EVALUATOR_REVISION = "pending-offline-evaluation-v1"


@dataclass(frozen=True, slots=True)
class _PatternTemplate:
    key: str
    name: str
    dimension: HarnessDimension
    delta: HarnessConfigDelta


_TEMPLATES: dict[HarnessReasonCode, _PatternTemplate] = {
    HarnessReasonCode.COMPARE_BRANCH_MISSING: _PatternTemplate(
        key="compare-retrieval-profile",
        name="Use compare retrieval profile for comparison tasks",
        dimension=HarnessDimension.ORCHESTRATION,
        delta=HarnessConfigDelta(
            orchestration=HarnessOrchestrationConfig(
                retrieval_profile="compare",
                max_subqueries=2,
            )
        ),
    ),
    HarnessReasonCode.GRAPH_FOLLOWUP_MISSING: _PatternTemplate(
        key="bounded-graph-followup",
        name="Allow a bounded graph follow-up",
        dimension=HarnessDimension.TOOL,
        delta=HarnessConfigDelta(
            tool=HarnessToolConfig(
                graph_hops=2,
            )
        ),
    ),
    HarnessReasonCode.PUBLIC_SOURCE_OVERREPRESENTED: _PatternTemplate(
        key="private-evidence-quota",
        name="Reserve evidence capacity for private context",
        dimension=HarnessDimension.CONTEXT,
        delta=HarnessConfigDelta(
            context=HarnessContextConfig(private_evidence_quota=3)
        ),
    ),
    HarnessReasonCode.CITATION_COVERAGE_BELOW_THRESHOLD: _PatternTemplate(
        key="strict-citation-coverage",
        name="Require supported claims and complete citations",
        dimension=HarnessDimension.OUTPUT,
        delta=HarnessConfigDelta(
            generation=HarnessGenerationConfig(answer_style="balanced"),
            output=HarnessOutputConfig(
                minimum_citation_coverage=0.9,
                claim_support_mode="supported",
                insufficient_evidence_behavior="retrieve_again",
            ),
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class PatternMiningResult:
    candidates: tuple[HarnessPattern, ...]
    created: int
    unchanged: int


class DeterministicPatternMiner:
    """Mine conservative Draft patterns from repeated, learnable failures."""

    def __init__(
        self,
        experiences: HarnessExperienceRepository,
        policies: HarnessPolicyRepository,
        *,
        repeated_failure_threshold: int = 3,
        min_cluster_size: int = 5,
    ) -> None:
        self._experiences = experiences
        self._policies = policies
        self._failure_threshold = repeated_failure_threshold
        self._min_cluster_size = min_cluster_size

    async def mine_scope(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> PatternMiningResult:
        experiences = list(
            await self._experiences.list_scoped(
                tenant_id=tenant_id,
                project_id=project_id,
                limit=500,
                learnable=True,
            )
        )
        existing = list(
            await self._policies.list_patterns(
                tenant_id=tenant_id,
                project_id=project_id,
                limit=500,
            )
        )
        latest = _latest_patterns(existing)
        buckets: dict[
            tuple[str, str, HarnessReasonCode, bool, bool, bool],
            list[HarnessExperienceEntry],
        ] = defaultdict(list)
        for experience in experiences:
            if experience.diagnosis.success or _is_social(experience):
                continue
            if not experience.diagnosis.reason_codes:
                continue
            intent = _primary_intent(experience)
            for reason in experience.diagnosis.reason_codes:
                if reason not in _TEMPLATES:
                    continue
                buckets[
                    (
                        experience.case_features.domain_pack,
                        intent,
                        reason,
                        experience.case_features.personal_knowledge,
                        experience.case_features.visual,
                        experience.case_features.graph_relations,
                    )
                ].append(experience)

        candidates: list[HarnessPattern] = []
        created = 0
        unchanged = 0
        for bucket, support in sorted(buckets.items(), key=lambda item: str(item[0])):
            if len(support) < self._failure_threshold:
                continue
            domain_pack, intent, reason, personal, visual, graph_relations = bucket
            predicate = HarnessTriggerPredicate(
                domain_pack=domain_pack,
                primary_intent=intent,
                personal_knowledge=True if personal else None,
                visual=True if visual else None,
                graph_relations=True if graph_relations else None,
                required_reason_codes=[reason],
            )
            matching = [
                item
                for item in experiences
                if _matches_predicate(item, predicate)
            ]
            contradictions = [item for item in matching if item.diagnosis.success]
            if (
                len(matching) < self._min_cluster_size
                or len(contradictions) > len(support)
            ):
                continue
            template = _TEMPLATES[reason]
            pattern_id = uuid5(
                NAMESPACE_URL,
                "hermesgraph:harness-pattern:"
                + canonical_json_hash(
                    {
                        "tenant_id": tenant_id,
                        "project_id": project_id,
                        "template": template.key,
                        "predicate": predicate.model_dump(mode="json"),
                        "delta": template.delta.model_dump(mode="json"),
                    }
                ),
            )
            previous = latest.get(pattern_id)
            support_ids = sorted(
                {item.experience_id for item in support},
                key=str,
            )[:500]
            contradiction_ids = sorted(
                {item.experience_id for item in contradictions},
                key=str,
            )[:500]
            if (
                previous is not None
                and previous.supporting_experience_ids == support_ids
                and previous.contradicting_experience_ids == contradiction_ids
            ):
                candidates.append(previous)
                unchanged += 1
                continue
            version = _next_patch(previous.version if previous is not None else None)
            payload = {
                "pattern_id": pattern_id,
                "version": version,
                "parent_version": previous.version if previous is not None else None,
                "tenant_id": tenant_id,
                "project_id": project_id,
                "name": template.name,
                "trigger_predicate": predicate,
                "dimensions": [template.dimension],
                "proposed_delta": template.delta,
                "supporting_experience_ids": support_ids,
                "contradicting_experience_ids": contradiction_ids,
                "support_count": len(support_ids),
                "failure_count": len(support_ids),
                "estimated_quality_lift": 0.0,
                "confidence": round(
                    len(support_ids) / max(1, len(support_ids) + len(contradiction_ids)),
                    4,
                ),
                "status": HarnessPatternStatus.DRAFT,
                "miner_revision": PATTERN_MINER_REVISION,
                "evaluator_revision": PATTERN_EVALUATOR_REVISION,
                "created_at": max(item.created_at for item in support),
            }
            pattern = HarnessPattern.model_validate(
                {**payload, "payload_hash": canonical_hash(payload)}
            )
            stored = await self._policies.save_pattern(pattern)
            latest[pattern_id] = stored
            candidates.append(stored)
            created += 1
        return PatternMiningResult(
            candidates=tuple(candidates),
            created=created,
            unchanged=unchanged,
        )


def _latest_patterns(patterns: list[HarnessPattern]) -> dict[UUID, HarnessPattern]:
    latest: dict[UUID, HarnessPattern] = {}
    for pattern in patterns:
        current = latest.get(pattern.pattern_id)
        if current is None or _semver(pattern.version) > _semver(current.version):
            latest[pattern.pattern_id] = pattern
    return latest


def _matches_predicate(
    experience: HarnessExperienceEntry,
    predicate: HarnessTriggerPredicate,
) -> bool:
    features = experience.case_features
    if features.domain_pack != predicate.domain_pack:
        return False
    if _primary_intent(experience) != predicate.primary_intent:
        return False
    for field in ("personal_knowledge", "visual", "graph_relations"):
        expected = getattr(predicate, field)
        if expected is not None and getattr(features, field) != expected:
            return False
    return True


def _is_social(experience: HarnessExperienceEntry) -> bool:
    return experience.case_features.intents == ["social"]


def _primary_intent(experience: HarnessExperienceEntry) -> str:
    return next(
        (intent for intent in experience.case_features.intents if intent != "social"),
        "lookup",
    )


def _next_patch(previous: str | None) -> str:
    if previous is None:
        return "0.1.0"
    major, minor, patch = _semver(previous)
    return f"{major}.{minor}.{patch + 1}"


def _semver(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)
