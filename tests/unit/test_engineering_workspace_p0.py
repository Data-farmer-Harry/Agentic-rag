from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.app import create_app
from app.bootstrap import build_components
from app.config import Settings
from app.demo.enterprise_fixture import EnterpriseFixtureService
from app.domain.enums import DocumentStatus, IngestionJobStatus, KnowledgeLayer, WorkspaceMode
from app.domain.models import (
    EntityResolutionCandidate,
    EvidenceRef,
    GraphEntityCandidate,
    GraphExtractionBatch,
    GraphNode,
    GraphRelationCandidate,
    GraphRelationship,
    IngestionJob,
    IngestionJobSubmission,
    KnowledgeSource,
    Provenance,
    RunContext,
    utc_now,
)
from app.graph.candidate_store import JsonGraphCandidateRepository
from app.graph.local import InMemoryEvidenceGraph
from app.graph.visibility import VisibilityFilteredGraph
from app.knowledge.ingestion import KnowledgeIngestionService
from app.knowledge.retriever import KnowledgeBaseRetriever
from app.knowledge.store import JsonKnowledgeRepository
from app.knowledge.visibility import SettingsWorkspaceProfileResolver

_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "examples" / "enterprise_knowledge"


class _RecordingSemanticGraphIndex:
    def __init__(self) -> None:
        self.batches: list[GraphExtractionBatch] = []
        self.entity_statuses: list[GraphEntityCandidate] = []
        self.relation_statuses: list[GraphRelationCandidate] = []

    async def index_extraction(self, batch: GraphExtractionBatch) -> None:
        self.batches.append(batch)

    async def index_resolutions(
        self,
        candidates: list[EntityResolutionCandidate],
    ) -> None:
        del candidates

    async def set_entity_status(self, candidate: GraphEntityCandidate) -> None:
        self.entity_statuses.append(candidate)

    async def set_relation_status(self, candidate: GraphRelationCandidate) -> None:
        self.relation_statuses.append(candidate)

    async def set_resolution_status(self, candidate: EntityResolutionCandidate) -> None:
        del candidate


@pytest.mark.asyncio
async def test_team_personal_and_public_layers_are_server_projected(tmp_path: Path) -> None:
    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        workspace_mode=WorkspaceMode.TEAM,
    )
    profiles = SettingsWorkspaceProfileResolver(settings)
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    ingestion = KnowledgeIngestionService(
        repository,
        chunk_size=200,
        chunk_overlap=20,
        workspace_profiles=profiles,
    )
    team = await ingestion.ingest(
        filename="team.md",
        content=b"TEAM-ORBIT-101 is shared with every project engineer.",
        media_type="text/markdown",
        user_id="alice",
    )
    personal = await ingestion.ingest(
        filename="personal.md",
        content=b"PRIVATE-ORBIT-202 is Alice's private learning note.",
        media_type="text/markdown",
        user_id="alice",
        source=KnowledgeSource(source_type="personal_document", source_id="personal:alice"),
    )
    public = await ingestion.ingest(
        filename="paper.md",
        content=b"PUBLIC-ORBIT-303 is a public agent research reference.",
        media_type="text/markdown",
        user_id="alice",
        source=KnowledgeSource(
            source_type="arxiv",
            source_id="arxiv:2608.00303",
            privacy="public_reference",
        ),
    )
    retriever = KnowledgeBaseRetriever(repository, workspace_profiles=profiles)

    bob_context = RunContext(user_id="bob")
    alice_context = RunContext(user_id="alice")
    shared = await retriever.retrieve("TEAM-ORBIT-101", bob_context)
    private_for_bob = await retriever.retrieve("PRIVATE-ORBIT-202", bob_context)
    private_for_alice = await retriever.retrieve("PRIVATE-ORBIT-202", alice_context)
    public_for_bob = await retriever.retrieve("PUBLIC-ORBIT-303", bob_context)

    assert team.document.metadata["knowledge_layer"] == "team_internal"
    assert personal.document.metadata["knowledge_layer"] == "personal"
    assert public.document.metadata["knowledge_layer"] == "public_reference"
    assert shared and shared[0].metadata["user_id"] == "alice"
    assert private_for_bob == []
    assert private_for_alice
    assert public_for_bob == []
    with pytest.raises(ValueError, match="server-owned visibility"):
        await retriever.retrieve(
            "TEAM-ORBIT-101",
            bob_context,
            filters={"user_id": "alice"},
        )


