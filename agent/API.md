# Xuannv Agent API

面向前端的统一入口是 **Agent 服务**。前端只需要访问 Agent，不需要直接访问 AEF 模型推理服务。

## 服务拓扑

```text
Frontend
  -> Agent API :7870
      -> AEF inference service :7862
      -> Harbin/Haidian embedding-api http://60.31.21.42:22065
      -> agent/reports/*.html, *.md, assets/*.png
```

- 对前端暴露：`http://112.111.7.74:1112`
- 内部依赖：
  - 雅江 AEF：`http://127.0.0.1:7862`
  - 哈尔滨/海淀 embedding-api：`http://60.31.21.42:22065`
- 默认已开启 CORS：`AGENT_CORS_ORIGINS=*`

当前公网访问通过 EIP DNAT 转发：

```text
112.111.7.74:1112 -> 实例 7870
```

生产或联调环境建议只暴露 Agent 对外端口，不要把模型服务端口直接暴露给前端或公网。

## 任务和地区

当前支持任务：

| 中文任务 | 说明 |
| --- | --- |
| `地物分类` | 雅江调用 AEF 地物分类 |
| `水体分布` / `水体提取` | 雅江调用 AEF 水体分类；哈尔滨调用 `water_extraction` 实时推理；海淀调用 `water_extraction` 专题结果 |
| `建筑物提取` | 哈尔滨调用 `building_extraction`，同时展示预生成专题图和实时推理图 |
| `土地利用分类` | 哈尔滨调用 `land_use_classification` 预生成专题结果 |
| `土地覆盖分类` | 海淀调用 `land_cover_classification` 专题结果；雅江兼容到地物分类 |
| `道路提取` | 海淀调用 `road_extraction` 专题结果 |
| `施工识别` | 海淀调用 `construction` 专题结果 |
| `高程地形` | 雅江调用 AEF 高程地形；哈尔滨暂不支持 |

当前可用地区：

| 地区 | 说明 |
| --- | --- |
| `雅江区域` | 本机 AEF 闭环验证区域，依赖 `127.0.0.1:7862`；支持本地 GeoTIFF patch 空间索引 |
| `哈尔滨新区` | 在线 embedding-api 区域，依赖 `60.31.21.42:22065` |
| `北京市海淀区` | 在线 embedding-api 区域；patch 检索、专题结果 PNG 和 embedding 预览已接入 |

哈尔滨新区当前可用月份：

```text
2025-04, 2025-06, 2025-08, 2025-09, 2025-10,
2026-01, 2026-02, 2026-03, 2026-04, 2026-05
```

如果请求哈尔滨不支持的月份或任务，接口会返回 `400`，`detail` 中包含可用任务/月信息。

哈尔滨一阶段专题任务：

| 任务 | 接口来源 | 图像产物 |
| --- | --- | --- |
| `建筑物提取` | `/regions/harbin/.../tasks/building_extraction/result` + `/system-models/building_extraction/infer` | 专题结果图、实时推理图、Embedding 预览图 |
| `土地利用分类` | `/regions/harbin/.../tasks/land_use_classification/result` | 专题结果图、Embedding 预览图 |
| `水体提取` | `/system-models/water_extraction/infer` | 实时推理图、Embedding 预览图 |

海淀专题任务：

| 任务 | Agent 标准任务 ID | 图像产物 |
| --- | --- | --- |
| `建筑物提取` | `building_extraction` | 专题结果图、Embedding 预览图 |
| `道路提取` | `road_extraction` | 专题结果图、Embedding 预览图 |
| `施工识别` | `construction` | 专题结果图、Embedding 预览图 |
| `土地利用分类` | `land_use_classification` | 专题结果图、Embedding 预览图 |
| `土地覆盖分类` | `land_cover_classification` | 专题结果图、Embedding 预览图 |
| `水体提取` | `water_extraction` | 专题结果图、Embedding 预览图 |

海淀当前使用 `/regions/haidian/patches/{patch_id}/tasks/{task}/result` 获取专题 PNG；`/system-models/.../infer` 暂未开放。

时间格式：

- 推荐由用户自然语言输入，例如：`去年九月份`、`2025年9月`
- 前端也可以显式传 `time_range: "2025-09"`

## 状态机

`POST /api/report` 的响应字段 `status` 决定前端如何展示：

| status | 含义 | 前端行为 |
| --- | --- | --- |
| `ok` | 报告已生成 | 展示 `message`，并渲染 `report` 卡片和右侧预览 |
| `needs_input` | 缺少必要槽位，通常是任务或月份 | 展示 `message`，等待用户继续输入 |
| `chat` | 普通自然语言对话 | 只展示 `message`，不展示报告卡片 |

## Endpoints

### `GET /api/health`

健康检查。

响应示例：

```json
{
  "status": "ok",
  "service": "xuannv-agent",
  "backend": "fastapi"
}
```

### `POST /api/report`

主接口。用于自然语言对话、补槽、报告生成。

请求体：

