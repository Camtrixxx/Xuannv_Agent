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
| `url` | 标注页深链，已带好 `region_id / class / model_type / training_method`（可选 `month`）。基址取环境变量 `AGENT_ANNOTATION_UI_BASE`（默认与 embedding-api 同址）。**注意 query 里的中文类名已做 URL 编码**（如 `class=%E6%B9%BF%E5%9C%B0`=湿地）；需要明文类名时用 `class_name` 或 `params.class`，不要从 `url` 里解 |
| `training_method` | 该区默认训练方式（`xuannv_earth`），信息性字段，来自 capabilities |
| `task_contract` | 该任务的时相契约：`temporal_mode`(single/pair) + `required_fields`，来自 capabilities |
| `class_name` | 待标注/训练的目标类名（=用户所说的非内置地物）|
| `model_type` | `single_time_detection`（单期）或 `change_detection`（换检）|
| `params` | 组成 `url` 的原始参数，前端也可用它自行拼链 |

**恢复流程**：用户标注并训练后回到同一会话说“标注好了 / 训练完了 / 好了”，Agent 会**立即绕过模型列表缓存，重新查询真实状态**（不轻信用户口头说法），并按 `region_id + class_name + model_type` 匹配最新可用模型：

- 已就绪：恢复原任务以及此前的月份/AOI/Patch；单期任务调用 `/models/{model_id}/infer_batch`（最多 100 个 Patch），将目标类别生成透明地图图层，再照常返回报告 `html_url` 与地图 `map_html_url`。
- 仍在训练：返回 `needs_input` 提示稍候，pending 任务继续保留。
- 训练失败：返回 `needs_annotation`，如实说明并再次给出标注入口。

前端不需要查询模型列表、保存 `model_id` 或调用推理接口，只需保持 `session_id` 不变并把用户的“训练完成”作为普通消息再次提交到 `/api/report`。当前自定义变化监测仍复用单期模型分别推理两个月份，由 Agent 计算新增/减少，因此交接的模型类型为 `single_time_detection`。

### 对话与报告的边界（重要）

Agent 会区分“生成/修改报告”“讨论报告”和“普通聊天”，同一句自然语言仍统一提交到 `/api/report`。

- **生成报告**：如“帮我分析雅江区域2025年9月的地物分类”“生成哈尔滨2025年10月建筑物提取报告”。
- **提问 / 闲聊 → `chat`**：如“海淀能分析什么”“这个准吗”“F1多少算好”“你好”“今天几号”。这些都不会生成报告。
- **报告问答 → `chat`**：如“为什么林地占比这么高”“总体精度怎么理解”，Agent 基于报告数据自然解释，不生成新报告。
- **报告编辑 → `ok + report`**：如“给我精简版”“把建议部分展开”“换成通俗说法”“补充风险分析”，Agent 不重新调用遥感模型，而是复用上一次结构化分析生成一个新的 HTML/Markdown/地图报告版本；新版本具有独立 URL，并进入历史报告列表。
- **更换任务 / 地区 / 月份 → `ok + report`**：如“换成水体分布”“换成哈尔滨新区”“换成2025年10月”，按新条件重新执行分析并生成默认完整版报告；能够继承的选区和槽位继续沿用，不兼容时会追问。
- `chat` 的 `message` 为**纯文本**，不含 Markdown 记号，前端可直接按文本渲染。

### 报告章节随任务/场景变化

报告正文的小节不是固定四段式：不同分析类型有各自的章节骨架，由任务族或场景决定。例如建筑物提取是「总体水平 / 高值区在哪里 / 能用来做什么」，片区综合体检是「一句话结论 / 四个专题横向对比 / 值得注意的信号」，建设扰动监测是「变了多少 / 变化集中在哪里 / 这些变化意味着什么」；所有骨架都以「数据说明与使用边界」结尾。

报告编辑（精简 / 扩充 / 通俗）沿用原报告的骨架，只改变篇幅与语气，不改变报告回答的问题。

这些小节只出现在 HTML / Markdown 报告正文内，**不属于接口字段**，前端无需按小标题做任何硬编码适配。

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
| `selected_patch_ids` | 海淀二选一 | 地图选择得到的 patch ID 列表，例如 `["patch_000020", "patch_000021"]`；与 `aoi` 至少提供一个 |
| `aoi` | 海淀二选一 | 地图框选范围，推荐 `{ "type": "bbox", "coordinates": [minLng, minLat, maxLng, maxLat] }`；与 `selected_patch_ids` 至少提供一个 |
| `before_time_range` / `after_time_range` | 否 | 仅建设扰动监测（场景 B）用的两期月份 `YYYY-MM`。前端也可让用户在 `prompt` 里写"2025-12 到 2026-05"由后端抽取 |

