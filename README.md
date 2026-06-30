# Xuannv Agent

面向遥感专题报告的智能 Agent。它负责理解用户的自然语言需求，提取任务、地区和时间等标准字段，调用区域模型服务完成分析，并生成包含图表、指标和文字解读的 HTML / Markdown 报告。

本仓库只包含 Agent 系统和面向 Agent 的推理服务运行时；训练工程、模型权重、原始数据和大体量推理产物不放在这里。

## 核心能力

- **意图理解**：从自然语言中识别任务、地区、月份；缺少月份时主动追问。
- **多轮会话**：使用 SQLite 保存会话、历史槽位、消息记录和报告索引。
- **任务编排**：根据地区和任务路由到本机 AEF 服务或在线区域 API。
- **报告生成**：输出结构化指标、图片资产、HTML 报告和 Markdown 原文。
- **前端联调**：提供统一 Agent API，前端不需要直接访问模型服务。

## 当前支持

| 地区 | 支持任务 | 模型/服务 |
| --- | --- | --- |
| 雅江区域 | 地物分类、水体分类、高程地形 | 本机 AEF 推理服务 |
| 哈尔滨新区 | 建筑物提取、土地利用分类、水体提取 | 在线 embedding-api |

雅江 AEF 服务默认复用服务器上的 `v1_2_continue_200` 模型资源。默认模型工程路径为：

```text
/data/heyuhang/yajiang-aef
```

## 架构

```text
前端
  -> Xuannv Agent :7870
      -> 雅江 AEF 推理服务 :7862
      -> 哈尔滨 embedding-api
      -> agent/reports/
```

Agent 是唯一面向前端的业务入口。AEF 推理服务作为内部模型能力，由 Agent 调用，不建议直接暴露给前端。

## 快速启动

推荐使用服务器上已有的 `hyh-dl` 环境：

```bash
cd /data/heyuhang/Xuannv_Agent
pip install -r requirements.txt
scripts/start_services.sh
```

查看服务状态：

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

## 服务脚本

```bash
# 同时管理 Agent 和 AEF
scripts/start_services.sh
scripts/status_services.sh
scripts/stop_services.sh

# 只管理 Agent
scripts/start_agent_backend.sh
scripts/status_agent_backend.sh
scripts/stop_agent_backend.sh

# 只管理 AEF 推理服务
scripts/start_aef_inference_service.sh
scripts/status_aef_inference_service.sh
scripts/stop_aef_inference_service.sh
```

## API

核心接口：

```text
GET  /api/health
POST /api/patches/search
POST /api/report
GET  /api/sessions
GET  /api/session/{session_id}
POST /api/session/reset
```

启动服务后可以访问：

```text
/api-docs
/docs
```

详细接口文档见 [agent/API.md](agent/API.md)。

## 配置

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | 可选。未设置时使用规则解析和模板报告兜底 |
| `AGENT_PORT` | `7870` | Agent 服务端口 |
| `AGENT_AEF_BASE_URL` | `http://127.0.0.1:7862` | 雅江 AEF 推理服务地址 |
| `AGENT_EMBEDDING_API_BASE_URL` | `http://60.31.21.42:22065` | 哈尔滨 embedding-api 地址 |
| `AGENT_CORS_ORIGINS` | `*` | CORS 来源 |
| `AEF_CODE_ROOT` | `/data/heyuhang/yajiang-aef` | 外部 AEF 模型工程根目录 |
| `AEF_PORT` | `7862` | AEF 推理服务端口 |
| `AEF_DEVICE` | `auto` | AEF 推理设备 |

如需切换模型资源，可以显式指定：

```bash
export AEF_CONFIG=/path/to/config.yaml
export AEF_MANIFEST=/path/to/train.jsonl
export AEF_DEPLOY_MODEL=/path/to/model_deploy.pt
export AEF_CACHE_DIR=/path/to/cache_dir
```

## 目录结构

```text
agent/
  backend/      FastAPI 应用、API 文档渲染、静态报告服务
  graph/        Agent 状态机和节点编排
  schemas/      请求、响应、报告数据结构
  services/     意图解析、记忆、报告、LLM、区域模型适配
  ui/           轻量前端页面

aef_inference/
  server.py     AEF 推理服务 API
  runner.py     模型加载、推理、可视化和缓存管理

scripts/        服务启动、停止和状态检查脚本
```

## 运行产物

以下内容会被 git 忽略：

```text
agent/reports/
agent/runtime/
data/
outputs/
checkpoints/
models/
```

不要提交 API key、`.env` 文件、模型权重、生成报告或缓存产物。

## 开发检查

```bash
python -m py_compile $(find agent aef_inference -name '*.py' -print)
scripts/status_services.sh
curl --noproxy '*' -sS http://127.0.0.1:7870/api/health
curl --noproxy '*' -sS http://127.0.0.1:7862/api/health
```

## 设计约定

- LLM 负责理解用户意图和组织报告语言，结构化指标来自模型服务。
- 月份是报告生成的关键槽位；缺失时 Agent 会先追问。
- 历史月份不会被静默复用，需要用户确认。
- 前端只调用 Agent，不直接调用模型服务。
- 当前雅江区域仍使用临时 patch 选择器，后续可替换为正式 AOI 检索服务。
