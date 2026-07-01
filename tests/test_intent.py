"""Unit tests for month inference and rule-based intent parsing.

These exercise the deterministic, network-free paths: DeepSeekProvider returns
None without an API key, and confident rule parses skip the LLM entirely.
"""

from __future__ import annotations

from datetime import date

from agent.schemas.report import MessageType, ReportRequest, infer_time_range
from agent.services.intent_service import IntentService
from agent.services.llm_provider import LLMProvider

TODAY = date(2026, 7, 1)


class _NullLLM(LLMProvider):
    """Offline stub: forces the deterministic rule path (never calls network)."""

    last_status = "not_called"

    def complete(self, system_prompt: str, user_prompt: str) -> str | None:
        return None


def test_infer_last_year_chinese_month():
    assert infer_time_range("去年九月份的水体报告", today=TODAY) == "2025-09"


def test_infer_explicit_year_month():
    assert infer_time_range("2025年9月", today=TODAY) == "2025-09"


def test_infer_last_month_rolls_over_year():
    assert infer_time_range("上个月的情况", today=date(2026, 1, 15)) == "2025-12"


def test_infer_this_month():
    assert infer_time_range("本月", today=TODAY) == "2026-07"


def test_infer_numeric_month():
    assert infer_time_range("给我10月的报告", today=TODAY) == "2026-10"


def test_infer_no_month_returns_empty():
    assert infer_time_range("生成一份遥感分析报告", today=TODAY) == ""


def _intent(prompt: str, **kwargs):
    service = IntentService(llm=_NullLLM(), today=TODAY)
    request = ReportRequest(task=kwargs.get("task", "地物分类"), region=kwargs.get("region", "雅江区域"), prompt=prompt)
    return service.parse(request)


def test_report_request_with_month_is_complete():
    intent = _intent("生成雅江区域去年九月的地物分类报告")
    assert intent.time_range == "2025-09"
    assert intent.is_complete
    assert "time_range" not in intent.missing_fields


def test_report_request_without_month_flags_missing():
    intent = _intent("生成一份地物分类报告")
    assert intent.time_range == ""
    assert "time_range" in intent.missing_fields
    assert not intent.is_complete


def test_capability_question_is_free_chat():
    intent = _intent("你能做什么")
    assert intent.message_type == MessageType.FREE_CHAT


def test_region_alias_normalized():
    intent = _intent("海淀区上个月的建筑物提取", region="雅江区域")
    assert intent.region == "北京市海淀区"