`custom_model_id` 是 Agent 内部恢复后写入的字段，前端不要传。`target_object` 通常也由 Agent 从话术或在线模型注册表识别；如果前端标注页支持完全自由的新类别，可在回到对话时显式传类别名作为兜底。模型匹配、批量推理、目标类别提取和报告/地图生成均由 Agent 完成。

海淀普通报告没有 `selected_patch_ids` 且没有有效 `aoi` 时返回 `needs_input`，提示用户先在地图上框选区域；
后端不会再从海淀全区自动抽取 Patch。已有海淀报告后的换任务或换月份会优先复用上一次成功选区。

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
- 场景 B/C 的地块级明细在 `analysis.aef_payload.top_patches`；结构与地图高亮方式见下文「场景 B/C 的地块级明细」。
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
    "map_html_url": "",
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

海淀报告存在可地理配准的结果图层时，`report.map_html_url` 会返回一个独立交互地图页面：

```json
"report": {
  "html_url": "/reports/haidian-water-xxxx.html",
  "markdown_url": "/reports/haidian-water-xxxx.md",
  "map_html_url": "/reports/haidian-water-xxxx.map.html"
}
```

前端可像报告页一样直接打开或放入 iframe。该页面已经包含高德卫星底图、WGS84→GCJ-02
坐标转换、结果图层开关、透明度控制和结果范围自动定位。URL 是相对路径，需拼接
`AGENT_BASE_URL`。

两个边界，前端按字段判断即可，不要写死：

- **只有海淀任务会生成这个页面。** 其他地区（雅江、哈尔滨新区）返回 `map_html_url: ""`，
  因为目前只有海淀链路产出带 `bounds_wgs84` 的地理配准结果。
- 海淀任务若本次没有可上图的结果图层，同样返回 `""`。

即：`map_html_url` 非空才显示"在地图中显示"入口，为空就隐藏。这个页面是静态产物，
**不接受任何 query 参数**（透明度、图层开关都是页面内部交互）。需要把结果叠进前端自有
地图、或想自己控制交互，走下面 `charts[]` 的 `overlay / bounds_wgs84 / patch_id` 字段。

说明：

- `analysis.charts[0]` 通常是**框选区域的高清卫星影像**（`卫星影像（框选区域）`），便于和模型专题结果并排对比；后面是模型结果图/叠加图。取图为尽力而为，失败时该图缺省，不影响报告生成。
- 地物分类等含类别分布的任务会返回 `analysis.data_table`（类别 + 占比），前端可渲染成分布表/占比条。
- `report` 卡片只需 `title`、`html_url`、`markdown_url` 即可展示；正文详情在 HTML 报告里。

### 地图图层与叠加显示（重点）

> **变更（v9 报告模板）**：叠图现在是**前端地图面板**的职责，不再内嵌在报告里。
> 生成的报告 HTML 已移除内置 Leaflet 地图，只保留文字、指标、结果图（静态）与数据表。
> 前端应把叠加图层渲染在自己的地图面板上（推荐做成右侧面板，与报告预览用一个按钮互相切换），
> 每次任务返回后用新响应重渲染，实现"结果实时上图"。**报告预览同样按响应实时更新**——
> 用新的 `report.html_url` / `markdown_url` 换掉上一次的报告，不必等用户再点卡片。
> 字段契约未变，下表照旧。

`analysis.charts[]` 里的每张图都是一个 `ChartAsset`。除了 `title / kind / url / caption`，图层相关的三个字段决定它在地图上如何显示：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `overlay` | bool | `true` = 这是一张**可叠加到地图的结果图层**（已地理配准）；`false` = 普通内嵌图，只在报告正文里平铺展示，不上地图 |
| `bounds_wgs84` | `[minLng, minLat, maxLng, maxLat]` | 该图层在地图上的地理范围（WGS84 经纬度）。**仅当 `overlay=true` 时非空**；用它把 `url` 图片作为 image overlay 铺到对应经纬度范围 |
| `patch_id` | string | 该图层覆盖的 patch。多 patch 拼接时是逗号分隔的 ID 串（如 `patch_000000,patch_000001`），单 patch 时是单个 ID，底图为空串 |

