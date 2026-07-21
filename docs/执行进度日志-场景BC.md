# 执行进度日志：场景 B / C 与收尾

> 自主执行的时间线。每完成一个阶段追加一条。关联：[执行计划](./执行计划-场景BC与收尾.md)

## 起点（场景 A 已完成）
- 场景 A（片区综合体检）端到端完成：intent→routing→RegionCheckupService→report。
- 基线：93 项 pytest 全绿。
- 工具就绪：`raster`（二值覆盖+公顷）、`aoi`（片区聚合 + 类别分布聚合）、`classmap`（多类分布）。

---

## 阶段五：场景 B（建设扰动短周期监测）—— ✅ 完成
- **工具** `agent/tools/change.py`：`foreground_mask` / `mask_for_task` / `binary_change`（两期像素级 diff → 新增/减少/保持像素+公顷）/ `aggregate_change`（多 patch 聚合，面积加权重算增长率）。+8 项纯函数测试。
- **服务** `agent/services/change_monitor_service.py`：AOI + task（缺省施工）+ 两月，逐 patch 拉两期结果做像素级 diff 聚合，产出 `AnalysisResult`（净变化/新增/减少/增长率 metrics + 逐 patch 净变化 TOP 表 + 变化叙事 + basemap）。+5 项 stub 测试。为此给 `AoiCoverService` 增 `fetch_result_array`（PNG→numpy），`ReportRequest` 增 `before_time_range`/`after_time_range`，`schemas` 增 `infer_two_months`（"X 到 Y""X 和 Y" 双月抽取）。
- **意图+路由**：`scenario="change"`（关键词：变化/扰动/监测/两期/对比/前后对比/动态/变化检测…；change 优先于 checkup）。新增 `report_agent._merge_change`（要两月+AOI，缺则分别友好追问）、`_run_analysis` 路由、跨轮 sticky、双月写入 memory。修意图规则：祈使式场景请求（含线索、非疑问）保持 rules-first 高置信 + 不被 "对比一下" 这类软疑问词误降级为 follow-up；同时保留"评价式问句（准不准/怎么样…）永远走讨论"。
- **验证**：**109 项 pytest 全绿**；祈使问法 **8/8** 路由到 change，评价式追问 **4/4** 正确留在讨论；service 直连 E2E 逻辑正确（建筑 +26.58 ha 增长与像素探针一致；施工两期恒为 0 属真实数据非 bug）；**live HTTP 出报告并渲染**（建筑变化 +27 公顷、+4.5%，HTML 含指标+逐 patch 表）。
- 遗留：面积按整块 patch 聚合，AOI 边界未做像素级裁剪（已在报告 limitation 标注）。

---

## 阶段六：场景 C（高硬化低绿地压力评分 / 补绿优先区）—— ✅ 完成
- **工具** `agent/tools/scoring.py`：`pressure_score`（0.5×硬化率 + 0.5×绿地缺口 → 0–100 分 + 高/中/低压分档，权重与阈值为带注释的常量）/ `rank_patches`（按分排序、打 rank、取 TOP-N，不改输入）/ `summarize_scores`（均分+各档计数）。+9 项纯函数测试。
- **服务** `agent/services/pressure_score_service.py`：逐 patch 取建筑物覆盖率（硬化，可靠）+ 土地覆盖树/灌/草之和（绿地率，参考），算压力分→排序→`AnalysisResult`（片区均分/高压数 metrics + 补绿优先区 TOP 表 + 决策建议；TOP-N bounds 入 aef_payload 供前端地图高亮）。+4 项 stub 测试。绿地率来源与模型局限在 limitations/confidence_notes 明确标注。
- **意图+路由**：`scenario="score"`（关键词：压力/硬化/绿地率/补绿/增绿/优先区/最缺绿/该绿化…；顺序 change→score→checkup）。`report_agent` 将 checkup/score 的"月份+AOI"槽位逻辑抽为共享 `_merge_month_aoi`（仅提示文案不同），新增 score 路由与跨轮 sticky。
- **验证**：**124 项 pytest 全绿**；补绿问法 **8/8** 路由到 score，四类场景（checkup/change/score/普通）互斥 **8/8** 无误伤；service 直连 E2E（14 patch，均分 57.3，TOP1 patch_000017 硬化57%/绿地3.6%→76.9分高压，分数单调递减）；**live HTTP 出报告并渲染**（平均压力分 57、高压 1 个、补绿优先区 TOP 表）。
- 遗留：绿地率依赖土地覆盖模型（有误判风险，已标注参考）；权重/分档为未校准启发式默认。

---

## 阶段七：收尾与文档 —— ✅ 完成
- **文档**：`开发计划-海淀三场景与架构演进.md`（阶段四/五/六 → ✅，进度摘要加"三场景已上线"，G2 变化检测 gap → 已解决）；`agent/API.md`（新增"海淀复合场景"表：三 scenario 的触发问法/必需输入/产出 + `before_time_range`/`after_time_range` 字段说明 + aef_payload.top_patches 定位）；`CLAUDE.md`（三 service 行、请求流 parse_intent/merge_memory/run_analysis 的 scenario 说明、新增 `agent/tools/` 纯函数工具段、ReportRequest 双月字段）。
- **鲁棒性补强**：`infer_time_range` 支持 `YYYY-MM` 数字月份直接抽取（此前只认"X年X月"），使"看2025-12的"这类多轮补月生效。
- **回归**：**124 项 pytest 全绿**；四类意图（checkup/change/score/普通）互斥 8/8；多轮 sticky 验证——change 记住两月只追问 AOI、score 记住场景两轮补齐后出报告；**live HTTP 4-way 冒烟全部 status=ok 出报告**（体检/变化/评分/普通各一）。
- 后端最终重启健康；planner（LLM function-calling）按计划留作下一版本，未并入主路径。

---

## 总结
- 海淀三场景（A 片区综合体检 / B 建设扰动监测 / C 补绿优先区评分）全部端到端上线并 live 验证。
- 架构一致：新场景 = 纯函数工具（`change.py`/`scoring.py`）+ 薄 service + 确定性意图路由，渲染层（ReportService）零改动，`/api/report` 单入口兼容。
- 测试从 93 → **124** 项全绿（+31：change tools 8、change monitor 8、scoring tools 9、pressure score 6）。
- 诚实性：变化/评分均忠实呈现模型输出，绿地率、逐期误差、patch 级聚合的局限在报告 limitations/confidence_notes 明确标注。
