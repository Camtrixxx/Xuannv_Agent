"""Report shape must follow the kind of analysis, not one global template.

Before skeletons, every report — a building-coverage run, a two-date change
comparison, a tree-planting priority ranking — was poured into the same four
sections, so they all read alike no matter how different the underlying analysis
was. These tests pin the routing and the invariants that keep each shape usable.
"""

from __future__ import annotations

import json
import re

from agent.config import ReportConfig
from agent.schemas.report import AnalysisResult, MetricCard, ReportRequest
from agent.services import report_skeletons
from agent.services.report_service import ReportService


def _headings(skeleton) -> list[str]:
    return [item["heading"] for item in skeleton.section_plan()]


def test_each_task_family_gets_its_own_shape():
    building = report_skeletons.resolve(task="建筑物提取")
    landcover = report_skeletons.resolve(task="土地覆盖分类")
    terrain = report_skeletons.resolve(task="高程地形")

    assert building.key == "binary_extraction"
    assert landcover.key == "multiclass"
    assert terrain.key == "terrain"
    # The whole point: different analyses ask different questions.
    assert _headings(building) != _headings(landcover) != _headings(terrain)


def test_scenario_outranks_task_name():
    """A checkup aggregates several tasks; its shape must not be overridden."""
    checkup = report_skeletons.resolve(task="建筑物提取", scenario="checkup")
    assert checkup.key == "checkup"
    assert "四个专题横向对比" in _headings(checkup)

    change = report_skeletons.resolve(task="施工识别·建设扰动监测", scenario="change")
    assert change.key == "change"
    assert "变了多少" in _headings(change)

    score = report_skeletons.resolve(task="补绿优先区评分", scenario="score")
    assert score.key == "score"
    assert "最该补绿的地块" in _headings(score)


def test_custom_object_gets_reliability_forward_shape():
    """A freshly trained model's reader is validating it, not consuming it."""
    skeleton = report_skeletons.resolve(task="湿地识别", custom_object="湿地")
    assert skeleton.key == "custom_object"
    assert "结果可信度与下一步" in _headings(skeleton)


def test_unknown_task_falls_back_to_generic():
    assert report_skeletons.resolve(task="某种没见过的任务").key == "generic"
    assert report_skeletons.resolve().key == "generic"


def test_every_skeleton_ends_with_plain_language_caveats():
    """Technical boundaries belong last, in the same place, in every report."""
    for skeleton in report_skeletons.all_skeletons():
        plan = skeleton.section_plan()
        assert plan[-1]["heading"] == "数据说明与使用边界", skeleton.key
        assert len(plan) >= 3, skeleton.key
        fmt = skeleton.output_format()
        # The contract must enumerate exactly the planned sections.
        assert isinstance(fmt["analysis"], list)
        assert len(fmt["analysis"]) == len(plan), skeleton.key
        assert [b["title"] for b in fmt["analysis"]] == [i["heading"] for i in plan]