前端渲染规则：

- **底图**（`卫星影像（框选区域）`）：`overlay=false`、`bounds_wgs84=[]`。作为报告首图或地图底衬展示，不参与图层叠加。
- **结果图层**（`overlay=true`）：用 `bounds_wgs84` 作为 image overlay 的地理范围铺到地图上，`url` 为图片地址（拼 `AGENT_BASE_URL`）。图层可与底图叠加，支持透明度调节。
- **坐标系**：`bounds_wgs84` 是 **WGS84**。若底图用高德/腾讯瓦片（GCJ-02），必须先把边界做 WGS84→GCJ-02 转换再叠加，否则会有约 500m 偏移。用天地图/Esri 等 WGS84 底图则可直接叠。
- **图层替换而非累加**：新任务返回后应先清掉上一次的 overlay 再铺新层，否则多次任务的结果会互相压盖。
- **一图 or 多图**：多 patch 选区会优先在后端拼接成**一张无缝大图层**（标题形如 `建筑物提取专题结果（N patch 拼接）`，`bounds_wgs84` 为各 patch 的并集，`patch_id` 为逗号分隔全集）；当 patch 选区不连续、分辨率不一致或拼接失败时，回退为**逐 patch 多张图层**（每张各带自己的 `bounds_wgs84` 和单个 `patch_id`）。两种情况前端处理方式一致：遍历 `charts`，对 `overlay=true` 的逐张铺图即可，无需区分拼接与否。

不同任务/场景的图层类型：

| 报告类型 | `overlay=true` 的图层 | 说明 |
| --- | --- | --- |
| 普通专题报告（建筑/道路/施工/水体/地物） | 专题结果图层 | 二值/多类专题彩色结果，铺在选区上 |
| 场景 A 片区体检（`checkup`） | `土地覆盖分类（N patch 拼接）` | 片区土地覆盖彩色分类图层 |
| 场景 B 建设扰动监测（`change`） | `变化专题（N patch 拼接）` | 两期差分的变化图层（新增/减少着色） |
| 场景 C 补绿优先区评分（`score`） | `补绿压力热力图（N patch 拼接）` | 逐像素连续着色的压力热力图层（红=高压、黄=中压、绿=低压） |

示例（一张底图 + 一张拼接结果图层）：

```json
"charts": [
  {
    "title": "卫星影像（框选区域）",
    "kind": "image",
    "url": "/reports/assets/basemap_xxxx.png",
    "caption": "所选区域的高清卫星影像。",
    "overlay": false,
    "bounds_wgs84": [],
    "patch_id": ""
  },
  {
    "title": "建筑物提取专题结果（2 patch 拼接）",
    "kind": "image",
    "url": "/reports/assets/haidian_building_xxxx.png",
    "caption": "所选 patch 的建筑物提取彩色结果，可叠加到地图。",
    "overlay": true,
    "bounds_wgs84": [116.239959, 39.885118, 116.269775, 39.896843],
    "patch_id": "patch_000000,patch_000001"
  }
]
```

最小叠图逻辑（伪代码，每次 `/api/report` 返回后调用一次）：

```js
const BASE = "http://112.111.7.74:1112";

function updateMapPanel(payload) {
  const charts = payload.analysis?.charts || [];
  const layers = charts.filter(
    (c) => c.overlay && c.bounds_wgs84?.length === 4 && c.url
  );
  clearOverlays();                       // 先清旧层，避免多次任务叠在一起
  const corners = [];
  for (const c of layers) {
    const [minLng, minLat, maxLng, maxLat] = c.bounds_wgs84;
    // 高德/腾讯底图需先转 GCJ-02；WGS84 底图可直接用原值
    const sw = toBasemapCRS(minLng, minLat);
    const ne = toBasemapCRS(maxLng, maxLat);
    corners.push(sw, ne);
    addImageOverlay(BASE + c.url, [sw, ne], {
      opacity: 0.7,
      name: c.title || c.patch_id      // 供图层开关显示
    });
  }
  if (corners.length) fitBounds(corners);
  // 非叠加图（底图、图表）仍按普通图片展示
}
```

### 场景 B/C 的地块级明细

