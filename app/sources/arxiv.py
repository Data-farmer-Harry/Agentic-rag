from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import TrustLevel
from app.domain.models import KnowledgeSource

ARXIV_API_URL = "https://export.arxiv.org/api/query"
DEFAULT_CATEGORIES = (
    "cs.AI",
    "cs.CL",
    "cs.IR",
    "cs.LG",
    "cs.CV",
    "cs.SE",
    "cs.HC",
)
DEFAULT_TOPICS = (
    "agentic RAG",
    "knowledge graph",
    "long-term memory",
    "tool use",
    "multimodal agent",
    "self-improving agent",
    "self-evolving agent",
    "software agent",
)

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_OPEN_SEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
_VERSION_PATTERN = re.compile(r"^(?P<id>.+?)v(?P<version>[1-9][0-9]*)$")
_SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_ALLOWED_ARXIV_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}


def utc_now() -> datetime:
    return datetime.now(UTC)


class ArxivSourceError(RuntimeError):
    pass


class ArxivResponseTooLarge(ArxivSourceError):
    pass


class ArxivPaper(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arxiv_id: str = Field(min_length=3, max_length=100)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=1_000)
    summary: str = Field(default="", max_length=50_000)
    authors: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    primary_category: str | None = Field(default=None, max_length=100)
    published: datetime
    updated: datetime
    abstract_url: str = Field(max_length=2_000)
    pdf_url: str = Field(max_length=2_000)
    license_uri: str | None = Field(default=None, max_length=2_000)
    doi: str | None = Field(default=None, max_length=500)
    journal_ref: str | None = Field(default=None, max_length=2_000)
    comment: str | None = Field(default=None, max_length=5_000)

    @property
    def versioned_id(self) -> str:
        return f"{self.arxiv_id}v{self.version}"

    def knowledge_source(self, *, acquired_at: datetime | None = None) -> KnowledgeSource:
        return KnowledgeSource(
            source_type="arxiv",
            source_id=f"arxiv:{self.arxiv_id}",
            title=self.title[:500],
            source_revision=f"v{self.version}",
            canonical_uri=self.abstract_url,
            license_uri=self.license_uri,
            privacy="public_reference",
            trust=TrustLevel.OBSERVED,
            acquired_at=acquired_at or utc_now(),
        )


class ArxivSyncConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str | None = Field(default=None, max_length=20_000)
    categories: tuple[str, ...] = DEFAULT_CATEGORIES
    topics: tuple[str, ...] = DEFAULT_TOPICS
    max_results: int = Field(default=100, ge=1, le=10_000)
    page_size: int = Field(default=100, ge=1, le=100)
    max_downloads: int = Field(default=25, ge=0, le=10_000)
    max_pdf_bytes: int = Field(default=10_000_000, ge=1_024, le=100_000_000)
    max_total_bytes: int = Field(default=250_000_000, ge=1_024, le=50_000_000_000)
    request_delay_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    max_retries: int = Field(default=4, ge=0, le=10)
    timeout_seconds: float = Field(default=45.0, ge=1.0, le=300.0)
    user_agent: str = Field(
        default="HermesGraph/0.1 (personal research knowledge connector)",
        min_length=10,
        max_length=500,
    )

    @property
    def resolved_query(self) -> str:
        return self.query or build_computer_science_query(
            categories=self.categories,
            topics=self.topics,
        )


class ArxivManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paper: ArxivPaper
    status: Literal[
        "metadata",
        "downloaded",
        "duplicate",
        "submitted",
        "skipped_oversize",
        "error",
    ] = "metadata"
    pdf_path: str | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    byte_size: int | None = Field(default=None, ge=1)
    duplicate_of: str | None = None
    ingestion_job_id: str | None = None
    error: str | None = Field(default=None, max_length=2_000)
    fetched_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ArxivManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    query: str = ""
    entries: dict[str, ArxivManifestEntry] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class ArxivSyncSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    fetched: int = 0
    downloaded: int = 0
    submitted: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: int = 0
    bytes_downloaded: int = 0
    manifest_path: str


