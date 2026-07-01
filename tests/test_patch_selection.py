"""Unit tests for frontend patch-id -> AEF sample index mapping."""

from __future__ import annotations

from agent.services.aef_analysis_service import MockPatchSelector
from agent.services.patch_selection_service import PatchSelectionService
from agent.services.yajiang_patch_index_service import YajiangPatchIndexService


def _indices(patch_ids, count):
    return MockPatchSelector()._selected_patch_indices(patch_ids, count)


def test_patch_prefix_stripped_to_int():
    assert _indices(["patch_000040"], 1) == [40]


def test_plain_integer_id():
    assert _indices(["7"], 1) == [7]


def test_count_limits_results():
    assert _indices(["patch_000040", "patch_000041", "patch_000042"], 2) == [40, 41]


def test_invalid_ids_skipped():
    assert _indices(["patch_abc", "not_a_patch"], 1) == []


def test_negative_index_skipped():
    assert _indices(["patch_-3", "patch_000005"], 1) == [5]


def test_empty_selection():
    assert _indices([], 1) == []


def test_haidian_patch_search_allows_empty_task():
    service = PatchSelectionService()
    patch = {
        "has_embedding": True,
        "available_tasks": ["building_extraction"],
    }
    assert service._task_available("haidian", patch, "") is True


def test_yajiang_patch_index_allows_empty_task():
    service = YajiangPatchIndexService()
    service._patches = [
        {
            "patch_id": "patch_000001",
            "sample_index": 1,
            "bounds_wgs84": [116.0, 39.0, 117.0, 40.0],
            "available_months": ["2025-12"],
            "available_tasks": ["landcover"],
        }
    ]
    rows = service.search([116.1, 39.1, 116.2, 39.2], task_id="", time_range="2025-12", limit=5)
    assert [row["patch_id"] for row in rows] == ["patch_000001"]
