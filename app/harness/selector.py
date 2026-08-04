from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from app.domain.contracts import HarnessExperienceRepository, HarnessPolicyRepository
from app.domain.models import RunContext, RunTrajectory
from app.harness.consumer import (
    BoundedHarnessConsumer,
    stable_canary_assignment,
)
from app.harness.features import build_case_features
from app.harness.models import (
    CaseFeatures,
    HarnessConfigDelta,
    HarnessOverlayMode,
    HarnessPattern,
    HarnessPatternStatus,
    HarnessTriggerPredicate,
    RunHarnessOverlay,
    canonical_hash,
)

SELECTOR_REVISION = "deterministic-governed-selector-v2"
EXPERIENCE_BANK_REVISION = "experience-bank-v1"
PATTERN_BANK_REVISION = "pattern-bank-v2"


class HarnessOverlaySelector:
    """Create a frozen observe/shadow overlay without mutating runtime behavior."""

    def __init__(
        self,
        experiences: HarnessExperienceRepository,
        policies: HarnessPolicyRepository,
        *,
        mode: HarnessOverlayMode,
        max_patterns: int = 3,
        consumer: BoundedHarnessConsumer | None = None,
        canary_percentage: int = 10,
    ) -> None:
        if not 0 <= canary_percentage <= 100:
            raise ValueError("canary_percentage must be between 0 and 100")
        self._experiences = experiences
        self._policies = policies
        self._mode = mode
        self._max_patterns = max_patterns
        self._consumer = consumer or BoundedHarnessConsumer()
        self._canary_percentage = canary_percentage

    async def select(
        self,
        *,
        context: RunContext,
        query: str,
        baseline_policy_versions: dict[str, str],
    ) -> RunHarnessOverlay | None:
        if self._mode == HarnessOverlayMode.DISABLED:
            return None
        existing = await self._policies.get_overlay(
            context.run_id,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
        )
        if existing is not None:
            return existing
        features = build_case_features(
            RunTrajectory(context=context, user_input=query)
        )
        experiences = list(
            await self._experiences.list_scoped(
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                limit=500,
                learnable=True,
            )
        )
        ranked = sorted(
            (
                (_experience_similarity(features, item.case_features), item)
                for item in experiences
            ),
            key=lambda item: (item[0], item[1].created_at),
            reverse=True,
        )
        positive_ids = [
            item.experience_id
            for score, item in ranked
            if score > 0 and item.diagnosis.success
        ][:3]
        negative_ids = [
            item.experience_id
            for score, item in ranked
            if score > 0 and not item.diagnosis.success
        ][:3]
        selected, conflicts = await self._select_patterns(context, features)
        predicted = _merge_pattern_deltas(selected)
        if self._mode in {HarnessOverlayMode.CANARY, HarnessOverlayMode.ACTIVE}:
            projection = self._consumer.project(predicted)
            effective = projection.effective_delta
            clamped_fields = list(projection.clamped_fields)
            conflicts.extend(
                f"consumer_rejected:{item}" for item in projection.rejected_fields
            )
        else:
            effective = predicted
            clamped_fields = []
        conflicts = conflicts[:50]
        selected_versions = [
            f"{item.pattern_id}@{item.version}" for item in selected
        ]
        created_at = datetime.now(UTC)
        overlay_id = uuid5(
            NAMESPACE_URL,
            f"hermesgraph:harness-overlay:{context.run_id}:{self._mode.value}:"
            + ",".join(selected_versions),
        )
        payload = {
            "overlay_id": overlay_id,
            "run_id": context.run_id,
            "tenant_id": context.tenant_id,
            "project_id": context.project_id,
            "baseline_policy_versions": baseline_policy_versions,
            "selected_pattern_versions": selected_versions,
            "positive_experience_ids": positive_ids,
            "negative_experience_ids": negative_ids,
            "effective_delta": effective,
            "clamped_fields": clamped_fields,
            "rejected_conflicts": conflicts,
            "selection_trace_codes": [
                f"mode:{self._mode.value}",
                f"positive_neighbors:{len(positive_ids)}",
                f"negative_neighbors:{len(negative_ids)}",
                f"patterns:{len(selected)}",
            ],
            "selector_revision": SELECTOR_REVISION,
            "experience_bank_revision": EXPERIENCE_BANK_REVISION,
            "pattern_bank_revision": PATTERN_BANK_REVISION,
            "mode": self._mode,
            "created_at": created_at,
            "expires_at": created_at + timedelta(hours=24),
        }
        overlay = RunHarnessOverlay.model_validate(
            {**payload, "payload_hash": canonical_hash(payload)}
        )
        return await self._policies.save_overlay(overlay)

    async def _select_patterns(
        self,
        context: RunContext,
        features: CaseFeatures,
    ) -> tuple[list[HarnessPattern], list[str]]:
        allowed = _allowed_statuses(self._mode)
        if not allowed:
            return [], []
        all_patterns = await self._policies.list_patterns(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            limit=500,
        )
        latest: dict[object, HarnessPattern] = {}
        for pattern in all_patterns:
            current = latest.get(pattern.pattern_id)
            if current is None or _semver(pattern.version) > _semver(current.version):
                latest[pattern.pattern_id] = pattern
        eligible: list[HarnessPattern] = []
        rollout_rejections: list[str] = []
        for pattern in latest.values():
            status = await self._effective_status(pattern)
            if status not in allowed or not _matches_features(
                features,
                pattern.trigger_predicate,
            ):
                continue
            version = f"{pattern.pattern_id}@{pattern.version}"
            if (
                self._mode == HarnessOverlayMode.CANARY
                and status == HarnessPatternStatus.CANARY
                and not stable_canary_assignment(
                    context=context,
                    pattern_version=version,
                    percentage=self._canary_percentage,
                )
            ):
                rollout_rejections.append(f"canary_not_assigned:{version}")
                continue
            eligible.append(pattern)
        eligible.sort(
            key=lambda item: (
                item.confidence,
                item.support_count,
                _semver(item.version),
            ),
            reverse=True,
        )
        selected: list[HarnessPattern] = []
        claimed_dimensions: set[str] = set()
        conflicts: list[str] = []
        for pattern in eligible:
            dimensions = {item.value for item in pattern.dimensions}
            overlap = dimensions & claimed_dimensions
            if overlap:
                conflicts.append(
                    f"dimension_conflict:{pattern.pattern_id}:{','.join(sorted(overlap))}"
                )
                continue
            selected.append(pattern)
            claimed_dimensions.update(dimensions)
            if len(selected) >= self._max_patterns:
                break
        return selected, [*rollout_rejections, *conflicts][:50]

    async def _effective_status(
        self,
        pattern: HarnessPattern,
    ) -> HarnessPatternStatus:
        transitions = await self._policies.list_pattern_transitions(
            pattern.pattern_id,
            tenant_id=pattern.tenant_id,
            project_id=pattern.project_id,
            pattern_version=pattern.version,
        )
        applied = [item for item in transitions if item.applied]
        return applied[-1].to_status if applied else pattern.status


