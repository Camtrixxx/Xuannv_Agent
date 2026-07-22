# Xuannv Agent — Codex 开发交接参考

> 面向在另一个 Codex/AI 环境里继续开发本项目的人。目标：读完这份就能上手，
> 少踩坑。写于 2026-07，对应提交 `145edc7` 之后。权威细节仍以 `CLAUDE.md` 为准，
> 本文补充「当前状态」「真实踩过的坑」「联调注意事项」这些 CLAUDE.md 里没有的。

---

## 0. 一句话概括

Xuannv Agent 是一个**遥感专题报告智能体服务**：接收自然语言请求（如「海淀区
2025年12月建筑物提取报告」），经 LangGraph 状态机解析意图 → 校验/补齐槽位 →
调用后端模型 API 出结果 → 生成结构化 HTML/Markdown 报告。

支持三个区域：**雅江 (Yajiang)**、**哈尔滨新区 (Harbin)**、**北京海淀 (Haidian)**。
最近的重点工作是**海淀的自定义地物标注与训练能力**（非原生地物的识别交接）。

---

## 1. 快速跑起来

```bash
# 依赖
pip install -r requirements.txt

# 启动 Agent（端口 7870）
python -m agent.backend.app --host 0.0.0.0 --port 7870
# 或用脚本（后台 nohup + 读取 .env）
bash scripts/start_agent_backend.sh

# 健康检查
curl --noproxy '*' -sS http://127.0.0.1:7870/api/health

# 前端 mock 页面
# 浏览器打开 http://127.0.0.1:7870/ui

# 测试（149 passed, 2 skipped —— 2 个 skip 是需 live API 的冒烟）
python -m pytest tests/ -q
```

**AEF 推理服务**（仅雅江需要，端口 7862）是独立的，海淀/哈尔滨用不到，联调海淀
时可以不启动它。

---

## 2. 架构与请求流

### 服务拓扑
```
前端 → Agent (:7870) → AEF 推理 (:7862, 仅雅江) + 哈尔滨/海淀 embedding API (远端)
              ↓
       agent/reports/*.html, *.md, assets/*.png
```
- **Agent 是唯一入口**，前端只调 Agent。
- 哈尔滨和海淀**共用同一个远端 embedding API**（`AGENT_EMBEDDING_API_BASE_URL`，
  默认 `http://60.31.21.42:22065`），只是走不同的 `/regions/{harbin|haidian}/...` 路径。

### 请求流（LangGraph 8 节点状态机，核心在 `agent/graph/report_agent.py`）
```
load_memory → parse_intent → merge_memory → route → run_analysis → generate_report → write_memory
```
- **parse_intent**：规则优先（`IntentService`，置信度 ≥0.6 就不调 LLM），DeepSeek 兜底。
  分类为 report_request / slot_fill / free_chat / change_context / confirmation / follow_up。
  同时确定 `scenario`（checkup / change / score，海淀三场景）。
- **merge_memory**：合并历史槽位；**上一轮的月份会被静默复用**（没有二次确认）。
  月份是唯一必填槽位，缺了才追问。
- **能力门控（capability gate，在 merge_memory 内、场景槽位逻辑之前）**：
  见第 4 节，这是最近的重点。
- **route**：分支到 ask_clarification / chat_response / run_analysis。
- **run_analysis**：按 scenario 分派到 RegionCheckupService / ChangeMonitorService /
  PressureScoreService（海淀三场景），否则走 RegionalAnalysisService（普通单任务报告）。

### 月份可用范围（硬约束，`agent/services/region_availability.py`）
- 雅江：2023-01 .. 2026-03（按季度）
- 哈尔滨：2025-04 / 06 / 08 / 09 / 10
- **海淀：2025-12 .. 2026-05** ← 联调海淀时月份必须落在这个区间，否则先被月份校验拦下

---

## 3. 关键服务（`agent/services/`）

所有服务都是依赖注入式（构造函数接收 config 和协作对象，默认用生产实现），便于测试。

