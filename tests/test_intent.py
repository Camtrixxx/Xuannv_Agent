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
    # Mirrors the frontend default: no task pre-selected, region from the map.
    service = IntentService(llm=_NullLLM(), today=TODAY)
    request = ReportRequest(task=kwargs.get("task", ""), region=kwargs.get("region", "雅江区域"), prompt=prompt)
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


def test_report_without_task_flags_task_missing():
    # No task is ever silently defaulted; the agent must ask which task.
    intent = _intent("帮我生成一份报告")
    assert intent.task == ""
    assert "task" in intent.missing_fields
    assert not intent.is_complete


def test_task_named_in_prompt_is_not_missing():
    intent = _intent("雅江区域的水体分布")
    assert intent.task == "水体分布"
    assert "task" not in intent.missing_fields


def test_capability_question_is_free_chat():
    intent = _intent("你能做什么")
    assert intent.message_type == MessageType.FREE_CHAT


def test_region_alias_normalized():
    intent = _intent("海淀区上个月的建筑物提取", region="雅江区域")
    assert intent.region == "北京市海淀区"


def test_followup_question_detected():
    intent = _intent("详细讲讲基于林地占比80.9%的这个结论")
    assert intent.message_type == MessageType.FOLLOW_UP
    assert intent.is_complete  # follow-up needs no slots


def test_rewrite_request_detected_as_followup():
    for prompt in ("给我一份精简版", "用通俗的话重写一下", "这部分再展开详细点", "总结一下"):
        assert _intent(prompt).message_type == MessageType.FOLLOW_UP, prompt


def test_detailed_request_with_new_task_is_not_followup():
    # Naming a concrete task + month means a (new) report, not a discussion.
    intent = _intent("详细分析雅江区域2025年9月的水体分布")
    assert intent.message_type != MessageType.FOLLOW_UP
    assert intent.task == "水体分布"
