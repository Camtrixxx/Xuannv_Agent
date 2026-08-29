# Xuannv Agent 前端接入指南

这份文档给前端同事快速适配用。接口全集看 `/api-docs`，这里重点讲页面怎么接、地图怎么传、报告怎么展示。

## 联调入口

```text
Agent Base URL: http://60.31.21.42:22070
接口文档:       http://60.31.21.42:22070/api-docs
Swagger:        http://60.31.21.42:22070/docs
健康检查:       http://60.31.21.42:22070/api/health
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

海淀报告必须传 `selected_patch_ids`，或传有效的 bbox `aoi` 让后端在框选范围内检索 Patch。
两者都不传时，接口返回 `status=needs_input` 并提示用户框选区域，不再从海淀全区自动选择 Patch。

## 响应状态

`POST /api/report` 只需要按 `status` 分流：

| status | 前端处理 |
| --- | --- |
| `ok` | 展示助手消息和报告卡片 |
| `needs_input` | 展示补充提示，不展示报告 |
| `needs_annotation` | 展示助手消息，并按 `action` 引导用户跳转标注页（见「非原生任务：自定义标注跳转」） |
| `chat` | 展示自然语言回复，不展示报告 |

报告生成后的多轮对话无需增加新接口：

- “为什么这个占比高 / 这个结论怎么理解”返回 `status=chat`，只更新聊天消息。
- “给我精简版 / 扩写建议部分 / 改成通俗版本”返回 `status=ok` 和一份新的 `report`；后端复用原分析数据，不重复调用遥感模型。前端按普通成功报告处理，立即替换右侧报告 URL，同时把新版本加入历史报告。
- “换成道路提取 / 换成哈尔滨新区 / 换成2026年3月”重新执行分析，返回新的默认完整版报告。

报告编辑版本的 `html_url`、`markdown_url`、`map_html_url` 都是新地址，不要继续使用上一版 iframe URL。

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
    "map_html_url": "/reports/xxx.map.html",
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
const BASE = "http://60.31.21.42:22070";
const htmlUrl = BASE + payload.report.html_url;
const mdUrl = BASE + payload.report.markdown_url;
const mapHtmlUrl = payload.report.map_html_url
  ? BASE + payload.report.map_html_url
  : "";
const imageUrl = BASE + payload.analysis.charts[0].url;
```

报告预览推荐：

- HTML 报告：右侧 iframe 加载 `htmlUrl`。
- 独立地图：海淀报告可直接用 iframe 加载 `mapHtmlUrl`；为空时隐藏地图入口。
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

## 非原生任务：自定义标注跳转（重点）

系统内置的任务（建筑物/道路/施工/水体/土地覆盖/土地利用/地物分类等）可以直接出报告。但当用户要分析的是**非内置地物**——例如「湿地」「机场」「河流」「操场」这类——系统没有现成模型，需要用户先去标注页标注少量样本、训练一个自定义模型，之后才能分析。

这条链路由 `POST /api/report` 的 `status=needs_annotation` + `action` 字段驱动。**Agent 自己不打开任何页面**，只下达跳转指令，由前端决定如何打开（通常新标签页打开 `action.url`）。

### 触发与响应

用户说「帮我看看这块地的湿地分布」时，若没有可用的湿地模型，响应形如：

```json
{
  "status": "needs_annotation",
  "message": "『湿地』不是内置地物，需要先在标注页标注少量样本再训练，完成后回来告诉我就行。",
  "action": {
    "type": "open_annotation_ui",
    "url": "http://<annotation-ui-base>/models/new?region_id=haidian&class=湿地&model_type=single_time_detection&training_method=xuannv_earth&month=202512",
    "class_name": "湿地",
    "model_type": "single_time_detection",
    "training_method": "xuannv_earth",
    "task_contract": { "temporal_mode": "single", "required_fields": ["month"] },
    "params": { "region_id": "haidian", "class": "湿地", "model_type": "single_time_detection", "training_method": "xuannv_earth", "month": "202512" }
  }
}
```

### 前端处理

1. 展示 `message`（助手气泡）。
2. 渲染一个显眼的引导按钮，如「去标注页训练『湿地』模型」，点击后新标签页打开 `action.url`（`url` 已带好 `region_id / class / model_type / training_method`，可选 `month`）。也可以用 `action.params` 自行拼链。
3. `action.model_type` 区分单期检测（`single_time_detection`）和变化检测（`change_detection`）；`task_contract.temporal_mode` 为 `single` 需 1 个月份，`pair` 需两期月份。这些是信息性字段，训练方式统一走后端默认（`training_method`），**前端不需要暴露训练法选择器**。

### 恢复流程

