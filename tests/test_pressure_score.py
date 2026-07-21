"""Scenario C orchestration tests (PressureScoreService), HTTP stubbed."""

from __future__ import annotations

import pytest

from agent.schemas.report import ReportRequest
from agent.services.pressure_score_service import PressureScoreService

AOI = {"type": "bbox", "coordinates": [116.20, 39.88, 116.26, 39.92]}
BOUNDS = [435014.236, 4415283.021, 436294.236, 4416563.021]
WHITE = (255, 255, 255)
RED = (230, 0, 0)
GREEN = (0, 100, 0)  # 树木/林地 in the legend
LEGEND = [
    {"id": "g1", "name": "树木/林地", "rgb": (0, 100, 0)},
    {"id": "b1", "name": "建成区", "rgb": (210, 60, 60)},
]


class _FakeSearch:
    def __init__(self, patches, status="ok"):
        self.status = status
        self.patches = patches
        self.region = "北京市海淀区"
        self.region_id = "haidian"
        self.task = "building_extraction"
        self.time_range = "202512"
        self.bbox = AOI["coordinates"]
        self.selected_patch_ids = [p["patch_id"] for p in patches]
        self.message = ""


def _colors(n_white, n_other, other_rgb):
    return [(n_white, WHITE), (n_other, other_rgb)]


def _req(aoi=None, tr="2025-12"):
    return ReportRequest(task="", region="北京市海淀区", prompt="补绿优先", time_range=tr,
                         session_id="score-t", aoi=aoi if aoi is not None else AOI)


def _svc(monkeypatch, patches, building_by_patch, landcover_by_patch):
    svc = PressureScoreService()
    monkeypatch.setattr(svc.patch_selection, "search", lambda payload: _FakeSearch(patches))
    monkeypatch.setattr(svc, "_get_legend", lambda region_id: LEGEND)

    def fake_colors(region_id, patch_id, task_id, month):
        if task_id == "building_extraction":
            return building_by_patch.get(patch_id)
        return landcover_by_patch.get(patch_id)

    monkeypatch.setattr(svc.aoi_cover, "_result_colors", fake_colors)
    return svc


def test_score_ranks_high_impervious_low_green_first(monkeypatch):
    patches = [{"patch_id": "hi", "bounds": BOUNDS}, {"patch_id": "lo", "bounds": BOUNDS}]
    # hi: 80% building, ~0% green.  lo: 10% building, 80% green.
    building = {
        "hi": _colors(2000, 8000, RED),   # 80% fg
        "lo": _colors(9000, 1000, RED),   # 10% fg
    }
    landcover = {
        "hi": _colors(9500, 500, GREEN),  # 5% green
        "lo": _colors(2000, 8000, GREEN), # 80% green
    }
    svc = _svc(monkeypatch, patches, building, landcover)
    res = svc.analyze(_req())

    top = res.aef_payload["top_patches"]
    assert top[0]["patch_id"] == "hi"
    assert top[0]["rank"] == 1
    assert top[0]["score"] > top[1]["score"]
    assert res.aef_payload["scenario"] == "score"
    assert res.data_table[0]["label"].startswith("#1")


def test_score_requires_aoi(monkeypatch):
    svc = PressureScoreService()
    with pytest.raises(RuntimeError, match="AOI|框选"):
        svc.analyze(_req(aoi={}))


def test_score_skips_patches_missing_results(monkeypatch):
    patches = [{"patch_id": "p1", "bounds": BOUNDS}]
    svc = _svc(monkeypatch, patches, {"p1": None}, {"p1": _colors(100, 0, GREEN)})
    with pytest.raises(RuntimeError, match="无法|patch"):
        svc.analyze(_req())


def test_green_ratio_sums_named_classes(monkeypatch):
    svc = PressureScoreService()
    dist = [
        {"label": "树木/林地", "ratio": 0.3},
        {"label": "灌木地", "ratio": 0.1},
        {"label": "草地", "ratio": 0.1},
        {"label": "建成区", "ratio": 0.5},
    ]
    assert svc._green_ratio(dist) == 0.5  # 0.3+0.1+0.1


# ------------------------------------------------------------ intent routing

def _scenario(prompt):
    from agent.services.intent_service import IntentService

    return IntentService().parse(ReportRequest(task="", region="北京市海淀区", prompt=prompt)).scenario


def test_score_cues_route_to_score():
    for p in ["这片区哪里最该绿化", "做个补绿优先区排序", "高硬化低绿地压力评分", "哪些地方最缺绿"]:
        assert _scenario(p) == "score", p


def test_score_does_not_swallow_other_scenarios():
    assert _scenario("帮我做个片区综合体检") == "checkup"
    assert _scenario("对比2025-12和2026-05的变化") == "change"
    assert _scenario("生成建筑物提取报告") == ""
