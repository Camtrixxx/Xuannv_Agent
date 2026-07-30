"""The report content layer must accept every JSON shape the LLM really returns.

Regression cover for a silent failure: a revision prompt echoes the previous
version's stored sections, which use ``heading``/``body``. The model copied those
key names, the parser only accepted ``title``/``text``, so a perfectly good
response was discarded and the hardcoded template rendered instead — while
``llm_provider`` still claimed ``deepseek``, hiding it from the API.
"""

from __future__ import annotations

import json

from agent.config import ReportConfig
from agent.schemas.report import AnalysisResult, MetricCard, ReportRequest
from agent.services.report_service import ReportService


class _ShapeLLM:
    """Returns a caller-supplied ``analysis``/``recommendations`` payload."""

    last_status = "ok"

    def __init__(self, analysis, recommendations=None) -> None:
        self._analysis = analysis
        self._recommendations = recommendations or ["建议核查边界。"]
        self.prompts: list[str] = []

    def complete(self, system_prompt, user_prompt):
        self.prompts.append(user_prompt)
        return json.dumps({
            "summary": "模型给出的摘要。",
            "highlights": ["要点一", "要点二"],
            "analysis": self._analysis,
            "recommendations": self._recommendations,
        }, ensure_ascii=False)


def _service(tmp_path, llm):
    return ReportService(
        config=ReportConfig(report_dir=tmp_path, asset_dir=tmp_path, reuse_existing=False),
        llm=llm,
    )


def _request():
    return ReportRequest(
        task="建筑物提取",
        region="北京市海淀区",
        prompt="生成建筑物报告",
        time_range="2026-03",
        session_id="content-shapes",
    )


def _analysis():
    return AnalysisResult(
        task="建筑物提取",
        region="北京市海淀区",
        time_range="2026-03",
        headline="北京市海淀区2026-03建筑物提取分析",
        summary="建筑物覆盖率为36.27%。",
        metrics=[MetricCard("建筑物覆盖率", "36.27%")],
        findings=["建筑集中在选区南部。"],
        recommendations=["建议结合地块边界复核。"],
        aef_payload={"fingerprint": "shape-fixture"},
    )


def test_heading_body_blocks_are_accepted(tmp_path):
    """The exact shape the revision prompt used to teach the model."""
    llm = _ShapeLLM([
        {"heading": "空间格局", "body": "南部建筑密度显著高于北部。"},
        {"heading": "业务含义", "body": "可用于评估建设强度与更新需求。"},
    ])
    artifact = _service(tmp_path, llm).build(_request(), _analysis())

    assert [s["heading"] for s in artifact.sections] == ["空间格局", "业务含义"]
    assert artifact.llm_provider == "deepseek"


def test_bare_paragraph_list_is_accepted_with_generated_headings(tmp_path):
    llm = _ShapeLLM(["第一段落分析正文。", "第二段落分析正文。"])
    artifact = _service(tmp_path, llm).build(_request(), _analysis())

    headings = [s["heading"] for s in artifact.sections]
    assert len(headings) == 2
    # Distinct headings, so the report never repeats one.
    assert len(set(headings)) == 2
    assert artifact.sections[0]["body"] == "第一段落分析正文。"
    assert artifact.llm_provider == "deepseek"


def test_single_prose_string_is_accepted(tmp_path):
    """A terse revision ("精简") often collapses analysis into one string."""
    llm = _ShapeLLM("覆盖率36.27%，约三分之一地表为建筑物。")
    artifact = _service(tmp_path, llm).build(_request(), _analysis())

    assert len(artifact.sections) == 1
    assert "36.27%" in artifact.sections[0]["body"]
    assert artifact.llm_provider == "deepseek"


def test_expanded_revision_keeps_five_sections(tmp_path):
    """The expand format asks for 3-5 sections; none may be truncated away."""
    llm = _ShapeLLM([
        {"title": f"小节{i}", "text": f"正文{i}"} for i in range(1, 6)
    ])
    artifact = _service(tmp_path, llm).build(_request(), _analysis())

    assert len(artifact.sections) == 5


def test_dict_recommendations_are_flattened_not_stringified(tmp_path):
    """A {priority, action} object must not reach the report as a dict repr."""
    llm = _ShapeLLM(
        [{"title": "解读", "text": "正文。"}],
        recommendations=[
            {"priority": "高", "action": "优先开展地块级调研。"},
            {"action": "接入精度评估接口。"},
            "保持常规监测。",
        ],
    )
    service = _service(tmp_path, llm)
    artifact = service.build(_request(), _analysis())

    markdown = (tmp_path / artifact.markdown_url.removeprefix("/reports/")).read_text(encoding="utf-8")
    assert "优先开展地块级调研。" in markdown
    assert "[高]" in markdown
    assert "接入精度评估接口。" in markdown
    assert "保持常规监测。" in markdown
    # No Python dict repr anywhere in the rendered report.
    assert "{'" not in markdown and "'action'" not in markdown


def test_unusable_content_is_reported_as_template_not_deepseek(tmp_path):
    """A 200 OK carrying no usable prose must not be labelled as an LLM report."""
    llm = _ShapeLLM([{"title": "只有标题没有正文"}])
    artifact = _service(tmp_path, llm).build(_request(), _analysis())

    assert artifact.llm_provider == "template:unusable_blocks"
    assert artifact.debug["content_status"] == "unusable_blocks"


def test_unparsable_json_is_reported_as_template(tmp_path):
    class _BadJSON:
        last_status = "ok"

        def complete(self, system_prompt, user_prompt):
            return "抱歉，我无法完成这个请求。"

    artifact = _service(tmp_path, _BadJSON()).build(_request(), _analysis())

    assert artifact.llm_provider == "template:unparsable_json"
    assert artifact.debug["content_status"] == "unparsable_json"


def test_revision_prompt_sends_previous_sections_as_title_text(tmp_path):
    """The prompt must not teach the model heading/body key names."""
    llm = _ShapeLLM([{"title": "解读", "text": "正文。"}])
    service = _service(tmp_path, llm)
    previous = {
        "title": "上一版报告",
        "summary": "上一版摘要。",
        "sections": [{"heading": "空间格局", "body": "南部更密。"}],
    }

    service.revise(_request(), _analysis(), "给我一份精简版", previous)

    payload = json.loads(llm.prompts[-1])
    blocks = payload["上一版报告"]["章节"]
    assert blocks == [{"title": "空间格局", "text": "南部更密。"}]
    # And the output contract pins the key names explicitly.
    assert "title" in payload["输出格式"]["analysis"]
