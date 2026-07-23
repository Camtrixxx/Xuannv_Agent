"""Unit tests for ReportAgent patch-carryover helpers (pure, no network)."""

from __future__ import annotations

from types import SimpleNamespace

from agent.graph.report_agent import ReportAgent


def test_used_patch_ids_from_patch_dict():
    analysis = SimpleNamespace(aef_payload={"patch": {"patch_id": "patch_000009"}})
    assert ReportAgent._used_patch_ids(analysis) == ["patch_000009"]


def test_used_patch_ids_from_sample_indices():
    analysis = SimpleNamespace(aef_payload={"sample_indices": [431]})
    assert ReportAgent._used_patch_ids(analysis) == ["patch_000431"]


def test_used_patch_ids_from_selected_ids():
    analysis = SimpleNamespace(aef_payload={"selected_patch_ids": ["patch_000002"]})
    assert ReportAgent._used_patch_ids(analysis) == ["patch_000002"]


def test_used_patch_ids_prefers_successful_multi_patch_results():
    analysis = SimpleNamespace(
        aef_payload={
            "requested_patch_ids": ["p1", "p2"],
            "used_patch_ids": ["p1", "p2"],
            "failed_patch_ids": ["p3"],
        }
    )
    assert ReportAgent._used_patch_ids(analysis) == ["p1", "p2"]


def test_used_patch_ids_empty():
    assert ReportAgent._used_patch_ids(SimpleNamespace(aef_payload={})) == []


def test_has_bbox():
    assert ReportAgent._has_bbox({"type": "bbox", "coordinates": [1, 2, 3, 4]})
    assert not ReportAgent._has_bbox({})
    assert not ReportAgent._has_bbox({"type": "bbox", "coordinates": [1, 2]})
    assert not ReportAgent._has_bbox({"name": "雅江区域"})


def test_detect_target_object_ignores_region_name_substring():
    # "雅江" must not trip the "江"→河流 non-native alias; a real object still fires.
    # (The actual stripping lives in taxonomy.non_native_object — this guards the
    # graph's delegation to it.)
    assert ReportAgent._detect_target_object(
        SimpleNamespace(user_prompt="雅江2025年6月的地物分类")
    ) == ""
    assert ReportAgent._detect_target_object(
        SimpleNamespace(user_prompt="雅江的水体分布")
    ) == ""
    assert ReportAgent._detect_target_object(
        SimpleNamespace(user_prompt="帮我在海淀这块地识别一下湿地")
    ) == "湿地"
    assert ReportAgent._detect_target_object(
        SimpleNamespace(user_prompt="看看海淀的河流")
    ) == "河流"