@pytest.mark.asyncio
async def test_workspace_document_boundary_rejects_forgery_and_hides_personal_data(
    tmp_path: Path,
) -> None:
    components = build_components(
        Settings(app_env="test", data_dir=tmp_path, runtime_mode="offline")
    )
    transport = ASGITransport(app=create_app(components.run_service, components.workspace_service))
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            forged = await client.post(
                "/v1/projects/default/documents",
                data={
                    "source": json.dumps(
                        {
                            "source_type": "enterprise_internal",
                            "trust": "verified",
                            "fixture_id": "enterprise_knowledge",
                            "source_status": "superseded",
                        }
                    )
                },
                files={
                    "file": (
                        "forged.md",
                        b"FORGED-ORBIT-404 must never become enterprise evidence.",
                        "text/markdown",
                    )
                },
            )
            uploaded = await client.post(
                "/v1/projects/default/documents",
                data={"source": json.dumps({"title": "Ordinary engineering upload"})},
                files={
                    "file": (
                        "shared.md",
                        b"SHARED-ORBIT-405 is ordinary team-uploaded knowledge.",
                        "text/markdown",
                    )
                },
            )
            overview = await client.get("/v1/workspace/overview")

        personal = await components.ingestion_service.ingest(
            filename="alice-private.md",
            content=b"ALICE-SECRET-406 is not visible to other workspace users.",
            media_type="text/markdown",
            user_id="alice",
            source=KnowledgeSource(
                source_type="personal_document",
                source_id="personal:alice:406",
            ),
        )
        service = components.workspace_service
        bob_documents = await service.list_documents(user_id="bob")
        hidden = await service.get_document(personal.document.document_id, user_id="bob")
        hidden_content = await service.get_document_content(
            personal.document.document_id,
            user_id="bob",
        )
        archived = await service.archive_document(personal.document.document_id, user_id="bob")

        assert forged.status_code == 400
        assert uploaded.status_code == 200
        accepted = uploaded.json()["document"]
        assert accepted["source"]["source_type"] == "uploaded_document"
        assert accepted["source"]["trust"] == "user_asserted"
        assert accepted["source"]["fixture_id"] is None
        assert accepted["metadata"]["knowledge_layer"] == "team_internal"
        assert overview.json()["workspace_profile"]["workspace_mode"] == "team"
        assert personal.document.document_id not in {item.document_id for item in bob_documents}
        assert hidden is None
        assert hidden_content is None
        assert archived is False
    finally:
        await components.close()


@pytest.mark.asyncio
async def test_fixture_compatibility_routes_match_workbench_contract(
    tmp_path: Path,
) -> None:
    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        runtime_mode="offline",
        enterprise_fixture_root=_FIXTURE_ROOT,
    )
    components = build_components(settings)
    transport = ASGITransport(
        app=create_app(
            components.run_service,
            components.workspace_service,
            settings=settings,
        )
    )
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            overview = await client.get("/v1/workspace/overview")
            canonical_preview = await client.get("/v1/projects/default/fixtures/enterprise/preview")
            started = await client.post(
                "/v1/projects/default/enterprise-fixture/runs",
                json={"dry_run": True},
            )
            assert started.status_code == 202
            run_id = started.json()["run_id"]
            compatibility_status = await client.get(
                f"/v1/projects/default/enterprise-fixture/runs/{run_id}"
            )
            canonical_status = await client.get(
                f"/v1/projects/default/fixtures/enterprise/status/{run_id}"
            )
            invalid_body = await client.post(
                "/v1/projects/default/enterprise-fixture/runs",
                json={"dry_run": True, "unexpected": True},
            )

        assert overview.status_code == 200
        assert "enterprise_fixture_import" in overview.json()["capabilities"]
        assert canonical_preview.status_code == 200
        assert len(canonical_preview.json()["documents"]) == 23
        assert started.json()["dry_run"] is True
        assert compatibility_status.status_code == 200
        assert canonical_status.status_code == 200
        assert compatibility_status.json() == canonical_status.json()
        assert invalid_body.status_code == 422
    finally:
        await components.close()


