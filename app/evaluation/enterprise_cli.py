from __future__ import annotations

import argparse
import asyncio
import math
import os
import re
import tempfile
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from qdrant_client import AsyncQdrantClient

from app.agent.model_provider import build_embedding_client, build_model_client
from app.config import Settings, get_settings
from app.domain.enums import TrustLevel
from app.domain.models import EvidenceRef, Provenance
from app.evaluation.enterprise import (
    EnterpriseAnswerArtifactSet,
    EnterpriseAnswerEvaluator,
    EnterpriseArtifactProvenance,
    EnterpriseEvaluationProvenance,
    EnterpriseGraphArtifactSet,
    EnterpriseGraphAssertionEvaluator,
    combine_enterprise_evaluation,
    compile_enterprise_retrieval_fixture,
    gate_enterprise_retrieval,
    load_enterprise_evaluation_dataset,
)
from app.evaluation.retrieval import (
    AgenticRetrievalEvaluator,
    RetrievalEvalReport,
    RetrievalGoldenSet,
)
from app.retrieval.agentic_retrieval import (
    AgenticRetrievalController,
    DeterministicQueryPlanner,
    OpenAIStructuredQueryPlanner,
)
from app.retrieval.embedding_providers import (
    DeterministicDenseEmbedder,
    OpenAIDenseEmbedder,
    build_sparse_embedder,
)
from app.retrieval.hybrid_retrieval_pipeline import RetrievalPipeline
from app.retrieval.qdrant_hybrid_retriever import QdrantHybridStore

_ASCII_TERM_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]*", re.IGNORECASE)
_CJK_SEQUENCE_PATTERN = re.compile(r"[\u4e00-\u9fff]+")
_CURRENT_VERSION_MARKERS = ("当前", "现行", "现在", "current", "latest")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate enterprise RAG contracts without mislabeling fixture results as production."
        ),
        epilog=(
            "Execution tiers:\n"
            "  fixture (default): fully offline lexical contract regression. Its report is never "
            "production evidence.\n"
            "  qdrant: reads an existing configured Qdrant collection only. It never creates a "
            "collection, payload index, or document. The collection must already contain the "
            "fixture sources with matching source_id, tenant_id, project_id, and active status.\n"
            "\n"
            "A Qdrant system gate requires --answers and --graphs declared as live_run with "
            "their run IDs. Missing or offline/unverified artifacts fail the combined gate; use "
            "--report-only only when collecting a diagnostic report."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("examples/enterprise_knowledge/evaluation/golden_questions.json"),
        help="Enterprise golden question set linked to its manifest",
    )
    parser.add_argument(
        "--retrieval-fixture",
        type=Path,
        default=Path("examples/evaluation/enterprise_retrieval_golden.json"),
        help="Atomic destination for the compiled AgenticRetrievalEvaluator fixture",
    )
    parser.add_argument(
        "--answers",
        type=Path,
        help="JSON answer artifact set; missing required answers fail closed",
    )
    parser.add_argument(
        "--graphs",
        type=Path,
        help="JSON graph artifact set; missing required graph paths fail closed",
    )
    parser.add_argument(
        "--answer-artifact-provenance",
        choices=("offline_fixture", "external_unverified", "live_run"),
        default="external_unverified",
        help="Declared source of --answers. live_run additionally requires --answer-run-id.",
    )
    parser.add_argument(
        "--answer-run-id",
        help="Stable runtime ID for a --answers artifact declared as live_run.",
    )
    parser.add_argument(
        "--graph-artifact-provenance",
        choices=("offline_fixture", "external_unverified", "live_run"),
        default="external_unverified",
        help="Declared source of --graphs. live_run additionally requires --graph-run-id.",
    )
    parser.add_argument(
        "--graph-run-id",
        help="Stable runtime ID for a --graphs artifact declared as live_run.",
    )
    parser.add_argument(
        "--retrieval-backend",
        "--backend",
        dest="retrieval_backend",
        choices=("fixture", "qdrant"),
        default="fixture",
        help="fixture is an offline lexical contract; qdrant is an existing read-only live index.",
    )
    parser.add_argument(
        "--planner-mode",
        choices=("deterministic", "openai"),
        default="deterministic",
        help=(
            "Planner for the Qdrant gate. Fixture mode is intentionally deterministic and zero API."
        ),
    )
    parser.add_argument(
        "--qdrant-collection",
        help="Override the configured Qdrant collection for --retrieval-backend qdrant.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Atomic destination for the combined report, including backend and artifact provenance"
        ),
    )
    parser.add_argument(
        "--compile-only",
        action="store_true",
        help="Validate source contracts and write only the offline retrieval fixture",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Exit zero after writing a failed diagnostic report; never changes report.passed",
    )
    return parser


