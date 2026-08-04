from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.domain.models import RunContext, RunExecutionPolicy
from app.harness.models import (
    HarnessConfigDelta,
    HarnessContextConfig,
    HarnessMemoryConfig,
    HarnessOrchestrationConfig,
    HarnessToolConfig,
    RunHarnessOverlay,
)

CONSUMER_REVISION = "bounded-harness-consumer-v1"


@dataclass(frozen=True, slots=True)
class HarnessConsumerLimits:
    max_capsule_memories: int = 20
    max_graph_hops: int = 3
    max_subqueries: int = 4
    max_retrieval_rounds: int = 2


@dataclass(frozen=True, slots=True)
class HarnessConsumerProjection:
    effective_delta: HarnessConfigDelta
    allowed_fields: tuple[str, ...]
    clamped_fields: tuple[str, ...]
    rejected_fields: tuple[str, ...]

    @property
    def compatible(self) -> bool:
        return bool(self.allowed_fields) and not self.rejected_fields


class BoundedHarnessConsumer:
    """Project Pattern deltas onto the small runtime-owned policy surface."""

    def __init__(self, limits: HarnessConsumerLimits | None = None) -> None:
        self._limits = limits or HarnessConsumerLimits()

    def project(self, delta: HarnessConfigDelta) -> HarnessConsumerProjection:
        payload: dict[str, object] = {}
        allowed: list[str] = []
        clamped: list[str] = []
        rejected: list[str] = []

        if delta.context is not None:
            context_payload: dict[str, object] = {}
            self._bounded_int(
                context_payload,
                allowed,
                clamped,
                "context.capsule_memory_limit",
                "capsule_memory_limit",
                delta.context.capsule_memory_limit,
                self._limits.max_capsule_memories,
            )
            if delta.context.max_capsule_tokens is not None:
                rejected.append("context.max_capsule_tokens")
            if delta.context.private_evidence_quota is not None:
                rejected.append("context.private_evidence_quota")
            if context_payload:
                payload["context"] = HarnessContextConfig.model_validate(context_payload)

        if delta.tool is not None:
            tool_payload: dict[str, object] = {}
            self._bounded_int(
                tool_payload,
                allowed,
                clamped,
                "tool.graph_hops",
                "graph_hops",
                delta.tool.graph_hops,
                self._limits.max_graph_hops,
            )
            if delta.tool.source_diversity_limit is not None:
                rejected.append("tool.source_diversity_limit")
            if delta.tool.allow_graph_followup is not None:
                rejected.append("tool.allow_graph_followup")
            if tool_payload:
                payload["tool"] = HarnessToolConfig.model_validate(tool_payload)

        if delta.orchestration is not None:
            orchestration_payload: dict[str, object] = {}
            if delta.orchestration.retrieval_profile is not None:
                orchestration_payload["retrieval_profile"] = (
                    delta.orchestration.retrieval_profile
                )
                allowed.append("orchestration.retrieval_profile")
            self._bounded_int(
                orchestration_payload,
                allowed,
                clamped,
                "orchestration.max_subqueries",
                "max_subqueries",
                delta.orchestration.max_subqueries,
                self._limits.max_subqueries,
            )
            self._bounded_int(
                orchestration_payload,
                allowed,
                clamped,
                "orchestration.max_retrieval_rounds",
                "max_retrieval_rounds",
                delta.orchestration.max_retrieval_rounds,
                self._limits.max_retrieval_rounds,
            )
            if orchestration_payload:
                payload["orchestration"] = HarnessOrchestrationConfig.model_validate(
                    orchestration_payload
                )

        if delta.memory is not None:
            memory_payload: dict[str, object] = {}
            if delta.memory.memory_type_quota:
                rejected.append("memory.memory_type_quota")
            if delta.memory.memory_min_confidence is not None:
                memory_payload["memory_min_confidence"] = (
                    delta.memory.memory_min_confidence
                )
                allowed.append("memory.memory_min_confidence")
            if memory_payload:
                payload["memory"] = HarnessMemoryConfig.model_validate(memory_payload)

        rejected.extend(_leaf_paths("generation", delta.generation))
        rejected.extend(_leaf_paths("output", delta.output))
        effective = HarnessConfigDelta.model_validate(payload)
        return HarnessConsumerProjection(
            effective_delta=effective,
            allowed_fields=tuple(sorted(set(allowed))),
            clamped_fields=tuple(sorted(set(clamped))),
            rejected_fields=tuple(sorted(set(rejected))),
        )

    def resolve_policy(
        self,
        *,
        context: RunContext,
        overlay: RunHarnessOverlay,
        apply_requested: bool,
    ) -> RunExecutionPolicy:
        projection = self.project(overlay.effective_delta)
        rejected = {
            *projection.rejected_fields,
            *(
                item.removeprefix("consumer_rejected:")
                for item in overlay.rejected_conflicts
                if item.startswith("consumer_rejected:")
            ),
        }
        behavior_applied = (
            apply_requested
            and bool(overlay.selected_pattern_versions)
            and bool(projection.allowed_fields)
            and not rejected
        )
        delta = projection.effective_delta
        payload: dict[str, object] = {
            "resolver_revision": CONSUMER_REVISION,
            "behavior_applied": behavior_applied,
            "overlay_id": overlay.overlay_id,
            "overlay_hash": overlay.payload_hash,
            "selected_pattern_versions": overlay.selected_pattern_versions,
            "applied_pattern_versions": (
                overlay.selected_pattern_versions if behavior_applied else []
            ),
            "capsule_memory_limit": (
                delta.context.capsule_memory_limit
                if delta.context is not None
                else None
            ),
            "memory_min_confidence": (
                delta.memory.memory_min_confidence
                if delta.memory is not None
                else None
            ),
            "retrieval_profile": (
                delta.orchestration.retrieval_profile
                if delta.orchestration is not None
                else None
            ),
            "max_subqueries": (
                delta.orchestration.max_subqueries
                if delta.orchestration is not None
                else None
            ),
            "max_retrieval_rounds": (
                delta.orchestration.max_retrieval_rounds
                if delta.orchestration is not None
                else None
            ),
            "graph_hop_cap": (
                delta.tool.graph_hops if delta.tool is not None else None
            ),
            "clamped_fields": sorted(
                {*overlay.clamped_fields, *projection.clamped_fields}
            ),
            "rejected_fields": sorted(rejected),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return RunExecutionPolicy.model_validate(
            {
                **payload,
                "policy_hash": hashlib.sha256(encoded.encode()).hexdigest(),
            }
        )

    @staticmethod
    def _bounded_int(
        target: dict[str, object],
        allowed: list[str],
        clamped: list[str],
        path: str,
        key: str,
        value: int | None,
        hard_max: int,
    ) -> None:
        if value is None:
            return
        bounded = min(value, hard_max)
        target[key] = bounded
        allowed.append(path)
        if bounded != value:
            clamped.append(path)


def stable_canary_assignment(
    *,
    context: RunContext,
    pattern_version: str,
    percentage: int,
) -> bool:
    if percentage <= 0:
        return False
    if percentage >= 100:
        return True
    identity = (
        f"{CONSUMER_REVISION}:{context.tenant_id}:{context.project_id}:"
        f"{context.run_id}:{pattern_version}"
    )
    bucket = int(hashlib.sha256(identity.encode()).hexdigest()[:8], 16) % 100
    return bucket < percentage


def _leaf_paths(prefix: str, model: Any | None) -> list[str]:
    if model is None:
        return []
    payload = model.model_dump(mode="json", exclude_none=True)
    return [f"{prefix}.{key}" for key in sorted(payload)]


__all__ = [
    "BoundedHarnessConsumer",
    "CONSUMER_REVISION",
    "HarnessConsumerLimits",
    "HarnessConsumerProjection",
    "stable_canary_assignment",
]