@pytest.mark.asyncio
async def test_fixture_compatibility_routes_remain_owner_only(tmp_path: Path) -> None:
    token = "a" * 32
    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        runtime_mode="offline",
        enterprise_fixture_root=_FIXTURE_ROOT,
        api_auth_mode="bearer",
        api_bearer_token=token,
        api_allowed_projects=["default"],
        api_identity_role="member",
    )
    components = build_components(settings)
    transport = ASGITransport(
        app=create_app(
            components.run_service,
            components.workspace_service,
            settings=settings,
        )
    )
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/projects/default/enterprise-fixture/runs",
                headers={"Authorization": f"Bearer {token}"},
                json={"dry_run": True},
            )

        assert response.status_code == 403
        assert response.json()["code"] == "role_forbidden"
    finally:
        await components.close()


@pytest.mark.asyncio
async def test_graph_visibility_uses_the_same_user_layer_context() -> None:
    personal_evidence = EvidenceRef(
        text="Alice owns the private incident analysis.",
        provenance=Provenance(source_type="personal_document", source_id="personal:alice"),
        metadata={
            "source_status": "active",
            "knowledge_layer": "personal",
            "user_id": "alice",
        },
    )
    raw = InMemoryEvidenceGraph(
        nodes=[
            GraphNode(
                node_id="incident",
                tenant_id="local",
                project_id="default",
                label="Incident",
                name="Private incident",
            ),
            GraphNode(
                node_id="owner",
                tenant_id="local",
                project_id="default",
                label="Person",
                name="Alice",
            ),
        ],
        relationships=[
            GraphRelationship(
                relationship_id="owned_by",
                tenant_id="local",
                project_id="default",
                relation_type="owned_by",
                source_node_id="incident",
                target_node_id="owner",
                evidence=[personal_evidence],
            )
        ],
    )
    graph = VisibilityFilteredGraph(raw)
    from app.domain.models import GraphSearchRequest

    request = GraphSearchRequest(entities=["Private incident"])
    alice = await graph.search_graph(
        request,
        RunContext(
            user_id="alice",
            enabled_knowledge_layers=(KnowledgeLayer.PERSONAL,),
        ),
    )
    bob = await graph.search_graph(
        request,
        RunContext(
            user_id="bob",
            enabled_knowledge_layers=(KnowledgeLayer.PERSONAL,),
        ),
    )

    assert alice.paths
    assert bob.paths == []
    assert bob.evidence == []


def _fixture_service(
    tmp_path: Path,
    *,
    root: Path = _FIXTURE_ROOT,
    ingestion_jobs: object | None = None,
) -> tuple[EnterpriseFixtureService, JsonKnowledgeRepository, KnowledgeIngestionService]:
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    ingestion = KnowledgeIngestionService(
        repository,
        chunk_size=200,
        chunk_overlap=20,
    )
    service = EnterpriseFixtureService(
        root=root,
        data_dir=tmp_path / "runs",
        knowledge_repository=repository,
        ingestion=ingestion,
        ingestion_jobs=ingestion_jobs,  # type: ignore[arg-type]
    )
    return service, repository, ingestion


@pytest.mark.asyncio
async def test_fixture_preview_is_complete_and_reimport_is_idempotent(
    tmp_path: Path,
) -> None:
    service, repository, ingestion = _fixture_service(tmp_path)

    initial = await service.preview(tenant_id="local", project_id="default")
    imported = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    repeated_preview = await service.preview(tenant_id="local", project_id="default")
    repeated = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    documents_before_reset = await repository.list_documents(include_archived=True)
    unrelated = await ingestion.ingest(
        filename="unrelated.md",
        content=b"UNRELATED-ORBIT-501 remains after resetting the fixture.",
        media_type="text/markdown",
        user_id="alice",
    )
    reset = await service.reset(tenant_id="local", project_id="default")
    remaining = await repository.get_document(unrelated.document.document_id)

    assert len(initial.documents) == 23
    assert initial.counts == {"create": 22, "historical": 1}
    assert imported.status == "succeeded"
    assert isinstance(imported.created_at, datetime)
    assert len(imported.completed_document_ids) == 23
    assert len(repeated_preview.documents) == 23
    assert repeated_preview.counts == {"unchanged": 23}
    assert repeated.status == "succeeded"
    assert repeated.completed_document_ids == {}
    fixture_documents_before_reset = [
        document
        for document in documents_before_reset
        if document.source.fixture_id == EnterpriseFixtureService.FIXTURE_ID
    ]
    fixture_documents_after_reset = [
        document
        for document in await repository.list_documents(include_archived=True)
        if document.source.fixture_id == EnterpriseFixtureService.FIXTURE_ID
    ]
    assert (
        sum(document.status == DocumentStatus.ACTIVE for document in fixture_documents_before_reset)
        == 22
    )
    assert (
        sum(
            document.status == DocumentStatus.ARCHIVED
            for document in fixture_documents_before_reset
        )
        == 1
    )
    assert (
        sum(document.status == DocumentStatus.ACTIVE for document in fixture_documents_after_reset)
        == 0
    )
    assert (
        sum(
            document.status == DocumentStatus.ARCHIVED for document in fixture_documents_after_reset
        )
        == 23
    )
    yaml_document = next(
        document
        for document in fixture_documents_before_reset
        if document.source.source_id == "northstar:api:conversation-v1"
    )
    assert yaml_document.media_type == "application/yaml"
    assert reset.archived_document_ids
    assert remaining is not None and remaining.status == DocumentStatus.ACTIVE


