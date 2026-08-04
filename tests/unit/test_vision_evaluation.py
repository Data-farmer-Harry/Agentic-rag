from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.models import (
    NormalizedBoundingBox,
    VisionAnalysis,
    VisualRegionDraft,
)
from app.evaluation.graph_extraction import OpenAIUsageAccumulator
from app.evaluation.vision import (
    ExpectedVisionRegion,
    VisionEvalThresholds,
    VisionEvaluator,
    VisionGoldenCase,
    VisionGoldenSet,
)
from app.evaluation.vision_cli import _select_cases

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _case(*, required_pass: bool = False) -> VisionGoldenCase:
    return VisionGoldenCase(
        case_id="dashboard_table",
        description="A table with exact OCR and a bounded region",
        asset_path="fixture.png",
        asset_sha256=hashlib.sha256(_ONE_PIXEL_PNG).hexdigest(),
        category="table",
        difficulty="easy",
        tags=["synthetic", "ocr"],
        required_pass=required_pass,
        required_title_term_groups=[["dashboard"]],
        required_summary_term_groups=[["qdrant"], ["retrieval", "search"]],
        required_visible_text=["Recall 0.91", "Latency 24 ms"],
        forbidden_title_terms=["security override"],
        expected_regions=[
            ExpectedVisionRegion(
                region_id="metrics_table",
                accepted_categories=["table"],
                required_term_groups=[["metrics", "retrieval"]],
                required_visible_text=["Qdrant", "Recall 0.91"],
                bounding_box=NormalizedBoundingBox(
                    x_min=0.1,
                    y_min=0.2,
                    x_max=0.9,
                    y_max=0.8,
                ),
                minimum_iou=0.5,
            )
        ],
        max_regions=2,
    )


def _analysis() -> VisionAnalysis:
    return VisionAnalysis(
        title="Retrieval dashboard",
        summary="A Qdrant retrieval dashboard with quality and latency metrics.",
        visible_text="Qdrant | Recall 0.91 | Latency 24 ms",
        regions=[
            VisualRegionDraft(
                label="Metrics table",
                category="table",
                description="Retrieval metrics for Qdrant.",
                visible_text="Qdrant Recall 0.91 Latency 24 ms",
                bounding_box=NormalizedBoundingBox(
                    x_min=0.1,
                    y_min=0.2,
                    x_max=0.9,
                    y_max=0.8,
                ),
                confidence=0.98,
            )
        ],
    )


class _Analyzer:
    revision = "vision-eval-fixture-v1"

    def __init__(
        self,
        analysis: VisionAnalysis,
        tracker: OpenAIUsageAccumulator | None = None,
    ) -> None:
        self._analysis = analysis
        self._tracker = tracker

    async def analyze(
        self,
        content: bytes,
        *,
        media_type: str,
        filename: str,
    ) -> VisionAnalysis:
        assert content == _ONE_PIXEL_PNG
        assert media_type == "image/png"
        assert filename == "fixture.png"
        if self._tracker is not None:
            self._tracker.observe(
                SimpleNamespace(
                    usage=SimpleNamespace(
                        input_tokens=100,
                        output_tokens=20,
                        total_tokens=120,
                        input_tokens_details=SimpleNamespace(cached_tokens=10),
                    )
                )
            )
        return self._analysis

    async def close(self) -> None:
        return None


class _FlakyAnalyzer(_Analyzer):
    def __init__(self, analysis: VisionAnalysis) -> None:
        super().__init__(analysis)
        self.calls = 0

    async def analyze(
        self,
        content: bytes,
        *,
        media_type: str,
        filename: str,
    ) -> VisionAnalysis:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("temporary upstream disconnect")
        return await super().analyze(
            content,
            media_type=media_type,
            filename=filename,
        )


@pytest.mark.asyncio
async def test_vision_evaluator_scores_components_slices_usage_and_cost(
    tmp_path: Path,
) -> None:
    (tmp_path / "fixture.png").write_bytes(_ONE_PIXEL_PNG)
    tracker = OpenAIUsageAccumulator()
    golden = VisionGoldenSet(name="vision fixture", revision="v1", cases=[_case()])

    report = await VisionEvaluator(
        _Analyzer(_analysis(), tracker),
        asset_root=tmp_path,
        usage_probe=tracker.snapshot,
        input_cost_per_million=2.0,
        cached_input_cost_per_million=1.0,
        output_cost_per_million=10.0,
    ).run(golden)

    assert report.passed is True
    assert report.metrics.case_pass_rate == 1.0
    assert report.metrics.ocr_recall == 1.0
    assert report.metrics.region_recall == 1.0
    assert report.metrics.bbox_accuracy == 1.0
    assert report.category_metrics["table"].pass_rate == 1.0
    assert report.difficulty_metrics["easy"].region_recall == 1.0
    assert report.tag_metrics["ocr"].ocr_recall == 1.0
    assert report.cases[0].region_matches == {"metrics_table": 0}
    assert report.cases[0].region_ious == {"metrics_table": 1.0}
    assert report.usage.total_tokens == 120
    assert report.estimated_cost_usd == 0.00039


