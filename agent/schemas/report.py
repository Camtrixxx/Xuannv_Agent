from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import re
from typing import Any


class AgentStatus:
    IDLE = "idle"
    PARSED = "parsed"
    OK = "ok"
    NEEDS_INPUT = "needs_input"
    NEEDS_CONFIRMATION = "needs_confirmation"
    # A non-native object needs a custom model: the agent hands off to the
    # annotation UI and pauses until the user reports training is done.
    NEEDS_ANNOTATION = "needs_annotation"
    CHAT = "chat"
    ERROR = "error"


class AgentRoute:
    ASK_CLARIFICATION = "ask_clarification"
    ASK_CONFIRMATION = "ask_confirmation"
    CHAT_RESPONSE = "chat_response"
    RUN_ANALYSIS = "run_analysis"


class MessageType:
    REPORT_REQUEST = "report_request"
    SLOT_FILL = "slot_fill"
    FREE_CHAT = "free_chat"
    CHANGE_CONTEXT = "change_context"
    CONFIRMATION = "confirmation"
    # Asking about / discussing / drilling into an already-generated report.
    FOLLOW_UP = "follow_up"


@dataclass(slots=True)
class ReportRequest:
    task: str
    region: str
    prompt: str
    time_range: str = ""
    session_id: str = "default"
    selected_patch_ids: list[str] = field(default_factory=list)
    aoi: dict[str, Any] = field(default_factory=dict)
    # Two-date window for change monitoring (scenario B). Empty for other flows.
    before_time_range: str = ""
    after_time_range: str = ""
    # Custom-model analysis (non-native object). Empty for native tasks.
    custom_model_id: str = ""
    target_object: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReportRequest":
        prompt = str(payload.get("prompt") or "生成一份遥感分析报告")
        raw_time_range = str(payload.get("time_range") or "").strip()
        return cls(
            task=str(payload.get("task") or ""),
            # Region is left empty when unspecified — the "雅江 default" is applied
            # downstream by IntentService, so an empty region stays distinguishable
            # from an explicit 雅江 choice (lets the agent inherit the session's
            # region across turns instead of snapping back to the default).
            region=str(payload.get("region") or ""),
            prompt=prompt,
            time_range=raw_time_range,
            session_id=str(payload.get("session_id") or "default"),
            selected_patch_ids=[str(item) for item in (payload.get("selected_patch_ids") or []) if str(item).strip()],
            aoi=payload.get("aoi") if isinstance(payload.get("aoi"), dict) else {},
            before_time_range=str(payload.get("before_time_range") or "").strip(),
            after_time_range=str(payload.get("after_time_range") or "").strip(),
            custom_model_id=str(payload.get("custom_model_id") or "").strip(),
            target_object=str(payload.get("target_object") or "").strip(),
        )


@dataclass(slots=True)
class AgentIntent:
    message_type: str
    task: str
    region: str
    time_range: str
    user_prompt: str
    missing_fields: list[str] = field(default_factory=list)
    confirmation_fields: list[str] = field(default_factory=list)
    confidence: float = 0.0
    source: str = "rule"
    # Composite scenario (e.g. "checkup" 片区体检). Empty = ordinary single-task report.
    scenario: str = ""
    # Non-native analysis object detected this turn (e.g. "湿地"), and the custom
    # model resolved for it once ready. Empty = ordinary native task.
    target_object: str = ""
    custom_model_id: str = ""
    debug: dict[str, Any] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return len(self.missing_fields) == 0 and len(self.confirmation_fields) == 0


@dataclass(slots=True)
class MetricCard:
    label: str
    value: str
    description: str = ""


@dataclass(slots=True)
class ChartAsset:
    title: str
    kind: str
    url: str
    caption: str
    # When set, the frontend can georeference this image onto a map. bounds are
    # WGS84 [min_lon, min_lat, max_lon, max_lat]; overlay marks it as an
    # on-map result layer (vs. a plain inline figure).
    bounds_wgs84: list[float] = field(default_factory=list)
    overlay: bool = False
    # Optional owner used by multi-patch reports and map layer controls.
    patch_id: str = ""


