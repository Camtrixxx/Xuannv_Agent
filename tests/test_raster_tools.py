"""Unit tests for the pure raster tools (P3.1 binary coverage)."""

from __future__ import annotations

from agent.tools.raster import (
    BINARY_TASK_BACKGROUND,
    area_ha_from_bounds,
    binary_coverage,
    binary_foreground_ratio,
)

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (230, 0, 0)
ORANGE = (245, 158, 11)

# Haidian patch: EPSG:32650 metres, 1280m x 1280m = 163.84 ha.
UTM_BOUNDS = [435014.236, 4415283.021, 436294.236, 4416563.021]


def test_building_ratio_on_white_background():
    # 29.4% red foreground on white (matches live sample).
    counts = [(int(0.706 * 16384), WHITE), (int(0.294 * 16384), RED)]
    assert binary_foreground_ratio(counts, WHITE) == 0.294


def test_road_uses_black_background():
    # Roads paint orange on BLACK — background differs per task.
    counts = [(12000, BLACK), (4384, ORANGE)]
    assert binary_foreground_ratio(counts, BLACK) == round(4384 / 16384, 4)


def test_foreground_over_50pct_does_not_flip():
    # Regression: old "dominant colour = background" heuristic would report 0.20
    # here (calling the 80% foreground "background"). Fixed ratio must be 0.80.
    counts = [(80, RED), (20, WHITE)]  # foreground dominant
    assert binary_foreground_ratio(counts, WHITE) == 0.8


def test_all_background_is_zero():
    assert binary_foreground_ratio([(16384, WHITE)], WHITE) == 0.0


def test_empty_image_is_zero():
    assert binary_foreground_ratio([], WHITE) == 0.0


def test_antialiasing_near_background_counts_as_background():
    # A near-white stray pixel (within tolerance) should not inflate coverage.
    counts = [(16000, WHITE), (100, (250, 250, 250)), (284, RED)]
    assert binary_foreground_ratio(counts, WHITE) == round(284 / 16384, 4)


def test_area_from_utm_bounds_is_exact():
    assert area_ha_from_bounds(UTM_BOUNDS, projected=True) == 163.84


def test_area_none_when_not_projected_or_malformed():
    assert area_ha_from_bounds(UTM_BOUNDS, projected=False) is None
    assert area_ha_from_bounds([1, 2, 3], projected=True) is None
    assert area_ha_from_bounds(None, projected=True) is None
    assert area_ha_from_bounds([0, 0, 0, 0], projected=True) is None


def test_binary_coverage_combines_ratio_and_area():
    counts = [(int(0.706 * 16384), WHITE), (int(0.294 * 16384), RED)]
    out = binary_coverage("building_extraction", counts, UTM_BOUNDS)
    assert out["foreground_ratio"] == 0.294
    assert out["total_area_ha"] == 163.84
    assert out["covered_area_ha"] == round(163.84 * 0.294, 2)


def test_binary_coverage_empty_for_multiclass_task():
    # Land cover is not binary → tool returns {} so caller keeps generic path.
    assert binary_coverage("land_cover_classification", [(1, WHITE)], UTM_BOUNDS) == {}


def test_all_binary_tasks_registered():
    assert set(BINARY_TASK_BACKGROUND) == {
        "building_extraction",
        "road_extraction",
        "construction",
        "water_extraction",
    }