def _allowed_statuses(mode: HarnessOverlayMode) -> set[HarnessPatternStatus]:
    if mode == HarnessOverlayMode.SHADOW:
        return {HarnessPatternStatus.SHADOW}
    if mode == HarnessOverlayMode.CANARY:
        return {HarnessPatternStatus.CANARY, HarnessPatternStatus.ACTIVE}
    if mode == HarnessOverlayMode.ACTIVE:
        return {HarnessPatternStatus.ACTIVE}
    return set()


def _matches_features(
    features: CaseFeatures,
    predicate: HarnessTriggerPredicate,
) -> bool:
    if features.domain_pack != predicate.domain_pack:
        return False
    primary = next(
        (intent for intent in features.intents if intent != "social"),
        "lookup",
    )
    if primary != predicate.primary_intent:
        return False
    for field in ("personal_knowledge", "visual", "graph_relations"):
        expected = getattr(predicate, field)
        if expected is not None and getattr(features, field) != expected:
            return False
    return predicate.language is None or features.language == predicate.language


def _experience_similarity(left: CaseFeatures, right: CaseFeatures) -> float:
    if left.domain_pack != right.domain_pack:
        return 0.0
    left_tokens = set(left.query_token_hashes)
    right_tokens = set(right.query_token_hashes)
    union = left_tokens | right_tokens
    token_score = len(left_tokens & right_tokens) / len(union) if union else 0.0
    intent_score = 1.0 if set(left.intents) & set(right.intents) else 0.0
    feature_score = (
        int(left.personal_knowledge == right.personal_knowledge)
        + int(left.visual == right.visual)
        + int(left.graph_relations == right.graph_relations)
        + int(left.code == right.code)
    ) / 4
    return round(0.6 * token_score + 0.25 * intent_score + 0.15 * feature_score, 6)


def _merge_pattern_deltas(patterns: list[HarnessPattern]) -> HarnessConfigDelta:
    payload: dict[str, object] = {}
    for pattern in patterns:
        for key, value in pattern.proposed_delta.model_dump(
            mode="json",
            exclude_none=True,
        ).items():
            payload[key] = value
    return HarnessConfigDelta.model_validate(payload)


def _semver(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)
