"""A shortening edit must actually shorten the whole report.

Before this, "精简一下" trimmed only the prose the model writes: the data section
is assembled by the renderer straight from ``analysis``, so it stayed
byte-identical and a 精简版 came out ~20% smaller overall. Worse, every
shortening instruction got the same output contract, so "再精简一点" returned
essentially the previous version — which reads as the edit being ignored.

These tests pin the three things that make a shortening visible: distinct levels,
distinct contracts per level, and renderer-side truncation of the data section
(with the omission stated, never silent).
"""

from __future__ import annotations

import json
import re

from agent.config import ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportRequest
from agent.services.report_service import ReportService
from agent.services.report_skeletons import resolve


class _BudgetLLM:
    """Writes exactly to whichever length budget the contract states.

    Stands in for a model that obeys instructions, so document size becomes a
    direct function of the contract. If a level's budgets don't shrink, the
    rendered report won't shrink either and the assertions below fail — which is
    the actual defect being guarded, not model sloppiness.
    """

    last_status = "ok"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    @staticmethod
    def _budget(spec: object, default: int) -> int:
        found = re.findall(r"不超过(\d+)字", str(spec))
        return min(int(n) for n in found) if found else default

    def complete(self, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        fmt = json.loads(user_prompt)["输出格式"]
        blocks = fmt["analysis"]
        if isinstance(blocks, list):
            titles = [b["title"] for b in blocks]
        else:
            titles = re.findall(r"'([^']+)'", str(blocks))
        body = self._budget(blocks, 300)
        return json.dumps({
            "summary": "结" * self._budget(fmt["summary"], 200),
            "highlights": ["要" * 20] * 4,
            "analysis": [{"title": t, "text": "文" * body} for t in titles],
            "recommendations": ["议" * 25] * 4,
        }, ensure_ascii=False)


def _analysis() -> AnalysisResult:
    return AnalysisResult(
        task="建筑物提取",
        region="北京市海淀区",
        time_range="2026-03",
        headline="北京市海淀区2026-03建筑物提取",
        summary="建筑覆盖率为36.27%。",
        metrics=[MetricCard("建筑覆盖率", "36.27%"), MetricCard("建筑面积", "128.4 公顷")],
        findings=["南部地块更密集。"],
        recommendations=["建议结合地块边界复核。"],
        charts=[ChartAsset(
            "建筑物提取专题结果", "image", "/reports/assets/b.png", "彩色结果图",
            [116.2, 39.9, 116.22, 39.92], True, "patch_000001",
        )],
        data_table=[
            {"label": f"类别{i}", "ratio": 0.1, "value": 10.0} for i in range(8)
        ],
        data_table_title="建筑分布",
        patch_results=[
            {"patch_id": f"patch_{i:06d}", "status": "ok", "metrics": {"coverage_ratio": 0.3}}
            for i in range(8)
        ],
        limitations=["面积按整块 patch 统计。"],
        confidence_notes=["轻量统计指标。"],
        data_source="haidian_embedding_api",
        aef_payload={"fingerprint": "compaction-fixture"},
    )


def _service(tmp_path, llm) -> ReportService:
    return ReportService(
        config=ReportConfig(report_dir=tmp_path, asset_dir=tmp_path, reuse_existing=False),
        llm=llm,
    )


def _request() -> ReportRequest:
    return ReportRequest(
        task="建筑物提取", region="北京市海淀区", prompt="生成报告",
        time_range="2026-03", session_id="compaction",
    )


def _markdown(tmp_path, artifact) -> str:
    name = artifact.markdown_url.removeprefix("/reports/")
    return (tmp_path / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ level routing

def test_compact_levels_separate_first_and_repeated_shortenings():
    level = ReportService._compact_level
    assert level("给我一份精简版") == 1
    assert level("精简一下报告") == 1
    # "再/更/特别" mean "shorter than the last shortening"; without a second level
    # these produced a byte-for-byte rerun of the first one.
    assert level("再精简一点") == 2
    assert level("给我一个特别简短的版本") == 2
    assert level("能不能更简洁一些") == 2


def test_shortening_is_recognised_without_the_word_精简():
    assert ReportService._compact_level("再短一点") == 2
    assert ReportService._compact_level("太长了") == 2


def test_non_shortening_edits_are_level_zero():
    for instruction in ("扩充一下", "换成通俗说法", "补充风险分析", "把建议展开"):
        assert ReportService._compact_level(instruction) == 0, instruction


def test_two_shortenings_are_tellable_apart_in_the_report_list():
    """Same title twice reads as the second edit having done nothing."""
    assert ReportService._revision_label("精简一下") == "精简版"
    assert ReportService._revision_label("再精简一点") == "极简版"


# ------------------------------------------------------------------ the contract

def test_each_level_gets_a_stricter_contract():
    one = ReportService._revision_output_format("精简一下")
    two = ReportService._revision_output_format("再精简一点")
    assert one != two
    # Budgets are upper bounds, not ranges: a range invites writing to its top.
    for spec in (one, two):
        assert "不超过" in spec["summary"]
        assert "-" not in spec["summary"]
    assert _BudgetLLM._budget(two["summary"], 0) < _BudgetLLM._budget(one["summary"], 0)
    assert _BudgetLLM._budget(two["analysis"], 0) < _BudgetLLM._budget(one["analysis"], 0)


def test_shortening_keeps_the_opening_question_and_the_caveats():
    """An edit changes length, never which questions the report answers."""
    spec = ReportService._revision_output_format(
        "再精简一点", skeleton=resolve(task="建筑物提取")
    )
    assert "总体水平" in spec["analysis"]
    assert "数据说明与使用边界" in spec["analysis"]
    # The middle sections are what a shortening gives up.
    assert "高值区在哪里" not in spec["analysis"]


# ------------------------------------------------------------ renderer truncation

def test_data_section_shrinks_with_the_level():
    service = ReportService(config=ReportConfig(reuse_existing=False), llm=_BudgetLLM())
    analysis = _analysis()

    full_table, full_patches, _ = service._compact_data(analysis, 0)
    assert len(full_table) == 8 and len(full_patches) == 8

    _, patches_one, _ = service._compact_data(analysis, 1)
    table_two, patches_two, _ = service._compact_data(analysis, 2)
    assert len(patches_one) == 5
    assert len(table_two) == 4
    # Never zero: an empty table reads as "this run produced no patch data".
    assert 0 < len(patches_two) < len(patches_one)


def test_omitted_patch_rows_are_stated_not_silently_dropped(tmp_path):
    llm = _BudgetLLM()
    service = _service(tmp_path, llm)
    analysis = _analysis()
    service.build(_request(), analysis)
    revised = service.revise(_request(), analysis, "再精简一点", {"summary": "上一版摘要。"})

    markdown = _markdown(tmp_path, revised)
    assert "Patch 处理明细" in markdown
    assert "其余 7 个 patch 明细见完整版报告" in markdown
    html = (tmp_path / revised.html_url.removeprefix("/reports/")).read_text(encoding="utf-8")
    assert "其余 7 个 patch" in html


# ------------------------------------------------------------------- end-to-end

def test_each_shortening_produces_a_visibly_smaller_report(tmp_path):
    """The user-visible property: 精简 < base, and 再精简 < 精简."""
    llm = _BudgetLLM()
    service = _service(tmp_path, llm)
    analysis = _analysis()
    previous = {"summary": "上一版摘要。"}

    base = service.build(_request(), analysis)
    once = service.revise(_request(), analysis, "精简一下报告", previous)
    twice = service.revise(_request(), analysis, "再精简一点", previous)

    sizes = [len(_markdown(tmp_path, a)) for a in (base, once, twice)]
    assert sizes[0] > sizes[1] > sizes[2]
    # A shortening that only trims prose lands around 80% of the original; the
    # point of truncating the data section too is to get materially below that.
    assert sizes[1] < sizes[0] * 0.75
    assert sizes[2] < sizes[0] * 0.55


def test_shortening_keeps_the_headline_evidence(tmp_path):
    """Smaller must not mean gutted: metrics and the caveats always survive."""
    service = _service(tmp_path, _BudgetLLM())
    analysis = _analysis()
    revised = service.revise(
        _request(), analysis, "给我一个特别简短的版本", {"summary": "上一版摘要。"}
    )

    markdown = _markdown(tmp_path, revised)
    assert "36.27%" in markdown
    assert "128.4 公顷" in markdown
    assert "数据说明与使用边界" in markdown
    assert "建筑分布" in markdown
    assert [s["heading"] for s in revised.sections] == ["总体水平", "数据说明与使用边界"]


def test_non_shortening_edit_keeps_the_full_data_section(tmp_path):
    service = _service(tmp_path, _BudgetLLM())
    analysis = _analysis()
    revised = service.revise(_request(), analysis, "换成通俗说法", {"summary": "上一版摘要。"})

    markdown = _markdown(tmp_path, revised)
    assert "其余" not in markdown
    assert markdown.count("| patch_") == 8