async def _run(args: argparse.Namespace) -> int:
    retrieval_backend = str(getattr(args, "retrieval_backend", "fixture"))
    planner_mode = str(getattr(args, "planner_mode", "deterministic"))
    dataset = load_enterprise_evaluation_dataset(args.dataset)
    retrieval_fixture = compile_enterprise_retrieval_fixture(dataset)
    _write_atomic(
        args.retrieval_fixture,
        retrieval_fixture.model_dump_json(indent=2) + "\n",
    )
    if args.compile_only:
        print(
            {
                "dataset_name": dataset.golden.name,
                "dataset_revision": dataset.golden.revision,
                "retrieval_fixture": str(args.retrieval_fixture.resolve()),
                "document_count": len(retrieval_fixture.documents),
                "case_count": len(retrieval_fixture.cases),
                "required_case_count": len(retrieval_fixture.required_case_ids),
            }
        )
        return 0

    if retrieval_backend == "fixture" and planner_mode != "deterministic":
        raise ValueError(
            "--planner-mode openai requires --retrieval-backend qdrant; "
            "fixture mode is deliberately zero API"
        )

    retrieval_report = None
    retrieval_error = None
    qdrant_collection: str | None = None
    retrieval_evidence = "offline_fixture"
    try:
        if retrieval_backend == "fixture":
            retrieval_report = await _evaluate_fixture_retrieval(retrieval_fixture)
        elif retrieval_backend == "qdrant":
            settings = get_settings()
            qdrant_collection = _qdrant_collection_name(
                settings,
                getattr(args, "qdrant_collection", None),
            )
            retrieval_report = await _evaluate_qdrant_retrieval(
                retrieval_fixture,
                settings=settings,
                collection_name=qdrant_collection,
                planner_mode=planner_mode,
            )
            retrieval_evidence = "live_qdrant_read_only"
        else:
            raise ValueError(f"unsupported retrieval backend: {retrieval_backend}")
    except Exception as exc:
        retrieval_error = f"{type(exc).__name__}: {exc}"
        if retrieval_backend == "qdrant":
            retrieval_evidence = "unavailable"
    retrieval = gate_enterprise_retrieval(retrieval_report, error=retrieval_error)

    answer_provenance = _artifact_provenance(
        path=args.answers,
        kind=str(getattr(args, "answer_artifact_provenance", "external_unverified")),
        run_id=getattr(args, "answer_run_id", None),
        label="answer",
    )
    graph_provenance = _artifact_provenance(
        path=args.graphs,
        kind=str(getattr(args, "graph_artifact_provenance", "external_unverified")),
        run_id=getattr(args, "graph_run_id", None),
        label="graph",
    )
    answer_artifacts = (
        EnterpriseAnswerArtifactSet.load(args.answers)
        if args.answers is not None
        else EnterpriseAnswerArtifactSet()
    )
    graph_artifacts = (
        EnterpriseGraphArtifactSet.load(args.graphs)
        if args.graphs is not None
        else EnterpriseGraphArtifactSet()
    )
    answers = EnterpriseAnswerEvaluator(dataset).evaluate(answer_artifacts.answers)
    graph = EnterpriseGraphAssertionEvaluator(dataset).evaluate(graph_artifacts.graphs)
    report = combine_enterprise_evaluation(
        dataset,
        retrieval,
        answers,
        graph,
        provenance=EnterpriseEvaluationProvenance(
            retrieval_backend=retrieval_backend,
            retrieval_evidence=retrieval_evidence,
            planner_mode=planner_mode,
            qdrant_collection=qdrant_collection,
            answer_artifact=answer_provenance,
            graph_artifact=graph_provenance,
        ),
    )
    output = args.output or _default_output_path()
    payload = report.model_dump_json(indent=2)
    _write_atomic(output, payload + "\n")
    print(payload)
    print(f"report_path={output.resolve()}")
    return 0 if args.report_only or report.passed else 1


