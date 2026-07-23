"""Stitch pixel-aligned result PNGs into one image by their UTM grid.

Regional patches (Haidian) are regular tiles on a fixed UTM grid, so combining
per-patch result PNGs into a single seamless layer is a pure paste — no
reprojection or resampling. Each tile is placed at its metre offset from the
union's top-left corner, scaled by the shared pixels-per-metre of the PNGs.
Gaps (non-contiguous selections) stay transparent.

Kept dependency-light (PIL only) and side-effect-scoped to the one output path
so both HaidianEmbeddingAnalysisService and ChangeMonitorService can share it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from PIL import Image

from agent.schemas.report import ChartAsset


def stitch_tiles(
    tiles: list[tuple[list[float], Path]],
    out_path: Path,
    *,
    max_pixels: int = 64_000_000,
) -> Path | None:
    """Mosaic per-tile PNGs onto one RGBA canvas by their projected bounds.

    ``tiles`` is a list of ``(bounds_projected, png_path)`` where
    ``bounds_projected`` is ``[xmin, ymin, xmax, ymax]`` in metres (UTM). Returns
    ``out_path`` on success, or ``None`` when there are fewer than two tiles,
    bounds/sizes are missing or inconsistent, the canvas would exceed
    ``max_pixels``, or any file can't be read/written — callers then fall back to
    per-tile overlays.
    """
    prepared: list[tuple[list[float], Path, int, int]] = []
    for bounds, asset_path in tiles:
        if not (isinstance(bounds, list) and len(bounds) == 4):
            return None
        try:
            with Image.open(asset_path) as image:
                w, h = image.size
        except OSError:
            return None
        if w <= 0 or h <= 0:
            return None
        prepared.append(([float(v) for v in bounds], asset_path, w, h))
    if len(prepared) < 2:
        return None

    # Shared resolution: every tile must agree on pixels-per-metre, else the grid
    # paste would misalign — bail so callers fall back to per-tile overlays.
    def _res(tile: tuple[list[float], Path, int, int]) -> tuple[float, float]:
        (xmin, ymin, xmax, ymax), _, w, h = tile
        return (w / (xmax - xmin), h / (ymax - ymin))

    px_per_m_x, px_per_m_y = _res(prepared[0])
    for tile in prepared[1:]:
        rx, ry = _res(tile)
        if abs(rx - px_per_m_x) > 1e-6 or abs(ry - px_per_m_y) > 1e-6:
            return None

    union_xmin = min(t[0][0] for t in prepared)
    union_ymin = min(t[0][1] for t in prepared)
    union_xmax = max(t[0][2] for t in prepared)
    union_ymax = max(t[0][3] for t in prepared)
    canvas_w = round((union_xmax - union_xmin) * px_per_m_x)
    canvas_h = round((union_ymax - union_ymin) * px_per_m_y)
    if canvas_w <= 0 or canvas_h <= 0 or canvas_w * canvas_h > max_pixels:
        return None

    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    for (xmin, ymin, xmax, ymax), asset_path, w, h in prepared:
        # Column offset from the union's left edge; row offset from its TOP edge
        # (image y grows downward, UTM y grows upward — hence ymax).
        left = round((xmin - union_xmin) * px_per_m_x)
        top = round((union_ymax - ymax) * px_per_m_y)
        try:
            with Image.open(asset_path) as image:
                canvas.paste(image.convert("RGBA"), (left, top))
        except OSError:
            return None

    try:
        canvas.save(out_path)
    except OSError:
        return None
    return out_path


def _union_wgs84(bounds_list: list[Any]) -> list[float] | None:
    """Bounding box that contains every valid ``[lon,lat,lon,lat]`` in the list."""
    valid = [
        [float(v) for v in item[:4]]
        for item in bounds_list
        if isinstance(item, list) and len(item) == 4
    ]
    if not valid:
        return None
    return [
        min(item[0] for item in valid),
        min(item[1] for item in valid),
        max(item[2] for item in valid),
        max(item[3] for item in valid),
    ]


def build_mosaic_overlay(
    tiles: list[dict[str, Any]],
    asset_dir: Path,
    *,
    stem: str,
    fingerprint: str,
    merged_title: str,
    merged_caption: str,
    per_patch_title: str,
    per_patch_caption: str,
) -> list[ChartAsset]:
    """Merge per-patch result PNGs into ONE mosaic overlay when possible.

    ``tiles`` is a list of dicts, each ``{"patch_id", "path" (Path), "bounds_wgs84"
    [lon,lat,lon,lat], "bounds" (projected [xmin,ymin,xmax,ymax] metres)}``. When
    every tile carries 4-element projected bounds and there are at least two, they
    are stitched by their UTM grid into a single seamless layer (one map toggle,
    one figure) — like the road/building report. Otherwise (single patch, or any
    tile missing/mismatched bounds) it falls back to one overlay per patch.

    Returns a list of ``ChartAsset`` (``overlay=True``); empty when ``tiles`` is
    empty. ``stem``/``fingerprint`` name the merged PNG deterministically so an
    identical selection reuses the same file. Titles/captions are supplied by the
    caller so each scenario can describe its own theme.
    """
    if not tiles:
        return []
    used_ids = [t["patch_id"] for t in tiles]
    stitchable = [(t["bounds"], t["path"]) for t in tiles if len(t.get("bounds") or []) == 4]
    if len(stitchable) == len(tiles) and len(tiles) > 1:
        union = _union_wgs84([t["bounds_wgs84"] for t in tiles])
        digest = hashlib.sha1(f"{fingerprint}:{sorted(used_ids)}".encode("utf-8")).hexdigest()[:12]
        out_path = asset_dir / f"{stem}_{digest}.png"
        mosaic = stitch_tiles(stitchable, out_path)
        if mosaic is not None and union is not None:
            return [
                ChartAsset(
                    title=merged_title.format(n=len(used_ids)),
                    kind="image",
                    url=f"/reports/assets/{mosaic.name}",
                    caption=merged_caption.format(n=len(used_ids)),
                    bounds_wgs84=[float(v) for v in union][:4],
                    overlay=True,
                    patch_id=",".join(used_ids),
                )
            ]
    # Fallback: one overlay per patch (single patch, or stitching bailed).
    return [
        ChartAsset(
            title=per_patch_title.format(patch_id=t["patch_id"]),
            kind="image",
            url=f"/reports/assets/{t['path'].name}",
            caption=per_patch_caption,
            bounds_wgs84=t["bounds_wgs84"],
            overlay=True,
            patch_id=t["patch_id"],
        )
        for t in tiles
    ]
