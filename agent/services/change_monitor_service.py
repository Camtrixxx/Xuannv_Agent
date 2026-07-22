"""Scenario B — 建设扰动短周期监测 (change monitoring).

Given one AOI, one binary task, and two months, diff the pixel-aligned result
PNGs per patch and aggregate into gained / lost / net area over the selection.
Produces one ``AnalysisResult`` that flows through the existing ReportService.

Haidian only in v1. Model accuracy is upstream; we report change faithfully.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from PIL import Image

from agent.config import EmbeddingAPIConfig, ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportRequest
from agent.services.aoi_cover_service import AoiCoverService
from agent.services.haidian_embedding_service import TASK_DISPLAY, TASK_TO_HAIDIAN
from agent.services.model_registry_service import ModelRegistryService
from agent.services.patch_selection_service import PatchSelectionService
from agent.services.satellite_basemap import basemap_chart
from agent.tools.change import (
    aggregate_change,
    binary_change,
    change_rgba,
    custom_model_mask,
    mask_for_task,
)

# Tasks meaningful to monitor over a short window (built environment dynamics).
CHANGE_TASKS = {"construction", "building_extraction", "road_extraction", "water_extraction"}
DEFAULT_CHANGE_TASK = "construction"


class ChangeMonitorService:
    """Two-date change monitoring over one AOI."""

    def __init__(
        self,
        config: EmbeddingAPIConfig | None = None,
        report_config: ReportConfig | None = None,
        aoi_cover: AoiCoverService | None = None,
        patch_selection: PatchSelectionService | None = None,
        registry: ModelRegistryService | None = None,
    ) -> None:
        self.config = config or EmbeddingAPIConfig()
        self.report_config = report_config or ReportConfig()
        self.asset_dir = self.report_config.asset_dir
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.patch_selection = patch_selection or PatchSelectionService(self.config)
        self.aoi_cover = aoi_cover or AoiCoverService(self.config, self.patch_selection)
        self.registry = registry or ModelRegistryService(self.config)

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        region_id = "haidian"
        # Custom-model change monitoring (non-native object, e.g. 湿地): the
        # object's ready model diffs its own two-date foreground masks. Native
        # tasks keep the system-task path.
        model_id = getattr(request, "custom_model_id", "") or ""
        custom_class = getattr(request, "target_object", "") or ""
        task_id = self._resolve_task(request.task) if not model_id else "custom"
        before, after = self._resolve_months(request)
        bbox = self._bbox(request.aoi)
        if bbox is None:
            raise RuntimeError("建设扰动监测需要先在地图上框选一个范围（AOI）。")
        if not before or not after or before == after:
            raise RuntimeError("变化监测需要两个不同的月份（起始月与对比月）。")

        patches = self._resolve_patches(region_id, before, bbox)
        if not patches:
            raise RuntimeError("当前框选范围内没有可用于监测的 patch，请调整范围或月份。")

        per_patch, rows, overlays = self._diff_patches(region_id, task_id, before, after, patches, model_id)
        agg = aggregate_change(per_patch)
        if agg["patch_count"] == 0:
            raise RuntimeError("两期结果均无法获取，无法计算变化。请更换月份或范围。")
        # Look up the custom model's training metadata once so the report can be
        # honest about method + feature source (and warn on AEF annual features).
        model_info = self.registry.model_status(model_id, region_id) if model_id else None
        return self._build_result(
            request, task_id, before, after, agg, rows, model_id, custom_class, model_info, overlays
        )

    def _resolve_task(self, task: str) -> str:
        task_id = TASK_TO_HAIDIAN.get(task, "")
        if task_id in CHANGE_TASKS:
            return task_id
        return DEFAULT_CHANGE_TASK

    @staticmethod
    def _resolve_months(request: ReportRequest) -> tuple[str, str]:
        before = (getattr(request, "before_time_range", "") or "").replace("-", "")
        after = (getattr(request, "after_time_range", "") or "").replace("-", "")
        return before, after

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

    def _diff_patches(self, region_id, task_id, before, after, patches, model_id=""):
        """Return (per_patch_change_dicts, table_rows, overlay_charts).

        Skips patches missing a date. For every patch that diffs, also renders a
        change-map PNG (gained red / lost blue / transparent elsewhere) as an
        ``overlay=True`` ChartAsset so the report map can georeference it.
        """
        per_patch: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        overlays: list[ChartAsset] = []
        for patch in patches:
            patch_id = str(patch.get("patch_id") or "")
            if not patch_id:
                continue
            arr_a = self.aoi_cover.fetch_result_array(region_id, patch_id, task_id, before, model_id)
            arr_b = self.aoi_cover.fetch_result_array(region_id, patch_id, task_id, after, model_id)
            if arr_a is None or arr_b is None:
                continue
            if model_id:
                mask_a = custom_model_mask(arr_a)
                mask_b = custom_model_mask(arr_b)
            else:
                mask_a = mask_for_task(arr_a, task_id)
                mask_b = mask_for_task(arr_b, task_id)
            if mask_a is None or mask_b is None or mask_a.shape != mask_b.shape:
                continue
            from agent.tools.raster import area_ha_from_bounds

            total_ha = area_ha_from_bounds(patch.get("bounds"), projected=True)
            change = binary_change(mask_a, mask_b, total_ha)
            per_patch.append(change)
            rows.append(
                {
                    "label": patch_id,
                    "ratio": None,
                    "gained_ha": change["gained_ha"],
                    "lost_ha": change["lost_ha"],
                    "net_ha": change["net_ha"],
                }
            )
            overlay = self._change_overlay(patch, patch_id, task_id, before, after, mask_a, mask_b)
            if overlay is not None:
                overlays.append(overlay)
        # Rank the table by net growth so the biggest movers surface first.
        rows.sort(key=lambda r: (r["net_ha"] if r["net_ha"] is not None else 0), reverse=True)
        return per_patch, rows, overlays

    def _change_overlay(self, patch, patch_id, task_id, before, after, mask_a, mask_b) -> ChartAsset | None:
        """Render one patch's change mask to a PNG and wrap it as a map overlay.

        Returns None when the patch has no WGS84 bounds (nothing to georeference)
        or the PNG can't be written — the report simply omits that layer.
        """
        bounds = [float(v) for v in (patch.get("bounds_wgs84") or [])][:4]
        if len(bounds) != 4:
            return None
        try:
            rgba = change_rgba(mask_a, mask_b)
        except ValueError:
            return None
        digest = hashlib.sha1(f"{patch_id}:{task_id}:{before}:{after}".encode("utf-8")).hexdigest()[:12]
        out_path = self.asset_dir / f"haidian_change_{task_id}_{patch_id}_{digest}.png"
        try:
            Image.fromarray(rgba, mode="RGBA").save(out_path)
        except (OSError, ValueError):
            return None
        return ChartAsset(
            title=f"变化专题 · {patch_id}",
            kind="image",
            url=f"/reports/assets/{out_path.name}",
            caption="红色为新增、蓝色为减少的目标区域（两期逐像素差）。",
            bounds_wgs84=bounds,
            overlay=True,
            patch_id=patch_id,
        )

    def _build_result(self, request, task_id, before, after, agg, rows, model_id="", custom_class="", model_info=None, overlays=None) -> AnalysisResult:
        region = "北京市海淀区"
        # Custom-model runs show the user's own class name; native runs use the
        # built-in task display name.
        name = (custom_class or "自定义地物") if model_id else TASK_DISPLAY.get(task_id, task_id)
        b_disp, a_disp = self._fmt_month(before), self._fmt_month(after)
        span = f"{b_disp} → {a_disp}"
        net = agg["net_area_ha"]
        gained, lost = agg["gained_area_ha"], agg["lost_area_ha"]
        trend = "扩张" if (net or 0) > 0 else ("收缩" if (net or 0) < 0 else "基本持平")

        metrics = [
            MetricCard(f"{name}净变化", f"{net:+.0f} 公顷" if net is not None else "—", f"{span}（{agg['patch_count']} 个 patch）"),
            MetricCard("新增面积", f"{gained:.0f} 公顷" if gained is not None else "—", f"{name}新出现区域"),
            MetricCard("减少面积", f"{lost:.0f} 公顷" if lost is not None else "—", f"{name}消失区域"),
        ]
        if agg.get("growth_ratio") is not None:
            metrics.append(MetricCard("增长率", f"{agg['growth_ratio'] * 100:+.1f}%", f"相对 {b_disp} 存量"))

        basemap = basemap_chart(self._bbox(request.aoi), self.asset_dir, f"change-{request.session_id}")
        # Satellite basemap first, then one change-map overlay per patch so the
        # report's Leaflet map georeferences red=gained / blue=lost onto it.
        charts = ([basemap] if basemap else []) + list(overlays or [])
        table = [
            {"label": r["label"], "ratio": None, "value": r["net_ha"]}
            for r in rows[:10]
            if r["net_ha"] is not None
        ]
        return AnalysisResult(
            task=f"{name}·建设扰动监测",
            region=region,
            time_range=span,
            headline=f"{region}{span} {name}建设扰动监测",
            summary=(
                f"{region}框选片区 {span} 的{name}变化：净变化约 {net:+.0f} 公顷"
                f"（新增 {gained:.0f}、减少 {lost:.0f}），整体呈{trend}态势。"
                if net is not None else f"{region}框选片区 {span} 的{name}变化监测。"
            ),
            metrics=metrics,
            findings=self._findings(name, span, agg, rows, trend),
            recommendations=[
                f"建议对净增最显著的 patch（见清单）实地核验是否为新建设或施工扰动。",
                "如需更细节奏，可在月内多期之间逐月比对，捕捉短周期扰动过程。",
            ],
            narrative_blocks=[
                {
                    "title": "监测范围与口径",
                    "text": (
                        f"两期结果 PNG 逐像素对齐后按前景变化统计：新增＝后期为{name}前景、前期不是；"
                        f"减少反之。面积按相交 patch 的 UTM 面积换算，命中 {agg['patch_count']} 个 patch。"
                        "地图上红色为新增区域、蓝色为减少区域，未变化区域透明。"
                    ),
                },
            ],
            risks=self._risks(agg),
            method_notes=self._method_notes(task_id, before, after, model_id, name, model_info),
            limitations=self._limitations(model_id, before, after, model_info),
            confidence_notes=[
                "两期专题结果均来自在线海淀 embedding-api，PNG 逐像素对齐（128×128）。",
                "变化统计为 Agent 从结果图计算的轻量指标，正式评估仍需接入标签或评估接口。",
            ],
            data_source="haidian_embedding_api",
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            aef_payload={
                "scenario": "change",
                "task_id": task_id,
                "before": before,
                "after": after,
                "aggregate": agg,
                "top_patches": rows[:10],
                "custom_model_id": model_id,
                "custom_class": custom_class,
            },
            charts=charts,
            data_table=table,
            data_table_title=f"各 patch 净变化 TOP（公顷）" if table else "",
        )

    @staticmethod
    def _fmt_month(m: str) -> str:
        return f"{m[:4]}-{m[4:6]}" if len(m) >= 6 else m

    def _findings(self, name, span, agg, rows, trend) -> list[str]:
        out: list[str] = []
        net = agg["net_area_ha"]
        if net is not None:
            out.append(f"{span} 期间{name}净变化约 {net:+.0f} 公顷，整体{trend}。")
            out.append(f"其中新增约 {agg['gained_area_ha']:.0f} 公顷，减少约 {agg['lost_area_ha']:.0f} 公顷。")
        movers = [r for r in rows if r.get("net_ha")]
        if movers:
            top = movers[0]
            out.append(f"净增最显著的片区为 {top['label']}，约 {top['net_ha']:+.1f} 公顷，建议优先关注。")
        return out

    def _risks(self, agg) -> list[str]:
        gained = agg.get("gained_area_ha") or 0
        lost = agg.get("lost_area_ha") or 0
        if gained and lost and min(gained, lost) / max(gained, lost) > 0.6:
            return ["新增与减少面积相当，可能包含较多模型逐期抖动导致的伪变化，解读需谨慎。"]
        return []

    # Human-readable labels for the backend's training-method / feature codes.
    _METHOD_LABEL = {
        "xuannv_earth": "玄女地球 embedding", "traditional_ml": "传统 S2+随机森林",
        "aef": "AEF 年度特征+MLP", "dinov3_sat493m": "DINOv3-SAT493M+MLP",
        "pu_query_retrieval": "PU+Query 相似度召回", "binary_conv3x3": "Binary Conv 3x3 few-shot",
        "random_forest": "随机森林", "pixel_mlp": "像素级 MLP",
    }
    _FEATURE_LABEL = {
        "xuannv_embedding": "玄女地球 embedding", "sentinel2_l2a": "Sentinel-2 L2A 光学",
        "aef": "AEF 年度特征", "dinov3_sat493m": "DINOv3-SAT493M 特征",
    }

    def _model_method_note(self, model_info) -> str:
        """One sentence describing how the custom model was actually trained."""
        if model_info is None:
            return "该地物由少量标注样本训练/相似度召回得到。"
        method = self._METHOD_LABEL.get(
            model_info.resolved_training_method or model_info.requested_training_method, ""
        )
        feature = self._FEATURE_LABEL.get(model_info.feature_source, model_info.feature_source or "")
        bits = []
        if feature:
            bits.append(f"特征来源：{feature}")
        if method:
            bits.append(f"训练算法：{method}")
        if model_info.n_samples:
            bits.append(f"有效标注 {model_info.n_samples} 个多边形")
        if model_info.accuracy is not None:
            metric = model_info.metric_name or "训练集指标"
            bits.append(f"{metric}≈{model_info.accuracy:.3f}（训练集，非泛化精度）")
        return "自定义模型：" + "；".join(bits) + "。" if bits else "该地物由自定义模型识别。"

    def _method_notes(self, task_id, before, after, model_id, name, model_info=None) -> list[str]:
        if model_id:
            return [
                f"Agent 将请求识别为『{name}·建设扰动监测』（非内置地物），使用自定义模型 {model_id}。",
                f"调用海淀 embedding-api：{self.config.base_url}，逐 patch 用 POST /models/{model_id}/infer 出两期结果再做像素级 diff。",
                "自定义模型输出为“目标类=类别色、背景=灰底”的二值图；变化＝两期目标前景掩膜的逐像素差。",
                self._model_method_note(model_info),
            ]
        return [
            f"Agent 将请求识别为『建设扰动监测』场景，region=haidian、task={task_id}、{before}→{after}。",
            f"调用海淀 embedding-api：{self.config.base_url}，逐 patch 拉取两期结果做像素级 diff。",
            "变化＝两期二值前景掩膜的逐像素差；面积按整块 patch 的 UTM 面积换算。",
        ]

    @staticmethod
    def _same_year(before: str, after: str) -> bool:
        digits = lambda m: "".join(ch for ch in str(m) if ch.isdigit())
        b, a = digits(before), digits(after)
        return len(b) >= 4 and len(a) >= 4 and b[:4] == a[:4]

    def _limitations(self, model_id, before="", after="", model_info=None) -> list[str]:
        common = [
            "变化来自两期模型输出之差，模型逐期误差会叠加为伪变化，显著斑块建议实地核验。",
            "面积按整块 patch 聚合，AOI 边界未做像素级裁剪，边缘片区略有高估。",
        ]
        if model_id:
            common.insert(
                0,
                "该地物由少量标注样本训练/相似度召回得到，精度仅供参考，不代表官方产品级结果。",
            )
            # AEF features are per-year: two months in the same year share one
            # annual embedding, so their model outputs are identical → any
            # "change" would be spurious. Warn loudly rather than report zeros.
            if model_info is not None and model_info.uses_annual_feature and self._same_year(before, after):
                common.insert(
                    0,
                    "⚠️ 该模型使用 AEF 年度特征，同一年内各月份共用同一年度 embedding，"
                    "因此本次同年两期对比的模型输出实际相同，变化结果无意义；"
                    "如需年内变化，请改用玄女地球/DINOv3 特征训练的模型，或改为跨年度对比。",
                )
        return common
