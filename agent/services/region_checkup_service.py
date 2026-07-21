"""Scenario A — 片区综合体检 (region checkup).

Given one AOI + month, aggregate the reliable binary coverages
(building/water/road/construction) into hectare + share metrics over the whole
selection, plus a land-cover class distribution. Produces one ``AnalysisResult``
that flows through the existing ReportService unchanged.

Haidian only in v1 (the only region with the binary task bundle + legend).
Model accuracy is upstream; we report the model's output faithfully.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.config import EmbeddingAPIConfig, ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportRequest
from agent.services.aoi_cover_service import AoiCoverService
from agent.services.haidian_embedding_service import TASK_DISPLAY
from agent.services.patch_selection_service import PatchSelectionService
from agent.services.satellite_basemap import basemap_chart
from agent.tools.aoi import aggregate_binary_coverage, aggregate_class_distribution
from agent.tools.classmap import class_distribution, normalize_legend
from agent.tools.raster import binary_coverage

# The checkup bundle: reliable binary coverages, in report order.
CHECKUP_BINARY_TASKS = ["building_extraction", "road_extraction", "water_extraction", "construction"]
LAND_COVER_TASK = "land_cover_classification"


class RegionCheckupService:
    """Aggregate a multi-task 片区体检 over one AOI."""

    def __init__(
        self,
        config: EmbeddingAPIConfig | None = None,
        report_config: ReportConfig | None = None,
        aoi_cover: AoiCoverService | None = None,
        patch_selection: PatchSelectionService | None = None,
    ) -> None:
        self.config = config or EmbeddingAPIConfig()
        self.report_config = report_config or ReportConfig()
        self.asset_dir = self.report_config.asset_dir
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.patch_selection = patch_selection or PatchSelectionService(self.config)
        self.aoi_cover = aoi_cover or AoiCoverService(self.config, self.patch_selection)
        self._legend_cache: dict[str, list[dict[str, Any]]] = {}

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        region_id = "haidian"
        month = (request.time_range or "").replace("-", "")
        bbox = self._bbox(request.aoi)
        if bbox is None:
            raise RuntimeError("片区体检需要先在地图上框选一个范围（AOI）。")

        patches = self._resolve_patches(region_id, month, bbox)
        if not patches:
            raise RuntimeError("当前框选范围内没有可用于体检的 patch，请调整范围或月份。")

        coverages = self._binary_coverages(region_id, month, patches)
        land_cover = self._land_cover_distribution(region_id, month, patches)
        total_ha = next((c["total_area_ha"] for c in coverages.values() if c.get("total_area_ha")), 0.0)
        patch_count = next((c["patch_count"] for c in coverages.values()), len(patches))
        return self._build_result(request, month, patch_count, total_ha, coverages, land_cover)

    def _resolve_patches(self, region_id: str, month: str, bbox: list[float]) -> list[dict[str, Any]]:
        # Any binary task shares the same patch grid; use one to enumerate the AOI.
        search = self.patch_selection.search(
            {"region": "北京市海淀区", "task": "建筑物提取", "time_range": month, "bbox": bbox, "limit": 200}
        )
        return search.patches if search.status == "ok" else []

    @staticmethod
    def _bbox(aoi: Any) -> list[float] | None:
        if not isinstance(aoi, dict) or aoi.get("type") != "bbox":
            return None
        coords = aoi.get("coordinates")
        if not isinstance(coords, list) or len(coords) != 4:
            return None
        try:
            bbox = [float(v) for v in coords]
        except (TypeError, ValueError):
            return None
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            return None
        return bbox

    def _binary_coverages(
        self, region_id: str, month: str, patches: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for task_id in CHECKUP_BINARY_TASKS:
            rows: list[dict[str, Any]] = []
            for patch, counts in self.aoi_cover.iter_patch_colors(region_id, task_id, month, patches):
                cov = binary_coverage(task_id, counts, patch.get("bounds"))
                if cov:
                    rows.append(cov)
            if rows:
                out[task_id] = aggregate_binary_coverage(rows)
        return out

    def _land_cover_distribution(
        self, region_id: str, month: str, patches: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        legend = self._legend(region_id, LAND_COVER_TASK)
        if not legend:
            return []
        per_patch: list[list[dict[str, Any]]] = []
        for patch, counts in self.aoi_cover.iter_patch_colors(region_id, LAND_COVER_TASK, month, patches):
            dist = class_distribution(counts, legend, patch.get("bounds"))
            if dist:
                per_patch.append(dist)
        return aggregate_class_distribution(per_patch)

    def _legend(self, region_id: str, task_id: str) -> list[dict[str, Any]]:
        if task_id not in self._legend_cache:
            from urllib.parse import urlencode

            query = urlencode({"region_id": region_id})
            raw = self.aoi_cover.http.get_list_optional(f"/system-models/{task_id}/classes?{query}")
            self._legend_cache[task_id] = normalize_legend(raw)
        return self._legend_cache[task_id]

    def _build_result(
        self,
        request: ReportRequest,
        month: str,
        patch_count: int,
        total_ha: float,
        coverages: dict[str, dict[str, Any]],
        land_cover: list[dict[str, Any]],
    ) -> AnalysisResult:
        region = "北京市海淀区"
        tr = request.time_range
        metrics = [MetricCard("体检片区面积", f"{total_ha:.0f} 公顷", f"覆盖 {patch_count} 个 patch 的合计面积")]
        for task_id in CHECKUP_BINARY_TASKS:
            agg = coverages.get(task_id)
            if not agg or agg.get("coverage_ratio") is None:
                continue
            name = TASK_DISPLAY.get(task_id, task_id)
            metrics.append(
                MetricCard(f"{name}覆盖率", f"{agg['coverage_ratio'] * 100:.1f}%", f"约 {agg['covered_area_ha']:.0f} 公顷")
            )

        findings = self._findings(coverages, land_cover)
        basemap = basemap_chart(self._bbox(request.aoi), self.asset_dir, f"checkup-{request.session_id}")
        return AnalysisResult(
            task="片区综合体检",
            region=region,
            time_range=tr,
            headline=f"{region}{tr}片区综合体检",
            summary=self._summary(tr, patch_count, total_ha, coverages),
            metrics=metrics,
            findings=findings,
            recommendations=self._recommendations(coverages, land_cover),
            narrative_blocks=[
                {
                    "title": "体检范围",
                    "text": (
                        f"本次体检针对地图框选片区，命中 {patch_count} 个 patch，合计约 {total_ha:.0f} 公顷，"
                        f"数据月份 {tr}。各专题覆盖率按与片区相交的整块 patch 面积加权统计。"
                    ),
                },
                {
                    "title": "专题构成",
                    "text": (
                        "体检聚合建筑物、道路、水体、施工四个二值专题的覆盖率与公顷数，"
                        "并附土地覆盖各类别占比，用于快速掌握片区的建设强度与地表构成。"
                    ),
                },
            ],
            risks=self._risks(coverages),
            method_notes=[
                f"Agent 将请求识别为『片区综合体检』场景，region=haidian、month={month}。",
                f"调用海淀 embedding-api：{self.config.base_url}，逐 patch 拉取专题结果并聚合。",
                "覆盖率＝前景像素占比；面积＝相交 patch 的 UTM 面积×占比之和（整块统计，未做 AOI 边界精确裁剪）。",
            ],
            limitations=[
                "面积按整块 patch 聚合，AOI 边界处未做像素级裁剪，边缘片区略有高估。",
                "土地覆盖分类为模型直接输出，仅供地表构成参考；水体/建筑等定量以对应二值专题为准。",
            ],
            confidence_notes=[
                "四个二值专题结果与土地覆盖分布均来自在线海淀 embedding-api。",
                "体检指标为 Agent 从专题结果图聚合得到的轻量统计，正式评估仍需接入标签或评估接口。",
            ],
            data_source="haidian_embedding_api",
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            aef_payload={
                "scenario": "checkup",
                "month": month,
                "patch_count": patch_count,
                "total_area_ha": round(total_ha, 2),
                "coverages": coverages,
            },
            charts=[basemap] if basemap else [],
            data_table=land_cover,
            data_table_title="土地覆盖各类别占比（片区聚合）" if land_cover else "",
        )

    @staticmethod
    def _pct(coverages: dict[str, dict[str, Any]], task_id: str) -> float | None:
        agg = coverages.get(task_id)
        if not agg or agg.get("coverage_ratio") is None:
            return None
        return agg["coverage_ratio"] * 100

    def _summary(self, tr: str, patch_count: int, total_ha: float, coverages: dict[str, dict[str, Any]]) -> str:
        b = self._pct(coverages, "building_extraction")
        w = self._pct(coverages, "water_extraction")
        parts = [f"北京市海淀区{tr}框选片区体检：合计约 {total_ha:.0f} 公顷（{patch_count} 个 patch）。"]
        if b is not None:
            parts.append(f"建筑物覆盖约 {b:.1f}%")
        if w is not None:
            parts.append(f"水体覆盖约 {w:.1f}%")
        return "，".join(parts) + "。"

    def _findings(self, coverages: dict[str, dict[str, Any]], land_cover: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        for task_id in CHECKUP_BINARY_TASKS:
            agg = coverages.get(task_id)
            if not agg or agg.get("coverage_ratio") is None:
                continue
            name = TASK_DISPLAY.get(task_id, task_id)
            out.append(f"{name}覆盖率约 {agg['coverage_ratio'] * 100:.1f}%，面积约 {agg['covered_area_ha']:.0f} 公顷。")
        b = self._pct(coverages, "building_extraction")
        if b is not None:
            level = "高" if b >= 40 else ("中等" if b >= 20 else "较低")
            out.append(f"片区建设强度{level}（建筑物覆盖 {b:.1f}%）。")
        if land_cover:
            top = land_cover[0]
            out.append(f"土地覆盖以「{top['label']}」为主，约占 {top['ratio'] * 100:.1f}%。")
        return out

    def _recommendations(self, coverages: dict[str, dict[str, Any]], land_cover: list[dict[str, Any]]) -> list[str]:
        recs: list[str] = []
        c = self._pct(coverages, "construction")
        if c is not None and c >= 5:
            recs.append(f"施工覆盖约 {c:.1f}%，建议结合多期数据关注建设扰动动态（场景 B）。")
        b = self._pct(coverages, "building_extraction")
        if b is not None and b >= 40:
            recs.append("建设强度较高，可进一步评估绿地与硬化平衡（场景 C 压力评分）。")
        recs.append("如需边界更精确的面积，可缩小框选范围或后续引入 AOI 像素级裁剪。")
        return recs

    def _risks(self, coverages: dict[str, dict[str, Any]]) -> list[str]:
        missing = [TASK_DISPLAY.get(t, t) for t in CHECKUP_BINARY_TASKS if t not in coverages]
        if missing:
            return [f"以下专题在本月/本片区暂无有效结果，未纳入体检：{'、'.join(missing)}。"]
        return []
