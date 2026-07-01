"""Unit tests for the shared service helpers."""

from __future__ import annotations

from agent.services.common import bbox_intersection_score, extract_json_object, strip_markdown


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


def test_strip_markdown_removes_bold_and_headings():
    assert strip_markdown("**解答疑问**：内容") == "解答疑问：内容"
    assert strip_markdown("# 标题") == "标题"
    assert strip_markdown("使用 `code` 片段") == "使用 code 片段"


def test_strip_markdown_bullets_become_dots():
    out = strip_markdown("- 林地占比80.9%\n- 水体19.1%")
    assert out == "• 林地占比80.9%\n• 水体19.1%"


def test_strip_markdown_keeps_numbered_lists():
    out = strip_markdown("1. **要点一**\n2. 要点二")
    assert out == "1. 要点一\n2. 要点二"


def test_strip_markdown_plain_text_unchanged():
    assert strip_markdown("你好，我可以帮你生成报告。") == "你好，我可以帮你生成报告。"