async def _evaluate_fixture_retrieval(
    retrieval_fixture: RetrievalGoldenSet,
) -> RetrievalEvalReport:
    retriever = _fixture_retriever(retrieval_fixture)
    controller = AgenticRetrievalController(
        RetrievalPipeline({"enterprise_fixture_lexical": retriever}),
        planner=DeterministicQueryPlanner(max_subqueries=4),
        max_rounds=2,
        max_subqueries=4,
    )
    try:
        return await AgenticRetrievalEvaluator(controller).run(retrieval_fixture)
    finally:
        await controller.close()


async def _evaluate_qdrant_retrieval(
    retrieval_fixture: RetrievalGoldenSet,
    *,
    settings: Settings,
    collection_name: str,
    planner_mode: str,
) -> RetrievalEvalReport:
    """Run the existing agentic controller against a pre-existing, non-mutating Qdrant store."""

    qdrant_store: QdrantHybridStore | None = None
    qdrant_client: AsyncQdrantClient | None = None
    embedding_client: AsyncOpenAI | None = None
    controller: AgenticRetrievalController | None = None
    planner: Any | None = None
    try:
        qdrant_client = _qdrant_client(settings)
        if not await qdrant_client.collection_exists(collection_name):
            raise RuntimeError(
                f"Qdrant collection {collection_name!r} does not exist; "
                "enterprise evaluation will not create or index it"
            )
        planner = _build_qdrant_planner(settings, planner_mode)
        dense, embedding_client = _build_qdrant_dense_embedder(settings)
        qdrant_store = ReadOnlyEnterpriseQdrantStore(
            qdrant_client,
            dense,
            build_sparse_embedder(
                settings.qdrant_sparse_encoder,
                bm25_k1=settings.qdrant_bm25_k1,
                bm25_b=settings.qdrant_bm25_b,
                bm25_average_document_tokens=(
                    settings.qdrant_bm25_average_document_tokens
                ),
            ),
            collection_name=collection_name,
            prefetch_limit=settings.qdrant_prefetch_limit,
            rrf_k=settings.qdrant_rrf_k,
            create_payload_indexes=False,
            use_sparse_idf=settings.qdrant_sparse_idf,
        )
        controller = AgenticRetrievalController(
            RetrievalPipeline({"qdrant_hybrid": qdrant_store}),
            planner=planner,
            max_rounds=settings.retrieval_max_rounds,
            max_subqueries=settings.retrieval_max_subqueries,
        )
        return await AgenticRetrievalEvaluator(controller).run(retrieval_fixture)
    finally:
        if controller is not None:
            await controller.close()
        elif planner is not None:
            close = getattr(planner, "close", None)
            if close is not None:
                await close()
        if qdrant_store is not None:
            await qdrant_store.close()
        elif qdrant_client is not None:
            await qdrant_client.close()
        if embedding_client is not None:
            await embedding_client.close()


class ReadOnlyEnterpriseQdrantStore(QdrantHybridStore):
    """Evaluation adapter that retains retrieval behavior but refuses all provisioning."""

    async def ensure_collection(self) -> None:
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            if not await self._client.collection_exists(self.collection_name):
                raise RuntimeError(
                    f"Qdrant collection {self.collection_name!r} disappeared during evaluation; "
                    "the read-only gate will not recreate it"
                )
            await self._validate_collection()
            self._ready = True


def _qdrant_client(settings: Settings) -> AsyncQdrantClient:
    if not settings.qdrant_url:
        raise ValueError("QDRANT_URL is required for --retrieval-backend qdrant")
    if settings.qdrant_url == ":memory:":
        return AsyncQdrantClient(location=":memory:")
    return AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=(
            settings.qdrant_api_key.get_secret_value()
            if settings.qdrant_api_key is not None
            else None
        ),
        timeout=min(settings.agent_timeout_seconds, 60),
    )


