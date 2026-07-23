"""Scenario C — 高硬化低绿地压力评分 / 补绿优先区.

Per patch in an AOI: score built-up (impervious) intensity against green
deficit, rank patches, surface the TOP-N as补绿 priority zones with reasons.
Produces one ``AnalysisResult`` through the existing ReportService.

Haidian only in v1. Impervious comes from the reliable building binary task;
green comes from the advisory land-cover model — the report flags that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import hashlib

import numpy as np
from PIL import Image

from agent.config import EmbeddingAPIConfig, ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportRequest
from agent.services.aoi_cover_service import AoiCoverService
from agent.services.patch_selection_service import PatchSelectionService
from agent.services.satellite_basemap import basemap_chart
from agent.tools.classmap import class_distribution, normalize_legend
from agent.tools.mosaic import build_mosaic_overlay
from agent.tools.raster import area_ha_from_bounds, binary_coverage
from agent.tools.scoring import band_rgba, pressure_score, rank_patches, summarize_scores

BUILDING_TASK = "building_extraction"
LAND_COVER_TASK = "land_cover_classification"
# Land-cover class names counted as green (matched on the API legend labels).
GREEN_NAME_CUES = ("树", "林", "灌木", "草", "植被", "绿")
TOP_N = 10


class PressureScoreService:
    """Rank AOI patches by 高硬化低绿地 pressure."""

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
        self._legend: list[dict[str, Any]] | None = None

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        region_id = "haidian"
        month = (request.time_range or "").replace("-", "")
        bbox = self._bbox(request.aoi)
        if bbox is None:
            raise RuntimeError("补绿优先区评分需要先在地图上框选一个范围（AOI）。")

        patches = self._resolve_patches(region_id, month, bbox)
        if not patches:
            raise RuntimeError("当前框选范围内没有可用于评分的 patch，请调整范围或月份。")

        scored = self._score_patches(region_id, month, patches)
        if not scored:
            raise RuntimeError("无法获取该月份的建筑/土地覆盖结果，评分未完成。请更换月份或范围。")

        summary = summarize_scores(scored)
        ranked = rank_patches(scored, top_n=TOP_N)
        overlays = self._score_overlays(scored, month)
        return self._build_result(request, month, summary, ranked, overlays)

    def _resolve_patches(self, region_id: str, month: str, bbox: list[float]) -> list[dict[str, Any]]:
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

    def _get_legend(self, region_id: str) -> list[dict[str, Any]]:
        if self._legend is None:
            from urllib.parse import urlencode

            query = urlencode({"region_id": region_id})
            raw = self.aoi_cover.http.get_list_optional(f"/system-models/{LAND_COVER_TASK}/classes?{query}")
            self._legend = normalize_legend(raw)
        return self._legend

    @staticmethod
    def _green_ratio(distribution: list[dict[str, Any]]) -> float:
        total = 0.0
        for row in distribution:
            label = row.get("label") or ""
            if any(cue in label for cue in GREEN_NAME_CUES):
                total += row.get("ratio") or 0.0
        return round(total, 4)

    def _score_patches(self, region_id: str, month: str, patches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        legend = self._get_legend(region_id)
        scored: list[dict[str, Any]] = []
        for patch in patches:
            patch_id = str(patch.get("patch_id") or "")
            if not patch_id:
                continue
            bld_colors = self.aoi_cover._result_colors(region_id, patch_id, BUILDING_TASK, month)
            lc_colors = self.aoi_cover._result_colors(region_id, patch_id, LAND_COVER_TASK, month)
            if bld_colors is None or lc_colors is None:
                continue
            cov = binary_coverage(BUILDING_TASK, bld_colors, patch.get("bounds"))
            imp_ratio = cov.get("foreground_ratio") if cov else None
            dist = class_distribution(lc_colors, legend, patch.get("bounds"))
            if imp_ratio is None or not dist:
                continue
            green = self._green_ratio(dist)
            score = pressure_score(imp_ratio, green)
            total_ha = area_ha_from_bounds(patch.get("bounds"), projected=True)
            scored.append(
                {
                    "patch_id": patch_id,
                    "bounds": patch.get("bounds"),
                    "bounds_wgs84": patch.get("bounds_wgs84"),
                    **score,
                    "area_ha": total_ha,
                }
            )
        return scored

    def _score_overlays(self, scored: list[dict[str, Any]], month: str) -> list[ChartAsset]:
        """Render each patch as a solid band-colour tile, stitch into one heatmap.

        High=red / medium=orange / low=green, semi-transparent so the satellite
        basemap reads through. Patches missing WGS84 bounds are left off the map.
        """
        tiles: list[dict[str, Any]] = []
        for row in scored:
            tile = self._render_band_tile(row, month)
            if tile is not None:
                tiles.append(tile)
        return build_mosaic_overlay(
            tiles,
            self.asset_dir,
            stem="haidian_score_mosaic",
            fingerprint=f"score:{month}",
            merged_title="补绿压力热力图（{n} patch 拼接）",
            merged_caption=(
                "红=高压（补绿最优先）、橙=中压、绿=低压，按 UTM 网格将 {n} 个 patch "
                "拼接为一张连续图，叠加在卫星底图上。"
            ),
            per_patch_title="补绿压力 · {patch_id}",
            per_patch_caption="红=高压（补绿最优先）、橙=中压、绿=低压。",
        )

    def _render_band_tile(self, row: dict[str, Any], month: str) -> dict[str, Any] | None:
        bounds_wgs84 = [float(v) for v in (row.get("bounds_wgs84") or [])][:4]
        if len(bounds_wgs84) != 4:
            return None
        patch_id = str(row.get("patch_id") or "")
        colour = band_rgba(str(row.get("band") or ""))
        # A small solid tile is enough — the map georeferences it to patch bounds;
        # the mosaic paste scales tiles by shared pixels-per-metre, so equal sizes.
        rgba = np.zeros((128, 128, 4), dtype=np.uint8)
        rgba[:, :] = colour
        digest = hashlib.sha1(f"{patch_id}:{month}:{row.get('band')}".encode("utf-8")).hexdigest()[:12]
        out_path = self.asset_dir / f"haidian_score_{patch_id}_{digest}.png"
        try:
            Image.fromarray(rgba, mode="RGBA").save(out_path)
        except (OSError, ValueError):
            return None
        bounds_proj = row.get("bounds") or []
        bounds_proj = [float(v) for v in bounds_proj][:4] if len(bounds_proj) == 4 else []
        return {
            "patch_id": patch_id,
            "path": out_path,
            "bounds_wgs84": bounds_wgs84,
            "bounds": bounds_proj,
        }

    def _build_result(self, request, month, summary, ranked, overlays=None) -> AnalysisResult:
        region = "北京市海淀区"
        tr = request.time_range
        mean = summary["mean_score"]
        metrics = [
            MetricCard("片区平均压力分", f"{mean:.0f} / 100" if mean is not None else "—", f"覆盖 {summary['patch_count']} 个 patch"),
            MetricCard("高压 patch 数", f"{summary['high']} 个", "高硬化且低绿地，补绿优先"),
            MetricCard("中压 / 低压", f"{summary['medium']} / {summary['low']} 个", "中等 / 较低压力"),
        ]
        if ranked:
            top = ranked[0]
            metrics.append(
                MetricCard(
                    "最高压片区",
                    f"{top['score']:.0f} 分",
                    f"{top['patch_id']}（硬化 {top['impervious_ratio'] * 100:.0f}%，绿地 {top['green_ratio'] * 100:.0f}%）",
                )
            )
        table = [{"label": f"#{r['rank']} {r['patch_id']}", "ratio": None, "value": r["score"]} for r in ranked]
        basemap = basemap_chart(self._bbox(request.aoi), self.asset_dir, f"score-{request.session_id}")
        return AnalysisResult(
            task="补绿优先区评分",
            region=region,
            time_range=tr,
            headline=f"{region}{tr}高硬化低绿地压力评分",
            summary=self._summary(region, tr, summary, ranked),
            metrics=metrics,
            findings=self._findings(summary, ranked),
            recommendations=self._recommendations(ranked),
            narrative_blocks=[
                {
                    "title": "评分方法",
                    "text": (
                        "压力分＝硬化强度与绿地缺口的加权组合（各占 50%，0–100 分）。"
                        "硬化强度取自建筑物提取（可靠），绿地率取自土地覆盖分类中树木/灌木/草地之和（模型参考值）。"
                        f"共评分 {summary['patch_count']} 个 patch，按分从高到低取前 {len(ranked)} 个作为补绿优先区。"
                    ),
                },
            ],
            risks=self._risks(summary),
            method_notes=[
                f"Agent 将请求识别为『补绿优先区评分』场景，region=haidian、month={month}。",
                f"调用海淀 embedding-api：{self.config.base_url}，逐 patch 取建筑物覆盖率与土地覆盖绿地率。",
                "压力分＝0.5×硬化率 + 0.5×(1−绿地率)，再×100；分档 高压≥67 / 中压≥34 / 低压<34。",
            ],
            limitations=[
                "绿地率来自土地覆盖模型，存在类别误判，绿地相关结论为参考值，需实地或更高精度数据校核。",
                "评分按整块 patch 统计，未做 AOI 边界像素级裁剪；权重与分档为未经地面校准的启发式默认值。",
            ],
            confidence_notes=[
                "硬化强度来自建筑物二值专题（较可靠）；绿地率来自土地覆盖分类（参考）。",
                "压力分为 Agent 依既定权重计算的相对排序指标，用于圈定优先区，不构成绝对绿化标准。",
            ],
            data_source="haidian_embedding_api",
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            aef_payload={
                "scenario": "score",
                "month": month,
                "summary": summary,
                "weights": {"impervious": 0.5, "green_deficit": 0.5},
                "top_patches": [
                    {k: r.get(k) for k in ("rank", "patch_id", "score", "band", "impervious_ratio", "green_ratio", "bounds")}
                    for r in ranked
                ],
            },
            charts=([basemap] if basemap else []) + list(overlays or []),
            data_table=table,
            data_table_title="补绿优先区 TOP（压力分）" if table else "",
        )

    def _summary(self, region, tr, summary, ranked) -> str:
        mean = summary["mean_score"]
        parts = [f"{region}{tr}框选片区补绿优先区评分：{summary['patch_count']} 个 patch，平均压力分 {mean:.0f}/100（高压 {summary['high']} 个）。"]
        if ranked:
            top = ranked[0]
            parts.append(f"最需补绿的是 {top['patch_id']}（{top['score']:.0f} 分，硬化 {top['impervious_ratio'] * 100:.0f}%、绿地 {top['green_ratio'] * 100:.0f}%）。")
        return "".join(parts)

    def _findings(self, summary, ranked) -> list[str]:
        out = [f"片区共 {summary['patch_count']} 个 patch，平均压力分 {summary['mean_score']:.0f}/100，其中高压 {summary['high']} 个、中压 {summary['medium']} 个、低压 {summary['low']} 个。"]
        for r in ranked[:3]:
            out.append(
                f"#{r['rank']} {r['patch_id']}：压力分 {r['score']:.0f}（{r['band']}），硬化 {r['impervious_ratio'] * 100:.0f}%、绿地 {r['green_ratio'] * 100:.0f}%。"
            )
        return out

    def _recommendations(self, ranked) -> list[str]:
        recs: list[str] = []
        high = [r for r in ranked if r.get("band") == "高压"]
        if high:
            ids = "、".join(r["patch_id"] for r in high[:3])
            recs.append(f"优先在高压片区（如 {ids}）推进口袋公园、屋顶/立体绿化等补绿措施。")
        recs.append("绿地率来自土地覆盖模型，落地前建议结合实地或更高精度影像核验高压片区的真实绿量。")
        recs.append("可结合场景 B 的建设扰动监测，识别近期新增硬化导致压力上升的片区。")
        return recs

    def _risks(self, summary) -> list[str]:
        if summary["patch_count"] and summary["high"] / summary["patch_count"] > 0.5:
            return ["过半 patch 处于高压区间，片区整体硬化偏高、绿地不足，建议系统性规划补绿。"]
        return []
