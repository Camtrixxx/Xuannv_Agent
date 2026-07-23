"""Tests for the multiclass legend tool (class_map)."""

from __future__ import annotations

import numpy as np

from agent.tools.classmap import class_distribution, class_mask, hex_to_rgb, normalize_legend

# Real Haidian land_cover legend subset.
RAW_LEGEND = [
    {"id": "sys_lc_1", "name": "树木覆盖", "color": "#006400"},
    {"id": "sys_lc_5", "name": "建成区", "color": "#BEAA82"},
    {"id": "sys_lc_8", "name": "永久性水体", "color": "#1E64DC"},
]
LEGEND = normalize_legend(RAW_LEGEND)

TREE = (0, 100, 0)
BUILT = (190, 170, 130)
WATER = (30, 100, 220)
# 1280m x 1280m UTM patch = 163.84 ha.
BOUNDS = [435014.236, 4415283.021, 436294.236, 4416563.021]


def test_hex_to_rgb():
    assert hex_to_rgb("#1E64DC") == (30, 100, 220)
    assert hex_to_rgb("BEAA82") == (190, 170, 130)


def test_normalize_skips_malformed():
    out = normalize_legend([{"id": "a", "name": "x", "color": "#006400"}, {"id": "b", "color": None}, {"id": "c", "color": "#zz"}])
    assert len(out) == 1
    assert out[0]["rgb"] == (0, 100, 0)


def test_distribution_shares_and_area():
    counts = [(8192, BUILT), (4096, TREE), (4096, WATER)]  # 50/25/25 of 16384
    rows = class_distribution(counts, LEGEND, BOUNDS)
    by_label = {r["label"]: r for r in rows}
    assert by_label["建成区"]["ratio"] == 0.5
    assert by_label["树木覆盖"]["ratio"] == 0.25
    assert by_label["建成区"]["value"] == round(163.84 * 0.5, 2)
    # sorted by share desc
    assert rows[0]["label"] == "建成区"


def test_shares_sum_to_one_with_unknown_bucket():
    unknown_color = (12, 200, 30)  # far from every legend colour
    counts = [(8000, BUILT), (8384, unknown_color)]
    rows = class_distribution(counts, LEGEND, BOUNDS)
    labels = {r["label"] for r in rows}
    assert "其他/未识别" in labels
    assert round(sum(r["ratio"] for r in rows), 4) == 1.0


def test_antialiasing_pixel_snaps_to_nearest_class():
    near_tree = (4, 104, 3)  # within tolerance of #006400
    counts = [(16384, near_tree)]
    rows = class_distribution(counts, LEGEND, BOUNDS)
    assert len(rows) == 1
    assert rows[0]["label"] == "树木覆盖"
    assert rows[0]["ratio"] == 1.0


def test_empty_legend_returns_empty():
    assert class_distribution([(1, TREE)], [], BOUNDS) == []


def test_no_area_when_bounds_missing():
    rows = class_distribution([(16384, TREE)], LEGEND, None)
    assert rows[0]["ratio"] == 1.0
    assert "value" not in rows[0]


def test_class_mask_matches_named_classes():
    # Top-left quadrant tree (green), rest built-up. Cue "树" → only tree pixels.
    arr = np.full((4, 4, 3), BUILT, dtype=np.uint8)
    arr[:2, :2] = TREE
    mask = class_mask(arr, LEGEND, ("树",))
    assert mask.sum() == 4
    assert mask[0, 0] and not mask[3, 3]


def test_class_mask_empty_legend_all_false():
    arr = np.full((3, 3, 3), TREE, dtype=np.uint8)
    mask = class_mask(arr, [], ("树",))
    assert mask.shape == (3, 3) and not mask.any()


def test_class_mask_no_cue_match_all_false():
    arr = np.full((3, 3, 3), TREE, dtype=np.uint8)
    mask = class_mask(arr, LEGEND, ("道路",))
    assert not mask.any()