def _qdrant_collection_name(settings: Settings, override: str | None) -> str:
    collection_name = (override or settings.qdrant_collection).strip()
    if not collection_name:
        raise ValueError("a non-empty Qdrant collection name is required")
    return collection_name


def _build_qdrant_planner(settings: Settings, planner_mode: str) -> Any:
    if planner_mode == "deterministic":
        return DeterministicQueryPlanner(max_subqueries=settings.retrieval_max_subqueries)
    if planner_mode == "openai":
        client = build_model_client(settings, timeout=120, max_retries=1)
        return OpenAIStructuredQueryPlanner(
            client,
            model=settings.retrieval_planner_model or settings.openai_model,
            max_output_tokens=settings.retrieval_planner_max_output_tokens,
        )
    raise ValueError(f"unsupported planner mode: {planner_mode}")


def _build_qdrant_dense_embedder(
    settings: Settings,
) -> tuple[Any, AsyncOpenAI | None]:
    if settings.embedding_provider == "deterministic":
        return DeterministicDenseEmbedder(settings.embedding_dimensions), None
    embedding_client = build_embedding_client(
        settings,
        timeout=min(settings.agent_timeout_seconds, 60),
    )
    return (
        OpenAIDenseEmbedder(
            embedding_client,
            model=settings.embedding_model,
            dimension=settings.embedding_dimensions,
        ),
        embedding_client,
    )


def _artifact_provenance(
    *,
    path: Path | None,
    kind: str,
    run_id: str | None,
    label: str,
) -> EnterpriseArtifactProvenance:
    if path is None:
        if kind != "external_unverified" or run_id is not None:
            raise ValueError(
                f"--{label}-artifact-provenance and --{label}-run-id require --{label}s"
            )
        return EnterpriseArtifactProvenance(kind="not_provided")
    return EnterpriseArtifactProvenance.model_validate({"kind": kind, "run_id": run_id})


def _fixture_retriever(dataset: RetrievalGoldenSet) -> EnterpriseFixtureLexicalRetriever:
    return EnterpriseFixtureLexicalRetriever(dataset)


