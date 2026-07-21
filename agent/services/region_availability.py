"""Region month/task availability checks used for friendly pre-validation.

The vocabulary itself (which months/tasks each region has) lives in
``agent.taxonomy``; this module is the thin query/validation layer the agent
calls so an unavailable month becomes a clarification *before* any upstream
API call — instead of surfacing a raw HTTP error to the user.
"""

from __future__ import annotations

from agent.taxonomy import (
    HAIDIAN_MONTHS,
    HARBIN_MONTHS,
    REGION_COVERAGE_HINT,
    REGION_MONTHS,
    REGION_TASKS,
    YAJIANG_MONTHS,
    resolve_region_id,
)

__all__ = [
    "HAIDIAN_MONTHS",
    "HARBIN_MONTHS",
    "YAJIANG_MONTHS",
    "REGION_COVERAGE_HINT",
    "REGION_MONTHS",
    "REGION_TASKS",
    "resolve_region_id",
    "available_months",
    "region_tasks",
    "coverage_hint",
    "is_month_available",
    "format_month_zh",
    "unavailable_message",
]


def available_months(region: str) -> list[str]:
    return REGION_MONTHS.get(resolve_region_id(region), [])


def region_tasks(region: str) -> list[str]:
    return REGION_TASKS.get(resolve_region_id(region), [])


def coverage_hint(region: str) -> str:
    return REGION_COVERAGE_HINT.get(resolve_region_id(region), "该区域的可用月份有限")


def is_month_available(region: str, month: str) -> bool:
    months = available_months(region)
    # Unknown coverage (empty list) means "don't pre-validate here".
    return not months or month in months


def format_month_zh(month: str) -> str:
    try:
        year, mon = month.split("-")
        return f"{int(year)}年{int(mon)}月"
    except (ValueError, AttributeError):
        return month


def unavailable_message(region: str, month: str) -> str:
    region_id = resolve_region_id(region)
    hint = REGION_COVERAGE_HINT.get(region_id, "该区域的可用月份有限")
    months = REGION_MONTHS.get(region_id) or []
    example = f"，比如 {format_month_zh(months[-1])}" if months else ""
    return f"{hint}。你说的 {format_month_zh(month)} 暂时没有可用数据，换一个试试{example}。"
