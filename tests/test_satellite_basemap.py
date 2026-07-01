"""Unit tests for the satellite basemap tile math (no network)."""

from __future__ import annotations

from agent.services.satellite_basemap import (
    _extent_px,
    _lonlat_to_pixel,
    _pick_zoom,
    _valid_bounds,
)

HAIDIAN = [116.239959, 39.885118, 116.254804, 39.896747]


def test_valid_bounds():
    assert _valid_bounds(HAIDIAN)
    assert not _valid_bounds([1, 2, 3])          # wrong length
    assert not _valid_bounds([10, 0, 5, 1])      # min_lng >= max_lng
    assert not _valid_bounds([0, 0, 1, 91])      # lat out of mercator range


def test_pixel_is_monotonic():
    # Larger lon -> larger x; larger lat -> smaller y (north is up).
    x0, y0 = _lonlat_to_pixel(116.24, 39.88, 16)
    x1, y1 = _lonlat_to_pixel(116.26, 39.90, 16)
    assert x1 > x0
    assert y1 < y0


def test_pick_zoom_keeps_footprint_bounded():
    z = _pick_zoom(HAIDIAN, max_px=1200)
    w, h = _extent_px(HAIDIAN, z)
    assert max(w, h) <= 1200
    # one zoom deeper would exceed the cap (i.e. we picked the highest that fits)
    w2, h2 = _extent_px(HAIDIAN, z + 1)
    assert max(w2, h2) > 1200


def test_pick_zoom_for_small_area_is_high():
    # ~1.3km footprint should land at a high, detailed zoom.
    assert _pick_zoom(HAIDIAN) >= 15
