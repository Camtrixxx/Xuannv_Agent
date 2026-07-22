from __future__ import annotations

import html
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from agent.config import ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportArtifact, ReportRequest
from agent.services.common import extract_json_object
from agent.services.llm_provider import DeepSeekProvider, LLMProvider


REPORT_TEMPLATE_VERSION = "agent-report-v7"

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

# Injected into report HTML when a result chart carries WGS84 bounds. Runs after
# the report page loads inside the preview iframe, so the map container already
# has a real size (avoids the blank-tile issue of initialising while hidden).
# High德 satellite + roads tiles; result PNG overlaid via L.imageOverlay.
_RESULT_MAP_JS = """
(function () {
  var cfg = __CONFIG__;
  // 高德瓦片是 GCJ-02（火星坐标系），结果图边界是 WGS84。二者直接叠加会有
  // ~500m 偏移，需把 WGS84 边界转成 GCJ-02 再叠加，与底图对齐。
  var A = 6378245.0, EE = 0.00669342162296594323;
  function tLat(x, y) {
    var r = -100 + 2*x + 3*y + 0.2*y*y + 0.1*x*y + 0.2*Math.sqrt(Math.abs(x));
    r += (20*Math.sin(6*x*Math.PI) + 20*Math.sin(2*x*Math.PI)) * 2/3;
    r += (20*Math.sin(y*Math.PI) + 40*Math.sin(y/3*Math.PI)) * 2/3;
    r += (160*Math.sin(y/12*Math.PI) + 320*Math.sin(y*Math.PI/30)) * 2/3;
    return r;
  }
  function tLng(x, y) {
    var r = 300 + x + 2*y + 0.1*x*x + 0.1*x*y + 0.1*Math.sqrt(Math.abs(x));
    r += (20*Math.sin(6*x*Math.PI) + 20*Math.sin(2*x*Math.PI)) * 2/3;
    r += (20*Math.sin(x*Math.PI) + 40*Math.sin(x/3*Math.PI)) * 2/3;
    r += (150*Math.sin(x/12*Math.PI) + 300*Math.sin(x/30*Math.PI)) * 2/3;
    return r;
  }
  function wgs2gcj(lng, lat) {
    var dLat = tLat(lng - 105, lat - 35), dLng = tLng(lng - 105, lat - 35);
    var rad = lat / 180 * Math.PI, m = Math.sin(rad); m = 1 - EE*m*m;
    var sm = Math.sqrt(m);
    dLat = (dLat * 180) / ((A * (1 - EE)) / (m * sm) * Math.PI);
    dLng = (dLng * 180) / (A / sm * Math.cos(rad) * Math.PI);
    return [lng + dLng, lat + dLat];
  }
  function init() {
    var el = document.getElementById('resultMap');
    if (!el || !window.L) { return; }
    var overlays = cfg.overlays || [];
    if (!overlays.length) { return; }
    var allBounds = [];
    var layerMap = {};
    overlays.forEach(function (item, index) {
      var b = item.bounds;
      var sw = wgs2gcj(b[0], b[1]), ne = wgs2gcj(b[2], b[3]);
      var latLng = [[sw[1], sw[0]], [ne[1], ne[0]]];
      allBounds.push(latLng[0], latLng[1]);
      var name = item.title || ('专题结果 ' + (index + 1));
      layerMap[name] = L.imageOverlay(item.url, latLng, {opacity: 0.7, interactive: false});
    });
    var map = L.map(el, {zoomControl: true, attributionControl: false});
    var sat = L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',
      {subdomains: ['1','2','3','4'], maxZoom: 19}).addTo(map);
    var labels = L.tileLayer('https://webst0{s}.is.autonavi.com/appmaptile?style=8&x={x}&y={y}&z={z}',
      {subdomains: ['1','2','3','4'], maxZoom: 19});
    Object.keys(layerMap).forEach(function (name) { layerMap[name].addTo(map); });
    map.fitBounds(allBounds);
    L.control.layers({'卫星影像': sat}, Object.assign(layerMap, {'道路注记': labels}),
      {position: 'topright', collapsed: false}).addTo(map);
    var slider = document.getElementById('mapOpacity');
    var val = document.getElementById('mapOpacityVal');
    if (slider) {
      slider.addEventListener('input', function () {
        Object.keys(layerMap).forEach(function (name) {
          if (name !== '道路注记') { layerMap[name].setOpacity(slider.value / 100); }
        });
        if (val) { val.textContent = slider.value + '%'; }
      });
    }
    setTimeout(function () { map.invalidateSize(); }, 120);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
"""


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

    def build(self, request: ReportRequest, analysis: AnalysisResult) -> ReportArtifact:
        title = f"{analysis.headline}报告"
        slug = self._slug(self._report_identity(request, analysis))
        html_path = self.report_dir / f"{slug}.html"
        md_path = self.report_dir / f"{slug}.md"
        metrics = self._business_metrics(analysis)

        if self.config.reuse_existing and self._can_reuse(html_path, md_path):
            abstract = self._read_existing_abstract(md_path) or self._fallback_summary(request, analysis)
            return ReportArtifact(
                title=title,
                abstract=abstract,
                sections=[],
                metrics=metrics,
                charts=analysis.charts,
                html_url=f"/reports/{html_path.name}",
                markdown_url=f"/reports/{md_path.name}",
                llm_provider="reused",
                reused=True,
                generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

        content = self._generate_content(request, analysis, metrics)
        llm_status = getattr(self.llm, "last_status", "template")
        llm_provider = "deepseek" if llm_status == "ok" else f"template:{llm_status}"

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
            llm_provider=llm_provider,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            debug={"llm_status": llm_status, "slug": slug},
        )

    # ------------------------------------------------------------------ content

    def _generate_content(self, request: ReportRequest, analysis: AnalysisResult, metrics: list[MetricCard]) -> dict:
        system_prompt = (
            "你是一位资深遥感分析师，正在为业务和管理读者撰写遥感专题分析报告。"
            "写作要求：结论先行，层次分明，语言专业但通俗易懂；聚焦“数据说明了什么、"
            "对业务意味着什么、下一步该怎么做”。不要罗列系统参数、接口字段或免责声明，"
            "不要出现模型文件、服务地址、patch 编号等技术细节。必须忠于给定数据，"
            "严禁编造未提供的数字、坐标或事件；不得引入输入中没有的行业阈值、典型范围、"
            "比较基准、因果归因或可靠性评级。没有明确精度证据时，不得声称结果可靠、准确或"
            "可直接替代现场核查。只输出 JSON，不要输出多余文字。"
        )
        payload = json.dumps(
            {
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
                "输出格式": {
                    "summary": "结论先行的执行摘要，160-240字，讲清区域、时间、任务的核心结论与业务价值",
                    "highlights": "3-5 条核心要点，每条一句话、可独立成立，最重要的结论排在最前",
                    "analysis": "2-4 个深度解读小节；每节为 {title: 小标题, text: 180-280字的详实分析}，"
                    "覆盖空间格局、主导特征、值得关注的信号和数据边界；没有空间证据时不要推断聚集性",
                    "recommendations": "3-5 条可执行的建议或风险提醒，务实、面向行动",
                },
                "禁止": [
                    "不要出现模型文件路径、服务地址、patch 编号、接口字段、坐标系等系统内部信息",
                    "不要出现 mock、占位、模拟、原型等字样",
                    "不要编造输入中没有的具体数字、坐标或真实事件",
                    "不要用外部常识补充典型阈值、行业平均值或对比基准",
                    "不要把全区域任务可用性摘要解释为本次选区的空间结论",
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        text = self.llm.complete(system_prompt, payload)
        if text:
            parsed = extract_json_object(text)
            if parsed:
                analysis_blocks = self._clean_blocks(parsed.get("analysis"))
                if analysis_blocks:
                    return {
                        "summary": str(parsed.get("summary") or self._fallback_summary(request, analysis)),
                        "highlights": self._list_or_default(parsed.get("highlights"), analysis.findings)[:5],
                        "analysis": analysis_blocks,
                        "recommendations": self._list_or_default(
                            parsed.get("recommendations"), self._merged_actions(analysis)
                        )[:6],
                    }
        return self._fallback_content(request, analysis)

    def _clean_blocks(self, value) -> list[dict[str, str]]:
        blocks: list[dict[str, str]] = []
        if not isinstance(value, list):
            return blocks
        for item in value:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            body = str(item.get("text") or "").strip()
            if title and body:
                blocks.append({"title": title, "text": body})
        return blocks[:4]

    def _fallback_content(self, request: ReportRequest, analysis: AnalysisResult) -> dict:
        blocks = [{"title": "分析解读", "text": analysis.summary or self._fallback_summary(request, analysis)}]
        findings = [f for f in analysis.findings if "Agent" not in f and "标准化" not in f]
        if findings:
            blocks.append({"title": "主要发现", "text": " ".join(findings[:4])})
        return {
            "summary": self._fallback_summary(request, analysis),
            "highlights": (findings or analysis.findings)[:5],
            "analysis": blocks,
            "recommendations": self._merged_actions(analysis)[:6],
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

    def _list_or_default(self, value, default: list[str]) -> list[str]:
        if not isinstance(value, list):
            return default
        items = [str(item).strip() for item in value if str(item).strip()]
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

        map_section, map_head, map_script = self._result_map_html(analysis)

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
    .result-map {{ width:100%; height:420px; border-radius:10px; overflow:hidden; border:1px solid var(--line); background:#dbe6de; }}
    .map-controls {{ display:flex; align-items:center; gap:10px; margin-top:12px; font-size:13px; color:var(--muted); }}
    .map-controls input[type="range"] {{ flex:1; }}
  </style>{map_head}
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
{map_section}
    <section id="analysis">
      <h2 class="section-title">深度解读</h2>
      <div class="card">{analysis_html}</div>
    </section>
    <section class="card rec" id="rec">
      <h2>建议与提醒</h2>
      <ol>{rec_html}</ol>
    </section>
    <p class="footer">本报告由遥感报告助手自动生成 · 数据来源：{html.escape(source)} · 生成日期 {html.escape(generated_at)}</p>
  </main>{map_script}
</body>
</html>
"""

    def _result_map_html(self, analysis: AnalysisResult) -> tuple[str, str, str]:
        """Build an interactive Leaflet map that georeferences the result PNG.

        Returns ``(section_html, head_html, script_html)``. All three are empty
        when no chart is flagged ``overlay`` with valid WGS84 bounds. The map is
        embedded in the report HTML itself so it renders inside the right-side
        preview iframe (and in the standalone/downloaded report).
        """
        overlays = [
            c
            for c in analysis.charts
            if getattr(c, "overlay", False)
            and isinstance(getattr(c, "bounds_wgs84", None), list)
            and len(c.bounds_wgs84) == 4
            and getattr(c, "url", "")
        ]
        if not overlays:
            return "", "", ""

        head = (
            '\n  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">'
            '\n  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>'
        )
        section = f"""
    <section class="card" id="result-map">
      <h2>在地图上查看结果</h2>
      <div class="result-map" id="resultMap"></div>
      <div class="map-controls">
        <span>结果透明度</span>
        <input type="range" min="0" max="100" value="70" id="mapOpacity">
        <span id="mapOpacityVal">70%</span>
      </div>
      <p class="footer" style="text-align:left;margin-top:10px">地图包含 {len(overlays)} 个 patch 结果图层，可在右上角逐层开关。</p>
    </section>"""
        cfg = json.dumps(
            {
                "overlays": [
                    {
                        "url": c.url,
                        "bounds": [float(v) for v in c.bounds_wgs84],
                        "title": c.title or getattr(c, "patch_id", "") or f"专题结果 {index + 1}",
                    }
                    for index, c in enumerate(overlays)
                ]
            },
            ensure_ascii=False,
        )
        script = "\n  <script>\n" + _RESULT_MAP_JS.replace("__CONFIG__", cfg) + "\n  </script>"
        return section, head, script

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
        html_files = sorted(self.report_dir.glob("*.html"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old_html in html_files[max_reports:]:
            stem = old_html.stem
            old_md = self.report_dir / f"{stem}.md"
            for path in (old_html, old_md):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
