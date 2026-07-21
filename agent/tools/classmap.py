"""Multiclass legend tool: map result-PNG colours to named classes + shares.

The region API serves the authoritative legend via
``GET /system-models/{task}/classes?region_id=`` as ``{id, name, color}`` (hex).
This tool is the pure half: given that legend and a PNG's colour histogram, it
assigns every pixel to its nearest legend colour and returns per-class pixel
counts, area (ha) and share. Accuracy of the underlying model is not our concern
— we faithfully report whatever it predicted, under the correct labels.
"""

from __future__ import annotations

from typing import Any, Iterable

from agent.tools.raster import area_ha_from_bounds

# Max squared RGB distance for a pixel to still count as a legend colour.
# Result PNGs are flat-colour; this only absorbs edge anti-aliasing.
_MATCH_MAX_DIST2 = 60 ** 2


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    h = value.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def normalize_legend(raw: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """API ModelClass rows → ``[{id, name, rgb}]`` (skips malformed rows)."""
    out: list[dict[str, Any]] = []
    for row in raw or []:
        color = row.get("color")
        if not isinstance(color, str) or len(color.lstrip("#")) < 6:
            continue
        out.append({"id": row.get("id"), "name": row.get("name") or "未命名", "rgb": hex_to_rgb(color)})
    return out


def _nearest(rgb: tuple[int, int, int], legend: list[dict[str, Any]]) -> dict[str, Any] | None:
    best = None
    best_d = _MATCH_MAX_DIST2 + 1
    for cls in legend:
        lr, lg, lb = cls["rgb"]
        d = (rgb[0] - lr) ** 2 + (rgb[1] - lg) ** 2 + (rgb[2] - lb) ** 2
        if d < best_d:
            best_d, best = d, cls
    return best if best_d <= _MATCH_MAX_DIST2 else None


def class_distribution(
    color_counts: Iterable[tuple[int, tuple[int, int, int]]],
    legend: list[dict[str, Any]],
    bounds_projected: list[float] | None,
) -> list[dict[str, Any]]:
    """Per-class shares for one result PNG, sorted by share desc.

    Returns rows ``{label, class_id, ratio, value(ha)?}`` — the exact shape the
    report ``data_table`` renders. Unmatched pixels roll into a "其他/未识别"
    row so shares always sum to 1. Empty legend → ``[]`` (caller keeps its
    generic path).
    """
    if not legend:
        return []
    per_class: dict[Any, int] = {}
    unknown = 0
    total = 0
    for count, rgb in color_counts:
        total += count
        match = _nearest(tuple(rgb), legend)
        if match is None:
            unknown += count
        else:
            per_class[match["id"]] = per_class.get(match["id"], 0) + count
    if total <= 0:
        return []

    total_ha = area_ha_from_bounds(bounds_projected, projected=True)
    by_id = {c["id"]: c for c in legend}
    rows: list[dict[str, Any]] = []
    for cid, pixels in per_class.items():
        ratio = round(pixels / total, 4)
        row = {"label": by_id[cid]["name"], "class_id": cid, "ratio": ratio}
        if total_ha is not None:
            row["value"] = round(total_ha * ratio, 2)
        rows.append(row)
    if unknown > 0:
        ratio = round(unknown / total, 4)
        row = {"label": "其他/未识别", "class_id": None, "ratio": ratio}
        if total_ha is not None:
            row["value"] = round(total_ha * ratio, 2)
        rows.append(row)
    rows.sort(key=lambda r: r["ratio"], reverse=True)
    return rows
