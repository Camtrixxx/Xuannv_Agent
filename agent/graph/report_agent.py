from __future__ import annotations

import json
from typing import Any, TypedDict

from agent.schemas.report import (
    AgentResponse,
    AgentRoute,
    AgentStatus,
    MessageType,
    ReportRequest,
    infer_two_months,
    to_dict,
)
from agent.services.aef_analysis_service import AEFAnalysisService
from agent.services.analysis_service import MockAnalysisService
from agent.services.intent_service import IntentService
from agent.services.llm_provider import DeepSeekProvider, LLMProvider
from agent.services.common import extract_json_object, strip_markdown
from agent.services.memory_service import MemoryService
from agent.services.region_availability import (
    coverage_hint,
    is_month_available,
    region_tasks,
    unavailable_message,
)
from agent.services.regional_analysis_service import RegionalAnalysisService
from agent.services.region_checkup_service import RegionCheckupService
from agent.services.change_monitor_service import ChangeMonitorService
from agent.services.custom_model_analysis_service import CustomModelAnalysisService
from agent.services.pressure_score_service import PressureScoreService
from agent.services.capability_service import CapabilityService, NEEDS_ANNOTATION as CAP_NEEDS_ANNOTATION, CUSTOM_READY, CUSTOM_TRAINING, CUSTOM_FAILED
from agent.services.report_service import ReportService
from agent.taxonomy import native_object, non_native_object, region_from_bbox

try:
    from langgraph.graph import END, StateGraph
except ImportError:  # Keep the agent runnable before optional deps are installed.
    END = None
    StateGraph = None


class ReportAgentState(TypedDict, total=False):
    request: ReportRequest
    intent: dict[str, Any]
    memory: dict[str, Any]
    status: str
    message: str
    analysis: Any
    report: Any
    action: dict[str, Any]
    debug: dict[str, Any]