class _EchoLLM:
    """Answers with exactly the sections the output contract asked for.

    Stands in for a model that obeys the contract, so a failing assertion means
    the contract itself is wrong rather than the model being sloppy. The build
    contract lists sections as objects; the revision contract states them in
    prose (``只保留这些小节，顺序不变：['总体水平', ...]``), so both are read.
    """

    last_status = "ok"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []

    def complete(self, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        self.system_prompts.append(system_prompt)
        blocks = json.loads(user_prompt)["输出格式"]["analysis"]
        if isinstance(blocks, list):
            titles = [b["title"] for b in blocks]
        else:
            titles = re.findall(r"'([^']+)'", str(blocks))
        return json.dumps({
            "summary": "摘要。",
            "highlights": ["要点一"],
            "analysis": [{"title": t, "text": f"{t}的正文。"} for t in titles],
            "recommendations": ["建议一。"],
        }, ensure_ascii=False)


def _analysis(task: str, scenario: str = "") -> AnalysisResult:
    payload = {"fingerprint": f"skeleton-{task}-{scenario}"}
    if scenario:
        payload["scenario"] = scenario
    return AnalysisResult(
        task=task,
        region="北京市海淀区",
        time_range="2026-03",
        headline=f"北京市海淀区{task}",
        summary="覆盖率为36.27%。",
        metrics=[MetricCard("覆盖率", "36.27%")],
        findings=["南部更密。"],
        recommendations=["建议结合地块边界复核。"],
        limitations=["面积按整块统计。"],
        confidence_notes=["轻量统计指标。"],
        aef_payload=payload,
    )


def _service(tmp_path, llm):
    return ReportService(
        config=ReportConfig(report_dir=tmp_path, asset_dir=tmp_path, reuse_existing=False),
        llm=llm,
    )


def _request(task: str) -> ReportRequest:
    return ReportRequest(
        task=task, region="北京市海淀区", prompt="生成报告",
        time_range="2026-03", session_id="skeleton",
    )


def test_rendered_report_uses_the_task_skeleton(tmp_path):
    llm = _EchoLLM()
    artifact = _service(tmp_path, llm).build(_request("建筑物提取"), _analysis("建筑物提取"))

    assert [s["heading"] for s in artifact.sections] == [
        "总体水平", "高值区在哪里", "能用来做什么", "数据说明与使用边界",
    ]
    # The per-section brief and length budget must reach the model, not just the
    # heading — the brief is what makes 总体水平 read differently per task family.
    contract = json.loads(llm.prompts[0])["输出格式"]["analysis"]
    assert all(block["text"].strip() for block in contract)
    assert any("字" in block["text"] for block in contract)
    assert artifact.llm_provider == "deepseek"


def test_rendered_scenario_report_differs_from_task_report(tmp_path):
    llm = _EchoLLM()
    service = _service(tmp_path, llm)
    plain = service.build(_request("建筑物提取"), _analysis("建筑物提取"))
    checkup = service.build(_request("片区综合体检"), _analysis("片区综合体检", "checkup"))

    plain_headings = [s["heading"] for s in plain.sections]
    checkup_headings = [s["heading"] for s in checkup.sections]
    assert plain_headings != checkup_headings
    assert "四个专题横向对比" in checkup_headings
    # The tone instructions differ too, not just the section list: each skeleton
    # tells the model a different reader question and audience.
    assert llm.system_prompts[0] != llm.system_prompts[1]
    assert "片区管理者" in llm.system_prompts[1]


def test_all_skeleton_sections_survive_parsing(tmp_path):
    """A skeleton longer than the old hardcoded cap must not be truncated."""
    llm = _EchoLLM()
    for skeleton in report_skeletons.all_skeletons():
        analysis = _analysis("建筑物提取", skeleton.key if skeleton.key in {"checkup", "change", "score"} else "")
        analysis.task = "建筑物提取"
        artifact = _service(tmp_path, llm).build(_request("建筑物提取"), analysis)
        assert len(artifact.sections) >= 3


def test_compact_revision_keeps_opening_and_caveats(tmp_path):
    """An edit changes length and tone, never which questions are answered."""
    llm = _EchoLLM()
    service = _service(tmp_path, llm)
    analysis = _analysis("建筑物提取")
    service.build(_request("建筑物提取"), analysis)
    revised = service.revise(_request("建筑物提取"), analysis, "精简一下", {"summary": "上一版摘要。"})

    headings = [s["heading"] for s in revised.sections]
    assert headings == ["总体水平", "数据说明与使用边界"]


def test_expanded_revision_keeps_the_full_skeleton(tmp_path):
    llm = _EchoLLM()
    service = _service(tmp_path, llm)
    analysis = _analysis("建筑物提取")
    revised = service.revise(_request("建筑物提取"), analysis, "详细扩充一下", {"summary": "上一版摘要。"})

    assert [s["heading"] for s in revised.sections] == [
        "总体水平", "高值区在哪里", "能用来做什么", "数据说明与使用边界",
    ]


def test_template_fallback_also_follows_the_skeleton(tmp_path):
    """A template report should still read as the right kind of report."""
    class _DeadLLM:
        last_status = "missing_api_key"

        def complete(self, system_prompt, user_prompt):
            return None

    artifact = _service(tmp_path, _DeadLLM()).build(
        _request("片区综合体检"), _analysis("片区综合体检", "checkup")
    )

    headings = [s["heading"] for s in artifact.sections]
    assert headings[0] == "一句话结论"
    assert "数据说明与使用边界" in headings
    assert artifact.llm_provider == "template:missing_api_key"
