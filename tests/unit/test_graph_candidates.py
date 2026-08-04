import asyncio
import hashlib
import json
import multiprocessing
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

from app.domain.enums import GraphCandidateStatus
from app.domain.models import (
    EntityResolutionCandidate,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeSource,
)
from app.graph.candidate_service import (
    GraphCandidateReviewError,
    GraphCandidateService,
    KnowledgeGraphIngestionCoordinator,
)
from app.graph.candidate_store import JsonGraphCandidateRepository
from app.graph.extraction import RuleBasedEntityRelationExtractor
from app.graph.resolution import DeterministicEntityResolver


def _write_candidate_batches(
    path: str,
    worker_number: int,
    start_gate: Any,
    batch_count: int,
) -> None:
    async def write() -> None:
        repository = JsonGraphCandidateRepository(Path(path))
        extractor = RuleBasedEntityRelationExtractor()
        for index in range(batch_count):
            text = (
                f"# Worker {worker_number} Topic {index}\n"
                f"Agent{worker_number}Item{index} uses Tool{worker_number}Item{index}."
            )
            document = KnowledgeDocument(
                filename=f"worker-{worker_number}-{index}.md",
                title=f"Worker {worker_number} Topic {index}",
                media_type="text/markdown",
                byte_size=len(text.encode("utf-8")),
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                storage_key=f"uploads/worker-{worker_number}-{index}.md",
                chunk_count=1,
            )
            chunk = KnowledgeChunk(
                document_id=document.document_id,
                chunk_index=0,
                text=text,
                content_hash=hashlib.sha256(f"chunk:{text}".encode()).hexdigest(),
                char_end=len(text),
            )
            batch = await extractor.extract(document, [chunk])
            await repository.save_batch(batch)

    start_gate.wait(timeout=10)
    asyncio.run(write())


def _document_and_chunk() -> tuple[KnowledgeDocument, KnowledgeChunk]:
    document = KnowledgeDocument(
        filename="architecture.md",
        title="Architecture",
        media_type="text/markdown",
        byte_size=128,
        content_hash="a" * 64,
        storage_key="uploads/architecture.md",
        chunk_count=1,
    )
    chunk = KnowledgeChunk(
        document_id=document.document_id,
        chunk_index=0,
        text=(
            "# Retrieval Architecture\n"
            "HermesGraph uses Qdrant.\n"
            "Qdrant depends on Neo4j.\n"
            "The runtime exposes `search_knowledge`."
        ),
        content_hash="b" * 64,
        char_end=112,
    )
    return document, chunk


class _RecordingSemanticIndex:
    def __init__(self) -> None:
        self.batches = []
        self.entities = []
        self.relations = []
        self.resolutions = []

    async def index_extraction(self, batch):  # type: ignore[no-untyped-def]
        self.batches.append(batch)

    async def set_entity_status(self, candidate):  # type: ignore[no-untyped-def]
        self.entities.append(candidate)

    async def set_relation_status(self, candidate):  # type: ignore[no-untyped-def]
        self.relations.append(candidate)

    async def index_resolutions(self, candidates):  # type: ignore[no-untyped-def]
        self.resolutions.extend(candidates)

    async def set_resolution_status(self, candidate):  # type: ignore[no-untyped-def]
        self.resolutions.append(candidate)


class _RecordingStructuralIndex:
    def __init__(self) -> None:
        self.indexed = []
        self.archived = []

    async def index_document(self, document, chunks):  # type: ignore[no-untyped-def]
        self.indexed.append((document, chunks))

    async def archive_document(  # type: ignore[no-untyped-def]
        self, document_id, *, tenant_id, project_id
    ):
        self.archived.append((document_id, tenant_id, project_id))


class _CapturingExtractor:
    def __init__(self) -> None:
        self.chunks: list[KnowledgeChunk] = []
        self._delegate = RuleBasedEntityRelationExtractor()

    async def extract(self, document, chunks, *, domain_pack="general"):  # type: ignore[no-untyped-def]
        self.chunks = list(chunks)
        return await self._delegate.extract(document, chunks, domain_pack=domain_pack)


