# Xuannv Agent 前端接入指南

这份文档给前端同事快速适配用。接口全集看 `/api-docs`，这里重点讲页面怎么接、地图怎么传、报告怎么展示。

## 联调入口

```text
Agent Base URL: http://112.111.7.74:1112
接口文档:       http://112.111.7.74:1112/api-docs
Swagger:        http://112.111.7.74:1112/docs
健康检查:       http://112.111.7.74:1112/api/health
```

前端只调用 Agent，不需要直接访问 AEF 模型服务，也不需要直接访问海淀/哈尔滨 embedding-api。

## 前端需要实现什么

| 模块 | 前端负责 | 后端负责 |
| --- | --- | --- |
| 对话 | 维护 `session_id`，发送用户输入，展示 `message` | 意图识别、补槽、多轮记忆、报告生成 |
| 任务/地区标签 | 让用户选择 `region`、`task`；`task` 可以为空 | 校验任务和地区，必要时返回 `needs_input` |
| 地图 | 展示地图、框选矩形、取得 bbox、展示候选 patch 边界 | 根据 bbox 检索 patch，并按任务/月过滤 |
| 报告 | 展示报告卡片、图片、右侧预览 HTML/Markdown | 生成 HTML、Markdown、PNG 资源 |
| 历史会话 | 调列表和详情接口，恢复消息与报告 | SQLite 保存会话、消息和报告索引 |

## 推荐页面状态

前端至少维护这些状态：

```ts
type AgentUiState = {
  sessionId: string;
  region: "雅江区域" | "哈尔滨新区" | "北京市海淀区";
  task: string; // 可以是空字符串
  selectedPatchIds: string[];
  selectedAoi: null | {
    type: "bbox";
    coordinates: [number, number, number, number];
  };
};
```

`sessionId` 建议在新建聊天时生成并持久化，例如 `session_${Date.now()}`。

## 地区和任务标签

```js
const REGION_TASKS = {
  "雅江区域": ["地物分类", "水体分布", "高程地形"],
  "哈尔滨新区": ["建筑物提取", "土地利用分类", "水体提取"],
  "北京市海淀区": ["建筑物提取", "道路提取", "施工识别", "土地利用分类", "土地覆盖分类", "水体提取"]
};
```

任务标签不要强制默认选中。用户可以先框选地图，之后再选择任务或直接在自然语言里说明任务。

## 地图框选到 patch

### 1. bbox 格式

前端从地图矩形中取 WGS84 经纬度范围：

```text
[minLng, minLat, maxLng, maxLat]
```

示例：

```json
[116.24, 39.88, 116.30, 39.93]
```

注意不是 `[lat, lng]`，也不是 GeoJSON polygon。当前推荐先传 bbox。

### 2. 搜索候选 patch

```http
POST /api/patches/search
Content-Type: application/json
```

```json
{
  "region": "北京市海淀区",
  "task": "",
  "time_range": "",
  "bbox": [116.24, 39.88, 116.30, 39.93],
  "limit": 12
}
```

`task` 和 `time_range` 可以为空，适合“先框选、后补任务/月”的交互。如果前端已经知道任务和月份，也可以传入，后端会提前过滤。

前端应先确认 bbox 是 `[minLng, minLat, maxLng, maxLat]` 且四个值为有限数字。响应 `status=invalid` 时提示用户重新框选；`status=retryable_error` 时提示稍后重试，不要把它当成报告生成失败。

### 3. 展示候选 patch

响应里的关键字段：

```json
{
  "status": "ok",
  "region_id": "haidian",
  "selected_patch_ids": ["patch_000020"],
  "patches": [
    {
      "patch_id": "patch_000020",
      "bounds_wgs84": [116.284627, 39.908468, 116.299484, 39.920091],
      "available_months": ["202512", "20251201", "202601"],
      "available_tasks": ["water_extraction", "land_use_classification", "road_extraction", "land_cover_classification", "building_extraction"],
      "score": 1.0
    }
  ],
  "message": "已定位到 1 个候选 patch。"
}
```

前端建议：

- 默认选中第一个 patch。
- 用 `bounds_wgs84` 在地图上画候选 patch 矩形。
- 用户点击候选项时切换该项的选中状态，保留完整 `selectedPatchIds` 数组。
- 默认最多选择 8 个 patch；达到上限时提示用户先取消一个或缩小框选范围。
- 可以展示 `score`、可用月份、可用任务，但不要求展示全部元数据。

## 生成报告

用户点击发送时调用：

```http
POST /api/report
Content-Type: application/json
```

基础请求：

```json
{
  "session_id": "session_20260702_001",
  "region": "北京市海淀区",
  "task": "建筑物提取",
  "prompt": "给我一份2025年12月建筑物提取报告",
  "selected_patch_ids": ["patch_000020", "patch_000021"],
  "aoi": {
    "type": "bbox",
    "coordinates": [116.24, 39.88, 116.30, 39.93]
  }
}
```

如果用户没有选 patch，也可以不传 `selected_patch_ids` 和 `aoi`。后端会根据地区、任务、月份自动选择可用 patch。

## 响应状态

`POST /api/report` 只需要按 `status` 分流：

| status | 前端处理 |
| --- | --- |
| `ok` | 展示助手消息和报告卡片 |
| `needs_input` | 展示补充提示，不展示报告 |
| `chat` | 展示自然语言回复，不展示报告 |

