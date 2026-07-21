"""Tests for AOI coverage: pure aggregation tool + service (mocked HTTP)."""

from __future__ import annotations

import io

from PIL import Image

from agent.services.aoi_cover_service import AoiCoverService
from agent.services.patch_selection_service import PatchSearchResult
from agent.tools.aoi import aggregate_binary_coverage, aggregate_class_distribution

WHITE = (255, 255, 255)
RED = (230, 0, 0)
# 1280m x 1280m UTM patch = 163.84 ha.
BOUNDS = [435014.236, 4415283.021, 436294.236, 4416563.021]


# ----------------------------------------------------------------- pure tool

def test_aggregate_is_area_weighted_not_mean_of_ratios():
    # Patch A: 10% of 163.84 ha; Patch B: 50% of 163.84 ha.
    rows = [
        {"foreground_ratio": 0.10, "total_area_ha": 163.84, "covered_area_ha": 16.38},
        {"foreground_ratio": 0.50, "total_area_ha": 163.84, "covered_area_ha": 81.92},
    ]
    out = aggregate_binary_coverage(rows)
    assert out["patch_count"] == 2
    assert out["total_area_ha"] == 327.68
    assert out["covered_area_ha"] == round(16.38 + 81.92, 2)
    # area-weighted ratio = 98.30 / 327.68 ≈ 0.30 (mean of ratios would be 0.30 too here,
    # but the point is it divides summed areas, not averaging ratios)
    assert out["coverage_ratio"] == round((16.38 + 81.92) / 327.68, 4)


def test_aggregate_excludes_area_missing_rows_from_totals():
    rows = [
        {"foreground_ratio": 0.20, "total_area_ha": 163.84, "covered_area_ha": 32.77},
        {"foreground_ratio": 0.20, "total_area_ha": None, "covered_area_ha": None},
    ]
    out = aggregate_binary_coverage(rows)
    assert out["patch_count"] == 2
    assert out["area_patch_count"] == 1
    assert out["total_area_ha"] == 163.84


def test_aggregate_empty():
    out = aggregate_binary_coverage([])
    assert out["patch_count"] == 0
    assert out["coverage_ratio"] is None
    assert out["total_area_ha"] == 0.0


# --------------------------------------------- class distribution aggregation

def test_aggregate_class_distribution_sums_ha_and_reweights():
    # Patch A: 树木 100ha + 建成区 60ha; Patch B: 树木 40ha + 水体 40ha. Total 240ha.
    patch_a = [
        {"label": "树木覆盖", "class_id": "t", "ratio": 0.625, "value": 100.0},
        {"label": "建成区", "class_id": "b", "ratio": 0.375, "value": 60.0},
    ]
    patch_b = [
        {"label": "树木覆盖", "class_id": "t", "ratio": 0.5, "value": 40.0},
        {"label": "永久性水体", "class_id": "w", "ratio": 0.5, "value": 40.0},
    ]
    out = aggregate_class_distribution([patch_a, patch_b])
    by = {r["class_id"]: r for r in out}
    assert by["t"]["value"] == 140.0  # 100 + 40
    assert by["t"]["ratio"] == round(140.0 / 240.0, 4)
    assert by["b"]["value"] == 60.0
    assert out[0]["class_id"] == "t"  # sorted by share desc
    assert round(sum(r["ratio"] for r in out), 4) == 1.0


def test_aggregate_class_distribution_skips_rows_without_ha():
    patch_a = [{"label": "树木覆盖", "class_id": "t", "ratio": 1.0}]  # no value
    patch_b = [{"label": "建成区", "class_id": "b", "ratio": 1.0, "value": 50.0}]
    out = aggregate_class_distribution([patch_a, patch_b])
    assert len(out) == 1
    assert out[0]["class_id"] == "b"


def test_aggregate_class_distribution_empty():
    assert aggregate_class_distribution([]) == []
    assert aggregate_class_distribution([[], []]) == []


# ----------------------------------------------------------------- service

def _png_bytes(fg_fraction: float, size: int = 32) -> bytes:
    """A flat binary result PNG: fg_fraction red on white."""
    im = Image.new("RGB", (size, size), WHITE)
    px = im.load()
    fg_cols = int(round(size * fg_fraction))
    for x in range(fg_cols):
        for y in range(size):
            px[x, y] = RED
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _service_with_stubs(monkeypatch, patches, fg_by_patch, task="building_extraction"):
    svc = AoiCoverService()

    def fake_search(payload):
        return PatchSearchResult(
            status="ok", region="北京市海淀区", region_id="haidian", task=task,
            time_range="2025-12", bbox=[116.2, 39.9, 116.25, 39.95],
            patches=patches, selected_patch_ids=[p["patch_id"] for p in patches],
            message="",
        )

    monkeypatch.setattr(svc.patch_selection, "search", fake_search)

    def fake_fetch(url, asset_label):
        for pid, frac in fg_by_patch.items():
            if pid in url:
                return _png_bytes(frac)
        raise RuntimeError("not found")

    monkeypatch.setattr(svc.http, "fetch_bytes", fake_fetch)
    return svc


def test_service_aggregates_two_patches(monkeypatch):
    patches = [
        {"patch_id": "patch_A", "bounds": BOUNDS},
        {"patch_id": "patch_B", "bounds": BOUNDS},
    ]
    svc = _service_with_stubs(monkeypatch, patches, {"patch_A": 0.25, "patch_B": 0.75})
    out = svc.analyze({"region": "北京市海淀区", "task": "building_extraction", "time_range": "2025-12", "bbox": [1, 2, 3, 4]})
    assert out.status == "ok"
    assert out.summary["patch_count"] == 2
    assert out.summary["total_area_ha"] == round(163.84 * 2, 2)
    # 0.25 + 0.75 halves → area-weighted 0.50
    assert out.summary["coverage_ratio"] == 0.5
    assert "建筑物提取覆盖约 50" in out.message


def test_service_rejects_multiclass_task(monkeypatch):
    svc = _service_with_stubs(monkeypatch, [{"patch_id": "p", "bounds": BOUNDS}], {"p": 0.1}, task="land_cover_classification")
    out = svc.analyze({"region": "北京市海淀区", "task": "land_cover_classification", "time_range": "2025-12", "bbox": [1, 2, 3, 4]})
    assert out.status == "unsupported"
    assert "官方图例" in out.message


def test_service_skips_unfetchable_patches(monkeypatch):
    patches = [
        {"patch_id": "patch_ok", "bounds": BOUNDS},
        {"patch_id": "patch_bad", "bounds": BOUNDS},
    ]
    svc = _service_with_stubs(monkeypatch, patches, {"patch_ok": 0.25})  # patch_bad raises; 8/32=0.25 exact
    out = svc.analyze({"region": "北京市海淀区", "task": "building_extraction", "time_range": "2025-12", "bbox": [1, 2, 3, 4]})
    assert out.summary["patch_count"] == 1  # bad one skipped
    assert out.summary["coverage_ratio"] == 0.25
