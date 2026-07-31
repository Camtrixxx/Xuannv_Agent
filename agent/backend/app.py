from __future__ import annotations

import argparse
import html
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from agent.config import load_config
from agent.graph.report_agent import ReportAgent
from agent.schemas.report import ReportRequest, to_dict
from agent.services.patch_selection_service import PatchSelectionService


PROJECT_ROOT = Path(__file__).resolve().parents[2]
UI_PATH = PROJECT_ROOT / "agent" / "ui" / "agent_dashboard_mock.html"
API_DOC_PATH = PROJECT_ROOT / "agent" / "API.md"
FRONTEND_GUIDE_PATH = PROJECT_ROOT / "agent" / "FRONTEND_GUIDE.md"
REPORT_DIR = PROJECT_ROOT / "agent" / "reports"

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:
    FastAPI = None
    HTTPException = None
    CORSMiddleware = None
    FileResponse = None
    HTMLResponse = None
    JSONResponse = None
    StaticFiles = None


# --- OpenAPI request/response schemas ------------------------------------
# Purpose is Swagger (/docs) documentation only: the route handlers still take
# the raw dict and go through ReportRequest.from_dict / PatchSelectionService,
# so runtime behaviour is unchanged. Requests allow extra fields so the schema
# can never reject a payload the dict path would have accepted.
try:
    from pydantic import BaseModel, ConfigDict, Field

    class ReportRequestModel(BaseModel):
        """Body of POST /api/report."""
        model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [{
            "session_id": "frontend-session-001",
            "region": "北京市海淀区",
            "task": "建筑物提取",
            "prompt": "给我一份2026年3月建筑物提取报告",
            "selected_patch_ids": ["patch_000000", "patch_000001"],
            "aoi": {"type": "bbox", "coordinates": [116.19, 39.88, 116.24, 39.91]},
        }]})

        prompt: str = Field(..., description="用户自然语言输入（必填）")
        session_id: str = Field("default", description="会话 ID，前端应为每个聊天窗口生成稳定 ID")
        task: str = Field("", description="前端选择的任务，可为空；空时由 Agent 从自然语言提取或追问")
        region: str = Field("", description="前端选择的地区，可为空；空时按文本/框选 AOI 推断，否则默认雅江区域")
        time_range: str = Field("", description="YYYY-MM，前端已知月份时可直接传")
        selected_patch_ids: list[str] = Field(default_factory=list, description="地图选中的 patch ID 列表")
        aoi: dict = Field(default_factory=dict, description="地图框选范围 {type:'bbox', coordinates:[minLng,minLat,maxLng,maxLat]}")
        before_time_range: str = Field("", description="场景 B 两期对比的前期月份 YYYY-MM")
        after_time_range: str = Field("", description="场景 B 两期对比的后期月份 YYYY-MM")
        custom_model_id: str = Field("", description="自定义模型 ID（非原生地物分析时由 Agent 内部解析，前端一般不传）")
        target_object: str = Field("", description="非原生分析对象（如湿地），前端一般不传，由 Agent 解析")

    class PatchSearchModel(BaseModel):
        """Body of POST /api/patches/search."""
        model_config = ConfigDict(extra="allow", json_schema_extra={"examples": [{
            "region": "北京市海淀区",
            "task": "",
            "time_range": "",
            "bbox": [116.24, 39.88, 116.30, 39.93],
            "limit": 12,
        }]})

        region: str = Field(..., description="地区名，如 北京市海淀区 / 哈尔滨新区 / 雅江区域")
        bbox: list[float] = Field(..., description="[minLng, minLat, maxLng, maxLat]，四个有限数字")
        task: str = Field("", description="任务名，可为空（先框选后补任务）")
        time_range: str = Field("", description="YYYY-MM，可为空")
        limit: int = Field(12, description="返回候选 patch 上限")

    class SessionResetModel(BaseModel):
        """Body of POST /api/session/reset."""
        model_config = ConfigDict(json_schema_extra={"examples": [{"session_id": "frontend-session-001"}]})
        session_id: str = Field("default", description="要清空的会话 ID")

    class ReportArtifactResponseModel(BaseModel):
        """Report URLs returned when status=ok."""
        model_config = ConfigDict(extra="allow")

        title: str
        html_url: str = Field(description="完整 HTML 报告的相对地址")
        markdown_url: str = Field(description="Markdown 报告的相对地址")
        map_html_url: str = Field(
            "",
            description="海淀交互式结果地图的相对地址；没有可上图结果时为空字符串",
        )

    class ReportResponseModel(BaseModel):
        """Response of POST /api/report; additional analysis fields remain open."""
        model_config = ConfigDict(extra="allow")

        status: str
        message: str = ""
        session_id: str = "default"
        report: ReportArtifactResponseModel | None = None
        analysis: dict | None = None
        request: dict = Field(default_factory=dict)
        intent: dict | None = None
        memory: dict = Field(default_factory=dict)
        action: dict = Field(default_factory=dict)
        debug: dict = Field(default_factory=dict)

