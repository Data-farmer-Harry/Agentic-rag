from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.enums import RunStatus
from app.domain.models import AnswerResponse, Claim, RunContext, RunTrajectory
from app.harness.consumer import BoundedHarnessConsumer, stable_canary_assignment
from app.harness.evaluation import DeterministicPatternEvaluator
from app.harness.evolution import HarnessPatternEvolutionService
from app.harness.experience import assemble_experience
from app.harness.mining import DeterministicPatternMiner
from app.harness.models import (
    HarnessConfigDelta,
    HarnessOverlayMode,
    HarnessPattern,
    HarnessPatternStatus,
    HarnessReasonCode,
    HarnessToolConfig,
    HarnessTriggerPredicate,
    canonical_hash,
)
from app.harness.repository import (
    JsonHarnessExperienceRepository,
    JsonHarnessPolicyRepository,
)
from app.harness.selector import HarnessOverlaySelector


def failing_compare(index: int) -> RunTrajectory:
    return RunTrajectory(
        context=RunContext(
            project_id="patterns",
            domain_pack="research_reference",
            session_id=f"session-{index}",
        ),
        user_input="比较 GraphRAG 和 LightRAG 的主要区别",
        status=RunStatus.COMPLETED,
        answer=AnswerResponse(
            answer_markdown="They differ.",
            claims=[Claim(text="Unsupported comparison")],
        ),
        completed_at=datetime(2026, 1, index + 1, tzinfo=UTC),
    )


