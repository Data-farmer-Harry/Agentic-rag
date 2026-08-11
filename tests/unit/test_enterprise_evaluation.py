from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain.enums import EvidenceLevel, TrustLevel
from app.domain.models import (
    EvidenceRef,
    GraphNode,
    GraphPath,
    GraphRelationship,
    Provenance,
    RetrievalBundle,
    RunContext,
)
from app.evaluation import enterprise_cli
from app.evaluation.enterprise import (
    EnterpriseAnswerArtifactSet,
    EnterpriseAnswerEvaluator,
    EnterpriseAnswerObservation,
    EnterpriseArtifactProvenance,
    EnterpriseEvaluationDataset,
    EnterpriseEvaluationProvenance,
    EnterpriseGoldenCase,
    EnterpriseGoldenSet,
    EnterpriseGraphArtifactSet,
    EnterpriseGraphAssertionEvaluator,
    EnterpriseGraphObservation,
    EnterpriseJudgeResult,
    compile_enterprise_retrieval_fixture,
    gate_enterprise_retrieval,
    load_enterprise_evaluation_dataset,
)
from app.evaluation.retrieval import (
    AgenticRetrievalEvaluator,
    RetrievalEvalReport,
    RetrievalGoldenCase,
    RetrievalGoldenSet,
)

_DATASET_PATH = Path("examples/enterprise_knowledge/evaluation/golden_questions.json")


@pytest.fixture
def enterprise_dataset() -> EnterpriseEvaluationDataset:
    return load_enterprise_evaluation_dataset(_DATASET_PATH)


def test_enterprise_dataset_validates_and_compiles_existing_retrieval_contract(
    enterprise_dataset: EnterpriseEvaluationDataset,
) -> None:
    fixture = compile_enterprise_retrieval_fixture(enterprise_dataset)

    assert fixture.revision == "2026-08-06-v2"
    assert len(fixture.documents) == 53
    assert len(fixture.cases) == 16
    assert fixture.required_case_ids == enterprise_dataset.golden.required_case_ids
    assert all(case.required_case for case in fixture.cases)
    current_token = next(
        case for case in fixture.cases if case.case_id == "enterprise-current-token-algorithm"
    )
    assert current_token.category == "temporal_conflict"
    assert current_token.difficulty == "version_resolution"
    assert current_token.expected_intent == "knowledge_query"
    assert current_token.expected_source_ids == [
        "northstar:adr:012",
        "northstar:service:sentinel",
    ]
    assert current_token.forbidden_source_ids == ["northstar:adr:009"]
    assert any(
        item.source_id == "northstar:architecture:system-overview" and item.text
        for item in fixture.documents
    )


def test_enterprise_schema_rejects_duplicate_ids_empty_assertions_and_bad_citation_floor(
    enterprise_dataset: EnterpriseEvaluationDataset,
) -> None:
    base = enterprise_dataset.golden.cases[0].model_dump(mode="json")
    duplicate_sources = {**base, "required_source_ids": ["northstar:service:relay"] * 2}
    with pytest.raises(ValidationError, match="required source IDs must be unique"):
        EnterpriseGoldenCase.model_validate(duplicate_sources)

    empty_fact = {**base, "required_facts": [""]}
    with pytest.raises(ValidationError, match="enterprise assertions must not be empty"):
        EnterpriseGoldenCase.model_validate(empty_fact)

    insufficient = next(
        case
        for case in enterprise_dataset.golden.cases
        if case.case_id == "enterprise-nonexistent-component"
    ).model_dump(mode="json")
    insufficient["min_citations"] = 1
    with pytest.raises(ValidationError, match="must require zero citations"):
        EnterpriseGoldenCase.model_validate(insufficient)

    golden = enterprise_dataset.golden.model_dump(mode="json")
    golden["required_case_ids"] = [golden["required_case_ids"][0]] * 2
    with pytest.raises(ValidationError, match="required case IDs must be unique"):
        EnterpriseGoldenSet.model_validate(golden)

    with pytest.raises(ValidationError, match="live_run artifacts require a non-empty run_id"):
        EnterpriseArtifactProvenance(kind="live_run")
    with pytest.raises(ValidationError, match="run_id must not be blank"):
        EnterpriseArtifactProvenance(kind="live_run", run_id="  ")
    with pytest.raises(ValidationError, match="live_system_evidence requires live Qdrant"):
        EnterpriseEvaluationProvenance(live_system_evidence=True)


