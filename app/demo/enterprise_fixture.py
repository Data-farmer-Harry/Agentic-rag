"""Validated, idempotent importer for the checked-in enterprise knowledge fixture."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import Field, field_validator, model_validator

from app.domain.contracts import (
    GraphCandidateRepository,
    KnowledgeRepository,
    SemanticGraphIndexPort,
)
from app.domain.enums import (
    DocumentStatus,
    GraphCandidateStatus,
    IngestionJobStatus,
    TrustLevel,
)
from app.domain.models import (
    GraphEntityCandidate,
    GraphExtractionBatch,
    GraphRelationCandidate,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSource,
    StrictModel,
    utc_now,
)
from app.knowledge.ingestion_jobs import IngestionJobService
from app.knowledge.knowledge_ingestion import KnowledgeIngestionService


class EnterpriseFixtureError(ValueError):
    pass


class EnterpriseManifestDocument(StrictModel):
    source_id: str = Field(
        min_length=1,
        max_length=1_000,
        pattern=r"^northstar:[a-z0-9][a-z0-9:_-]{1,999}$",
    )
    path: str = Field(min_length=1, max_length=1_000)
    title: str = Field(min_length=1, max_length=500)
    source_revision: str = Field(min_length=1, max_length=200)
    trust: TrustLevel = TrustLevel.USER_ASSERTED
    status: Literal["draft", "active", "superseded", "archived"] = "active"
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    last_reviewed_at: str | None = Field(default=None, max_length=32)
    effective_from: str | None = Field(default=None, max_length=32)
    effective_to: str | None = Field(default=None, max_length=32)
    supersedes: str | None = Field(default=None, max_length=1_000)
    superseded_by: str | None = Field(default=None, max_length=1_000)
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def relative_path_only(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value.startswith("~"):
            raise ValueError("Fixture document path must be a safe relative path")
        return path.as_posix()

    @model_validator(mode="after")
    def validate_temporal_range(self) -> EnterpriseManifestDocument:
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_from > self.effective_to
        ):
            raise ValueError("Fixture effective_from must not follow effective_to")
        return self


class EnterpriseManifest(StrictModel):
    name: str = Field(min_length=1, max_length=500)
    revision: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    source_type: Literal["enterprise_internal"]
    privacy: Literal["private"]
    documents: list[EnterpriseManifestDocument] = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_references(self) -> EnterpriseManifest:
        source_ids = {entry.source_id for entry in self.documents}
        if len(source_ids) != len(self.documents):
            raise ValueError("Fixture manifest source_id values must be unique")
        for entry in self.documents:
            for related in (entry.supersedes, entry.superseded_by):
                if related is not None and related not in source_ids:
                    raise ValueError(
                        f"Fixture relationship references unknown source_id: {related}"
                    )
            if entry.status == "superseded" and entry.superseded_by is None:
                raise ValueError("Superseded fixture documents require superseded_by")
        return self


class EnterpriseGraphSeedNode(StrictModel):
    node_id: str = Field(min_length=2, max_length=200, pattern=r"^[a-z0-9][a-z0-9:_-]+$")
    name: str = Field(min_length=2, max_length=300)
    entity_type: str = Field(pattern=r"^[A-Z][A-Za-z0-9_]{1,63}$")
    evidence_source_id: str = Field(pattern=r"^northstar:[a-z0-9][a-z0-9:_-]{1,999}$")
    aliases: list[str] = Field(default_factory=list, max_length=20)


class EnterpriseGraphSeedRelation(StrictModel):
    relation_id: str = Field(
        min_length=2,
        max_length=200,
        pattern=r"^[a-z0-9][a-z0-9:_-]+$",
    )
    source: str = Field(min_length=2, max_length=200)
    target: str = Field(min_length=2, max_length=200)
    relation_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    evidence_source_id: str = Field(pattern=r"^northstar:[a-z0-9][a-z0-9:_-]{1,999}$")


class EnterpriseGraphSeed(StrictModel):
    revision: str = Field(min_length=1, max_length=200)
    reviewed_by: str = Field(min_length=1, max_length=200)
    reviewed_at: datetime
    nodes: list[EnterpriseGraphSeedNode] = Field(min_length=1, max_length=200)
    relations: list[EnterpriseGraphSeedRelation] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_graph(self) -> EnterpriseGraphSeed:
        node_ids = {item.node_id for item in self.nodes}
        relation_ids = {item.relation_id for item in self.relations}
        if len(node_ids) != len(self.nodes):
            raise ValueError("Enterprise graph seed node IDs must be unique")
        if len(relation_ids) != len(self.relations):
            raise ValueError("Enterprise graph seed relation IDs must be unique")
        for relation in self.relations:
            if relation.source not in node_ids or relation.target not in node_ids:
                raise ValueError("Enterprise graph seed relation references an unknown node")
            if relation.source == relation.target:
                raise ValueError("Enterprise graph seed self-relations are not allowed")
        return self


@dataclass(frozen=True, slots=True)
class ValidatedEnterpriseFixtureDocument:
    entry: EnterpriseManifestDocument
    path: Path
    content: bytes
    content_hash: str


@dataclass(frozen=True, slots=True)
class ValidatedEnterpriseFixture:
    root: Path
    manifest: EnterpriseManifest
    documents: tuple[ValidatedEnterpriseFixtureDocument, ...]
    graph_seed: EnterpriseGraphSeed | None


class EnterpriseFixtureValidator:
    """Validates both manifest semantics and immutable fixture file bytes."""

    def __init__(self, root: Path, *, require_graph_seed: bool = False) -> None:
        self._root = root.resolve()
        self._require_graph_seed = require_graph_seed

    def load(self) -> ValidatedEnterpriseFixture:
        manifest_path = self._root / "manifest.json"
        try:
            manifest = EnterpriseManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise EnterpriseFixtureError("Enterprise fixture manifest is unavailable") from exc
        except (OSError, ValueError) as exc:
            raise EnterpriseFixtureError("Enterprise fixture manifest is invalid") from exc
        graph_seed_path = self._root / "graph_seed.json"
        graph_seed: EnterpriseGraphSeed | None = None
        try:
            graph_seed = EnterpriseGraphSeed.model_validate_json(
                graph_seed_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            if self._require_graph_seed:
                raise EnterpriseFixtureError("Enterprise graph seed is unavailable") from exc
        except (OSError, ValueError) as exc:
            raise EnterpriseFixtureError("Enterprise graph seed is invalid") from exc
        if graph_seed is not None and graph_seed.revision != manifest.revision:
            raise EnterpriseFixtureError("Enterprise graph seed revision does not match manifest")
        manifest_by_source = {entry.source_id: entry for entry in manifest.documents}
        graph_evidence_sources = (
            {item.evidence_source_id for item in graph_seed.nodes}
            | {item.evidence_source_id for item in graph_seed.relations}
            if graph_seed is not None
            else set()
        )
        for source_id in graph_evidence_sources:
            entry = manifest_by_source.get(source_id)
            if entry is None or entry.status != "active" or entry.trust != TrustLevel.VERIFIED:
                raise EnterpriseFixtureError(
                    "Enterprise graph seed evidence must reference active verified documents"
                )
        documents: list[ValidatedEnterpriseFixtureDocument] = []
        for entry in manifest.documents:
            path = (self._root / entry.path).resolve()
            if not path.is_relative_to(self._root) or not path.is_file():
                raise EnterpriseFixtureError(
                    f"Enterprise fixture file is unavailable: {entry.path}"
                )
            content = path.read_bytes()
            if not content:
                raise EnterpriseFixtureError(f"Enterprise fixture file is empty: {entry.path}")
            content_hash = hashlib.sha256(content).hexdigest()
            if entry.content_hash is not None and entry.content_hash != content_hash:
                raise EnterpriseFixtureError(
                    f"Enterprise fixture content hash does not match: {entry.path}"
                )
            documents.append(
                ValidatedEnterpriseFixtureDocument(
                    entry=entry,
                    path=path,
                    content=content,
                    content_hash=content_hash,
                )
            )
        return ValidatedEnterpriseFixture(
            root=self._root,
            manifest=manifest,
            documents=tuple(documents),
            graph_seed=graph_seed,
        )


class FixtureImportPlanItem(StrictModel):
    source_id: str
    filename: str
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_revision: str
    action: Literal["create", "refresh", "replace", "historical", "unchanged"]
    predecessor_document_ids: list[UUID] = Field(default_factory=list)


class EnterpriseFixturePreview(StrictModel):
    fixture_id: str
    manifest_revision: str
    tenant_id: str
    project_id: str
    documents: list[FixtureImportPlanItem]
    counts: dict[str, int]


class EnterpriseFixtureRun(StrictModel):
    run_id: UUID = Field(default_factory=uuid4)
    fixture_id: str
    manifest_revision: str
    tenant_id: str
    project_id: str
    requested_by: str
    dry_run: bool
    fixture_fingerprint: str = Field(default="0" * 64, pattern=r"^[a-f0-9]{64}$")
    lifecycle_generation: int = Field(default=0, ge=0)
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    plan: list[FixtureImportPlanItem] = Field(default_factory=list)
    job_ids: dict[str, UUID] = Field(default_factory=dict)
    job_statuses: dict[str, str] = Field(default_factory=dict)
    completed_document_ids: dict[str, UUID] = Field(default_factory=dict)
    archived_predecessor_ids: list[UUID] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    curated_graph_entities: int = Field(default=0, ge=0)
    curated_graph_relations: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class EnterpriseFixtureResetResult(StrictModel):
    fixture_id: str
    tenant_id: str
    project_id: str
    archived_document_ids: list[UUID] = Field(default_factory=list)


class _FixtureRunStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def save(self, run: EnterpriseFixtureRun) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = run.model_dump(mode="json", exclude_none=True)
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, run_id: UUID) -> EnterpriseFixtureRun | None:
        if not self._path.exists():
            return None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EnterpriseFixtureError("Enterprise fixture run state is invalid") from exc
        runs = payload if isinstance(payload, list) else [payload]
        for item in reversed(runs):
            run = EnterpriseFixtureRun.model_validate(item)
            if run.run_id == run_id:
                return run
        return None

    def list_scope(self, *, tenant_id: str, project_id: str) -> list[EnterpriseFixtureRun]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EnterpriseFixtureError("Enterprise fixture run state is invalid") from exc
        runs = payload if isinstance(payload, list) else [payload]
        return [
            run
            for item in runs
            if (run := EnterpriseFixtureRun.model_validate(item)).tenant_id == tenant_id
            and run.project_id == project_id
        ]

    def replace(self, run: EnterpriseFixtureRun) -> None:
        existing: list[dict[str, object]] = []
        if self._path.exists():
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
                existing = payload if isinstance(payload, list) else [payload]
            except (OSError, ValueError) as exc:
                raise EnterpriseFixtureError("Enterprise fixture run state is invalid") from exc
        encoded = run.model_dump(mode="json", exclude_none=True)
        replaced = False
        for index, item in enumerate(existing):
            if item.get("run_id") == str(run.run_id):
                existing[index] = encoded
                replaced = True
                break
        if not replaced:
            existing.append(encoded)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(existing, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)


class EnterpriseFixtureService:
    """Manifest-driven import service with revision-safe replacement semantics."""

    FIXTURE_ID = "enterprise_knowledge"
    IMPORTER_USER_ID = "fixture-importer"

    def __init__(
        self,
        *,
        root: Path,
        data_dir: Path,
        knowledge_repository: KnowledgeRepository,
        ingestion: KnowledgeIngestionService,
        ingestion_jobs: IngestionJobService | None = None,
        graph_candidate_repository: GraphCandidateRepository | None = None,
        semantic_graph_index: SemanticGraphIndexPort | None = None,
    ) -> None:
        if (graph_candidate_repository is None) != (semantic_graph_index is None):
            raise ValueError(
                "Curated fixture graph import requires both candidate and semantic indexes"
            )
        self._validator = EnterpriseFixtureValidator(
            root,
            require_graph_seed=semantic_graph_index is not None,
        )
        self._knowledge = knowledge_repository
        self._ingestion = ingestion
        self._ingestion_jobs = ingestion_jobs
        self._graph_candidates = graph_candidate_repository
        self._semantic_graph_index = semantic_graph_index
        self._runs = _FixtureRunStore(data_dir / "enterprise_fixture_runs.json")
        self._lifecycle_generation = 0
        self._lifecycle_lock = asyncio.Lock()

    async def preview(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> EnterpriseFixturePreview:
        fixture = self._validator.load()
        self._assert_scope(fixture, tenant_id=tenant_id, project_id=project_id)
        plan = await self._plan(fixture, tenant_id=tenant_id, project_id=project_id)
        counts: dict[str, int] = {}
        for item in plan:
            counts[item.action] = counts.get(item.action, 0) + 1
        return EnterpriseFixturePreview(
            fixture_id=self.FIXTURE_ID,
            manifest_revision=fixture.manifest.revision,
            tenant_id=tenant_id,
            project_id=project_id,
            documents=plan,
            counts=counts,
        )

    async def start(
        self,
        *,
        tenant_id: str,
        project_id: str,
        requested_by: str,
        dry_run: bool = False,
    ) -> EnterpriseFixtureRun:
        # Reset and all import finalization share one lifecycle boundary.  A
        # reset therefore cannot miss a just-submitted job or be followed by a
        # stale graph seed.
        async with self._lifecycle_lock:
            return await self._start_locked(
                tenant_id=tenant_id,
                project_id=project_id,
                requested_by=requested_by,
                dry_run=dry_run,
            )

    async def _start_locked(
        self,
        *,
        tenant_id: str,
        project_id: str,
        requested_by: str,
        dry_run: bool,
    ) -> EnterpriseFixtureRun:
        generation = self._lifecycle_generation
        fixture = self._validator.load()
        self._assert_scope(fixture, tenant_id=tenant_id, project_id=project_id)
        plan = await self._plan(fixture, tenant_id=tenant_id, project_id=project_id)
        if generation != self._lifecycle_generation:
            raise EnterpriseFixtureError("Fixture reset interrupted import planning")
        executable = [item for item in plan if item.action != "unchanged"]
        run = EnterpriseFixtureRun(
            fixture_id=self.FIXTURE_ID,
            manifest_revision=fixture.manifest.revision,
            tenant_id=tenant_id,
            project_id=project_id,
            requested_by=requested_by,
            dry_run=dry_run,
            fixture_fingerprint=_fixture_fingerprint(fixture),
            lifecycle_generation=generation,
            status="succeeded" if dry_run else "running",
            plan=plan,
            completed_at=utc_now() if dry_run else None,
        )
        self._runs.replace(run)
        if dry_run:
            return run
        if not executable:
            finalized = await self._finalize_successful_run(run)
            self._runs.replace(finalized)
            return finalized
        by_source_id = {item.entry.source_id: item for item in fixture.documents}
        if self._ingestion_jobs is None:
            return await self._run_inline(run, by_source_id)
        job_ids: dict[str, UUID] = {}
        job_statuses: dict[str, str] = {}
        errors: dict[str, str] = {}
        for item in executable:
            if generation != self._lifecycle_generation:
                return await self._fail_reset_run(run)
            source_document = by_source_id[item.source_id]
            try:
                submission = await self._ingestion_jobs.submit(
                    filename=source_document.path.name,
                    content=source_document.content,
                    media_type=_media_type(source_document.path),
                    tenant_id=tenant_id,
                    project_id=project_id,
                    user_id=self.IMPORTER_USER_ID,
                    source=self._source_for(fixture, source_document),
                )
                job_ids[item.source_id] = submission.job.job_id
                job_statuses[item.source_id] = submission.job.status.value
            except Exception as exc:
                # A malformed file or staging outage must not discard already
                # queued fixture work. Persist a source-level diagnostic instead.
                errors[item.source_id] = f"submission_failed:{type(exc).__name__}"
                job_statuses[item.source_id] = "submission_failed"
            run = run.model_copy(
                update={
                    "job_ids": job_ids,
                    "job_statuses": job_statuses,
                    "errors": errors,
                    "updated_at": utc_now(),
                }
            )
            self._runs.replace(run)
            # Keep the submitted job durable before the conditional lifecycle
            # check so a future caller can always cancel or compensate it.
            if generation != self._lifecycle_generation:
                return await self._fail_reset_run(run)

        if not job_ids:
            failed = run.model_copy(
                update={
                    "status": "failed",
                    "completed_at": utc_now(),
                    "updated_at": utc_now(),
                }
            )
            self._runs.replace(failed)
            return failed
        return run

    async def get_status(
        self,
        run_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> EnterpriseFixtureRun | None:
        # Finalization can archive predecessors and publish curated graph
        # evidence, so it must serialize with reset as well.
        async with self._lifecycle_lock:
            return await self._get_status_locked(
                run_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )

    async def _get_status_locked(
        self,
        run_id: UUID,
        *,
        tenant_id: str,
        project_id: str,
    ) -> EnterpriseFixtureRun | None:
        run = self._runs.get(run_id)
        if (
            run is None
            or run.tenant_id != tenant_id
            or run.project_id != project_id
            or run.status != "running"
            or self._ingestion_jobs is None
        ):
            return run
        if run.lifecycle_generation != self._lifecycle_generation:
            return await self._fail_reset_run(run)
        completed = dict(run.completed_document_ids)
        errors = dict(run.errors)
        job_statuses = dict(run.job_statuses)
        for source_id, job_id in run.job_ids.items():
            job = await self._ingestion_jobs.get(
                job_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if job is None:
                errors[source_id] = "job_not_found"
                job_statuses[source_id] = "missing"
            elif job.status == IngestionJobStatus.SUCCEEDED and job.document_id is not None:
                completed[source_id] = job.document_id
                job_statuses[source_id] = job.status.value
            elif job.status in {IngestionJobStatus.FAILED, IngestionJobStatus.CANCELLED}:
                errors[source_id] = job.error_code or job.status.value
                job_statuses[source_id] = job.status.value
            else:
                job_statuses[source_id] = job.status.value
        refreshed = run.model_copy(
            update={
                "completed_document_ids": completed,
                "errors": errors,
                "job_statuses": job_statuses,
                "updated_at": utc_now(),
            }
        )
        expected_source_ids = {item.source_id for item in run.plan if item.action != "unchanged"}
        terminal_source_ids = set(completed) | set(errors)
        if not expected_source_ids.issubset(terminal_source_ids):
            self._runs.replace(refreshed)
            return refreshed
        if run.lifecycle_generation != self._lifecycle_generation:
            return await self._fail_reset_run(refreshed)
        if errors:
            refreshed = refreshed.model_copy(update={"status": "failed", "completed_at": utc_now()})
        elif len(completed) == len(run.job_ids):
            refreshed = await self._finalize_successful_run(refreshed)
        self._runs.replace(refreshed)
        return refreshed

    async def reset(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> EnterpriseFixtureResetResult:
        async with self._lifecycle_lock:
            self._lifecycle_generation += 1
            for run in self._runs.list_scope(
                tenant_id=tenant_id,
                project_id=project_id,
            ):
                if run.status != "running":
                    continue
                cancelled = run.model_copy(
                    update={
                        "status": "failed",
                        "errors": {**run.errors, "__reset__": "fixture_reset"},
                        "updated_at": utc_now(),
                        "completed_at": utc_now(),
                    }
                )
                self._runs.replace(cancelled)
                await self._cancel_run_jobs(cancelled)
            documents = await self._knowledge.list_documents(
                tenant_id=tenant_id,
                project_id=project_id,
                include_archived=True,
            )
            archived: list[UUID] = []
            for document in documents:
                if (
                    document.source.fixture_id != self.FIXTURE_ID
                    or document.status == DocumentStatus.ARCHIVED
                ):
                    continue
                if await self._ingestion.archive(
                    document.document_id,
                    tenant_id=tenant_id,
                    project_id=project_id,
                ):
                    archived.append(document.document_id)
            await self._deactivate_curated_graph(
                tenant_id=tenant_id,
                project_id=project_id,
            )
            return EnterpriseFixtureResetResult(
                fixture_id=self.FIXTURE_ID,
                tenant_id=tenant_id,
                project_id=project_id,
                archived_document_ids=archived,
            )

    async def _plan(
        self,
        fixture: ValidatedEnterpriseFixture,
        *,
        tenant_id: str,
        project_id: str,
    ) -> list[FixtureImportPlanItem]:
        existing = await self._knowledge.list_documents(
            tenant_id=tenant_id,
            project_id=project_id,
            include_archived=True,
        )
        by_source: dict[str, list[KnowledgeDocument]] = {}
        for document in existing:
            if document.source.fixture_id == self.FIXTURE_ID:
                by_source.setdefault(document.source.source_id, []).append(document)
        plan: list[FixtureImportPlanItem] = []
        for source_document in fixture.documents:
            entry = source_document.entry
            current = [
                document
                for document in by_source.get(entry.source_id, [])
                if document.status == DocumentStatus.ACTIVE
            ]
            action: Literal["create", "refresh", "replace", "historical", "unchanged"]
            predecessors: list[UUID] = []
            matching = by_source.get(entry.source_id, [])
            same_revision = any(
                document.content_hash == source_document.content_hash
                and document.source.source_revision == entry.source_revision
                and document.source.source_status == entry.status
                for document in matching
            )
            if entry.status != "active":
                action = "unchanged" if same_revision else "historical"
                predecessors = [document.document_id for document in current]
            elif not current:
                action = "create"
            elif any(document.content_hash == source_document.content_hash for document in current):
                action = (
                    "unchanged"
                    if any(
                        document.source.source_revision == entry.source_revision
                        for document in current
                        if document.content_hash == source_document.content_hash
                    )
                    else "refresh"
                )
            else:
                action = "replace"
                predecessors = [document.document_id for document in current]
            plan.append(
                FixtureImportPlanItem(
                    source_id=entry.source_id,
                    filename=source_document.path.name,
                    content_hash=source_document.content_hash,
                    source_revision=entry.source_revision,
                    action=action,
                    predecessor_document_ids=predecessors,
                )
            )
        return plan

    async def _run_inline(
        self,
        run: EnterpriseFixtureRun,
        documents: Mapping[str, ValidatedEnterpriseFixtureDocument],
    ) -> EnterpriseFixtureRun:
        completed: dict[str, UUID] = {}
        errors: dict[str, str] = {}
        for item in run.plan:
            if item.action == "unchanged":
                continue
            source_document = documents[item.source_id]
            try:
                result = await self._ingestion.ingest(
                    filename=source_document.path.name,
                    content=source_document.content,
                    media_type=_media_type(source_document.path),
                    tenant_id=run.tenant_id,
                    project_id=run.project_id,
                    user_id=self.IMPORTER_USER_ID,
                    source=self._source_for_document(run, source_document),
                )
                completed[item.source_id] = result.document.document_id
            except Exception as exc:
                errors[item.source_id] = type(exc).__name__
        updated = run.model_copy(
            update={
                "completed_document_ids": completed,
                "errors": errors,
                "job_statuses": {
                    item.source_id: ("succeeded" if item.source_id in completed else "failed")
                    for item in run.plan
                    if item.action != "unchanged"
                },
                "updated_at": utc_now(),
            }
        )
        if errors:
            updated = updated.model_copy(update={"status": "failed", "completed_at": utc_now()})
        else:
            updated = await self._finalize_successful_run(updated)
        self._runs.replace(updated)
        return updated

    async def _finalize_successful_run(
        self,
        run: EnterpriseFixtureRun,
    ) -> EnterpriseFixtureRun:
        # Callers hold _lifecycle_lock through predecessor archival and graph
        # publication so reset cannot leave a stale curated release active.
        archived = set(run.archived_predecessor_ids)
        plan_by_source = {item.source_id: item for item in run.plan}
        fixture = self._validator.load()
        if (
            run.lifecycle_generation != self._lifecycle_generation
            or run.fixture_fingerprint != _fixture_fingerprint(fixture)
        ):
            return await self._fail_stale_run(run, reason="fixture_snapshot_changed")
        source_by_id = {item.entry.source_id: item for item in fixture.documents}
        for source_id, document_id in run.completed_document_ids.items():
            plan = plan_by_source[source_id]
            source_document = source_by_id[source_id]
            if source_document.entry.status != "active":
                await self._ingestion.archive(
                    document_id,
                    tenant_id=run.tenant_id,
                    project_id=run.project_id,
                )
            for predecessor in plan.predecessor_document_ids:
                if predecessor == document_id or predecessor in archived:
                    continue
                if await self._ingestion.archive(
                    predecessor,
                    tenant_id=run.tenant_id,
                    project_id=run.project_id,
                ):
                    archived.add(predecessor)
        try:
            graph_entities, graph_relations = await self._seed_curated_graph(
                fixture,
                tenant_id=run.tenant_id,
                project_id=run.project_id,
            )
        except Exception as exc:
            return run.model_copy(
                update={
                    "status": "failed",
                    "errors": {
                        **run.errors,
                        "__curated_graph__": f"graph_seed_failed:{type(exc).__name__}",
                    },
                    "archived_predecessor_ids": sorted(archived, key=str),
                    "updated_at": utc_now(),
                    "completed_at": utc_now(),
                }
            )
        return run.model_copy(
            update={
                "status": "succeeded",
                "archived_predecessor_ids": sorted(archived, key=str),
                "curated_graph_entities": graph_entities,
                "curated_graph_relations": graph_relations,
                "updated_at": utc_now(),
                "completed_at": utc_now(),
            }
        )

    async def _cancel_run_jobs(self, run: EnterpriseFixtureRun) -> None:
        if self._ingestion_jobs is None:
            return
        for job_id in run.job_ids.values():
            try:
                await self._ingestion_jobs.cancel(
                    job_id,
                    tenant_id=run.tenant_id,
                    project_id=run.project_id,
                )
                continue
            except (KeyError, ValueError):
                pass
            job = await self._ingestion_jobs.get(
                job_id,
                tenant_id=run.tenant_id,
                project_id=run.project_id,
            )
            if job is not None and job.document_id is not None:
                await self._ingestion.archive(
                    job.document_id,
                    tenant_id=run.tenant_id,
                    project_id=run.project_id,
                )

    async def _fail_reset_run(self, run: EnterpriseFixtureRun) -> EnterpriseFixtureRun:
        await self._cancel_run_jobs(run)
        return await self._fail_stale_run(run, reason="fixture_reset")

    async def _fail_stale_run(
        self,
        run: EnterpriseFixtureRun,
        *,
        reason: str,
    ) -> EnterpriseFixtureRun:
        for document_id in set(run.completed_document_ids.values()):
            await self._ingestion.archive(
                document_id,
                tenant_id=run.tenant_id,
                project_id=run.project_id,
            )
        failed = run.model_copy(
            update={
                "status": "failed",
                "errors": {**run.errors, "__fixture_lifecycle__": reason},
                "updated_at": utc_now(),
                "completed_at": utc_now(),
            }
        )
        self._runs.replace(failed)
        return failed

    async def _seed_curated_graph(
        self,
        fixture: ValidatedEnterpriseFixture,
        *,
        tenant_id: str,
        project_id: str,
    ) -> tuple[int, int]:
        if self._graph_candidates is None or self._semantic_graph_index is None:
            return 0, 0
        if fixture.graph_seed is None:
            raise EnterpriseFixtureError("Enterprise graph seed is unavailable")
        documents = await self._knowledge.list_documents(
            tenant_id=tenant_id,
            project_id=project_id,
            include_archived=True,
        )
        active_by_source = {
            document.source.source_id: document
            for document in documents
            if document.status == DocumentStatus.ACTIVE
            and document.source.fixture_id == self.FIXTURE_ID
            and document.source.source_status == "active"
            and document.source.trust == TrustLevel.VERIFIED
        }
        evidence_source_ids = {item.evidence_source_id for item in fixture.graph_seed.nodes} | {
            item.evidence_source_id for item in fixture.graph_seed.relations
        }
        evidence_chunks: dict[str, Sequence[KnowledgeChunk]] = {}
        for source_id in sorted(evidence_source_ids):
            document = active_by_source.get(source_id)
            if document is None:
                raise EnterpriseFixtureError(f"Curated graph evidence is not active: {source_id}")
            chunks = await self._knowledge.list_chunks(
                document.document_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if not chunks:
                raise EnterpriseFixtureError(f"Curated graph evidence has no chunks: {source_id}")
            evidence_chunks[source_id] = chunks

        seed = fixture.graph_seed
        seed_document_id = self._graph_seed_document_id(
            revision=seed.revision,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        await self._deactivate_superseded_graph_releases(
            active_document_id=seed_document_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        extractor_revision = f"curated-enterprise-fixture:{seed.revision}"
        entity_ids = {
            item.node_id: uuid5(
                NAMESPACE_URL,
                f"{seed_document_id}:entity:{item.node_id}",
            )
            for item in seed.nodes
        }
        entities = [
            GraphEntityCandidate(
                candidate_id=entity_ids[item.node_id],
                document_id=seed_document_id,
                tenant_id=tenant_id,
                project_id=project_id,
                canonical_name=item.name,
                entity_type=item.entity_type,
                aliases=item.aliases,
                source_chunk_ids=[
                    _select_evidence_chunks(
                        evidence_chunks[item.evidence_source_id],
                        ((item.name, item.aliases),),
                    )[0].chunk_id
                ],
                confidence=1.0,
                extractor_revision=extractor_revision,
                domain_pack="software_engineering",
                status=GraphCandidateStatus.APPROVED,
                rationale="Versioned enterprise fixture fact reviewed before import.",
                reviewed_by=seed.reviewed_by,
                reviewed_at=seed.reviewed_at,
                created_at=seed.reviewed_at,
                updated_at=seed.reviewed_at,
            )
            for item in seed.nodes
        ]
        nodes_by_id = {item.node_id: item for item in seed.nodes}
        names = {item.node_id: item.name for item in seed.nodes}
        relations = [
            GraphRelationCandidate(
                candidate_id=uuid5(
                    NAMESPACE_URL,
                    f"{seed_document_id}:relation:{item.relation_id}",
                ),
                document_id=seed_document_id,
                tenant_id=tenant_id,
                project_id=project_id,
                source_candidate_id=entity_ids[item.source],
                target_candidate_id=entity_ids[item.target],
                source_name=names[item.source],
                target_name=names[item.target],
                relation_type=item.relation_type,
                source_chunk_ids=[
                    chunk.chunk_id
                    for chunk in _select_evidence_chunks(
                        evidence_chunks[item.evidence_source_id],
                        (
                            (
                                nodes_by_id[item.source].name,
                                nodes_by_id[item.source].aliases,
                            ),
                            (
                                nodes_by_id[item.target].name,
                                nodes_by_id[item.target].aliases,
                            ),
                        ),
                    )
                ],
                confidence=1.0,
                extractor_revision=extractor_revision,
                domain_pack="software_engineering",
                status=GraphCandidateStatus.APPROVED,
                rationale="Versioned enterprise fixture relation reviewed before import.",
                reviewed_by=seed.reviewed_by,
                reviewed_at=seed.reviewed_at,
                created_at=seed.reviewed_at,
                updated_at=seed.reviewed_at,
            )
            for item in seed.relations
        ]
        batch = GraphExtractionBatch(
            batch_id=uuid5(NAMESPACE_URL, f"{seed_document_id}:batch"),
            document_id=seed_document_id,
            tenant_id=tenant_id,
            project_id=project_id,
            domain_pack="software_engineering",
            extractor_revision=extractor_revision,
            entities=entities,
            relations=relations,
            created_at=seed.reviewed_at,
        )
        stored = await self._graph_candidates.save_batch(batch)
        await self._semantic_graph_index.index_extraction(stored)
        return len(stored.entities), len(stored.relations)

    async def _deactivate_superseded_graph_releases(
        self,
        *,
        active_document_id: UUID,
        tenant_id: str,
        project_id: str,
    ) -> None:
        if self._graph_candidates is None or self._semantic_graph_index is None:
            return
        entities = await self._graph_candidates.list_entities(
            tenant_id=tenant_id,
            project_id=project_id,
        )
        old_document_ids = {
            item.document_id
            for item in entities
            if item.document_id != active_document_id
            and item.extractor_revision.startswith("curated-enterprise-fixture:")
            and item.status != GraphCandidateStatus.ARCHIVED
        }
        for document_id in sorted(old_document_ids, key=str):
            old_entities = await self._graph_candidates.list_entities(
                tenant_id=tenant_id,
                project_id=project_id,
                document_id=document_id,
            )
            old_relations = await self._graph_candidates.list_relations(
                tenant_id=tenant_id,
                project_id=project_id,
                document_id=document_id,
            )
            await self._archive_semantic_candidates(old_entities, old_relations)
            await self._graph_candidates.archive_document(
                document_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )

    async def _deactivate_curated_graph(
        self,
        *,
        tenant_id: str,
        project_id: str,
    ) -> None:
        if self._graph_candidates is None or self._semantic_graph_index is None:
            return
        curated_entities = await self._graph_candidates.list_entities(
            tenant_id=tenant_id,
            project_id=project_id,
        )
        document_ids = {
            item.document_id
            for item in curated_entities
            if item.extractor_revision.startswith("curated-enterprise-fixture:")
            and item.status != GraphCandidateStatus.ARCHIVED
        }
        for document_id in sorted(document_ids, key=str):
            entities = await self._graph_candidates.list_entities(
                tenant_id=tenant_id,
                project_id=project_id,
                document_id=document_id,
            )
            relations = await self._graph_candidates.list_relations(
                tenant_id=tenant_id,
                project_id=project_id,
                document_id=document_id,
            )
            await self._archive_semantic_candidates(entities, relations)
            await self._graph_candidates.archive_document(
                document_id,
                tenant_id=tenant_id,
                project_id=project_id,
            )

    async def _archive_semantic_candidates(
        self,
        entities: Sequence[GraphEntityCandidate],
        relations: Sequence[GraphRelationCandidate],
    ) -> None:
        if self._semantic_graph_index is None:
            return
        now = utc_now()
        for relation in relations:
            await self._semantic_graph_index.set_relation_status(
                relation.model_copy(
                    update={"status": GraphCandidateStatus.ARCHIVED, "updated_at": now}
                )
            )
        for entity in entities:
            await self._semantic_graph_index.set_entity_status(
                entity.model_copy(
                    update={"status": GraphCandidateStatus.ARCHIVED, "updated_at": now}
                )
            )

    @staticmethod
    def _graph_seed_document_id(
        *,
        revision: str,
        tenant_id: str,
        project_id: str,
    ) -> UUID:
        return uuid5(
            NAMESPACE_URL,
            f"fixture://enterprise_knowledge/graph-seed/{revision}/{tenant_id}/{project_id}",
        )

    def _source_for(
        self,
        fixture: ValidatedEnterpriseFixture,
        source_document: ValidatedEnterpriseFixtureDocument,
    ) -> KnowledgeSource:
        return _knowledge_source(
            source_document,
            fixture.manifest.source_type,
            fixture.manifest.privacy,
        )

    def _source_for_document(
        self,
        run: EnterpriseFixtureRun,
        source_document: ValidatedEnterpriseFixtureDocument,
    ) -> KnowledgeSource:
        return _knowledge_source(source_document, "enterprise_internal", "private")

    @staticmethod
    def _assert_scope(
        fixture: ValidatedEnterpriseFixture,
        *,
        tenant_id: str,
        project_id: str,
    ) -> None:
        if fixture.manifest.tenant_id != tenant_id or fixture.manifest.project_id != project_id:
            raise EnterpriseFixtureError("Fixture manifest is not available in this workspace")


def _knowledge_source(
    source_document: ValidatedEnterpriseFixtureDocument,
    source_type: str,
    privacy: str,
) -> KnowledgeSource:
    entry = source_document.entry
    return KnowledgeSource(
        source_type=source_type,
        source_id=entry.source_id,
        title=entry.title,
        source_revision=entry.source_revision,
        canonical_uri=f"fixture://enterprise_knowledge/{entry.path}",
        privacy=privacy,
        trust=entry.trust,
        source_status=entry.status,
        owner=entry.owner,
        last_reviewed_at=entry.last_reviewed_at,
        effective_from=entry.effective_from,
        effective_to=entry.effective_to,
        supersedes_source_id=entry.supersedes,
        superseded_by_source_id=entry.superseded_by,
        fixture_id=EnterpriseFixtureService.FIXTURE_ID,
    )


def _media_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".yaml", ".yml"}:
        return "application/yaml"
    if suffix == ".json":
        return "application/json"
    return "text/markdown" if suffix in {".md", ".markdown"} else "text/plain"


def _fixture_fingerprint(fixture: ValidatedEnterpriseFixture) -> str:
    payload = {
        "manifest": fixture.manifest.model_dump(mode="json"),
        "graph_seed": (
            fixture.graph_seed.model_dump(mode="json") if fixture.graph_seed is not None else None
        ),
        "documents": [
            {
                "source_id": item.entry.source_id,
                "content_hash": item.content_hash,
            }
            for item in fixture.documents
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _select_evidence_chunks(
    chunks: Sequence[KnowledgeChunk],
    entities: Sequence[tuple[str, Sequence[str]]],
) -> tuple[KnowledgeChunk, ...]:
    for chunk in chunks:
        normalized = chunk.text.casefold()
        if all(
            any(marker in normalized for marker in _entity_markers(name, aliases))
            for name, aliases in entities
        ):
            return (chunk,)
    selected: list[KnowledgeChunk] = []
    for name, aliases in entities:
        matching = next(
            (
                chunk
                for chunk in chunks
                if any(marker in chunk.text.casefold() for marker in _entity_markers(name, aliases))
            ),
            None,
        )
        if matching is None:
            raise EnterpriseFixtureError(f"Curated graph evidence does not support: {name}")
        if matching not in selected:
            selected.append(matching)
    if selected:
        return tuple(selected)
    names = ", ".join(name for name, _ in entities)
    raise EnterpriseFixtureError(f"Curated graph evidence does not support: {names}")


def _entity_markers(name: str, aliases: Sequence[str]) -> tuple[str, ...]:
    phrases = [value.casefold().strip() for value in (name, *aliases) if value.strip()]
    tokens = [
        token
        for phrase in phrases
        for token in re.findall(r"[a-z0-9-]+", phrase)
        if len(token) >= 4 and token not in {"service", "runbook", "database", "team"}
    ]
    return tuple(dict.fromkeys([*phrases, *tokens]))