@pytest.mark.asyncio
async def test_vision_evaluator_recovers_only_after_a_retryable_case_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "fixture.png").write_bytes(_ONE_PIXEL_PNG)
    analyzer = _FlakyAnalyzer(_analysis())
    golden = VisionGoldenSet(name="vision fixture", revision="v1", cases=[_case()])

    report = await VisionEvaluator(
        analyzer,
        asset_root=tmp_path,
        max_case_attempts=2,
        retry_base_seconds=0.0,
    ).run(golden)

    assert report.passed is True
    assert report.model_attempts == 2
    assert report.recovered_cases == 1
    assert report.cases[0].attempt_count == 2
    assert report.cases[0].attempt_errors == ["ConnectionError: temporary upstream disconnect"]


@pytest.mark.asyncio
async def test_required_vision_case_blocks_zero_threshold_gate(tmp_path: Path) -> None:
    (tmp_path / "fixture.png").write_bytes(_ONE_PIXEL_PNG)
    bad = _analysis().model_copy(update={"visible_text": "nothing useful"})
    golden = VisionGoldenSet(
        name="required fixture",
        revision="v1",
        cases=[_case(required_pass=True)],
    )

    report = await VisionEvaluator(
        _Analyzer(bad),
        asset_root=tmp_path,
        thresholds=VisionEvalThresholds(
            minimum_success_rate=0.0,
            minimum_case_pass_rate=0.0,
            minimum_title_term_recall=0.0,
            minimum_summary_term_recall=0.0,
            minimum_ocr_recall=0.0,
            minimum_region_recall=0.0,
            minimum_region_category_accuracy=0.0,
            minimum_region_text_recall=0.0,
            minimum_bbox_accuracy=0.0,
            minimum_forbidden_content_accuracy=0.0,
        ),
    ).run(golden)

    assert report.passed is False
    assert report.gate_failures == ["required_case:dashboard_table"]
    assert report.cases[0].reasons == ["ocr_mismatch"]


def test_vision_golden_contract_rejects_unsafe_paths_and_invalid_regions() -> None:
    with pytest.raises(ValidationError, match="relative"):
        VisionGoldenCase(
            case_id="unsafe_path",
            description="Invalid fixture",
            asset_path="../secret.png",
            asset_sha256="a" * 64,
            category="security",
        )

    with pytest.raises(ValidationError, match="region IDs"):
        VisionGoldenCase(
            case_id="duplicate_regions",
            description="Invalid fixture",
            asset_path="fixture.png",
            asset_sha256="a" * 64,
            category="diagram",
            expected_regions=[
                ExpectedVisionRegion(
                    region_id="same",
                    accepted_categories=["diagram"],
                ),
                ExpectedVisionRegion(
                    region_id="same",
                    accepted_categories=["diagram"],
                ),
            ],
        )


def test_repository_vision_golden_set_has_frozen_asset_and_slice_contract() -> None:
    dataset_path = Path("examples/evaluation/vision_golden.json")
    fixture = VisionGoldenSet.load(dataset_path)

    assert fixture.revision == "2026-07-16-v4"
    assert len(fixture.cases) == 11
    assert sum(len(case.expected_regions) for case in fixture.cases) == 13
    assert sum("natural_arxiv" in case.tags for case in fixture.cases) == 3
    assert {case.case_id for case in fixture.cases if case.required_pass} == {
        "synthetic_prompt_injection_benchmark",
        "synthetic_blank_low_information",
    }
    assert {case.category for case in fixture.cases} == {
        "diagram",
        "chart",
        "table",
        "interface",
        "scan",
        "multi_region",
        "security",
        "negative",
        "document",
    }
    for case in fixture.cases:
        asset = dataset_path.parent / case.asset_path
        assert asset.is_file()
        assert hashlib.sha256(asset.read_bytes()).hexdigest() == case.asset_sha256

    v1_fixture = VisionGoldenSet.load(Path("examples/evaluation/vision_golden_v1.json"))
    assert v1_fixture.revision == "2026-07-16-v1"
    assert len(v1_fixture.cases) == 11
    v2_fixture = VisionGoldenSet.load(Path("examples/evaluation/vision_golden_v2.json"))
    assert v2_fixture.revision == "2026-07-16-v2"
    assert sum(len(case.expected_regions) for case in v2_fixture.cases) == 14
    v3_fixture = VisionGoldenSet.load(Path("examples/evaluation/vision_golden_v3.json"))
    assert v3_fixture.revision == "2026-07-16-v3"
    assert sum(len(case.expected_regions) for case in v3_fixture.cases) == 13


def test_vision_cli_selects_a_reproducible_validated_subset() -> None:
    fixture = VisionGoldenSet(
        name="subset fixture",
        revision="v1",
        cases=[
            _case(),
            _case().model_copy(update={"case_id": "second_case"}),
        ],
    )

    selected = _select_cases(fixture, ["second_case"])

    assert [case.case_id for case in selected.cases] == ["second_case"]
    assert selected.revision.startswith("v1+subset.")
    assert selected.revision == _select_cases(fixture, ["second_case"]).revision
    with pytest.raises(ValueError, match="Unknown Vision case IDs"):
        _select_cases(fixture, ["missing_case"])
    with pytest.raises(ValueError, match="more than once"):
        _select_cases(fixture, ["second_case", "second_case"])