| 服务 | 职责 |
|---|---|
| `IntentService` | 规则意图解析（中文关键词 + 月份推断 + 任务/区域别名归一化），置信度低时 DeepSeek 兜底 |
| `MemoryService` | SQLite 持久化（`agent/runtime/agent_memory.sqlite3`），三张表 sessions/messages/reports |
| `RegionalAnalysisService` | 路由器：按区域分派到 AEF(雅江)/Harbin/Haidian 三个适配器 |
| `HaidianEmbeddingAnalysisService` | 海淀普通任务：建筑/道路提取、施工、土地利用/覆盖分类、水体。用 patch 级结果 PNG 端点 |
| `RegionCheckupService` | **场景A 片区综合体检**：一个 AOI+月份，聚合 4 个二值任务覆盖 + 地物类别分布 |
| `ChangeMonitorService` | **场景B 建设扰动监测**：一个 AOI+两个月，逐 patch 像素级 diff → 新增/减少/净变化。自定义模型也走这里 |
| `PressureScoreService` | **场景C 高硬化低绿地压力评分**：按 patch 打分，排 TOP-N 补绿优先区 |
| `ModelRegistryService` | 只读封装 embedding-api `/models`(+`/{id}`,`/jobs/{id}`,`/capabilities`)，按区缓存。区分 system/custom，暴露 `find_custom_models` / `capabilities` / 富字段 ModelInfo |
| `CapabilityService` | **能力门控**：`resolve(region, object)` 判定 native/custom_ready/custom_training/custom_failed/needs_annotation，并构造标注交接 action |
| `PatchSelectionService` | 支撑 `POST /api/patches/search`，按 bbox 交集给 patch 打分排序 |
| `ReportService` | 生成业务向 HTML/Markdown 报告（模板 `agent-report-v5`），DeepSeek 组织语言，失败回退模板 |
| `DeepSeekProvider` | 裸 urllib 的 DeepSeek 客户端，无 SDK 依赖，失败返回 None（上游据此回退） |

### 纯函数工具（`agent/tools/`，无网络无配置，单测充分）
`raster`（二值覆盖+公顷）、`classmap`（多类分布）、`aoi`（跨 patch 聚合）、
`change`（两期掩膜 diff）、`scoring`（压力评分+排名）。
`AoiCoverService.fetch_result_array` / `iter_patch_colors` 是喂给它们的共享抓取循环。

---

## 4. 最近重点：自定义地物标注与训练能力（核心！）

这是本项目当前的主线工作，分 5 个阶段做完（提交 `5655fe0`..`4d57006`），
之后又对接了后端能力接口升级（`17b05b2`）和前端交接卡片（`1e83dc4`,`145edc7`）。

### 背景问题
海淀有 8 类**非原生地物**（湿地、路口、操场、机场、体育场、垃圾场、火车站、
露天停车场），系统内置模型不认识。用户如果问「分析湿地变化」，Agent 不能瞎给结果，
要引导用户先去标注页标注样本、训练一个自定义模型，训好再回来继续分析。

### 流程（Agent 只判定+交接+恢复，自己不训练、不弹页面）
```
用户提非原生任务
   → CapabilityService.resolve 判定为 needs_annotation
   → 返回 status=needs_annotation + action(open_annotation_ui 深链)
   → [前端标注页] 画样本 → POST /models 训练(异步)   ← 与 Agent 无关
   → 用户回来说"标注好了"
   → Agent 查模型【真实状态】(不信用户的话)：
       就绪 → 恢复原任务(自动带回之前的月份/AOI) → 出报告
       训练中 → "请稍候"
       失败 → 说明原因 + 再给标注入口
```

### 五种能力判定（`CapabilityService`）
| kind | 含义 | Agent 行为 |
|---|---|---|
| `native` | 内置任务/地物 | 直接分析 |
| `custom_ready` | 有就绪自定义模型 | 设 `custom_model_id`，走 ChangeMonitorService |
| `custom_training` | 模型训练中 | `needs_input`，让用户等 |
| `custom_failed` | 上次训练失败 | `needs_annotation`，说明失败原因 + 重给入口 |
| `needs_annotation` | 啥都没有 | `needs_annotation` + 标注深链 |