@pytest.mark.asyncio
async def test_fixture_curated_graph_is_approved_evidence_backed_and_repeatable(
    tmp_path: Path,
) -> None:
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    ingestion = KnowledgeIngestionService(repository, chunk_size=200, chunk_overlap=20)
    candidates = JsonGraphCandidateRepository(tmp_path / "graph_candidates.json")
    semantic = _RecordingSemanticGraphIndex()
    service = EnterpriseFixtureService(
        root=_FIXTURE_ROOT,
        data_dir=tmp_path / "runs",
        knowledge_repository=repository,
        ingestion=ingestion,
        graph_candidate_repository=candidates,
        semantic_graph_index=semantic,
    )

    imported = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    repeated = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    entities = await candidates.list_entities(tenant_id="local", project_id="default")
    relations = await candidates.list_relations(tenant_id="local", project_id="default")
    active_chunk_ids = {
        chunk.chunk_id
        for document in await repository.list_documents(
            tenant_id="local",
            project_id="default",
        )
        for chunk in await repository.list_chunks(
            document.document_id,
            tenant_id="local",
            project_id="default",
        )
    }

    assert imported.status == repeated.status == "succeeded"
    assert imported.curated_graph_entities == repeated.curated_graph_entities == 16
    assert imported.curated_graph_relations == repeated.curated_graph_relations == 16
    assert len(entities) == 16
    assert len(relations) == 16
    assert all(item.status.value == "approved" for item in [*entities, *relations])
    assert all(set(item.source_chunk_ids) <= active_chunk_ids for item in [*entities, *relations])
    assert len(semantic.batches) == 2
    assert len(semantic.batches[-1].entities) == 16

    await service.reset(tenant_id="local", project_id="default")

    assert len(semantic.entity_statuses) == 16
    assert len(semantic.relation_statuses) == 16
    assert all(item.status.value == "archived" for item in semantic.entity_statuses)
    assert all(item.status.value == "archived" for item in semantic.relation_statuses)

    restored = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    restored_entities = await candidates.list_entities(
        tenant_id="local",
        project_id="default",
    )
    restored_relations = await candidates.list_relations(
        tenant_id="local",
        project_id="default",
    )
    assert restored.status == "succeeded"
    assert len(restored_entities) == len(restored_relations) == 16
    assert all(item.status.value == "approved" for item in restored_entities)
    assert all(item.status.value == "approved" for item in restored_relations)


