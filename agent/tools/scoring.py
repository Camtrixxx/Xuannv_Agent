"""Pressure-scoring tools (scenario C): 高硬化低绿地 → 补绿优先.

Pure functions: given a patch's impervious (built-up) share and green share,
produce a 0–100 pressure score and a band. High built-up + low green ⇒ high
pressure ⇒ higher补绿 priority. Weights/thresholds are module constants with
their rationale, so the planner or a config can tune them later.

Reliability note (enforced by the caller, documented here): the built-up share
should come from the *building* binary task (reliable), while the green share
comes from the land-cover model (advisory). The score is only as trustworthy as
its weakest input — callers must surface that in the report.
"""

from __future__ import annotations

from typing import Any, Iterable

# Weights: how much each factor drives pressure. Equal by default — a patch is
# under pressure both when it is heavily built AND when it lacks green; we don't
# privilege one signal over the other without evidence to.
WEIGHT_IMPERVIOUS = 0.5
WEIGHT_GREEN_DEFICIT = 0.5

# Band thresholds on the 0–100 score. Even thirds: a defensible default with no
# ground-truth calibration; documented as heuristic in the report.
BAND_HIGH = 67.0
BAND_MEDIUM = 34.0

# Semi-transparent fill colours per band for the on-map heatmap overlay: high
# pressure (补绿最优先) red, medium orange, low green. Alpha keeps the satellite
# basemap readable underneath.
BAND_RGBA = {
    "高压": (220, 38, 38, 150),
    "中压": (245, 158, 11, 150),
    "低压": (34, 197, 94, 130),
}


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def pressure_score(impervious_ratio: float, green_ratio: float) -> dict[str, Any]:
    """0–100 pressure score + band from built-up and green shares (each 0–1).

    ``green_deficit = 1 - green_ratio``. Score is the weighted blend rescaled to
    0–100. Returns ``{score, band, impervious_ratio, green_ratio}``.
    """
    imp = _clamp01(impervious_ratio)
    green = _clamp01(green_ratio)
    deficit = 1.0 - green
    denom = WEIGHT_IMPERVIOUS + WEIGHT_GREEN_DEFICIT
    raw = (WEIGHT_IMPERVIOUS * imp + WEIGHT_GREEN_DEFICIT * deficit) / denom
    score = round(raw * 100, 1)
    band = "高压" if score >= BAND_HIGH else ("中压" if score >= BAND_MEDIUM else "低压")
    return {
        "score": score,
        "band": band,
        "impervious_ratio": round(imp, 4),
        "green_ratio": round(green, 4),
    }


def band_rgba(band: str) -> tuple[int, int, int, int]:
    """Fill colour (RGBA) for a pressure band; transparent for an unknown band."""
    return BAND_RGBA.get(band, (0, 0, 0, 0))


def _box_smooth(field, radius: int):
    """Mean-filter a float field with a square window (edge-shrinking, no deps).

    Turns a 0/1 pixel mask into a local *density* in [0,1] so the pressure field
    varies continuously instead of being binary speckle. Uses a summed-area table
    so the window mean is O(1) per pixel regardless of radius.
    """
    import numpy as np

    if radius <= 0:
        return field.astype(np.float32)
    f = field.astype(np.float32)
    h, w = f.shape
    sat = np.zeros((h + 1, w + 1), dtype=np.float32)
    sat[1:, 1:] = f.cumsum(axis=0).cumsum(axis=1)
    ys = np.arange(h)
    xs = np.arange(w)
    y0 = np.clip(ys - radius, 0, h)
    y1 = np.clip(ys + radius + 1, 0, h)
    x0 = np.clip(xs - radius, 0, w)
    x1 = np.clip(xs + radius + 1, 0, w)
    total = (
        sat[y1[:, None], x1[None, :]]
        - sat[y0[:, None], x1[None, :]]
        - sat[y1[:, None], x0[None, :]]
        + sat[y0[:, None], x0[None, :]]
    )
    area = (y1 - y0)[:, None] * (x1 - x0)[None, :]
    return total / np.maximum(area, 1)


# Pressure colour ramp stops (score 0→1 → RGB): green → yellow → red.
_RAMP = ((0.0, (34, 197, 94)), (0.5, (245, 200, 60)), (1.0, (220, 38, 38)))


def _ramp_rgb(t):
    """Vectorized green→yellow→red lookup for a float array ``t`` in [0,1]."""
    import numpy as np

    t = np.clip(t, 0.0, 1.0)
    (t0, c0), (t1, c1), (t2, c2) = _RAMP
    out = np.zeros(t.shape + (3,), dtype=np.float32)
    lo = t <= t1
    # Lower half: green→yellow.
    f = np.where(lo, (t - t0) / (t1 - t0), 0.0)[..., None]
    out = np.where(lo[..., None], np.array(c0) + (np.array(c1) - np.array(c0)) * f, out)
    # Upper half: yellow→red.
    f2 = np.where(~lo, (t - t1) / (t2 - t1), 0.0)[..., None]
    out = np.where(~lo[..., None], np.array(c1) + (np.array(c2) - np.array(c1)) * f2, out)
    return out.astype(np.uint8)


def pressure_field_rgba(
    impervious_mask,
    green_mask,
    *,
    radius: int = 8,
    alpha: int = 150,
):
    """Per-pixel continuous pressure field → green→yellow→red RGBA overlay.

    Both masks are boolean HxW (impervious = built-up pixels, green = vegetation).
    Each is box-smoothed to a local density in [0,1]; pressure = weighted blend of
    impervious density and green *deficit* (same weights as ``pressure_score``),
    then coloured on the ramp. A uniform ``alpha`` keeps the basemap readable.
    Returns an HxWx4 uint8 array.
    """
    import numpy as np

    imp = _box_smooth(impervious_mask, radius)
    green = _box_smooth(green_mask, radius)
    denom = WEIGHT_IMPERVIOUS + WEIGHT_GREEN_DEFICIT
    field = (WEIGHT_IMPERVIOUS * imp + WEIGHT_GREEN_DEFICIT * (1.0 - green)) / denom
    rgb = _ramp_rgb(field)
    h, w = field.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., :3] = rgb
    rgba[..., 3] = alpha
    return rgba


def rank_patches(rows: Iterable[dict[str, Any]], top_n: int = 10) -> list[dict[str, Any]]:
    """Sort scored patch rows by pressure desc, tag rank, take top_n.

    Each input row must carry a ``score``; rows without one sort last. Returns
    new dicts with an added 1-based ``rank`` (does not mutate inputs).
    """
    ordered = sorted(
        (r for r in rows if r is not None),
        key=lambda r: (r.get("score") if r.get("score") is not None else -1),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    for i, r in enumerate(ordered[: max(0, top_n)], start=1):
        out.append({**r, "rank": i})
    return out


def summarize_scores(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """AOI-level roll-up: mean score, counts per band, patch count."""
    scores = [r["score"] for r in rows if r and r.get("score") is not None]
    if not scores:
        return {"patch_count": 0, "mean_score": None, "high": 0, "medium": 0, "low": 0}
    bands = [r.get("band") for r in rows if r and r.get("score") is not None]
    return {
        "patch_count": len(scores),
        "mean_score": round(sum(scores) / len(scores), 1),
        "high": bands.count("高压"),
        "medium": bands.count("中压"),
        "low": bands.count("低压"),
    }
