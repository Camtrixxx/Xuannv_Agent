# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Xuannv Agent is a remote sensing thematic report agent service. It accepts natural language requests for landcover classification, water extraction, building/road extraction, construction detection, land use / land cover classification, and DEM analysis across three regions (Yajiang, Harbin New Area, and Beijing Haidian District), then generates structured HTML/Markdown reports with charts and metrics.

The repository now also includes the AEF inference service runtime (`aef_inference/`), which provides the model-side REST API that the Agent calls for Yajiang region tasks. The inference service reuses training code, model configs, and weights from an external `AEF_CODE_ROOT` path — the weights and training code themselves are not in this repo.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# --- Agent service ---
# Start the server
python -m agent.backend.app --host 0.0.0.0 --port 7870
# or via uvicorn directly
uvicorn agent.backend.app:app --host 0.0.0.0 --port 7870
# Legacy http.server fallback (no FastAPI)
python -m agent.backend.app --legacy-http

# --- AEF inference service ---
# Start AEF inference (defaults to AEF_CODE_ROOT=/data/heyuhang/yajiang-aef)
python -m aef_inference.server --host 127.0.0.1 --port 7862
# With custom model paths:
python -m aef_inference.server \
  --config /data/heyuhang/yajiang-aef/configs/yajiang_v1_2_continue_200.yaml \
  --manifest /data/heyuhang/yajiang-aef/data/full_npy/train.jsonl \
  --deploy-model /data/heyuhang/yajiang-aef/outputs/.../exports/..._deploy.pt \
  --cache-dir /data/heyuhang/yajiang-aef/outputs/aef_inference_service_v1_2_continue_200

