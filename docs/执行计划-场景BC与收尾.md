# 执行计划：场景 B / C 与收尾（自主执行）

> 编制 2026-07｜执行者：Agent（夜间自主执行）｜关联：[开发计划-海淀三场景与架构演进](./开发计划-海淀三场景与架构演进.md)、[执行进度日志](./执行进度日志-场景BC.md)
>
> **背景**：场景 A（片区综合体检）已端到端完成并通过 93 项 pytest。本计划完成剩余的场景 B（建设扰动短周期监测）、场景 C（高硬化低绿地压力评分 / 补绿优先区），以及文档收尾。

## 执行红线（全程遵守）

1. **每一步保持测试全绿**：每完成一个工具/服务/路由改动即跑 `pytest`，红了立刻修，不带病推进。
2. **纯函数工具先行 + 单测**：算法逻辑落在 `agent/tools/`，纯函数，先写实现再写测试。
3. **复用不重写**：复用 `raster` / `aoi` / `classmap` / `AoiCoverService.iter_patch_colors` / `ReportService`；新场景 = 新 service + 意图路由，不改渲染层。
4. **确定性意图识别**：场景触发用规则关键词，不依赖 LLM 可达。
5. **海淀 only（v1）**：三场景都只对北京市海淀区开放。
6. **兼容优先**：`/api/report` 行为不破坏，普通报告与场景 A 回归不受影响。
7. **模型准确性归上游**：忠实呈现模型输出，必要处加 limitation 说明，不替模型背书。
8. **进度可追溯**：每个阶段结束在 `docs/执行进度日志-场景BC.md` 追加一条（时间、做了什么、测试数、遗留）。

## 数据事实（已实测）

- 6 个月全可用：202512 / 202601 / 202602 / 202603 / 202604 / 202605。
- 五个任务在各月均有结果，PNG 128×128、**逐像素对齐**（可做像素级变化检测）。
- 二值前景/背景映射见 `BINARY_TASK_BACKGROUND`；土地覆盖图例见 `classmap` + `/system-models/{task}/classes`。
- 土地覆盖模型精度不可靠（山地误判为水体）——场景 C 的"绿地/硬化"以**二值专题**为准，土地覆盖仅作参考并标注。

---

## 阶段五：场景 B —— 建设扰动短周期监测

**目标**：同一 AOI、两个月份，对某个二值专题（默认施工/建筑）做两期对比，产出：覆盖率变化、新增/减少面积、逐 patch 变化清单、变化叙事。

### 交付物
- `agent/tools/change.py`（纯函数）
  - `binary_change(mask_a_counts_or_array, mask_b, ...)`：两期前景的新增/减少/保持像素与面积（公顷）。基于逐像素对齐的两张 PNG。
  - `aggregate_change(per_patch)`：多 patch 变化聚合（新增 ha、减少 ha、净变化、变化率）。
- `agent/services/change_monitor_service.py`：`ChangeMonitorService.analyze(request)`
  - 入参：AOI + task + 两个月（before/after）。缺省 task=施工地检测。
  - 逐 patch 拉两期结果 → 像素级 diff → 聚合 → 一份 `AnalysisResult`（metrics：净变化/新增/减少；data_table：逐 patch 变化；findings + 变化叙事）。
- 意图与路由
  - `AgentIntent` 复用 `scenario` 字段，新增值 `"change"`。
  - `intent_service`：识别"变化/新增/扩张/对比/两期/监测/扰动/什么变了"等 + 需要两个月（`before_month`/`after_month`）。
  - `report_agent`：新增 `_merge_change`（要 task + 两个月 + AOI），`_run_analysis` 路由到 `ChangeMonitorService`。
  - 月份解析：扩展意图，支持"从 X 到 Y""X 和 Y 对比"两个月份抽取（新增 schema 字段 `before_time_range`/`after_time_range` 或在 change 分支内解析 prompt）。
- 测试：`tests/test_change_tools.py`（纯函数）+ `tests/test_change_monitor.py`（service，stub HTTP）+ 意图批测。

### 验收
- pytest 全绿；两期对比问法 ≥8 条正确路由到 change；
- 直连 service 端到端产出变化报告（净变化 ha 合理、逐 patch 清单非空）；
- live HTTP 出报告并渲染。

---

## 阶段六：场景 C —— 高硬化低绿地压力评分 / 补绿优先区

**目标**：同一 AOI 内，对每个 patch 计算"建设压力分"（硬化高、绿地低 → 分高），排序输出 TOP-N 补绿优先区，每项带依据。

### 交付物
- `agent/tools/scoring.py`（纯函数）
  - `pressure_score(building_ratio, green_ratio, ...)`：归一化加权分（默认：硬化正权、绿地负权），返回 0–100 分与分档。
  - `rank_patches(rows, top_n)`：按分排序取 TOP-N，附排名与依据字段。
  - 权重、方向、分档阈值集中为常量，注释说明依据。
- `agent/services/pressure_score_service.py`：`PressureScoreService.analyze(request)`
  - 逐 patch：建筑物覆盖率（硬化代理，可靠）+ 绿地率（土地覆盖树木+灌木+草地之和，标注为参考）。
  - 计算压力分 → 排序 → 一份 `AnalysisResult`（metrics：片区均分/高压 patch 数；data_table：TOP-N 优先区含分数与依据；findings + 决策建议）。
  - patch 需可定位（bounds / patch_id）以便前端在地图上高亮——charts 复用 basemap，TOP-N 的 bounds 放入 `aef_payload` 供前端标注。
- 意图与路由
  - `scenario="score"`；识别"压力/硬化/绿地率/补绿/优先/哪里该绿化/最需要/排序/最缺绿"等 + 需要月份 + AOI。
  - `report_agent._merge_score`（要月份 + AOI，不要 task），`_run_analysis` 路由到 `PressureScoreService`。
- 测试：`tests/test_scoring_tools.py` + `tests/test_pressure_score.py` + 意图批测。

### 验收
- pytest 全绿；压力/补绿问法 ≥8 条正确路由到 score；
- 直连 service 端到端产出 TOP-N 优先区（分数单调、依据齐全）；
- live HTTP 出报告并渲染；绿地率来源与局限有明确标注。

---

## 阶段七：收尾与文档

- 更新 `docs/开发计划-海淀三场景与架构演进.md`：阶段四/五/六状态改为 ✅，补一句"三场景已上线"。
- 更新 `agent/API.md`（若存在）：说明三个场景的触发方式、所需输入（AOI/月份）、返回。
- 更新 `CLAUDE.md`（若存在相关章节）：场景清单与意图关键词。
- 三场景意图互斥性回归：checkup / change / score / 普通报告 四类各 ≥6 条，确认无相互误伤。
- 全量 pytest 最终一次；清理临时文件；后端重启冒烟。

### 关于 planner（不在本次范围）
LLM function-calling planner 复杂度与不确定性最高，且三场景用确定性路由已可跑通。**本次不做**，作为下一版本，避免夜间无监督下引入路由回归。若前面阶段全部提前完成且时间充裕，仅在**新分支**做原型，不并入主路径。

---

## 执行顺序与自检节奏

```
阶段五 B：tools+test → service+test → intent/route+test → 直连E2E → live HTTP → 日志
阶段六 C：tools+test → service+test → intent/route+test → 直连E2E → live HTTP → 日志
阶段七  ：文档 → 互斥回归 → 全量pytest → 冒烟 → 日志
```

每个 `→` 后跑一次 `pytest tests/ -q`。任一阶段 live HTTP 因外部 API 抖动失败，记录并继续（service 直连 E2E 通过即视为逻辑达标，HTTP 抖动不阻塞）。