def test_enterprise_loader_rejects_unknown_source_id(tmp_path: Path) -> None:
    copied_root = tmp_path / "enterprise_knowledge"
    shutil.copytree(_DATASET_PATH.parent.parent, copied_root)
    golden_path = copied_root / "evaluation" / "golden_questions.json"
    payload = json.loads(golden_path.read_text(encoding="utf-8"))
    payload["cases"][0]["required_source_ids"] = ["northstar:unknown:source"]
    golden_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="references unknown sources"):
        load_enterprise_evaluation_dataset(golden_path)


def test_answer_evaluator_passes_complete_grounded_answers_and_keeps_judge_advisory(
    enterprise_dataset: EnterpriseEvaluationDataset,
) -> None:
    observations = _perfect_answers(enterprise_dataset)

    report = EnterpriseAnswerEvaluator(enterprise_dataset).evaluate(
        observations,
        judge=lambda case, answer: EnterpriseJudgeResult(
            score=1.0,
            rationale=f"advisory:{case.case_id}:{len(answer.answer_markdown)}",
        ),
    )

    assert report.passed is True
    assert report.passed_count == report.total == 16
    assert report.required_failed_case_ids == []
    assert report.category_metrics["temporal_conflict"].passed == 1
    injection = next(
        item for item in report.cases if item.case_id == "enterprise-untrusted-ticket-injection"
    )
    assert injection.refusal_detected is True
    assert injection.judge is not None and injection.judge.score == 1.0


def test_answer_evaluator_fails_closed_for_missing_fact_even_with_perfect_judge(
    enterprise_dataset: EnterpriseEvaluationDataset,
) -> None:
    first_case = enterprise_dataset.golden.cases[0]
    observation = EnterpriseAnswerObservation(
        case_id=first_case.case_id,
        answer_markdown="A short unsupported response.",
        citation_source_ids=first_case.required_source_ids,
        confidence=EvidenceLevel.SUPPORTED,
    )
    report = EnterpriseAnswerEvaluator(enterprise_dataset).evaluate(
        [observation],
        judge=lambda case, answer: EnterpriseJudgeResult(score=1.0, rationale="cannot override"),
    )
    result = next(item for item in report.cases if item.case_id == first_case.case_id)

    assert result.passed is False
    assert "missing_required_facts" in result.hard_failures
    assert result.judge is not None and result.judge.score == 1.0
    assert first_case.case_id in report.required_failed_case_ids


def test_answer_evaluator_rejects_insufficient_citations_and_secret_leak(
    enterprise_dataset: EnterpriseEvaluationDataset,
) -> None:
    insufficient = next(
        case
        for case in enterprise_dataset.golden.cases
        if case.case_id == "enterprise-nonexistent-component"
    )
    injection = next(
        case
        for case in enterprise_dataset.golden.cases
        if case.case_id == "enterprise-untrusted-ticket-injection"
    )
    observations = [
        EnterpriseAnswerObservation(
            case_id=insufficient.case_id,
            answer_markdown="知识库中没有 QUASAR-NONE 的可靠记录。",
            citation_source_ids=["northstar:service:polaris"],
            confidence=EvidenceLevel.INSUFFICIENT,
        ),
        EnterpriseAnswerObservation(
            case_id=injection.case_id,
            answer_markdown="\n".join(
                [
                    *injection.required_facts,
                    *injection.required_entities,
                    "ATLAS_PROD_MASTER_KEY=abcdefghi123456789",
                ]
            ),
            citation_source_ids=injection.required_source_ids,
            confidence=EvidenceLevel.SUPPORTED,
        ),
    ]
    report = EnterpriseAnswerEvaluator(enterprise_dataset).evaluate(observations)
    by_case = {item.case_id: item for item in report.cases}

    assert "insufficient_answer_has_citations" in by_case[insufficient.case_id].hard_failures
    assert (
        "embedded_instruction_executed_or_secret_leaked" in by_case[injection.case_id].hard_failures
    )


def test_graph_evaluator_accepts_active_evidence_backed_expected_paths(
    enterprise_dataset: EnterpriseEvaluationDataset,
) -> None:
    report = EnterpriseGraphAssertionEvaluator(enterprise_dataset).evaluate(
        _perfect_graphs(enterprise_dataset)
    )

    assert report.passed is True
    assert report.required_failed_case_ids == []
    assert report.applicable_total == 7
    assert report.category_metrics["architecture"].passed == 1


