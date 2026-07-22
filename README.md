# Xuannv Agent

面向遥感专题报告的智能 Agent。它理解用户的自然语言需求，提取任务、地区、时间等标准字段，路由到对应的区域模型服务完成分析，并生成包含图表、指标和文字解读的 HTML / Markdown 报告。

本仓库只包含 Agent 系统和面向 Agent 的推理服务运行时；训练工程、模型权重、原始数据和大体量推理产物不放在这里。

## 核心能力

- **意图理解**：规则优先、LLM 兜底，从自然语言中识别任务、地区、月份（如“去年九月”→`2025-09`）；缺少月份时主动追问。
- **多轮会话**：使用 SQLite 保存会话、历史槽位、消息记录和报告索引；新请求未指定月份时自动沿用上一次的月份。
- **任务编排**：8 节点 LangGraph 状态机，根据地区和任务路由到本机 AEF 服务或在线区域 API；海淀支持三个复合场景。
- **多 patch 聚合**：前端地图框选多个 patch 后，推理和报告覆盖全部选中 patch，指标为区域面积加权合计，结果图按 UTM 网格拼接为一张连续图。
- **能力边界与自定义地物**：请求非内置地物时，Agent 判定可直接分析、复用已训练自定义模型，还是引导前往标注/训练。
- **报告生成**：输出结构化指标、图片资产、HTML 报告和 Markdown 原文；报告可按内容指纹复用。
- **跟进问答**：报告生成后，围绕已有报告的追问基于存储的报告上下文回答，不重复生成。

## 当前支持

| 地区 | 支持任务 | 模型 / 服务 |
| --- | --- | --- |
| 雅江区域 | 地物分类、水体分类、高程地形 | 本机 AEF 推理服务；本地 patch 空间索引 |
| 哈尔滨新区 | 建筑物提取、土地利用分类、水体提取 | 在线 embedding-api |
| 北京市海淀区 | 建筑物提取、道路提取、施工识别、土地利用 / 土地覆盖分类、水体提取 | 在线 embedding-api 专题结果 |

海淀区在普通单任务报告之外，还提供三个复合场景：

- **场景 A · 片区综合体检**：一个框选 AOI + 一个月份，聚合 4 个二值任务覆盖率 + 土地覆盖分布为一份综合报告。
- **场景 B · 建设扰动短周期监测**：一个 AOI + 一个二值任务（默认施工）+ 两个月份，逐 patch 像素级比对结果图，汇总为新增 / 减少 / 净变化面积。
- **场景 C · 高硬化低绿地压力评分**：逐 patch 对硬化度与绿地缺口打分，排出 TOP-N 补绿优先区。

雅江 AEF 服务默认复用服务器上的 `v1_2_continue_200` 模型资源，默认模型工程路径：

```text
/data/heyuhang/yajiang-aef
```

## 架构

```text
前端
  -> Xuannv Agent :7870
      -> 雅江 AEF 推理服务 :7862
      -> 哈尔滨 / 海淀 embedding-api（共用 base URL，不同 /regions 路径）
      -> agent/reports/*.html, *.md, assets/*.png
```

Agent 是唯一面向前端的业务入口。AEF 推理服务作为内部模型能力由 Agent 调用，不建议直接暴露给前端。

### 请求流程（LangGraph 状态机）

```text
load_memory -> parse_intent -> merge_memory -> route -> run_analysis -> generate_report -> write_memory
```

- **parse_intent**：规则优先分类（报告请求 / 补槽 / 闲聊 / 换上下文 / 跟进提问），置信度不足时用 DeepSeek 兜底；同时确定海淀复合场景。
- **merge_memory**：合并历史槽位并复用上一次月份；能力门在此判定非内置地物走哪条路径。
- **route**：分流到追问、闲聊回复或运行分析。
- **run_analysis**：按场景分派到片区体检 / 变化监测 / 压力评分服务，普通任务走区域分析服务。

## 快速启动

推荐使用服务器上已有的 `hyh-dl` 环境：

```bash
cd /data/heyuhang/Xuannv_Agent
pip install -r requirements.txt
scripts/start_services.sh    # 先启动 AEF，健康检查通过后再启动 Agent
```

