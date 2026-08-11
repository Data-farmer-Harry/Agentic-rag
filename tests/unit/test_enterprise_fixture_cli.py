from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.config import Settings
from app.demo import enterprise_fixture_cli
from app.demo.enterprise_fixture import (
    EnterpriseFixturePreview,
    EnterpriseFixtureResetResult,
    EnterpriseFixtureRun,
    FixtureImportPlanItem,
)
from app.domain.enums import DocumentStatus, GraphCandidateStatus

_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "enterprise_knowledge"


class _FakeRepository:
    def __init__(self, documents: list[SimpleNamespace]) -> None:
        self._documents = documents

    async def list_documents(self, **_: object) -> list[SimpleNamespace]:
        return self._documents


class _FakeGraphCandidates:
    def __init__(self, collection: SimpleNamespace) -> None:
        self._collection = collection

    async def list_candidates(self, **_: object) -> SimpleNamespace:
        return self._collection


class _FakeWorkspace:
    def __init__(
        self,
        *,
        preview: EnterpriseFixturePreview,
        run: EnterpriseFixtureRun,
        reset: EnterpriseFixtureResetResult,
        on_reset: Callable[[], None] | None = None,
    ) -> None:
        self._preview = preview
        self._run = run
        self._reset = reset
        self._on_reset = on_reset
        self.reset_calls = 0

    async def preview_enterprise_fixture(self, **_: object) -> EnterpriseFixturePreview:
        return self._preview

    async def start_enterprise_fixture(self, **_: object) -> EnterpriseFixtureRun:
        return self._run

    async def enterprise_fixture_status(
        self,
        run_id: UUID,
        **_: object,
    ) -> EnterpriseFixtureRun | None:
        return self._run if run_id == self._run.run_id else None

    async def reset_enterprise_fixture(self, **_: object) -> EnterpriseFixtureResetResult:
        self.reset_calls += 1
        if self._on_reset is not None:
            self._on_reset()
        return self._reset


class _FakeComponents:
    def __init__(
        self,
        *,
        workspace: _FakeWorkspace,
        documents: list[SimpleNamespace],
        candidates: SimpleNamespace,
    ) -> None:
        self.workspace_service = workspace
        self.knowledge_repository = _FakeRepository(documents)
        self.graph_candidate_service = _FakeGraphCandidates(candidates)
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


def _components() -> tuple[_FakeComponents, EnterpriseFixtureRun]:
    document_id = uuid4()
    plan = FixtureImportPlanItem(
        source_id="northstar:architecture:system-overview",
        filename="system-overview.md",
        content_hash="a" * 64,
        source_revision="2026.06",
        action="unchanged",
    )
    run = EnterpriseFixtureRun(
        fixture_id="enterprise_knowledge",
        manifest_revision="northstar-r3",
        tenant_id="local",
        project_id="default",
        requested_by="fixture-cli",
        dry_run=False,
        fixture_fingerprint="b" * 64,
        lifecycle_generation=7,
        status="succeeded",
        plan=[plan],
        completed_document_ids={"northstar:architecture:system-overview": document_id},
        curated_graph_entities=16,
        curated_graph_relations=16,
    )
    preview = EnterpriseFixturePreview(
        fixture_id="enterprise_knowledge",
        manifest_revision="northstar-r3",
        tenant_id="local",
        project_id="default",
        documents=[plan],
        counts={"unchanged": 1},
    )
    reset = EnterpriseFixtureResetResult(
        fixture_id="enterprise_knowledge",
        tenant_id="local",
        project_id="default",
        archived_document_ids=[document_id],
    )
    documents = [
        SimpleNamespace(
            document_id=document_id,
            source=SimpleNamespace(fixture_id="enterprise_knowledge"),
            status=DocumentStatus.ACTIVE,
            chunk_count=3,
        )
    ]
    curated_entity = SimpleNamespace(
        document_id=uuid4(),
        extractor_revision="curated-enterprise-fixture:northstar-r3",
        status=GraphCandidateStatus.APPROVED,
    )
    archived_curated_entity = SimpleNamespace(
        document_id=uuid4(),
        extractor_revision="curated-enterprise-fixture:northstar-r2",
        status=GraphCandidateStatus.ARCHIVED,
    )
    curated_relation = SimpleNamespace(
        document_id=uuid4(),
        extractor_revision="curated-enterprise-fixture:northstar-r3",
        status=GraphCandidateStatus.APPROVED,
    )
    document_candidate = SimpleNamespace(
        document_id=document_id,
        extractor_revision="openai-graph-extraction-v6",
        status=GraphCandidateStatus.PENDING,
    )
    candidates = SimpleNamespace(
        entities=[curated_entity, archived_curated_entity, document_candidate],
        relations=[curated_relation, document_candidate],
    )
    workspace = _FakeWorkspace(preview=preview, run=run, reset=reset)
    return _FakeComponents(
        workspace=workspace,
        documents=documents,
        candidates=candidates,
    ), run


def _install_components(
    monkeypatch: pytest.MonkeyPatch,
    components: _FakeComponents,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        enterprise_fixture_cli,
        "get_settings",
        lambda: Settings(app_env="test", data_dir=tmp_path),
    )
    monkeypatch.setattr(enterprise_fixture_cli, "build_components", lambda _: components)