### ok 响应使用字段

```json
{
  "status": "ok",
  "message": "报告已生成。",
  "report": {
    "title": "北京市海淀区2025-12建筑物提取遥感分析报告",
    "abstract": "...",
    "html_url": "/reports/xxx.html",
    "markdown_url": "/reports/xxx.md",
    "metrics": [],
    "charts": []
  },
  "analysis": {
    "data_source": "haidian_embedding_api",
    "charts": [
      {
        "title": "建筑物提取专题结果",
        "url": "/reports/assets/xxx.png",
        "caption": "..."
      }
    ]
  }
}
```

URL 都是相对路径。前端拼成绝对地址：

```js
const BASE = "http://112.111.7.74:1112";
const htmlUrl = BASE + payload.report.html_url;
const mdUrl = BASE + payload.report.markdown_url;
const imageUrl = BASE + payload.analysis.charts[0].url;
```

报告预览推荐：

- HTML 报告：右侧 iframe 加载 `htmlUrl`。
- Markdown：可以 fetch `mdUrl` 后渲染预览，也可以提供“原文”切换。
- 图片：直接使用 `chart.url` 拼接绝对路径展示。

### needs_input 示例

```json
{
  "status": "needs_input",
  "message": "北京市海淀区可以分析：建筑物提取、道路提取、施工识别、土地利用分类、土地覆盖分类、水体提取。你想看哪一个呢？",
  "intent": {
    "missing_fields": ["task"]
  }
}
```

前端只需要把 `message` 当助手回复展示，等待用户继续输入。

### chat 示例

```json
{
  "status": "chat",
  "message": "我是玄女遥感报告助手，可以帮你把自然语言需求整理成遥感专题任务，并生成图文报告。"
}
```

## 历史会话

左侧历史会话：

```http
GET /api/sessions?limit=40
```

点击某个会话：

```http
GET /api/session/{session_id}
```

清空当前会话：

```http
POST /api/session/reset
Content-Type: application/json
```

```json
{
  "session_id": "session_20260702_001"
}
```

## 最小联调代码

```js
const BASE = "http://112.111.7.74:1112";

async function searchPatches({region, task, timeRange, bbox}) {
  const res = await fetch(`${BASE}/api/patches/search`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      region,
      task: task || "",
      time_range: timeRange || "",
      bbox,
      limit: 12
    })
  });
  const payload = await res.json();
  if (!res.ok || payload.status !== "ok") {
    throw new Error(payload.detail || payload.message || "patch 检索失败");
  }
  return payload;
}

async function sendReport({sessionId, region, task, prompt, selectedPatchIds, selectedAoi}) {
  const res = await fetch(`${BASE}/api/report`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      session_id: sessionId,
      region,
      task: task || "",
      prompt,
      selected_patch_ids: selectedPatchIds || [],
      aoi: selectedAoi || {}
    })
  });
  const payload = await res.json();
  if (!res.ok) {
    throw new Error(payload.detail || payload.message || "请求失败");
  }
  return payload;
}
```

## 推荐联调样例

海淀 patch 搜索：

```json
{
  "region": "北京市海淀区",
  "task": "",
  "time_range": "",
  "bbox": [116.24, 39.88, 116.30, 39.93],
  "limit": 3
}
```

哈尔滨 patch 搜索：

```json
{
  "region": "哈尔滨新区",
  "task": "",
  "time_range": "",
  "bbox": [126.50, 45.74, 126.57, 45.765],
  "limit": 3
}
```

雅江 patch 搜索：

```json
{
  "region": "雅江区域",
  "task": "",
  "time_range": "",
  "bbox": [95.039912, 29.341305, 95.563335, 29.861653],
  "limit": 3
}
```

报告生成：

```json
{
  "session_id": "demo-front-001",
  "region": "北京市海淀区",
  "task": "建筑物提取",
  "prompt": "给我一份2025年12月建筑物提取报告",
  "selected_patch_ids": ["patch_000020"],
  "aoi": {
    "type": "bbox",
    "coordinates": [116.24, 39.88, 116.30, 39.93]
  }
}
```

## 验收清单

- 能访问 `GET /api/health`。
- 地区切换后任务标签同步变化。
- 地图框选后能调用 `/api/patches/search`。
- 候选 patch 能在地图上画出边界。
- 点击候选 patch 可多选/取消；发送报告请求会携带完整的 `selected_patch_ids` 和 `aoi`。
- 多 patch 报告能展示合计指标、每个 patch 的处理状态和多个地图图层。
- `status=needs_input` 时不弹错误，只展示补充提示。
- `status=chat` 时只展示文本回复。
- `status=ok` 时展示报告卡片、图片、HTML 预览和 Markdown 入口。
- 历史会话列表可恢复上一轮消息和报告。

## 常见坑

- bbox 顺序必须是 `[minLng, minLat, maxLng, maxLat]`。
- `task` 可以为空，但生成报告前用户最终必须提供任务；可以通过标签或自然语言提供。
- `time_range` 可以不传，Agent 会从自然语言解析，例如 `去年九月份`。
- 报告、Markdown、图片 URL 都是相对路径，需要拼接 `Agent Base URL`。
- 前端不要直接调用 `127.0.0.1:7862` 或 `60.31.21.42:22065`，这些由 Agent 代理。
