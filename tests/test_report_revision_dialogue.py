from __future__ import annotations

from agent.config import MemoryConfig
from agent.graph.report_agent import ReportAgent
from agent.schemas.report import (
    AgentStatus,
    AnalysisResult,
    ChartAsset,
    MessageType,
    MetricCard,
    ReportArtifact,
    ReportRequest,
)
from agent.services.capability_service import Capability, NATIVE
from agent.services.llm_provider import LLMProvider
from agent.services.memory_service import MemoryService


class _ChatLLM(LLMProvider):
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        return "林地占比来自报告中的分类结果，可以结合结果图继续核查。"


class _NativeCapability:
    def resolve(self, region, target_object, **kwargs):
        return Capability(kind=NATIVE, target_object=target_object, region_id="yajiang")

    def annotation_action(self, cap, **kwargs):  # pragma: no cover
        return {}


class _RecordingAnalysis:
    def __init__(self) -> None:
        self.requests: list[ReportRequest] = []

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        self.requests.append(request)
        return AnalysisResult(
            task=request.task,
            region=request.region,
            time_range=request.time_range,
            headline=f"{request.region}{request.time_range}{request.task}",
            summary="林地占比80%，建设用地占比20%。",
            metrics=[MetricCard("林地占比", "80%", "按选区统计")],
            findings=["林地是当前选区的主导类别。"],
            recommendations=["建议关注林地边缘变化。"],
            charts=[ChartAsset("分类结果", "image", "/reports/assets/result.png", "分类图")],
            data_source="aef_inference",
            aef_payload={"fingerprint": f"analysis-{len(self.requests)}", "used_patch_ids": ["patch_000001"]},
        )


class _RevisionReportService:
    def __init__(self) -> None:
        self.build_calls = []
        self.revise_calls = []

    def build(self, request, analysis):
        self.build_calls.append((request, analysis))
        index = len(self.build_calls)
        return ReportArtifact(
            title=f"{analysis.headline}报告",
            abstract=analysis.summary,
            sections=[{"heading": "主要发现", "body": analysis.findings[0]}],
            metrics=analysis.metrics,
            charts=analysis.charts,
            html_url=f"/reports/original-{index}.html",
            markdown_url=f"/reports/original-{index}.md",
            map_html_url=f"/reports/original-{index}.map.html",
        )

    def revise(self, request, analysis, instruction, previous_context=None):
        self.revise_calls.append((request, analysis, instruction, previous_context))
        index = len(self.revise_calls)
        return ReportArtifact(
            title=f"{analysis.headline}·精简版报告",
            abstract="林地占比80%，为当前选区主导类别。",
            sections=[{"heading": "精简结论", "body": "林地占主导。"}],
            metrics=analysis.metrics,
            charts=analysis.charts,
            html_url=f"/reports/revision-{index}.html",
            markdown_url=f"/reports/revision-{index}.md",
            map_html_url=f"/reports/revision-{index}.map.html",
        )


def _agent(tmp_path):
    analysis = _RecordingAnalysis()
    reports = _RevisionReportService()
    agent = ReportAgent(
        memory_service=MemoryService(MemoryConfig(db_path=tmp_path / "memory.sqlite3")),
        chat_llm=_ChatLLM(),
        capability_service=_NativeCapability(),
        analysis_service=analysis,
        change_service=analysis,
        checkup_service=analysis,
        score_service=analysis,
        custom_model_service=analysis,
        report_service=reports,
    )
    return agent, analysis, reports


def _request(prompt, session_id="revision-session", **kwargs):
    return ReportRequest.from_dict({"session_id": session_id, "prompt": prompt, **kwargs})


def test_report_edit_creates_new_artifact_without_rerunning_analysis(tmp_path):
    agent, analysis, reports = _agent(tmp_path)
    original = agent.run(_request("生成雅江区域2025年9月地物分类报告"))
    assert original.status == AgentStatus.OK
    assert len(analysis.requests) == 1

    revised = agent.run(_request("给我一份精简版"))

    assert revised.status == AgentStatus.OK
    assert revised.intent.message_type == MessageType.REPORT_EDIT
    assert revised.message == "已按你的要求生成新版报告。"
    assert revised.report is not None
    assert revised.report.html_url == "/reports/revision-1.html"
    assert revised.report.html_url != original.report.html_url
    assert revised.report.charts[0].url == "/reports/assets/result.png"
    assert len(analysis.requests) == 1
    assert len(reports.revise_calls) == 1
    revision_request, revision_analysis, instruction, previous = reports.revise_calls[0]
    assert revision_request.task == "地物分类"
    assert revision_request.region == "雅江区域"
    assert revision_request.time_range == "2025-09"
    assert instruction == "给我一份精简版"
    assert revision_analysis.summary == "林地占比80%，建设用地占比20%。"
    assert previous["artifact"]["html_url"] == original.report.html_url
    assert len(revised.memory["reports"]) == 2


def test_report_question_stays_natural_language_without_new_report(tmp_path):
    agent, analysis, reports = _agent(tmp_path)
    agent.run(_request("生成雅江区域2025年9月地物分类报告"))

    response = agent.run(_request("为什么林地占比这么高"))

    assert response.status == AgentStatus.CHAT
    assert response.intent.message_type == MessageType.FOLLOW_UP
    assert response.report is None
    assert "林地占比" in response.message
    assert len(analysis.requests) == 1
    assert reports.revise_calls == []


def test_task_change_after_revision_generates_default_report(tmp_path):
    agent, analysis, reports = _agent(tmp_path)
    agent.run(_request("生成雅江区域2025年9月地物分类报告"))
    agent.run(_request("给我一份精简版"))

    changed = agent.run(_request("换成2025年9月水体分布"))

    assert changed.status == AgentStatus.OK
    assert changed.intent.message_type == MessageType.CHANGE_CONTEXT
    assert changed.report is not None
    assert len(analysis.requests) == 2
    assert analysis.requests[-1].task == "水体分布"
    assert analysis.requests[-1].region == "雅江区域"
    assert len(reports.build_calls) == 2


def test_legacy_report_context_can_still_be_revised():
    analysis = ReportAgent._analysis_from_report_context({
        "title": "北京市海淀区湿地识别报告",
        "region": "北京市海淀区",
        "task": "湿地识别",
        "time_range": "2026-02",
        "summary": "湿地覆盖率为12%。",
        "metrics": [{"label": "湿地覆盖率", "value": "12%"}],
        "sections": [{"heading": "主要发现", "body": "湿地集中在选区北部。"}],
        "distribution": [{"class_name": "湿地", "ratio": 0.12}],
        "used_patch_ids": ["patch_000024"],
    })

    assert analysis is not None
    assert analysis.region == "北京市海淀区"
    assert analysis.task == "湿地识别"
    assert analysis.summary == "湿地覆盖率为12%。"
    assert analysis.metrics[0].value == "12%"
    assert analysis.findings == ["湿地集中在选区北部。"]
    assert analysis.aef_payload["used_patch_ids"] == ["patch_000024"]
