from __future__ import annotations

from io import BytesIO

from PIL import Image

from agent.config import EmbeddingAPIConfig, ReportConfig
from agent.schemas.report import ReportRequest
from agent.services.custom_model_analysis_service import CustomModelAnalysisService
from agent.services.model_registry_service import ModelInfo


class _Registry:
    def model_status(self, model_id, region_id=""):
        return ModelInfo.from_payload({
            "id": model_id,
            "name": "湿地模型",
            "type": "single_time_detection",
            "task_type": "land_use_classification",
            "status": "completed",
            "source": "custom",
            "created_at": "2026-07-29T12:00:00",
            "classes": [
                {"id": "wet", "name": "湿地", "color": "#ff0000"},
                {"id": "other", "name": "其他", "color": "#0000ff"},
            ],
        })


def _png(colors):
    image = Image.new("RGB", (2, 2))
    image.putdata(colors)
    out = BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def test_multi_patch_batch_inference_extracts_only_target_class(tmp_path, monkeypatch):
    service = CustomModelAnalysisService(
        config=EmbeddingAPIConfig(max_selected_patches=64),
        report_config=ReportConfig(report_dir=tmp_path, asset_dir=tmp_path / "assets"),
        registry=_Registry(),
    )
    patches = {
        "patch_000001": {
            "patch_id": "patch_000001", "has_embedding": True,
            "available_months": ["202603"],
            "bounds": [0, 0, 1000, 1000],
            "bounds_wgs84": [116.20, 39.88, 116.21, 39.89],
        },
        "patch_000002": {
            "patch_id": "patch_000002", "has_embedding": True,
            "available_months": ["202603"],
            "bounds": [1000, 0, 2000, 1000],
            "bounds_wgs84": [116.21, 39.88, 116.22, 39.89],
        },
    }
    red, blue, grey = (255, 0, 0), (0, 0, 255), (200, 200, 200)
    images = {
        "result-1": _png([red, blue, grey, grey]),
        "result-2": _png([red, red, blue, grey]),
    }
    monkeypatch.setattr(service.http, "get_json", lambda path: patches[path.rsplit("/", 1)[-1]])
    monkeypatch.setattr(service.http, "post_json", lambda path, body: {
        "total": 2, "success_count": 2, "error_count": 0,
        "results": [
            {"patch_id": "patch_000001", "status": "ok", "result_url": "result-1"},
            {"patch_id": "patch_000002", "status": "ok", "result_url": "result-2"},
        ],
    })
    monkeypatch.setattr(service.http, "fetch_bytes", lambda url, asset_label: images[url])
    monkeypatch.setattr("agent.services.custom_model_analysis_service.basemap_chart", lambda *a, **k: None)

    result = service.analyze(ReportRequest(
        task="湿地识别",
        region="北京市海淀区",
        prompt="海淀2026年3月湿地分布",
        time_range="2026-03",
        selected_patch_ids=["patch_000001", "patch_000002"],
        custom_model_id="model_wet",
        target_object="湿地",
    ))

    aggregate = result.aef_payload["aggregate"]
    assert aggregate["patch_count"] == 2
    assert aggregate["coverage_ratio"] == 0.375
    assert aggregate["covered_area_ha"] == 75.0
    assert result.aef_payload["used_patch_ids"] == ["patch_000001", "patch_000002"]
    overlays = [chart for chart in result.charts if chart.overlay]
    assert len(overlays) == 1
    mosaic = tmp_path / "assets" / overlays[0].url.rsplit("/", 1)[-1]
    with Image.open(mosaic) as image:
        alpha = image.convert("RGBA").getchannel("A")
        assert sum(1 for value in alpha.getdata() if value) == 3
