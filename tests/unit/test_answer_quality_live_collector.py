from __future__ import annotations

from uuid import uuid4

import pytest

from app.domain.enums import EvidenceLevel, RunStatus, TrustLevel
from app.domain.models import (
    AdaptiveRAGRoute,
    AnswerResponse,
    Claim,
    EvidenceRef,
    Provenance,
    RunContext,
    RunTrajectory,
)
from app.evaluation.answer_quality import (
    AnswerQualityCase,
    AnswerQualityEvidence,
    AnswerQualityExpectedClaim,
    AnswerQualityGoldenSet,
    AnswerQualityVariant,
)
from app.evaluation.answer_quality_live_cli import (
    LiveAnswerAnnotation,
    LiveClaimAssignment,
    collect_live_artifact,
)


def _dataset() -> AnswerQualityGoldenSet:
    return AnswerQualityGoldenSet(
        name="live collector test",
        revision="test-v1",
        dataset_kind="evaluation_spec",
        cases=[
            AnswerQualityCase(
                case_id="service-boundary",
                query="What does Polaris do?",
                category="service",
                expected_claims=[
                    AnswerQualityExpectedClaim(
                        claim_id="polaris-role",
                        text="Polaris retrieves text evidence.",
                    )
                ],
                evidence=[
                    AnswerQualityEvidence(
                        evidence_id="polaris-service",
                        source_id="northstar:service:polaris#chunk=0",
                        text="Polaris retrieves text evidence.",
                        supports_claim_ids=["polaris-role"],
                    )
                ],
                required_variants=["self_rag"],
            )
        ],
    )


def _trajectory(*, project_id: str = "default", source_id: str | None = None) -> RunTrajectory:
    evidence = EvidenceRef(
        text="Polaris retrieves text evidence.",
        provenance=Provenance(
            source_type="enterprise_fixture",
            source_id=source_id or "northstar:service:polaris#chunk=0",
            trust=TrustLevel.VERIFIED,
        ),
    )
    route = AdaptiveRAGRoute(
        strategy="multi_step",
        knowledge_route="global_summary",
        requires_multi_source=True,
        self_reflection=True,
    )
    return RunTrajectory(
        context=RunContext(project_id=project_id, adaptive_rag_route=route),
        user_input="What does Polaris do?",
        status=RunStatus.COMPLETED,
        answer=AnswerResponse(
            answer_markdown="Polaris retrieves text evidence.",
            claims=[
                Claim(
                    text="Polaris retrieves text evidence.",
                    evidence_ids=[evidence.evidence_id],
                    level=EvidenceLevel.SUPPORTED,
                )
            ],
            citations=[evidence],
            confidence=EvidenceLevel.SUPPORTED,
            adaptive_rag_route=route,
        ),
    )


def _annotation(
    run: RunTrajectory,
    *,
    variant: AnswerQualityVariant = "self_rag",
) -> LiveAnswerAnnotation:
    return LiveAnswerAnnotation(
        revision="human-v1",
        run_id=str(run.context.run_id),
        case_id="service-boundary",
        variant=variant,
        claim_assignments=[
            LiveClaimAssignment(
                claim_index=0,
                expected_claim_id="polaris-role",
                answer_quote="Polaris retrieves text evidence.",
            )
        ],
    )


def test_collect_live_artifact_maps_runtime_evidence_to_stable_golden_id() -> None:
    run = _trajectory()

    artifact = collect_live_artifact(
        _dataset(),
        _annotation(run),
        run,
        expected_project_id="default",
    )

    assert artifact.provenance.kind == "live_run"
    assert artifact.provenance.run_ids == [str(run.context.run_id)]
    assert artifact.answers[0].variant == "self_rag"
    assert artifact.answers[0].claims[0].citation_ids == ["polaris-service"]
    assert artifact.answers[0].cited_evidence_ids == ["polaris-service"]


def test_collect_live_artifact_rejects_cross_project_trajectory() -> None:
    run = _trajectory(project_id="foreign")

    with pytest.raises(ValueError, match="project scope"):
        collect_live_artifact(
            _dataset(),
            _annotation(run),
            run,
            expected_project_id="default",
        )


def test_collect_live_artifact_rejects_unannotated_runtime_source() -> None:
    run = _trajectory(source_id=f"unknown:{uuid4()}#chunk=0")

    with pytest.raises(ValueError, match="not annotated"):
        collect_live_artifact(_dataset(), _annotation(run), run)


def test_collect_live_artifact_rejects_variant_not_observed_in_route() -> None:
    run = _trajectory()

    with pytest.raises(ValueError, match="graph evidence"):
        collect_live_artifact(_dataset(), _annotation(run, variant="graph_rag"), run)
