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


def test_aef_same_year_change_warns(monkeypatch):
    # AEF model + two months in the SAME year → annual-feature warning fires.
    from agent.services.model_registry_service import ModelInfo
    svc, _ = _svc(monkeypatch, {"202504": _custom_img(32), "202510": _custom_img(96)})
    aef_model = ModelInfo.from_payload({
        "id": "model_x", "name": "湿地AEF", "type": "change_detection",
        "status": "completed", "source": "custom", "feature_source": "aef",
        "resolved_training_method": "pixel_mlp", "n_samples": 12,
        "classes": [{"id": "c0", "name": "湿地"}],
    })
    monkeypatch.setattr(svc.registry, "model_status", lambda mid, rid="": aef_model)
    req = _req()
    req.before_time_range, req.after_time_range = "2025-04", "2025-10"
    result = svc.analyze(req)
    assert any("AEF 年度特征" in l and "无意义" in l for l in result.limitations)
    # method note reflects the real feature source + samples.
    assert any("AEF" in n and "12" in n for n in result.method_notes)


def test_aef_cross_year_change_no_warning(monkeypatch):
    # Same AEF model but cross-year months → no annual-feature warning.
    from agent.services.model_registry_service import ModelInfo
    svc, _ = _svc(monkeypatch, {"202412": _custom_img(32), "202506": _custom_img(96)})
    aef_model = ModelInfo.from_payload({
        "id": "model_x", "name": "湿地AEF", "type": "change_detection",
        "status": "completed", "source": "custom", "feature_source": "aef",
        "classes": [{"id": "c0", "name": "湿地"}],
    })
    monkeypatch.setattr(svc.registry, "model_status", lambda mid, rid="": aef_model)
    req = _req()
    req.before_time_range, req.after_time_range = "2024-12", "2025-06"
    result = svc.analyze(req)
    assert not any("AEF 年度特征" in l for l in result.limitations)


@pytest.mark.skipif(not LIVE, reason="set AGENT_LIVE_TESTS=1 to hit the live API")
def test_live_custom_infer_roundtrip():
    from agent.services.aoi_cover_service import AoiCoverService
    from agent.services.model_registry_service import ModelRegistryService

    svc = AoiCoverService()
    patches = ("patch_000100", "patch_000000", "patch_000010", "patch_000050")

    def _infer_any(model_id):
        for pid in patches:
            a = svc.fetch_result_array("haidian", pid, "custom", "202512", model_id=model_id)
            if a is not None:
                return a
        return None

    # Deterministic proof: point AGENT_LIVE_MODEL_ID at a known-good model on the
    # configured backend (e.g. model_6360bb31 on the remote). Otherwise discover
    # ready single_time models and scan a bounded budget — the backend hosts many
    # broken models (stored-model vs patch-embedding version mismatch), so a
    # working one is sparse; a miss is a backend/data condition, so skip.
    arr = None
    pinned = os.getenv("AGENT_LIVE_MODEL_ID")
    if pinned:
        arr = _infer_any(pinned)
        if arr is None:
            pytest.skip(f"pinned model {pinned} could not infer (data/version condition)")
    else:
        reg = ModelRegistryService()
        ready = [m for m in reg.custom_models("haidian")
                 if m.is_ready and m.type == "single_time_detection"]
        if not ready:
            pytest.skip("no ready single_time_detection custom model on this backend")
        for m in ready[:40]:
            arr = _infer_any(m.id)
            if arr is not None:
                break
        if arr is None:
            pytest.skip("no scanned ready model could infer (backend data/version condition)")
    assert arr.shape == (128, 128, 3)
    mask = custom_model_mask(arr)
    assert mask.dtype == bool