用户完成标注/训练后回到**同一会话**说「标注好了 / 训练完了 / 好了」，再次调 `/api/report`（带同一 `session_id`）即可。Agent 会**绕过缓存重新核验该模型的真实状态**，不轻信口头说法，也不要求前端提供 `model_id`：

- 模型已就绪 → 按区域、类别和模型类型找到最新可用模型，恢复原任务（沿用之前的月份/AOI/Patch），批量推理并返回 `status=ok` + 报告；海淀结果同时带 `report.map_html_url`。
- 仍在训练 → 返回 `status=needs_input`，提示稍候。
- 训练失败 → 返回 `status=needs_annotation`，如实说明并再次给出 `action`（可重试标注）。

前端无需自己记忆"待标注"状态——只要保持 `session_id` 不变，Agent 会跟踪 pending 模型并在下一轮自动接续。联调阶段可用 mock 页面承接 `action.url`。

```js
// needs_annotation 分流
if (payload.status === "needs_annotation") {
  showAssistantMessage(payload.message);
  const a = payload.action; // {type:"open_annotation_ui", url, class_name, ...}
  showActionButton(`去标注页训练『${a.class_name}』模型`, () => window.open(a.url, "_blank"));
  // 用户回来后，正常发"标注好了"到同一 session 即可恢复
}
```

## 地图页面与图层叠加（重点）

海淀报告成功时优先使用 `report.map_html_url`：这是后端生成的完整交互地图页面，前端只需
拼接 `BASE` 后放进 iframe 或新标签页，无需自行处理底图、坐标转换和 image overlay。
历史会话的 `memory.reports[]` 也保存该字段。

如果正式前端需要把分析结果集成进自己的地图组件，再使用下述
`analysis.charts[].overlay + bounds_wgs84` 图层协议。两种接法同时保留。

> 报告正文 HTML 不内嵌地图，只保留文字、指标、结果图和数据表。独立地图使用
> `report.map_html_url`；需要集成进前端自有地图时，`overlay / bounds_wgs84 / patch_id`
> 图层协议照旧。推荐接法见下面「右侧双视图：报告 ⇄ 地图」。

`analysis.charts[]` 里每张图是一个 `ChartAsset`。除 `title / kind / url / caption` 外，三个字段决定它在地图上如何显示：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `overlay` | bool | `true`=可叠加到地图的**结果图层**（已地理配准）；`false`=普通内嵌图，只在报告正文里展示 |
| `bounds_wgs84` | `[minLng,minLat,maxLng,maxLat]` | 图层的地理范围（WGS84）。仅当 `overlay=true` 时非空 |
| `patch_id` | string | 图层覆盖的 patch；多 patch 拼接时为逗号分隔 ID 串，底图为空串 |

### 前端渲染规则

- **底图**（`卫星影像（框选区域）`）：`overlay=false`、`bounds_wgs84=[]`。作报告首图/地图底衬，不参与叠加。
- **结果图层**（`overlay=true`）：用 `bounds_wgs84` 作为 image overlay 的地理范围，把 `url` 图片铺到地图上，可叠在底图之上并支持透明度。
- **不必区分"一张拼接图"还是"多张逐 patch 图"**：后端能拼就拼成一张大图层（`bounds_wgs84` 为并集），拼不了就回退多张（各带自己的 `bounds_wgs84`）。前端只需遍历 `charts`、对 `overlay=true` 的逐张铺图。
- **坐标系必须对齐**：`bounds_wgs84` 是 WGS84。底图若用高德/腾讯瓦片（GCJ-02），要先把边界做 WGS84→GCJ-02 转换，否则整层偏移约 500m。用天地图/Esri 等 WGS84 底图可直接叠。
- **每次任务先清旧层**：图层是"替换"语义，不是"累加"。新任务的结果铺上去之前先移除上一次的 overlay。
- **懒初始化地图**：地图容器隐藏时初始化会拿到 0 尺寸、渲染成白块。首次切到地图视图时再建实例，切回来时调一次 `invalidateSize()`。

### 右侧双视图：报告 ⇄ 地图

推荐的页面结构（`agent/ui/agent_dashboard_mock.html` 已按此实现，可直接参考）：

- 右侧面板一个容器、两个视图：`报告` 和 `地图`，用顶部分段按钮互相切换（同一块区域切换，不并排）。
- 聊天里的报告卡片给两个入口：`打开完整 HTML 报告`（进报告视图）和 `在地图上查看结果`（进地图视图）。
- 地图视图底部放图层控制：透明度滑块、逐图层开关、底图切换、`回到结果范围`。
- **实时更新（两个视图都要）**：每次 `/api/report` 返回 `status=ok` 后，地图和报告都换成本次任务的结果，不需要用户再点一次卡片。
  - 地图：调 `updateMapPanel(payload)`，正在显示就原地换层并重新 `fitBounds`。
  - 报告：用 `report.html_url` / `report.markdown_url` 重载右侧报告视图；用户之前在看 Markdown 就仍给 Markdown，避免视图形态被切走。
  - 当前显示的那个视图就地更新；另一个视图把新结果**排队**，并在它的分段按钮上打一个小圆点，用户切过去时再套用。用户手动点卡片里的报告链接，优先级高于排队的结果。
  - 不需要 WebSocket，按响应重渲染即可。