```json
{
  "session_id": "frontend-session-001",
  "task": "地物分类",
  "region": "雅江区域",
  "prompt": "给我一份去年九月份的地物分类报告",
  "time_range": ""
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `session_id` | 否 | 会话 ID。前端应为每个聊天窗口生成稳定 ID。默认 `default` |
| `task` | 否 | 前端选择的任务。可以为空；为空时 Agent 会从自然语言提取、继承上下文，或返回 `needs_input` |
| `region` | 否 | 前端选择的地区。默认 `雅江区域` |
| `prompt` | 是 | 用户自然语言输入 |
| `time_range` | 否 | `YYYY-MM`，前端已知月份时可直接传 |
| `selected_patch_ids` | 否 | 地图选择得到的 patch ID 列表，例如 `["patch_000020"]` |
| `aoi` | 否 | 地图框选范围，推荐 `{ "type": "bbox", "coordinates": [minLng, minLat, maxLng, maxLat] }` |

哈尔滨请求示例：

```json
{
  "session_id": "frontend-session-harbin-001",
  "task": "土地利用分类",
  "region": "哈尔滨新区",
  "prompt": "给我一份去年九月份哈尔滨新区土地利用分类报告",
  "time_range": ""
}
```

成功生成报告响应节选：

```json
{
  "status": "ok",
  "message": "报告已生成。",
  "session_id": "frontend-session-001",
  "request": {
    "task": "地物分类",
    "region": "雅江区域",
    "prompt": "给我一份去年九月份的地物分类报告",
    "time_range": "2025-09",
    "session_id": "frontend-session-001"
  },
  "report": {
    "title": "雅江区域2025-09地物分类遥感分析报告",
    "html_url": "/reports/2025-09-aef_inference-xxxx.html",
    "markdown_url": "/reports/2025-09-aef_inference-xxxx.md",
    "llm_provider": "template:missing_api_key",
    "reused": false
  },
  "analysis": {
    "data_source": "aef_inference",
    "metrics": [
      {"label": "任务", "value": "地物分类", "description": "Agent 识别后的标准任务"},
      {"label": "总体精度", "value": "98.3%", "description": "与 WorldCover 真值对比得到的平均精度"}
    ],
    "charts": [
      {
        "title": "地物分类真值与推理对比",
        "kind": "image",
        "url": "/reports/assets/aef_landcover_compare_xxxx.png",
        "caption": "展示地物分类真值、模型预测、正确/错误区域和置信度。"
      }
    ],
    "aef_payload": {
      "service": "http://127.0.0.1:7862",
      "task": "landcover",
      "region": "雅江区域",
      "time_range": "2025-09",
      "sample_indices": [40],
      "selector": "frontend_selected_patch",
      "selected_patch_ids": ["patch_000040"]
    }
  }
}
```

哈尔滨报告响应中的 `analysis.data_source` 为 `harbin_embedding_api`，`analysis.aef_payload` 会包含：

```json
{
  "service": "http://60.31.21.42:22065",
  "region_id": "harbin",
  "task": "land_use_classification",
  "version": "v2",
  "month": "2025-09",
  "patch": {"patch_id": "patch_000173"},
  "task_summary": {"total_patches": 424}
}
```

海淀报告响应中的 `analysis.data_source` 为 `haidian_embedding_api`，`analysis.aef_payload` 会包含：

```json
{
  "service": "http://60.31.21.42:22065",
  "region_id": "haidian",
  "task": "building_extraction",
  "version": "v1",
  "month": "202512",
  "patch": {"patch_id": "patch_000000"},
  "task_api_status": "available"
}
```

缺月份响应示例：

```json
{
  "status": "needs_input",
  "message": "请在需求里补充要分析的月份，例如：去年十月份、2025年9月。",
  "report": null
}
```

自然语言聊天响应示例：

```json
{
  "status": "chat",
  "message": "我是雅江遥感报告助手，主要帮你把自然语言需求整理成标准化遥感任务，调用 AEF 模型完成地物分类、水体分类或高程地形分析，然后生成带图表的报告。",
  "report": null
}
```

前端 URL 拼接规则：

```text
AGENT_BASE_URL = http://112.111.7.74:1112
absolute_html_url = AGENT_BASE_URL + response.report.html_url
absolute_image_url = AGENT_BASE_URL + response.analysis.charts[0].url
```

### `POST /api/patches/search`

地图选区到 patch 的检索接口。前端框选地图后，把 bbox 交给 Agent，由 Agent 代理查询区域 patch 服务并返回候选 patch。

`task` 和 `time_range` 都可以先传空字符串。这样前端可以支持“先框选地图定位 patch，再让用户选择任务/输入月份”的交互。若传入任务或月份，后端会尽量提前过滤出支持该任务/月份的 patch。

当前支持情况：

| 地区 | 支持情况 |
| --- | --- |
| `哈尔滨新区` | 支持 bbox 检索 patch |
| `北京市海淀区` | 支持 bbox 检索 patch，并可按任务和月份返回真实专题结果 |
| `雅江区域` | 支持 bbox 检索本地 patch，并将 `patch_000400` 映射为 AEF `sample_index=400` |

请求示例：

```json
{
  "region": "哈尔滨新区",
  "task": "",
  "time_range": "",
  "bbox": [126.5, 45.74, 126.57, 45.765],
  "limit": 10
}
```

海淀请求示例：

```json
{
  "region": "北京市海淀区",
  "task": "建筑物提取",
  "time_range": "2025-12",
  "bbox": [116.24, 39.88, 116.29, 39.90],
  "limit": 10
}
```

雅江请求示例：

```json
{
  "region": "雅江区域",
  "task": "地物分类",
  "time_range": "2025-09",
  "bbox": [95.03, 29.34, 95.08, 29.38],
  "limit": 10
}
```

响应示例：

```json
{
  "status": "ok",
  "region": "哈尔滨新区",
  "region_id": "harbin",
  "task": "land_use_classification",
  "time_range": "2025-09",
  "bbox": [126.5, 45.74, 126.57, 45.765],
  "selected_patch_ids": ["patch_000002"],
  "patches": [
    {
      "patch_id": "patch_000002",
      "bounds_wgs84": [126.549189, 45.744418, 126.565129, 45.75628],
      "available_months": ["2025-04", "2025-06", "2025-08", "2025-09", "2025-10", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05"],
      "available_tasks": ["land_use_classification", "building_extraction"],
      "score": 1.0
    }
  ]
}
```

生成报告时，前端应把用户选中的 patch 带到 `/api/report`：

```json
{
  "session_id": "frontend-session-001",
  "region": "哈尔滨新区",
  "task": "土地利用分类",
  "prompt": "给我一份去年九月份哈尔滨新区土地利用分类报告",
  "time_range": "",
  "selected_patch_ids": ["patch_000002"],
  "aoi": {
    "type": "bbox",
    "coordinates": [126.5, 45.74, 126.57, 45.765]
  }
}
```

### `GET /api/sessions?limit=30`

获取最近会话列表。

响应示例：

```json
{
  "status": "ok",
  "sessions": [
    {
      "session_id": "frontend-session-001",
      "title": "雅江区域 地物分类 2025-09",
      "summary": "最近一次报告任务：雅江区域，地物分类，2025-09。",
      "mode": "ok",
      "updated_at": "2026-06-26T06:30:00+00:00"
    }
  ]
}
```

### `GET /api/session/{session_id}`

获取单个会话详情、最近消息、记忆和最近报告。

响应示例结构：

```json
{
  "status": "ok",
  "session": {
    "session_id": "frontend-session-001",
    "messages": [],
    "reports": []
  },
  "memory": {
    "current_intent": {},
    "pending_slots": [],
    "recent_messages": [],
    "reports": []
  }
}
```

### `POST /api/session/reset`

清空指定会话的记忆、消息和报告索引。

请求体：

```json
{
  "session_id": "frontend-session-001"
}
```

响应：

```json
{
  "status": "ok",
  "session_id": "frontend-session-001"
}
```

### `GET /reports/{filename}`

静态报告和图片文件。

常见类型：

- `.html`: 完整 HTML 报告
- `.md`: Markdown 原文
- `.png`: 报告图像资产

## 前端最小接入流程

1. 前端启动时调用 `GET /api/health`。
2. 用户发送消息时调用 `POST /api/report`。
3. 把 `message` 渲染为助手回复。
4. 当 `status=ok && report` 时，渲染报告卡片。
5. 点击报告卡片时，在右侧面板加载 `AGENT_BASE_URL + report.html_url`。
6. Markdown 按钮加载 `AGENT_BASE_URL + report.markdown_url`。
7. 左侧历史会话调用 `GET /api/sessions` 和 `GET /api/session/{session_id}`。

## Curl 示例

```bash
BASE=http://112.111.7.74:1112

curl --noproxy '*' "$BASE/api/health"

curl --noproxy '*' -X POST "$BASE/api/report" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "demo-001",
    "task": "地物分类",
    "region": "雅江区域",
    "prompt": "给我一份去年九月份的地物分类报告"
  }'

curl --noproxy '*' -X POST "$BASE/api/report" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "demo-001",
    "task": "水体分布",
    "region": "雅江区域",
    "prompt": "给我一份2025年9月的水体分类报告"
  }'

curl --noproxy '*' -X POST "$BASE/api/report" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "demo-001",
    "task": "高程地形",
    "region": "雅江区域",
    "prompt": "生成一份2025年9月雅江区域高程地形分析报告"
  }'

curl --noproxy '*' -X POST "$BASE/api/report" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "demo-harbin-water",
    "task": "水体提取",
    "region": "哈尔滨新区",
    "prompt": "给我一份去年九月份哈尔滨新区水体提取报告"
  }'

curl --noproxy '*' -X POST "$BASE/api/report" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "demo-harbin-landuse",
    "task": "土地利用分类",
    "region": "哈尔滨新区",
    "prompt": "给我一份去年九月份哈尔滨新区土地利用分类报告"
  }'

curl --noproxy '*' -X POST "$BASE/api/report" \
  -H 'Content-Type: application/json' \
  -d '{
    "session_id": "demo-harbin-building",
    "task": "建筑物提取",
    "region": "哈尔滨新区",
    "prompt": "给我一份2025年10月哈尔滨新区建筑物提取报告"
  }'
```
