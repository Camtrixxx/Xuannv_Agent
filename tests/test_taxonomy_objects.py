"""Tests for object detection in taxonomy: native vs. non-native classification.

The core concern here is the substring-collision class of bug: Chinese has no
word boundaries, so a place name can share characters with an object alias (the
"江"→河流 alias fires on 雅"江"). Region names are stripped before matching so a
whole class of such false positives is removed at the source — every caller of
native_object / non_native_object benefits, not just one call site.
"""

from __future__ import annotations

from agent.taxonomy import native_object, non_native_object, region_from_bbox


def test_region_name_does_not_trip_object_alias():
    # 雅"江" must not be read as the "江"→河流 non-native object.
    assert non_native_object("雅江2025年6月的地物分类") == ""
    assert non_native_object("雅江的水体分布") == ""
    # 海淀 / 哈尔滨 likewise carry no object substrings that should fire.
    assert non_native_object("海淀2026年3月的建筑物提取") == ""
    assert non_native_object("哈尔滨新区的土地利用分类") == ""


def test_real_non_native_object_still_detected():
    assert non_native_object("帮我在海淀这块地识别一下湿地") == "湿地"
    assert non_native_object("看看海淀的河流") == "河流"
    assert non_native_object("识别一下机场") == "机场"


def test_object_word_survives_region_strip():
    # Region stripped, but a genuine object elsewhere in the sentence still fires,
    # and the longest alias wins over an incidental short substring.
    assert non_native_object("哈尔滨的江边有没有湿地") == "湿地"
    assert non_native_object("露天停车场在哪") == "露天停车场"  # beats "停车场"


def test_native_object_prefers_longest_alias():
    # "土地覆盖分类" must not be shadowed by a shorter substring match.
    assert native_object("海淀的建筑物") == "建筑物提取"
    assert native_object("雅江的树木") == "土地覆盖分类"


def test_native_and_non_native_are_disjoint_on_region_names():
    # A bare region name resolves to neither an object bucket.
    for name in ["雅江", "海淀", "哈尔滨新区", "北京市海淀区"]:
        assert native_object(name) == ""
        assert non_native_object(name) == ""


# ------------------------------------------------------- AOI -> region inference
# A frontend map AOI whose centre lands unambiguously in one region's box lets
# the agent recover the region when the user names none — no more "是雅江吗?" on
# framed 海淀 coordinates.


def _bbox(min_lng, min_lat, max_lng, max_lat):
    return {"type": "bbox", "coordinates": [min_lng, min_lat, max_lng, max_lat]}


def test_region_from_bbox_haidian():
    # A small box inside the Haidian footprint resolves to 海淀.
    assert region_from_bbox(_bbox(116.20, 39.88, 116.26, 39.92)) == "北京市海淀区"


def test_region_from_bbox_harbin():
    assert region_from_bbox(_bbox(126.40, 45.80, 126.50, 45.90)) == "哈尔滨新区"


def test_region_from_bbox_outside_all_returns_empty():
    # Open ocean — centre in no region's box → "" (caller keeps its default).
    assert region_from_bbox(_bbox(0.0, 0.0, 1.0, 1.0)) == ""


def test_region_from_bbox_rejects_malformed():
    assert region_from_bbox({}) == ""
    assert region_from_bbox({"type": "bbox", "coordinates": [1, 2]}) == ""
    assert region_from_bbox({"name": "雅江区域"}) == ""
    assert region_from_bbox(None) == ""


def test_region_from_bbox_sloppy_rectangle_still_resolves():
    # A rectangle that spills past Haidian's edge but is still centred on it
    # resolves — centre-in-box, not strict containment.
    assert region_from_bbox(_bbox(116.30, 40.05, 116.50, 40.20)) == "北京市海淀区"
