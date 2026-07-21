"""Phase 3: custom-model change monitoring (infer stubbed) + live smoke.

Verifies that a ready custom model flows through the same scenario-B pixel diff
as native tasks: infer PNGs (target=class colour, background grey) → foreground
masks → gained/lost/net, with honest custom-model method notes/limitations.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from agent.schemas.report import ReportRequest
from agent.services.change_monitor_service import ChangeMonitorService
from agent.tools.change import CUSTOM_MODEL_BACKGROUND, custom_model_mask

LIVE = os.getenv("AGENT_LIVE_TESTS") == "1"
AOI = {"type": "bbox", "coordinates": [116.20, 39.88, 116.26, 39.92]}
BOUNDS = [435014.236, 4415283.021, 436294.236, 4416563.021]  # 163.84 ha
CLASS_COLOR = (32, 190, 218)
GREY = CUSTOM_MODEL_BACKGROUND


class _FakeSearch:
    def __init__(self, patches):
        self.status = "ok"
        self.patches = patches
        self.region = "北京市海淀区"
        self.region_id = "haidian"
        self.task = "custom"
        self.time_range = "202512"
        self.bbox = AOI["coordinates"]
        self.selected_patch_ids = [p["patch_id"] for p in patches]
        self.message = ""


def _custom_img(fg_rows, size=128):
    """Grey background with the first ``fg_rows`` rows painted the class colour."""
    arr = np.full((size, size, 3), GREY, dtype=np.uint8)
    if fg_rows:
        arr[:fg_rows, :] = CLASS_COLOR
    return arr


def test_custom_model_mask_uses_grey_background():
    arr = _custom_img(64)
    mask = custom_model_mask(arr)
    assert mask[:64].all() and not mask[64:].any()


def _svc(monkeypatch, arrays_by_month):
    svc = ChangeMonitorService()
    # Keep offline: basemap_chart fetches ArcGIS tiles otherwise.
    monkeypatch.setattr("agent.services.change_monitor_service.basemap_chart", lambda *a, **k: None)
    monkeypatch.setattr(svc.patch_selection, "search", lambda payload: _FakeSearch([{"patch_id": "patch_A", "bounds": BOUNDS}]))
    calls = {"model_ids": []}

    def fake_fetch(region_id, patch_id, task_id, month, model_id=""):
        calls["model_ids"].append(model_id)
        return arrays_by_month.get(month)

    monkeypatch.setattr(svc.aoi_cover, "fetch_result_array", fake_fetch)
    return svc, calls


def _req(model_id="model_x", obj="湿地"):
    return ReportRequest(
        task="", region="北京市海淀区", prompt="对比湿地变化", time_range="",
        session_id="cc-t", aoi=AOI, before_time_range="2025-12", after_time_range="2026-05",
        custom_model_id=model_id, target_object=obj,
    )


def test_custom_change_growth(monkeypatch):
    # before: 32 rows target; after: 96 rows target → net gain.
    svc, calls = _svc(monkeypatch, {"202512": _custom_img(32), "202605": _custom_img(96)})
    result = svc.analyze(_req())
    # infer was driven by the custom model id, not the system task path.
    assert set(calls["model_ids"]) == {"model_x"}
    assert result.aef_payload["custom_model_id"] == "model_x"
    assert result.aef_payload["custom_class"] == "湿地"
    # net area positive (grew from 25% to 75% of 163.84 ha).
    assert result.aef_payload["aggregate"]["net_area_ha"] > 0
    # honest custom-model framing.
    assert "湿地" in result.headline
    assert any("自定义模型" in n for n in result.method_notes)
    assert any("精度仅供参考" in l for l in result.limitations)


def test_custom_change_needs_two_dates(monkeypatch):
    svc, _ = _svc(monkeypatch, {"202512": _custom_img(10)})
    req = _req()
    req.after_time_range = "2025-12"  # same month
    with pytest.raises(RuntimeError):
        svc.analyze(req)


@pytest.mark.skipif(not LIVE, reason="set AGENT_LIVE_TESTS=1 to hit the live API")
def test_live_custom_infer_roundtrip():
    from agent.services.aoi_cover_service import AoiCoverService

    svc = AoiCoverService()
    # model_6360bb31 is a ready single_time_detection custom model (verified).
    arr = svc.fetch_result_array("haidian", "patch_000100", "custom", "202512", model_id="model_6360bb31")
    assert arr is not None and arr.shape == (128, 128, 3)
    mask = custom_model_mask(arr)
    assert mask.dtype == bool
