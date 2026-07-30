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
- **parse_intent**: Rules-first classification (IntentService) with DeepSeek LLM fallback; classifies as `report_request`, `slot_fill`, `free_chat`, `change_context`, `confirmation`, or `follow_up` (a question/discussion about the already-generated report — cued by 详细/解释/为什么/这个结论… when no new task or month is named). Also deterministically sets `AgentIntent.scenario` (`checkup`/`change`/`score`) for the Haidian composite scenarios from keyword cues; questions are excluded (they stay discussion). An imperative scenario ask is kept rules-first (high confidence) so it doesn't defer to the LLM and get reclassified
- **merge_memory**: Merges new slots with historical slots; if a previous month exists but user didn't specify one on a new report request, it is silently reused (the previous two-step confirmation flow was removed). Clarification is only asked when no month is available at all. Scenario turns branch first: `checkup`/`score` need month + AOI (shared `_merge_month_aoi`), `change` needs two months + AOI (`_merge_change`); scenarios are sticky across slot-fill turns
- **route**: Branches to `ask_clarification` (no month available), `chat_response` (casual chat), or `run_analysis` (all slots filled). There is no longer an `ask_confirmation` branch — the `AgentRoute.ASK_CONFIRMATION` / `AgentStatus.NEEDS_CONFIRMATION` constants remain defined but are unused
- **capability gate** (inside `merge_memory`, before scenario slot logic): if the turn names a **non-native** object (湿地/机场/…), `CapabilityService.resolve` decides — `custom_ready` sets `intent.custom_model_id` and falls through; `custom_training` returns `needs_input` (wait); `custom_failed`/`needs_annotation` return `needs_annotation` with an `open_annotation_ui` `action` and persist `pending_custom_model`. If a pending model exists, the next turn re-verifies its **real** status (never trusts “好了”) and resumes the original task or re-offers the handoff
- **run_analysis**: Dispatches by `intent.scenario` to `RegionCheckupService` / `ChangeMonitorService` / `PressureScoreService` (Haidian composite scenarios), otherwise to `RegionalAnalysisService` (ordinary single-task reports). A resolved `custom_model_id` makes `ChangeMonitorService` run the custom model via `POST /models/{id}/infer` per patch (target=class colour on grey background) instead of the system-task result endpoint
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
| `RegionCheckupService` | **Scenario A (片区综合体检, Haidian).** Over one framed AOI + month, aggregates 4 binary-task coverages + land-cover class distribution into one `AnalysisResult`. Uses `agent/tools/aoi.py` + `classmap`. `scenario="checkup"` |
| `ChangeMonitorService` | **Scenario B (建设扰动短周期监测, Haidian).** One AOI, one binary task (default 施工), two months → per-patch pixel-level diff of the aligned result PNGs, aggregated to gained/lost/net area. Uses `agent/tools/change.py` + `AoiCoverService.fetch_result_array`. `scenario="change"` |
| `PressureScoreService` | **Scenario C (高硬化低绿地压力评分 / 补绿优先区, Haidian).** Per patch scores built-up (impervious, from building task) against green deficit (from land-cover), ranks TOP-N补绿 priority zones. Uses `agent/tools/scoring.py`. `scenario="score"`. Green ratio is advisory (land-cover model), flagged in report limitations |
| `ModelRegistryService` | Read-only wrapper over embedding-api `GET /models` (+ `/models/{id}`, `/models/jobs/{id}`, `/models/capabilities`), cached per region. Splits results into `system` vs `custom` (native taxonomy), exposes `find_custom_models(region, class_name)`, `capabilities(region)` (training methods + task contracts), and `ModelInfo.is_ready/is_training/is_failed` + enriched training metadata (`resolved_training_method/feature_source/accuracy/n_samples`, `uses_annual_feature` for AEF). Never trains or annotates |
| `CapabilityService` | Capability gate in front of `run_analysis`. `resolve(region, object)` classifies a requested analysis object into `native` (built-in task/land-cover class → proceed), `custom_ready` (trained model → proceed with `model_id`), `custom_training` (ask to wait), `custom_failed` (say training failed, offer retry), or `needs_annotation` (hand off to标注). Builds the `open_annotation_ui` handoff `action`, enriched from `capabilities` (region's `default_training_method` + task temporal contract; no method picker exposed). Uses `agent/taxonomy.py` native/non-native word lists |
| `PatchSelectionService` | Backs `POST /api/patches/search`. Given a frontend map bbox + region + task + month, returns candidate patches ranked by bbox-intersection score. Yajiang uses the local `YajiangPatchIndexService`; Harbin/Haidian query the remote embedding API's `/regions/{id}/patches` |
| `YajiangPatchIndexService` | Builds a local spatial index of Yajiang raw GeoTIFF patches by parsing GeoTIFF tags directly (no rasterio — only `pyproj` for CRS→WGS84). Reads from `downloads/xuannv_embeddings/extracted/raw/yajiang`, caches the index to `agent/runtime/yajiang_patch_index.json` |
| `MockAnalysisService` | Deterministic placeholder with matplotlib bar charts; used when no real service is configured or as fallback |
| `ReportService` | Generates business-oriented HTML/Markdown reports (template `agent-report-v9`): 执行摘要 → 核心要点 → 关键指标 → 结果图与数据分布 → 深度解读 → 建议与提醒. **No embedded map** — the on-map overlay module lives in the frontend's right-side map panel (driven by `charts[].overlay` / `bounds_wgs84`); the report only shows result images as static figures. DeepSeek organizes the language (conclusion-first, layered prose, no technical jargon); metric cards are filtered to business metrics only and no internal payload/paths are emitted. Falls back to a template on API failure. Class distributions come from `AnalysisResult.data_table`. The sections *inside* 深度解读 are not fixed — they come from a `ReportSkeleton` (see below). Report reuse via content fingerprinting; auto-prunes old reports (default: keep 50) |
| `report_skeletons` | Per-task/per-scenario report shapes (pure data, no I/O). Each skeleton carries the reader's question, the audience, and a brief + length budget per section; `output_format()` renders the `输出格式` contract handed to the writing model. `resolve()` picks by scenario (`checkup`/`change`/`score`, read from `aef_payload["scenario"]`) → custom-model object → task family (substring match on the taxonomy vocabulary) → generic. Every skeleton ends with the same 数据说明与使用边界 section. Template fallback and report revisions reuse the same headings, so a degraded or edited report still reads as the right kind of report |
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

### Pure-function Tools (`agent/tools/`)

Side-effect-free, independently unit-tested analysis atoms the Haidian scenario services compose (no network, no config). `raster` (binary coverage + hectares from bounds), `classmap` (multi-class distribution from a legend), `aoi` (aggregate coverage / class distribution across an AOI's patches), `change` (pixel-level two-date mask diff → gained/lost/net + aggregate), `scoring` (0–100 pressure score, patch ranking, roll-up). `AoiCoverService.iter_patch_colors` / `fetch_result_array` are the shared fetch loops feeding them.

### Data Schemas (`agent/schemas/report.py`)

- **ReportRequest**: `task`, `region`, `prompt`, `time_range` (YYYY-MM), `session_id`, `selected_patch_ids` (frontend map-selected patches), `aoi` (bbox selection), `before_time_range`/`after_time_range` (scenario B two-date window), `custom_model_id`/`target_object` (custom-model analysis of a non-native object)
- **AgentResponse**: adds `action` (`{}` normally; an `open_annotation_ui` handoff instruction when `status=needs_annotation`). New status `needs_annotation`
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
                 satellite_basemap.py (Esri World Imagery tiles for the patch footprint),
                 common.py (shared bbox scoring + LLM JSON extraction + markdown stripping)
tests/           pytest units for the deterministic, network-free helpers
  prompts/       (reserved, currently empty)
  ui/            Mock frontend HTML page
  assets/        (reserved, currently empty)
aef_inference/
  server.py      FastAPI app exposing /api/infer, /api/patch-rgb, /api/health, /api/meta
  runner.py      Model loading, inference, visualization, caching (1436 lines)
docs/            Product/planning docs: capability-boundary analysis (10m 底座场景边界),
                 the development plans (海淀三场景与架构演进; 自定义地物标注与训练能力)
scripts/         start/stop/status shell scripts for Agent, AEF, and both together
```

### Important Design Rules

- **Reports and runtime data live in `agent/reports/` and `agent/runtime/`** — these directories are gitignored and must never be committed
- **Model weights and training code are not in this repo** — the AEF inference service loads them from `AEF_CODE_ROOT` (an external path on the server) via `PYTHONPATH`
- **AEF service should start before Agent** — `scripts/start_services.sh` enforces this by health-checking AEF before launching Agent
- **Month is the only required slot** — the agent will ask for clarification if missing, even when task and region are provided
- **Month availability is pre-validated** — before dispatching to any model/API, the agent checks the requested month against `region_availability.py` (Yajiang 2023-01..2026-03 by quarter; Harbin 2025-04/06/08/09/10; Haidian 2025-12..2026-05). An unavailable month returns a friendly `needs_input` with the region's coverage instead of a raw upstream error. Analysis failures are likewise caught and turned into a friendly reply
- **Historical month is silently reused** — on a new report request with no month, the previous session's month is adopted without a confirmation step. Clarification is only asked when no month exists at all. (This replaced an earlier explicit-confirmation flow; the confirmation node/route/status constants remain in the code but are dead)
- **Follow-up questions are answered grounded, not regenerated** — after a report is generated, a compact context (summary, business metrics, class distribution, deep-interpretation blocks) is stored in `sessions.report_context`. A `follow_up` (or any chat turn with a stored context) is answered by the chat LLM using that context, so the agent discusses/explains the existing report instead of producing a new one
- **Frontend map selection is optional** — when the request carries `selected_patch_ids`/`aoi`, services prefer those patches; otherwise they fall back to AOI search or a deterministic/global pick
- **Rules-first, LLM-fallback** for intent parsing: when rule confidence ≥ 0.6, skip the LLM call entirely
- **All LLM calls degrade gracefully** — if `DEEPSEEK_API_KEY` is unset or the API fails, the system falls back to template-based report content and rule-based intent parsing
- **The agent has no authentication** — CORS is open by default (`*`); this is designed as an internal service behind EIP DNAT
