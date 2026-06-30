# Xuannv Agent

面向遥感专题报告的 Agent 服务。该仓库只包含 Agent 后端、前端 mock、会话记忆、报告生成和区域模型 API 适配层；模型训练、AEF 推理服务和大体量数据资产不放在这里。

## 当前能力

- 多轮会话与 SQLite 持久记忆
- 自然语言意图解析、月份补槽和历史槽位确认
- 报告生成，输出 HTML / Markdown / 图片资产
- 雅江区域：调用外部 AEF 推理服务
- 哈尔滨新区：调用在线 embedding-api
  - 建筑物提取
  - 土地利用分类
  - 水体提取

## 服务依赖

Agent 是统一入口，前端只需要调用 Agent。

```text
Frontend
  -> Xuannv Agent
      -> Yajiang AEF service, default http://127.0.0.1:7862
      -> Harbin embedding-api, default http://60.31.21.42:22065
      -> agent/reports/*.html, *.md, assets/*.png
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
scripts/start_agent_backend.sh
scripts/status_agent_backend.sh
scripts/stop_agent_backend.sh
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
```

## 不放入仓库的内容

- `agent/reports/`
- `agent/runtime/`
- 训练代码和训练配置
- 模型权重、推理输出、大数据资产
- API key 和 `.env`