class ReportAgent:
    """Report orchestration.

    Uses LangGraph when available, while preserving a small fallback path so the
    local prototype can still run before optional agent packages are installed.
    """

    def __init__(
        self,
        intent_service: IntentService | None = None,
        memory_service: MemoryService | None = None,
        chat_llm: LLMProvider | None = None,
        analysis_service: AEFAnalysisService | MockAnalysisService | RegionalAnalysisService | None = None,
        report_service: ReportService | None = None,
        checkup_service: RegionCheckupService | None = None,
        change_service: ChangeMonitorService | None = None,
        score_service: PressureScoreService | None = None,
        capability_service: CapabilityService | None = None,
        custom_model_service: CustomModelAnalysisService | None = None,
    ) -> None:
        self.intent_service = intent_service or IntentService()
        self.memory_service = memory_service or MemoryService()
        self.chat_llm = chat_llm or DeepSeekProvider()
        self.analysis_service = analysis_service or RegionalAnalysisService()
        self.report_service = report_service or ReportService()
        self.checkup_service = checkup_service or RegionCheckupService()
        self.change_service = change_service or ChangeMonitorService()
        self.score_service = score_service or PressureScoreService()
        self.capability_service = capability_service or CapabilityService()
        self.custom_model_service = custom_model_service or CustomModelAnalysisService()
        self.graph = self._build_graph() if StateGraph is not None else None

    def run(self, request: ReportRequest) -> AgentResponse:
        if self.graph is None:
            state = self._merge_memory(self._parse_intent(self._load_memory({"request": request})))
            route = self._route_after_merge(state)
            if route == AgentRoute.CHAT_RESPONSE:
                state = self._chat_response(state)
            elif route == AgentRoute.ASK_CLARIFICATION:
                state = self._ask_clarification(state)
            else:
                state = self._generate_report(self._run_analysis(state))
            state = self._write_memory(state)
            if state.get("status") in {AgentStatus.NEEDS_INPUT, AgentStatus.NEEDS_ANNOTATION, AgentStatus.CHAT}:
                return self._response_from_state(request, state)
        else:
            state = self.graph.invoke({"request": request})

        return self._response_from_state(request, state)

    def load_session(self, session_id: str) -> dict[str, Any]:
        return self.memory_service.get_session(session_id)

    def list_sessions(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.memory_service.list_sessions(limit=limit)

    def _response_from_state(self, request: ReportRequest, state: ReportAgentState) -> AgentResponse:
        response_request = state.get("request") or request
        return AgentResponse(
            status=str(state.get("status") or "ok"),
            request=response_request,
            intent=state.get("intent"),
            message=str(state.get("message") or ""),
            session_id=response_request.session_id,
            memory=self.memory_service.snapshot(response_request.session_id),
            analysis=state.get("analysis"),
            report=state.get("report"),
            action=state.get("action") or {},
            debug=state.get("debug") or {},
        )

    def _build_graph(self):
        graph = StateGraph(ReportAgentState)
        graph.add_node("load_memory", self._load_memory)
        graph.add_node("parse_intent", self._parse_intent)
        graph.add_node("merge_memory", self._merge_memory)
        graph.add_node("ask_clarification", self._ask_clarification)
        graph.add_node("chat_response", self._chat_response)
        graph.add_node("run_analysis", self._run_analysis)
        graph.add_node("generate_report", self._generate_report)
        graph.add_node("write_memory", self._write_memory)
        graph.set_entry_point("load_memory")
        graph.add_edge("load_memory", "parse_intent")
        graph.add_edge("parse_intent", "merge_memory")
        graph.add_conditional_edges(
            "merge_memory",
            self._route_after_merge,
            {
                AgentRoute.CHAT_RESPONSE: "chat_response",
                AgentRoute.ASK_CLARIFICATION: "ask_clarification",
                AgentRoute.RUN_ANALYSIS: "run_analysis",
            },
        )
        graph.add_edge("ask_clarification", "write_memory")
        graph.add_edge("chat_response", "write_memory")
        graph.add_edge("run_analysis", "generate_report")
        graph.add_edge("generate_report", "write_memory")
        graph.add_edge("write_memory", END)
        return graph.compile()

    def _load_memory(self, state: ReportAgentState) -> ReportAgentState:
        request = state["request"]
        self.memory_service.append_user(request.session_id, request.prompt)
        state["memory"] = self.memory_service.snapshot(request.session_id)
        state["debug"] = {"session_id": request.session_id}
        return state

    def _parse_intent(self, state: ReportAgentState) -> ReportAgentState:
        request = state["request"]
        intent = self.intent_service.parse(request)
        if request.target_object:
            intent.target_object = request.target_object
        state["intent"] = intent
        state["status"] = AgentStatus.PARSED
        state["message"] = "已完成意图分类。"
        state.setdefault("debug", {})["intent"] = intent.debug
        return state

    def _merge_memory(self, state: ReportAgentState) -> ReportAgentState:
        intent = state["intent"]
        memory = state.get("memory") or {}
        previous = memory.get("current_intent") or {}
        pending = memory.get("pending_slots") or []

        # Resume-after-training takes precedence: if a custom model is pending for
        # this session, this turn is very likely the user coming back to continue
        # (even a "好了"/"继续" that would otherwise look like chat). Verify the
        # model's real status before proceeding — never trust the user's word.
        if memory.get("pending_custom_model"):
            resumed = self._resume_after_training(state, intent, memory)
            if resumed is not None:
                return resumed

        if intent.message_type in {MessageType.FREE_CHAT, MessageType.FOLLOW_UP}:
            state["status"] = AgentStatus.CHAT
            state["message"] = ""
            return state

        # Region resolution when the user didn't explicitly choose one (neither
        # named in text nor picked in the frontend dropdown — see IntentService's
        # region_explicit). The parser silently defaults such turns to 雅江, so we
        # recover the real region from two better signals, in order:
        #   1. the framed map AOI — if its centre lands in exactly one region's
        #      geographic box, that's an unambiguous this-turn signal (fixes the
        #      "framed 海淀 coords but agent asks 是雅江吗?" gap);
        #   2. otherwise the session's earlier region, so it carries across turns.
        # Done here — before the capability gate and every scenario / slot branch —
        # so every downstream path (which rebuilds the request from intent.region)
        # sees it. An explicit choice this turn always wins and is left untouched.
        region_explicit = bool((intent.debug or {}).get("region_explicit"))
        if not region_explicit:
            inferred = region_from_bbox(state["request"].aoi)
            if inferred:
                intent.region = inferred
            elif previous.get("region"):
                intent.region = previous.get("region")

        # Capability gate: if the user is asking about a non-native object (湿地,
        # 机场, …) that has no ready custom model, hand off to annotation / ask to
        # wait — before any scenario slot logic, so we don't ask for a month/AOI
        # for something we can't analyze yet.
        gated = self._gate_capability(state, intent, memory, previous)
        if gated is not None:
            return gated

        # Keep the checkup scenario sticky across the clarification turns it needs
        # (user says "体检" → we ask for an AOI → next turn they draw it + give a
        # month, with no "体检" word). Inherit only while its slots are still pending.
        if not intent.scenario and previous.get("scenario") in {"checkup", "change", "score"} and pending:
            intent.scenario = previous.get("scenario")

        # Scenario A (片区综合体检): needs a month + a map AOI, not a single task.
        # Handled before the ordinary task/month slot logic so it asks for the
        # right things (frame an area) instead of "which task?".
        if intent.scenario == "checkup":
            return self._merge_checkup(state, intent, memory, previous)
        # Scenario B (建设扰动监测): needs two months + a map AOI.
        if intent.scenario == "change":
            return self._merge_change(state, intent, memory, previous)
        # Scenario C (补绿优先区评分): needs a month + a map AOI (same slots as checkup).
        if intent.scenario == "score":
            return self._merge_month_aoi(state, intent, memory, previous, kind="score")

        # Did the user name a task or month *this turn* (vs. it only being inherited)?
        specified_now = bool(intent.task) or bool(intent.time_range) or bool(intent.target_object)

        # Follow-ups (slot fills, confirmations, "换个任务", or any turn with prior
        # context) inherit the earlier report slots so the user only supplies what's
        # still missing. Historical task/month are reused silently — no default is ever
        # invented, so an unspecified task stays empty and gets asked for.
        inherits_context = (
            intent.message_type in {MessageType.SLOT_FILL, MessageType.CONFIRMATION, MessageType.CHANGE_CONTEXT}
            or bool(pending)
            or bool(previous)
        )
        if inherits_context:
            if not intent.task and previous.get("task"):
                intent.task = previous.get("task")
            if not intent.time_range and previous.get("time_range"):
                intent.time_range = previous.get("time_range")

        missing = []
        if not intent.task and not intent.custom_model_id:
            missing.append("task")
        if not intent.time_range:
            missing.append("time_range")
        if not self._has_analysis_area(state["request"], intent.region, memory):
            missing.append("aoi")
        intent.missing_fields = missing
        intent.confirmation_fields = []

        if missing:
            state["status"] = AgentStatus.NEEDS_INPUT
            ordinary_missing = [field for field in missing if field != "aoi"]
            if ordinary_missing:
                state["message"] = self._clarify_message(ordinary_missing, intent.region)
                if "aoi" in missing:
                    state["message"] += " 另外，请先在地图上框选要分析的区域。"
            else:
                state["message"] = (
                    "请先在地图上框选要分析的区域并确认 Patch，我会按你选择的范围生成海淀专题报告。"
                )
            return state

        # Pre-validate the month against the region's real coverage so an
        # unavailable month becomes a friendly clarification instead of a raw
        # upstream error. The bad month is forgotten so the user can re-supply.
        if not is_month_available(intent.region, intent.time_range):
            state["message"] = unavailable_message(intent.region, intent.time_range)
            intent.time_range = ""
            intent.missing_fields = ["time_range"]
            state["status"] = AgentStatus.NEEDS_INPUT
            return state

        # A report already exists and this turn added nothing new (e.g. the user
        # replied "1" to a menu, or "嗯"): they are continuing the conversation, not
        # asking to regenerate the same report. Route to grounded discussion.
        if memory.get("report_context") and not specified_now:
            state["status"] = AgentStatus.CHAT
            state["message"] = ""
            return state

        state["status"] = AgentStatus.OK
        state["message"] = "已完成意图解析，准备执行遥感分析。"
        return state

    def _merge_checkup(self, state, intent, memory, previous) -> ReportAgentState:
        """Slot logic for the 片区体检 scenario: needs month + a map AOI."""
        return self._merge_month_aoi(state, intent, memory, previous, kind="checkup")

    # Prompts per month+AOI scenario (checkup / score share identical slot logic).
    _MONTH_AOI_COPY = {
        "checkup": {
            "aoi_extra": " 另外请先在地图上框选体检片区。",
            "aoi_only": "请先在地图上框选一个片区范围，我就为这个片区生成综合体检报告。",
            "ok": "已确认片区体检范围，正在聚合各专题结果。",
        },
        "score": {
            "aoi_extra": " 另外请先在地图上框选要评估的片区。",
            "aoi_only": "请先在地图上框选一个片区范围，我就为它做高硬化低绿地压力评分、圈定补绿优先区。",
            "ok": "已确认评估范围，正在逐 patch 计算硬化与绿地压力分。",
        },
    }

    def _merge_month_aoi(self, state, intent, memory, previous, *, kind: str) -> ReportAgentState:
        """Shared slot logic for scenarios needing one month + a map AOI."""
        copy = self._MONTH_AOI_COPY[kind]
        request = state["request"]
        # Month may come from the frontend picker (request.time_range) or a prior turn.
        # The LLM intent path drops the frontend month, so recover it here.
        if not intent.time_range and request.time_range:
            intent.time_range = request.time_range
        if not intent.time_range and previous.get("time_range"):
            intent.time_range = previous.get("time_range")
        intent.confirmation_fields = []

        missing: list[str] = []
        if not intent.time_range:
            missing.append("time_range")
        has_aoi = self._has_bbox(request.aoi)
        if not has_aoi:
            missing.append("aoi")
        intent.missing_fields = missing

        if "time_range" in missing:
            state["status"] = AgentStatus.NEEDS_INPUT
            base = self._clarify_message(["time_range"], intent.region)
            state["message"] = (base + copy["aoi_extra"]) if not has_aoi else base
            return state
        if "aoi" in missing:
            state["status"] = AgentStatus.NEEDS_INPUT
            state["message"] = copy["aoi_only"]
            return state
        if not is_month_available(intent.region, intent.time_range):
            state["message"] = unavailable_message(intent.region, intent.time_range)
            intent.time_range = ""
            intent.missing_fields = ["time_range"]
            state["status"] = AgentStatus.NEEDS_INPUT
            return state

        state["status"] = AgentStatus.OK
        state["message"] = copy["ok"]
        return state

    def _llm_two_months(self, prompt: str) -> tuple[str, str] | None:
        """Semantic two-month extraction for the change scenario.

        Rule parsing (``infer_two_months``) only catches well-formed tokens. This
        fallback uses the chat LLM to read fuzzy/relative phrasing ("今年一月到三月",
        "2026-1和2026-3", "对比年初和现在"). Returns an ordered (before, after) pair
        of distinct YYYY-MM months, or None on any failure (→ graceful ask). The
        request/response API is unchanged; this is purely a backend extraction step.
        """
        text = (prompt or "").strip()
        if not text:
            return None
        from datetime import date

        today = getattr(self.intent_service, "today", None) or date.today()
        system_prompt = (
            "你从用户中文输入里抽取『建设扰动变化检测』要对比的两个月份。"
            "只输出 JSON，不要解释。无法确定两个不同月份时把字段留空。"
        )
        user_prompt = json.dumps(
            {
                "用户输入": text,
                "当前日期": today.isoformat(),
                "说明": "解析相对表达（今年/去年/年初/月份中文数字）；两个月份可能写成区间或并列",
                "输出要求": {
                    "before": "较早的月份，YYYY-MM；无法确定留空",
                    "after": "较晚的月份，YYYY-MM；无法确定留空",
                },
                "示例": [
                    {"用户输入": "今年一月份到三月份", "before": "2026-01", "after": "2026-03"},
                    {"用户输入": "2026-1和2026-3", "before": "2026-01", "after": "2026-03"},
                    {"用户输入": "对比2025-12和2026-05", "before": "2025-12", "after": "2026-05"},
                ],
            },
            ensure_ascii=False,
        )
        try:
            raw = self.chat_llm.complete(system_prompt, user_prompt)
        except Exception:
            return None
        if not raw:
            return None
        payload = extract_json_object(raw)
        if not isinstance(payload, dict):
            return None
        before = self._normalize_month(payload.get("before"))
        after = self._normalize_month(payload.get("after"))
        if not (before and after) or before == after:
            return None
        return (before, after) if before < after else (after, before)

    @staticmethod
    def _normalize_month(value: Any) -> str:
        """Coerce a model-returned month to canonical YYYY-MM, or '' if unusable."""
        import re

        m = re.search(r"(20\d{2})[-/.]?(1[0-2]|0?[1-9])", str(value or ""))
        if not m:
            return ""
        return f"{m.group(1)}-{int(m.group(2)):02d}"

    def _merge_change(self, state, intent, memory, previous) -> ReportAgentState:
        """Slot logic for the 建设扰动监测 scenario: needs two months + a map AOI."""
        request = state["request"]
        # Prefer explicit before/after from the request; else parse the prompt;
        # else recover a remembered pair. Persist so later slot-fills stick.
        before = request.before_time_range or ""
        after = request.after_time_range or ""
        if not (before and after):
            before, after = infer_two_months(intent.user_prompt)
        # Rule extraction only covers well-formed tokens ("2025-12", "2025年12月").
        # For fuzzy/relative phrasing ("今年一月到三月", "2026-1和2026-3") fall back to
        # the LLM's semantic reading before we recover a remembered pair or ask.
        if not (before and after):
            before, after = self._llm_two_months(intent.user_prompt) or (before, after)
        if not (before and after):
            before = before or previous.get("before_time_range") or ""
            after = after or previous.get("after_time_range") or ""
        # Normalize + store back onto intent/request for downstream + memory.
        request.before_time_range = before
        request.after_time_range = after
        intent.debug["change_window"] = {"before": before, "after": after}

        has_two_months = bool(before and after and before != after)
        has_aoi = self._has_bbox(request.aoi)
        intent.confirmation_fields = []
        missing: list[str] = []
        if not has_two_months:
            missing.append("time_range")
        if not has_aoi:
            missing.append("aoi")
        intent.missing_fields = missing

        if missing:
            state["status"] = AgentStatus.NEEDS_INPUT
            if not has_two_months and not has_aoi:
                state["message"] = "做建设扰动监测需要两个月份对比，并在地图上框选片区。请告诉我起始月和对比月（如 2025-12 和 2026-05），并框选范围。"
            elif not has_two_months:
                state["message"] = "请给出要对比的两个月份（如 2025-12 和 2026-05），我来分析这段时间片区里的建设扰动。"
            else:
                state["message"] = "请先在地图上框选一个片区范围，我就为它做两期建设扰动对比。"
            return state

        for m in (before, after):
            if not is_month_available(intent.region, m):
                state["message"] = unavailable_message(intent.region, m)
                request.before_time_range = ""
                request.after_time_range = ""
                intent.missing_fields = ["time_range"]
                state["status"] = AgentStatus.NEEDS_INPUT
                return state

        intent.time_range = f"{before}→{after}"
        state["status"] = AgentStatus.OK
        state["message"] = "已确认监测范围与两期月份，正在做像素级变化对比。"
        return state

    # ------------------------------------------------- custom-model capability

    @staticmethod
    def _detect_target_object(intent) -> str:
        """The non-native analysis object named this turn, or "" if native.

        ``non_native_object`` already strips region names, so a place like 雅江
        can't collide with the "江"→河流 substring alias.
        """
        explicit = str(getattr(intent, "target_object", "") or "").strip()
        if explicit:
            return explicit
        known = non_native_object(intent.user_prompt or "")
        if known:
            return known
        return ""

    def _detect_target_object_dynamic(self, intent) -> str:
        known = self._detect_target_object(intent)
        if known:
            return known
        detector = getattr(self.capability_service, "detect_custom_object", None)
        if callable(detector):
            return str(detector(intent.region, intent.user_prompt or "") or "")
        return ""

    def _gate_capability(self, state, intent, memory, previous):
        """Return a state if a non-native object blocks analysis, else None.

        Only genuinely non-native objects (湿地/机场/…) are gated; native tasks
        return None and flow through the ordinary path untouched.
        """
        obj = self._detect_target_object_dynamic(intent)
        if not obj:
            return None
        intent.target_object = obj
        model_type = self._custom_model_type(intent.scenario)
        cap = self.capability_service.resolve(intent.region, obj, model_type=model_type)
        state.setdefault("debug", {})["capability"] = {
            "kind": cap.kind, "object": obj, "class": cap.class_name, "model_id": cap.model_id,
        }
        if cap.kind == CUSTOM_READY:
            # A trained model exists — remember it so run_analysis uses it, and
            # let the ordinary scenario/slot logic continue (fall through).
            intent.custom_model_id = cap.model_id
            intent.task = intent.task or f"{cap.class_name}识别"
            return None
        if cap.kind == CUSTOM_TRAINING:
            state["status"] = AgentStatus.NEEDS_INPUT
            state["message"] = (
                f"『{cap.class_name}』的自定义模型正在训练中，还不能用来分析。"
                f"训练完成后回来告诉我“好了”，我就继续。"
            )
            self._remember_pending(state, intent, cap, model_status=cap.model_status, model_id=cap.model_id)
            return state
        if cap.kind == CUSTOM_FAILED:
            return self._handoff_annotation(state, intent, cap, failed=True)
        if cap.kind == CAP_NEEDS_ANNOTATION:
            return self._handoff_annotation(state, intent, cap)
        return None

    def _handoff_annotation(self, state, intent, cap, *, failed: bool = False):
        """Hand off to the annotation UI.

        ``failed=True`` means a prior training attempt failed — say so plainly
        and frame it as a retry, rather than the first-time "not built-in" copy.
        """
        month = intent.time_range or (state["request"].time_range or "")
        model_type = self._custom_model_type(intent.scenario)
        action = self.capability_service.annotation_action(cap, model_type=model_type, month=month)
        state["status"] = AgentStatus.NEEDS_ANNOTATION
        state["action"] = action
        if failed:
            state["message"] = (
                f"『{cap.class_name}』上一次训练没有成功（模型状态 {cap.model_status or 'failed'}）。"
                f"多半是标注样本太少或不够典型。已重新为你打开标注入口，"
                f"建议补几个更清晰的样本再训练，完成后回来告诉我“标注好了 / 训练完了”，我继续这次分析。"
            )
        else:
            state["message"] = (
                f"『{cap.class_name}』不是内置地物，需要先在标注页标注少量样本再训练"
                f"（样本很少时系统会自动用相似度召回，无需大量标注）。"
                f"已为你准备好标注入口，完成后回来告诉我“标注好了 / 训练完了”，我就继续这次分析。"
            )
        self._remember_pending(state, intent, cap, action=action, model_status=cap.model_status, model_id=cap.model_id)
        return state

    def _remember_pending(self, state, intent, cap, *, action=None, model_status="", model_id=""):
        """Persist enough to resume the original task once the model is ready."""
        request = state["request"]
        pending = {
            "class_name": cap.class_name,
            "target_object": cap.target_object,
            "region": intent.region,
            "region_id": cap.region_id,
            "scenario": intent.scenario or ((state.get("memory") or {}).get("current_intent") or {}).get("scenario", ""),
            "model_id": model_id,
            "model_status": model_status,
            "model_type": (action or {}).get("model_type") or self._custom_model_type(intent.scenario),
            "action": action or {},
            # Original request params so the resumed turn reruns the same task.
            "request": {
                "task": intent.task,
                "time_range": intent.time_range,
                "before_time_range": getattr(request, "before_time_range", ""),
                "after_time_range": getattr(request, "after_time_range", ""),
                "selected_patch_ids": list(request.selected_patch_ids),
                "aoi": request.aoi,
            },
        }
        self.memory_service.set_pending_custom_model(request.session_id, pending)

    def _resume_after_training(self, state, intent, memory):
        """If a custom model is pending, check its real status and resume/wait.

        Returns a state to short-circuit merge_memory, or None to fall through
        to normal handling (e.g. the user clearly started a different request).
        """
        pending = memory.get("pending_custom_model") or {}
        class_name = pending.get("class_name") or ""
        region = pending.get("region") or intent.region
        # The user may have moved on from the pending custom object. Abandon it
        # and fall through to normal handling when this turn either:
        #   - names a *different* non-native object (the gate re-offers it), or
        #   - clearly asks for a native, built-in task/object (道路提取, …) and
        #     no longer mentions the pending object. Otherwise "是道路提取" stays
        #     wrongly stuck re-offering "湿地".
        prompt = intent.user_prompt or ""
        new_obj = self._detect_target_object_dynamic(intent)
        pending_obj = non_native_object(class_name)
        if new_obj and pending_obj and new_obj != pending_obj:
            self.memory_service.clear_pending_custom_model(state["request"].session_id)
            return None
        if not new_obj and (intent.task or native_object(prompt)) and (
            not pending_obj or pending_obj not in prompt
        ):
            self.memory_service.clear_pending_custom_model(state["request"].session_id)
            return None

        model_type = str(pending.get("model_type") or self._custom_model_type(pending.get("scenario") or ""))
        cap = self.capability_service.resolve(
            region,
            class_name,
            model_type=model_type,
            refresh=True,
        )
        if cap.kind == CUSTOM_READY:
            self.memory_service.clear_pending_custom_model(state["request"].session_id)
            return self._apply_resumed_pending(state, intent, pending, cap)
        if cap.kind == CUSTOM_TRAINING:
            state["status"] = AgentStatus.NEEDS_INPUT
            state["message"] = (
                f"『{class_name}』的模型还在训练中，请稍等片刻，训练完成后再对我说“好了”。"
            )
            return state
        cap.class_name = cap.class_name or class_name
        # Training failed between turns → say so and re-offer as a retry.
        if cap.kind == CUSTOM_FAILED:
            return self._handoff_annotation(state, intent, cap, failed=True)
        # Still nothing ready (annotation never started) — re-offer handoff.
        return self._handoff_annotation(state, intent, cap)

    def _apply_resumed_pending(self, state, intent, pending, cap):
        """Restore the original task params + the now-ready model, then proceed."""
        request = state["request"]
        saved = pending.get("request") or {}
        # Restore original slots the user supplied before the detour.
        intent.task = intent.task or saved.get("task") or ""
        intent.scenario = intent.scenario or pending.get("scenario") or ""
        intent.target_object = pending.get("target_object") or cap.target_object
        intent.custom_model_id = cap.model_id
        intent.task = intent.task or saved.get("task") or f"{cap.class_name}识别"
        if not intent.time_range:
            intent.time_range = saved.get("time_range") or ""
        if not getattr(request, "before_time_range", ""):
            request.before_time_range = saved.get("before_time_range") or ""
        if not getattr(request, "after_time_range", ""):
            request.after_time_range = saved.get("after_time_range") or ""
        if not request.selected_patch_ids and saved.get("selected_patch_ids"):
            request.selected_patch_ids = [str(item) for item in saved["selected_patch_ids"] if str(item).strip()]
        if not self._has_bbox(request.aoi) and self._has_bbox(saved.get("aoi")):
            request.aoi = saved.get("aoi")
        state.setdefault("debug", {})["resumed_custom_model"] = cap.model_id
        # Re-enter the appropriate scenario slot logic now that the model is ready.
        memory = state.get("memory") or {}
        previous = memory.get("current_intent") or {}
        if intent.scenario == "change":
            return self._merge_change(state, intent, memory, previous)
        if intent.scenario in {"checkup", "score"}:
            return self._merge_month_aoi(state, intent, memory, previous, kind=intent.scenario)
        # Ordinary single-task custom analysis: needs a month.
        if not intent.time_range:
            intent.missing_fields = ["time_range"]
            state["status"] = AgentStatus.NEEDS_INPUT
            state["message"] = f"『{cap.class_name}』模型已就绪。请补充要分析的月份。"
            return state
        if not self._has_analysis_area(request, intent.region, memory):
            intent.missing_fields = ["aoi"]
            state["status"] = AgentStatus.NEEDS_INPUT
            state["message"] = (
                f"『{cap.class_name}』模型已就绪。请先在地图上框选要分析的区域并确认 Patch。"
            )
            return state
        state["status"] = AgentStatus.OK
        state["message"] = f"『{cap.class_name}』模型已就绪，正在生成分析。"
        return state

    @staticmethod
    def _custom_model_type(scenario: str) -> str:
        # The current change workflow runs one single-date custom model at two
        # months and computes gained/lost pixels inside the Agent. Selecting a
        # change_detection checkpoint here would not match that inference path.
        return "single_time_detection"

    def _route_after_merge(self, state: ReportAgentState) -> str:
        if state.get("status") == AgentStatus.CHAT:
            return AgentRoute.CHAT_RESPONSE
        if state.get("status") in {AgentStatus.NEEDS_INPUT, AgentStatus.NEEDS_ANNOTATION}:
            # Both are terminal "ask the user" turns; the message/action is
            # already set by the gate/slot logic.
            return AgentRoute.ASK_CLARIFICATION
        return AgentRoute.RUN_ANALYSIS

    def _clarify_message(self, missing: list[str], region: str = "") -> str:
        need_task = "task" in missing
        need_month = "time_range" in missing
        tasks = region_tasks(region)
        task_list = "、".join(tasks) if tasks else "地物分类、水体分布、高程地形"
        region_name = region or "该区域"
        hint = coverage_hint(region)
        if need_task and need_month:
            return (
                f"好的，帮你生成报告～ {region_name}可以分析：{task_list}。"
                f"你想看哪一个、哪个月份呢？（{hint}）"
            )
        if need_task:
            return f"{region_name}可以分析：{task_list}。你想看哪一个呢？"
        return f"请补充要分析的月份。{hint}。"

    def _ask_clarification(self, state: ReportAgentState) -> ReportAgentState:
        # Preserve a NEEDS_ANNOTATION status (with its action); otherwise this is
        # an ordinary missing-slot clarification.
        if state.get("status") != AgentStatus.NEEDS_ANNOTATION:
            state["status"] = AgentStatus.NEEDS_INPUT
        # merge_memory already set the right prompt (missing-slot or unavailable-month).
        if not state.get("message"):
            intent = state.get("intent")
            missing = intent.missing_fields if intent else ["time_range"]
            region = intent.region if intent else ""
            state["message"] = self._clarify_message(missing, region)
        return state

    def _chat_response(self, state: ReportAgentState) -> ReportAgentState:
        request = state["request"]
        memory = state.get("memory") or {}
        report_context = memory.get("report_context") or {}
        if report_context:
            # Grounded discussion: answer questions about the last report using its
            # actual content, instead of regenerating a report.
            system_prompt = (
                "你是遥感报告助手。用户正在就上一次生成的遥感分析报告进行追问、讨论或修改。"
                "请基于下面提供的报告内容满足他的需求：\n"
                "· 如果是提问，就聚焦那一点详细解释（指标含义、结论依据、空间格局或业务建议）；\n"
                "· 如果要求换种说法、精简、展开、重写、润色或总结，就据此重新组织相应内容，"
                "输出层次清晰、语言通俗专业的结果（精简版要短，详细版要展开）。\n"
                "始终忠于报告中的数据，不要编造报告里没有的数字或事实，"
                "也不要输出系统参数、文件路径或免责声明。"
                "用自然口语、纯文本作答，不要用 Markdown 排版。"
            )
            recent = memory.get("recent_messages") or []
            convo = "\n".join(
                f"{'用户' if m.get('role') == 'user' else '助手'}：{m.get('content', '')}"
                for m in recent[-6:]
            )
            user_prompt = (
                f"最近的对话：\n{convo}\n\n"
                f"上一次报告内容（JSON）：\n{json.dumps(report_context, ensure_ascii=False)}\n\n"
                f"用户最新一句：{request.prompt}\n\n"
                "请结合对话上下文理解用户意图并回答；如果用户用了序号（如“1”）或“那个/第一个”"
                "等指代，请对应到你上一条消息里列出的选项。"
            )
        else:
            system_prompt = (
                "你是遥感报告助手。请用简洁自然的中文回答用户，不要生成报告，除非用户明确要求。"
                "你能覆盖三个区域及其任务：雅江区域（地物分类、水体分布、高程地形）、"
                "哈尔滨新区（建筑物提取、土地利用分类、水体提取）、"
                "北京市海淀区（建筑物提取、道路提取、施工识别、土地利用分类、土地覆盖分类、水体提取）。"
                "如果用户问某个区域能做什么，就据此如实回答。"
                "用自然口语、纯文本作答，不要用 Markdown 排版。"
            )
            user_prompt = (
                f"用户问题：{request.prompt}\n"
                "请自然地回答；你可以帮助生成地物分类、水体分布、高程地形等遥感专题报告。"
            )
        text = self.chat_llm.complete(system_prompt, user_prompt)
        state["status"] = AgentStatus.CHAT
        if text:
            state["message"] = strip_markdown(text)
        elif report_context:
            # Grounded turn but the LLM hiccuped — stay on-topic and invite a retry
            # rather than falling back to a generic greeting.
            state["message"] = "抱歉，我这会儿没组织好回答，可以再说一次或换个说法吗？你也可以直接打开上面的报告查看。"
        else:
            state["message"] = self._fallback_chat_response(request.prompt)
        return state

    def _fallback_chat_response(self, prompt: str) -> str:
        text = prompt.strip()
        if any(key in text for key in ["你是谁", "你是什么", "你是什么助手", "你是干什么"]):
            return (
                "我是雅江遥感报告助手，主要帮你把自然语言需求整理成标准化遥感任务，"
                "调用 AEF 模型完成地物分类、水体分类或高程地形分析，然后生成带图表的报告。"
            )
        if any(key in text for key in ["你能做什么", "你可以做什么", "你会做什么", "功能"]):
            return (
                "我可以帮你生成地物分类、水体分类和高程地形报告。你只要告诉我地区、任务和月份，"
                "比如“给我一份去年九月份的水体分类报告”，我会自动补齐流程并生成报告。"
            )
        return "我在。你可以直接和我聊天，也可以让我生成地物分类、水体分类或高程地形分析报告。"

    def _run_analysis(self, state: ReportAgentState) -> ReportAgentState:
        intent = state["intent"]
        request = state["request"]

        # Scenario B: two-date change monitoring over the framed AOI.
        if intent.scenario == "change":
            change_request = ReportRequest(
                task=intent.task,
                region=intent.region,
                prompt=intent.user_prompt,
                session_id=request.session_id,
                aoi=request.aoi,
                before_time_range=request.before_time_range,
                after_time_range=request.after_time_range,
                custom_model_id=intent.custom_model_id,
                target_object=intent.target_object,
            )
            state["request"] = change_request
            try:
                state["analysis"] = self.change_service.analyze(change_request)
            except Exception as exc:
                state["analysis"] = None
                state["status"] = AgentStatus.NEEDS_INPUT
                state["message"] = (
                    "抱歉，这次建设扰动监测没能完成（可能是某个月份数据暂不可用或框选范围内无可用 patch）。"
                    "你可以换月份或重新框选范围再试。"
                )
                state.setdefault("debug", {})["analysis_error"] = str(exc)
            return state

        # Scenario C: rank patches by 高硬化低绿地 pressure over the framed AOI.
        if intent.scenario == "score":
            score_request = ReportRequest(
                task="补绿优先区评分",
                region=intent.region,
                prompt=intent.user_prompt,
                time_range=intent.time_range,
                session_id=request.session_id,
                aoi=request.aoi,
            )
            state["request"] = score_request
            try:
                state["analysis"] = self.score_service.analyze(score_request)
            except Exception as exc:
                state["analysis"] = None
                state["status"] = AgentStatus.NEEDS_INPUT
                state["message"] = (
                    "抱歉，这次补绿优先区评分没能完成（可能是该月份数据暂不可用或框选范围内无可用 patch）。"
                    "你可以换个月份或重新框选范围再试。"
                )
                state.setdefault("debug", {})["analysis_error"] = str(exc)
            return state

        # Scenario A: aggregate a multi-task checkup over the framed AOI.
        if intent.scenario == "checkup":
            checkup_request = ReportRequest(
                task="片区综合体检",
                region=intent.region,
                prompt=intent.user_prompt,
                time_range=intent.time_range,
                session_id=request.session_id,
                aoi=request.aoi,
            )
            state["request"] = checkup_request
            try:
                state["analysis"] = self.checkup_service.analyze(checkup_request)
            except Exception as exc:
                state["analysis"] = None
                state["status"] = AgentStatus.NEEDS_INPUT
                state["message"] = (
                    "抱歉，这次片区体检没能完成（可能是该月份数据暂不可用或框选范围内无可用 patch）。"
                    "你可以换个月份或重新框选范围再试。"
                )
                state.setdefault("debug", {})["analysis_error"] = str(exc)
            return state

        selected_patch_ids = request.selected_patch_ids
        aoi = request.aoi
        # If this turn brought no map selection (e.g. "换成建筑物提取"), keep the same
        # patch as the previous report in this session so only the task/month changes —
        # instead of falling back to a fresh global pick on a different patch.
        inherited_patch = False
        if not selected_patch_ids and not self._has_bbox(aoi):
            ctx = (state.get("memory") or {}).get("report_context") or {}
            if ctx.get("region") == intent.region and ctx.get("used_patch_ids"):
                selected_patch_ids = list(ctx["used_patch_ids"])
                inherited_patch = True

        if intent.custom_model_id:
            custom_request = ReportRequest(
                task=intent.task or f"{intent.target_object}识别",
                region=intent.region,
                prompt=intent.user_prompt,
                time_range=intent.time_range,
                session_id=request.session_id,
                selected_patch_ids=selected_patch_ids,
                aoi=aoi,
                custom_model_id=intent.custom_model_id,
                target_object=intent.target_object,
            )
            state["request"] = custom_request
            try:
                state["analysis"] = self.custom_model_service.analyze(custom_request)
                return state
            except Exception as exc:
                state["analysis"] = None
                state["status"] = AgentStatus.NEEDS_INPUT
                state["message"] = (
                    "自定义模型已经找到，但这次推理没有完成。请确认所选 Patch 支持该月份，"
                    "或重新框选区域后再试。"
                )
                state.setdefault("debug", {})["analysis_error"] = str(exc)
                return state

        def _request(sel: list[str]) -> ReportRequest:
            return ReportRequest(
                task=intent.task,
                region=intent.region,
                prompt=intent.user_prompt,
                time_range=intent.time_range,
                session_id=request.session_id,
                selected_patch_ids=sel,
                aoi=aoi,
            )

        normalized_request = _request(selected_patch_ids)
        state["request"] = normalized_request
        try:
            state["analysis"] = self.analysis_service.analyze(normalized_request)
            return state
        except Exception as exc:
            # Never leave the user's previous area and silently pick patches from
            # elsewhere in Haidian. Ask for a new map selection instead.
            if inherited_patch:
                state["analysis"] = None
                state["status"] = AgentStatus.NEEDS_INPUT
                intent.missing_fields = ["aoi"]
                state["message"] = (
                    "上一次选择的 Patch 不支持当前任务或月份，请在地图上重新框选分析区域。"
                )
                state.setdefault("debug", {})["analysis_error"] = str(exc)
                return state
            state["analysis"] = None
            state["status"] = AgentStatus.NEEDS_INPUT
            state["message"] = (
                "抱歉，这次分析没能完成（可能是该月份数据暂不可用或模型服务繁忙）。"
                "你可以换个月份或任务再试一次。"
            )
            state.setdefault("debug", {})["analysis_error"] = str(exc)
        return state

    def _generate_report(self, state: ReportAgentState) -> ReportAgentState:
        if state.get("analysis") is None:
            # Analysis failed upstream; keep the friendly message set there.
            return state
        state["report"] = self.report_service.build(state["request"], state["analysis"])
        state["message"] = "报告已生成。"
        state["status"] = AgentStatus.OK
        return state

    def _write_memory(self, state: ReportAgentState) -> ReportAgentState:
        request = state["request"]
        intent = state.get("intent")
        current_intent = {}
        pending_slots = []
        mode = str(state.get("status") or "idle")
        if intent is not None:
            current_intent = {
                "message_type": intent.message_type,
                "task": intent.task,
                "region": intent.region,
                "time_range": intent.time_range,
                "missing_fields": intent.missing_fields,
                "confirmation_fields": intent.confirmation_fields,
                "confidence": intent.confidence,
                "source": intent.source,
                "scenario": intent.scenario,
                "before_time_range": getattr(request, "before_time_range", ""),
                "after_time_range": getattr(request, "after_time_range", ""),
            }
            pending_slots = intent.missing_fields or intent.confirmation_fields
        if state.get("status") == AgentStatus.CHAT:
            previous = self.memory_service.snapshot(request.session_id)
            current_intent = previous.get("current_intent") or current_intent
            pending_slots = previous.get("pending_slots") or pending_slots
        summary = self._summarize_state(state)
        self.memory_service.update(
            request.session_id,
            current_intent=current_intent,
            pending_slots=pending_slots,
            summary=summary,
            mode=mode,
        )
        self.memory_service.append_agent(request.session_id, str(state.get("message") or ""))
        report = state.get("report")
        if report is not None:
            self.memory_service.record_report(
                request.session_id,
                title=report.title,
                html_url=report.html_url,
                markdown_url=report.markdown_url,
                map_html_url=report.map_html_url,
                request=to_dict(request),
            )
            self.memory_service.set_report_context(
                request.session_id,
                self._report_context(request, state.get("analysis"), report),
            )
        state["memory"] = self.memory_service.snapshot(request.session_id)
        return state

    def _report_context(self, request: ReportRequest, analysis: Any, report: Any) -> dict[str, Any]:
        """Compact snapshot of the last report so follow-up questions stay grounded."""
        return {
            "title": report.title,
            "region": getattr(analysis, "region", request.region),
            "task": getattr(analysis, "task", request.task),
            "time_range": getattr(analysis, "time_range", request.time_range),
            "summary": report.abstract,
            "metrics": [{"label": m.label, "value": m.value} for m in (report.metrics or [])],
            "distribution": getattr(analysis, "data_table", []) or [],
            "sections": report.sections or [],
            # Patch(es) actually used, so a task/month change reuses the same location.
            "used_patch_ids": self._used_patch_ids(analysis),
        }

    @staticmethod
    def _has_bbox(aoi: Any) -> bool:
        return (
            isinstance(aoi, dict)
            and aoi.get("type") == "bbox"
            and isinstance(aoi.get("coordinates"), list)
            and len(aoi["coordinates"]) == 4
        )

    def _has_analysis_area(self, request: ReportRequest, region: str, memory: dict[str, Any]) -> bool:
        """Haidian requires an explicit or previously confirmed map selection."""
        region_text = str(region or "")
        if "海淀" not in region_text and region_text.lower() not in {"haidian", "beijing_haidian"}:
            return True
        if request.selected_patch_ids or self._has_bbox(request.aoi):
            return True
        context = (memory or {}).get("report_context") or {}
        return context.get("region") == region and bool(context.get("used_patch_ids"))

    @staticmethod
    def _used_patch_ids(analysis: Any) -> list[str]:
        payload = getattr(analysis, "aef_payload", {}) or {}
        used = payload.get("used_patch_ids")
        if isinstance(used, list) and used:
            return list(dict.fromkeys(str(item) for item in used if str(item).strip()))
        patch_results = payload.get("patch_results")
        if isinstance(patch_results, list):
            ids = [
                str(item.get("patch_id"))
                for item in patch_results
                if isinstance(item, dict) and item.get("status") == "ok" and item.get("patch_id")
            ]
            if ids:
                return list(dict.fromkeys(ids))
        patches = payload.get("patches")
        if isinstance(patches, list):
            ids = [
                str(item.get("patch_id"))
                for item in patches
                if isinstance(item, dict) and item.get("patch_id")
            ]
            if ids:
                return list(dict.fromkeys(ids))
        patch = payload.get("patch")
        if isinstance(patch, dict) and patch.get("patch_id"):
            return [str(patch["patch_id"])]
        sample_indices = payload.get("sample_indices")
        if isinstance(sample_indices, list) and sample_indices:
            return [f"patch_{int(i):06d}" for i in sample_indices]
        selected = payload.get("selected_patch_ids")
        if isinstance(selected, list) and selected:
            return [str(s) for s in selected]
        return []

    def _summarize_state(self, state: ReportAgentState) -> str:
        intent = state.get("intent")
        if intent is None:
            return ""
        if state.get("status") == AgentStatus.NEEDS_INPUT:
            return f"用户正在准备{intent.region}{intent.task}报告，缺少字段：{','.join(intent.missing_fields)}。"
        if state.get("status") == AgentStatus.CHAT:
            return "用户正在与遥感报告助手进行自然语言对话。"
        return f"最近一次报告任务：{intent.region}，{intent.task}，{intent.time_range}。"