@pytest.mark.asyncio
async def test_rule_extractor_is_stable_and_evidence_backed() -> None:
    document, chunk = _document_and_chunk()
    extractor = RuleBasedEntityRelationExtractor()

    first = await extractor.extract(document, [chunk])
    second = await extractor.extract(document, [chunk])

    names = {item.canonical_name for item in first.entities}
    assert {"Retrieval Architecture", "HermesGraph", "Qdrant", "Neo4j"} <= names
    assert {item.relation_type for item in first.relations} == {"uses", "depends_on"}
    assert [item.candidate_id for item in first.entities] == [
        item.candidate_id for item in second.entities
    ]
    assert [item.candidate_id for item in first.relations] == [
        item.candidate_id for item in second.relations
    ]
    assert all(item.source_chunk_ids == [chunk.chunk_id] for item in first.relations)
    assert all(item.status == GraphCandidateStatus.PENDING for item in first.relations)


@pytest.mark.asyncio
async def test_reextraction_archives_only_stale_pending_candidates(
    tmp_path: Path,
) -> None:
    document, first_chunk = _document_and_chunk()
    extractor = RuleBasedEntityRelationExtractor()
    repository = JsonGraphCandidateRepository(tmp_path / "graph_candidates.json")
    first_batch = await extractor.extract(document, [first_chunk])
    await repository.save_batch(first_batch)

    reviewed = next(
        item for item in first_batch.entities if item.canonical_name == "Neo4j"
    ).model_copy(update={"status": GraphCandidateStatus.APPROVED})
    await repository.save_entity(reviewed)

    replacement_text = "# Retrieval Architecture\nHermesGraph uses PostgreSQL."
    replacement_chunk = KnowledgeChunk(
        document_id=document.document_id,
        chunk_index=0,
        text=replacement_text,
        content_hash=hashlib.sha256(replacement_text.encode()).hexdigest(),
        char_end=len(replacement_text),
    )
    replacement = await extractor.extract(document, [replacement_chunk])
    await repository.save_batch(replacement)

    entities = await repository.list_entities(document_id=document.document_id)
    relations = await repository.list_relations(document_id=document.document_id)
    by_name = {item.canonical_name: item for item in entities}
    assert by_name["Neo4j"].status == GraphCandidateStatus.APPROVED
    assert by_name["Qdrant"].status == GraphCandidateStatus.ARCHIVED
    assert by_name["PostgreSQL"].status == GraphCandidateStatus.PENDING
    assert any(item.status == GraphCandidateStatus.ARCHIVED for item in relations)
    assert any(item.status == GraphCandidateStatus.PENDING for item in relations)


@pytest.mark.asyncio
async def test_candidate_evidence_reconciliation_preserves_reviewed_history(
    tmp_path: Path,
) -> None:
    document, chunk = _document_and_chunk()
    repository = JsonGraphCandidateRepository(tmp_path / "graph_candidates.json")
    batch = await RuleBasedEntityRelationExtractor().extract(document, [chunk])
    await repository.save_batch(batch)
    reviewed = next(
        item for item in batch.entities if item.canonical_name == "Neo4j"
    ).model_copy(update={"status": GraphCandidateStatus.APPROVED})
    await repository.save_entity(reviewed)

    preview = await repository.reconcile_pending_evidence(set(), dry_run=True)
    assert preview.entities_archived == len(batch.entities) - 1
    assert preview.relations_archived == len(batch.relations)
    assert all(
        item.status != GraphCandidateStatus.ARCHIVED
        for item in await repository.list_entities()
    )

    applied = await repository.reconcile_pending_evidence(set())
    assert applied == preview
    entities = await repository.list_entities()
    assert next(item for item in entities if item.canonical_name == "Neo4j").status == (
        GraphCandidateStatus.APPROVED
    )
    assert all(
        item.status == GraphCandidateStatus.ARCHIVED
        for item in entities
        if item.canonical_name != "Neo4j"
    )
    assert all(
        item.status == GraphCandidateStatus.ARCHIVED
        for item in await repository.list_relations()
    )


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="Cross-process graph store locking requires a Unix process model",
)
def test_candidate_store_serializes_cross_process_writers(tmp_path: Path) -> None:
    context = multiprocessing.get_context("fork")
    start_gate = context.Event()
    store_path = tmp_path / "graph_candidates.json"
    batch_count = 8
    processes = [
        context.Process(
            target=_write_candidate_batches,
            args=(str(store_path), worker_number, start_gate, batch_count),
        )
        for worker_number in range(2)
    ]
    for process in processes:
        process.start()
    start_gate.set()
    for process in processes:
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
            pytest.fail("Cross-process graph candidate writer did not finish")
        assert process.exitcode == 0

    repository = JsonGraphCandidateRepository(store_path)
    relations = asyncio.run(repository.list_relations())
    assert len(relations) == batch_count * len(processes)