def failing_graph_followup(index: int) -> RunTrajectory:
    return RunTrajectory(
        context=RunContext(
            project_id="graph-patterns",
            domain_pack="research_reference",
            session_id=f"graph-session-{index}",
        ),
        user_input="查找 GraphRAG 和实体社区之间的证据路径",
        status=RunStatus.COMPLETED,
        answer=AnswerResponse(
            answer_markdown="No graph follow-up was executed.",
            claims=[Claim(text="The relationship is unresolved")],
        ),
        tags=["graph_followup_missing"],
        completed_at=datetime(2026, 3, index + 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_pattern_miner_requires_repetition_and_is_idempotent(
    tmp_path: Path,
) -> None:
    experiences = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    policies = JsonHarnessPolicyRepository(tmp_path / "policies.jsonl")
    miner = DeterministicPatternMiner(
        experiences,
        policies,
        repeated_failure_threshold=3,
        min_cluster_size=3,
    )
    for index in range(2):
        await experiences.save(assemble_experience(failing_compare(index)))

    assert (await miner.mine_scope(tenant_id="local", project_id="patterns")).created == 0
    await experiences.save(assemble_experience(failing_compare(2)))
    first = await miner.mine_scope(tenant_id="local", project_id="patterns")
    second = await miner.mine_scope(tenant_id="local", project_id="patterns")

    assert first.created == 1
    assert len(first.candidates) == 1
    pattern = first.candidates[0]
    assert pattern.status == HarnessPatternStatus.DRAFT
    assert pattern.support_count == 3
    assert pattern.trigger_predicate.primary_intent == "compare"
    assert pattern.proposed_delta.output is not None
    assert pattern.proposed_delta.output.minimum_citation_coverage == 0.9
    assert second.created == 0
    assert second.unchanged == 1


@pytest.mark.asyncio
async def test_social_failures_never_create_business_patterns(tmp_path: Path) -> None:
    experiences = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    policies = JsonHarnessPolicyRepository(tmp_path / "policies.jsonl")
    for index in range(5):
        trajectory = RunTrajectory(
            context=RunContext(
                project_id="social",
                session_id=f"session-{index}",
            ),
            user_input="你好",
            status=RunStatus.FAILED,
            completed_at=datetime(2026, 2, index + 1, tzinfo=UTC),
        )
        await experiences.save(assemble_experience(trajectory))

    result = await DeterministicPatternMiner(
        experiences,
        policies,
        repeated_failure_threshold=3,
        min_cluster_size=3,
    ).mine_scope(tenant_id="local", project_id="social")

    assert result.candidates == ()


@pytest.mark.asyncio
async def test_shadow_selector_freezes_approved_pattern_without_applying_it(
    tmp_path: Path,
) -> None:
    experiences = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    drafts = JsonHarnessPolicyRepository(tmp_path / "drafts.jsonl")
    for index in range(3):
        await experiences.save(assemble_experience(failing_compare(index)))
    mined = await DeterministicPatternMiner(
        experiences,
        drafts,
        repeated_failure_threshold=3,
        min_cluster_size=3,
    ).mine_scope(tenant_id="local", project_id="patterns")
    draft = mined.candidates[0]
    payload = draft.model_dump(mode="python", exclude={"payload_hash"})
    payload["status"] = HarnessPatternStatus.SHADOW
    shadow = HarnessPattern.model_validate(
        {**payload, "payload_hash": canonical_hash(payload)}
    )
    policies = JsonHarnessPolicyRepository(tmp_path / "policies.jsonl")
    await policies.save_pattern(shadow)
    selector = HarnessOverlaySelector(
        experiences,
        policies,
        mode=HarnessOverlayMode.SHADOW,
        max_patterns=3,
    )
    context = RunContext(
        project_id="patterns",
        domain_pack="research_reference",
    )

    overlay = await selector.select(
        context=context,
        query="比较 GraphRAG 和 LightRAG 的主要区别",
        baseline_policy_versions={"harness": "baseline-v1"},
    )
    duplicate = await selector.select(
        context=context,
        query="this changed query is ignored for the same frozen run",
        baseline_policy_versions={"harness": "baseline-v1"},
    )

    assert overlay is not None
    assert duplicate == overlay
    assert overlay.mode == HarnessOverlayMode.SHADOW
    assert overlay.selected_pattern_versions == [
        f"{shadow.pattern_id}@{shadow.version}"
    ]
    assert overlay.effective_delta.output is not None
    assert overlay.positive_experience_ids == []
    assert overlay.negative_experience_ids


@pytest.mark.asyncio
async def test_pattern_evaluation_stages_with_immutable_promotion_evidence(
    tmp_path: Path,
) -> None:
    experiences = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    policies = JsonHarnessPolicyRepository(tmp_path / "policies.jsonl")
    for index in range(3):
        await experiences.save(assemble_experience(failing_graph_followup(index)))
    mined = await DeterministicPatternMiner(
        experiences,
        policies,
        repeated_failure_threshold=3,
        min_cluster_size=3,
    ).mine_scope(tenant_id="local", project_id="graph-patterns")
    pattern = next(
        item
        for item in mined.candidates
        if HarnessReasonCode.GRAPH_FOLLOWUP_MISSING
        in item.trigger_predicate.required_reason_codes
    )
    evolution = HarnessPatternEvolutionService(
        policies,
        DeterministicPatternEvaluator(
            experiences,
            min_support_cases=3,
        ),
    )

    result = await evolution.evaluate_and_stage(
        pattern.pattern_id,
        project_id="graph-patterns",
        pattern_version=pattern.version,
    )
    duplicate = await evolution.evaluate_and_stage(
        pattern.pattern_id,
        project_id="graph-patterns",
        pattern_version=pattern.version,
    )

    assert result.effective_status == HarnessPatternStatus.SHADOW
    assert result.evaluation.required_cases_passed is True
    assert result.evaluation.regression_passed is True
    assert result.evaluation.consumer_compatible is True
    assert result.promotion_evidence.offline_ready is True
    assert result.promotion_evidence.evaluation_payload_hash == (
        result.evaluation.payload_hash
    )
    assert [item.to_status for item in result.transitions] == [
        HarnessPatternStatus.OFFLINE_PASS,
        HarnessPatternStatus.SHADOW,
    ]
    assert all(item.applied for item in result.transitions)
    assert duplicate.evaluation == result.evaluation
    assert duplicate.promotion_evidence == result.promotion_evidence
    assert duplicate.transitions == []
    transitions = await policies.list_pattern_transitions(
        pattern.pattern_id,
        tenant_id="local",
        project_id="graph-patterns",
        pattern_version=pattern.version,
    )
    assert len(transitions) == 2


@pytest.mark.asyncio
async def test_pattern_canary_requires_human_and_consumer_compatibility(
    tmp_path: Path,
) -> None:
    experiences = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    policies = JsonHarnessPolicyRepository(tmp_path / "policies.jsonl")
    for index in range(3):
        await experiences.save(assemble_experience(failing_graph_followup(index)))
    mined = await DeterministicPatternMiner(
        experiences,
        policies,
        repeated_failure_threshold=3,
        min_cluster_size=3,
    ).mine_scope(tenant_id="local", project_id="graph-patterns")
    pattern = next(
        item
        for item in mined.candidates
        if item.proposed_delta.tool is not None
    )
    evolution = HarnessPatternEvolutionService(
        policies,
        DeterministicPatternEvaluator(experiences, min_support_cases=3),
    )
    await evolution.evaluate_and_stage(
        pattern.pattern_id,
        project_id="graph-patterns",
    )

    denied = await evolution.transition(
        pattern.pattern_id,
        HarnessPatternStatus.CANARY,
        project_id="graph-patterns",
    )
    canary = await evolution.transition(
        pattern.pattern_id,
        HarnessPatternStatus.CANARY,
        project_id="graph-patterns",
        human_approved=True,
        expected_from_status=HarnessPatternStatus.SHADOW,
    )
    active = await evolution.transition(
        pattern.pattern_id,
        HarnessPatternStatus.ACTIVE,
        project_id="graph-patterns",
        human_approved=True,
        expected_from_status=HarnessPatternStatus.CANARY,
    )

    assert denied.applied is False
    assert "human_approval_required" in denied.reasons
    assert canary.applied is True
    assert active.applied is True
    assert await evolution.effective_status(pattern) == HarnessPatternStatus.ACTIVE
    selector = HarnessOverlaySelector(
        experiences,
        policies,
        mode=HarnessOverlayMode.ACTIVE,
    )
    active_overlay = await selector.select(
        context=RunContext(
            project_id="graph-patterns",
            domain_pack="research_reference",
        ),
        query="查找 GraphRAG 和实体社区之间的证据路径",
        baseline_policy_versions={"harness": "baseline-v1"},
    )
    rollback = await evolution.rollback(
        pattern.pattern_id,
        project_id="graph-patterns",
        reason="contract regression",
    )
    rolled_back_overlay = await selector.select(
        context=RunContext(
            project_id="graph-patterns",
            domain_pack="research_reference",
        ),
        query="查找 GraphRAG 和实体社区之间的证据路径",
        baseline_policy_versions={"harness": "baseline-v1"},
    )

    assert active_overlay is not None
    assert active_overlay.selected_pattern_versions == [
        f"{pattern.pattern_id}@{pattern.version}"
    ]
    assert rollback.applied is True
    assert rolled_back_overlay is not None
    assert rolled_back_overlay.selected_pattern_versions == []


@pytest.mark.asyncio
async def test_required_case_failure_blocks_staging_even_with_zero_regression_limit(
    tmp_path: Path,
) -> None:
    experiences = JsonHarnessExperienceRepository(tmp_path / "experiences.jsonl")
    policies = JsonHarnessPolicyRepository(tmp_path / "policies.jsonl")
    pattern_payload = {
        "pattern_id": failing_graph_followup(0).context.run_id,
        "version": "0.1.0",
        "parent_version": None,
        "tenant_id": "local",
        "project_id": "required-gate",
        "name": "Missing evidence must block promotion",
        "trigger_predicate": HarnessTriggerPredicate(
            domain_pack="research_reference",
            primary_intent="research",
            required_reason_codes=[HarnessReasonCode.GRAPH_FOLLOWUP_MISSING],
        ),
        "dimensions": ["tool"],
        "proposed_delta": HarnessConfigDelta(
            tool=HarnessToolConfig(graph_hops=2)
        ),
        "supporting_experience_ids": [failing_compare(0).context.run_id],
        "contradicting_experience_ids": [],
        "support_count": 1,
        "failure_count": 1,
        "estimated_quality_lift": 0.0,
        "confidence": 1.0,
        "status": HarnessPatternStatus.DRAFT,
        "miner_revision": "test",
        "evaluator_revision": "pending",
        "created_at": datetime(2026, 4, 1, tzinfo=UTC),
    }
    pattern = HarnessPattern.model_validate(
        {**pattern_payload, "payload_hash": canonical_hash(pattern_payload)}
    )
    await policies.save_pattern(pattern)
    evolution = HarnessPatternEvolutionService(
        policies,
        DeterministicPatternEvaluator(
            experiences,
            min_support_cases=1,
            max_score_regression=0.0,
        ),
    )

    result = await evolution.evaluate_and_stage(
        pattern.pattern_id,
        project_id="required-gate",
    )

    assert result.effective_status == HarnessPatternStatus.DRAFT
    assert result.transitions == []
    assert result.evaluation.required_cases_passed is False
    assert "required.evidence_integrity" in (
        result.evaluation.failed_required_case_ids
    )
    assert result.promotion_evidence.offline_ready is False


def test_canary_assignment_is_stable_and_bounded() -> None:
    context = RunContext(project_id="canary")
    version = "00000000-0000-0000-0000-000000000001@1.0.0"

    assert stable_canary_assignment(
        context=context,
        pattern_version=version,
        percentage=0,
    ) is False
    assert stable_canary_assignment(
        context=context,
        pattern_version=version,
        percentage=100,
    ) is True
    first = stable_canary_assignment(
        context=context,
        pattern_version=version,
        percentage=25,
    )
    second = stable_canary_assignment(
        context=context,
        pattern_version=version,
        percentage=25,
    )
    assert first == second


def test_bounded_consumer_rejects_unimplemented_output_policy() -> None:
    compare = failing_compare(0)
    delta = HarnessConfigDelta.model_validate(
        {
            "tool": {"graph_hops": 2},
            "output": {"minimum_citation_coverage": 0.9},
        }
    )

    projection = BoundedHarnessConsumer().project(delta)

    assert projection.effective_delta.tool is not None
    assert projection.effective_delta.tool.graph_hops == 2
    assert projection.compatible is False
    assert projection.rejected_fields == ("output.minimum_citation_coverage",)
    assert compare.context.execution_policy is None
