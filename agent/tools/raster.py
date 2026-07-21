"""Raster statistics tools: turn a result PNG into coverage ratio + area.

Two independent primitives, kept pure so they can be unit-tested and later
composed by the planner:

- ``binary_foreground_ratio``: fraction of *non-background* pixels. Unlike a
  "dominant colour is background" heuristic (which silently flips when the
  target covers >50% of a patch), the background colour is fixed per task, so
  the ratio stays correct at any coverage level.
- ``area_ha_from_bounds``: hectares spanned by projected (metre) bounds. Haidian
  patches are EPSG:32650 (UTM 50N) → exact, not an estimate.

``binary_coverage`` combines them into the dict the report layer consumes.
"""

from __future__ import annotations

from typing import Iterable

# Per-task background RGB for binary result PNGs (verified against live API):
# building/water/construction paint the target on white; roads on black.
BINARY_TASK_BACKGROUND: dict[str, tuple[int, int, int]] = {
    "building_extraction": (255, 255, 255),
    "water_extraction": (255, 255, 255),
    "construction": (255, 255, 255),
    "road_extraction": (0, 0, 0),
}

# Colour distance tolerance: PNGs are flat-colour, but guard against stray
# anti-aliasing pixels at edges by treating near-background as background.
_BG_TOLERANCE = 24


def _is_background(rgb: tuple[int, int, int], background: tuple[int, int, int]) -> bool:
    return all(abs(a - b) <= _BG_TOLERANCE for a, b in zip(rgb, background))


def binary_foreground_ratio(
    color_counts: Iterable[tuple[int, tuple[int, int, int]]],
    background: tuple[int, int, int],
) -> float:
    """Fraction of pixels that are not the given background colour.

    ``color_counts`` is ``[(count, (r, g, b)), ...]`` (PIL ``getcolors`` order).
    Returns 0.0 for an empty image. Robust to any coverage level — no
    dominant-colour assumption.
    """
    total = 0
    foreground = 0
    for count, rgb in color_counts:
        total += count
        if not _is_background(tuple(rgb), background):
            foreground += count
    if total <= 0:
        return 0.0
    return round(foreground / total, 4)


def area_ha_from_bounds(bounds: list[float] | None, *, projected: bool) -> float | None:
    """Hectares covered by ``[minx, miny, maxx, maxy]``.

    ``projected=True`` means metre units (UTM) → exact. Returns ``None`` when
    bounds are missing/malformed or not projected (WGS84 degrees need a
    latitude-dependent conversion we defer to the AOI tool in P3.2).
    """
    if not projected or not bounds or len(bounds) != 4:
        return None
    width_m = abs(float(bounds[2]) - float(bounds[0]))
    height_m = abs(float(bounds[3]) - float(bounds[1]))
    if width_m <= 0 or height_m <= 0:
        return None
    return round(width_m * height_m / 10_000.0, 2)


def binary_coverage(
    task_id: str,
    color_counts: Iterable[tuple[int, tuple[int, int, int]]],
    bounds_projected: list[float] | None,
) -> dict[str, float | None]:
    """Coverage ratio (+ covered area in ha when bounds are known) for a task.

    Returns ``{}`` when the task is not a known binary task, so callers can fall
    back to their existing generic handling for multiclass tasks.
    """
    background = BINARY_TASK_BACKGROUND.get(task_id)
    if background is None:
        return {}
    ratio = binary_foreground_ratio(color_counts, background)
    total_ha = area_ha_from_bounds(bounds_projected, projected=True)
    covered_ha = round(total_ha * ratio, 2) if total_ha is not None else None
    return {
        "foreground_ratio": ratio,
        "total_area_ha": total_ha,
        "covered_area_ha": covered_ha,
    }
