from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path
from uuid import UUID

import httpx
from pydantic import Field, model_validator

from app.domain.models import RunTrajectory, StrictModel
from app.evaluation.answer_quality import (
    AnswerQualityArtifactProvenance,
    AnswerQualityArtifactSet,
    AnswerQualityGoldenSet,
    AnswerQualityObservedClaim,
    AnswerQualityVariant,
    AnswerQualityVariantObservation,
    load_answer_quality_golden_set,
)


class LiveClaimAssignment(StrictModel):
    claim_index: int = Field(ge=0)
    expected_claim_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,99}$")
    answer_quote: str = Field(min_length=1, max_length=10_000)


class LiveAnswerAnnotation(StrictModel):
    revision: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,99}$")
    variant: AnswerQualityVariant
    claim_assignments: list[LiveClaimAssignment] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_assignments(self) -> LiveAnswerAnnotation:
        indexes = [item.claim_index for item in self.claim_assignments]
        claim_ids = [item.expected_claim_id for item in self.claim_assignments]
        if len(indexes) != len(set(indexes)):
            raise ValueError("live claim indexes must be unique")
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("live expected claim IDs must be unique")
        return self


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Collect a fail-closed answer-quality artifact from completed live runs"
    )
    value.add_argument("--base-url", default="http://127.0.0.1:8001")
    value.add_argument("--project-id", default="default")
    value.add_argument("--api-token")
    value.add_argument("--dataset", type=Path, required=True)
    value.add_argument(
        "--annotation",
        type=Path,
        action="append",
        required=True,
        help="Repeat for every case/variant in a paired live artifact",
    )
    value.add_argument("--output", type=Path, required=True)
    return value


async def run(args: argparse.Namespace) -> int:
    dataset = load_answer_quality_golden_set(args.dataset)
    if dataset.dataset_kind != "evaluation_spec":
        raise ValueError("live collection requires an evaluation_spec dataset")
    headers = {"Accept": "application/json"}
    if args.api_token:
        headers["Authorization"] = f"Bearer {args.api_token}"
    annotations = [
        LiveAnswerAnnotation.model_validate_json(path.read_text(encoding="utf-8"))
        for path in args.annotation
    ]
    artifacts: list[AnswerQualityArtifactSet] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for annotation in annotations:
            url = (
                f"{args.base_url.rstrip('/')}/v1/projects/{args.project_id}/runs/"
                f"{annotation.run_id}"
            )
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            trajectory = RunTrajectory.model_validate(response.json())
            artifacts.append(
                collect_live_artifact(
                    dataset,
                    annotation,
                    trajectory,
                    expected_project_id=args.project_id,
                )
            )
    run_ids = [item.run_id for item in annotations]
    answers = [answer for artifact in artifacts for answer in artifact.answers]
    artifact = AnswerQualityArtifactSet(
        provenance=AnswerQualityArtifactProvenance(
            kind="live_run",
            label="HermesGraph live runs; annotations="
            + ",".join(item.revision for item in annotations),
            run_ids=run_ids,
            model_revision=_shared_model_revision(artifacts),
        ),
        answers=answers,
    )
    _write_atomic(args.output, artifact.model_dump_json(indent=2) + "\n")
    print(artifact.model_dump_json(indent=2))
    return 0


