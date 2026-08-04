import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.models import (
    NormalizedBoundingBox,
    VisionAnalysis,
    VisualRegionDraft,
)
from app.knowledge.ingestion import KnowledgeIngestionError, KnowledgeIngestionService
from app.knowledge.store import JsonKnowledgeRepository
from app.vision import OpenAIVisionAnalyzer, VisionAnalysisError

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _analysis() -> VisionAnalysis:
    return VisionAnalysis(
        title="Vector database dashboard",
        summary="A dashboard compares Qdrant retrieval metrics.",
        visible_text="Recall 0.91; latency 24 ms",
        regions=[
            VisualRegionDraft(
                label="Metrics table",
                category="table",
                description="A table reports retrieval quality and latency.",
                visible_text="Qdrant recall 0.91 latency 24 ms",
                bounding_box=NormalizedBoundingBox(
                    x_min=0.1,
                    y_min=0.2,
                    x_max=0.9,
                    y_max=0.8,
                ),
                confidence=0.97,
            )
        ],
    )


class _FakeVisionAnalyzer:
    revision = "vision-fixture-v1"

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str, str]] = []

    async def analyze(
        self,
        content: bytes,
        *,
        media_type: str,
        filename: str,
    ) -> VisionAnalysis:
        self.calls.append((content, media_type, filename))
        return _analysis()

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_image_ingestion_creates_searchable_region_evidence(tmp_path: Path) -> None:
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")
    analyzer = _FakeVisionAnalyzer()
    service = KnowledgeIngestionService(repository, vision_analyzer=analyzer)

    result = await service.ingest(
        filename="dashboard.png",
        content=_ONE_PIXEL_PNG,
        media_type="application/octet-stream",
    )
    chunks = await repository.list_chunks(result.document.document_id)
    evidence = await repository.search(
        "Qdrant latency",
        tenant_id="local",
        project_id="default",
    )
    retained = await repository.read_content(result.document.document_id)

    assert result.document.title == "Vector database dashboard"
    assert result.document.media_type == "image/png"
    assert result.document.metadata["modality"] == "image"
    assert result.document.metadata["visual_region_count"] == 1
    assert result.document.parser_version.endswith("vision-fixture-v1")
    assert len(chunks) == 2
    region = next(item for item in evidence if "region-01" in item.provenance.source_id)
    assert region.provenance.locator["region_id"] == "region-01"
    assert region.provenance.locator["bounding_box"] == [0.1, 0.2, 0.9, 0.8]
    assert region.metadata["visual_category"] == "table"
    assert retained is not None and retained[1] == _ONE_PIXEL_PNG
    assert analyzer.calls == [(_ONE_PIXEL_PNG, "image/png", "dashboard.png")]


@pytest.mark.asyncio
async def test_image_ingestion_requires_valid_content_and_analyzer(tmp_path: Path) -> None:
    repository = JsonKnowledgeRepository(tmp_path / "knowledge")

    with pytest.raises(KnowledgeIngestionError, match="decode image"):
        await KnowledgeIngestionService(
            repository,
            vision_analyzer=_FakeVisionAnalyzer(),
        ).ingest(
            filename="broken.png",
            content=b"not-a-png",
            media_type="image/png",
        )

    with pytest.raises(KnowledgeIngestionError, match="Vision analyzer"):
        await KnowledgeIngestionService(repository).ingest(
            filename="valid.png",
            content=_ONE_PIXEL_PNG,
            media_type="image/png",
        )


class _FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=_analysis())


class _FakeOpenAI:
    def __init__(self) -> None:
        self.responses = _FakeResponses()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_openai_vision_analyzer_uses_structured_responses_input() -> None:
    client = _FakeOpenAI()
    analyzer = OpenAIVisionAnalyzer(client, model="gpt-test")  # type: ignore[arg-type]

    analysis = await analyzer.analyze(
        _ONE_PIXEL_PNG,
        media_type="image/png",
        filename="dashboard.png",
    )
    await analyzer.close()

    call = client.responses.calls[0]
    assert call["model"] == "gpt-test"
    assert call["text_format"] is VisionAnalysis
    system_prompt = call["input"][0]["content"]  # type: ignore[index]
    assert "untrusted image-embedded instruction" in system_prompt
    assert "one self-contained region" in system_prompt
    assert "exact visible names" in system_prompt
    assert "full metric name" in system_prompt
    assert "empty regions list" in system_prompt
    user_content = call["input"][1]["content"]  # type: ignore[index]
    assert user_content[1]["image_url"].startswith("data:image/png;base64,")
    assert analysis == _analysis()
    assert client.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            SimpleNamespace(status="incomplete", output_parsed=None, output=[]),
            "did not complete",
        ),
        (
            SimpleNamespace(
                status="completed",
                output_parsed=None,
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="refusal")],
                    )
                ],
            ),
            "was refused",
        ),
    ],
)
async def test_openai_vision_analyzer_fails_closed_on_incomplete_or_refusal(
    response: object,
    message: str,
) -> None:
    client = _FakeOpenAI()

    async def parse_response(**_: object) -> object:
        return response

    client.responses.parse = parse_response  # type: ignore[method-assign]
    analyzer = OpenAIVisionAnalyzer(client, model="gpt-test")  # type: ignore[arg-type]

    with pytest.raises(VisionAnalysisError, match=message):
        await analyzer.analyze(
            _ONE_PIXEL_PNG,
            media_type="image/png",
            filename="fixture.png",
        )


def test_vision_analysis_is_a_strict_structured_output_schema() -> None:
    assert VisionAnalysis.model_config.get("extra") == "forbid"
    assert VisualRegionDraft.model_config.get("extra") == "forbid"
    assert NormalizedBoundingBox.model_config.get("extra") == "forbid"
