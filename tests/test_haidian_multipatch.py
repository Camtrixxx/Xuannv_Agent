"""Multi-patch ordinary-report coverage for the Haidian service."""

from __future__ import annotations

from io import BytesIO
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


def _png_bytes(colour: tuple[int, int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (4, 4), colour).save(buffer, format="PNG")
    return buffer.getvalue()


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


def test_report_html_has_no_embedded_map(tmp_path):
    """The on-map overlay module moved out of the report into the frontend's
    right-side map panel, so the report HTML must no longer embed a Leaflet map.
    The per-patch result images stay as static figures."""
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
    service = ReportService(config=ReportConfig(report_dir=tmp_path, asset_dir=tmp_path))
    assert not hasattr(service, "_result_map_html")
    page = service._render_html(
        "海淀建筑物提取",
        {"summary": "s", "highlights": [], "analysis": [], "recommendations": []},
        analysis,
        analysis.metrics,
    )
    for marker in ("resultMap", "leaflet", "L.imageOverlay", "在地图上查看结果", "mapOpacity"):
        assert marker not in page, marker
    # The result images themselves are still shown inline.
    assert "/reports/assets/p1.png" in page
    assert "/reports/assets/p2.png" in page


def test_overlay_metadata_survives_for_the_map_panel(tmp_path, monkeypatch):
    """The frontend map panel is driven by charts[].overlay + bounds_wgs84, so
    those fields must keep flowing out of the analysis service unchanged."""
    patches = [_patch("p1", 116.20), _patch("p2", 116.21)]
    service = _service(monkeypatch, tmp_path, patches)
    result = service.analyze(
        ReportRequest(
            task="建筑物提取",
            region="北京市海淀区",
            prompt="海淀2025年12月建筑物提取",
            time_range="2025-12",
            selected_patch_ids=["p1", "p2"],
        )
    )
    layers = [c for c in result.charts if c.overlay]
    assert layers, "expected at least one map-ready overlay layer"
    for chart in layers:
        assert len(chart.bounds_wgs84) == 4
        assert chart.url


def test_remote_asset_refreshes_when_same_result_url_changes(tmp_path, monkeypatch):
    service = HaidianEmbeddingAnalysisService(
        config=EmbeddingAPIConfig(base_url="http://test"),
        report_config=ReportConfig(asset_dir=tmp_path),
    )
    request = ReportRequest(
        task="土地覆盖分类",
        region="北京市海淀区",
        prompt="检查专题结果更新",
        time_range="2026-02",
    )
    old_content = _png_bytes((245, 220, 90))
    new_content = _png_bytes((190, 170, 130))
    responses = iter([old_content, new_content, new_content])
    monkeypatch.setattr(service.http, "fetch_bytes", lambda *args, **kwargs: next(responses))

    args = (
        "/regions/haidian/patches/patch_000107/tasks/land_cover_classification/result"
        "?format=png&version=v1&month=202602",
        request,
        "land_cover_classification",
        "task_result_patch_000107",
    )
    old_path = service._download_remote_asset(*args)
    new_path = service._download_remote_asset(*args)
    repeated_new_path = service._download_remote_asset(*args)

    assert old_path != new_path
    assert new_path == repeated_new_path
    assert old_path.read_bytes() == old_content
    assert new_path.read_bytes() == new_content


def test_mosaic_url_changes_when_one_patch_result_changes(tmp_path, monkeypatch):
    service = HaidianEmbeddingAnalysisService(
        config=EmbeddingAPIConfig(base_url="http://test"),
        report_config=ReportConfig(asset_dir=tmp_path),
    )
    request = ReportRequest(
        task="土地覆盖分类",
        region="北京市海淀区",
        prompt="检查拼接结果更新",
        time_range="2026-02",
    )
    old_content = _png_bytes((245, 220, 90))
    new_content = _png_bytes((190, 170, 130))
    monkeypatch.setattr(service.http, "fetch_bytes", lambda *args, **kwargs: old_content)
    old_tile = service._download_remote_asset("/patch-107.png", request, "land_cover_classification", "p107")
    monkeypatch.setattr(service.http, "fetch_bytes", lambda *args, **kwargs: new_content)
    new_tile = service._download_remote_asset("/patch-107.png", request, "land_cover_classification", "p107")
    neighbour = _png(tmp_path / "p108.png", 1)
    south = {"bounds": [0.0, 0.0, 1280.0, 1280.0]}
    north = {"bounds": [0.0, 1280.0, 1280.0, 2560.0]}
    stats = {}

    old_mosaic = service._stitch_patches(
        [(south, old_tile, stats), (north, neighbour, stats)], "land_cover_classification"
    )
    new_mosaic = service._stitch_patches(
        [(south, new_tile, stats), (north, neighbour, stats)], "land_cover_classification"
    )

    assert old_mosaic is not None
    assert new_mosaic is not None
    assert old_mosaic != new_mosaic
