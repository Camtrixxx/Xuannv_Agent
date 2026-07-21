"""Single source of truth for the agent's region and task vocabulary.

Everything about *which regions and tasks exist*, how users phrase them, how
they map to each backend's API ids, and which months each region covers lives
here. Other modules (intent parsing, patch selection, region services,
availability checks) import from this file instead of keeping private copies —
so the vocabulary can no longer drift out of sync (previously scattered across
5+ modules).

Coverage sources:
- Yajiang: local AEF quarterly imagery 2023Q1–2026Q1 (verified against the
  running inference service), i.e. months 2023-01 through 2026-03.
- Harbin / Haidian: the embedding-api docs at
  https://github.com/go-bananas-wwj/embedding-api (docs/API.md).
"""

from __future__ import annotations


# --- Regions -------------------------------------------------------------

# User-facing region names recognized by the intent parser.
SUPPORTED_REGIONS = ["雅江区域", "哈尔滨新区", "哈尔滨区域", "北京市海淀区"]

# Free-text alias -> canonical display name (used by the intent parser).
REGION_ALIASES = {
    "哈尔滨": "哈尔滨新区",
    "哈尔滨新区": "哈尔滨新区",
    "哈尔滨区域": "哈尔滨区域",
    "雅江": "雅江区域",
    "雅江区域": "雅江区域",
    "海淀": "北京市海淀区",
    "北京市海淀区": "北京市海淀区",
}

# Exact display/alias -> region id (used by patch selection; "" if unknown).
REGION_IDS = {
    "雅江区域": "yajiang",
    "雅江": "yajiang",
    "yajiang": "yajiang",
    "哈尔滨新区": "harbin",
    "harbin": "harbin",
    "harbin_new_area": "harbin",
    "北京市海淀区": "haidian",
    "海淀区": "haidian",
    "海淀": "haidian",
    "haidian": "haidian",
}


# --- Tasks ---------------------------------------------------------------

# User-facing task names recognized by the intent parser.
SUPPORTED_TASKS = [
    "地物分类",
    "土地覆盖分类",
    "水体分布",
    "水体提取",
    "建筑物提取",
    "道路提取",
    "施工识别",
    "土地利用分类",
    "高程地形",
]

# Free-text alias -> canonical display task name (used by the intent parser).
TASK_ALIASES = {
    "土地覆盖": "土地覆盖分类",
    "土地覆盖分类": "土地覆盖分类",
    "土地利用": "土地利用分类",
    "土地利用分类": "土地利用分类",
    "用地分类": "土地利用分类",
    "用地": "土地利用分类",
    "水体分类": "水体提取",
    "水体提取": "水体提取",
    "水体分布": "水体分布",
    "建筑物": "建筑物提取",
    "建筑提取": "建筑物提取",
    "建筑物提取": "建筑物提取",
    "楼房": "建筑物提取",
    "道路": "道路提取",
    "道路提取": "道路提取",
    "道路识别": "道路提取",
    "施工": "施工识别",
    "施工识别": "施工识别",
    "施工检测": "施工识别",
    "施工地检测": "施工识别",
    "高程": "高程地形",
    "地形": "高程地形",
}

# Display/alias task name -> per-region backend API id.
TASK_TO_HAIDIAN = {
    "建筑物提取": "building_extraction",
    "建筑提取": "building_extraction",
    "道路提取": "road_extraction",
    "道路识别": "road_extraction",
    "施工识别": "construction",
    "施工检测": "construction",
    "施工地检测": "construction",
    "土地利用分类": "land_use_classification",
    "土地利用": "land_use_classification",
    "土地覆盖分类": "land_cover_classification",
    "土地覆盖": "land_cover_classification",
    "水体提取": "water_extraction",
    "水体分布": "water_extraction",
    "水体分类": "water_extraction",
}

TASK_TO_YAJIANG = {
    "地物分类": "landcover",
    "土地覆盖": "landcover",
    "土地覆盖分类": "landcover",
    "水体分布": "water",
    "水体分类": "water",
    "水体提取": "water",
    "高程地形": "dem",
    "高程重建": "dem",
    "地形分析": "dem",
}

TASK_TO_HARBIN = {
    "水体分布": "water_extraction",
    "水体分类": "water_extraction",
    "水体提取": "water_extraction",
    "建筑物提取": "building_extraction",
    "建筑提取": "building_extraction",
    "土地利用分类": "land_use_classification",
    "土地利用": "land_use_classification",
    "用地分类": "land_use_classification",
}

# Harbin tasks whose result comes from the static AEF export (vs. live infer).
STATIC_TASKS = {"building_extraction", "land_use_classification"}
SYSTEM_MODEL_TASKS = {"building_extraction", "water_extraction"}


# --- Month coverage ------------------------------------------------------

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
# Harbin embedding-api: explicit available_months from live patch metadata/API docs.
HARBIN_MONTHS = [
    "2025-04", "2025-06", "2025-08", "2025-09", "2025-10",
    "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
]
# Haidian embedding-api v1: 202512..202605 per the API docs.
HAIDIAN_MONTHS = _months_between("2025-12", "2026-05")

REGION_MONTHS: dict[str, list[str]] = {
    "yajiang": YAJIANG_MONTHS,
    "harbin": HARBIN_MONTHS,
    "haidian": HAIDIAN_MONTHS,
}