### 各任务/场景的图层

| 报告类型 | `overlay=true` 图层 | 图层含义 |
| --- | --- | --- |
| 普通专题（建筑/道路/施工/水体/地物/土地覆盖/利用） | 专题结果图层 | 二值/多类彩色结果铺在选区上 |
| 场景 A 片区体检（`checkup`） | `土地覆盖分类（N patch 拼接）` | 土地覆盖彩色分类图层 |
| 场景 B 建设扰动监测（`change`） | `变化专题（N patch 拼接）` | 两期差分变化图层（新增/减少着色） |
| 场景 C 补绿优先区评分（`score`） | `补绿压力热力图（N patch 拼接）` | 逐像素连续着色热力图层（红=高压、黄=中压、绿=低压） |

### 最小叠图代码

```js
const BASE = "http://60.31.21.42:22070";
let overlays = [];

// 每次 /api/report 返回 status=ok 后调用一次
function updateMapPanel(payload, map) {
  const charts = (payload.analysis || {}).charts || [];
  const layers = charts.filter(
    (c) => c.overlay && Array.isArray(c.bounds_wgs84) && c.bounds_wgs84.length === 4 && c.url
  );

  overlays.forEach((o) => map.removeLayer(o));   // 图层是替换语义
  overlays = [];

  const corners = [];
  for (const c of layers) {
    const [minLng, minLat, maxLng, maxLat] = c.bounds_wgs84;
    // 高德/腾讯底图先转 GCJ-02；WGS84 底图直接用原值
    const sw = toBasemapCRS(minLng, minLat);
    const ne = toBasemapCRS(maxLng, maxLat);
    // 多数地图库用 [lat, lng] 顺序，注意翻转
    corners.push([sw[1], sw[0]], [ne[1], ne[0]]);
    overlays.push(
      map.addImageOverlay(BASE + c.url, [[sw[1], sw[0]], [ne[1], ne[0]]], {
        title: c.title || c.patch_id,
        opacity: 0.7
      })
    );
  }
  if (corners.length) map.fitBounds(corners);
}
```

### 场景 B/C 地块级明细

`analysis.aef_payload.top_patches` 给出逐 patch 排行（补绿优先区 / 净变化最大的地块）：

- **场景 C（`score`）**：`{rank, patch_id, score, band, impervious_ratio, green_ratio, bounds}`。这里的 `bounds` 是 **UTM 投影坐标（米），不是经纬度**；地图高亮请优先用对应结果图层的 `bounds_wgs84`，或用 `patch_id` 去 `/api/patches/search` 结果里取 `bounds_wgs84`。
- **场景 B（`change`）**：`{label(=patch_id), gained_ha, lost_ha, net_ha, ratio}`，不含坐标，用 `patch_id` 关联高亮。

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
const BASE = "http://60.31.21.42:22070";

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
- `status=needs_annotation` 时展示引导按钮，点击能用 `action.url` 打开标注页；用户回来说"标注好了"能在同一会话恢复。
- 结果图层：`overlay=true` 的图能在**地图面板**上用 `bounds_wgs84` 叠出来（与底图对齐、无偏移），`overlay=false` 的底图/图表正常内嵌在报告里。
- 右侧面板能在`报告`与`地图`之间来回切换，切换后地图不出白块。
- 连续跑两个任务，地图面板显示的是**最新**任务的图层（旧层已清除），报告视图也自动换成最新那份报告。
- 看着地图时来了新结果，`报告`按钮出现小圆点；切回`报告`即显示新报告。
- `status=chat` 时只展示文本回复。
- `status=ok` 时展示报告卡片、图片、HTML 预览和 Markdown 入口。
- 历史会话列表可恢复上一轮消息和报告。

## 常见坑

- bbox 顺序必须是 `[minLng, minLat, maxLng, maxLat]`。
- `task` 可以为空，但生成报告前用户最终必须提供任务；可以通过标签或自然语言提供。
- `time_range` 可以不传，Agent 会从自然语言解析，例如 `去年九月份`。
- 报告、Markdown、图片 URL 都是相对路径，需要拼接 `Agent Base URL`。
- 前端不要直接调用 `127.0.0.1:7862` 或 `192.168.108.218:9065`，这些由 Agent 代理。