@pytest.mark.asyncio
async def test_pending_resolution_candidates_are_compacted_to_a_forest(
    tmp_path: Path,
) -> None:
    repository = JsonGraphCandidateRepository(tmp_path / "graph_candidates.json")
    extractor = RuleBasedEntityRelationExtractor()
    for index in range(4):
        text = f"# Runtime {index}\nHermesGraph uses Tool{index}."
        document = KnowledgeDocument(
            filename=f"runtime-{index}.md",
            title=f"Runtime {index}",
            media_type="text/markdown",
            byte_size=len(text),
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
            storage_key=f"uploads/runtime-{index}.md",
            chunk_count=1,
        )
        chunk = KnowledgeChunk(
            document_id=document.document_id,
            chunk_index=0,
            text=text,
            content_hash=hashlib.sha256(f"chunk:{text}".encode()).hexdigest(),
            char_end=len(text),
        )
        await repository.save_batch(await extractor.extract(document, [chunk]))

    entities = [
        item
        for item in await repository.list_entities()
        if item.canonical_name == "HermesGraph"
    ]
    proposals = [
        EntityResolutionCandidate(
            left_entity_id=left.candidate_id,
            right_entity_id=right.candidate_id,
            left_document_id=left.document_id,
            right_document_id=right.document_id,
            left_name=left.canonical_name,
            right_name=right.canonical_name,
            canonical_name="HermesGraph",
            entity_type=left.entity_type,
            match_strategy="exact_name",
            source_chunk_ids=sorted(
                {*left.source_chunk_ids, *right.source_chunk_ids}, key=str
            ),
            confidence=0.98,
            resolver_revision="historical-pairwise-v1",
        )
        for left, right in combinations(entities, 2)
    ]
    assert len(proposals) == 6

    stored = await repository.save_resolutions(proposals)
    assert len(stored) == 3
    assert len(await repository.list_resolutions()) == 3
    assert await repository.compact_pending_resolutions() == (3, 3)


