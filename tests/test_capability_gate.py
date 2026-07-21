"""Phase 2: capability gate + annotation handoff + resume-after-training.

Uses a fake CapabilityService (no network) and an isolated on-disk MemoryService
so pending-model state survives across turns within a test.
"""

from __future__ import annotations

from agent.config import MemoryConfig
from agent.graph.report_agent import ReportAgent
from agent.schemas.report import AgentStatus, ReportRequest
from agent.services.capability_service import (
    Capability, CUSTOM_FAILED, CUSTOM_READY, CUSTOM_TRAINING, NATIVE, NEEDS_ANNOTATION,
)
from agent.services.llm_provider import LLMProvider
from agent.services.memory_service import MemoryService


class FakeCapability:
    """Scriptable CapabilityService: maps class name -> Capability kind."""

    def __init__(self, by_object):
        self._by_object = by_object  # obj/class -> Capability
        self.annotation_ui_base = "http://ui.test"

    def resolve(self, region, target_object):
        from agent.taxonomy import non_native_object, resolve_region_id
        key = non_native_object(target_object) or target_object
        cap = self._by_object.get(key)
        if cap is None:
            return Capability(kind=NATIVE, target_object=target_object,
                              region_id=resolve_region_id(region))
        cap.region_id = resolve_region_id(region)
        cap.target_object = target_object
        return cap

    def annotation_action(self, cap, *, model_type="single_time_detection", month=""):
        return {"type": "open_annotation_ui",
                "url": f"http://ui.test/models/new?class={cap.class_name}",
                "class_name": cap.class_name, "model_type": model_type}


class MuteLLM(LLMProvider):
    def complete(self, system_prompt: str, user_prompt: str) -> str:  # pragma: no cover
        return ""


class _StubAnalysis:
    """Returns a canned AnalysisResult so gate tests never hit the network."""

    def analyze(self, request):
        from agent.schemas.report import AnalysisResult
        return AnalysisResult(
            task=request.task or "分析", region=request.region, time_range=request.time_range,
            headline="stub", summary="stub", metrics=[], findings=[], recommendations=[],
        )


class _StubReport:
    def build(self, request, analysis):
        from agent.schemas.report import ReportArtifact
        return ReportArtifact(
            title="stub", abstract="stub", sections=[], metrics=[], charts=[],
            html_url="/x.html", markdown_url="/x.md",
        )


def _agent(tmp_path, by_object):
    mem = MemoryService(MemoryConfig(db_path=tmp_path / "mem.sqlite3"))
    stub = _StubAnalysis()
    return ReportAgent(
        memory_service=mem,
        chat_llm=MuteLLM(),
        capability_service=FakeCapability(by_object),
        analysis_service=stub,
        change_service=stub,
        checkup_service=stub,
        score_service=stub,
        report_service=_StubReport(),
    )


AOI = {"type": "bbox", "coordinates": [116.20, 39.88, 116.26, 39.92]}


def _req(prompt, sid, **kw):
    return ReportRequest.from_dict({"prompt": prompt, "region": "北京市海淀区", "session_id": sid, **kw})


def test_needs_annotation_handoff(tmp_path):
    agent = _agent(tmp_path, {"湿地": Capability(kind=NEEDS_ANNOTATION, class_name="湿地")})
    r = agent.run(_req("对比2025-12和2026-05的湿地变化", "s1", aoi=AOI))
    assert r.status == AgentStatus.NEEDS_ANNOTATION
    assert r.action.get("type") == "open_annotation_ui"
    assert "湿地" in r.action.get("url", "")
    # Pending state persisted for resume.
    assert r.memory["pending_custom_model"]["class_name"] == "湿地"


def test_custom_training_asks_to_wait(tmp_path):
    agent = _agent(tmp_path, {"机场": Capability(kind=CUSTOM_TRAINING, class_name="机场", model_id="m1", model_status="training")})
    r = agent.run(_req("监测机场用地变化 2025-12 到 2026-05", "s2", aoi=AOI))
    assert r.status == AgentStatus.NEEDS_INPUT
    assert "训练" in r.message
    assert r.memory["pending_custom_model"]["class_name"] == "机场"


