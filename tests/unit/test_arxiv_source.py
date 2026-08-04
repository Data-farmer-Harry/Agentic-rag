from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.sources.arxiv import (
    ArxivIngestionClient,
    ArxivPaper,
    ArxivSourceConnector,
    ArxivSourceStore,
    ArxivSyncConfig,
    build_computer_science_query,
    parse_arxiv_feed,
)


def _feed(*ids: str) -> bytes:
    entries = "".join(
        f"""
  <entry>
    <id>http://arxiv.org/abs/{paper_id}</id>
    <updated>2026-07-1{index}T00:00:00Z</updated>
    <published>2026-07-0{index}T00:00:00Z</published>
    <title> Agentic retrieval paper {index} </title>
    <summary>Evidence-first graph retrieval.</summary>
    <author><name>Researcher {index}</name></author>
    <category term="cs.AI" />
    <arxiv:primary_category term="cs.AI" />
    <arxiv:license>http://creativecommons.org/licenses/by/4.0/</arxiv:license>
    <link rel="alternate" href="http://arxiv.org/abs/{paper_id}" />
    <link title="pdf" href="http://arxiv.org/pdf/{paper_id}" />
  </entry>
"""
        for index, paper_id in enumerate(ids, start=1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>{len(ids)}</opensearch:totalResults>
  {entries}
</feed>
""".encode()


def _paper() -> ArxivPaper:
    papers, _ = parse_arxiv_feed(_feed("2607.00001v2"))
    return papers[0]


class _RecordingIngestor:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    async def submit(self, paper: ArxivPaper, path: Path) -> str:
        del path
        self.submitted.append(paper.versioned_id)
        return f"job-{len(self.submitted)}"

    async def close(self) -> None:
        return None


def test_build_query_and_parse_atom_metadata() -> None:
    query = build_computer_science_query(
        categories=("cs.AI", "cs.IR"),
        topics=("agentic RAG", "knowledge graph"),
    )
    assert "cat:cs.AI OR cat:cs.IR" in query
    assert 'all:"agentic RAG"' in query

    papers, total = parse_arxiv_feed(_feed("2607.00001v2"))
    assert total == 1
    paper = papers[0]
    assert paper.arxiv_id == "2607.00001"
    assert paper.version == 2
    assert paper.authors == ("Researcher 1",)
    assert paper.categories == ("cs.AI",)
    assert paper.license_uri == "http://creativecommons.org/licenses/by/4.0/"
    assert paper.abstract_url == "https://arxiv.org/abs/2607.00001v2"
    source = paper.knowledge_source()
    assert source.source_type == "arxiv"
    assert source.source_id == "arxiv:2607.00001"
    assert source.title == "Agentic retrieval paper 1"
    assert source.source_revision == "v2"
    assert source.privacy == "public_reference"


@pytest.mark.asyncio
async def test_sync_is_budgeted_resumable_and_atomic(tmp_path: Path) -> None:
    pdfs = {
        "/pdf/2607.00001v1": b"%PDF-1.7\nfirst",
        "/pdf/2607.00002v1": b"%PDF-1.7\nsecond",
    }
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/query":
            query = parse_qs(request.url.query.decode())
            assert "search_query" in query
            return httpx.Response(200, content=_feed("2607.00001v1", "2607.00002v1"))
        content = pdfs[request.url.path]
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf", "Content-Length": str(len(content))},
            content=content,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = ArxivSourceStore(tmp_path / "arxiv")
    config = ArxivSyncConfig(
        max_results=2,
        max_downloads=1,
        request_delay_seconds=0,
    )
    connector = ArxivSourceConnector(config, store, client=client)
    first = await connector.sync()
    second = await connector.sync()
    await client.aclose()

    assert first.downloaded == 1
    assert first.skipped == 1
    assert second.downloaded == 1
    manifest = await store.load()
    assert set(manifest.entries) == {"2607.00001v1", "2607.00002v1"}
    assert all(entry.status == "downloaded" for entry in manifest.entries.values())
    assert all(store.has_pdf(entry) for entry in manifest.entries.values())
    assert requests.count("/pdf/2607.00001v1") == 1
    assert requests.count("/pdf/2607.00002v1") == 1
    assert not list((tmp_path / "arxiv").glob("*.tmp"))


@pytest.mark.asyncio
async def test_sync_can_explicitly_refresh_already_submitted_pdfs(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/query":
            return httpx.Response(200, content=_feed("2607.00001v1"))
        return httpx.Response(200, content=b"%PDF-1.7\nrefresh-fixture")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ingestor = _RecordingIngestor()
    connector = ArxivSourceConnector(
        ArxivSyncConfig(max_results=1, max_downloads=1, request_delay_seconds=0),
        ArxivSourceStore(tmp_path / "arxiv"),
        client=client,
        ingestor=ingestor,  # type: ignore[arg-type]
    )

    first = await connector.sync()
    second = await connector.refresh_cached_submissions()
    await connector.close()
    await client.aclose()

    assert first.submitted == 1
    assert second.submitted == 1
    assert second.fetched == 0
    assert ingestor.submitted == ["2607.00001v1", "2607.00001v1"]


@pytest.mark.asyncio
async def test_submit_pending_cached_is_resumable_without_refetching(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/query":
            return httpx.Response(
                200,
                content=_feed("2607.00001v1", "2607.00002v1"),
            )
        return httpx.Response(200, content=f"%PDF-1.7\n{request.url.path}".encode())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = ArxivSourceStore(tmp_path / "arxiv")
    downloader = ArxivSourceConnector(
        ArxivSyncConfig(max_results=2, max_downloads=2, request_delay_seconds=0),
        store,
        client=client,
    )
    await downloader.sync()
    ingestor = _RecordingIngestor()
    submitter = ArxivSourceConnector(
        ArxivSyncConfig(max_results=1, max_downloads=0, request_delay_seconds=0),
        store,
        client=client,
        ingestor=ingestor,  # type: ignore[arg-type]
    )

    first = await submitter.submit_pending_cached()
    second = await submitter.submit_pending_cached()
    await submitter.close()
    await client.aclose()

    assert first.fetched == 0
    assert first.submitted == 2
    assert second.submitted == 0
    assert ingestor.submitted == ["2607.00001v1", "2607.00002v1"]
    manifest = await store.load()
    assert all(entry.ingestion_job_id for entry in manifest.entries.values())


@pytest.mark.asyncio
async def test_sync_deduplicates_pdf_content_by_hash(tmp_path: Path) -> None:
    content = b"%PDF-1.7\nsame-content"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/query":
            return httpx.Response(200, content=_feed("2607.00003v1", "2607.00004v1"))
        return httpx.Response(200, content=content)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = ArxivSourceStore(tmp_path / "arxiv")
    connector = ArxivSourceConnector(
        ArxivSyncConfig(max_results=2, max_downloads=2, request_delay_seconds=0),
        store,
        client=client,
    )
    summary = await connector.sync()
    await client.aclose()

    assert summary.downloaded == 1
    assert summary.duplicates == 1
    manifest = await store.load()
    entries = list(manifest.entries.values())
    assert entries[0].content_hash == entries[1].content_hash
    assert entries[0].pdf_path == entries[1].pdf_path
    assert len(list(store.pdf_root.glob("*.pdf"))) == 1


@pytest.mark.asyncio
async def test_sync_rejects_oversize_pdf_before_buffering(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/query":
            return httpx.Response(200, content=_feed("2607.00005v1"))
        return httpx.Response(
            200,
            headers={"Content-Length": "5000"},
            content=b"%PDF-1.7\nsmall-wire-fixture",
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    store = ArxivSourceStore(tmp_path / "arxiv")
    connector = ArxivSourceConnector(
        ArxivSyncConfig(
            max_results=1,
            max_downloads=1,
            max_pdf_bytes=1024,
            request_delay_seconds=0,
        ),
        store,
        client=client,
    )
    summary = await connector.sync()
    await client.aclose()

    assert summary.skipped == 1
    assert summary.errors == 0
    assert not list(store.pdf_root.glob("*.pdf"))
    manifest = await store.load()
    assert manifest.entries["2607.00005v1"].status == "skipped_oversize"


@pytest.mark.asyncio
async def test_ingestion_submission_carries_public_source_contract(tmp_path: Path) -> None:
    received: bytes | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal received
        received = request.content
        return httpx.Response(202, json={"job": {"job_id": "job-123"}})

    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.7\nfixture")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ingestor = ArxivIngestionClient("http://agent.local", client=client)
    job_id = await ingestor.submit(_paper(), path)
    await client.aclose()

    assert job_id == "job-123"
    assert received is not None
    decoded = received.decode("utf-8")
    assert 'name="source"' in decoded
    assert '"source_type":"arxiv"' in decoded
    assert '"privacy":"public_reference"' in decoded
    assert '"title":"Agentic retrieval paper 1"' in decoded
    assert json.dumps("arxiv:2607.00001")[1:-1] in decoded