@pytest.mark.asyncio
async def test_candidate_review_approves_relation_entities_and_records_audit(
    tmp_path: Path,
) -> None:
    document, chunk = _document_and_chunk()
    extractor = RuleBasedEntityRelationExtractor()
    repository = JsonGraphCandidateRepository(tmp_path / "graph_candidates.json")
    semantic = _RecordingSemanticIndex()
    structural = _RecordingStructuralIndex()
    coordinator = KnowledgeGraphIngestionCoordinator(
        extractor,
        repository,
        structural_index=structural,
        semantic_index=semantic,
    )
    service = GraphCandidateService(repository, semantic_index=semantic)

    await coordinator.index_document(document, [chunk])
    pending = await service.list_candidates()
    relation = next(item for item in pending.relations if item.relation_type == "uses")
    approved = await service.review_relation(
        relation.candidate_id,
        GraphCandidateStatus.APPROVED,
        reviewer_id="human-reviewer",
        reason="Explicit statement verified against the source chunk.",
    )

    assert approved.status == GraphCandidateStatus.APPROVED
    scoped = await service.list_candidates()
    linked_ids = {relation.source_candidate_id, relation.target_candidate_id}
    assert all(
        item.status == GraphCandidateStatus.APPROVED
        for item in scoped.entities
        if item.candidate_id in linked_ids
    )
    assert semantic.batches
    assert semantic.relations[-1].status == GraphCandidateStatus.APPROVED
    reviews = await repository.list_reviews()
    assert len(reviews) == 3
    assert {item.candidate_type for item in reviews} == {"entity", "relation"}
    assert await service.list_candidates(project_id="other") == type(scoped)()

    rejected_entity = await service.review_entity(
        relation.source_candidate_id,
        GraphCandidateStatus.REJECTED,
        reviewer_id="human-reviewer",
        reason="Canonical entity was rejected during resolver review.",
    )
    after_entity_rejection = await service.list_candidates()
    rejected_relation = next(
        item
        for item in after_entity_rejection.relations
        if item.candidate_id == relation.candidate_id
    )
    assert rejected_entity.status == GraphCandidateStatus.REJECTED
    assert rejected_relation.status == GraphCandidateStatus.REJECTED
    assert semantic.relations[-1].status == GraphCandidateStatus.REJECTED
    assert len(await repository.list_reviews()) == 5

    await coordinator.archive_document(
        document.document_id,
        tenant_id="local",
        project_id="default",
    )
    archived = await service.list_candidates(status=GraphCandidateStatus.ARCHIVED)
    assert len(archived.entities) == len(scoped.entities)
    assert len(archived.relations) == len(scoped.relations)
    assert structural.archived


@pytest.mark.asyncio
async def test_graph_extraction_samples_bounded_evidence_but_indexes_full_document(
    tmp_path: Path,
) -> None:
    document, _ = _document_and_chunk()
    document = document.model_copy(
        update={
            "source": KnowledgeSource(
                source_type="arxiv",
                source_id="arxiv:2607.12764",
                privacy="public_reference",
            )
        }
    )
    chunks = [
        KnowledgeChunk(
            document_id=document.document_id,
            chunk_index=index,
            text=f"chunk-{index}-" + "x" * 990,
            content_hash=hashlib.sha256(str(index).encode()).hexdigest(),
            char_end=1_000,
        )
        for index in range(10)
    ]
    extractor = _CapturingExtractor()
    structural = _RecordingStructuralIndex()
    coordinator = KnowledgeGraphIngestionCoordinator(
        extractor,
        JsonGraphCandidateRepository(tmp_path / "graph_candidates.json"),
        structural_index=structural,
        max_extraction_chars=8_000,
        public_reference_max_extraction_chars=3_000,
    )

    await coordinator.index_document(document, chunks)

    assert 1 < len(extractor.chunks) < len(chunks)
    assert sum(len(item.text) for item in extractor.chunks) <= 3_000
    assert extractor.chunks[0].chunk_index == 0
    assert extractor.chunks[-1].chunk_index == 9
    assert structural.indexed[0][1] == chunks


@pytest.mark.asyncio
async def test_rejected_entity_blocks_relation_approval(tmp_path: Path) -> None:
    document, chunk = _document_and_chunk()
    repository = JsonGraphCandidateRepository(tmp_path / "graph_candidates.json")
    batch = await RuleBasedEntityRelationExtractor().extract(document, [chunk])
    await repository.save_batch(batch)
    service = GraphCandidateService(repository)
    relation = batch.relations[0]

    await service.review_entity(
        relation.source_candidate_id,
        GraphCandidateStatus.REJECTED,
        reviewer_id="reviewer",
    )
    assert (
        await repository.get_relation(relation.candidate_id)
    ).status == GraphCandidateStatus.REJECTED
    with pytest.raises(GraphCandidateReviewError, match="not allowed"):
        await service.review_relation(
            relation.candidate_id,
            GraphCandidateStatus.APPROVED,
            reviewer_id="reviewer",
        )


