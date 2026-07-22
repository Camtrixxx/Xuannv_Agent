"""Multi-patch ordinary-report coverage for the Haidian service."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from agent.config import EmbeddingAPIConfig, ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportRequest
from agent.services.haidian_embedding_service import HaidianEmbeddingAnalysisService
from agent.services.report_service import ReportService


BOUNDS = [435014.236, 4415283.021, 436294.236, 4416563.021]


def _patch(patch_id: str, lon: float) -> dict:
    return {
        "patch_id": patch_id,
        "has_embedding": True,
        "available_months": ["202512"],
        "available_tasks": ["building_extraction"],
        "bounds": BOUNDS,
        "bounds_wgs84": [lon, 39.90, lon + 0.01, 39.91],
    }


def _png(path: Path, foreground_columns: int) -> Path:
    image = Image.new("RGB", (4, 4), (255, 255, 255))
    pixels = image.load()
    for x in range(foreground_columns):
        for y in range(4):
            pixels[x, y] = (230, 0, 0)
    image.save(path, format="PNG")
    return path


def _service(monkeypatch, tmp_path: Path, patches: list[dict], failed: set[str] | None = None):
    service = HaidianEmbeddingAnalysisService(
        config=EmbeddingAPIConfig(base_url="http://test", max_selected_patches=8),
        report_config=ReportConfig(asset_dir=tmp_path),
    )
    by_id = {item["patch_id"]: item for item in patches}
    failed = failed or set()
    result_paths = {
        item["patch_id"]: _png(tmp_path / f"{item['patch_id']}.png", index + 1)
        for index, item in enumerate(patches)
    }
    embedding = _png(tmp_path / "embedding.png", 1)

    monkeypatch.setattr(service, "_get_json", lambda path: by_id[path.rsplit("/", 1)[-1]])
    monkeypatch.setattr(service, "_get_json_optional", lambda path: {})

    def fake_download(remote_url, request, task_id, kind):
        if kind.startswith("task_result_"):
            patch_id = kind.removeprefix("task_result_")
            if patch_id in failed:
                raise RuntimeError("模拟结果图服务失败")
            return result_paths[patch_id]
        return embedding

    monkeypatch.setattr(service, "_download_remote_asset", fake_download)
    monkeypatch.setattr("agent.services.haidian_embedding_service.basemap_chart", lambda *args: None)
    return service


def test_selected_patches_are_aggregated_and_keep_each_result(monkeypatch, tmp_path):
    patches = [_patch("p1", 116.20), _patch("p2", 116.21)]
    service = _service(monkeypatch, tmp_path, patches)
    result = service.analyze(
        ReportRequest(
            task="建筑物提取",
            region="北京市海淀区",
            prompt="分析两个 patch",
            time_range="2025-12",
            selected_patch_ids=["p1", "p2"],
        )
    )

    assert result.aef_payload["used_patch_ids"] == ["p1", "p2"]
    assert result.aef_payload["failed_patch_ids"] == []
    assert result.aef_payload["image_stats"]["patch_count"] == 2
    # Multiple successful patches mosaic into ONE seamless overlay layer,
    # owned by the comma-joined patch ids.
    overlays = [chart for chart in result.charts if chart.overlay]
    assert len(overlays) == 1
    assert overlays[0].patch_id == "p1,p2"
    assert len(result.patch_results) == 2
    assert result.aef_payload["fingerprint"] == service._fingerprint(
        "building_extraction", "p1,p2", "202512", {"summary": {}, "image": result.aef_payload["image_stats"]}
    )


def test_one_failed_patch_is_reported_without_losing_successes(monkeypatch, tmp_path):
    patches = [_patch("p1", 116.20), _patch("p2", 116.21), _patch("p3", 116.22)]
    service = _service(monkeypatch, tmp_path, patches, failed={"p2"})
    result = service.analyze(
        ReportRequest(
            task="建筑物提取",
            region="北京市海淀区",
            prompt="分析三个 patch",
            time_range="2025-12",
            selected_patch_ids=["p1", "p2", "p3"],
        )
    )

    assert result.aef_payload["used_patch_ids"] == ["p1", "p3"]
    assert result.aef_payload["failed_patch_ids"] == ["p2"]
    assert "获取失败" in result.summary
    failed_rows = [row for row in result.patch_results if row["status"] == "failed"]
    assert failed_rows[0]["patch_id"] == "p2"


def test_report_map_contains_all_patch_layers(tmp_path):
    analysis = AnalysisResult(
        task="建筑物提取",
        region="北京市海淀区",
        time_range="2025-12",
        headline="海淀建筑物提取",
        summary="summary",
        metrics=[MetricCard("目标覆盖率", "20%")],
        findings=[],
        recommendations=[],
        charts=[
            ChartAsset("p1", "image", "/reports/assets/p1.png", "p1", [116.2, 39.9, 116.21, 39.91], True, "p1"),
            ChartAsset("p2", "image", "/reports/assets/p2.png", "p2", [116.21, 39.9, 116.22, 39.91], True, "p2"),
        ],
    )
    section, _, script = ReportService(config=ReportConfig(report_dir=tmp_path, asset_dir=tmp_path))._result_map_html(analysis)
    assert "地图包含 2 个 patch 结果图层" in section
    assert '"title": "p1"' in script
    assert '"title": "p2"' in script
