"""Change-detection tools: two aligned binary masks → gained / lost / stayed.

Haidian result PNGs for a given patch are pixel-aligned across months (verified:
128×128, same grid), so a two-date change map is an elementwise mask compare.
Kept pure (numpy in / dicts out) so it unit-tests without any network or PIL.

- ``binary_change``: one patch, two dates → pixel counts + hectares for the
  gained (0→1), lost (1→0) and stayed (1→1) sets.
- ``aggregate_change``: sum those across an AOI's patches.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from agent.tools.raster import BINARY_TASK_BACKGROUND, _BG_TOLERANCE


def foreground_mask(rgb_array: np.ndarray, background: tuple[int, int, int]) -> np.ndarray:
    """Boolean HxW mask of non-background pixels (near-bg counts as bg).

    ``rgb_array`` is HxWx3 uint8. Same fixed-background rule as ``raster`` so a
    change mask and a coverage ratio agree on what "foreground" means.
    """
    bg = np.asarray(background, dtype=np.int16)
    diff = np.abs(rgb_array.astype(np.int16) - bg)
    return (diff > _BG_TOLERANCE).any(axis=-1)


def mask_for_task(rgb_array: np.ndarray, task_id: str) -> np.ndarray | None:
    """Foreground mask for a binary task, or ``None`` if the task isn't binary."""
    background = BINARY_TASK_BACKGROUND.get(task_id)
    if background is None:
        return None
    return foreground_mask(rgb_array, background)


# Custom-model result PNGs paint the target class in its class colour on a fixed
# light-grey background (verified against live /models infer output).
CUSTOM_MODEL_BACKGROUND = (200, 200, 200)


def custom_model_mask(rgb_array: np.ndarray) -> np.ndarray:
    """Foreground (target) mask for a custom-model result PNG.

    Target pixels are the class colour; non-target is grey ``(200,200,200)``.
    """
    return foreground_mask(rgb_array, CUSTOM_MODEL_BACKGROUND)


def binary_change(
    mask_before: np.ndarray,
    mask_after: np.ndarray,
    total_area_ha: float | None,
) -> dict[str, Any]:
    """Per-patch change stats between two aligned foreground masks.

    Returns pixel counts and (when ``total_area_ha`` is known) hectares for
    gained/lost/stayed, plus net change. Raises ``ValueError`` on shape
    mismatch — misaligned masks must never be silently compared.
    """
    if mask_before.shape != mask_after.shape:
        raise ValueError(f"mask shape mismatch: {mask_before.shape} vs {mask_after.shape}")
    total_px = int(mask_before.size)
    gained_px = int(np.count_nonzero(~mask_before & mask_after))
    lost_px = int(np.count_nonzero(mask_before & ~mask_after))
    stayed_px = int(np.count_nonzero(mask_before & mask_after))
    before_px = int(np.count_nonzero(mask_before))
    after_px = int(np.count_nonzero(mask_after))

    def _ha(px: int) -> float | None:
        if total_area_ha is None or total_px <= 0:
            return None
        return round(total_area_ha * px / total_px, 2)

    return {
        "total_px": total_px,
        "before_px": before_px,
        "after_px": after_px,
        "gained_px": gained_px,
        "lost_px": lost_px,
        "stayed_px": stayed_px,
        "net_px": after_px - before_px,
        "before_ha": _ha(before_px),
        "after_ha": _ha(after_px),
        "gained_ha": _ha(gained_px),
        "lost_ha": _ha(lost_px),
        "net_ha": (None if total_area_ha is None else round((_ha(after_px) or 0) - (_ha(before_px) or 0), 2)),
    }


def aggregate_change(per_patch: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Sum per-patch change dicts into an AOI-wide change summary.

    Areas (ha) are summed where present; ratios are recomputed against the
    summed before/after areas so the AOI figure is area-weighted. Patch count
    reflects how many patches contributed.
    """
    patch_count = 0
    gained_ha = lost_ha = before_ha = after_ha = 0.0
    have_area = False
    for row in per_patch:
        if not row:
            continue
        patch_count += 1
        for key, acc in (("gained_ha", "g"), ("lost_ha", "l"), ("before_ha", "b"), ("after_ha", "a")):
            val = row.get(key)
            if val is None:
                continue
            have_area = True
            if acc == "g":
                gained_ha += val
            elif acc == "l":
                lost_ha += val
            elif acc == "b":
                before_ha += val
            else:
                after_ha += val

    net_ha = round(after_ha - before_ha, 2) if have_area else None
    growth_ratio = round((after_ha - before_ha) / before_ha, 4) if have_area and before_ha > 0 else None
    return {
        "patch_count": patch_count,
        "before_area_ha": round(before_ha, 2) if have_area else None,
        "after_area_ha": round(after_ha, 2) if have_area else None,
        "gained_area_ha": round(gained_ha, 2) if have_area else None,
        "lost_area_ha": round(lost_ha, 2) if have_area else None,
        "net_area_ha": net_ha,
        "growth_ratio": growth_ratio,
    }
