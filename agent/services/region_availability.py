"""Single source of truth for which months each region can actually analyze.

Used for pre-validation in the agent so an unavailable month is turned into a
friendly clarification *before* any model/API call — instead of surfacing a raw
upstream HTTP error to the user.

Coverage sources:
- Yajiang: local AEF quarterly imagery 2023Q1–2026Q1 (verified against the
  running inference service), i.e. months 2023-01 through 2026-03.
- Harbin / Haidian: the embedding-api docs at
  https://github.com/go-bananas-wwj/embedding-api (docs/API.md).
"""

from __future__ import annotations


def _months_between(start: str, end: str) -> list[str]:
    sy, sm = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out: list[str] = []
    year, month = sy, sm
    while (year, month) <= (ey, em):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


# Yajiang AEF: quarterly imagery 2023Q1..2026Q1 -> every month in that span.
YAJIANG_MONTHS = _months_between("2023-01", "2026-03")
# Harbin embedding-api: explicit available_months from the API docs.
HARBIN_MONTHS = ["2025-04", "2025-06", "2025-08", "2025-09", "2025-10"]
# Haidian embedding-api v1: 202512..202605 per the API docs.
HAIDIAN_MONTHS = _months_between("2025-12", "2026-05")

REGION_MONTHS: dict[str, list[str]] = {
    "yajiang": YAJIANG_MONTHS,
    "harbin": HARBIN_MONTHS,
    "haidian": HAIDIAN_MONTHS,
}

REGION_COVERAGE_HINT: dict[str, str] = {
    "yajiang": "雅江区域目前可分析 2023 年 1 月至 2026 年 3 月（按季度更新）",
    "harbin": "哈尔滨新区目前可分析 2025 年 4、6、8、9、10 月",
    "haidian": "北京市海淀区目前可分析 2025 年 12 月至 2026 年 5 月",
}


def resolve_region_id(region: str) -> str:
    text = str(region or "")
    if "哈尔滨" in text or text.lower() in {"harbin", "harbin_new_area"}:
        return "harbin"
    if "海淀" in text or text.lower() in {"haidian", "beijing_haidian"}:
        return "haidian"
    if "雅江" in text or text.lower() == "yajiang":
        return "yajiang"
    return "yajiang"


def available_months(region: str) -> list[str]:
    return REGION_MONTHS.get(resolve_region_id(region), [])


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
