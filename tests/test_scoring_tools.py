"""Tests for pressure-scoring tools (scenario C)."""

from __future__ import annotations

from agent.tools.scoring import pressure_score, rank_patches, summarize_scores


def test_score_monotonic_in_impervious():
    low = pressure_score(0.1, 0.5)["score"]
    high = pressure_score(0.8, 0.5)["score"]
    assert high > low


def test_score_monotonic_in_green_deficit():
    lush = pressure_score(0.5, 0.9)["score"]
    barren = pressure_score(0.5, 0.05)["score"]
    assert barren > lush


def test_score_bands():
    assert pressure_score(1.0, 0.0)["band"] == "高压"  # score 100
    assert pressure_score(0.0, 1.0)["band"] == "低压"  # score 0
    mid = pressure_score(0.5, 0.5)
    assert mid["score"] == 50.0 and mid["band"] == "中压"


def test_score_clamps_out_of_range():
    s = pressure_score(1.5, -0.2)
    assert s["score"] == 100.0
    assert s["impervious_ratio"] == 1.0 and s["green_ratio"] == 0.0


def test_rank_patches_orders_and_tags_rank():
    rows = [
        {"label": "p1", "score": 40.0},
        {"label": "p2", "score": 90.0},
        {"label": "p3", "score": 65.0},
    ]
    ranked = rank_patches(rows, top_n=2)
    assert [r["label"] for r in ranked] == ["p2", "p3"]
    assert ranked[0]["rank"] == 1 and ranked[1]["rank"] == 2


def test_rank_patches_handles_missing_score():
    rows = [{"label": "a", "score": 10.0}, {"label": "b"}]
    ranked = rank_patches(rows, top_n=5)
    assert ranked[0]["label"] == "a"  # scored one first
    assert len(ranked) == 2


def test_rank_does_not_mutate_input():
    rows = [{"label": "p1", "score": 40.0}]
    rank_patches(rows)
    assert "rank" not in rows[0]


def test_summarize_scores():
    rows = [
        {"score": 80.0, "band": "高压"},
        {"score": 50.0, "band": "中压"},
        {"score": 20.0, "band": "低压"},
        {"score": 90.0, "band": "高压"},
    ]
    out = summarize_scores(rows)
    assert out["patch_count"] == 4
    assert out["high"] == 2 and out["medium"] == 1 and out["low"] == 1
    assert out["mean_score"] == round((80 + 50 + 20 + 90) / 4, 1)


def test_summarize_empty():
    out = summarize_scores([])
    assert out["patch_count"] == 0 and out["mean_score"] is None
