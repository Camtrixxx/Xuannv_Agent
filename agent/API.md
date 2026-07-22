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

各地区当前可用月份。Agent 在调用模型**之前**会做事前校验：月份不在范围内时返回 `needs_input`（HTTP 200）并友好提示可用范围，**不会抛 400**。

| 地区 | 可用月份 |
| --- | --- |
| `雅江区域` | `2023-01` ～ `2026-03`（按季度更新，区间内任意月份可用） |
| `哈尔滨新区` | `2025-04`、`2025-06`、`2025-08`、`2025-09`、`2025-10`、`2026-01` ～ `2026-05` |
| `北京市海淀区` | `2025-12` ～ `2026-05` |

请求某地区不支持的任务时，Agent 同样返回 `needs_input`，并在 `message` 中列出该地区可选任务。

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
| `needs_input` | 需要用户补充信息：缺任务、缺月份，或月份不在可用范围；也用于自定义模型**训练中**时的等待提示 | 展示 `message`（会列出可选任务或可用月份），等待用户继续输入 |
| `needs_annotation` | 用户要分析的是**非内置地物**（如湿地、机场），且没有可用的自定义模型（从未标注，或上次训练失败）。需要先去标注页标注少量样本再训练 | 展示 `message`，并按 `action` 打开标注入口（见下）；用户完成后回到对话说“标注好了 / 训练完了”即可继续 |
| `chat` | 自然语言对话：闲聊、提问、就已有报告追问/解释/改写 | 只展示 `message`，不展示报告卡片 |

### `action` 交接指令（配合 `needs_annotation`）

当 `status = needs_annotation` 时，响应会带一个 `action` 对象，指示前端把用户交接到标注页。其他状态下 `action` 为 `{}`（空对象）。Agent **自己不打开任何页面**，只下达指令；打开方式（通常新标签页打开 `url`）由前端决定。

```json
{
  "status": "needs_annotation",
  "message": "『湿地』不是内置地物，需要先在标注页标注少量样本再训练……",
  "action": {
    "type": "open_annotation_ui",
    "url": "http://<embedding-api-base>/models/new?region_id=haidian&class=湿地&model_type=single_time_detection&training_method=xuannv_earth&month=202512",
    "class_name": "湿地",
    "model_type": "single_time_detection",
    "training_method": "xuannv_earth",
    "task_contract": { "temporal_mode": "single", "required_fields": ["month"] },
    "params": { "region_id": "haidian", "class": "湿地", "model_type": "single_time_detection", "training_method": "xuannv_earth", "month": "202512" }
  }
}
```

`training_method` 与 `task_contract` 来自后端 `GET /models/capabilities?region_id=`（Agent 每区缓存查询），取该区 `default_training_method`（当前为 `xuannv_earth`）和对应任务的时相契约；capabilities 不可达时回退到内置默认。**Agent 不暴露训练法选择器**，该字段仅为信息性——训练统一走后端默认方式。

字段说明：

| 字段 | 含义 |
| --- | --- |
| `type` | 指令类型，目前只有 `open_annotation_ui`。前端据此决定处理方式 |
| `url` | 标注页深链，已带好 `region_id / class / model_type / training_method`（可选 `month`）。基址取环境变量 `AGENT_ANNOTATION_UI_BASE`（默认与 embedding-api 同址）|
| `training_method` | 该区默认训练方式（`xuannv_earth`），信息性字段，来自 capabilities |
| `task_contract` | 该任务的时相契约：`temporal_mode`(single/pair) + `required_fields`，来自 capabilities |
| `class_name` | 待标注/训练的目标类名（=用户所说的非内置地物）|
| `model_type` | `single_time_detection`（单期）或 `change_detection`（换检）|
| `params` | 组成 `url` 的原始参数，前端也可用它自行拼链 |

**恢复流程**：用户标注并训练后回到对话说“标注好了 / 训练完了 / 好了”，Agent 会**重新查询该模型的真实状态**（不轻信用户口头说法）：已就绪则恢复原任务（沿用之前的月份/AOI）继续分析；仍在训练则提示稍候；若训练失败则如实说明并再次给出标注入口（`action` 复现）。调试阶段前端可用 mock 页面承接该 `url`。

### 对话与报告的边界（重要）

Agent 只有在用户**明确请求生成报告**时才会生成报告；提问和闲聊一律走 `chat`，即使句子里带了地区、任务、月份。

- **生成报告**：如“帮我分析雅江区域2025年9月的地物分类”“生成哈尔滨2025年10月建筑物提取报告”。
- **提问 / 闲聊 → `chat`**：如“海淀能分析什么”“这个准吗”“F1多少算好”“你好”“今天几号”。这些都不会生成报告。
- **就已有报告追问 / 改写 → `chat`（不重新生成）**：报告生成后，用户可以问“为什么林地占比这么高”“总体精度怎么理解”，或说“给我精简版”“把建议部分展开”“换个通俗说法”，Agent 基于该报告内容作答，不会重复出报告。
- **只改任务 / 月份**：如“换成水体分布”“换成2025年10月”，Agent 保持同一 patch（地图选区），只重算对应任务/月份；若该 patch 不支持新任务，会自动换到可用 patch 并照常出报告。
- `chat` 的 `message` 为**纯文本**，不含 Markdown 记号，前端可直接按文本渲染。

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
| `selected_patch_ids` | 否 | 地图选择得到的 patch ID 列表，例如 `["patch_000020", "patch_000021"]`；海淀普通报告默认最多处理 8 个 |
| `aoi` | 否 | 地图框选范围，推荐 `{ "type": "bbox", "coordinates": [minLng, minLat, maxLng, maxLat] }` |
| `before_time_range` / `after_time_range` | 否 | 仅建设扰动监测（场景 B）用的两期月份 `YYYY-MM`。前端也可让用户在 `prompt` 里写"2025-12 到 2026-05"由后端抽取 |