def test_graph_evaluator_rejects_pending_relation_and_superseded_provenance(
    enterprise_dataset: EnterpriseEvaluationDataset,
) -> None:
    pending = EnterpriseGraphObservation(
        case_id="enterprise-polaris-ownership",
        paths=[
            _graph_path(
                [("Polaris", "Knowledge Systems", "OWNED_BY")],
                source_id="northstar:service:polaris",
                status="pending",
            ),
            _graph_path(
                [("Polaris", "Qdrant", "READS_FROM")],
                source_id="northstar:service:polaris",
                status="pending",
            ),
        ],
    )
    superseded = EnterpriseGraphObservation(
        case_id="enterprise-current-token-algorithm",
        paths=[
            _graph_path(
                [("ADR-012", "ADR-009", "SUPERSEDES")],
                source_id="northstar:adr:009",
            )
        ],
    )
    report = EnterpriseGraphAssertionEvaluator(enterprise_dataset).evaluate([pending, superseded])
    by_case = {item.case_id: item for item in report.cases}

    assert (
        "inactive_or_unproven_graph_path_returned"
        in by_case["enterprise-polaris-ownership"].hard_failures
    )
    assert (
        "graph_relationship_not_active:pending"
        in by_case["enterprise-polaris-ownership"].rejected_path_reasons
    )
    assert (
        "graph_evidence_source_not_active:superseded"
        in by_case["enterprise-current-token-algorithm"].rejected_path_reasons
    )


@pytest.mark.asyncio
async def test_retrieval_required_gate_is_fail_closed_and_knowledge_query_is_compatible() -> None:
    dataset = RetrievalGoldenSet(
        name="enterprise fixture",
        revision="v1",
        documents=[],
        required_case_ids=["required"],
        cases=[
            RetrievalGoldenCase(
                case_id="required",
                query="What owns Polaris?",
                category="ownership",
                difficulty="multi_source",
                expected_source_ids=["northstar:service:polaris"],
                expected_intent="knowledge_query",
                required_case=True,
            )
        ],
    )
    passing = await AgenticRetrievalEvaluator(
        _RetrievalController({"required": ["northstar:service:polaris"]})
    ).run(dataset)
    failing = await AgenticRetrievalEvaluator(_RetrievalController({"required": []})).run(dataset)

    assert passing.required_gate_passed is True
    assert passing.cases[0].planned_intent == "lookup"
    assert failing.required_gate_passed is False
    assert failing.required_failed_case_ids == ["required"]
    assert "required_retrieval_case_failed" in gate_enterprise_retrieval(failing).gate_failures


@pytest.mark.asyncio
async def test_offline_enterprise_fixture_retriever_rejects_unknown_explicit_identifier(
    enterprise_dataset: EnterpriseEvaluationDataset,
) -> None:
    retriever = enterprise_cli._fixture_retriever(
        compile_enterprise_retrieval_fixture(enterprise_dataset)
    )
    evidence = await retriever.retrieve(
        "QUASAR-NONE 服务的负责人是谁？",
        RunContext(),
        filters={"tenant_id": "local", "project_id": "default"},
    )

    assert evidence == ()


@pytest.mark.asyncio
async def test_offline_enterprise_fixture_retrieval_passes_every_required_case(
    enterprise_dataset: EnterpriseEvaluationDataset,
) -> None:
    report = await enterprise_cli._evaluate_fixture_retrieval(
        compile_enterprise_retrieval_fixture(enterprise_dataset)
    )

    assert report.passed == report.total == 16
    assert report.required_gate_passed is True
    assert report.required_failed_case_ids == []
    assert gate_enterprise_retrieval(report).passed is True


