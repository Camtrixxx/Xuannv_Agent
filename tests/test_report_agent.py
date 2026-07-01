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


def test_used_patch_ids_empty():
    assert ReportAgent._used_patch_ids(SimpleNamespace(aef_payload={})) == []


def test_has_bbox():
    assert ReportAgent._has_bbox({"type": "bbox", "coordinates": [1, 2, 3, 4]})
    assert not ReportAgent._has_bbox({})
    assert not ReportAgent._has_bbox({"type": "bbox", "coordinates": [1, 2]})
    assert not ReportAgent._has_bbox({"name": "雅江区域"})
