"""All catalogued custom objects complete the mock handoff/resume flow."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from agent.config import MemoryConfig
from agent.graph.report_agent import ReportAgent
from agent.schemas.report import AgentStatus, AnalysisResult, ReportArtifact, ReportRequest
from agent.services.capability_service import CapabilityService
from agent.services.llm_provider import LLMProvider
from agent.services.memory_service import MemoryService
from agent.services.model_registry_service import ModelRegistryService


CUSTOM_CLASSES = [
    "湿地",
    "河流",
    "湖泊",
    "池塘",
    "道路十字路口",
    "操场",
    "机场",
    "体育场",
    "大型垃圾场",
    "火车站",
    "露天停车场",
]

CUSTOM_CLASS_CASES = [
    pytest.param(
        class_name,
        marks=(
            pytest.mark.xfail(
                strict=True,
                reason="道路十字路口当前被内置‘道路’能力抢先匹配",
            )
            if class_name == "道路十字路口"
            else []
        ),
    )
    for class_name in CUSTOM_CLASSES
]


class _MuteLLM(LLMProvider):
    def complete(self, system_prompt: str, user_prompt: str) -> str:  # pragma: no cover
        return ""


class _UnusedAnalysis:
    def analyze(self, request):  # pragma: no cover
        raise AssertionError("custom object must not route to a native analysis service")


class _RecordingCustomAnalysis:
    def __init__(self) -> None:
        self.requests: list[ReportRequest] = []

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        self.requests.append(request)
        return AnalysisResult(
            task=f"{request.target_object}识别",
            region=request.region,
            time_range=request.time_range,
            headline=f"{request.target_object}测试",
            summary="mock custom inference completed",
            metrics=[],
            findings=[],
            recommendations=[],
            aef_payload={"used_patch_ids": list(request.selected_patch_ids)},
        )


class _StubReport:
    def build(self, request, analysis):
        return ReportArtifact(
            title=analysis.headline,
            abstract=analysis.summary,
            sections=[],
            metrics=[],
            charts=[],
            html_url=f"/reports/{request.custom_model_id}.html",
            markdown_url=f"/reports/{request.custom_model_id}.md",
            map_html_url=f"/reports/{request.custom_model_id}.map.html",
        )


def _model_payload(class_name: str) -> dict:
    slug = CUSTOM_CLASSES.index(class_name) + 1
    return {
        "id": f"model_mock_{slug:02d}",
        "name": f"分类头_{class_name}",
        "type": "single_time_detection",
        "task_type": "building_extraction",
        "status": "completed",
        "source": "custom",
        "created_at": f"2026-07-30T10:{slug:02d}:00",
        "completed_at": f"2026-07-30T10:{slug:02d}:01",
        "classes": [{"id": "cls_000", "name": class_name, "color": "#20beda"}],
    }


@pytest.mark.parametrize("class_name", CUSTOM_CLASS_CASES)
def test_custom_catalog_mock_handoff_then_model_resume(tmp_path, monkeypatch, class_name):
    visible_models: list[dict] = []
    model_list_calls = {"count": 0}
    registry = ModelRegistryService(cache_ttl=60.0)

    def fake_models(path):
        model_list_calls["count"] += 1
        return list(visible_models)

    monkeypatch.setattr(registry.http, "get_list_optional", fake_models)
    monkeypatch.setattr(
        registry.http,
        "get_json_optional",
        lambda path: {
            "default_training_method": "xuannv_earth",
            "task_contracts": {
                "land_use_classification": {
                    "temporal_mode": "single",
                    "required_fields": ["month"],
                }
            },
        },
    )
    capability = CapabilityService(
        registry=registry,
        annotation_ui_base="http://mock-training.test",
    )
    custom_analysis = _RecordingCustomAnalysis()
    unused = _UnusedAnalysis()
    agent = ReportAgent(
        memory_service=MemoryService(MemoryConfig(db_path=tmp_path / "memory.sqlite3")),
        chat_llm=_MuteLLM(),
        capability_service=capability,
        analysis_service=unused,
        change_service=unused,
        checkup_service=unused,
        score_service=unused,
        custom_model_service=custom_analysis,
        report_service=_StubReport(),
    )
    session_id = f"catalog-{CUSTOM_CLASSES.index(class_name)}"
    first = agent.run(ReportRequest.from_dict({
        "session_id": session_id,
        "region": "北京市海淀区",
        "prompt": f"请生成海淀区2026年2月{class_name}分布报告",
        "selected_patch_ids": ["patch_000024", "patch_000025"],
    }))

    assert first.status == AgentStatus.NEEDS_ANNOTATION
    assert first.action["type"] == "open_annotation_ui"
    assert first.action["class_name"] == class_name
    assert first.action["model_type"] == "single_time_detection"
    parsed = urlparse(first.action["url"])
    params = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}" == "http://mock-training.test"
    assert params["region_id"] == ["haidian"]
    assert params["class"] == [class_name]
    assert params["training_method"] == ["xuannv_earth"]
    assert first.memory["pending_custom_model"]["class_name"] == class_name
    assert custom_analysis.requests == []

    visible_models.append(_model_payload(class_name))
    resumed = agent.run(ReportRequest.from_dict({
        "session_id": session_id,
        "region": "北京市海淀区",
        "prompt": "我已经标注训练完成",
    }))

    expected_model_id = visible_models[0]["id"]
    assert resumed.status == AgentStatus.OK
    assert resumed.intent is not None
    assert resumed.intent.target_object == class_name
    assert resumed.intent.custom_model_id == expected_model_id
    assert resumed.request.custom_model_id == expected_model_id
    assert resumed.request.target_object == class_name
    assert resumed.request.time_range == "2026-02"
    assert resumed.request.selected_patch_ids == ["patch_000024", "patch_000025"]
    assert resumed.report is not None
    assert resumed.report.map_html_url.endswith(f"{expected_model_id}.map.html")
    assert resumed.memory.get("pending_custom_model") in (None, {})
    assert len(custom_analysis.requests) == 1
    assert model_list_calls["count"] == 2