except ImportError:
    BaseModel = None
    ReportRequestModel = None
    PatchSearchModel = None
    SessionResetModel = None
    ReportResponseModel = None


def parse_args() -> argparse.Namespace:
    config = load_config()
    parser = argparse.ArgumentParser(description="Serve the Xuannv Agent backend.")
    parser.add_argument("--host", default=config.server.host)
    parser.add_argument("--port", type=int, default=config.server.port)
    parser.add_argument("--legacy-http", action="store_true", help="Use the built-in http.server fallback.")
    return parser.parse_args()


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def file_response(handler: BaseHTTPRequestHandler, path: Path, content_type: str) -> None:
    if not path.exists() or not path.is_file():
        handler.send_error(404)
        return
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _safe_report_path(path: str) -> Path:
    rel = unquote(path.removeprefix("/reports/"))
    target = (REPORT_DIR / rel).resolve()
    root = REPORT_DIR.resolve()
    try:
        if not target.is_relative_to(root):
            raise ValueError("report path is outside report directory")
    except AttributeError:
        if not str(target).startswith(str(root)):
            raise ValueError("report path is outside report directory")
    return target


def _report_content_type(path: Path) -> str:
    if path.suffix == ".png":
        return "image/png"
    if path.suffix == ".md":
        return "text/markdown; charset=utf-8"
    return "text/html; charset=utf-8"