### 后端能力接口（同事已更新，Agent 已对接）
- `GET /models/capabilities?region_id=` → 4 种训练方式（xuannv_earth 默认 /
  traditional_ml / aef / dinov3_sat493m，海淀哈尔滨都可用）+ task_contracts（时相契约）。
- `POST /models`（统一训练入口，带 `training_method` 枚举）——**训练由前端标注页提交，
  Agent 不碰**。
- `GET /models/{id}` 富字段：resolved_training_method / feature_source / accuracy /
  n_samples，喂进报告的诚实话术。

### ⚠️ AEF 年度特征陷阱（重要，已在代码里处理）
AEF 训练的模型用**年度特征**——同一年内各月份共用同一个年度 embedding。所以
**用 AEF 模型做同年内两期变化检测是没意义的**（两期 embedding 相同 → 输出相同 →
任何"变化"都是伪的）。`ChangeMonitorService._limitations` 会检测
`feature_source=aef` + 同年，自动在限制说明顶部打 ⚠️ 警告，建议改用玄女地球/DINOv3
或跨年度对比。跨年度则不警告。

### few-shot 阈值 = 10（不是 5）
有效 Polygon < 10 用 PU+Query 相似度召回，≥10 用 Binary Conv 3x3。话术本身阈值无关。

---

## 5. 真实踩过的坑 / 已知问题（这份文档的价值所在）

这些是实际联调时遇到的，CLAUDE.md 里没有：

### 5.1 改完 Agent 代码必须重启后端才生效 ★最常见
后台服务是 nohup 常驻进程。改了 `.py` 不会热重载。联调时如果发现新逻辑「没生效」，
**先怀疑服务在跑旧代码**。曾经一个服务跑了一天多，是能力门控提交之前的旧代码，
导致「分析湿地」不走标注交接、反而在问月份。
```bash
bash scripts/stop_agent_backend.sh && bash scripts/start_agent_backend.sh
```

### 5.2 端口速查（别搞混）
| 端口 | 是什么 |
|---|---|
| `7870` | **Agent 服务**（本项目主服务），监听 0.0.0.0 |
| `7862` | AEF 推理服务（仅雅江） |
| 远端 `60.31.21.42:22065` | embedding API（模型/推理，哈尔滨+海淀共用） |
| 本地 `9061` | 同事起的**本地 embedding API 实例**（同一套接口，但见 5.3） |
| `7871`(本地) | VS Code SSH **端口转发**的本地端，隧道到远程 7870。不是服务器上的端口 |

> 本机 IP `10.119.16.35`。VS Code 转发时若本地 7870 被占，会自动挑 7871 做本地端。

### 5.3 本地 9061 的 infer 跑不通（后端数据问题，非 Agent 问题）
本地 9061 那个 embedding-api 实例，`infer` 目前失败：模型权重是 v2(128维)、
本地 patch embedding 是 v1(64维)，张量对不上。**远端 22065 同一条链路能跑通**。
联调时优先用远端 22065（Agent 默认就是它）。本地要用需后端统一 embedding 版本。

### 5.4 海淀自定义模型：数量多但 infer 不稳定，且 8 类非原生地物一个都没训
海淀有 523 个自定义模型，但几乎全是重复训练「建筑/道路/水体/变化」这些**原生**类；
8 类非原生地物（湿地/机场/…）命中 0 个。即便已有的就绪模型，infer 成功率也低
（扫了 199 个就绪单期模型的前 30 个都失败），原因同 5.3 的版本不一致。已验证
`model_6360bb31` 能跑通，可作为 live 测试的已知可用模型：
```bash
AGENT_LIVE_MODEL_ID=model_6360bb31 python -m pytest tests/test_custom_change.py -q
```

### 5.5 换检方式的产品决策（别改错方向）
非原生地物做变化检测：**沿用现有**——单期模型跑两个月各推理一次，Agent 自己做
像素级 diff。**没有**改成 change_detection 模型单次传 before/after 推理。这是
明确决策，不是遗漏。

### 5.6 训练法不暴露选择器
标注交接的 action 里带了 `training_method`，但**只用后端默认 xuannv_earth**，
不给用户/前端做选择。这是产品决策，action 里的字段仅信息性。