### 海淀复合场景（框选 AOI + 一句话触发）

海淀区支持三个复合分析场景，均由 `prompt` 关键词确定性识别，输入走同一个 `/api/report`。除普通专题报告外，`intent.scenario` 会标记为下列之一：

| 场景 | `scenario` | 触发问法（示例） | 必需输入 | 产出 |
| --- | --- | --- | --- | --- |
| A 片区综合体检 | `checkup` | "帮我做个片区综合体检" / "整体评估一下这块区域" | 月份 + 框选 AOI | 片区面积 + 四专题覆盖率 + 土地覆盖各类别占比 |
| B 建设扰动监测 | `change` | "对比 2025-12 和 2026-05 的变化" / "监测建设扰动" / "前后对比建筑扩张" | 两个月份 + 框选 AOI（缺省 task=施工） | 净变化/新增/减少面积 + 增长率 + 逐 patch 净变化 TOP |
| C 补绿优先区评分 | `score` | "这片区哪里最该补绿" / "高硬化低绿地压力评分" / "哪些地方最缺绿" | 月份 + 框选 AOI | 片区平均压力分 + 高压 patch 数 + 补绿优先区 TOP-N（含依据） |

说明：
- 缺月份或未框选 AOI 时返回 `needs_input`，`message` 会分别友好追问；多轮补齐后场景保持不变（sticky）。
- 纯问句（"这个对比准不准"、"哪里缺绿吗"）按既有原则走讨论，不触发报告。
- 场景 B/C 的地块级明细在 `report.aef_payload.top_patches`（含 `bounds`，供前端在地图上高亮）。
- v1 仅海淀开放；绿地率来自土地覆盖模型，报告 `limitations` 中已标注其精度局限。

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
    "llm_provider": "deepseek",
    "reused": false
  },
  "analysis": {
    "data_source": "aef_inference",
    "metrics": [
      {"label": "任务", "value": "地物分类", "description": "Agent 识别后的标准任务"},
      {"label": "总体精度", "value": "98.3%", "description": "与 WorldCover 真值对比得到的平均精度"}
    ],
    "data_table": [
      {"label": "林地", "ratio": 0.809, "value": 3312},
      {"label": "永久水体", "ratio": 0.191, "value": 782}
    ],
    "data_table_title": "地物类型分布",
    "charts": [
      {
        "title": "卫星影像（框选区域）",
        "kind": "image",
        "url": "/reports/assets/basemap_xxxx.png",
        "caption": "所选区域的高清卫星影像，可与下方模型专题结果直接对比。"
      },
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

说明：

- `analysis.charts[0]` 通常是**框选区域的高清卫星影像**（`卫星影像（框选区域）`），便于和模型专题结果并排对比；后面是模型结果图/叠加图。取图为尽力而为，失败时该图缺省，不影响报告生成。
- 地物分类等含类别分布的任务会返回 `analysis.data_table`（类别 + 占比），前端可渲染成分布表/占比条。
- `report` 卡片只需 `title`、`html_url`、`markdown_url` 即可展示；正文详情在 HTML 报告里。

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

缺信息响应示例（缺任务/月份时会列出该地区可选任务与月份范围）：

```json
{
  "status": "needs_input",
  "message": "好的，帮你生成报告～ 哈尔滨新区可以分析：建筑物提取、土地利用分类、水体提取。你想看哪一个、哪个月份呢？（哈尔滨新区目前可分析 2025 年 4、6、8、9、10 月，以及 2026 年 1 至 5 月）",
  "report": null
}
```

月份不可用响应示例（事前校验，HTTP 200）：

```json
{
  "status": "needs_input",
  "message": "雅江区域目前可分析 2023 年 1 月至 2026 年 3 月（按季度更新）。你说的 2026年6月 暂时没有可用数据，换一个试试，比如 2026年3月。",
  "report": null
}
```

自然语言聊天 / 追问响应示例：

```json
{
  "status": "chat",
  "message": "总体精度是衡量整个分类结果正确程度的核心指标：正确分类的像素占总像素的比例。这份报告里总体精度约 98%，说明模型识别地物的整体能力较好。",
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

检索失败时也会返回 JSON 状态，便于前端给出可操作提示：`invalid` 表示 bbox 缺失、坐标无效或框选退化，应重新框选；`retryable_error` 表示上游 patch 服务在自动重试后仍暂时不可用，应稍后重试。两种状态均不会把用户可恢复的问题伪装成 HTTP 500。

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
- `.png`: 报告图像资产（含卫星影像、专题结果图、embedding 预览图）

### 页面（HTML，便于查看/联调）

| 路径 | 说明 |
| --- | --- |
| `GET /` , `GET /ui` | 单屏聊天原型页 |
| `GET /workflow` | 工作原理 / 使用说明页 |
| `GET /api-docs` , `GET /api-docs.md` | 本接口文档（HTML / Markdown） |
| `GET /frontend-guide` , `GET /frontend-guide.md` | 前端接入指南（HTML / Markdown） |
| `GET /docs` | FastAPI 自动生成的 Swagger |

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
