"""Change-scenario two-month extraction: rule-first, LLM semantic fallback.

The frontend API is unchanged — before/after_time_range stay optional. When the
prompt phrases the two months fuzzily ("今年一月到三月", "2026-1和2026-3"), rule
parsing (infer_two_months) misses them, so _merge_change falls back to the chat
LLM. These tests use scriptable/mute LLMs (no network) and an isolated on-disk
MemoryService.
"""

from __future__ import annotations

import json

from agent.config import MemoryConfig
from agent.graph.report_agent import ReportAgent
from agent.schemas.report import AgentStatus, ReportRequest
from agent.services.capability_service import Capability, NATIVE
from agent.services.llm_provider import LLMProvider
from agent.services.memory_service import MemoryService


class _MuteLLM(LLMProvider):
    """Never returns anything — proves rule hits don't need the LLM, and that an
    empty LLM reply degrades gracefully to a clarification ask."""

    def __init__(self):
        self.calls = 0

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        return ""


class _ScriptedLLM(LLMProvider):
    """Returns a fixed (before, after) JSON, mimicking DeepSeek's semantic read."""

    def __init__(self, before: str, after: str):
        self.before, self.after = before, after
        self.calls = 0

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        return json.dumps({"before": self.before, "after": self.after})


class _FakeCapability:
    annotation_ui_base = "http://ui.test"

    def resolve(self, region, target_object, *, model_type="single_time_detection", refresh=False):
        from agent.taxonomy import resolve_region_id
        return Capability(kind=NATIVE, target_object=target_object,
                          region_id=resolve_region_id(region))

    def annotation_action(self, cap, **kw):
        return {}


class _StubAnalysis:
    def analyze(self, request):
        from agent.schemas.report import AnalysisResult
        return AnalysisResult(
            task=request.task or "分析", region=request.region, time_range=request.time_range,
            headline="stub", summary="stub", metrics=[], findings=[], recommendations=[],
        )


class _StubReport:
    def build(self, request, analysis):
        from agent.schemas.report import ReportArtifact
        return ReportArtifact(title="stub", abstract="stub", sections=[], metrics=[],
                              charts=[], html_url="/x.html", markdown_url="/x.md")


AOI = {"type": "bbox", "coordinates": [116.20, 39.88, 116.26, 39.92]}


def _agent(tmp_path, chat_llm):
    mem = MemoryService(MemoryConfig(db_path=tmp_path / "mem.sqlite3"))
    stub = _StubAnalysis()
    return ReportAgent(
        memory_service=mem,
        chat_llm=chat_llm,
        capability_service=_FakeCapability(),
        analysis_service=stub,
        change_service=stub,
        checkup_service=stub,
        score_service=stub,
        report_service=_StubReport(),
    )


def _req(prompt, sid, **kw):
    return ReportRequest.from_dict(
        {"prompt": prompt, "region": "北京市海淀区", "session_id": sid, **kw}
    )


def test_rule_hit_needs_no_llm(tmp_path):
    # Well-formed months → rule parser wins, LLM must NOT be called.
    llm = _MuteLLM()
    agent = _agent(tmp_path, llm)
    r = agent.run(_req("对比2025-12和2026-05的建设扰动", "c1", aoi=AOI))
    assert r.status == AgentStatus.OK
    assert r.intent.time_range == "2025-12→2026-05"
    assert llm.calls == 0


def test_llm_recovers_one_digit_months(tmp_path):
    # "2026-1和2026-3" — one-digit months rule regex misses → LLM fallback.
    agent = _agent(tmp_path, _ScriptedLLM("2026-01", "2026-03"))
    r = agent.run(_req("对比2026-1和2026-3的变化", "c2", aoi=AOI))
    assert r.status == AgentStatus.OK
    assert r.intent.time_range == "2026-01→2026-03"


def test_llm_recovers_relative_chinese(tmp_path):
    # "今年一月份到三月份" — relative year + Chinese numerals → LLM fallback.
    agent = _agent(tmp_path, _ScriptedLLM("2026-01", "2026-03"))
    r = agent.run(_req("帮我做今年一月份到三月份的变化检测", "c3", aoi=AOI))
    assert r.status == AgentStatus.OK
    assert r.intent.time_range == "2026-01→2026-03"


def test_llm_orders_reversed_pair(tmp_path):
    # Model returns later month first → we still emit chronological order.
    agent = _agent(tmp_path, _ScriptedLLM("2026-03", "2026-01"))
    r = agent.run(_req("看看三月和一月的建设变化", "c4", aoi=AOI))
    assert r.status == AgentStatus.OK
    assert r.intent.time_range == "2026-01→2026-03"


def test_empty_llm_degrades_to_ask(tmp_path):
    # No months anywhere and the LLM yields nothing → graceful clarification ask,
    # not a crash.
    llm = _MuteLLM()
    agent = _agent(tmp_path, llm)
    r = agent.run(_req("帮我做个建设扰动监测", "c5", aoi=AOI))
    assert r.status == AgentStatus.NEEDS_INPUT
    assert "月份" in r.message
    assert llm.calls >= 1  # the fallback was attempted


def test_llm_identical_months_rejected(tmp_path):
    # A degenerate pair (same month twice) is not a valid two-date window → ask.
    agent = _agent(tmp_path, _ScriptedLLM("2026-03", "2026-03"))
    r = agent.run(_req("对比这两个月", "c6", aoi=AOI))
    assert r.status == AgentStatus.NEEDS_INPUT
