from __future__ import annotations

import json

from agent.config import ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportRequest
from agent.services.report_service import ReportService


class _RevisionLLM:
    last_status = "ok"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        payload = json.loads(user_prompt)
        compact = "报告编辑要求" in payload
        return json.dumps({
            "summary": "精简后的核心结论。" if compact else "原始报告摘要。",
            "highlights": ["核心要点一", "核心要点二"],
            "analysis": [{"title": "分析结论", "text": "严格保留原始分析事实。"}],
            "recommendations": ["建议继续核查。"],
        }, ensure_ascii=False)


def test_revision_has_unique_urls_and_preserves_map_layers(tmp_path):
    llm = _RevisionLLM()
    service = ReportService(
        config=ReportConfig(report_dir=tmp_path, asset_dir=tmp_path, reuse_existing=True),
        llm=llm,
    )
    request = ReportRequest(
        task="湿地识别",
        region="北京市海淀区",
        prompt="生成湿地报告",
        time_range="2026-02",
        session_id="revision-service",
        custom_model_id="model_wet",
        target_object="湿地",
    )
    analysis = AnalysisResult(
        task="湿地识别",
        region="北京市海淀区",
        time_range="2026-02",
        headline="北京市海淀区2026-02湿地识别分析",
        summary="湿地覆盖率为12%。",
        metrics=[MetricCard("湿地覆盖率", "12%")],
        findings=["湿地集中在选区北部。"],
        recommendations=["建议核查边界。"],
        charts=[ChartAsset(
            "湿地结果", "image", "/reports/assets/wetland.png", "湿地图层",
            [116.2, 39.9, 116.22, 39.92], True, "patch_000024",
        )],
        data_source="haidian_embedding_api",
        aef_payload={"fingerprint": "wetland-result-v1", "used_patch_ids": ["patch_000024"]},
    )
    original = service.build(request, analysis)
    previous = {
        "title": original.title,
        "summary": original.abstract,
        "sections": original.sections,
    }

    first = service.revise(request, analysis, "给我一份精简版", previous)
    second = service.revise(request, analysis, "给我一份精简版", previous)

    assert first.reused is False and second.reused is False
    assert "精简版" in first.title
    assert first.html_url != original.html_url
    assert first.html_url != second.html_url
    assert first.markdown_url != second.markdown_url
    assert first.map_html_url != second.map_html_url
    assert first.charts[0].url == "/reports/assets/wetland.png"
    markdown = (tmp_path / first.markdown_url.removeprefix("/reports/")).read_text(encoding="utf-8")
    assert "精简后的核心结论" in markdown
    revision_payload = json.loads(llm.prompts[-1])
    assert revision_payload["报告编辑要求"] == "给我一份精简版"
    assert revision_payload["上一版报告"]["标题"] == original.title


def test_revision_reuses_legacy_map_url_when_analysis_has_no_layers(tmp_path):
    service = ReportService(
        config=ReportConfig(report_dir=tmp_path, asset_dir=tmp_path, reuse_existing=True),
        llm=_RevisionLLM(),
    )
    request = ReportRequest(
        task="湿地识别",
        region="北京市海淀区",
        prompt="精简报告",
        time_range="2026-02",
        session_id="legacy-revision",
    )
    analysis = AnalysisResult(
        task="湿地识别",
        region="北京市海淀区",
        time_range="2026-02",
        headline="北京市海淀区湿地识别",
        summary="湿地覆盖率为12%。",
        metrics=[MetricCard("湿地覆盖率", "12%")],
        findings=["湿地集中在选区北部。"],
        recommendations=[],
        charts=[],
    )

    revised = service.revise(
        request,
        analysis,
        "给我一份精简版",
        {"artifact": {"map_html_url": "/reports/legacy.map.html"}},
    )

    assert revised.map_html_url == "/reports/legacy.map.html"
