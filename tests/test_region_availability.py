"""Unit tests for per-region month availability pre-validation."""

from __future__ import annotations

from agent.services.region_availability import (
    is_month_available,
    resolve_region_id,
    unavailable_message,
)


def test_resolve_region_id():
    assert resolve_region_id("雅江区域") == "yajiang"
    assert resolve_region_id("哈尔滨新区") == "harbin"
    assert resolve_region_id("北京市海淀区") == "haidian"
    assert resolve_region_id("haidian") == "haidian"


def test_yajiang_quarterly_coverage():
    assert is_month_available("雅江区域", "2025-09")
    assert is_month_available("雅江区域", "2026-03")  # 2026Q1
    assert not is_month_available("雅江区域", "2026-06")  # 2026Q2 has no imagery
    assert not is_month_available("雅江区域", "2022-12")


def test_harbin_coverage():
    assert is_month_available("哈尔滨新区", "2025-04")
    assert is_month_available("哈尔滨新区", "2025-10")
    assert not is_month_available("哈尔滨新区", "2025-05")
    assert not is_month_available("哈尔滨新区", "2026-01")


def test_haidian_coverage():
    assert is_month_available("北京市海淀区", "2025-12")
    assert is_month_available("北京市海淀区", "2026-05")
    assert not is_month_available("北京市海淀区", "2025-11")
    assert not is_month_available("北京市海淀区", "2026-06")


def test_unavailable_message_is_friendly():
    msg = unavailable_message("雅江区域", "2026-06")
    assert "2026年6月" in msg
    assert "雅江" in msg
    # suggests a valid example month
    assert "比如" in msg
