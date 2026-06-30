# Xuannv Agent

遥感专题报告 Agent。它把用户的自然语言需求整理成标准化任务，调用区域模型服务完成分析，并生成带图表、指标和文字解读的 HTML / Markdown 报告。

这个仓库只放 Agent 系统和面向 Agent 的推理服务运行时；训练工程、模型权重、原始数据和大体量推理产物不放在这里。

## What It Does

- 理解用户意图：识别任务、地区、月份，缺少月份时主动追问。
- 支持多轮会话：SQLite 记录会话、历史槽位和报告索引。
- 编排分析流程：根据地区路由到本机 AEF 服务或在线区域 API。
- 生成专题报告：输出结构化指标、图片资产、HTML 报告和 Markdown 原文。
- 服务前端联调：提供统一 Agent API，前端不需要直接访问模型服务。

## Current Coverage

| Region | Tasks | Backend |
| --- | --- | --- |
| 雅江区域 | 地物分类、水体分类、高程地形 | 本机 AEF 推理服务 |
| 哈尔滨新区 | 建筑物提取、土地利用分类、水体提取 | 在线 embedding-api |

雅江 AEF 服务默认使用服务器上的 `v1_2_continue_200` 模型资源。模型路径通过环境变量配置，默认指向：

```text
/data/heyuhang/yajiang-aef
```

## Architecture

```text
Frontend
  -> Xuannv Agent :7870
      -> Yajiang AEF Inference :7862
      -> Harbin Embedding API
      -> agent/reports/
```

Agent 是唯一对前端暴露的业务入口。AEF 推理服务只作为内部模型能力，被 Agent 调用。

## Quick Start

推荐使用现有 `hyh-dl` 环境：

```bash
cd /data/heyuhang/Xuannv_Agent
pip install -r requirements.txt
scripts/start_services.sh
```

检查状态：

```bash
scripts/status_services.sh
```

本机访问：

```text
http://127.0.0.1:7870/
http://127.0.0.1:7870/api-docs
```

停止服务：

```bash
scripts/stop_services.sh
```

## Service Scripts

```bash
# Agent + AEF
scripts/start_services.sh
scripts/status_services.sh
scripts/stop_services.sh

# Agent only
scripts/start_agent_backend.sh
scripts/status_agent_backend.sh
scripts/stop_agent_backend.sh

# AEF only
scripts/start_aef_inference_service.sh
scripts/status_aef_inference_service.sh
scripts/stop_aef_inference_service.sh
```

## API

核心接口：

```text
GET  /api/health
POST /api/report
GET  /api/sessions
GET  /api/session/{session_id}
POST /api/session/reset
```

启动服务后访问：

```text
/api-docs
/docs
```

详细接口文档见 [agent/API.md](agent/API.md)。

## Configuration

常用环境变量：

| Variable | Default | Description |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | empty | 可选。未设置时使用规则解析和模板报告兜底 |
| `AGENT_PORT` | `7870` | Agent 服务端口 |
| `AGENT_AEF_BASE_URL` | `http://127.0.0.1:7862` | 雅江 AEF 推理服务 |
| `AGENT_EMBEDDING_API_BASE_URL` | `http://60.31.21.42:22065` | 哈尔滨 embedding-api |
| `AGENT_CORS_ORIGINS` | `*` | CORS 来源 |
| `AEF_CODE_ROOT` | `/data/heyuhang/yajiang-aef` | 外部模型工程根目录 |
| `AEF_PORT` | `7862` | AEF 推理服务端口 |
| `AEF_DEVICE` | `auto` | AEF 推理设备 |

AEF 模型资源也可以显式指定：

```bash
export AEF_CONFIG=/path/to/config.yaml
export AEF_MANIFEST=/path/to/train.jsonl
export AEF_DEPLOY_MODEL=/path/to/model_deploy.pt
export AEF_CACHE_DIR=/path/to/cache_dir
```

## Project Layout

```text
agent/
  backend/      FastAPI app, API docs renderer, static report serving
  graph/        Agent state machine and routing
  schemas/      request / response / report data structures
  services/     intent, memory, report, LLM, regional model adapters
  ui/           lightweight frontend page

aef_inference/
  server.py     AEF inference FastAPI service
  runner.py     model loading, inference, visualization, cache handling

scripts/        service start / stop / status helpers
```

## Runtime Files

These paths are intentionally ignored by git:

```text
agent/reports/
agent/runtime/
data/
outputs/
checkpoints/
models/
```

Do not commit API keys, `.env` files, model weights, generated reports, or cache artifacts.

## Development Checks

```bash
python -m py_compile $(find agent aef_inference -name '*.py' -print)
scripts/status_services.sh
curl --noproxy '*' -sS http://127.0.0.1:7870/api/health
curl --noproxy '*' -sS http://127.0.0.1:7862/api/health
```

## Design Notes

- LLM 负责理解与表达，结构化指标来自模型服务。
- 月份是报告生成的关键槽位；缺失时 Agent 会追问。
- 历史月份不会被静默复用，需要用户确认。
- 前端只调用 Agent；模型服务不直接暴露给前端。
- 当前雅江区域仍使用临时 patch 选择器，后续可替换为正式 AOI 检索服务。
