# Xuannv Agent

面向遥感专题报告的 Agent 服务。该仓库包含 Agent 后端、前端 mock、会话记忆、报告生成、区域模型 API 适配层，以及面向 Agent 的 AEF 推理服务壳；模型训练工程、大体量数据资产和模型权重不放在这里。

## 当前能力

- 多轮会话与 SQLite 持久记忆
- 自然语言意图解析、月份补槽和历史槽位确认
- 报告生成，输出 HTML / Markdown / 图片资产
- 雅江区域：调用外部 AEF 推理服务
- 哈尔滨新区：调用在线 embedding-api
  - 建筑物提取
  - 土地利用分类
  - 水体提取
- AEF 推理服务：提供 `/api/infer`、`/api/patch-rgb` 等接口给 Agent 调用

## 服务依赖

Agent 是统一入口，前端只需要调用 Agent。AEF 服务可以由本仓库脚本启动，但会复用服务器上已有的模型代码、配置、数据和权重路径。

```text
Frontend
  -> Xuannv Agent
      -> Yajiang AEF service, default http://127.0.0.1:7862
      -> Harbin embedding-api, default http://60.31.21.42:22065
      -> agent/reports/*.html, *.md, assets/*.png
```

默认 AEF 相关路径：

```text
AEF_CODE_ROOT=/data/heyuhang/yajiang-aef
AEF_CONFIG=$AEF_CODE_ROOT/configs/yajiang_v1_2_continue_200.yaml
AEF_MANIFEST=$AEF_CODE_ROOT/data/full_npy/train.jsonl
AEF_DEPLOY_MODEL=$AEF_CODE_ROOT/outputs/aef_hyh_yajiang_v1_2_continue_200/exports/aef_hyh_yajiang_v1_2_continue_200_deploy.pt
AEF_CACHE_DIR=$AEF_CODE_ROOT/outputs/aef_inference_service_v1_2_continue_200
```

## 快速启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m agent.backend.app --host 0.0.0.0 --port 7870
```

打开：

```text
http://localhost:7870/
http://localhost:7870/api-docs
```

也可以使用脚本：

```bash
scripts/start_services.sh
scripts/status_services.sh
scripts/stop_services.sh
```

只管理 Agent：

```bash
scripts/start_agent_backend.sh
scripts/status_agent_backend.sh
scripts/stop_agent_backend.sh
```

只管理 AEF 推理服务：

```bash
scripts/start_aef_inference_service.sh
scripts/status_aef_inference_service.sh
scripts/stop_aef_inference_service.sh
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | 可选。未设置时使用规则解析和模板报告兜底 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | LLM 模型名 |
| `DEEPSEEK_ENDPOINT` | `https://api.deepseek.com/chat/completions` | LLM API 地址 |
| `AGENT_AEF_BASE_URL` | `http://127.0.0.1:7862` | 雅江 AEF 推理服务 |
| `AGENT_EMBEDDING_API_BASE_URL` | `http://60.31.21.42:22065` | 哈尔滨/海淀 embedding-api |
| `AGENT_PORT` | `7870` | Agent 服务端口 |
| `AGENT_CORS_ORIGINS` | `*` | CORS 来源 |
| `AEF_CODE_ROOT` | `/data/heyuhang/yajiang-aef` | 现有 AEF 训练/模型代码根目录 |
| `AEF_PORT` | `7862` | AEF 推理服务端口 |
| `AEF_CONFIG` | 见上方默认路径 | AEF 配置文件 |
| `AEF_MANIFEST` | 见上方默认路径 | AEF 数据 manifest |
| `AEF_DEPLOY_MODEL` | 见上方默认路径 | AEF deploy 模型权重 |
| `AEF_CACHE_DIR` | 见上方默认路径 | AEF 推理产物缓存目录 |

## API 文档

详细接口见 [agent/API.md](agent/API.md)。

启动服务后也可以访问：

```text
/api-docs
/docs
```

## 目录结构

```text
agent/
  backend/      FastAPI 入口和静态页面服务
  graph/        Agent 状态机 / LangGraph 编排
  schemas/      请求、响应、报告数据结构
  services/     意图解析、记忆、报告、区域模型服务适配
  ui/           当前 mock 前端页面
scripts/        后台启停脚本
aef_inference/  AEF 推理服务 API 和 runner
```

## 不放入仓库的内容

- `agent/reports/`
- `agent/runtime/`
- 训练代码和训练配置
- 模型权重、推理输出、大数据资产
- API key 和 `.env`