场景 B（`change`）与 C（`score`）在 `analysis.aef_payload.top_patches` 里给出逐 patch 明细，供前端做排行榜或地图高亮：

- **场景 C（`score`）** 每项：`{rank, patch_id, score, band(高压/中压/低压), impervious_ratio, green_ratio, bounds}`。注意这里的 `bounds` 是 **UTM 投影坐标**（米），**不是经纬度**；若要在经纬度地图上高亮该 patch，请优先使用对应结果图层的 `bounds_wgs84`，或用 `patch_id` 去 `/api/patches/search` 的结果里取 `bounds_wgs84`。
- **场景 B（`change`）** 每项：`{label(=patch_id), gained_ha, lost_ha, net_ha, ratio}`，按净变化排序；不含坐标，如需高亮同样用 `patch_id` 关联。

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

海淀报告响应中的 `analysis.data_source` 为 `haidian_embedding_api`。海淀普通专题报告支持**多 patch 合计**（选中 N 个 patch 时，指标为区域合计、图层为多 patch 拼接），`analysis.aef_payload` 会包含：

```json
{
  "service": "http://60.31.21.42:22065",
  "region_id": "haidian",
  "task": "building_extraction",
  "version": "v1",
  "month": "202512",
  "patch_count": 2,
  "requested_patch_ids": ["patch_000000", "patch_000001"],
  "used_patch_ids": ["patch_000000", "patch_000001"],
  "failed_patch_ids": [],
  "omitted_patch_ids": [],
  "selected_patch_ids": ["patch_000000", "patch_000001"],
  "patch_selection_source": "frontend_selected_patch",
  "task_api_status": "available"
}
```

- `used_patch_ids` 是**实际参与出图与指标汇总**的 patch；`failed_patch_ids` 为抓取失败被跳过的；`omitted_patch_ids` 为超出上限未处理的。前端可据此展示"命中 N 个 patch / M 个失败"。
- 单 patch 是 `patch_count=1` 的自然特例，字段结构一致。
- 后续轮次若只换任务/月份（"换成道路提取"），Agent 会沿用上一轮 `used_patch_ids` 整组 patch，无需前端重传。

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

> 注意：`messages` 和 `reports` 都在 **`memory`** 下，`session` 里没有这两个字段。

```json
{
  "status": "ok",
  "session": {
    "session_id": "frontend-session-001",
    "title": "北京市海淀区 水体提取 2025-12",
    "summary": "最近一次报告任务：北京市海淀区，水体提取，2025-12。",
    "mode": "ok",
    "turn_count": 4,
    "last_user_message": "给我一份2025年12月水体提取报告",
    "last_agent_message": "报告已生成。",
    "current_intent": {"task": "水体提取", "region": "北京市海淀区", "time_range": "2025-12"},
    "task": "水体提取",
    "region": "北京市海淀区",
    "time_range": "2025-12",
    "created_at": "2026-07-26T06:30:00+00:00",
    "updated_at": "2026-07-26T06:32:10+00:00"
  },
  "memory": {
    "current_intent": {},
    "pending_slots": [],
    "recent_messages": [{"role": "user", "content": "...", "created_at": "..."}],
    "reports": [
      {
        "title": "北京市海淀区水体提取报告",
        "html_url": "/reports/2025-12-haidian-xxxx.html",
        "markdown_url": "/reports/2025-12-haidian-xxxx.md",
        "map_html_url": "/reports/2025-12-haidian-xxxx.map.html",
        "request": {},
        "created_at": "2026-07-26T06:32:10+00:00"
      }
    ],
    "report_context": {},
    "pending_custom_model": {}
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
6. “在地图中显示”加载 `AGENT_BASE_URL + report.map_html_url`；字段为空时隐藏入口。
7. Markdown 按钮加载 `AGENT_BASE_URL + report.markdown_url`。
8. **两个视图都按响应实时更新**：每次 `status=ok` 后，报告视图换成新的 `html_url`，
   地图视图换成新的 `map_html_url`。当前显示的那个就地更新，另一个排队并在它的切换按钮上
   打提示点，用户切过去时套用。用户手动点卡片里的链接，优先级高于排队的结果。
9. 左侧历史会话调用 `GET /api/sessions` 和 `GET /api/session/{session_id}`；历史报告在
   `memory.reports[]` 里（不在 `session` 下），每条同样带 `map_html_url`，所以历史会话也能还原地图。

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