def test_parser_preserves_existing_flags_and_adds_run_status_lookup() -> None:
    run_id = uuid4()

    imported = enterprise_fixture_cli.parser().parse_args(["--dry-run", "--no-wait"])
    reset = enterprise_fixture_cli.parser().parse_args(["--reset-fixture"])
    status = enterprise_fixture_cli.parser().parse_args(["--status", str(run_id)])

    assert imported.dry_run is True
    assert imported.no_wait is True
    assert imported.reset_fixture is False
    assert imported.status is None
    assert reset.reset_fixture is True
    assert reset.status is None
    assert status.status == run_id
    assert status.reset_fixture is False


@pytest.mark.asyncio
async def test_import_output_preserves_legacy_envelope_and_adds_observability(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    components, run = _components()
    _install_components(monkeypatch, components, tmp_path)

    exit_code = await enterprise_fixture_cli.run(
        argparse.Namespace(
            root=_FIXTURE_ROOT,
            tenant="local",
            project="default",
            dry_run=False,
            reset_fixture=False,
            no_wait=False,
            status=None,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert set(("preview", "run", "summary")) <= set(payload)
    assert payload["run"]["run_id"] == str(run.run_id)
    assert payload["summary"]["graph_candidates"] == 2
    assert payload["summary"]["curated_graph_entities"] == 1
    assert payload["summary"]["curated_graph_relations"] == 1
    assert payload["summary"]["archived_curated_graph_entities"] == 1
    assert payload["summary"]["run_curated_graph_entities"] == 16
    assert payload["lifecycle"] == {
        "operation": "import",
        "run_id": str(run.run_id),
        "status": "succeeded",
        "fixture_fingerprint": "b" * 64,
        "lifecycle_generation": 7,
        "generation_observed": True,
        "created_at": payload["run"]["created_at"],
        "updated_at": payload["run"]["updated_at"],
        "completed_at": payload["run"]["completed_at"],
    }
    assert payload["curated_graph"] == {
        "entities": 1,
        "relations": 1,
        "archived_entities": 1,
        "archived_relations": 0,
        "run_entities": 16,
        "run_relations": 16,
    }
    assert components.started is True
    assert components.closed is True


@pytest.mark.asyncio
async def test_status_and_reset_outputs_include_lifecycle_and_curated_graph_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    components, run = _components()
    _install_components(monkeypatch, components, tmp_path)

    def archive_after_reset() -> None:
        for document in components.knowledge_repository._documents:
            document.status = DocumentStatus.ARCHIVED
        collection = components.graph_candidate_service._collection
        for candidate in [*collection.entities, *collection.relations]:
            if candidate.extractor_revision.startswith("curated-enterprise-fixture:"):
                candidate.status = GraphCandidateStatus.ARCHIVED

    components.workspace_service._on_reset = archive_after_reset

    status_exit = await enterprise_fixture_cli.run(
        argparse.Namespace(
            root=_FIXTURE_ROOT,
            tenant="local",
            project="default",
            dry_run=False,
            reset_fixture=False,
            no_wait=False,
            status=run.run_id,
        )
    )
    status_payload = json.loads(capsys.readouterr().out)

    reset_exit = await enterprise_fixture_cli.run(
        argparse.Namespace(
            root=_FIXTURE_ROOT,
            tenant="local",
            project="default",
            dry_run=False,
            reset_fixture=True,
            no_wait=False,
            status=None,
        )
    )
    reset_payload = json.loads(capsys.readouterr().out)

    assert status_exit == 0
    assert status_payload["run"]["run_id"] == str(run.run_id)
    assert status_payload["lifecycle"]["operation"] == "status"
    assert status_payload["lifecycle"]["fixture_fingerprint"] == "b" * 64
    assert status_payload["lifecycle"]["lifecycle_generation"] == 7
    assert status_payload["summary"]["curated_graph_entities"] == 1
    assert status_payload["summary"]["curated_graph_relations"] == 1
    assert status_payload["curated_graph"]["entities"] == 1
    assert status_payload["curated_graph"]["relations"] == 1

    assert reset_exit == 0
    assert reset_payload["fixture_id"] == "enterprise_knowledge"
    assert reset_payload["archived_document_ids"]
    assert reset_payload["lifecycle"]["operation"] == "reset"
    assert reset_payload["lifecycle"]["status"] == "reset"
    assert reset_payload["lifecycle"]["lifecycle_generation"] is None
    assert reset_payload["lifecycle"]["generation_observed"] is False
    assert len(reset_payload["lifecycle"]["fixture_fingerprint"]) == 64
    assert reset_payload["summary"]["curated_graph_entities"] == 0
    assert reset_payload["summary"]["curated_graph_relations"] == 0
    assert reset_payload["curated_graph"] == {
        "entities": 0,
        "relations": 0,
        "archived_entities": 2,
        "archived_relations": 1,
        "run_entities": None,
        "run_relations": None,
    }
    assert components.workspace_service.reset_calls == 1


@pytest.mark.asyncio
async def test_status_not_found_is_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    components, _ = _components()
    _install_components(monkeypatch, components, tmp_path)

    exit_code = await enterprise_fixture_cli.run(
        argparse.Namespace(
            root=_FIXTURE_ROOT,
            tenant="local",
            project="default",
            dry_run=False,
            reset_fixture=False,
            no_wait=False,
            status=uuid4(),
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "not_found"
    assert payload["lifecycle"]["operation"] == "status"
    assert payload["lifecycle"]["fixture_fingerprint"] is None
    assert payload["curated_graph"]["entities"] is None
    assert payload["curated_graph"]["relations"] is None