def collect_live_artifact(
    dataset: AnswerQualityGoldenSet,
    annotation: LiveAnswerAnnotation,
    trajectory: RunTrajectory,
    *,
    expected_project_id: str | None = None,
) -> AnswerQualityArtifactSet:
    if str(trajectory.context.run_id) != annotation.run_id:
        raise ValueError("annotation run ID does not match the retrieved trajectory")
    if expected_project_id is not None and trajectory.context.project_id != expected_project_id:
        raise ValueError("run project scope is inconsistent")
    if trajectory.status.value != "completed" or trajectory.answer is None:
        raise ValueError("live artifact requires a completed run with an answer")
    case = next((item for item in dataset.cases if item.case_id == annotation.case_id), None)
    if case is None:
        raise ValueError("annotation references an unknown answer-quality case")
    _validate_variant(annotation.variant, trajectory)
    answer = trajectory.answer
    assignments = {item.claim_index: item for item in annotation.claim_assignments}
    if set(assignments) != set(range(len(answer.claims))):
        raise ValueError("every live answer claim must be assigned exactly once")
    expected_claim_ids = {item.claim_id for item in case.expected_claims}
    if {item.expected_claim_id for item in annotation.claim_assignments} - expected_claim_ids:
        raise ValueError("annotation references a claim outside the selected case")
    evidence_by_source = {item.source_id: item.evidence_id for item in case.evidence}
    if len(evidence_by_source) != len(case.evidence):
        raise ValueError("live evaluation evidence source IDs must be unique")
    run_evidence = {item.evidence_id: item for item in answer.citations}
    mapped_run_evidence: dict[UUID, str] = {}
    for evidence_id, evidence in run_evidence.items():
        mapped_id = evidence_by_source.get(evidence.provenance.source_id)
        if mapped_id is None:
            raise ValueError(
                "run citation source is not annotated in the evaluation spec: "
                f"{evidence.provenance.source_id}"
            )
        mapped_run_evidence[evidence_id] = mapped_id
    observed_claims: list[AnswerQualityObservedClaim] = []
    for index, claim in enumerate(answer.claims):
        assignment = assignments[index]
        quote = " ".join(assignment.answer_quote.split())
        if quote not in " ".join(answer.answer_markdown.split()):
            raise ValueError(f"answer quote for claim index {index} is not present in the run")
        mapped: list[str] = []
        for evidence_id in claim.evidence_ids:
            claim_evidence = run_evidence.get(evidence_id)
            if claim_evidence is None:
                raise ValueError("a live claim references evidence absent from run citations")
            mapped_id = mapped_run_evidence[evidence_id]
            mapped.append(mapped_id)
        observed_claims.append(
            AnswerQualityObservedClaim(
                claim_id=assignment.expected_claim_id,
                text=claim.text,
                answer_quote=assignment.answer_quote,
                citation_ids=list(dict.fromkeys(mapped)),
            )
        )
    return AnswerQualityArtifactSet(
        provenance=AnswerQualityArtifactProvenance(
            kind="live_run",
            label=f"HermesGraph live run; annotation={annotation.revision}",
            run_ids=[annotation.run_id],
            model_revision=trajectory.context.model,
        ),
        answers=[
            AnswerQualityVariantObservation(
                case_id=case.case_id,
                variant=annotation.variant,
                answer_markdown=answer.answer_markdown,
                claim_inventory_complete=True,
                citation_inventory_complete=True,
                claims=observed_claims,
                cited_evidence_ids=sorted(set(mapped_run_evidence.values())),
            )
        ],
    )


def _validate_variant(variant: AnswerQualityVariant, trajectory: RunTrajectory) -> None:
    answer = trajectory.answer
    assert answer is not None
    route = answer.adaptive_rag_route
    graph_used = bool(answer.graph_paths) or any(
        event.tool_name in {"search_graph", "retrieve_evidence_subgraph", "compare_graph_entities"}
        for event in trajectory.tool_events
    )
    if variant == "graph_rag" and not graph_used:
        raise ValueError("graph_rag artifacts require observed graph evidence or tool use")
    if variant == "vector_only" and graph_used:
        raise ValueError("vector_only artifacts must not contain graph evidence or tool use")
    if variant == "self_rag" and (
        route is None or route.strategy != "multi_step" or not route.self_reflection
    ):
        raise ValueError("self_rag artifacts require an observed reflective multi-step route")
    if variant == "single_step" and (
        route is None or route.strategy != "single_step" or route.self_reflection
    ):
        raise ValueError("single_step artifacts require an observed non-reflective route")


def _shared_model_revision(artifacts: list[AnswerQualityArtifactSet]) -> str | None:
    values = {item.provenance.model_revision for item in artifacts}
    return next(iter(values)) if len(values) == 1 else None


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    raise SystemExit(asyncio.run(run(parser().parse_args())))


if __name__ == "__main__":
    main()