def test_native_object_not_gated(tmp_path):
    # 建筑物 is native → no gate, ordinary change flow proceeds to slot logic.
    agent = _agent(tmp_path, {})
    r = agent.run(_req("对比2025-12和2026-05的建筑物变化", "s3", aoi=AOI))
    assert r.status != AgentStatus.NEEDS_ANNOTATION
    assert not r.action


def test_resume_after_training_ready(tmp_path):
    # Turn 1: needs annotation → pending. Turn 2: model now ready → resume + run.
    caps = {"湿地": Capability(kind=NEEDS_ANNOTATION, class_name="湿地")}
    agent = _agent(tmp_path, caps)
    r1 = agent.run(_req("对比2025-12和2026-05的湿地变化", "s4", aoi=AOI))
    assert r1.status == AgentStatus.NEEDS_ANNOTATION

    # Model finished training between turns.
    caps["湿地"] = Capability(kind=CUSTOM_READY, class_name="湿地", model_id="model_x", model_status="completed")
    r2 = agent.run(_req("标注好了", "s4", aoi=AOI))
    # Pending cleared, and the change scenario resumed (months were remembered).
    assert r2.memory.get("pending_custom_model") in (None, {})
    assert r2.intent is not None and r2.intent.custom_model_id == "model_x"


def test_resume_still_training(tmp_path):
    caps = {"机场": Capability(kind=NEEDS_ANNOTATION, class_name="机场")}
    agent = _agent(tmp_path, caps)
    agent.run(_req("监测机场变化 2025-12 到 2026-05", "s5", aoi=AOI))
    caps["机场"] = Capability(kind=CUSTOM_TRAINING, class_name="机场", model_id="m", model_status="training")
    r = agent.run(_req("好了", "s5", aoi=AOI))
    assert r.status == AgentStatus.NEEDS_INPUT
    assert "训练" in r.message
    # Still pending (not cleared).
    assert r.memory["pending_custom_model"]["class_name"] == "机场"


def test_ok_without_pending_does_not_resume(tmp_path):
    # "好了" with nothing pending must not trigger resume machinery.
    agent = _agent(tmp_path, {})
    r = agent.run(_req("好了", "s6"))
    assert r.status in {AgentStatus.CHAT, AgentStatus.NEEDS_INPUT}
    assert not r.memory.get("pending_custom_model")


def test_failed_training_offers_retry(tmp_path):
    # A model exists but its last training failed → handoff framed as a retry.
    agent = _agent(tmp_path, {"湿地": Capability(kind=CUSTOM_FAILED, class_name="湿地", model_id="m_f", model_status="failed")})
    r = agent.run(_req("对比2025-12和2026-05的湿地变化", "sf1", aoi=AOI))
    assert r.status == AgentStatus.NEEDS_ANNOTATION
    assert r.action.get("type") == "open_annotation_ui"
    assert "没有成功" in r.message or "训练" in r.message
    # Pending remembered so the retry can resume.
    assert r.memory["pending_custom_model"]["class_name"] == "湿地"


def test_resume_after_failed_training(tmp_path):
    # Turn 1: needs annotation. Turn 2: training came back failed → retry copy.
    caps = {"机场": Capability(kind=NEEDS_ANNOTATION, class_name="机场")}
    agent = _agent(tmp_path, caps)
    agent.run(_req("监测机场变化 2025-12 到 2026-05", "sf2", aoi=AOI))
    caps["机场"] = Capability(kind=CUSTOM_FAILED, class_name="机场", model_id="mf", model_status="failed")
    r = agent.run(_req("训练完了", "sf2", aoi=AOI))
    assert r.status == AgentStatus.NEEDS_ANNOTATION
    assert "没有成功" in r.message or "没成功" in r.message
    # Still pending (retry not yet done).
    assert r.memory["pending_custom_model"]["class_name"] == "机场"