def _inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def _render_markdown_html(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    chunks: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []
    in_code = False
    code_lines: list[str] = []
    table_lines: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            chunks.append(f"<p>{_inline_markdown(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_bullets() -> None:
        nonlocal bullets
        if bullets:
            items = "".join(f"<li>{_inline_markdown(item)}</li>" for item in bullets)
            chunks.append(f"<ul>{items}</ul>")
            bullets = []

    def flush_table() -> None:
        nonlocal table_lines
        if not table_lines:
            return
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in table_lines
            if line.strip().startswith("|")
        ]
        table_lines = []
        if len(rows) < 2:
            return
        header = rows[0]
        body = rows[2:] if all(set(cell) <= {"-", ":", " "} for cell in rows[1]) else rows[1:]
        head_html = "".join(f"<th>{_inline_markdown(cell)}</th>" for cell in header)
        body_html = "".join(
            "<tr>" + "".join(f"<td>{_inline_markdown(cell)}</td>" for cell in row) + "</tr>"
            for row in body
        )
        chunks.append(f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>")

    for raw_line in lines:
        line = raw_line.rstrip()
        if line.startswith("```"):
            flush_paragraph()
            flush_bullets()
            flush_table()
            if in_code:
                chunks.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if line.strip().startswith("|"):
            flush_paragraph()
            flush_bullets()
            table_lines.append(line)
            continue
        flush_table()
        if not line.strip():
            flush_paragraph()
            flush_bullets()
            continue
        if line.startswith("#"):
            flush_paragraph()
            flush_bullets()
            level = min(len(line) - len(line.lstrip("#")), 3)
            title = line[level:].strip()
            anchor = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "-", title).strip("-").lower()
            chunks.append(f'<h{level} id="{html.escape(anchor)}">{_inline_markdown(title)}</h{level}>')
            continue
        if line.startswith("- "):
            flush_paragraph()
            bullets.append(line[2:].strip())
            continue
        paragraph.append(line.strip())

    flush_paragraph()
    flush_bullets()
    flush_table()
    if in_code:
        chunks.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")

    return "\n".join(chunks)


def _docs_page(markdown_path: Path, title: str) -> str:
    markdown_text = markdown_path.read_text(encoding="utf-8")
    body = _render_markdown_html(markdown_text)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin: 0; background: #f6f7f9; color: #1f2937; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; }}
    main {{ max-width: 1040px; margin: 0 auto; padding: 34px 20px 64px; }}
    .top {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 18px; }}
    .top a {{ color: #2563eb; text-decoration: none; font-weight: 700; }}
    article {{ background: #fff; border: 1px solid #dbe3ea; border-radius: 10px; padding: 28px; box-shadow: 0 16px 42px rgba(15, 23, 42, 0.07); }}
    h1 {{ margin-top: 0; font-size: 34px; }}
    h2 {{ margin-top: 34px; padding-top: 10px; border-top: 1px solid #e5e7eb; }}
    h3 {{ margin-top: 28px; }}
    p, li {{ line-height: 1.78; }}
    code {{ background: #eef2ff; color: #1d4ed8; border-radius: 5px; padding: 2px 5px; font-size: 0.92em; }}
    pre {{ background: #0f172a; color: #e5e7eb; border-radius: 8px; padding: 16px; overflow: auto; line-height: 1.55; }}
    pre code {{ background: transparent; color: inherit; padding: 0; }}
    table {{ width: 100%; border-collapse: collapse; margin: 14px 0 20px; font-size: 14px; }}
    th, td {{ border: 1px solid #e5e7eb; padding: 10px; text-align: left; vertical-align: top; }}
    th {{ background: #f8fafc; }}
    ul {{ padding-left: 22px; }}
    @media (max-width: 760px) {{ article {{ padding: 18px; }} h1 {{ font-size: 28px; }} }}
  </style>
</head>
<body>
  <main>
    <div class="top">
      <strong>Xuannv Agent</strong>
      <nav><a href="/frontend-guide">前端指南</a> · <a href="/api-docs">接口文档</a> · <a href="/docs">Swagger</a> · <a href="/api/health">Health</a></nav>
    </div>
    <article>{body}</article>
  </main>
</body>
</html>
"""


def _api_docs_page() -> str:
    return _docs_page(API_DOC_PATH, "Xuannv Agent API")


def _frontend_guide_page() -> str:
    return _docs_page(FRONTEND_GUIDE_PATH, "Xuannv Agent 前端接入指南")


def _no_store_html(content: str) -> HTMLResponse:
    return HTMLResponse(content, headers={"Cache-Control": "no-store, max-age=0"})


def create_app(agent: ReportAgent | None = None):
    if FastAPI is None:
        raise RuntimeError("FastAPI is not installed. Use --legacy-http or install fastapi uvicorn.")

    report_agent = agent or ReportAgent()
    patch_selector = PatchSelectionService()
    app = FastAPI(
        title="Xuannv Agent",
        version="0.2.0",
        description=(
            "玄女遥感专题报告 Agent 的统一入口。前端只调用本服务。\n\n"
            "- 完整接口文档：[/api-docs](/api-docs)\n"
            "- 前端接入指南：[/frontend-guide](/frontend-guide)\n"
            "- 健康检查：[/api/health](/api/health)\n\n"
            "核心接口是 `POST /api/report`，按返回 `status` 分流（ok / needs_input / "
            "needs_annotation / chat）。非内置地物走标注训练交接（`needs_annotation` + `action`）；"
            "报告图层通过 `analysis.charts[].overlay` + `bounds_wgs84` 叠加到地图。"
        ),
    )
    config = load_config()
    if CORSMiddleware is not None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    if StaticFiles is not None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        app.mount("/reports", StaticFiles(directory=str(REPORT_DIR)), name="reports")

    @app.get("/", response_class=HTMLResponse)
    def ui() -> HTMLResponse:
        return _no_store_html(UI_PATH.read_text(encoding="utf-8"))

    @app.get("/ui", response_class=HTMLResponse)
    def ui_alias() -> HTMLResponse:
        return _no_store_html(UI_PATH.read_text(encoding="utf-8"))

    @app.get("/workflow", response_class=HTMLResponse)
    def workflow() -> HTMLResponse:
        return HTMLResponse(WORKFLOW_HTML)

    @app.get("/api-docs", response_class=HTMLResponse)
    def api_docs() -> HTMLResponse:
        return HTMLResponse(_api_docs_page())

    @app.get("/api-docs.md")
    def api_docs_markdown() -> FileResponse:
        return FileResponse(API_DOC_PATH, media_type="text/markdown; charset=utf-8")

    @app.get("/frontend-guide", response_class=HTMLResponse)
    def frontend_guide() -> HTMLResponse:
        return HTMLResponse(_frontend_guide_page())

    @app.get("/frontend-guide.md")
    def frontend_guide_markdown() -> FileResponse:
        return FileResponse(FRONTEND_GUIDE_PATH, media_type="text/markdown; charset=utf-8")

    @app.get("/api/health")
    def health() -> dict:
        return {
            "status": "ok",
            "service": "xuannv-agent",
            "backend": "fastapi",
        }

    @app.get("/api/sessions")
    def sessions(limit: int = 30) -> dict:
        return {"status": "ok", "sessions": report_agent.list_sessions(limit=limit)}

    @app.get("/api/session/{session_id}")
    def session_detail(session_id: str) -> dict:
        session = report_agent.load_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="session not found")
        return {
            "status": "ok",
            "session": session,
            # Full history: the frontend rebuilds the whole conversation from
            # this, not just the agent's short context window.
            "memory": report_agent.memory_service.snapshot(session_id, full_history=True),
        }

    @app.post(
        "/api/report",
        response_model=ReportResponseModel,
        summary="主接口：对话 / 补槽 / 生成报告",
        description=(
            "自然语言驱动的统一入口。返回体的 `status` 决定前端行为：`ok`(报告已生成)、"
            "`needs_input`(缺任务/月份或月份不可用)、`needs_annotation`(非内置地物需先标注训练，"
            "见 `action`)、`chat`(闲聊/追问)。详见 /api-docs 与 /frontend-guide。"
        ),
    )
    def report(payload: ReportRequestModel) -> JSONResponse:
        try:
            request = ReportRequest.from_dict(payload.model_dump())
            response = report_agent.run(request)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(to_dict(response))

    @app.post(
        "/api/patches/search",
        summary="地图选区检索候选 patch",
        description=(
            "把地图框选的 bbox 交给 Agent，返回按 bbox 相交度排序的候选 patch（含 `bounds_wgs84`）。"
            "`task`/`time_range` 可留空以支持先框选后补任务。检索问题以 `status=invalid`(重新框选) 或 "
            "`retryable_error`(稍后重试) 返回，不伪装成 HTTP 500。"
        ),
    )
    def search_patches(payload: PatchSearchModel) -> JSONResponse:
        try:
            result = patch_selector.search(payload.model_dump())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(to_dict(result))

    @app.post("/api/session/reset", summary="清空会话记忆")
    def reset_session(payload: SessionResetModel) -> dict:
        session_id = str(payload.session_id or "default")
        report_agent.memory_service.reset(session_id)
        return {"status": "ok", "session_id": session_id}

    return app


def make_handler(agent: ReportAgent):
    patch_selector = PatchSelectionService()

    class AgentHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            print(f"{self.address_string()} - {fmt % args}")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/ui"}:
                file_response(self, UI_PATH, "text/html; charset=utf-8")
                return
            if parsed.path == "/workflow":
                self._workflow_page()
                return
            if parsed.path == "/api/health":
                json_response(self, {"status": "ok", "service": "xuannv-agent", "backend": "http.server"})
                return
            if parsed.path == "/api/sessions":
                params = parse_qs(parsed.query)
                raw_limit = params.get("limit", ["30"])[0]
                try:
                    limit = int(raw_limit)
                except ValueError:
                    limit = 30
                json_response(self, {"status": "ok", "sessions": agent.list_sessions(limit=limit)})
                return
            if parsed.path.startswith("/api/session/"):
                session_id = unquote(parsed.path.removeprefix("/api/session/"))
                session = agent.load_session(session_id)
                if not session:
                    self.send_error(404)
                    return
                json_response(self, {
                    "status": "ok",
                    "session": session,
                    "memory": agent.memory_service.snapshot(session_id, full_history=True),
                })
                return
            if parsed.path.startswith("/reports/"):
                self._serve_report(parsed.path)
                return
            self.send_error(404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/session/reset":
                self._reset_session()
                return
            if parsed.path == "/api/patches/search":
                self._search_patches()
                return
            if parsed.path != "/api/report":
                self.send_error(404)
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                request = ReportRequest.from_dict(payload)
                response = agent.run(request)
            except Exception as exc:
                json_response(self, {"status": "error", "error": str(exc)}, status=400)
                return
            json_response(self, to_dict(response))

        def _search_patches(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                result = patch_selector.search(payload)
            except Exception as exc:
                json_response(self, {"status": "error", "error": str(exc)}, status=400)
                return
            json_response(self, to_dict(result))

        def _reset_session(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
                session_id = str(payload.get("session_id") or "default")
                agent.memory_service.reset(session_id)
            except Exception as exc:
                json_response(self, {"status": "error", "error": str(exc)}, status=400)
                return
            json_response(self, {"status": "ok", "session_id": session_id})

        def _serve_report(self, path: str) -> None:
            try:
                target = _safe_report_path(path)
            except ValueError:
                self.send_error(403)
                return
            file_response(self, target, _report_content_type(target))

        def _workflow_page(self) -> None:
            data = WORKFLOW_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return AgentHandler


WORKFLOW_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>遥感报告助手 · 工作原理</title>
  <style>
    :root {
      --bg: #f6f7f9; --card: #ffffff; --text: #1f2328; --muted: #6b7280;
      --line: #e5e7eb; --primary: #2563eb; --primary-soft: #eef4ff; --green: #16a34a;
      --shadow: 0 18px 45px rgba(15, 23, 42, 0.1);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; color: var(--text);
      background: radial-gradient(1200px 600px at 100% -10%, #e8eefc 0%, rgba(232,238,252,0) 55%), var(--bg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    main { max-width: 1080px; margin: 0 auto; padding: 30px 20px 64px; }
    .topbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 26px; }
    .brand { font-weight: 800; font-size: 18px; letter-spacing: .2px; }
    .brand small { color: var(--muted); font-weight: 600; margin-left: 8px; font-size: 13px; }
    .back { color: var(--primary); text-decoration: none; font-weight: 700; font-size: 14px; }
    .back:hover { text-decoration: underline; }
    .hero { text-align: center; margin: 8px 0 34px; }
    .hero .eyebrow { color: var(--primary); font-weight: 800; font-size: 13px; letter-spacing: .6px; }
    .hero h1 { margin: 12px 0 12px; font-size: 34px; line-height: 1.25; }
    .hero p { max-width: 640px; margin: 0 auto; color: var(--muted); line-height: 1.9; font-size: 16px; }
    h2.section { font-size: 20px; margin: 40px 0 16px; }
    .steps { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; }
    .step {
      background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 18px 16px;
      box-shadow: var(--shadow); position: relative; min-height: 178px;
    }
    .step .n {
      display: inline-flex; align-items: center; justify-content: center; width: 26px; height: 26px;
      border-radius: 999px; background: var(--primary-soft); color: var(--primary); font-weight: 800; font-size: 13px;
    }
    .step .ico { font-size: 26px; margin: 12px 0 8px; }
    .step h3 { margin: 0 0 6px; font-size: 15px; }
    .step p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.7; }
    .cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
    .card { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 20px; box-shadow: var(--shadow); }
    .card h3 { margin: 0 0 4px; font-size: 17px; }
    .card .region-note { color: var(--muted); font-size: 12.5px; margin: 0 0 14px; }
    .tags { display: flex; flex-wrap: wrap; gap: 8px; }
    .tag { background: var(--primary-soft); color: var(--primary); border-radius: 999px; padding: 5px 11px; font-size: 13px; font-weight: 600; }
    .examples { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
    .ex {
      background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px;
      color: var(--text); line-height: 1.6; box-shadow: var(--shadow); position: relative;
    }
    .ex::before { content: "“"; position: absolute; top: 2px; left: 10px; font-size: 34px; color: #cbd5e1; }
    .ex span { display: block; padding-left: 18px; }
    .deliver { background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 22px 24px; box-shadow: var(--shadow); }
    .deliver ul { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 12px 26px; }
    .deliver li { position: relative; padding-left: 30px; line-height: 1.7; color: var(--text); }
    .deliver li::before {
      content: "✓"; position: absolute; left: 0; top: 1px; width: 20px; height: 20px; border-radius: 999px;
      background: rgba(22,163,74,.12); color: var(--green); font-weight: 800; font-size: 12px;
      display: inline-flex; align-items: center; justify-content: center;
    }
    .footer { margin-top: 44px; text-align: center; color: var(--muted); font-size: 13px; line-height: 1.9; }
    .footer a { color: var(--muted); text-decoration: underline; }
    @media (max-width: 900px) {
      .steps { grid-template-columns: repeat(2, 1fr); }
      .cards, .examples { grid-template-columns: 1fr; }
      .deliver ul { grid-template-columns: 1fr; }
      .hero h1 { font-size: 28px; }
    }
  </style>
</head>
<body>
  <main>
    <div class="topbar">
      <div class="brand">遥感报告助手<small>工作原理</small></div>
      <a class="back" href="/">← 返回助手</a>
    </div>

    <div class="hero">
      <div class="eyebrow">它是如何工作的</div>
      <h1>用一句话，生成专业遥感分析报告</h1>
      <p>你不需要懂任何遥感专业参数。只要用日常语言说出想看的地区、时间和内容，助手会自动理解、分析并为你整理成一份图文并茂的报告。</p>
    </div>

    <h2 class="section">五步，从一句话到一份报告</h2>
    <div class="steps">
      <article class="step"><span class="n">1</span><div class="ico">📝</div><h3>描述你的需求</h3><p>用日常语言说清地区、时间和想分析的内容，例如“雅江区域去年九月的水体分布”。</p></article>
      <article class="step"><span class="n">2</span><div class="ico">🧠</div><h3>智能理解意图</h3><p>助手自动识别出地区、分析任务和时间，无需你填写任何专业参数或表单。</p></article>
      <article class="step"><span class="n">3</span><div class="ico">💬</div><h3>缺什么补什么</h3><p>如果少了关键信息（比如没说月份），助手会主动追问，你一句话补齐即可。</p></article>
      <article class="step"><span class="n">4</span><div class="ico">🛰️</div><h3>遥感模型分析</h3><p>调用对应区域的遥感模型完成识别与计算，得到关键指标和结果图。</p></article>
      <article class="step"><span class="n">5</span><div class="ico">📄</div><h3>一键生成报告</h3><p>自动整理成结构化图文报告，可在线预览，也可下载 HTML 或 Markdown。</p></article>
    </div>

    <h2 class="section">能帮你分析什么</h2>
    <div class="cards">
      <div class="card">
        <h3>雅江区域</h3>
        <p class="region-note">高原河谷地区 · 本地遥感模型</p>
        <div class="tags"><span class="tag">地物分类</span><span class="tag">水体分布</span><span class="tag">高程地形</span></div>
      </div>
      <div class="card">
        <h3>哈尔滨新区</h3>
        <p class="region-note">城市建设区 · 在线专题服务</p>
        <div class="tags"><span class="tag">建筑物提取</span><span class="tag">土地利用分类</span><span class="tag">水体提取</span></div>
      </div>
      <div class="card">
        <h3>北京市海淀区</h3>
        <p class="region-note">城市核心区 · 在线专题服务</p>
        <div class="tags"><span class="tag">建筑物提取</span><span class="tag">道路提取</span><span class="tag">施工识别</span><span class="tag">土地利用/覆盖分类</span><span class="tag">水体提取</span></div>
      </div>
    </div>

    <h2 class="section">试着这样问</h2>
    <div class="examples">
      <div class="ex"><span>帮我生成雅江区域去年九月的水体分布报告</span></div>
      <div class="ex"><span>哈尔滨新区 2025 年 10 月的建筑物提取</span></div>
      <div class="ex"><span>看看海淀区上个月的道路提取情况</span></div>
    </div>

    <h2 class="section">你会得到一份怎样的报告</h2>
    <div class="deliver">
      <ul>
        <li>一段专业的分析摘要，读一眼就懂结论</li>
        <li>关键指标卡片，量化呈现分析结果</li>
        <li>遥感结果图与叠加图，直观可视</li>
        <li>主要发现与风险提示，辅助判断</li>
        <li>可落地的后续建议</li>
        <li>可在线预览或下载的完整报告</li>
      </ul>
    </div>

    <p class="footer">
      助手支持多轮对话，会记住你上一次的地区和时间，让追问和调整更自然。<br>
      面向开发者？查看 <a href="/api-docs">接口文档</a>。
    </p>
  </main>
</body>
</html>
"""


def run_legacy_server(host: str, port: int) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), make_handler(ReportAgent()))
    print(f"Xuannv Agent listening on http://{host}:{port}")
    print(f"Open the UI at http://{host}:{port}/")
    print("Health check: /api/health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    args = parse_args()
    if args.legacy_http or FastAPI is None:
        run_legacy_server(args.host, args.port)
        return
    import uvicorn

    uvicorn.run(create_app(), host=args.host, port=args.port)


app = create_app() if FastAPI is not None else None


if __name__ == "__main__":
    main()
