from __future__ import annotations

from pathlib import Path

import pytest

from app.evaluation.self_learning import SelfLearningEffectEvaluator
from app.harness.experience import HarnessExperienceService
from app.harness.repository import JsonHarnessExperienceRepository, JsonHarnessPolicyRepository
from tests.unit.test_harness_patterns import failing_compare


@pytest.mark.asyncio
async def test_self_learning_gate_reports_real_data_as_observing_not_validated(
    tmp_path: Path,
) -> None:
    experiences = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    policies = JsonHarnessPolicyRepository(tmp_path / "patterns.jsonl")
    service = HarnessExperienceService(experiences)
    for index in range(3):
        await service.collect(failing_compare(index), trigger="run_completed")

    report = await SelfLearningEffectEvaluator(
        experiences,
        policies,
        minimum_experiences=3,
        minimum_feedback=0,
    ).evaluate(tenant_id="local", project_id="patterns")

    assert report.status == "observing"
    assert report.passed is False
    assert report.evaluation_coverage == 1.0
    assert report.pattern_count == 0
    assert "no_real_patterns_mined" in report.reasons


@pytest.mark.asyncio
async def test_self_learning_gate_fails_closed_when_no_observations_exist(
    tmp_path: Path,
) -> None:
    report = await SelfLearningEffectEvaluator(
        JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl"),
        JsonHarnessPolicyRepository(tmp_path / "patterns.jsonl"),
        minimum_experiences=1,
        minimum_feedback=0,
    ).evaluate(tenant_id="local", project_id="default")

    assert report.status == "not_ready"
    assert report.experience_count == 0
    assert report.passed is False
