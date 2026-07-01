"""Shared helpers used across agent services.

These are deliberately dependency-free and behaviour-preserving extractions of
logic that used to be copy-pasted into several service modules.
"""

from __future__ import annotations

import json
import re


def bbox_intersection_score(a: list[float], b: list[float]) -> float:
    """Overlap of two WGS84 bboxes, normalized by the smaller box's area.

    Returns 0.0 for malformed or non-overlapping boxes; 1.0 when the smaller
    box is fully contained in the larger one. Both boxes are
    ``[min_lng, min_lat, max_lng, max_lat]``.
    """
    if len(a) != 4 or len(b) != 4:
        return 0.0
    left = max(a[0], b[0])
    bottom = max(a[1], b[1])
    right = min(a[2], b[2])
    top = min(a[3], b[3])
    if right <= left or top <= bottom:
        return 0.0
    inter = (right - left) * (top - bottom)
    patch_area = max((a[2] - a[0]) * (a[3] - a[1]), 1e-12)
    query_area = max((b[2] - b[0]) * (b[3] - b[1]), 1e-12)
    return float(inter / min(patch_area, query_area))


def strip_markdown(text: str) -> str:
    """Turn LLM Markdown into clean plain text for chat bubbles.

    Chat is rendered as plain text, so ``**bold**``/``# heading``/``- item``
    would otherwise show their literal markers. Bullets become "• ", headings
    and emphasis/code markers are removed, and numbered lists are kept as-is.
    """
    if not text:
        return text
    out_lines = []
    for line in text.split("\n"):
        s = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)          # headings
        s = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", s)            # bullets -> •
        s = s.replace("**", "").replace("__", "")            # bold markers
        s = re.sub(r"\*([^*\n]+?)\*", r"\1", s)              # *italic*
        s = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"\1", s)   # _italic_
        s = s.replace("`", "")                                # inline code
        out_lines.append(s)
    result = "\n".join(out_lines)
    return re.sub(r"\n{3,}", "\n\n", result).strip()


def extract_json_object(text: str) -> dict | None:
    """Best-effort extraction of a single JSON object from LLM text output.

    Tolerates ```json fenced blocks and surrounding prose. Returns ``None`` when
    no JSON object can be parsed.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    match = re.search(r"\{.*\}", stripped, flags=re.S)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