# --- Script management ---
scripts/start_services.sh              # start AEF first, wait for healthy, then start Agent
scripts/status_services.sh             # check both services
scripts/stop_services.sh               # stop both services
scripts/start_agent_backend.sh         # Agent only (daemonize via nohup)
scripts/status_agent_backend.sh        # Agent health + recent logs
scripts/stop_agent_backend.sh          # Agent only (pgrep fallback if PID file stale)
scripts/start_aef_inference_service.sh # AEF only
scripts/status_aef_inference_service.sh
scripts/stop_aef_inference_service.sh
```

A small pytest suite under `tests/` covers the deterministic, network-free
helpers (bbox scoring, LLM JSON extraction, month inference, rule-based intent
parsing, patch-id mapping). Run it plus Python compilation and service smoke
tests:

```bash
python -m pytest tests/ -q
python -m py_compile $(find agent aef_inference -name '*.py' -print)
scripts/status_services.sh
curl --noproxy '*' -sS http://127.0.0.1:7870/api/health
curl --noproxy '*' -sS http://127.0.0.1:7862/api/health
```

## Architecture

### Request Flow (LangGraph State Machine)

The core of the agent is an 8-node LangGraph state machine in `agent/graph/report_agent.py`. Fallback path runs sequentially without LangGraph if the optional dependency is missing.

```
load_memory → parse_intent → merge_memory → route → ...
```

- **load_memory**: Reads SQLite session state, appends user message
- **parse_intent**: Rules-first classification (IntentService) with DeepSeek LLM fallback; classifies as `report_request`, `slot_fill`, `free_chat`, `change_context`, or `confirmation`
- **merge_memory**: Merges new slots with historical slots; if a previous month exists but user didn't specify one on a new report request, it is silently reused (the previous two-step confirmation flow was removed). Clarification is only asked when no month is available at all.
- **route**: Branches to `ask_clarification` (no month available), `chat_response` (casual chat), or `run_analysis` (all slots filled). There is no longer an `ask_confirmation` branch — the `AgentRoute.ASK_CONFIRMATION` / `AgentStatus.NEEDS_CONFIRMATION` constants remain defined but are unused
- **run_analysis**: Dispatches to regional analysis service
- **generate_report**: Builds HTML/Markdown via ReportService + DeepSeek
- **write_memory**: Persists slots, summary, messages, and report index back to SQLite

### Service Topology

```
Frontend → Agent (:7870) → AEF Inference (:7862) + Harbin/Haidian Embedding API (remote)
                ↓
         agent/reports/*.html, *.md, assets/*.png
```

Harbin and Haidian share the same remote embedding API base URL (`AGENT_EMBEDDING_API_BASE_URL`) but hit different `/regions/{harbin|haidian}/...` paths.

The Agent is the unified entry point. Frontend only calls Agent. The AEF inference service can run co-located on the same server.

### Agent Service Layer

All services accept dependency-injected configs and collaborators, defaulting to production implementations:

| Service | Role |
|---------|------|
| `IntentService` | Rule-based intent parsing with Chinese keyword matching, month inference (去年九月→2025-09), task/region alias normalization; falls back to DeepSeek LLM JSON extraction when rule confidence < 0.6 |
| `MemoryService` | SQLite persistence (`agent/runtime/agent_memory.sqlite3`) with three tables: `sessions`, `messages`, `reports`. Thread-safe via RLock |
| `RegionalAnalysisService` | Router: dispatches to `AEFAnalysisService` (Yajiang), `HarbinEmbeddingAnalysisService` (Harbin), or `HaidianEmbeddingAnalysisService` (Haidian) based on region name |
| `AEFAnalysisService` | HTTP client to the AEF inference service (`AGENT_AEF_BASE_URL`, default `:7862`). Sends `POST /api/infer` with `sample_indices` + `task`; downloads result PNGs to `agent/reports/assets/`. Patch selection: if the request carries `selected_patch_ids` (from the frontend map), `patch_000040` → AEF `sample_index=40`; otherwise falls back to a deterministic pick from hardcoded pools |
| `HarbinEmbeddingAnalysisService` | HTTP client to Harbin embedding API (`AGENT_EMBEDDING_API_BASE_URL`). Three task modes: pre-generated static results (building, land use), real-time system model inference (building, water), and embedding preview images. Validates against a hardcoded month allowlist. Patch selection prefers frontend `selected_patch_ids`, then re-selects from the request AOI bbox, then a stable global pick |
| `HaidianEmbeddingAnalysisService` | HTTP client to Haidian embedding API (same base URL, `/regions/haidian/...`). Tasks: building/road extraction, construction, land use / land cover classification, water extraction. Uses the patch-level thematic **result PNG** endpoint (`system-models` inference is not yet open for Haidian) plus an embedding preview; lightweight image stats are derived from the result PNG |
| `PatchSelectionService` | Backs `POST /api/patches/search`. Given a frontend map bbox + region + task + month, returns candidate patches ranked by bbox-intersection score. Yajiang uses the local `YajiangPatchIndexService`; Harbin/Haidian query the remote embedding API's `/regions/{id}/patches` |
| `YajiangPatchIndexService` | Builds a local spatial index of Yajiang raw GeoTIFF patches by parsing GeoTIFF tags directly (no rasterio — only `pyproj` for CRS→WGS84). Reads from `downloads/xuannv_embeddings/extracted/raw/yajiang`, caches the index to `agent/runtime/yajiang_patch_index.json` |
| `MockAnalysisService` | Deterministic placeholder with matplotlib bar charts; used when no real service is configured or as fallback |
| `ReportService` | Generates business-oriented HTML/Markdown reports (template `agent-report-v5`): 执行摘要 → 核心要点 → 关键指标 → 结果图与数据分布 → 深度解读 → 建议与提醒. DeepSeek organizes the language (conclusion-first, layered prose, no technical jargon); metric cards are filtered to business metrics only and no internal payload/paths are emitted. Falls back to a template on API failure. Class distributions come from `AnalysisResult.data_table`. Report reuse via content fingerprinting; auto-prunes old reports (default: keep 50) |
| `DeepSeekProvider` | Raw `urllib`-based HTTP client to DeepSeek API; no SDK dependency. Returns `None` on any failure, setting `last_status` for upstream fallback decisions |

### AEF Inference Service (`aef_inference/`)

A standalone FastAPI service that loads a PyTorch deploy model and exposes REST endpoints for the Agent to call. It intentionally does **not** contain model weights, training code, or data — those live at `AEF_CODE_ROOT` (default `/data/heyuhang/yajiang-aef`) and are added to `PYTHONPATH` at startup.

| File | Role |
|------|------|
| `server.py` | FastAPI app: `POST /api/infer` (main entry), `GET /api/patch-rgb` (RGB preview), `GET /api/health`, `GET /api/meta` |
| `runner.py` | Core engine: model loading (`load_deploy_model`), forward pass, artifact generation (matplotlib charts for landcover/water/dem), file-based caching with `SERVICE_CACHE_VERSION` key |

**Supported tasks**: `water` (JRC water classification with softmax + threshold), `landcover` (WorldCover 9-class), `dem` (elevation reconstruction with terrain viz), `all` (run all three).

**Key design points**:
- Artifacts are cached to disk (`AEF_CACHE_DIR`) keyed by `(sample_indices, task, version)` — identical requests hit cache
- Water classification uses a continuous 0–100 water grade from softmax, not raw argmax; `water_threshold` controls the binary mask
- DEM outputs separate "user display" charts (hillshade, contours, elevation zones, slope, profile) from "model validation" charts (target/prediction/error comparison)
- Landcover class mapping follows a custom WorldCover remapping defined in `data/full_npy/preprocess_meta.json`

### Data Schemas (`agent/schemas/report.py`)

- **ReportRequest**: `task`, `region`, `prompt`, `time_range` (YYYY-MM), `session_id`, `selected_patch_ids` (frontend map-selected patches), `aoi` (bbox selection)
- **AgentIntent**: Parsed intent with `message_type`, extracted slots, `confidence`, `missing_fields`, `confirmation_fields`. `is_complete` is True when nothing missing and nothing to confirm
- **AnalysisResult**: Structured analysis with metrics, findings, charts, narrative blocks, risks, limitations
- **ReportArtifact**: Output paths (`html_url`, `markdown_url`), sections, reuse flag
- **AgentResponse**: Union of all response states; `status` drives frontend behavior (`ok`, `needs_input`, `chat`). `needs_confirmation` is defined but no longer emitted

### Configuration (`agent/config.py`)

All config is env-driven via dataclasses with `field(default_factory=...)`. The Python code itself does no `.env` loading, but `scripts/start_agent_backend.sh` sources a gitignored `.env` at the repo root (if present) before launching, so secrets like `DEEPSEEK_API_KEY` persist across restarts without entering git. Set variables in the environment directly when running uvicorn manually. Key variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEEPSEEK_API_KEY` | (empty) | When unset, falls back to rule parsing and template reports |
| `AGENT_AEF_BASE_URL` | `http://127.0.0.1:7862` | Yajiang AEF inference service |
| `AGENT_EMBEDDING_API_BASE_URL` | `http://60.31.21.42:22065` | Harbin/Haidian embedding API (shared base URL) |
| `AGENT_YAJIANG_RAW_ROOT` | `downloads/xuannv_embeddings/extracted/raw/yajiang` | Yajiang raw GeoTIFF patch root for the local spatial index |
| `AGENT_YAJIANG_PATCH_INDEX` | `agent/runtime/yajiang_patch_index.json` | Cached Yajiang patch spatial index |
| `AGENT_PORT` | `7870` | Agent server port |
| `AGENT_MAX_REPORTS` | `50` | Max report files to retain; 0 = unlimited |
| `AEF_CODE_ROOT` | `/data/heyuhang/yajiang-aef` | External training/model code root (added to PYTHONPATH) |
| `AEF_PORT` | `7862` | AEF inference service port |
| `AEF_CONFIG` | `$AEF_CODE_ROOT/configs/yajiang_v1_2_continue_200.yaml` | Model config YAML |
| `AEF_MANIFEST` | `$AEF_CODE_ROOT/data/full_npy/train.jsonl` | Dataset manifest |
| `AEF_DEPLOY_MODEL` | `$AEF_CODE_ROOT/outputs/.../exports/..._deploy.pt` | Deploy model weights |
| `AEF_CACHE_DIR` | `$AEF_CODE_ROOT/outputs/aef_inference_service_v1_2_continue_200` | Inference artifact cache |
| `AEF_DEVICE` | `auto` | Torch device (`auto` resolves to cuda if available) |

### Backend Entry Point (`agent/backend/app.py`)

Dual-mode server: FastAPI (primary) with a legacy `http.server` fallback for environments without FastAPI installed. The `app` module-level variable is the FastAPI instance; `create_app()` builds it with CORS middleware and route registration. Static report files are served from `agent/reports/`.

Main routes: `POST /api/report` (run the agent), `POST /api/patches/search` (map-selection patch lookup via `PatchSelectionService`), `POST /api/session/reset`, `GET /api/sessions`, `GET /api/session/{id}`, `GET /api/health`. Both the FastAPI and legacy handlers register the same endpoints.

### Directory Layout

```
agent/
  backend/       FastAPI app, routes, markdown-to-HTML API docs renderer
  graph/         LangGraph state machine (ReportAgent)
  schemas/       Pydantic-style dataclasses for request/response/report
  services/      Intent parsing, memory, report gen, LLM provider, regional adapters
                 (aef/harbin/haidian), patch selection + Yajiang local patch index,
                 common.py (shared bbox scoring + LLM JSON extraction)
tests/           pytest units for the deterministic, network-free helpers
  prompts/       (reserved, currently empty)
  ui/            Mock frontend HTML page
  assets/        (reserved, currently empty)
aef_inference/
  server.py      FastAPI app exposing /api/infer, /api/patch-rgb, /api/health, /api/meta
  runner.py      Model loading, inference, visualization, caching (1436 lines)
scripts/         start/stop/status shell scripts for Agent, AEF, and both together
```

### Important Design Rules

- **Reports and runtime data live in `agent/reports/` and `agent/runtime/`** — these directories are gitignored and must never be committed
- **Model weights and training code are not in this repo** — the AEF inference service loads them from `AEF_CODE_ROOT` (an external path on the server) via `PYTHONPATH`
- **AEF service should start before Agent** — `scripts/start_services.sh` enforces this by health-checking AEF before launching Agent
- **Month is the only required slot** — the agent will ask for clarification if missing, even when task and region are provided
- **Month availability is pre-validated** — before dispatching to any model/API, the agent checks the requested month against `region_availability.py` (Yajiang 2023-01..2026-03 by quarter; Harbin 2025-04/06/08/09/10; Haidian 2025-12..2026-05). An unavailable month returns a friendly `needs_input` with the region's coverage instead of a raw upstream error. Analysis failures are likewise caught and turned into a friendly reply
- **Historical month is silently reused** — on a new report request with no month, the previous session's month is adopted without a confirmation step. Clarification is only asked when no month exists at all. (This replaced an earlier explicit-confirmation flow; the confirmation node/route/status constants remain in the code but are dead)
- **Frontend map selection is optional** — when the request carries `selected_patch_ids`/`aoi`, services prefer those patches; otherwise they fall back to AOI search or a deterministic/global pick
- **Rules-first, LLM-fallback** for intent parsing: when rule confidence ≥ 0.6, skip the LLM call entirely
- **All LLM calls degrade gracefully** — if `DEEPSEEK_API_KEY` is unset or the API fails, the system falls back to template-based report content and rule-based intent parsing
- **The agent has no authentication** — CORS is open by default (`*`); this is designed as an internal service behind EIP DNAT
