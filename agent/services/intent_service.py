from __future__ import annotations

import json
import re
import time
from datetime import date

from agent.config import IntentConfig
from agent.schemas.report import AgentIntent, MessageType, ReportRequest, infer_time_range
from agent.services.common import extract_json_object
from agent.services.llm_provider import DeepSeekProvider, LLMProvider
from agent.taxonomy import (
    REGION_ALIASES,
    SUPPORTED_REGIONS,
    SUPPORTED_TASKS,
    TASK_ALIASES,
)


class IntentService:
    """Extract normalized report parameters before AEF execution."""

    def __init__(
        self,
        llm: LLMProvider | None = None,
        config: IntentConfig | None = None,
        today: date | None = None,
    ) -> None:
        self.llm = llm or DeepSeekProvider()
        self.config = config or IntentConfig()
        self.today = today

    # Scenario A trigger phrases (片区综合体检). Kept deterministic so a checkup
    # request never depends on the LLM being reachable.
    _CHECKUP_CUES = (
        "体检", "综合分析", "综合评估", "片区分析", "片区评估",
        "整体分析", "整体评估", "摸底", "综合体检",
    )
    # Scenario B (建设扰动短周期监测). Two-date change over an AOI.
    _CHANGE_CUES = (
        "变化", "扰动", "监测", "两期", "对比", "比对", "对照", "新增", "增加了",
        "扩张", "扩建", "变了", "有没有变", "变化检测", "动态", "前后对比",
    )
    # Scenario C (高硬化低绿地压力评分 / 补绿优先区).
    _SCORE_CUES = (
        "压力", "硬化", "绿地率", "补绿", "增绿", "绿化优先", "补绿优先", "优先区",
        "最需要绿化", "哪里该绿化", "哪些地方缺绿", "缺绿", "最缺绿", "绿量不足",
        "压力评分", "优先绿化", "该绿化",
    )

    def _has_scenario_cue(self, prompt: str) -> bool:
        text = prompt or ""
        return (
            any(c in text for c in self._CHANGE_CUES)
            or any(c in text for c in self._CHECKUP_CUES)
            or any(c in text for c in self._SCORE_CUES)
        )

    def _detect_scenario(self, request: ReportRequest, intent: AgentIntent) -> str:
        # Scenarios are report-producing intents; skip pure chat/follow-up turns.
        # (Questions stay follow-up by the codebase's "questions never trigger a
        # report" rule — the user must ask explicitly.)
        if intent.message_type in {MessageType.FREE_CHAT, MessageType.FOLLOW_UP}:
            return ""
        text = request.prompt or ""
        # Change monitoring wins over checkup when a two-date intent is expressed,
        # so "对比两个月的变化" isn't swallowed by a generic "分析" cue. Score
        # (补绿优先) is checked before checkup for the same reason.
        if any(cue in text for cue in self._CHANGE_CUES):
            return "change"
        if any(cue in text for cue in self._SCORE_CUES):
            return "score"
        if any(cue in text for cue in self._CHECKUP_CUES):
            return "checkup"
        return ""

    def parse(self, request: ReportRequest) -> AgentIntent:
        started = time.perf_counter()
        rule_intent = self._validate(self._parse_with_rules(request))
        rule_intent.debug["rule_elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        if self.config.rules_first and rule_intent.confidence >= self.config.rule_confidence_threshold:
            rule_intent.debug["llm_skipped"] = True
            rule_intent.scenario = self._detect_scenario(request, rule_intent)
            return rule_intent

        llm_started = time.perf_counter()
        llm_intent = self._parse_with_llm(request)
        if llm_intent is not None:
            llm_intent.debug["llm_elapsed_ms"] = int((time.perf_counter() - llm_started) * 1000)
            llm_intent.debug["rule_candidate"] = {
                "message_type": rule_intent.message_type,
                "task": rule_intent.task,
                "region": rule_intent.region,
                "time_range": rule_intent.time_range,
                "confidence": rule_intent.confidence,
            }
            validated = self._validate(llm_intent)
            # Scenario is decided by deterministic cues, not the LLM, so a checkup
            # request works even when the model rewrites the task/message_type.
            validated.scenario = self._detect_scenario(request, validated)
            return validated

        rule_intent.debug["llm_status"] = getattr(self.llm, "last_status", "not_called")
        rule_intent.debug["llm_elapsed_ms"] = int((time.perf_counter() - llm_started) * 1000)
        rule_intent.scenario = self._detect_scenario(request, rule_intent)
        return rule_intent

    def _parse_with_llm(self, request: ReportRequest) -> AgentIntent | None:
        system_prompt = (
            "你是遥感分析任务的意图解析器。你的任务是把用户输入和前端选择项转换为标准 JSON，"
            "供后续 AEF 遥感模型调用。只输出 JSON，不要输出解释文字。"
        )
        user_prompt = json.dumps(
            {
                "前端选择任务": request.task,
                "前端选择地区": request.region,
                "用户自然语言": request.prompt,
                "支持任务": SUPPORTED_TASKS,
                "支持地区": SUPPORTED_REGIONS,
                "当前日期": (self.today or date.today()).isoformat(),
                "要求": {
                    "message_type": "report_request / slot_fill / free_chat / change_context / confirmation / follow_up 之一；"
                    "日常闲聊、问候、常识/时间/天气等无关问题一律 free_chat；"
                    "对上一次已生成报告结果的追问、解释、深入讨论，或要求换种说法/精简/展开/重写/总结"
                    "（未指定新的任务或月份）用 follow_up",
                    "task": "只有用户文本明确提到或前端已选择时才填写，且必须是支持任务之一；否则返回空字符串，绝不猜测",
                    "region": "必须是支持地区之一，优先使用前端选择，除非用户文本明确改写",
                    "time_range": "YYYY-MM 格式；如果用户没有明确月份，返回空字符串",
                    "missing_fields": "报告请求缺少的字段名列表；缺任务时包含 task，缺月份时包含 time_range",
                    "confirmation_fields": "需要用户确认的字段名列表；通常为空",
                    "confidence": "0 到 1 的数字",
                },
                "输出 JSON 示例": {
                    "message_type": "report_request",
                    "task": "地物分类",
                    "region": "雅江区域",
                    "time_range": "2025-10",
                    "missing_fields": [],
                    "confidence": 0.92,
                },
            },
            ensure_ascii=False,
        )
        text = self.llm.complete(system_prompt, user_prompt)
        if not text:
            return None
        payload = extract_json_object(text)
        if payload is None:
            return None
        return AgentIntent(
            message_type=str(payload.get("message_type") or "report_request"),
            task=str(payload.get("task") or request.task),
            region=str(payload.get("region") or request.region),
            time_range=str(payload.get("time_range") or ""),
            user_prompt=request.prompt,
            missing_fields=list(payload.get("missing_fields") or []),
            confirmation_fields=list(payload.get("confirmation_fields") or []),
            confidence=float(payload.get("confidence") or 0.0),
            source="deepseek",
            debug={"llm_status": getattr(self.llm, "last_status", "ok")},
        )

    def _parse_with_rules(self, request: ReportRequest) -> AgentIntent:
        message_type = MessageType.REPORT_REQUEST
        prompt = request.prompt.strip()
        confirmation_words = ["确认", "沿用", "可以", "好的", "没问题", "继续", "用上次"]
        negative_words = ["不要", "不是", "重新", "换一个", "不沿用"]
        capability_questions = [
            "你是谁",
            "你是什么",
            "你是什么助手",
            "你能做什么",
            "你可以做什么",
            "你会做什么",
            "你会干什么",
            "你是干什么",
            "你有什么功能",
            "介绍一下你",
            "介绍你自己",
        ]
        greeting_or_chat = ["你好", "您好", "闲聊", "聊聊天"]
        report_signals = [
            "报告",
            "分析",
            "生成",
            "出一份",
            "看一下",
            "看看",
            "地物",
            "水体",
            "道路",
            "施工",
            "高程",
            "地形",
            "土地覆盖",
            "土地利用",
            "用地",
            "建筑物",
            "建筑",
            "遥感",
            "分类",
            "分布",
            "重建",
            "去年",
            "今年",
            "明年",
            "上月",
            "本月",
            "月份",
            "月",
        ]
        has_report_signal = any(key in prompt for key in report_signals)
        is_short_user_question = "你" in prompt and len(prompt) <= 16 and not has_report_signal
        if (
            any(key in prompt for key in capability_questions)
            or any(key in prompt for key in greeting_or_chat)
            or is_short_user_question
        ):
            message_type = MessageType.FREE_CHAT
        if any(key in prompt for key in ["改成", "换成", "切换到", "地区改", "任务改"]):
            message_type = MessageType.CHANGE_CONTEXT
        if any(key in prompt for key in confirmation_words) and not any(key in prompt for key in negative_words):
            message_type = MessageType.CONFIRMATION
        if request.time_range and re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", request.time_range):
            message_type = MessageType.SLOT_FILL

        # Task is never silently defaulted — an empty task means "ask the user".
        task = request.task if request.task in SUPPORTED_TASKS else ""
        task_in_prompt = False
        for candidate in SUPPORTED_TASKS:
            if candidate in prompt:
                task = candidate
                task_in_prompt = True
        for key, candidate in TASK_ALIASES.items():
            if key in prompt:
                task = candidate
                task_in_prompt = True
        region = request.region if request.region in SUPPORTED_REGIONS else "雅江区域"
        region_in_prompt = False
        for candidate in SUPPORTED_REGIONS:
            if candidate in prompt:
                region = candidate
                region_in_prompt = True
        for key, candidate in REGION_ALIASES.items():
            if key in prompt:
                region = candidate
                region_in_prompt = True
        time_range = request.time_range or infer_time_range(prompt, today=self.today)
        if time_range and message_type == MessageType.REPORT_REQUEST and len(prompt) <= 12:
            message_type = MessageType.SLOT_FILL
        # A question that asks to explain/expand on existing results (and does not
        # name a new task or month) is a follow-up discussion, not a new report.
        followup_cues = [
            # 提问 / 解释
            "详细", "详解", "讲讲", "讲解", "解释", "展开", "为什么", "为何",
            "怎么理解", "如何理解", "什么意思", "啥意思", "具体说", "具体讲",
            "说说", "解读", "没看懂", "看不懂", "这个结论", "这一点", "这部分",
            "上面", "刚才", "刚刚", "报告里", "这份报告", "那份报告", "怎么得出", "依据",
            # 改写 / 微调 / 重组
            "重写", "改写", "重新写", "再写", "润色", "精简", "精简版", "简版",
            "简洁", "简短", "缩短", "浓缩", "通俗", "白话", "口语", "换个说法",
            "换种说法", "换个角度", "重新组织", "总结一下", "概括", "简单点", "详细点",
        ]
        if (
            message_type == MessageType.REPORT_REQUEST
            and not task_in_prompt
            and not time_range
            and any(cue in prompt for cue in followup_cues)
        ):
            message_type = MessageType.FOLLOW_UP

        # A question (even one that names a region/task/month) is conversation, not a
        # report request — unless the user explicitly asks to produce one. Route it to
        # discussion so we never surprise the user with a report they didn't ask for.
        request_verbs = [
            "生成", "出一份", "出份", "做一份", "做份", "来一份", "来份", "给我",
            "帮我生成", "帮我出", "帮我做", "帮我来", "写一份", "整一份",
        ]
        has_request_verb = any(v in prompt for v in request_verbs)
        question_tail = prompt.rstrip("。.！!~ 、,，").endswith(
            ("吗", "呢", "?", "？", "什么", "哪些", "哪个", "哪年", "哪月", "啥")
        )
        question_phrases = [
            "什么", "区别", "为什么", "为何", "啥意思", "是不是", "是否", "准不准", "准吗",
            "靠谱", "怎么样", "怎样", "如何", "有没有", "能分析", "能做",
            "支持哪", "可用月份", "多少", "哪些任务", "哪些月份", "可以分析", "对比一下",
        ]
        is_question = question_tail or any(p in prompt for p in question_phrases)
        # An imperative scenario ask ("前后对比一下建筑扩张") can trip a soft question
        # phrase ("对比一下") without being a real question. If there's a scenario cue,
        # no hard question tail, AND no evaluative question phrase (which always means
        # the user is questioning an existing result), keep it a report request.
        evaluative_q = ["准不准", "准吗", "靠谱", "怎么样", "怎样", "是不是", "是否", "为什么", "为何", "区别", "啥意思"]
        soft_question_only = (
            is_question
            and not question_tail
            and self._has_scenario_cue(prompt)
            and not any(q in prompt for q in evaluative_q)
        )
        if (
            message_type == MessageType.REPORT_REQUEST
            and is_question
            and not has_request_verb
            and not soft_question_only
        ):
            message_type = MessageType.FOLLOW_UP

        has_report_content = task_in_prompt or region_in_prompt or "报告" in prompt or "专题" in prompt
        # An imperative scenario ask (体检/变化监测…) is a genuine report request even
        # without a task/month in the text — keep it rules-first so it doesn't defer to
        # the LLM and get flipped non-deterministically. Questions are excluded above.
        scenario_cue = self._has_scenario_cue(prompt)
        if message_type in {MessageType.FREE_CHAT, MessageType.CHANGE_CONTEXT, MessageType.CONFIRMATION, MessageType.FOLLOW_UP}:
            confidence = 0.82
        elif message_type == MessageType.REPORT_REQUEST and scenario_cue:
            confidence = 0.84
        elif has_report_content and (time_range or task_in_prompt):
            # Genuine report request — real report content is present.
            confidence = 0.84
        else:
            # Ambiguous (e.g. chit-chat, or "报告" with nothing else). Let the LLM
            # decide chat-vs-report when a key is configured; degrade to rules otherwise.
            confidence = 0.5
        return AgentIntent(
            message_type=message_type,
            task=task,
            region=region,
            time_range=time_range,
            user_prompt=request.prompt,
            confidence=confidence,
            source="rule",
        )

    def _validate(self, intent: AgentIntent) -> AgentIntent:
        missing = set(intent.missing_fields)
        confirmation = set(intent.confirmation_fields)
        # Region keeps a sensible default; task is never silently defaulted.
        if intent.region not in SUPPORTED_REGIONS:
            intent.region = "雅江区域"
        if intent.task not in SUPPORTED_TASKS:
            intent.task = ""
        if intent.message_type not in {
            MessageType.REPORT_REQUEST,
            MessageType.SLOT_FILL,
            MessageType.FREE_CHAT,
            MessageType.CHANGE_CONTEXT,
            MessageType.CONFIRMATION,
            MessageType.FOLLOW_UP,
        }:
            intent.message_type = MessageType.REPORT_REQUEST
        if intent.message_type in {MessageType.FREE_CHAT, MessageType.FOLLOW_UP}:
            intent.missing_fields = []
            intent.confirmation_fields = []
            return intent
        # Month slot.
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", intent.time_range):
            intent.time_range = ""
            if intent.message_type != MessageType.CONFIRMATION:
                missing.add("time_range")
        else:
            missing.discard("time_range")
            confirmation.discard("time_range")
        # Task slot — required for report intents; filled from memory on confirmation/slot-fill.
        if intent.task:
            missing.discard("task")
        elif intent.message_type != MessageType.CONFIRMATION:
            missing.add("task")
        if intent.message_type == MessageType.CHANGE_CONTEXT and intent.time_range:
            missing.discard("time_range")
        if intent.message_type == MessageType.CONFIRMATION:
            missing.discard("time_range")
            missing.discard("task")
        intent.missing_fields = sorted(missing)
        intent.confirmation_fields = sorted(confirmation)
        return intent
