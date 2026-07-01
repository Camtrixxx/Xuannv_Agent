"""Unit tests for frontend patch-id -> AEF sample index mapping."""

from __future__ import annotations

from agent.services.aef_analysis_service import MockPatchSelector


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
