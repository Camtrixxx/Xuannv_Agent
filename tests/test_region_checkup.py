"""Scenario A (片区综合体检): intent detection, routing, and orchestration.

Live HTTP is stubbed — these lock the agent wiring, not the upstream model.
"""

from __future__ import annotations

from agent.schemas.report import ReportRequest
from agent.services.intent_service import IntentService
from agent.services.region_checkup_service import RegionCheckupService

AOI = {"type": "bbox", "coordinates": [116.20, 39.88, 116.26, 39.92]}
BOUNDS = [435014.236, 4415283.021, 436294.236, 4416563.021]  # 163.84 ha


# ------------------------------------------------------------ intent detection

def _intent(prompt, task="", aoi=None):
    # Force rules-first (no LLM) by using a high-confidence report phrasing path.
    isvc = IntentService()
    return isvc.parse(ReportRequest(task=task, region="北京市海淀区", prompt=prompt, aoi=aoi or {}))


def test_checkup_cue_sets_scenario():
    assert _intent("帮我做个片区综合体检").scenario == "checkup"
    assert _intent("这个区域整体评估一下").scenario == "checkup"


def test_ordinary_report_has_no_scenario():
    assert _intent("生成建筑物提取报告", task="建筑物提取").scenario == ""


def test_free_chat_never_checkup():
    # A greeting must not be hijacked into a checkup even if it says "体检".
    it = _intent("你好呀")
    assert it.scenario == ""


# ------------------------------------------------------------ orchestration

class _FakeSearch:
    def __init__(self, patches, status="ok"):
        self.status = status
        self.patches = patches
        self.region = "北京市海淀区"
        self.region_id = "haidian"
        self.task = "building_extraction"
        self.time_range = "2025-12"
        self.bbox = AOI["coordinates"]
        self.selected_patch_ids = [p["patch_id"] for p in patches]
        self.message = ""


def _checkup_with_stubs(monkeypatch, patches, colors_by_task, legend):
    svc = RegionCheckupService()
    monkeypatch.setattr(svc.patch_selection, "search", lambda payload: _FakeSearch(patches))
    monkeypatch.setattr(svc, "_legend", lambda region_id, task_id: legend)

    def fake_iter(region_id, task_id, month, patches_arg):
        counts = colors_by_task.get(task_id, [])
        for p in patches_arg:
            yield p, counts

    monkeypatch.setattr(svc.aoi_cover, "iter_patch_colors", fake_iter)
    return svc


def test_checkup_aggregates_metrics_and_table(monkeypatch):
    patches = [{"patch_id": "p1", "bounds": BOUNDS}, {"patch_id": "p2", "bounds": BOUNDS}]
    # building 50% (white bg + 50% red), water 0%, land_cover all built-up.
    colors = {
        "building_extraction": [(8192, (255, 255, 255)), (8192, (230, 0, 0))],
        "road_extraction": [(16384, (255, 255, 255))],
        "water_extraction": [(16384, (0, 0, 0))],
        "construction": [(16384, (0, 0, 0))],
        "land_cover_classification": [(16384, (190, 170, 130))],
    }
    legend = [{"id": "lc5", "name": "建成区", "rgb": (190, 170, 130)}]
    svc = _checkup_with_stubs(monkeypatch, patches, colors, legend)
    res = svc.analyze(ReportRequest(task="片区综合体检", region="北京市海淀区", prompt="体检", time_range="2025-12", aoi=AOI))

    labels = {m.label: m.value for m in res.metrics}
    assert "体检片区面积" in labels
    assert "建筑物提取覆盖率" in labels
    assert res.data_table_title.startswith("土地覆盖")
    assert res.data_table[0]["label"] == "建成区"
    assert res.aef_payload["scenario"] == "checkup"


def test_checkup_requires_aoi(monkeypatch):
    svc = RegionCheckupService()
    import pytest

    with pytest.raises(RuntimeError, match="AOI|框选"):
        svc.analyze(ReportRequest(task="片区综合体检", region="北京市海淀区", prompt="体检", time_range="2025-12", aoi={}))


def test_checkup_no_patches_raises(monkeypatch):
    svc = RegionCheckupService()
    monkeypatch.setattr(svc.patch_selection, "search", lambda payload: _FakeSearch([], status="ok"))
    import pytest

    with pytest.raises(RuntimeError, match="patch"):
        svc.analyze(ReportRequest(task="片区综合体检", region="北京市海淀区", prompt="体检", time_range="2025-12", aoi=AOI))