class EnterpriseFixtureLexicalRetriever:
    """CJK-aware deterministic baseline used only by the enterprise evaluation CLI."""

    def __init__(self, dataset: RetrievalGoldenSet) -> None:
        evidence: list[EvidenceRef] = []
        terms_by_evidence: dict[str, set[str]] = {}
        ascii_terms_by_evidence: dict[str, set[str]] = {}
        title_terms_by_evidence: dict[str, set[str]] = {}
        title_ascii_terms_by_evidence: dict[str, set[str]] = {}
        normalized_text_by_evidence: dict[str, str] = {}
        document_frequency: Counter[str] = Counter()
        for item in dataset.documents:
            metadata = {
                **item.metadata,
                "source_id": item.source_id,
                "title": item.title,
            }
            trust = TrustLevel(str(metadata.get("trust", TrustLevel.UNTRUSTED.value)))
            evidence_ref = EvidenceRef(
                text=item.text,
                title=item.title,
                provenance=Provenance(
                    source_type="enterprise_evaluation_fixture",
                    source_id=item.source_id,
                    trust=trust,
                ),
                metadata=metadata,
            )
            identity = str(evidence_ref.evidence_id)
            terms = _lexical_terms(f"{item.title or ''}\n{item.text}")
            title_terms = _lexical_terms(item.title or "")
            evidence.append(evidence_ref)
            terms_by_evidence[identity] = terms
            ascii_terms_by_evidence[identity] = _ascii_terms(f"{item.title or ''}\n{item.text}")
            title_terms_by_evidence[identity] = title_terms
            title_ascii_terms_by_evidence[identity] = _ascii_terms(item.title or "")
            normalized_text_by_evidence[identity] = _normalize_for_phrase(item.text)
            document_frequency.update(terms)
        document_count = max(len(evidence), 1)
        self._evidence = tuple(evidence)
        self._terms_by_evidence = terms_by_evidence
        self._ascii_terms_by_evidence = ascii_terms_by_evidence
        self._title_terms_by_evidence = title_terms_by_evidence
        self._title_ascii_terms_by_evidence = title_ascii_terms_by_evidence
        self._normalized_text_by_evidence = normalized_text_by_evidence
        self._inverse_document_frequency = {
            term: math.log((document_count + 1) / (count + 1)) + 1.0
            for term, count in document_frequency.items()
        }

    async def retrieve(
        self,
        query: str,
        context: object | None = None,
        *,
        filters: dict[str, Any] | None = None,
        top_k: int = 10,
    ) -> tuple[EvidenceRef, ...]:
        del context
        if top_k < 1:
            raise ValueError("top_k must be positive")
        query_terms = _lexical_terms(query)
        if not query_terms:
            return ()
        query_ascii_terms = _ascii_terms(query)
        known_ascii_terms = set().union(*self._ascii_terms_by_evidence.values())
        if any(
            _looks_like_explicit_identifier(term) and term not in known_ascii_terms
            for term in query_ascii_terms
        ):
            return ()
        denominator = sum(self._inverse_document_frequency.get(term, 1.0) for term in query_terms)
        normalized_query = _normalize_for_phrase(query)
        current_version_query = _contains_current_version_marker(query)
        scored: list[tuple[float, int, EvidenceRef]] = []
        for index, item in enumerate(self._evidence):
            if filters and any(item.metadata.get(key) != value for key, value in filters.items()):
                continue
            if current_version_query and item.metadata.get("status") == "superseded":
                continue
            identity = str(item.evidence_id)
            overlap = query_terms.intersection(self._terms_by_evidence[identity])
            if not overlap:
                continue
            # Coverage across the body dominates.  Title matches are useful tie-breakers,
            # but cannot outrank a document that substantively answers a multi-fact query.
            score = 12.0 * sum(
                self._inverse_document_frequency.get(term, 1.0) for term in overlap
            )
            score /= max(denominator, 1.0)
            ascii_overlap = query_ascii_terms.intersection(self._ascii_terms_by_evidence[identity])
            score += 1.5 * sum(
                self._inverse_document_frequency.get(term, 1.0) for term in ascii_overlap
            )
            title_overlap = query_terms.intersection(self._title_terms_by_evidence[identity])
            score += 0.5 * sum(
                self._inverse_document_frequency.get(term, 1.0) for term in title_overlap
            )
            title_ascii_overlap = query_ascii_terms.intersection(
                self._title_ascii_terms_by_evidence[identity]
            )
            score += 1.5 * sum(
                self._inverse_document_frequency.get(term, 1.0) for term in title_ascii_overlap
            )
            if normalized_query and normalized_query in self._normalized_text_by_evidence[identity]:
                score += 1.0
            scored.append((score, index, item.model_copy(update={"score": score})))
        scored.sort(key=lambda value: (-value[0], value[1]))
        return tuple(value[2] for value in scored[:top_k])


def _lexical_terms(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    terms = set(_ASCII_TERM_PATTERN.findall(normalized))
    for sequence in _CJK_SEQUENCE_PATTERN.findall(normalized):
        if len(sequence) == 1:
            terms.add(sequence)
            continue
        for width in (2, 3):
            terms.update(
                sequence[index : index + width]
                for index in range(max(len(sequence) - width + 1, 0))
            )
    return terms


def _ascii_terms(value: str) -> set[str]:
    return set(_ASCII_TERM_PATTERN.findall(unicodedata.normalize("NFKC", value).casefold()))


def _looks_like_explicit_identifier(value: str) -> bool:
    return "-" in value or any(character.isdigit() for character in value)


def _contains_current_version_marker(value: str) -> bool:
    normalized = _normalize_for_phrase(value)
    return any(_normalize_for_phrase(marker) in normalized for marker in _CURRENT_VERSION_MARKERS)


def _normalize_for_phrase(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def _default_output_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(".data/evals") / f"enterprise_rag_{timestamp}.json"


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
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
            temporary_path = Path(handle.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parser().parse_args())))


if __name__ == "__main__":
    main()
