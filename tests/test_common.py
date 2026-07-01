"""Unit tests for the shared service helpers."""

from __future__ import annotations

from agent.services.common import bbox_intersection_score, extract_json_object


def test_bbox_identical_boxes_score_one():
    assert bbox_intersection_score([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0


def test_bbox_containment_normalizes_by_smaller_box():
    # Query fully inside the patch -> normalized by the (smaller) query area.
    assert bbox_intersection_score([0, 0, 10, 10], [0, 0, 5, 5]) == 1.0


def test_bbox_partial_overlap():
    score = bbox_intersection_score([0, 0, 10, 10], [5, 5, 15, 15])
    assert abs(score - 0.25) < 1e-9


def test_bbox_no_overlap_is_zero():
    assert bbox_intersection_score([0, 0, 1, 1], [2, 2, 3, 3]) == 0.0


def test_bbox_malformed_is_zero():
    assert bbox_intersection_score([0, 0, 1], [0, 0, 1, 1]) == 0.0


def test_extract_json_plain_object():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_fenced_block():
    text = '```json\n{"a": 1, "b": 2}\n```'
    assert extract_json_object(text) == {"a": 1, "b": 2}


def test_extract_json_embedded_in_prose():
    text = 'here you go: {"task": "landcover", "n": [1, 2]} done'
    assert extract_json_object(text) == {"task": "landcover", "n": [1, 2]}


def test_extract_json_invalid_returns_none():
    assert extract_json_object("no json at all") is None
    assert extract_json_object("{not valid}") is None


def test_extract_json_non_object_returns_none():
    # A bare JSON array has no object braces to match.
    assert extract_json_object("[1, 2, 3]") is None