REGION_COVERAGE_HINT: dict[str, str] = {
    "yajiang": "雅江区域目前可分析 2023 年 1 月至 2026 年 3 月（按季度更新）",
    "harbin": "哈尔滨新区目前可分析 2025 年 4、6、8、9、10 月，以及 2026 年 1 至 5 月",
    "haidian": "北京市海淀区目前可分析 2025 年 12 月至 2026 年 5 月",
}

# region id -> user-facing task names it supports.
REGION_TASKS: dict[str, list[str]] = {
    "yajiang": ["地物分类", "水体分布", "高程地形"],
    "harbin": ["建筑物提取", "土地利用分类", "水体提取"],
    "haidian": ["建筑物提取", "道路提取", "施工识别", "土地利用分类", "土地覆盖分类", "水体提取"],
}


# --- Native vs. custom analysis objects ----------------------------------
# Land-cover (multiclass) legend for Haidian, from the live
# /system-models/land_cover_classification/classes endpoint.
LAND_COVER_CLASSES = [
    "树木覆盖", "灌木地", "草地", "耕地", "建成区", "裸地/稀疏植被", "永久性水体",
]

# Analysis objects the service can produce WITHOUT any custom annotation, i.e.
# they map to a system task or a land-cover class. Free-text alias -> the
# canonical native concept (used only to answer "is this native?"). Anything
# NOT covered here needs a custom model (see NON_NATIVE_ALIASES).
NATIVE_OBJECTS = {
    # binary system tasks
    "建筑物": "建筑物提取", "建筑": "建筑物提取", "楼房": "建筑物提取",
    "道路": "道路提取", "主干道": "道路提取", "马路": "道路提取",
    "施工": "施工识别", "工地": "施工识别", "建筑工地": "施工识别", "施工地": "施工识别",
    "水体": "水体提取", "水域": "水体提取", "水": "水体提取",
    # land-cover classes (multiclass model)
    "林地": "土地覆盖分类", "树木": "土地覆盖分类", "森林": "土地覆盖分类", "灌木": "土地覆盖分类",
    "草地": "土地覆盖分类", "草坪": "土地覆盖分类",
    "耕地": "土地覆盖分类", "农田": "土地覆盖分类", "农地": "土地覆盖分类",
    "裸地": "土地覆盖分类", "裸土": "土地覆盖分类",
    "建成区": "土地覆盖分类",
}

# Non-native analysis objects that require a CUSTOM model (annotate → train, or
# few-shot similarity recall for small sample counts). Free-text alias ->
# canonical class name used for training/matching. When a user asks for one of
# these and no ready custom model exists, the agent hands off to annotation.
NON_NATIVE_ALIASES = {
    "湿地": "湿地", "沼泽": "湿地",
    "河流": "河流", "河": "河流", "江": "河流",
    "湖泊": "湖泊", "湖": "湖泊",
    "池塘": "池塘", "水塘": "池塘",
    "十字路口": "道路十字路口", "路口": "道路十字路口", "交叉口": "道路十字路口",
    "操场": "操场",
    "机场": "机场", "飞机场": "机场",
    "体育场": "体育场", "运动场": "体育场", "球场": "体育场",
    "垃圾场": "大型垃圾场", "垃圾填埋场": "大型垃圾场", "填埋场": "大型垃圾场",
    "火车站": "火车站", "高铁站": "火车站", "车站": "火车站",
    "停车场": "露天停车场", "露天停车场": "露天停车场", "停车区": "露天停车场",
}

# Custom model statuses that mean "usable now" (system=ready, custom=completed).
READY_MODEL_STATUSES = {"ready", "completed"}
# Statuses that mean "still training, come back later".
TRAINING_MODEL_STATUSES = {"training", "running", "pending", "queued"}
# Statuses that mean the last training attempt failed (offer a retry).
FAILED_MODEL_STATUSES = {"failed", "error"}


def native_object(text: str) -> str:
    """Return the native concept a phrase maps to, or "" if not native."""
    t = str(text or "")
    for alias, concept in NATIVE_OBJECTS.items():
        if alias in t:
            return concept
    return ""


def non_native_object(text: str) -> str:
    """Return the canonical custom class a phrase names, or "" if none."""
    t = str(text or "")
    # Prefer the longest alias match so "露天停车场" beats "停车场".
    for alias in sorted(NON_NATIVE_ALIASES, key=len, reverse=True):
        if alias in t:
            return NON_NATIVE_ALIASES[alias]
    return ""


# --- Accessors -----------------------------------------------------------

def resolve_region_id(region: str) -> str:
    """Best-effort region id from a free-text region name (defaults yajiang)."""
    text = str(region or "")
    if "哈尔滨" in text or text.lower() in {"harbin", "harbin_new_area"}:
        return "harbin"
    if "海淀" in text or text.lower() in {"haidian", "beijing_haidian"}:
        return "haidian"
    if "雅江" in text or text.lower() == "yajiang":
        return "yajiang"
    return "yajiang"


def normalize_task(region_id: str, task: str) -> str:
    """Map a display/alias task name to the region's backend API id."""
    if region_id == "yajiang":
        return TASK_TO_YAJIANG.get(task, task)
    if region_id == "haidian":
        return TASK_TO_HAIDIAN.get(task, task)
    return TASK_TO_HARBIN.get(task, task)