---

## 6. 对前端的 API 契约（`POST /api/report` 响应）

响应 `status` 驱动前端行为：
| status | 前端处理 |
|---|---|
| `ok` | 渲染报告卡片 |
| `needs_input` | 显示 message（缺月份/等训练），等用户补充 |
| `chat` | 普通对话气泡 |
| `needs_annotation` | **显示标注提示卡 + 跳转按钮**（见下） |

`needs_annotation` 时带 `action`：
```json
{
  "type": "open_annotation_ui",
  "url": "http://<embedding-api>/models/new?region_id=haidian&class=湿地&model_type=single_time_detection&training_method=xuannv_earth",
  "class_name": "湿地",
  "model_type": "single_time_detection",
  "training_method": "xuannv_earth",
  "task_contract": {"temporal_mode": "single", "required_fields": ["month"]},
  "params": { ... }
}
```
前端 mock（`agent/ui/agent_dashboard_mock.html`，服务在 `/ui`）已实现渲染：
`renderAnnotation()` 画一个琥珀色卡片 + 「前往标注 →」胶囊按钮，点击新标签打开 `action.url`。
详细契约见 `agent/API.md`。

---

## 7. 配置（`agent/config.py`，全环境变量驱动）

| 变量 | 默认 | 用途 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空 | 未设则回退规则解析 + 模板报告 |
| `AGENT_EMBEDDING_API_BASE_URL` | `http://60.31.21.42:22065` | 哈尔滨/海淀 embedding API |
| `AGENT_AEF_BASE_URL` | `http://127.0.0.1:7862` | 雅江 AEF 推理 |
| `AGENT_ANNOTATION_UI_BASE` | 同 embedding-api | 标注页深链基址 |
| `AGENT_PORT` | `7870` | Agent 端口 |
| `AGENT_MAX_REPORTS` | `50` | 报告文件保留上限 |

`scripts/start_agent_backend.sh` 启动前会 source 仓库根的 `.env`（gitignored），
密钥放那里不进 git。手动 uvicorn 时需自己设环境变量。

---

## 8. 开发约定 / 注意事项

- **`agent/reports/` 和 `agent/runtime/` 是 gitignored**，绝不提交（报告、SQLite、缓存）。
- **模型权重和训练代码不在本仓库**，AEF 从 `AEF_CODE_ROOT` 外部路径加载。
- **规则优先、LLM 兜底**：意图解析置信度 ≥0.6 不调 LLM。
- **所有 LLM 调用优雅降级**：key 没设或 API 失败都回退模板/规则。
- **Agent 无鉴权**，CORS 默认全开（`*`），定位是 EIP DNAT 后的内网服务。
- **测试**：149 passed, 2 skipped。新增功能要配套加测试（`tests/` 下按服务分文件）。
  跑测试用 `python -m pytest tests/ -q`（本机 Python 在
  `/home/heyuhang/miniconda3/envs/hyh-dl/bin/python`）。
- **提交规范**：commit message 用英文 `type(scope): summary` 格式，末尾带
  `Co-Authored-By`。文档/计划用中文。**只有用户明确要求才提交/推送**。

---

## 9. 相关文档索引（`docs/`）

- `基于 10m 遥感数据底座的场景能力边界与应用方向.md` — 能力边界分析
- `开发计划-海淀三场景与架构演进.md` — 场景 A/B/C 设计
- `开发计划-自定义地物标注与训练能力.md` — 自定义地物能力完整设计（含第 8 节后端接口对接）
- `执行计划-场景BC与收尾.md` / `执行进度日志-场景BC.md` — 场景 B/C 执行记录
- `agent/API.md` — 对前端的完整接口契约
- `CLAUDE.md`（仓库根）— 最权威的架构说明，本文是它的「当前状态 + 踩坑」补充

---

## 10. 当前 git 状态提示

写作时本地 main 领先 origin/main 若干提交（前端交接卡片相关尚未推送）。
接手后先 `git log --oneline -10` 和 `git status` 看清楚，别把未推送的工作弄丢。
