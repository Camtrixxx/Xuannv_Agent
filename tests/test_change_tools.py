"""Tests for change-detection tools (scenario B)."""

from __future__ import annotations

import numpy as np
import pytest

from agent.tools.change import (
    CHANGE_GAINED_RGBA,
    CHANGE_LOST_RGBA,
    aggregate_change,
    binary_change,
    change_rgba,
    foreground_mask,
    mask_for_task,
)

WHITE = (255, 255, 255)
RED = (230, 0, 0)


def _img(fill, size=4):
    arr = np.zeros((size, size, 3), dtype=np.uint8)
    arr[:, :] = fill
    return arr


def test_foreground_mask_ignores_near_background():
    arr = _img(WHITE)
    arr[0, 0] = RED  # one clear foreground pixel
    arr[0, 1] = (250, 250, 250)  # near-white → still background
    mask = foreground_mask(arr, WHITE)
    assert mask.sum() == 1
    assert mask[0, 0]


def test_mask_for_task_none_for_multiclass():
    assert mask_for_task(_img(WHITE), "land_cover_classification") is None
    assert mask_for_task(_img(WHITE), "building_extraction") is not None


def test_binary_change_gained_lost_stayed():
    # before: left half foreground; after: right half foreground.
    before = np.zeros((4, 4), dtype=bool)
    before[:, :2] = True  # 8 px
    after = np.zeros((4, 4), dtype=bool)
    after[:, 2:] = True  # 8 px, disjoint
    out = binary_change(before, after, total_area_ha=16.0)  # 16px → 1ha/px
    assert out["before_px"] == 8 and out["after_px"] == 8
    assert out["gained_px"] == 8 and out["lost_px"] == 8 and out["stayed_px"] == 0
    assert out["net_px"] == 0
    assert out["gained_ha"] == 8.0 and out["lost_ha"] == 8.0
    assert out["net_ha"] == 0.0


def test_binary_change_monotonic_growth():
    before = np.zeros((10, 10), dtype=bool)
    before[:5, :] = True  # 50 px
    after = before.copy()
    after[5, :] = True  # +10 px
    out = binary_change(before, after, total_area_ha=100.0)  # 1ha/px
    assert out["gained_px"] == 10 and out["lost_px"] == 0
    assert out["net_px"] == 10 and out["net_ha"] == 10.0


def test_binary_change_no_area_when_bounds_missing():
    m = np.zeros((4, 4), dtype=bool)
    out = binary_change(m, m, total_area_ha=None)
    assert out["gained_ha"] is None and out["net_ha"] is None


def test_binary_change_shape_mismatch_raises():
    with pytest.raises(ValueError, match="shape mismatch"):
        binary_change(np.zeros((4, 4), dtype=bool), np.zeros((4, 5), dtype=bool), total_area_ha=1.0)


def test_aggregate_change_sums_and_reweights():
    p1 = {"before_ha": 40.0, "after_ha": 50.0, "gained_ha": 12.0, "lost_ha": 2.0}
    p2 = {"before_ha": 60.0, "after_ha": 66.0, "gained_ha": 8.0, "lost_ha": 2.0}
    out = aggregate_change([p1, p2])
    assert out["patch_count"] == 2
    assert out["before_area_ha"] == 100.0 and out["after_area_ha"] == 116.0
    assert out["gained_area_ha"] == 20.0 and out["lost_area_ha"] == 4.0
    assert out["net_area_ha"] == 16.0
    assert out["growth_ratio"] == round(16.0 / 100.0, 4)


def test_aggregate_change_empty():
    out = aggregate_change([])
    assert out["patch_count"] == 0
    assert out["net_area_ha"] is None
    assert out["growth_ratio"] is None


def test_change_rgba_colours_gained_and_lost():
    # before: left half foreground; after: right half foreground (disjoint).
    before = np.zeros((4, 4), dtype=bool)
    before[:, :2] = True
    after = np.zeros((4, 4), dtype=bool)
    after[:, 2:] = True
    rgba = change_rgba(before, after)
    assert rgba.shape == (4, 4, 4)
    # Gained (0→1) on the right → red; lost (1→0) on the left → blue.
    assert tuple(rgba[0, 3]) == CHANGE_GAINED_RGBA
    assert tuple(rgba[0, 0]) == CHANGE_LOST_RGBA


def test_change_rgba_unchanged_is_transparent():
    m = np.zeros((4, 4), dtype=bool)
    m[0, 0] = True  # stays foreground both dates
    rgba = change_rgba(m, m)
    # Every pixel unchanged → fully transparent (alpha 0) so basemap shows through.
    assert int(rgba[..., 3].max()) == 0


def test_change_rgba_shape_mismatch_raises():
    with pytest.raises(ValueError, match="shape mismatch"):
        change_rgba(np.zeros((4, 4), dtype=bool), np.zeros((4, 5), dtype=bool))
