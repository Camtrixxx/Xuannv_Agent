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