本机访问：

```text
http://127.0.0.1:7870/        # 前端页面
http://127.0.0.1:7870/ui      # Mock 前端
http://127.0.0.1:7870/api-docs
```

查看状态 / 停止：

```bash
scripts/status_services.sh
scripts/stop_services.sh
```

> 后端 `.py` 改动需重启服务（nohup 守护，无热重载）；`/ui` 页面以 `no-store` 返回，改前端只需浏览器强制刷新。

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
POST /api/patches/search      # 地图框选后的候选 patch 检索
POST /api/report              # 运行 Agent，生成报告
GET  /api/sessions
GET  /api/session/{session_id}
POST /api/session/reset
```

启动后可访问 `/api-docs` 与 `/docs`。详细接口文档见 [agent/API.md](agent/API.md)，前端对接说明见 [agent/FRONTEND_GUIDE.md](agent/FRONTEND_GUIDE.md)。

## 配置

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | 可选。未设置时使用规则解析和模板报告兜底 |
| `AGENT_PORT` | `7870` | Agent 服务端口 |
| `AGENT_AEF_BASE_URL` | `http://127.0.0.1:7862` | 雅江 AEF 推理服务地址 |
| `AGENT_EMBEDDING_API_BASE_URL` | `http://60.31.21.42:22065` | 哈尔滨 / 海淀 embedding-api 地址 |
| `AGENT_MAX_SELECTED_PATCHES` | `8` | 海淀普通报告单次处理的最大 patch 数 |
| `AGENT_MAX_REPORTS` | `50` | 报告文件保留上限，0 为不限 |
| `AGENT_CORS_ORIGINS` | `*` | CORS 来源 |
| `AEF_CODE_ROOT` | `/data/heyuhang/yajiang-aef` | 外部 AEF 模型工程根目录（加入 PYTHONPATH）|
| `AEF_PORT` | `7862` | AEF 推理服务端口 |
| `AEF_DEVICE` | `auto` | AEF 推理设备（`auto` 有 GPU 则用 cuda）|

`scripts/start_agent_backend.sh` 会在启动前 source 仓库根目录下 gitignored 的 `.env`，使 `DEEPSEEK_API_KEY` 等密钥在重启间持久，无需进入 git。手动运行 uvicorn 时请自行在环境中设置。

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
  graph/        LangGraph 状态机和节点编排
  schemas/      请求、响应、报告数据结构
  services/     意图解析、记忆、报告、LLM、区域模型适配、patch 检索、能力门
  tools/        无副作用分析原子：raster / classmap / aoi / change / scoring
  ui/           轻量前端页面
aef_inference/
  server.py     AEF 推理服务 API（/api/infer、/api/patch-rgb、/api/health、/api/meta）
  runner.py     模型加载、推理、可视化和缓存管理
docs/           能力边界与开发规划文档
scripts/        服务启动、停止和状态检查脚本
tests/          针对确定性、无网络辅助函数的 pytest 单测
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
python -m pytest tests/ -q
python -m py_compile $(find agent aef_inference -name '*.py' -print)
scripts/status_services.sh
curl --noproxy '*' -sS http://127.0.0.1:7870/api/health
curl --noproxy '*' -sS http://127.0.0.1:7862/api/health
```

## 设计约定

- LLM 负责理解用户意图和组织报告语言，结构化指标来自模型服务；LLM 不可用时全链路降级到规则解析和模板报告。
- 月份是报告生成的唯一必填槽位；缺失时先追问，新请求可自动沿用上一次月份。
- 前端只调用 Agent，不直接调用模型服务。
- 雅江、哈尔滨、海淀支持前端地图框选并定位 patch；框选即选中框内全部 patch（上限 `AGENT_MAX_SELECTED_PATCHES`），可点选取消。
- 海淀多 patch 报告按与选区相交的整块 patch 面积加权合计，结果图按 UTM 网格拼接为一张连续图，空缺保持透明。
- Agent 自身不做鉴权，CORS 默认放开，设计为 EIP DNAT 之后的内部服务。