@pytest.mark.asyncio
async def test_cross_document_resolution_requires_review_and_cascades(
    tmp_path: Path,
) -> None:
    first_document, first_chunk = _document_and_chunk()
    second_document = KnowledgeDocument(
        filename="operations.md",
        title="Operations",
        media_type="text/markdown",
        byte_size=96,
        content_hash="c" * 64,
        storage_key="uploads/operations.md",
        chunk_count=1,
    )
    second_chunk = KnowledgeChunk(
        document_id=second_document.document_id,
        chunk_index=0,
        text="HermesGraph supports Neo4j for durable graph traversal.",
        content_hash="d" * 64,
        char_end=54,
    )
    repository = JsonGraphCandidateRepository(tmp_path / "graph_candidates.json")
    semantic = _RecordingSemanticIndex()
    coordinator = KnowledgeGraphIngestionCoordinator(
        RuleBasedEntityRelationExtractor(),
        repository,
        entity_resolver=DeterministicEntityResolver(),
        semantic_index=semantic,
    )
    service = GraphCandidateService(repository, semantic_index=semantic)

    await coordinator.index_document(first_document, [first_chunk])
    await coordinator.index_document(second_document, [second_chunk])
    collection = await service.list_candidates()

    resolution = next(
        item
        for item in collection.resolutions
        if {item.left_name, item.right_name} == {"HermesGraph"}
    )
    assert resolution.status == GraphCandidateStatus.PENDING
    assert resolution.match_strategy == "exact_name"
    assert len(resolution.source_chunk_ids) == 2
    repeated = next(
        item
        for item in (await service.list_candidates()).resolutions
        if {item.left_name, item.right_name} == {"HermesGraph"}
    )
    assert resolution.candidate_id == repeated.candidate_id

    approved = await service.review_resolution(
        resolution.candidate_id,
        GraphCandidateStatus.APPROVED,
        reviewer_id="resolver-reviewer",
        reason="Both source passages refer to the same system.",
    )
    assert approved.status == GraphCandidateStatus.APPROVED
    endpoint_ids = {approved.left_entity_id, approved.right_entity_id}
    after_approval = await service.list_candidates()
    assert all(
        item.status == GraphCandidateStatus.APPROVED
        for item in after_approval.entities
        if item.candidate_id in endpoint_ids
    )
    assert semantic.resolutions[-1].status == GraphCandidateStatus.APPROVED
    assert {
        item.candidate_type for item in await repository.list_reviews()
    } == {"entity", "resolution"}

    await service.review_entity(
        approved.left_entity_id,
        GraphCandidateStatus.REJECTED,
        reviewer_id="resolver-reviewer",
        reason="The extracted endpoint is not a valid entity.",
    )
    rejected = await repository.get_resolution(approved.candidate_id)
    assert rejected is not None
    assert rejected.status == GraphCandidateStatus.REJECTED
    assert semantic.resolutions[-1].status == GraphCandidateStatus.REJECTED


@pytest.mark.asyncio
async def test_candidate_store_reads_v1_and_migrates_on_write(tmp_path: Path) -> None:
    document, chunk = _document_and_chunk()
    batch = await RuleBasedEntityRelationExtractor().extract(document, [chunk])
    path = tmp_path / "graph_candidates.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "entities": [item.model_dump(mode="json") for item in batch.entities],
                "relations": [item.model_dump(mode="json") for item in batch.relations],
                "reviews": [],
            }
        ),
        encoding="utf-8",
    )
    repository = JsonGraphCandidateRepository(path)

    assert len(await repository.list_entities()) == len(batch.entities)
    await repository.save_batch(batch)

    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["version"] == 2
    assert migrated["resolutions"] == []
