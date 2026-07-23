"""Scenario B orchestration tests (ChangeMonitorService), HTTP stubbed."""

from __future__ import annotations

import numpy as np
import pytest

from agent.schemas.report import ReportRequest
from agent.services.change_monitor_service import ChangeMonitorService

AOI = {"type": "bbox", "coordinates": [116.20, 39.88, 116.26, 39.92]}
BOUNDS = [435014.236, 4415283.021, 436294.236, 4416563.021]  # 163.84 ha
WHITE = (255, 255, 255)
RED = (230, 0, 0)


class _FakeSearch:
    def __init__(self, patches, status="ok"):
        self.status = status
        self.patches = patches
        self.region = "北京市海淀区"
        self.region_id = "haidian"
        self.task = "construction"
        self.time_range = "202512"
        self.bbox = AOI["coordinates"]
        self.selected_patch_ids = [p["patch_id"] for p in patches]
        self.message = ""


def _img(fg_rows, size=128):
    """White image with the first ``fg_rows`` rows painted red (foreground)."""
    arr = np.full((size, size, 3), 255, dtype=np.uint8)
    if fg_rows:
        arr[:fg_rows, :] = RED
    return arr


def _req(before="2025-12", after="2026-05", task="施工识别", aoi=None):
    return ReportRequest(
        task=task, region="北京市海淀区", prompt="变化", time_range="",
        session_id="chg-t", aoi=aoi if aoi is not None else AOI,
        before_time_range=before, after_time_range=after,
    )


def _svc_with(monkeypatch, patches, arrays_by_month):
    svc = ChangeMonitorService()
    monkeypatch.setattr(svc.patch_selection, "search", lambda payload: _FakeSearch(patches))

    def fake_fetch(region_id, patch_id, task_id, month, model_id=""):
        return arrays_by_month.get(month)

    monkeypatch.setattr(svc.aoi_cover, "fetch_result_array", fake_fetch)
    return svc


def test_change_reports_growth(monkeypatch):
    patches = [{"patch_id": "p1", "bounds": BOUNDS}, {"patch_id": "p2", "bounds": BOUNDS}]
    # before: 10 rows fg; after: 30 rows fg → monotonic growth.
    arrays = {"202512": _img(10), "202605": _img(30)}
    svc = _svc_with(monkeypatch, patches, arrays)
    res = svc.analyze(_req())

    assert res.aef_payload["scenario"] == "change"
    agg = res.aef_payload["aggregate"]
    assert agg["patch_count"] == 2
    assert agg["gained_area_ha"] > 0
    assert agg["lost_area_ha"] == 0.0
    assert agg["net_area_ha"] > 0
    labels = {m.label for m in res.metrics}
    assert any("净变化" in l for l in labels)


def test_change_merges_patches_into_one_overlay(monkeypatch, tmp_path):
    # Multiple patches on the same UTM grid stitch into ONE mosaic overlay so
    # the report map shows a single toggle and the body a single figure (like
    # road/building reports), not one layer per patch.
    # Two adjacent tiles stacked north/south so they form a real 2-tile grid.
    south = [435014.236, 4415283.021, 436294.236, 4416563.021]
    north = [435014.236, 4416563.021, 436294.236, 4417843.021]
    swgs = [116.20, 39.88, 116.26, 39.90]
    nwgs = [116.20, 39.90, 116.26, 39.92]
    patches = [
        {"patch_id": "p1", "bounds": south, "bounds_wgs84": swgs},
        {"patch_id": "p2", "bounds": north, "bounds_wgs84": nwgs},
    ]
    arrays = {"202512": _img(10), "202605": _img(30)}
    svc = _svc_with(monkeypatch, patches, arrays)
    svc.asset_dir = tmp_path
    res = svc.analyze(_req())

    overlays = [c for c in res.charts if getattr(c, "overlay", False)]
    assert len(overlays) == 1
    mosaic = overlays[0]
    assert mosaic.patch_id == "p1,p2"
    # Union WGS84 bounds span both tiles.
    assert mosaic.bounds_wgs84 == [116.20, 39.88, 116.26, 39.92]
    assert (tmp_path / mosaic.url.rsplit("/", 1)[-1]).exists()


def test_change_single_patch_overlay(monkeypatch, tmp_path):
    # A single patch can't be stitched → one per-patch overlay, still on the map.
    wgs = [116.20, 39.88, 116.26, 39.92]
    patches = [{"patch_id": "p1", "bounds": BOUNDS, "bounds_wgs84": wgs}]
    arrays = {"202512": _img(10), "202605": _img(30)}
    svc = _svc_with(monkeypatch, patches, arrays)
    svc.asset_dir = tmp_path
    res = svc.analyze(_req())

    overlays = [c for c in res.charts if getattr(c, "overlay", False)]
    assert len(overlays) == 1
    assert overlays[0].bounds_wgs84 == wgs
    assert (tmp_path / overlays[0].url.rsplit("/", 1)[-1]).exists()


def test_change_default_task_when_unsupported(monkeypatch):
    patches = [{"patch_id": "p1", "bounds": BOUNDS}]
    arrays = {"202512": _img(5), "202605": _img(5)}
    svc = _svc_with(monkeypatch, patches, arrays)
    res = svc.analyze(_req(task="土地覆盖分类"))  # not a change task → default construction
    assert res.aef_payload["task_id"] == "construction"


def test_change_requires_two_distinct_months(monkeypatch):
    svc = _svc_with(monkeypatch, [{"patch_id": "p1", "bounds": BOUNDS}], {"202512": _img(5)})
    with pytest.raises(RuntimeError, match="两个不同的月份|两个"):
        svc.analyze(_req(before="2025-12", after="2025-12"))


def test_change_requires_aoi(monkeypatch):
    svc = _svc_with(monkeypatch, [{"patch_id": "p1", "bounds": BOUNDS}], {"202512": _img(5), "202605": _img(9)})
    with pytest.raises(RuntimeError, match="AOI|框选"):
        svc.analyze(_req(aoi={}))


def test_change_skips_patches_missing_a_date(monkeypatch):
    patches = [{"patch_id": "p1", "bounds": BOUNDS}, {"patch_id": "p2", "bounds": BOUNDS}]
    # Only 'before' available → every patch skipped → no data raises.
    arrays = {"202512": _img(10)}
    svc = _svc_with(monkeypatch, patches, arrays)
    with pytest.raises(RuntimeError, match="无法|patch"):
        svc.analyze(_req())


# ------------------------------------------------------------ intent routing

def _scenario(prompt):
    from agent.services.intent_service import IntentService

    return IntentService().parse(ReportRequest(task="", region="北京市海淀区", prompt=prompt)).scenario


def test_change_cues_route_to_change():
    for p in ["监测一下建设扰动", "前后对比一下建筑扩张", "对比2025-12和2026-05的变化", "两期对比"]:
        assert _scenario(p) == "change", p


def test_evaluative_questions_stay_discussion():
    # Questioning an existing result must not trigger a new change report.
    for p in ["这个对比准不准", "变化结论靠谱吗", "这个监测怎么样"]:
        assert _scenario(p) == "", p


def test_change_wins_over_checkup_on_two_date_intent():
    # A change cue beats a generic checkup cue when both could match.
    assert _scenario("综合对比一下两期变化") == "change"
