from __future__ import annotations

import html
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from agent.config import ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportArtifact, ReportRequest
from agent.services.common import extract_json_object
from agent.services.llm_provider import DeepSeekProvider, LLMProvider
from agent.services.report_skeletons import ReportSkeleton, resolve


REPORT_TEMPLATE_VERSION = "agent-report-v9"
MAP_TEMPLATE_VERSION = "agent-result-map-v1"

# Metric cards that are metadata/plumbing rather than business findings. These are
# already conveyed by the header chips, so we keep them out of the metric grid.
_META_METRIC_LABELS = {
    "任务",
    "地区",
    "时间",
    "模型",
    "样本数",
    "Patch",
    "可用月份",
    "经纬度范围",
    "专题 ID",
    "专题ID",
    "接口状态",
    "结果尺寸",
}

_SOURCE_DISPLAY = {
    "aef_inference": "雅江遥感分析模型",
    "harbin_embedding_api": "哈尔滨在线专题服务",
    "haidian_embedding_api": "海淀在线专题服务",
    "prototype": "遥感分析流程",
}


class ReportService:
    def __init__(
        self,
        report_dir: str | Path = "agent/reports",
        llm: LLMProvider | None = None,
        config: ReportConfig | None = None,
    ) -> None:
        self.config = config or ReportConfig()
        self.report_dir = Path(report_dir) if report_dir != "agent/reports" else self.config.report_dir
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.llm = llm or DeepSeekProvider()
        # Why the *content* fell back, independent of transport health. A call can
        # return HTTP 200 ("ok") yet carry JSON we can't use, in which case the
        # report is template-generated — that must not be reported as "deepseek".
        self.last_content_status = "not_called"

    def build(self, request: ReportRequest, analysis: AnalysisResult) -> ReportArtifact:
        return self._build_artifact(request, analysis)

    def revise(
        self,
        request: ReportRequest,
        analysis: AnalysisResult,
        instruction: str,
        previous_context: dict | None = None,
    ) -> ReportArtifact:
        """Create a new report version from existing analysis, without inference."""
        revision_id = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        return self._build_artifact(
            request,
            analysis,
            revision_instruction=str(instruction or "").strip(),
            previous_context=previous_context or {},
            revision_id=revision_id,
        )

    def _build_artifact(
        self,
        request: ReportRequest,
        analysis: AnalysisResult,
        *,
        revision_instruction: str = "",
        previous_context: dict | None = None,
        revision_id: str = "",
    ) -> ReportArtifact:
        version_label = self._revision_label(revision_instruction) if revision_instruction else ""
        title = f"{analysis.headline}{f'·{version_label}' if version_label else ''}报告"
        identity = self._report_identity(request, analysis)
        if revision_instruction:
            identity = f"{identity}-revision-{revision_id}"
        slug = self._slug(identity)
        html_path = self.report_dir / f"{slug}.html"
        md_path = self.report_dir / f"{slug}.md"
        map_path = self.report_dir / f"{slug}.map.html"
        metrics = self._business_metrics(analysis)
        map_html_url = self._write_map_page(request, analysis, map_path)
        if revision_instruction and not map_html_url:
            artifact = (previous_context or {}).get("artifact") or {}
            map_html_url = str(artifact.get("map_html_url") or "")

        if not revision_instruction and self.config.reuse_existing and self._can_reuse(html_path, md_path):
            abstract = self._read_existing_abstract(md_path) or self._fallback_summary(request, analysis)
            return ReportArtifact(
                title=title,
                abstract=abstract,
                sections=[],
                metrics=metrics,
                charts=analysis.charts,
                html_url=f"/reports/{html_path.name}",
                markdown_url=f"/reports/{md_path.name}",
                map_html_url=map_html_url,
                llm_provider="reused",
                reused=True,
                generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

        self.last_content_status = "ok"
        content = self._generate_content(
            request,
            analysis,
            metrics,
            revision_instruction=revision_instruction,
            previous_context=previous_context or {},
        )
        llm_status = getattr(self.llm, "last_status", "template")
        content_status = self.last_content_status
        if llm_status == "ok" and content_status == "ok":
            llm_provider = "deepseek"
        elif llm_status == "ok":
            # Transport succeeded but the content was unusable — this report is
            # template-generated and must say so.
            llm_provider = f"template:{content_status}"
        else:
            llm_provider = f"template:{llm_status}"

        html_path.write_text(self._render_html(title, content, analysis, metrics), encoding="utf-8")
        md_path.write_text(self._render_markdown(title, content, analysis, metrics), encoding="utf-8")
        self._prune_reports()

        sections = [{"heading": block["title"], "body": block["text"]} for block in content["analysis"]]
        return ReportArtifact(
            title=title,
            abstract=content["summary"],
            sections=sections,
            metrics=metrics,
            charts=analysis.charts,
            html_url=f"/reports/{html_path.name}",
            markdown_url=f"/reports/{md_path.name}",
            map_html_url=map_html_url,
            llm_provider=llm_provider,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            debug={
                "llm_status": llm_status,
                "content_status": content_status,
                "slug": slug,
                "revision": bool(revision_instruction),
                "revision_instruction": revision_instruction,
            },
        )

    # ------------------------------------------------------------------ content

    def _generate_content(
        self,
        request: ReportRequest,
        analysis: AnalysisResult,
        metrics: list[MetricCard],
        *,
        revision_instruction: str = "",
        previous_context: dict | None = None,
    ) -> dict:
        skeleton = self._skeleton_for(request, analysis, revision_instruction=revision_instruction)
        system_prompt = (
            "你在为业务读者撰写一份遥感分析报告。"
            f"这份报告要回答的问题是：{skeleton.question}"
            f"读者是：{skeleton.audience}"
            "写作要求：结论先行，把数字翻译成读者能直观理解的说法"
            "（例如“约三分之一的地面是建筑”而不是“覆盖率36.27%，按面积加权计算”）；"
            "少用术语，必须出现的技术口径放到最后一节。"
            "不要罗列系统参数、接口字段或免责声明，不要出现模型文件、服务地址、"
            "patch 编号等技术细节。必须忠于给定数据，"
            "严禁编造未提供的数字、坐标或事件；不得引入输入中没有的行业阈值、典型范围、"
            "比较基准、因果归因或可靠性评级。没有明确精度证据时，不得声称结果可靠、准确或"
            "可直接替代现场核查。只输出 JSON，不要输出多余文字。"
        )
        if revision_instruction:
            system_prompt += (
                "这是一次已有报告的编辑操作，不得重新分析或改变任何指标。必须按照用户编辑要求调整"
                "篇幅、结构、语气和重点；未要求修改的事实与结论保持一致。"
            )
        payload_data = {
                "区域": request.region,
                "任务": analysis.task,
                "时间": request.time_range,
                "分析摘要线索": analysis.summary,
                "关键指标": [{"名称": m.label, "数值": m.value} for m in metrics],
                "数据分布": [
                    {"类别": row.get("label"), "占比": row.get("ratio")}
                    for row in analysis.data_table
                ],
                "发现线索": analysis.findings,
                "风险线索": analysis.risks,
                "方法与数据边界": [*analysis.method_notes, *analysis.limitations, *analysis.confidence_notes],
                "图表": [{"标题": c.title, "说明": c.caption} for c in analysis.charts],
                "输出格式": skeleton.output_format(),
                "禁止": [
                    "不要出现模型文件路径、服务地址、patch 编号、接口字段、坐标系等系统内部信息",
                    "不要出现 mock、占位、模拟、原型等字样",
                    "不要编造输入中没有的具体数字、坐标或真实事件",
                    "不要用外部常识补充典型阈值、行业平均值或对比基准",
                    "不要把全区域任务可用性摘要解释为本次选区的空间结论",
                ],
            }
        if revision_instruction:
            payload_data["报告编辑要求"] = revision_instruction
            payload_data["上一版报告"] = {
                "标题": (previous_context or {}).get("title"),
                "摘要": (previous_context or {}).get("summary"),
                # Normalize to the same {title, text} keys the output format asks
                # for. The stored artifact uses {heading, body}; feeding those in
                # verbatim taught the model to answer in *those* keys, which
                # _clean_blocks then rejected → silent template fallback.
                "章节": self._as_output_blocks((previous_context or {}).get("sections")),
            }
            payload_data["输出格式"] = self._revision_output_format(
                revision_instruction, skeleton=skeleton
            )
        payload = json.dumps(payload_data, ensure_ascii=False, indent=2)
        text = self.llm.complete(system_prompt, payload)
        if text:
            parsed = extract_json_object(text)
            if parsed:
                # Allow the skeleton's own section count (+1 slack) rather than a
                # fixed cap, so a longer skeleton or an expanded revision is not
                # silently truncated.
                analysis_blocks = self._clean_blocks(
                    parsed.get("analysis"), limit=len(skeleton.section_plan()) + 1
                )
                if analysis_blocks:
                    return {
                        "summary": str(parsed.get("summary") or self._fallback_summary(request, analysis)),
                        "highlights": self._list_or_default(parsed.get("highlights"), analysis.findings)[:5],
                        "analysis": analysis_blocks,
                        "recommendations": self._list_or_default(
                            parsed.get("recommendations"), self._merged_actions(analysis)
                        )[:6],
                    }
                self.last_content_status = "unusable_blocks"
            else:
                self.last_content_status = "unparsable_json"
        return self._fallback_content(
            request,
            analysis,
            revision_instruction=revision_instruction,
            previous_context=previous_context or {},
            skeleton=skeleton,
        )

    # Key aliases the model legitimately uses for a section's heading and prose.
    # "heading"/"body" in particular is what the stored artifact uses, so a
    # revision prompt echoing the previous version invites that spelling back.
    _BLOCK_TITLE_KEYS = ("title", "heading", "小标题", "标题")
    _BLOCK_TEXT_KEYS = ("text", "body", "content", "正文", "内容")

    @classmethod
    def _first_str(cls, item: dict, keys) -> str:
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _skeleton_for(
        request: ReportRequest,
        analysis: AnalysisResult,
        *,
        revision_instruction: str = "",
    ) -> ReportSkeleton:
        """Which report shape this analysis gets.

        The scenario is read from the analysis payload the scenario services
        stamp, so a checkup keeps its own shape no matter what task name it
        carries. A revision reuses the original's skeleton — an edit changes
        length and tone, never which questions the report answers.
        """
        payload = analysis.aef_payload if isinstance(analysis.aef_payload, dict) else {}
        return resolve(
            task=analysis.task or request.task,
            scenario=str(payload.get("scenario") or ""),
            custom_object=str(
                getattr(request, "target_object", "") or payload.get("custom_class") or ""
            ),
        )

    # Headings for blocks the model returned without one, in order.
    _UNTITLED_BLOCK_TITLES = ("分析解读", "延伸解读", "补充说明", "其他观察", "数据边界")

    def _clean_blocks(self, value, *, limit: int = 5) -> list[dict[str, str]]:
        """Normalize the model's ``analysis`` field into ``{title, text}`` blocks.

        Three shapes occur in practice and all carry real content: a list of
        heading/body objects, a list of bare paragraphs, and — for terse
        revisions — a single prose string. Accepting only the first shape meant a
        good response was dropped and the template silently rendered instead.
        """
        if isinstance(value, str):
            value = [value]
        blocks: list[dict[str, str]] = []
        if not isinstance(value, list):
            return blocks
        untitled = 0
        for item in value:
            if isinstance(item, dict):
                title = self._first_str(item, self._BLOCK_TITLE_KEYS)
                body = self._first_str(item, self._BLOCK_TEXT_KEYS)
            elif isinstance(item, str):
                title, body = "", item.strip()
            else:
                continue
            # Prose without a heading is still usable; only a body-less block is
            # worthless. Untitled blocks get distinct generic headings so the
            # report never shows the same heading twice.
            if not body:
                continue
            if not title:
                title = self._UNTITLED_BLOCK_TITLES[
                    min(untitled, len(self._UNTITLED_BLOCK_TITLES) - 1)
                ]
                untitled += 1
            blocks.append({"title": title, "text": body})
        return blocks[:limit]

    @classmethod
    def _as_output_blocks(cls, sections) -> list[dict[str, str]]:
        """Restate stored {heading, body} sections in the {title, text} shape."""
        blocks: list[dict[str, str]] = []
        for item in sections or []:
            if not isinstance(item, dict):
                continue
            blocks.append({
                "title": cls._first_str(item, cls._BLOCK_TITLE_KEYS),
                "text": cls._first_str(item, cls._BLOCK_TEXT_KEYS),
            })
        return blocks

    def _fallback_content(
        self,
        request: ReportRequest,
        analysis: AnalysisResult,
        *,
        revision_instruction: str = "",
        previous_context: dict | None = None,
        skeleton: ReportSkeleton | None = None,
    ) -> dict:
        # Borrow the skeleton's headings so a template-generated report still
        # reads as the right kind of report, rather than a generic 分析解读 stub.
        headings = [item["heading"] for item in (skeleton or resolve()).section_plan()]
        blocks = [{
            "title": headings[0],
            "text": analysis.summary or self._fallback_summary(request, analysis),
        }]
        findings = [f for f in analysis.findings if "Agent" not in f and "标准化" not in f]
        if findings and len(headings) > 1:
            blocks.append({"title": headings[1], "text": " ".join(findings[:4])})
        boundary = [*analysis.limitations, *analysis.confidence_notes]
        if boundary:
            blocks.append({"title": headings[-1], "text": " ".join(boundary[:3])})
        content = {
            "summary": self._fallback_summary(request, analysis),
            "highlights": (findings or analysis.findings)[:5],
            "analysis": blocks,
            "recommendations": self._merged_actions(analysis)[:6],
        }
        if revision_instruction and self._is_compact_revision(revision_instruction):
            previous_summary = str((previous_context or {}).get("summary") or content["summary"])
            content["summary"] = previous_summary[:180]
            content["highlights"] = content["highlights"][:3]
            content["analysis"] = content["analysis"][:1]
            content["recommendations"] = content["recommendations"][:3]
        return content

    @staticmethod
    def _is_compact_revision(instruction: str) -> bool:
        return any(key in instruction for key in ["精简", "简版", "简洁", "简短", "缩短", "浓缩", "概括", "简单点"])

    # Words that mean "shorter than the last shortening". Without a second level,
    # "再精简一点" produced a byte-for-byte rerun of "精简一下" — the same contract,
    # so the same output, which reads as the edit having been ignored.
    _COMPACT_INTENSIFIERS = ("再", "还要", "更", "特别", "极", "最", "非常", "超", "一句话", "太长")

    @classmethod
    def _compact_level(cls, instruction: str) -> int:
        """0 = not a shortening, 1 = shorten, 2 = shorten hard."""
        if not cls._is_compact_revision(instruction):
            # "再短一点" is a shortening even without the word 精简.
            if any(key in instruction for key in ("短一点", "短些", "太长")):
                return 2
            return 0
        return 2 if any(key in instruction for key in cls._COMPACT_INTENSIFIERS) else 1

    @classmethod
    def _revision_label(cls, instruction: str) -> str:
        level = cls._compact_level(instruction)
        if level >= 2:
            # Distinct from 精简版, so two successive shortenings are tellable
            # apart in the report list instead of showing the same title twice.
            return "极简版"
        if level == 1:
            return "精简版"
        if any(key in instruction for key in ["扩写", "扩充", "详细", "完整"]):
            return "详细版"
        if any(key in instruction for key in ["通俗", "白话", "口语"]):
            return "通俗版"
        return "修订版"

    # Every revision variant restates the section contract, because a terse
    # instruction ("精简") otherwise invites a bare prose string instead of
    # titled sections, and echoing the previous version invites its own
    # heading/body key names back.
    _BLOCK_CONTRACT = "每节必须是 {title: 小标题, text: 正文} 对象，键名只能用 title 和 text"

    @classmethod
    def _revision_output_format(
        cls, instruction: str, *, skeleton: ReportSkeleton | None = None
    ) -> dict[str, object]:
        """Output contract for an edit: same questions, different length/tone.

        The skeleton's headings are kept so a 精简版 is a shorter version of *this*
        report rather than a differently-shaped one; only the section count and
        per-section length move.
        """
        plan = (skeleton or resolve()).section_plan()
        headings = [item["heading"] for item in plan]
        level = cls._compact_level(instruction)
        if level >= 2:
            # "再精简一点" — one section plus the caveats, and the caveats shrink to
            # a single sentence. Budgets are stated as upper bounds ("不超过"),
            # because a range invites the model to write to its top end.
            kept = [headings[0], headings[-1]] if len(headings) > 1 else headings
            return {
                "summary": "不超过60字，一两句话给出最核心的结论和关键数字",
                "highlights": "最多2条，每条不超过20字",
                "analysis": f"只保留这些小节，顺序不变：{kept}；"
                f"『{kept[0]}』不超过80字，『{kept[-1]}』不超过50字且只讲最关键的一条边界；"
                f"{cls._BLOCK_CONTRACT}",
                "recommendations": "最多2条，每条不超过25字，只留最重要的",
            }
        if level == 1:
            # Keep the opening section and always keep the closing caveats.
            kept = [headings[0], headings[-1]] if len(headings) > 1 else headings
            return {
                "summary": "不超过90字，只保留核心结论、关键数字和最重要业务含义",
                "highlights": "2-3条核心要点，每条不超过25字",
                "analysis": f"只保留这些小节，顺序不变：{kept}；每节不超过110字；{cls._BLOCK_CONTRACT}",
                "recommendations": "2-3条最重要建议，每条为一个字符串",
            }
        if any(key in instruction for key in ["扩写", "扩充", "详细", "完整"]):
            return {
                "summary": "200-300字的完整执行摘要",
                "highlights": "4-6条核心要点",
                "analysis": f"保留全部小节，顺序不变：{headings}；每节220-360字；{cls._BLOCK_CONTRACT}",
                "recommendations": "4-6条分层、可执行建议，每条为一个字符串",
            }
        return {
            "summary": "严格按照编辑要求重写摘要",
            "highlights": "按照编辑要求组织核心要点",
            "analysis": f"保留这些小节，顺序不变：{headings}；"
            f"按编辑要求调整每节的篇幅、语气和重点；{cls._BLOCK_CONTRACT}",
            "recommendations": "保留事实依据并按编辑要求组织建议，每条为一个字符串",
        }

    def _fallback_summary(self, request: ReportRequest, analysis: AnalysisResult) -> str:
        base = analysis.summary.strip()
        if base:
            return base
        return (
            f"本报告聚焦{request.region}在{request.time_range}的{analysis.task}分析，"
            "综合关键指标、结果图与数据分布，给出主导特征、空间格局和后续建议。"
        )

    def _merged_actions(self, analysis: AnalysisResult) -> list[str]:
        actions = list(analysis.recommendations)
        for risk in analysis.risks:
            actions.append(f"注意：{risk}")
        return actions

    def _business_metrics(self, analysis: AnalysisResult) -> list[MetricCard]:
        return [m for m in analysis.metrics if m.label not in _META_METRIC_LABELS]

    # Keys the model uses when it returns a recommendation as an object rather
    # than a plain string (e.g. {"priority": "高", "action": "..."}).
    _ITEM_TEXT_KEYS = ("action", "text", "content", "建议", "内容", "描述", "title")
    _ITEM_LABEL_KEYS = ("priority", "优先级", "level", "等级")

    def _list_or_default(self, value, default: list[str]) -> list[str]:
        if not isinstance(value, list):
            return default
        items: list[str] = []
        for item in value:
            if isinstance(item, dict):
                # Never let a raw dict repr reach the report body.
                text = self._first_str(item, self._ITEM_TEXT_KEYS)
                if not text:
                    continue
                label = self._first_str(item, self._ITEM_LABEL_KEYS)
                items.append(f"[{label}] {text}" if label else text)
                continue
            text = str(item).strip()
            if text:
                items.append(text)
        return items or default

    # ------------------------------------------------------------------- render

    def _render_html(
        self,
        title: str,
        content: dict,
        analysis: AnalysisResult,
        metrics: list[MetricCard],
    ) -> str:
        chips = "".join(
            f"<span>{html.escape(text)}</span>"
            for text in (analysis.region, analysis.task, analysis.time_range)
            if text
        )
        highlights_html = "".join(f"<li>{html.escape(item)}</li>" for item in content["highlights"])
        metric_html = "".join(
            f"""<div class="metric"><strong>{html.escape(m.value)}</strong><span>{html.escape(m.label)}</span>"""
            + (f"<small>{html.escape(m.description)}</small>" if m.description else "")
            + "</div>"
            for m in metrics
        )
        charts_html = "".join(
            f"""<figure><img src="{html.escape(c.url)}" alt="{html.escape(c.title)}">"""
            f"""<figcaption><b>{html.escape(c.title)}</b>{html.escape(c.caption)}</figcaption></figure>"""
            for c in analysis.charts
        )
        table_html = self._distribution_html(analysis)
        patch_detail_html = self._patch_results_html(analysis)
        analysis_html = "".join(
            f"""<section class="block"><h3>{html.escape(b["title"])}</h3><p>{html.escape(b["text"])}</p></section>"""
            for b in content["analysis"]
        )
        rec_html = "".join(f"<li>{html.escape(item)}</li>" for item in content["recommendations"])
        generated_at = (analysis.generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"))[:10]
        source = _SOURCE_DISPLAY.get(analysis.data_source, "遥感分析模型")

        data_section = ""
        if charts_html or table_html or patch_detail_html:
            data_section = f"""
    <section class="card" id="data">
      <h2>结果图与数据</h2>
      <div class="figures">{charts_html}</div>
      {table_html}
      {patch_detail_html}
    </section>"""

        highlights_section = ""
        if highlights_html:
            highlights_section = f"""
    <section class="card highlights" id="highlights">
      <h2>核心要点</h2>
      <ul>{highlights_html}</ul>
    </section>"""

        metrics_section = ""
        if metric_html:
            metrics_section = f"""
    <section id="metrics">
      <h2 class="section-title">关键指标</h2>
      <div class="metrics">{metric_html}</div>
    </section>"""

        return f"""<!doctype html>
<!-- {REPORT_TEMPLATE_VERSION} -->
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg:#f5f7fa; --card:#fff; --ink:#1f2328; --muted:#6b7280; --line:#e6e9ee;
      --primary:#2563eb; --primary-soft:#eef4ff; --accent:#0f766e; --shadow:0 12px 34px rgba(15,23,42,.08);
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; color:var(--ink); background:var(--bg);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; line-height:1.85; }}
    main {{ max-width:920px; margin:0 auto; padding:32px 22px 72px; }}
    .hero {{ background:linear-gradient(135deg,#1e3a8a,#2563eb); color:#fff; border-radius:16px; padding:32px 30px;
      box-shadow:var(--shadow); }}
    .hero .eyebrow {{ font-size:13px; letter-spacing:.14em; opacity:.85; font-weight:700; }}
    .hero h1 {{ margin:12px 0 16px; font-size:27px; line-height:1.35; }}
    .chips {{ display:flex; flex-wrap:wrap; gap:8px; }}
    .chips span {{ background:rgba(255,255,255,.16); border:1px solid rgba(255,255,255,.28); border-radius:999px;
      padding:5px 13px; font-size:13px; font-weight:600; }}
    .lead {{ background:var(--card); border:1px solid var(--line); border-left:4px solid var(--primary);
      border-radius:12px; padding:20px 24px; margin:18px 0; font-size:16px; color:#374151; box-shadow:var(--shadow); }}
    h2.section-title {{ font-size:20px; margin:34px 0 14px; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:22px 24px;
      margin:18px 0; box-shadow:var(--shadow); }}
    .card h2 {{ margin:0 0 14px; font-size:20px; }}
    .highlights ul {{ margin:0; padding:0; list-style:none; display:grid; gap:10px; }}
    .highlights li {{ position:relative; padding-left:28px; }}
    .highlights li::before {{ content:"▍"; position:absolute; left:6px; color:var(--primary); }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; }}
    .metric {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px;
      box-shadow:var(--shadow); }}
    .metric strong {{ display:block; font-size:23px; color:var(--primary); }}
    .metric span {{ display:block; margin-top:4px; font-size:13px; font-weight:600; }}
    .metric small {{ display:block; margin-top:6px; color:var(--muted); font-size:12px; line-height:1.5; }}
    .figures {{ display:grid; gap:16px; }}
    figure {{ margin:0; }}
    figure img {{ width:100%; border-radius:10px; border:1px solid var(--line); }}
    figcaption {{ margin-top:8px; color:var(--muted); font-size:13px; }}
    figcaption b {{ display:block; color:var(--ink); font-size:14px; margin-bottom:2px; }}
    .dist {{ margin-top:20px; display:grid; gap:10px; }}
    .dist .row {{ display:grid; grid-template-columns:110px 1fr 62px 76px; align-items:center; gap:10px; font-size:14px; }}
    .dist .area {{ text-align:right; color:var(--muted); font-variant-numeric:tabular-nums; }}
    .dist .bar {{ background:#eef2f7; border-radius:999px; height:12px; overflow:hidden; }}
    .dist .bar i {{ display:block; height:100%; background:linear-gradient(90deg,#2563eb,#0f766e); border-radius:999px; }}
    .dist .pct {{ text-align:right; color:var(--muted); font-variant-numeric:tabular-nums; }}
    .patch-table {{ width:100%; border-collapse:collapse; margin-top:20px; font-size:13px; }}
    .patch-table th, .patch-table td {{ padding:9px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    .patch-table th {{ color:var(--muted); font-weight:600; }}
    .patch-ok {{ color:#047857; font-weight:700; }} .patch-failed {{ color:#b91c1c; font-weight:700; }}
    .block h3 {{ margin:0 0 8px; font-size:16px; color:var(--accent); }}
    .block p {{ margin:0; color:#374151; }}
    .block + .block {{ margin-top:18px; border-top:1px dashed var(--line); padding-top:18px; }}
    .rec ol {{ margin:0; padding-left:22px; display:grid; gap:10px; }}
    .footer {{ margin-top:30px; color:var(--muted); font-size:12.5px; text-align:center; }}
    @media (max-width:720px) {{ .metrics {{ grid-template-columns:repeat(2,1fr); }} .dist .row {{ grid-template-columns:84px 1fr 52px 64px; }} }}
  </style>
</head>
<body>
  <main>
    <header class="hero">
      <div class="eyebrow">遥感专题分析报告</div>
      <h1>{html.escape(title)}</h1>
      <div class="chips">{chips}</div>
    </header>
    <p class="lead">{html.escape(content["summary"])}</p>
{highlights_section}
{metrics_section}
{data_section}
    <section id="analysis">
      <h2 class="section-title">深度解读</h2>
      <div class="card">{analysis_html}</div>
    </section>
    <section class="card rec" id="rec">
      <h2>建议与提醒</h2>
      <ol>{rec_html}</ol>
    </section>
    <p class="footer">本报告由遥感报告助手自动生成 · 数据来源：{html.escape(source)} · 生成日期 {html.escape(generated_at)}</p>
  </main>
</body>
</html>
"""

    def _distribution_html(self, analysis: AnalysisResult) -> str:
        if not analysis.data_table:
            return ""
        rows = "".join(
            f"""<div class="row"><span>{html.escape(str(r.get('label')))}</span>"""
            f"""<span class="bar"><i style="width:{max(1, round(float(r.get('ratio') or 0) * 100)):d}%"></i></span>"""
            f"""<span class="pct">{float(r.get('ratio') or 0) * 100:.1f}%</span>"""
            + (f"""<span class="area">{float(r.get('value')):.1f} 公顷</span>""" if r.get("value") is not None else "")
            + "</div>"
            for r in analysis.data_table
        )
        title = html.escape(analysis.data_table_title or "数据分布")
        return f"""<h3 style="margin:22px 0 4px;font-size:15px;">{title}</h3><div class="dist">{rows}</div>"""

    def _patch_results_html(self, analysis: AnalysisResult) -> str:
        if not analysis.patch_results:
            return ""
        rows = []
        for item in analysis.patch_results:
            patch_id = html.escape(str(item.get("patch_id") or "暂无"))
            status = str(item.get("status") or "unknown")
            if status == "ok":
                metrics = item.get("metrics") or {}
                ratio = metrics.get("coverage_ratio", metrics.get("foreground_ratio"))
                if ratio is not None:
                    measure = f"覆盖率 {float(ratio) * 100:.1f}%"
                elif metrics.get("class_distribution"):
                    measure = f"{len(metrics['class_distribution'])} 类"
                else:
                    measure = "已获取结果"
                status_html = '<span class="patch-ok">已完成</span>'
            else:
                measure = str(item.get("error") or "获取失败")
                status_html = '<span class="patch-failed">未完成</span>'
            rows.append(f"<tr><td>{patch_id}</td><td>{status_html}</td><td>{html.escape(measure)}</td></tr>")
        return (
            '<h3 style="margin:22px 0 4px;font-size:15px;">Patch 处理明细</h3>'
            '<table class="patch-table"><thead><tr><th>Patch</th><th>状态</th><th>结果</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table>"
        )

    def _render_markdown(
        self,
        title: str,
        content: dict,
        analysis: AnalysisResult,
        metrics: list[MetricCard],
    ) -> str:
        lines = [f"<!-- {REPORT_TEMPLATE_VERSION} -->", "", f"# {title}", ""]
        chips = " · ".join(t for t in (analysis.region, analysis.task, analysis.time_range) if t)
        if chips:
            lines += [f"**{chips}**", ""]
        lines += [content["summary"], ""]

        if content["highlights"]:
            lines += ["## 核心要点", ""]
            lines += [f"- {item}" for item in content["highlights"]]
            lines.append("")

        if metrics:
            lines += ["## 关键指标", ""]
            for m in metrics:
                suffix = f"（{m.description}）" if m.description else ""
                lines.append(f"- **{m.label}**：{m.value}{suffix}")
            lines.append("")

        if analysis.charts or analysis.data_table or analysis.patch_results:
            lines += ["## 结果图与数据", ""]
            for c in analysis.charts:
                lines += [f"![{c.title}]({c.url})", "", f"*{c.caption}*", ""]
            if analysis.data_table:
                lines += [f"### {analysis.data_table_title or '数据分布'}", "", "| 类别 | 占比 |", "| --- | --- |"]
                for r in analysis.data_table:
                    lines.append(f"| {r.get('label')} | {float(r.get('ratio') or 0) * 100:.1f}% |")
                lines.append("")
            if analysis.patch_results:
                lines += ["### Patch 处理明细", "", "| Patch | 状态 | 结果 |", "| --- | --- | --- |"]
                for item in analysis.patch_results:
                    status = "已完成" if item.get("status") == "ok" else "未完成"
                    metrics = item.get("metrics") or {}
                    ratio = metrics.get("coverage_ratio", metrics.get("foreground_ratio"))
                    if ratio is not None:
                        result = f"覆盖率 {float(ratio) * 100:.1f}%"
                    elif metrics.get("class_distribution"):
                        result = f"{len(metrics['class_distribution'])} 类"
                    else:
                        result = str(item.get("error") or "已获取结果")
                    lines.append(f"| {item.get('patch_id', '暂无')} | {status} | {result} |")
                lines.append("")

        lines += ["## 深度解读", ""]
        for b in content["analysis"]:
            lines += [f"### {b['title']}", "", b["text"], ""]

        lines += ["## 建议与提醒", ""]
        for i, item in enumerate(content["recommendations"], 1):
            lines.append(f"{i}. {item}")
        lines.append("")

        source = _SOURCE_DISPLAY.get(analysis.data_source, "遥感分析模型")
        generated_at = (analysis.generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"))[:10]
        lines += ["---", f"*本报告由遥感报告助手自动生成 · 数据来源：{source} · 生成日期 {generated_at}*", ""]
        return "\n".join(lines)

    # ------------------------------------------------------------------ helpers

    def _write_map_page(
        self,
        request: ReportRequest,
        analysis: AnalysisResult,
        map_path: Path,
    ) -> str:
        """Write a standalone interactive map for a Haidian analysis result."""
        if "海淀" not in (analysis.region or request.region):
            map_path.unlink(missing_ok=True)
            return ""

        layers = []
        for chart in analysis.charts:
            bounds = chart.bounds_wgs84
            if not chart.overlay or not chart.url or len(bounds) != 4:
                continue
            try:
                numeric_bounds = [float(value) for value in bounds]
            except (TypeError, ValueError):
                continue
            if not all(math.isfinite(value) for value in numeric_bounds):
                continue
            min_lng, min_lat, max_lng, max_lat = numeric_bounds
            if min_lng >= max_lng or min_lat >= max_lat:
                continue
            layers.append(
                {
                    "title": chart.title or chart.patch_id or f"专题结果 {len(layers) + 1}",
                    "url": chart.url,
                    "bounds_wgs84": numeric_bounds,
                    "patch_ids": [item for item in chart.patch_id.split(",") if item],
                }
            )

        if not layers:
            map_path.unlink(missing_ok=True)
            return ""

        map_path.write_text(
            self._render_map_html(request, analysis, layers),
            encoding="utf-8",
        )
        return f"/reports/{map_path.name}"

    def _render_map_html(
        self,
        request: ReportRequest,
        analysis: AnalysisResult,
        layers: list[dict],
    ) -> str:
        payload = json.dumps(layers, ensure_ascii=False).replace("<", "\\u003c")
        title = f"{analysis.headline}结果地图"
        meta = " · ".join(
            item for item in (analysis.region or request.region, analysis.task, analysis.time_range) if item
        )
        return f"""<!doctype html>
<!-- {MAP_TEMPLATE_VERSION} -->
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root {{ --ink:#18202a; --muted:#667085; --line:#d9dee7; --panel:#fff; --accent:#1769e0; }}
    * {{ box-sizing:border-box; }}
    html, body {{ width:100%; height:100%; margin:0; overflow:hidden; color:var(--ink);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }}
    body {{ display:grid; grid-template-rows:auto minmax(0,1fr); background:#eef1f5; }}
    header {{ min-height:64px; padding:11px 18px; display:flex; align-items:center; justify-content:space-between;
      gap:18px; background:var(--panel); border-bottom:1px solid var(--line); z-index:1000; }}
    h1 {{ margin:0; font-size:17px; line-height:1.35; letter-spacing:0; }}
    .meta {{ margin-top:4px; color:var(--muted); font-size:12px; }}
    .opacity {{ display:flex; align-items:center; gap:9px; flex:0 0 auto; color:#344054; font-size:13px; }}
    .opacity input {{ width:128px; accent-color:var(--accent); }}
    #map {{ min-height:0; width:100%; background:#dfe5ec; }}
    #error {{ display:none; position:absolute; inset:64px 0 0; z-index:1200; place-items:center;
      padding:24px; background:#f7f8fa; color:#475467; text-align:center; }}
    .leaflet-control-layers {{ border-radius:6px; box-shadow:0 2px 10px rgba(16,24,40,.18); }}
    @media (max-width:640px) {{
      header {{ min-height:78px; padding:9px 12px; align-items:flex-start; }}
      h1 {{ font-size:15px; }} .meta {{ font-size:11px; }}
      .opacity {{ flex-direction:column; align-items:flex-end; gap:2px; font-size:11px; }}
      .opacity input {{ width:104px; }} #error {{ inset:78px 0 0; }}
    }}
  </style>
</head>
<body>
  <header>
    <div><h1>{html.escape(title)}</h1><div class="meta">{html.escape(meta)}</div></div>
    <label class="opacity"><span>结果透明度 <b id="opacityValue">70%</b></span>
      <input id="opacity" type="range" min="0" max="100" value="70" aria-label="结果透明度">
    </label>
  </header>
  <div id="map" aria-label="遥感分析结果地图"></div>
  <div id="error">地图组件加载失败，请检查网络后刷新页面。</div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const layers = {payload};
    const GCJ_A = 6378245.0;
    const GCJ_EE = 0.00669342162296594323;

    function transformLat(x, y) {{
      let r = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*Math.sqrt(Math.abs(x));
      r += (20*Math.sin(6*x*Math.PI) + 20*Math.sin(2*x*Math.PI))*2/3;
      r += (20*Math.sin(y*Math.PI) + 40*Math.sin(y/3*Math.PI))*2/3;
      r += (160*Math.sin(y/12*Math.PI) + 320*Math.sin(y*Math.PI/30))*2/3;
      return r;
    }}
    function transformLng(x, y) {{
      let r = 300 + x + 2*y + .1*x*x + .1*x*y + .1*Math.sqrt(Math.abs(x));
      r += (20*Math.sin(6*x*Math.PI) + 20*Math.sin(2*x*Math.PI))*2/3;
      r += (20*Math.sin(x*Math.PI) + 40*Math.sin(x/3*Math.PI))*2/3;
      r += (150*Math.sin(x/12*Math.PI) + 300*Math.sin(x/30*Math.PI))*2/3;
      return r;
    }}
    function wgs84ToGcj02(lng, lat) {{
      let dLat = transformLat(lng - 105, lat - 35);
      let dLng = transformLng(lng - 105, lat - 35);
      const rad = lat / 180 * Math.PI;
      const sin = Math.sin(rad);
      const magic = 1 - GCJ_EE * sin * sin;
      const sqrtMagic = Math.sqrt(magic);
      dLat = dLat * 180 / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtMagic) * Math.PI);
      dLng = dLng * 180 / (GCJ_A / sqrtMagic * Math.cos(rad) * Math.PI);
      return [lng + dLng, lat + dLat];
    }}
    function mapBounds(bounds) {{
      const sw = wgs84ToGcj02(bounds[0], bounds[1]);
      const ne = wgs84ToGcj02(bounds[2], bounds[3]);
      return [[sw[1], sw[0]], [ne[1], ne[0]]];
    }}

    if (!window.L) {{
      document.getElementById("error").style.display = "grid";
    }} else {{
      const map = L.map("map", {{zoomControl:true, attributionControl:false}});
      const satellite = L.tileLayer(
        "https://webst0{{s}}.is.autonavi.com/appmaptile?style=6&x={{x}}&y={{y}}&z={{z}}",
        {{subdomains:["1","2","3","4"], maxZoom:19}}
      ).addTo(map);
      const labels = L.tileLayer(
        "https://webst0{{s}}.is.autonavi.com/appmaptile?style=8&x={{x}}&y={{y}}&z={{z}}",
        {{subdomains:["1","2","3","4"], maxZoom:19}}
      ).addTo(map);
      const overlayControl = {{"道路与地名": labels}};
      const resultLayers = [];
      const fit = [];
      layers.forEach((item, index) => {{
        const bounds = mapBounds(item.bounds_wgs84);
        const layer = L.imageOverlay(item.url, bounds, {{opacity:.7, interactive:false}}).addTo(map);
        const name = overlayControl[item.title] ? `${{item.title}} (${{index + 1}})` : item.title;
        overlayControl[name] = layer;
        resultLayers.push(layer);
        fit.push(bounds[0], bounds[1]);
      }});
      L.control.layers({{"卫星影像": satellite}}, overlayControl, {{collapsed:false}}).addTo(map);
      map.fitBounds(fit, {{padding:[18,18], maxZoom:18}});
      const opacity = document.getElementById("opacity");
      opacity.addEventListener("input", () => {{
        const value = Number(opacity.value);
        document.getElementById("opacityValue").textContent = `${{value}}%`;
        resultLayers.forEach((layer) => layer.setOpacity(value / 100));
      }});
    }}
  </script>
</body>
</html>
"""

    def _slug(self, text: str) -> str:
        digest = re.sub(r"[^0-9a-zA-Z_-]+", "-", text).strip("-")
        suffix = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        return f"{digest}-{suffix}" if digest else f"report-{suffix}"

    def _report_identity(self, request: ReportRequest, analysis: AnalysisResult) -> str:
        fingerprint = ""
        if isinstance(analysis.aef_payload, dict):
            fingerprint = str(analysis.aef_payload.get("fingerprint") or "")
        if not fingerprint:
            fingerprint = hashlib.sha1(
                json.dumps(analysis.aef_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:12]
        return f"{request.region}-{analysis.task}-{request.time_range}-{analysis.data_source}-{fingerprint}"

    def _read_existing_abstract(self, md_path: Path) -> str:
        try:
            lines = md_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        for line in lines[1:]:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and not stripped.startswith("**"):
                return stripped
        return ""

    def _can_reuse(self, html_path: Path, md_path: Path) -> bool:
        if not html_path.exists() or not md_path.exists():
            return False
        try:
            return REPORT_TEMPLATE_VERSION in html_path.read_text(encoding="utf-8", errors="ignore")[:200]
        except OSError:
            return False

    def _prune_reports(self) -> None:
        max_reports = self.config.max_reports
        if max_reports <= 0:
            return
        html_files = sorted(
            (path for path in self.report_dir.glob("*.html") if not path.name.endswith(".map.html")),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for old_html in html_files[max_reports:]:
            stem = old_html.stem
            old_md = self.report_dir / f"{stem}.md"
            old_map = self.report_dir / f"{stem}.map.html"
            for path in (old_html, old_md, old_map):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
