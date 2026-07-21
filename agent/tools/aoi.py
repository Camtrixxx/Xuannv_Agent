"""AOI aggregation tool: roll per-patch binary coverage up to an area total.

Pure function, no I/O — the service layer resolves patches and computes each
patch's ``binary_coverage`` (see agent/tools/raster.py), then hands the rows
here to get an AOI-level summary. Kept separate and testable so the planner can
reuse it directly.
"""

from __future__ import annotations

from typing import Any, Iterable


def aggregate_binary_coverage(per_patch: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Sum per-patch binary coverage into one AOI summary.

    Each row is a ``binary_coverage`` result:
    ``{"foreground_ratio", "total_area_ha", "covered_area_ha"}``. Rows missing
    area (bounds unknown) still count toward ``patch_count`` but are excluded
    from the hectare totals, so ``coverage_ratio`` reflects only patches whose
    area is known — never silently understated.

    The AOI ratio is area-weighted (covered_ha / total_ha), NOT a mean of
    per-patch ratios, so large and small patches contribute proportionally.
    """
    patch_count = 0
    area_patch_count = 0
    total_ha = 0.0
    covered_ha = 0.0
    for row in per_patch:
        patch_count += 1
        total = row.get("total_area_ha")
        covered = row.get("covered_area_ha")
        if total is None or covered is None:
            continue
        area_patch_count += 1
        total_ha += float(total)
        covered_ha += float(covered)

    coverage_ratio = round(covered_ha / total_ha, 4) if total_ha > 0 else None
    return {
        "patch_count": patch_count,
        "area_patch_count": area_patch_count,
        "total_area_ha": round(total_ha, 2),
        "covered_area_ha": round(covered_ha, 2),
        "coverage_ratio": coverage_ratio,
    }


def aggregate_class_distribution(per_patch: Iterable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Merge many patches' land-cover distributions into one AOI-wide table.

    Each input is a ``class_distribution`` result (rows of
    ``{label, class_id, ratio, value(ha)?}``). Hectares are summed per class and
    shares recomputed against the AOI hectare total, so the AOI ratio is
    area-weighted — never a mean of per-patch ratios. Requires ``value`` (ha) on
    the rows; patches whose rows lack it are skipped (can't area-weight without
    it). Returns rows ``{label, class_id, ratio, value}`` sorted by share desc.
    """
    ha_by_class: dict[Any, float] = {}
    label_by_class: dict[Any, str] = {}
    total_ha = 0.0
    for rows in per_patch:
        for row in rows or []:
            value = row.get("value")
            if value is None:
                continue
            cid = row.get("class_id")
            ha_by_class[cid] = ha_by_class.get(cid, 0.0) + float(value)
            label_by_class[cid] = row.get("label") or label_by_class.get(cid) or "未命名"
            total_ha += float(value)

    if total_ha <= 0:
        return []
    out: list[dict[str, Any]] = []
    for cid, ha in ha_by_class.items():
        out.append(
            {
                "label": label_by_class[cid],
                "class_id": cid,
                "ratio": round(ha / total_ha, 4),
                "value": round(ha, 2),
            }
        )
    out.sort(key=lambda r: r["ratio"], reverse=True)
    return out