@dataclass(slots=True)
class AnalysisResult:
    task: str
    region: str
    time_range: str
    headline: str
    summary: str
    metrics: list[MetricCard]
    findings: list[str]
    recommendations: list[str]
    narrative_blocks: list[dict[str, Any]] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    method_notes: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    confidence_notes: list[str] = field(default_factory=list)
    data_source: str = "prototype"
    generated_at: str = ""
    aef_payload: dict[str, Any] = field(default_factory=dict)
    charts: list[ChartAsset] = field(default_factory=list)
    # Optional structured distribution surfaced in the report as a clean table
    # (e.g. land-cover class shares). Each row: {label, ratio, value?}.
    data_table: list[dict[str, Any]] = field(default_factory=list)
    data_table_title: str = ""
    # Optional per-patch records. Existing services can continue to return an
    # empty list while multi-patch regional services expose their detail rows.
    patch_results: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ReportArtifact:
    title: str
    abstract: str
    sections: list[dict[str, Any]]
    metrics: list[MetricCard]
    charts: list[ChartAsset]
    html_url: str
    markdown_url: str
    llm_provider: str = "template"
    reused: bool = False
    generated_at: str = ""
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentResponse:
    status: str
    request: ReportRequest
    intent: AgentIntent | None = None
    message: str = ""
    session_id: str = "default"
    memory: dict[str, Any] = field(default_factory=dict)
    analysis: AnalysisResult | None = None
    report: ReportArtifact | None = None
    # Handoff instruction for the frontend (e.g. open the annotation UI in a new
    # tab). Empty {} for ordinary turns. Shape: {type, url, class_name,
    # model_type, params}. The frontend interprets `type`; the agent never opens
    # tabs itself.
    action: dict[str, Any] = field(default_factory=dict)
    debug: dict[str, Any] = field(default_factory=dict)


def to_dict(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


def infer_two_months(prompt: str, today: date | None = None) -> tuple[str, str]:
    """Extract an ordered (before, after) YYYY-MM pair from a phrase.

    Handles "2025-12 到 2026-05", "2025年12月和2026年5月", etc. Returns the two
    earliest-vs-latest distinct months found (sorted), or ("","") if fewer than
    two are present. Used by the change-monitoring scenario.
    """
    text = (prompt or "").strip()
    found: list[str] = []
    # Explicit YYYY-MM tokens.
    for m in re.finditer(r"(20\d{2})[-/.](0[1-9]|1[0-2])", text):
        found.append(f"{m.group(1)}-{m.group(2)}")
    # 年X月 tokens.
    for m in re.finditer(r"(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月", text):
        found.append(f"{m.group(1)}-{int(m.group(2)):02d}")
    # Dedup preserving nothing but value, then sort chronologically.
    uniq = sorted(set(found))
    if len(uniq) < 2:
        return ("", "")
    return (uniq[0], uniq[-1])


def infer_time_range(prompt: str, today: date | None = None) -> str:
    text = prompt.strip()
    current = today or date.today()
    month_map = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
        "十一": 11,
        "十二": 12,
    }

    # Explicit YYYY-MM / YYYY/MM tokens win outright (e.g. "就看2025-12的").
    iso = re.search(r"(20\d{2})[-/.](0[1-9]|1[0-2])", text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}"

    year = current.year - 1 if "去年" in text else current.year
    if "前年" in text:
        year = current.year - 2
    if "明年" in text:
        year = current.year + 1
    year_match = re.search(r"(20\d{2})\s*年", text)
    if year_match:
        year = int(year_match.group(1))

    if "上个月" in text or "上月" in text:
        month = current.month - 1
        if month == 0:
            return f"{current.year - 1}-12"
        return f"{current.year}-{month:02d}"

    if "这个月" in text or "本月" in text or "当月" in text:
        return f"{current.year}-{current.month:02d}"

    numeric_month = re.search(r"(?<!\d)(1[0-2]|0?[1-9])\s*月", text)
    if numeric_month:
        return f"{year}-{int(numeric_month.group(1)):02d}"

    for zh_month in sorted(month_map, key=len, reverse=True):
        if f"{zh_month}月" in text or f"{zh_month}月份" in text:
            return f"{year}-{month_map[zh_month]:02d}"

    return ""
