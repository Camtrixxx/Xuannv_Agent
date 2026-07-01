from __future__ import annotations

import json
from typing import Any, TypedDict

from agent.schemas.report import AgentResponse, AgentRoute, AgentStatus, MessageType, ReportRequest, to_dict
from agent.services.aef_analysis_service import AEFAnalysisService
from agent.services.analysis_service import MockAnalysisService
from agent.services.intent_service import IntentService
from agent.services.llm_provider import DeepSeekProvider, LLMProvider
from agent.services.common import strip_markdown
from agent.services.memory_service import MemoryService
from agent.services.region_availability import (
    coverage_hint,
    is_month_available,
    region_tasks,
    unavailable_message,
)
from agent.services.regional_analysis_service import RegionalAnalysisService
from agent.services.report_service import ReportService

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
    ) -> None:
        self.intent_service = intent_service or IntentService()
        self.memory_service = memory_service or MemoryService()
        self.chat_llm = chat_llm or DeepSeekProvider()
        self.analysis_service = analysis_service or RegionalAnalysisService()
        self.report_service = report_service or ReportService()
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
            if state.get("status") in {AgentStatus.NEEDS_INPUT, AgentStatus.CHAT}:
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

        if intent.message_type in {MessageType.FREE_CHAT, MessageType.FOLLOW_UP}:
            state["status"] = AgentStatus.CHAT
            state["message"] = ""
            return state

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
        if not intent.task:
            missing.append("task")
        if not intent.time_range:
            missing.append("time_range")
        intent.missing_fields = missing
        intent.confirmation_fields = []

        if missing:
            state["status"] = AgentStatus.NEEDS_INPUT
            state["message"] = self._clarify_message(missing, intent.region)
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

        state["status"] = AgentStatus.OK
        state["message"] = "已完成意图解析，准备执行遥感分析。"
        return state

    def _route_after_merge(self, state: ReportAgentState) -> str:
        if state.get("status") == AgentStatus.CHAT:
            return AgentRoute.CHAT_RESPONSE
        if state.get("status") == AgentStatus.NEEDS_INPUT:
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
            user_prompt = (
                f"上一次报告内容（JSON）：\n{json.dumps(report_context, ensure_ascii=False)}\n\n"
                f"用户的需求：{request.prompt}\n\n请据此给出结果。"
            )
        else:
            system_prompt = (
                "你是遥感报告助手。请用简洁自然的中文回答用户，不要生成报告，除非用户明确要求。"
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
        normalized_request = ReportRequest(
            task=intent.task,
            region=intent.region,
            prompt=intent.user_prompt,
            time_range=intent.time_range,
            session_id=state["request"].session_id,
            selected_patch_ids=state["request"].selected_patch_ids,
            aoi=state["request"].aoi,
        )
        state["request"] = normalized_request
        try:
            state["analysis"] = self.analysis_service.analyze(normalized_request)
        except Exception as exc:  # Upstream/model failure — degrade to a friendly reply.
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
        }

    def _summarize_state(self, state: ReportAgentState) -> str:
        intent = state.get("intent")
        if intent is None:
            return ""
        if state.get("status") == AgentStatus.NEEDS_INPUT:
            return f"用户正在准备{intent.region}{intent.task}报告，缺少字段：{','.join(intent.missing_fields)}。"
        if state.get("status") == AgentStatus.CHAT:
            return "用户正在与遥感报告助手进行自然语言对话。"
        return f"最近一次报告任务：{intent.region}，{intent.task}，{intent.time_range}。"