class ArxivSourceStore:
    """Atomic, path-contained PDF cache and resumable source manifest."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.pdf_root = (self.root / "pdfs").resolve()
        self.manifest_path = self.root / "manifest.json"

    async def load(self) -> ArxivManifest:
        return await asyncio.to_thread(self._load)

    async def save(self, manifest: ArxivManifest) -> None:
        await asyncio.to_thread(self._save, manifest)

    async def write_pdf(self, paper: ArxivPaper, content: bytes) -> str:
        return await asyncio.to_thread(self._write_pdf, paper, content)

    def resolve_pdf(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if not candidate.is_relative_to(self.pdf_root):
            raise ArxivSourceError("Manifest PDF path escapes the arXiv store")
        return candidate

    def has_pdf(self, entry: ArxivManifestEntry) -> bool:
        if not entry.pdf_path or not entry.byte_size or not entry.content_hash:
            return False
        try:
            path = self.resolve_pdf(entry.pdf_path)
        except ArxivSourceError:
            return False
        return path.is_file() and path.stat().st_size == entry.byte_size

    def _load(self) -> ArxivManifest:
        if not self.manifest_path.exists():
            return ArxivManifest()
        try:
            return ArxivManifest.model_validate_json(
                self.manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ArxivSourceError("Unable to read the arXiv manifest") from exc

    def _save(self, manifest: ArxivManifest) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_name(
            f".{self.manifest_path.name}.{os.getpid()}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(manifest.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.manifest_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_pdf(self, paper: ArxivPaper, content: bytes) -> str:
        self.pdf_root.mkdir(parents=True, exist_ok=True)
        safe_id = _SAFE_NAME_PATTERN.sub("_", paper.versioned_id).strip("._")
        if not safe_id:
            raise ArxivSourceError("Unable to derive a safe arXiv PDF filename")
        path = (self.pdf_root / f"{safe_id}.pdf").resolve()
        if not path.is_relative_to(self.pdf_root):
            raise ArxivSourceError("arXiv PDF path escaped the source store")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return str(path.relative_to(self.root))


class ArxivIngestionClient:
    """Submit retained arXiv PDFs through the normal scoped ingestion API."""

    def __init__(
        self,
        base_url: str,
        *,
        project_id: str = "computer-science",
        user_id: str = "local-user",
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._project_id = project_id
        self._user_id = user_id
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def submit(self, paper: ArxivPaper, path: Path) -> str:
        content = await asyncio.to_thread(path.read_bytes)
        response = await self._client.post(
            f"{self._base_url}/v1/projects/{self._project_id}/ingestion-jobs",
            data={
                "user_id": self._user_id,
                "source": paper.knowledge_source().model_dump_json(),
            },
            files={"file": (path.name, content, "application/pdf")},
        )
        response.raise_for_status()
        payload = response.json()
        try:
            return cast(str, payload["job"]["job_id"])
        except (KeyError, TypeError) as exc:
            raise ArxivSourceError("Ingestion API returned no job identifier") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class ArxivSourceConnector:
    def __init__(
        self,
        config: ArxivSyncConfig,
        store: ArxivSourceStore,
        *,
        client: httpx.AsyncClient | None = None,
        ingestor: ArxivIngestionClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._config = config
        self._store = store
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._owns_client = client is None
        self._ingestor = ingestor
        self._sleep = sleep

    async def sync(self) -> ArxivSyncSummary:
        query = self._config.resolved_query
        papers = await self._fetch_papers(query)
        manifest = await self._store.load()
        entries = dict(manifest.entries)
        now = utc_now()
        for paper in papers:
            current = entries.get(paper.versioned_id)
            if current is None:
                entries[paper.versioned_id] = ArxivManifestEntry(paper=paper, fetched_at=now)
            else:
                entries[paper.versioned_id] = current.model_copy(
                    update={"paper": paper, "updated_at": now}
                )
        manifest = ArxivManifest(query=query, entries=entries, updated_at=now)
        await self._store.save(manifest)

        downloaded = submitted = duplicates = skipped = errors = total_bytes = 0
        hashes = {
            entry.content_hash: key
            for key, entry in entries.items()
            if entry.content_hash and self._store.has_pdf(entry)
        }
        for paper in papers:
            key = paper.versioned_id
            entry = entries[key]
            if self._store.has_pdf(entry):
                if self._ingestor is not None and entry.ingestion_job_id is None:
                    entry, was_submitted = await self._submit(entry)
                    submitted += int(was_submitted)
                    errors += int(entry.status == "error")
                    entries[key] = entry
                    manifest = await self._save_entry(manifest, entries)
                else:
                    skipped += 1
                continue
            if downloaded >= self._config.max_downloads:
                skipped += 1
                continue
            try:
                content = await self._request_bytes(
                    _validated_arxiv_url(paper.pdf_url),
                    max_bytes=self._config.max_pdf_bytes,
                    accept="application/pdf",
                )
                if not content.startswith(b"%PDF-"):
                    raise ArxivSourceError("arXiv response is not a valid PDF payload")
                if total_bytes + len(content) > self._config.max_total_bytes:
                    skipped += 1
                    continue
                digest = hashlib.sha256(content).hexdigest()
                duplicate_key = hashes.get(digest)
                if duplicate_key is not None:
                    duplicate_entry = entries[duplicate_key]
                    entry = entry.model_copy(
                        update={
                            "status": "duplicate",
                            "pdf_path": duplicate_entry.pdf_path,
                            "content_hash": digest,
                            "byte_size": len(content),
                            "duplicate_of": duplicate_key,
                            "error": None,
                            "updated_at": utc_now(),
                        }
                    )
                    duplicates += 1
                else:
                    pdf_path = await self._store.write_pdf(paper, content)
                    entry = entry.model_copy(
                        update={
                            "status": "downloaded",
                            "pdf_path": pdf_path,
                            "content_hash": digest,
                            "byte_size": len(content),
                            "duplicate_of": None,
                            "error": None,
                            "updated_at": utc_now(),
                        }
                    )
                    hashes[digest] = key
                    downloaded += 1
                    total_bytes += len(content)
                if self._ingestor is not None:
                    entry, was_submitted = await self._submit(entry)
                    submitted += int(was_submitted)
                    errors += int(entry.status == "error")
            except ArxivResponseTooLarge as exc:
                skipped += 1
                entry = entry.model_copy(
                    update={
                        "status": "skipped_oversize",
                        "error": str(exc),
                        "updated_at": utc_now(),
                    }
                )
            except (ArxivSourceError, httpx.HTTPError, OSError) as exc:
                errors += 1
                entry = entry.model_copy(
                    update={
                        "status": "error",
                        "error": str(exc)[:2_000],
                        "updated_at": utc_now(),
                    }
                )
            entries[key] = entry
            manifest = await self._save_entry(manifest, entries)
        return ArxivSyncSummary(
            query=query,
            fetched=len(papers),
            downloaded=downloaded,
            submitted=submitted,
            duplicates=duplicates,
            skipped=skipped,
            errors=errors,
            bytes_downloaded=total_bytes,
            manifest_path=str(self._store.manifest_path),
        )

    async def refresh_cached_submissions(self) -> ArxivSyncSummary:
        """Resubmit cached PDFs without fetching or changing the remote corpus."""

        manifest = await self._store.load()
        entries = dict(manifest.entries)
        submitted = skipped = errors = 0
        for key, entry in entries.items():
            if self._ingestor is None or not self._store.has_pdf(entry):
                skipped += 1
                continue
            entry, was_submitted = await self._submit(entry)
            submitted += int(was_submitted)
            errors += int(entry.status == "error")
            entries[key] = entry
            manifest = await self._save_entry(manifest, entries)
        return ArxivSyncSummary(
            query=manifest.query,
            fetched=0,
            downloaded=0,
            submitted=submitted,
            duplicates=0,
            skipped=skipped,
            errors=errors,
            bytes_downloaded=0,
            manifest_path=str(self._store.manifest_path),
        )

    async def submit_pending_cached(self) -> ArxivSyncSummary:
        """Submit cached PDFs that have no durable ingestion job reference."""

        manifest = await self._store.load()
        entries = dict(manifest.entries)
        submitted = skipped = errors = 0
        for key, entry in entries.items():
            if (
                self._ingestor is None
                or entry.ingestion_job_id is not None
                or not self._store.has_pdf(entry)
            ):
                skipped += 1
                continue
            entry, was_submitted = await self._submit(entry)
            submitted += int(was_submitted)
            errors += int(entry.status == "error")
            entries[key] = entry
            manifest = await self._save_entry(manifest, entries)
        return ArxivSyncSummary(
            query=manifest.query,
            fetched=0,
            downloaded=0,
            submitted=submitted,
            duplicates=0,
            skipped=skipped,
            errors=errors,
            bytes_downloaded=0,
            manifest_path=str(self._store.manifest_path),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._ingestor is not None:
            await self._ingestor.close()

    async def _fetch_papers(self, query: str) -> list[ArxivPaper]:
        papers: list[ArxivPaper] = []
        seen: set[str] = set()
        start = 0
        while len(papers) < self._config.max_results:
            page_size = min(
                self._config.page_size,
                self._config.max_results - len(papers),
            )
            content = await self._request_bytes(
                ARXIV_API_URL,
                params={
                    "search_query": query,
                    "start": start,
                    "max_results": page_size,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                },
                max_bytes=5_000_000,
                accept="application/atom+xml",
            )
            page, total = parse_arxiv_feed(content)
            if not page:
                break
            for paper in page:
                if paper.versioned_id not in seen:
                    papers.append(paper)
                    seen.add(paper.versioned_id)
                    if len(papers) >= self._config.max_results:
                        break
            start += len(page)
            if start >= total or len(page) < page_size:
                break
        return papers

    async def _request_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        accept: str,
        params: dict[str, str | int] | None = None,
    ) -> bytes:
        for attempt in range(self._config.max_retries + 1):
            retry_after = 0.0
            try:
                async with self._client.stream(
                    "GET",
                    url,
                    params=params,
                    headers={
                        "User-Agent": self._config.user_agent,
                        "Accept": accept,
                    },
                    follow_redirects=True,
                ) as response:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        retry_after = _retry_after_seconds(response.headers)
                        if attempt >= self._config.max_retries:
                            response.raise_for_status()
                    else:
                        response.raise_for_status()
                        length = _content_length(response.headers)
                        if length is not None and length > max_bytes:
                            raise ArxivResponseTooLarge(
                                f"Response exceeds the {max_bytes} byte limit"
                            )
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > max_bytes:
                                raise ArxivResponseTooLarge(
                                    f"Response exceeds the {max_bytes} byte limit"
                                )
                        await self._delay(self._config.request_delay_seconds)
                        return bytes(content)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= self._config.max_retries:
                    raise
            delay = max(retry_after, min(2.0**attempt, 30.0))
            await self._delay(delay)
        raise ArxivSourceError("arXiv request retry budget was exhausted")

    async def _submit(
        self,
        entry: ArxivManifestEntry,
    ) -> tuple[ArxivManifestEntry, bool]:
        if self._ingestor is None or entry.pdf_path is None:
            return entry, False
        try:
            job_id = await self._ingestor.submit(
                entry.paper,
                self._store.resolve_pdf(entry.pdf_path),
            )
            return (
                entry.model_copy(
                    update={
                        "status": "submitted",
                        "ingestion_job_id": job_id,
                        "error": None,
                        "updated_at": utc_now(),
                    }
                ),
                True,
            )
        except (ArxivSourceError, httpx.HTTPError, OSError) as exc:
            return (
                entry.model_copy(
                    update={
                        "status": "error",
                        "error": f"ingestion_submit_failed: {exc}"[:2_000],
                        "updated_at": utc_now(),
                    }
                ),
                False,
            )

    async def _save_entry(
        self,
        manifest: ArxivManifest,
        entries: dict[str, ArxivManifestEntry],
    ) -> ArxivManifest:
        updated = manifest.model_copy(
            update={"entries": dict(entries), "updated_at": utc_now()}
        )
        await self._store.save(updated)
        return updated

    async def _delay(self, seconds: float) -> None:
        if seconds > 0:
            await self._sleep(seconds)


def build_computer_science_query(
    *,
    categories: Sequence[str] = DEFAULT_CATEGORIES,
    topics: Sequence[str] = DEFAULT_TOPICS,
) -> str:
    normalized_categories = tuple(item.strip() for item in categories if item.strip())
    normalized_topics = tuple(item.strip() for item in topics if item.strip())
    if not normalized_categories:
        raise ValueError("At least one arXiv category is required")
    category_query = " OR ".join(f"cat:{item}" for item in normalized_categories)
    if not normalized_topics:
        return f"({category_query})"
    topic_query = " OR ".join(
        f'all:"{item.replace(chr(34), "").strip()}"' for item in normalized_topics
    )
    return f"({category_query}) AND ({topic_query})"


def parse_arxiv_feed(content: bytes) -> tuple[list[ArxivPaper], int]:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ArxivSourceError("arXiv returned malformed Atom XML") from exc
    total_text = _child_text(root, f"{_OPEN_SEARCH}totalResults") or "0"
    try:
        total = max(0, int(total_text))
    except ValueError as exc:
        raise ArxivSourceError("arXiv returned an invalid result count") from exc
    papers = [_parse_entry(entry) for entry in root.findall(f"{_ATOM}entry")]
    return papers, total


def _parse_entry(entry: ElementTree.Element) -> ArxivPaper:
    raw_id = _required_text(entry, f"{_ATOM}id")
    versioned_id = _arxiv_identifier(raw_id)
    match = _VERSION_PATTERN.fullmatch(versioned_id)
    if match is None:
        raise ArxivSourceError(f"arXiv entry has no versioned identifier: {versioned_id}")
    arxiv_id = match.group("id")
    version = int(match.group("version"))
    links = entry.findall(f"{_ATOM}link")
    abstract_url = _first_link(links, rel="alternate") or f"https://arxiv.org/abs/{versioned_id}"
    pdf_url = _first_link(links, title="pdf") or f"https://arxiv.org/pdf/{versioned_id}"
    authors = tuple(
        name
        for author in entry.findall(f"{_ATOM}author")
        if (name := _child_text(author, f"{_ATOM}name"))
    )
    categories = tuple(
        term
        for category in entry.findall(f"{_ATOM}category")
        if (term := category.attrib.get("term", "").strip())
    )
    primary = entry.find(f"{_ARXIV}primary_category")
    return ArxivPaper(
        arxiv_id=arxiv_id,
        version=version,
        title=_normalize_text(_required_text(entry, f"{_ATOM}title")),
        summary=_normalize_text(_child_text(entry, f"{_ATOM}summary") or ""),
        authors=authors,
        categories=categories,
        primary_category=(primary.attrib.get("term") if primary is not None else None),
        published=_required_text(entry, f"{_ATOM}published"),
        updated=_required_text(entry, f"{_ATOM}updated"),
        abstract_url=_https_url(abstract_url),
        pdf_url=_https_url(pdf_url),
        license_uri=_child_text(entry, f"{_ARXIV}license"),
        doi=_child_text(entry, f"{_ARXIV}doi"),
        journal_ref=_child_text(entry, f"{_ARXIV}journal_ref"),
        comment=_child_text(entry, f"{_ARXIV}comment"),
    )


def _arxiv_identifier(value: str) -> str:
    path = urlsplit(value).path
    marker = "/abs/"
    if marker in path:
        return path.split(marker, 1)[1].strip("/")
    return value.rsplit("/", 1)[-1]


def _first_link(
    links: Sequence[ElementTree.Element],
    *,
    rel: str | None = None,
    title: str | None = None,
) -> str | None:
    for link in links:
        if rel is not None and link.attrib.get("rel") != rel:
            continue
        if title is not None and link.attrib.get("title") != title:
            continue
        href = link.attrib.get("href", "").strip()
        if href:
            return href
    return None


def _required_text(parent: ElementTree.Element, tag: str) -> str:
    value = _child_text(parent, tag)
    if value is None:
        raise ArxivSourceError(f"arXiv entry is missing required field {tag}")
    return value


def _child_text(parent: ElementTree.Element, tag: str) -> str | None:
    child = parent.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _https_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme == "http":
        return value.replace("http://", "https://", 1)
    return value


def _validated_arxiv_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or parts.hostname not in _ALLOWED_ARXIV_HOSTS:
        raise ArxivSourceError("Refusing a non-arXiv PDF URL")
    return value


def _content_length(headers: httpx.Headers) -> int | None:
    value = headers.get("Content-Length")
    if value is None:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def _retry_after_seconds(headers: httpx.Headers) -> float:
    value = headers.get("Retry-After")
    if value is None:
        return 0.0
    try:
        return max(0.0, min(float(value), 300.0))
    except ValueError:
        return 0.0
