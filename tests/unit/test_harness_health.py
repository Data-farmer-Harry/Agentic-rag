from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.domain.enums import AnswerMode, RunStatus
from app.domain.models import AnswerResponse, RunContext, RunTrajectory
from app.harness.evaluation import DeterministicPatternEvaluator
from app.harness.evolution import HarnessPatternEvolutionService
from app.harness.experience import assemble_experience
from app.harness.health import HarnessPatternHealthMonitor
from app.harness.models import (
    HarnessConfigDelta,
    HarnessExperienceEntry,
    HarnessOutputConfig,
    HarnessPattern,
    HarnessPatternStatus,
    HarnessReasonCode,
    HarnessTriggerPredicate,
    canonical_hash,
)
from app.harness.repository import JsonHarnessExperienceRepository, JsonHarnessPolicyRepository


@pytest.mark.asyncio
async def test_health_monitor_rolls_back_observed_canary_regression(tmp_path: Path) -> None:
    experiences = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    policies = JsonHarnessPolicyRepository(tmp_path / "policies.jsonl")
    evolution = HarnessPatternEvolutionService(
        policies,
        DeterministicPatternEvaluator(experiences),
    )
    pattern = await policies.save_pattern(_pattern())
    key = f"{pattern.pattern_id}@{pattern.version}"
    for index in range(5):
        await experiences.save(_experience(index, success=False, pattern_version=key))
        await experiences.save(_experience(index + 10, success=True))

    monitor = HarnessPatternHealthMonitor(
        experiences,
        policies,
        evolution,
        min_applied_cases=5,
        min_control_cases=5,
    )
    reports = await monitor.monitor_scope(tenant_id="local", project_id="health")

    assert len(reports) == 1
    assert reports[0].sufficient is True
    assert reports[0].healthy is False
    assert reports[0].quality_lift < 0
    transitions = await policies.list_pattern_transitions(
        pattern.pattern_id,
        tenant_id="local",
        project_id="health",
        pattern_version=pattern.version,
    )
    assert [item.transition_type for item in transitions] == ["health_gate", "rollback"]
    assert transitions[0].applied is False
    assert transitions[1].applied is True
    assert await evolution.effective_status(pattern) == HarnessPatternStatus.ROLLED_BACK


@pytest.mark.asyncio
async def test_health_monitor_observes_without_rollback_until_samples_are_sufficient(
    tmp_path: Path,
) -> None:
    experiences = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    policies = JsonHarnessPolicyRepository(tmp_path / "policies.jsonl")
    evolution = HarnessPatternEvolutionService(
        policies,
        DeterministicPatternEvaluator(experiences),
    )
    pattern = await policies.save_pattern(_pattern())
    key = f"{pattern.pattern_id}@{pattern.version}"
    await experiences.save(_experience(1, success=False, pattern_version=key))
    await experiences.save(_experience(2, success=True))

    report = await HarnessPatternHealthMonitor(
        experiences,
        policies,
        evolution,
        min_applied_cases=3,
        min_control_cases=3,
    ).evaluate(pattern)

    assert report.sufficient is False
    assert report.healthy is False
    assert report.reasons[0].startswith("insufficient_health_sample")
    assert await evolution.effective_status(pattern) == HarnessPatternStatus.CANARY


def _pattern() -> HarnessPattern:
    pattern_id = uuid5(NAMESPACE_URL, "hermesgraph:test:health-pattern")
    payload = {
        "pattern_id": pattern_id,
        "version": "1.0.0",
        "parent_version": None,
        "tenant_id": "local",
        "project_id": "health",
        "name": "Require supported citations",
        "trigger_predicate": HarnessTriggerPredicate(
            domain_pack="software_engineering",
            primary_intent="compare",
            required_reason_codes=[HarnessReasonCode.CITATION_COVERAGE_BELOW_THRESHOLD],
        ),
        "dimensions": ["output"],
        "proposed_delta": HarnessConfigDelta(
            output=HarnessOutputConfig(minimum_citation_coverage=0.9)
        ),
        "supporting_experience_ids": [],
        "contradicting_experience_ids": [],
        "support_count": 5,
        "failure_count": 5,
        "estimated_quality_lift": 0.0,
        "confidence": 0.8,
        "status": HarnessPatternStatus.CANARY,
        "miner_revision": "test",
        "evaluator_revision": "test",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return HarnessPattern.model_validate({**payload, "payload_hash": canonical_hash(payload)})


def _experience(
    index: int,
    *,
    success: bool,
    pattern_version: str | None = None,
) -> HarnessExperienceEntry:
    answer = AnswerResponse(
        answer_markdown="A supported comparison." if success else "Unsupported.",
        response_mode=AnswerMode.CONVERSATIONAL if success else AnswerMode.GROUNDED,
    )
    trajectory = RunTrajectory(
        context=RunContext(
            project_id="health",
            domain_pack="software_engineering",
            session_id=f"health-{index}",
        ),
        user_input="比较 Polaris 和 Constellation",
        status=RunStatus.COMPLETED,
        answer=answer,
        completed_at=datetime(2026, 2, index + 1, tzinfo=UTC),
    )
    base = assemble_experience(trajectory)
    payload = base.model_dump(mode="json", exclude={"payload_hash"})
    payload["applied_pattern_versions"] = [pattern_version] if pattern_version else []
    return HarnessExperienceEntry.model_validate(
        {**payload, "payload_hash": canonical_hash(payload)}
    )