@pytest.mark.asyncio
async def test_fixture_graph_revision_archives_the_previous_curated_release(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fixture"
    shutil.copytree(_FIXTURE_ROOT, root)
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    ingestion = KnowledgeIngestionService(repository, chunk_size=200, chunk_overlap=20)
    candidates = JsonGraphCandidateRepository(tmp_path / "graph_candidates.json")
    semantic = _RecordingSemanticGraphIndex()
    service = EnterpriseFixtureService(
        root=root,
        data_dir=tmp_path / "runs",
        knowledge_repository=repository,
        ingestion=ingestion,
        graph_candidate_repository=candidates,
        semantic_graph_index=semantic,
    )
    first = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    assert first.status == "succeeded"

    target_path = root / "architecture" / "system-overview.md"
    target_path.write_text(
        target_path.read_text(encoding="utf-8") + "\n\nGraph release v2 marker.\n",
        encoding="utf-8",
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["revision"] = "test-graph-v2"
    changed = next(
        entry
        for entry in manifest["documents"]
        if entry["source_id"] == "northstar:architecture:system-overview"
    )
    changed["source_revision"] = "2026.07"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    graph_seed_path = root / "graph_seed.json"
    graph_seed = json.loads(graph_seed_path.read_text(encoding="utf-8"))
    graph_seed["revision"] = "test-graph-v2"
    graph_seed_path.write_text(
        json.dumps(graph_seed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    second = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    entities = await candidates.list_entities(tenant_id="local", project_id="default")
    relations = await candidates.list_relations(tenant_id="local", project_id="default")

    assert second.status == "succeeded"
    assert sum(item.status.value == "approved" for item in entities) == 16
    assert sum(item.status.value == "archived" for item in entities) == 16
    assert sum(item.status.value == "approved" for item in relations) == 16
    assert sum(item.status.value == "archived" for item in relations) == 16
    assert len(semantic.entity_statuses) == 16
    assert len(semantic.relation_statuses) == 16


@pytest.mark.asyncio
async def test_fixture_revision_replacement_archives_only_the_previous_fixture_revision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fixture"
    shutil.copytree(_FIXTURE_ROOT, root)
    service, repository, _ = _fixture_service(tmp_path / "data", root=root)
    first = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    assert first.status == "succeeded"

    target_path = root / "architecture" / "system-overview.md"
    target_path.write_text(
        target_path.read_text(encoding="utf-8") + "\n\nRevision replacement marker.\n",
        encoding="utf-8",
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["revision"] = "test-v2"
    changed = next(
        entry
        for entry in manifest["documents"]
        if entry["source_id"] == "northstar:architecture:system-overview"
    )
    changed["source_revision"] = "2026.07"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    graph_seed_path = root / "graph_seed.json"
    graph_seed = json.loads(graph_seed_path.read_text(encoding="utf-8"))
    graph_seed["revision"] = "test-v2"
    graph_seed_path.write_text(
        json.dumps(graph_seed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    preview = await service.preview(tenant_id="local", project_id="default")
    second = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    documents = await repository.list_documents(include_archived=True)
    revisions = [
        document
        for document in documents
        if document.source.source_id == "northstar:architecture:system-overview"
    ]

    assert (
        next(
            item.action
            for item in preview.documents
            if item.source_id == "northstar:architecture:system-overview"
        )
        == "replace"
    )
    assert second.status == "succeeded"
    assert sum(document.status == DocumentStatus.ACTIVE for document in revisions) == 1
    assert sum(document.status == DocumentStatus.ARCHIVED for document in revisions) == 1
    assert (
        next(
            document.source.source_revision
            for document in revisions
            if document.status == DocumentStatus.ACTIVE
        )
        == "2026.07"
    )


class _TerminalFixtureJobs:
    def __init__(self) -> None:
        self._jobs: dict[UUID, IngestionJob] = {}
        self._order: list[UUID] = []

    async def submit(
        self,
        *,
        filename: str,
        content: bytes,
        media_type: str | None,
        tenant_id: str,
        project_id: str,
        user_id: str,
        source: KnowledgeSource,
    ) -> IngestionJobSubmission:
        job = IngestionJob(
            tenant_id=tenant_id,
            project_id=project_id,
            user_id=user_id,
            filename=filename,
            media_type=media_type,
            byte_size=len(content),
            content_hash=hashlib.sha256(content).hexdigest(),
            staging_key=f"fixture/{source.source_id}.upload",
            source=source,
        )
        self._jobs[job.job_id] = job
        self._order.append(job.job_id)
        return IngestionJobSubmission(job=job)

    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> IngestionJob | None:
        job = self._jobs.get(job_id)
        if job is None or (job.tenant_id, job.project_id) != (tenant_id, project_id):
            return None
        if self._order.index(job_id) == 0:
            return job.model_copy(
                update={
                    "status": IngestionJobStatus.SUCCEEDED,
                    "document_id": uuid4(),
                }
            )
        return job.model_copy(
            update={
                "status": IngestionJobStatus.FAILED,
                "error_code": "fixture_job_failed",
            }
        )


class _AllSuccessFixtureJobs(_TerminalFixtureJobs):
    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> IngestionJob | None:
        job = self._jobs.get(job_id)
        if job is None or (job.tenant_id, job.project_id) != (tenant_id, project_id):
            return None
        return job.model_copy(
            update={
                "status": IngestionJobStatus.SUCCEEDED,
                "document_id": uuid4(),
            }
        )


class _ResettableFixtureJobs(_TerminalFixtureJobs):
    async def get(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> IngestionJob | None:
        job = self._jobs.get(job_id)
        if job is None or (job.tenant_id, job.project_id) != (tenant_id, project_id):
            return None
        return job

    async def cancel(
        self,
        job_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> IngestionJob:
        job = await self.get(job_id, tenant_id=tenant_id, project_id=project_id)
        if job is None:
            raise KeyError("Ingestion job not found")
        cancelled = job.model_copy(
            update={
                "status": IngestionJobStatus.CANCELLED,
                "can_retry": True,
                "completed_at": utc_now(),
            }
        )
        self._jobs[job_id] = cancelled
        return cancelled


def _write_small_fixture(root: Path) -> None:
    root.mkdir(parents=True)
    documents = []
    for name in ("one", "two"):
        path = root / f"{name}.md"
        path.write_text(f"{name} fixture document", encoding="utf-8")
        documents.append(
            {
                "source_id": f"northstar:fixture:{name}",
                "path": path.name,
                "title": f"Fixture {name}",
                "source_revision": "1",
            }
        )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "name": "Small fixture",
                "revision": "1",
                "tenant_id": "local",
                "project_id": "default",
                "source_type": "enterprise_internal",
                "privacy": "private",
                "documents": documents,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_fixture_async_failure_keeps_all_terminal_job_diagnostics(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    _write_small_fixture(root)
    jobs = _TerminalFixtureJobs()
    service, _, _ = _fixture_service(tmp_path / "data", root=root, ingestion_jobs=jobs)

    started = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    terminal = await service.get_status(
        started.run_id,
        tenant_id="local",
        project_id="default",
    )

    assert started.status == "running"
    assert terminal is not None
    assert terminal.status == "failed"
    assert len(terminal.job_ids) == 2
    assert len(terminal.completed_document_ids) == 1
    assert len(terminal.errors) == 1
    assert set(terminal.job_statuses.values()) == {"succeeded", "failed"}


@pytest.mark.asyncio
async def test_fixture_async_finalization_fails_closed_when_snapshot_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fixture"
    _write_small_fixture(root)
    jobs = _AllSuccessFixtureJobs()
    service, _, _ = _fixture_service(tmp_path / "data", root=root, ingestion_jobs=jobs)
    started = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["revision"] = "2"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    completed = await service.get_status(
        started.run_id,
        tenant_id="local",
        project_id="default",
    )

    assert completed is not None and completed.status == "failed"
    assert completed.errors["__fixture_lifecycle__"] == "fixture_snapshot_changed"


@pytest.mark.asyncio
async def test_fixture_reset_cancels_pending_async_imports(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    _write_small_fixture(root)
    jobs = _ResettableFixtureJobs()
    service, _, _ = _fixture_service(tmp_path / "data", root=root, ingestion_jobs=jobs)
    started = await service.start(
        tenant_id="local",
        project_id="default",
        requested_by="owner",
    )

    await service.reset(tenant_id="local", project_id="default")
    completed = await service.get_status(
        started.run_id,
        tenant_id="local",
        project_id="default",
    )

    assert completed is not None and completed.status == "failed"
    assert completed.errors["__reset__"] == "fixture_reset"
    assert all(job.status == IngestionJobStatus.CANCELLED for job in jobs._jobs.values())


@pytest.mark.asyncio
async def test_bootstrap_injects_one_workspace_profile_into_run_and_overview(
    tmp_path: Path,
) -> None:
    components = build_components(
        Settings(app_env="test", data_dir=tmp_path, runtime_mode="offline")
    )
    try:
        trajectory = await components.run_service.prepare_run("profile check")
        overview = await components.workspace_service.overview(user_id="local-user")

        assert trajectory.context.workspace_mode == WorkspaceMode.TEAM
        assert trajectory.context.enabled_knowledge_layers == (
            KnowledgeLayer.TEAM_INTERNAL,
            KnowledgeLayer.PERSONAL,
        )
        assert trajectory.context.domain_pack == "software_engineering"
        assert overview["workspace_profile"] is not None
        assert overview["workspace_profile"]["workspace_mode"] == "team"
        assert overview["workspace_profile"]["default_domain_pack"] == "software_engineering"
    finally:
        await components.close()