@pytest.mark.asyncio
async def test_enterprise_cli_writes_atomic_combined_report(
    enterprise_dataset: EnterpriseEvaluationDataset,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    retrieval_fixture = compile_enterprise_retrieval_fixture(enterprise_dataset)
    retrieval_report = await AgenticRetrievalEvaluator(
        _RetrievalController(
            {case.case_id: case.required_source_ids for case in enterprise_dataset.golden.cases}
        )
    ).run(retrieval_fixture)

    async def evaluate_fixture(_: RetrievalGoldenSet):
        return retrieval_report

    monkeypatch.setattr(enterprise_cli, "_evaluate_fixture_retrieval", evaluate_fixture)
    answers_path = tmp_path / "answers.json"
    graphs_path = tmp_path / "graphs.json"
    answers_path.write_text(
        EnterpriseAnswerArtifactSet(answers=_perfect_answers(enterprise_dataset)).model_dump_json(
            indent=2
        ),
        encoding="utf-8",
    )
    graphs_path.write_text(
        EnterpriseGraphArtifactSet(graphs=_perfect_graphs(enterprise_dataset)).model_dump_json(
            indent=2
        ),
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    fixture_output = tmp_path / "enterprise_retrieval.json"
    args = argparse.Namespace(
        dataset=_DATASET_PATH,
        retrieval_fixture=fixture_output,
        answers=answers_path,
        graphs=graphs_path,
        answer_artifact_provenance="external_unverified",
        answer_run_id=None,
        graph_artifact_provenance="external_unverified",
        graph_run_id=None,
        retrieval_backend="fixture",
        planner_mode="deterministic",
        qdrant_collection=None,
        output=output,
        compile_only=False,
        report_only=False,
    )

    assert await enterprise_cli._run(args) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["required_failed_case_ids"] == []
    assert report["provenance"]["retrieval_backend"] == "fixture"
    assert report["provenance"]["retrieval_evidence"] == "offline_fixture"
    assert report["provenance"]["live_system_evidence"] is False
    assert report["provenance"]["production_gate_passed"] is False
    assert "offline_fixture_retrieval_not_production" in report["provenance"]["limitations"]
    assert fixture_output.is_file()
    assert not _temporary_files(tmp_path)


@pytest.mark.asyncio
async def test_enterprise_cli_qdrant_live_artifacts_can_pass_production_gate(
    enterprise_dataset: EnterpriseEvaluationDataset,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    retrieval_fixture = compile_enterprise_retrieval_fixture(enterprise_dataset)
    retrieval_report = await AgenticRetrievalEvaluator(
        _RetrievalController(
            {case.case_id: case.required_source_ids for case in enterprise_dataset.golden.cases}
        )
    ).run(retrieval_fixture)

    async def evaluate_qdrant(
        fixture: RetrievalGoldenSet,
        *,
        settings: Settings,
        collection_name: str,
        planner_mode: str,
    ) -> RetrievalEvalReport:
        assert fixture == retrieval_fixture
        assert settings.qdrant_url == "http://qdrant.test"
        assert collection_name == "enterprise_live"
        assert planner_mode == "deterministic"
        return retrieval_report

    monkeypatch.setattr(enterprise_cli, "get_settings", lambda: _qdrant_settings())
    monkeypatch.setattr(enterprise_cli, "_evaluate_qdrant_retrieval", evaluate_qdrant)
    answers_path, graphs_path = _write_perfect_artifacts(tmp_path, enterprise_dataset)
    output = tmp_path / "qdrant-live-report.json"
    args = argparse.Namespace(
        dataset=_DATASET_PATH,
        retrieval_fixture=tmp_path / "enterprise_retrieval.json",
        answers=answers_path,
        graphs=graphs_path,
        answer_artifact_provenance="live_run",
        answer_run_id="answer-run-20260803-01",
        graph_artifact_provenance="live_run",
        graph_run_id="graph-run-20260803-01",
        retrieval_backend="qdrant",
        planner_mode="deterministic",
        qdrant_collection="enterprise_live",
        output=output,
        compile_only=False,
        report_only=False,
    )

    assert await enterprise_cli._run(args) == 0
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["passed"] is True
    assert report["provenance"] == {
        "retrieval_backend": "qdrant",
        "retrieval_evidence": "live_qdrant_read_only",
        "planner_mode": "deterministic",
        "qdrant_collection": "enterprise_live",
        "answer_artifact": {"kind": "live_run", "run_id": "answer-run-20260803-01"},
        "graph_artifact": {"kind": "live_run", "run_id": "graph-run-20260803-01"},
        "live_system_evidence": True,
        "production_gate_passed": True,
        "limitations": ["live_artifact_provenance_is_declared_by_the_evaluation_harness"],
    }


@pytest.mark.asyncio
async def test_enterprise_cli_qdrant_missing_artifacts_fails_closed_with_provenance(
    enterprise_dataset: EnterpriseEvaluationDataset,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    retrieval_fixture = compile_enterprise_retrieval_fixture(enterprise_dataset)
    retrieval_report = await AgenticRetrievalEvaluator(
        _RetrievalController(
            {case.case_id: case.required_source_ids for case in enterprise_dataset.golden.cases}
        )
    ).run(retrieval_fixture)

    async def evaluate_qdrant(
        _: RetrievalGoldenSet,
        *,
        settings: Settings,
        collection_name: str,
        planner_mode: str,
    ) -> RetrievalEvalReport:
        assert settings.qdrant_url == "http://qdrant.test"
        assert collection_name == "enterprise_live"
        assert planner_mode == "deterministic"
        return retrieval_report

    monkeypatch.setattr(enterprise_cli, "get_settings", lambda: _qdrant_settings())
    monkeypatch.setattr(enterprise_cli, "_evaluate_qdrant_retrieval", evaluate_qdrant)
    output = tmp_path / "qdrant-missing-artifacts-report.json"
    args = argparse.Namespace(
        dataset=_DATASET_PATH,
        retrieval_fixture=tmp_path / "enterprise_retrieval.json",
        answers=None,
        graphs=None,
        answer_artifact_provenance="external_unverified",
        answer_run_id=None,
        graph_artifact_provenance="external_unverified",
        graph_run_id=None,
        retrieval_backend="qdrant",
        planner_mode="deterministic",
        qdrant_collection="enterprise_live",
        output=output,
        compile_only=False,
        report_only=False,
    )

    assert await enterprise_cli._run(args) == 1
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["passed"] is False
    assert report["provenance"]["retrieval_evidence"] == "live_qdrant_read_only"
    assert report["provenance"]["answer_artifact"]["kind"] == "not_provided"
    assert report["provenance"]["graph_artifact"]["kind"] == "not_provided"
    assert report["provenance"]["production_gate_passed"] is False
    assert "live_answer_artifact_required" in report["gate_failures"]
    assert "live_graph_artifact_required" in report["gate_failures"]


@pytest.mark.asyncio
async def test_qdrant_gate_rejects_missing_collection_without_create_or_index(
    enterprise_dataset: EnterpriseEvaluationDataset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingCollectionClient:
        closed = False

        async def collection_exists(self, collection_name: str) -> bool:
            assert collection_name == "enterprise_live"
            return False

        async def close(self) -> None:
            self.closed = True

    client = MissingCollectionClient()
    monkeypatch.setattr(enterprise_cli, "_qdrant_client", lambda _: client)

    with pytest.raises(RuntimeError, match="will not create or index"):
        await enterprise_cli._evaluate_qdrant_retrieval(
            compile_enterprise_retrieval_fixture(enterprise_dataset),
            settings=_qdrant_settings(),
            collection_name="enterprise_live",
            planner_mode="deterministic",
        )

    assert client.closed is True


@pytest.mark.asyncio
async def test_read_only_qdrant_store_refuses_late_collection_deletion() -> None:
    class DeletedCollectionClient:
        closed = False
        create_attempted = False

        async def collection_exists(self, collection_name: str) -> bool:
            assert collection_name == "enterprise_live"
            return False

        async def create_collection(self, **_: object) -> None:
            self.create_attempted = True
            raise AssertionError("read-only enterprise evaluation must not create collections")

        async def close(self) -> None:
            self.closed = True

    client = DeletedCollectionClient()
    store = enterprise_cli.ReadOnlyEnterpriseQdrantStore(
        client,
        enterprise_cli.DeterministicDenseEmbedder(64),
        enterprise_cli.build_sparse_embedder("hashed"),
        collection_name="enterprise_live",
        create_payload_indexes=False,
    )
    try:
        with pytest.raises(RuntimeError, match="will not recreate"):
            await store.ensure_collection()
    finally:
        await store.close()

    assert client.create_attempted is False
    assert client.closed is True


@pytest.mark.asyncio
async def test_qdrant_gate_disables_payload_index_creation(
    enterprise_dataset: EnterpriseEvaluationDataset,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrieval_fixture = compile_enterprise_retrieval_fixture(enterprise_dataset)
    expected_report = await AgenticRetrievalEvaluator(
        _RetrievalController(
            {case.case_id: case.required_source_ids for case in enterprise_dataset.golden.cases}
        )
    ).run(retrieval_fixture)
    store_options: list[dict[str, object]] = []
    stores: list[ReadOnlyStore] = []

    class ExistingCollectionClient:
        closed = False

        async def collection_exists(self, collection_name: str) -> bool:
            assert collection_name == "enterprise_live"
            return True

        async def close(self) -> None:
            self.closed = True

    class ReadOnlyStore:
        closed = False

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.client = args[0]
            store_options.append(kwargs)
            stores.append(self)

        async def close(self) -> None:
            self.closed = True
            await self.client.close()

    class Controller:
        closed = False

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def close(self) -> None:
            self.closed = True

    class Evaluator:
        def __init__(self, controller: object) -> None:
            assert isinstance(controller, Controller)

        async def run(self, fixture: RetrievalGoldenSet) -> RetrievalEvalReport:
            assert fixture == retrieval_fixture
            return expected_report

    client = ExistingCollectionClient()
    monkeypatch.setattr(enterprise_cli, "_qdrant_client", lambda _: client)
    monkeypatch.setattr(enterprise_cli, "ReadOnlyEnterpriseQdrantStore", ReadOnlyStore)
    monkeypatch.setattr(enterprise_cli, "AgenticRetrievalController", Controller)
    monkeypatch.setattr(enterprise_cli, "AgenticRetrievalEvaluator", Evaluator)

    report = await enterprise_cli._evaluate_qdrant_retrieval(
        retrieval_fixture,
        settings=_qdrant_settings(),
        collection_name="enterprise_live",
        planner_mode="deterministic",
    )

    assert report == expected_report
    assert store_options == [{
        "collection_name": "enterprise_live",
        "prefetch_limit": 40,
        "rrf_k": 60,
        "create_payload_indexes": False,
        "use_sparse_idf": False,
    }]
    assert stores[0].closed is True
    assert client.closed is True


def _qdrant_settings() -> Settings:
    return Settings(
        qdrant_url="http://qdrant.test",
        embedding_provider="deterministic",
    )


def _write_perfect_artifacts(
    directory: Path,
    dataset: EnterpriseEvaluationDataset,
) -> tuple[Path, Path]:
    answers_path = directory / "answers.json"
    graphs_path = directory / "graphs.json"
    answers_path.write_text(
        EnterpriseAnswerArtifactSet(answers=_perfect_answers(dataset)).model_dump_json(indent=2),
        encoding="utf-8",
    )
    graphs_path.write_text(
        EnterpriseGraphArtifactSet(graphs=_perfect_graphs(dataset)).model_dump_json(indent=2),
        encoding="utf-8",
    )
    return answers_path, graphs_path


def _perfect_answers(dataset: EnterpriseEvaluationDataset) -> list[EnterpriseAnswerObservation]:
    return [
        EnterpriseAnswerObservation(
            case_id=case.case_id,
            answer_markdown="\n".join([*case.required_facts, *case.required_entities]),
            citation_source_ids=case.required_source_ids,
            confidence=(
                EvidenceLevel.INSUFFICIENT if case.expect_insufficient else EvidenceLevel.SUPPORTED
            ),
        )
        for case in dataset.golden.cases
    ]


def _perfect_graphs(dataset: EnterpriseEvaluationDataset) -> list[EnterpriseGraphObservation]:
    del dataset
    return [
        EnterpriseGraphObservation(
            case_id="enterprise-architecture-main-path",
            paths=[
                _graph_path(
                    [
                        ("Gatehouse", "Relay", "ROUTES_TO"),
                        ("Relay", "Polaris", "CALLS"),
                    ],
                    source_id="northstar:architecture:system-overview",
                ),
                _graph_path(
                    [
                        ("Relay", "Constellation", "CALLS"),
                        ("Constellation", "Neo4j", "READS_FROM"),
                    ],
                    source_id="northstar:architecture:request-data-flow",
                ),
            ],
        ),
        EnterpriseGraphObservation(
            case_id="enterprise-polaris-ownership",
            paths=[
                _graph_path(
                    [("Polaris", "Knowledge Systems", "OWNED_BY")],
                    source_id="northstar:service:polaris",
                ),
                _graph_path(
                    [("Polaris", "Qdrant", "READS_FROM")],
                    source_id="northstar:service:polaris",
                ),
            ],
        ),
        EnterpriseGraphObservation(
            case_id="enterprise-current-token-algorithm",
            paths=[
                _graph_path(
                    [("ADR-012", "ADR-009", "SUPERSEDES")],
                    source_id="northstar:adr:012",
                )
            ],
        ),
        EnterpriseGraphObservation(
            case_id="enterprise-jwks-affected-services",
            paths=[
                _graph_path(
                    [("Gatehouse", "Sentinel", "DEPENDS_ON")],
                    source_id="northstar:adr:012",
                ),
                _graph_path(
                    [("Relay", "Sentinel", "DEPENDS_ON")],
                    source_id="northstar:adr:012",
                ),
            ],
        ),
        EnterpriseGraphObservation(
            case_id="enterprise-outbox-convergence",
            paths=[
                _graph_path(
                    [("Foundry", "Transactional Outbox", "PUBLISHES_TO")],
                    source_id="northstar:architecture:event-driven-ingestion",
                ),
                _graph_path(
                    [("Outbox Dispatcher", "Transactional Outbox", "CONSUMES_FROM")],
                    source_id="northstar:data:transactional-outbox",
                ),
            ],
        ),
        EnterpriseGraphObservation(
            case_id="enterprise-provider-overload-incident",
            paths=[
                _graph_path(
                    [
                        ("INC-2026-0712", "Prism", "AFFECTED"),
                        ("Prism", "AI Runtime", "OWNED_BY"),
                    ],
                    source_id="northstar:incident:2026-0712",
                ),
                _graph_path(
                    [
                        (
                            "INC-2026-0712",
                            "Model Provider Degradation Runbook",
                            "MITIGATED_BY",
                        )
                    ],
                    source_id="northstar:runbook:model-provider-degradation",
                ),
            ],
        ),
        EnterpriseGraphObservation(
            case_id="enterprise-kubernetes-security",
            paths=[
                _graph_path(
                    [
                        ("Kubernetes", "Relay", "HOSTS"),
                        ("Relay", "Polaris", "CALLS"),
                        ("Polaris", "Qdrant", "READS_FROM"),
                    ],
                    source_id="northstar:infrastructure:kubernetes-platform",
                )
            ],
        ),
    ]


def _graph_path(
    edges: list[tuple[str, str, str]],
    *,
    source_id: str,
    status: str = "active",
) -> GraphPath:
    names = list(dict.fromkeys([name for source, target, _ in edges for name in (source, target)]))
    nodes = {
        name: GraphNode(
            node_id=f"node:{name}",
            tenant_id="local",
            project_id="default",
            label="Entity",
            name=name,
        )
        for name in names
    }
    evidence = EvidenceRef(
        text=f"Evidence for {source_id}",
        provenance=Provenance(
            source_type="enterprise_internal",
            source_id=f"{source_id}#chunk=0",
            trust=TrustLevel.VERIFIED,
        ),
    )
    relationships = [
        GraphRelationship(
            relationship_id=f"relationship:{index}",
            tenant_id="local",
            project_id="default",
            relation_type=relation_type,
            source_node_id=nodes[source].node_id,
            target_node_id=nodes[target].node_id,
            properties={"status": status, "candidate_status": "approved"},
            evidence=[evidence],
        )
        for index, (source, target, relation_type) in enumerate(edges)
    ]
    return GraphPath(nodes=list(nodes.values()), relationships=relationships, evidence=[evidence])


class _RetrievalController:
    def __init__(self, source_ids_by_case: dict[str, list[str]]) -> None:
        self._source_ids_by_case = source_ids_by_case

    async def retrieve(
        self,
        query: str,
        context: RunContext,
        *,
        filters: dict[str, object] | None = None,
        top_k: int = 10,
    ) -> RetrievalBundle:
        del query, filters, top_k
        case_id = context.session_id.removeprefix("retrieval-eval:")
        evidence = [
            EvidenceRef(
                text=f"Evidence for {source_id}",
                provenance=Provenance(
                    source_type="enterprise_evaluation_fixture",
                    source_id=f"{source_id}#chunk=0",
                    trust=TrustLevel.VERIFIED,
                ),
                metadata={"tenant_id": context.tenant_id, "project_id": context.project_id},
            )
            for source_id in self._source_ids_by_case.get(case_id, [])
        ]
        return RetrievalBundle(
            query="fixture",
            evidence=evidence,
            trace={"plan": {"intent": "lookup"}, "rounds": []},
        )


def _temporary_files(path: Path) -> list[Path]:
    return list(path.glob(".*.tmp"))
