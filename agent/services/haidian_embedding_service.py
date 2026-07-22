from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from PIL import Image

from agent.config import EmbeddingAPIConfig, ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportRequest
from agent.services.common import bbox_intersection_score
from agent.services.http_client import JsonHttpClient
from agent.services.satellite_basemap import basemap_chart
from agent.taxonomy import TASK_TO_HAIDIAN
from agent.tools.aoi import aggregate_binary_coverage, aggregate_class_distribution
from agent.tools.classmap import class_distribution, normalize_legend
from agent.tools.raster import binary_coverage


TASK_DISPLAY = {
    "building_extraction": "建筑物提取",
    "road_extraction": "道路提取",
    "construction": "施工地检测",
    "land_use_classification": "土地利用分类",
    "land_cover_classification": "土地覆盖分类",
    "water_extraction": "水体提取",
}

TASK_DESCRIPTIONS = {
    "building_extraction": "识别建筑物与高密度人工地表，适合城市更新、建设强度和地块开发研判。",
    "road_extraction": "识别道路与线性交通廊道，适合观察路网连通性、道路边界和建设区骨架。",
    "construction": "识别施工地或疑似施工扰动区域，适合城市建设动态、裸土扰动和工地分布排查。",
    "land_use_classification": "区分土地利用结构，适合做城市功能用地和综合空间格局分析。",
    "land_cover_classification": "区分地表覆盖类型，适合观察建设地、植被、水体等地表覆盖差异。",
    "water_extraction": "识别水体范围，适合观察河湖水面、湿地水域和水陆边界。",
}

BINARY_TASKS = {"building_extraction", "road_extraction", "construction", "water_extraction"}


def _stable_pick(items: list[dict[str, Any]], key: str, count: int) -> list[dict[str, Any]]:
    count = max(1, min(count, len(items)))
    return sorted(
        items,
        key=lambda item: hashlib.sha1(f"{key}:{item.get('patch_id')}".encode("utf-8")).hexdigest(),
    )[:count]


def _api_month(time_range: str) -> str:
    return str(time_range or "").replace("-", "")


class HaidianEmbeddingAnalysisService:
    """Analysis service backed by the Haidian regional embedding API."""

    def __init__(
        self,
        config: EmbeddingAPIConfig | None = None,
        report_config: ReportConfig | None = None,
    ) -> None:
        self.config = config or EmbeddingAPIConfig()
        self.report_config = report_config or ReportConfig()
        self.asset_dir = self.report_config.asset_dir
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.http = JsonHttpClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            error_prefix="海淀 embedding-api 调用失败",
        )
        # Cache class legends per task (id/name/color rarely change within a run).
        self._legend_cache: dict[str, list[dict[str, Any]]] = {}

    def _legend(self, task_id: str) -> list[dict[str, Any]]:
        if task_id not in self._legend_cache:
            from urllib.parse import urlencode

            query = urlencode({"region_id": "haidian"})
            raw = self.http.get_list_optional(f"/system-models/{task_id}/classes?{query}")
            self._legend_cache[task_id] = normalize_legend(raw)
        return self._legend_cache[task_id]

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        task_id = self._normalize_task(request.task)
        month = _api_month(request.time_range)
        if not month:
            raise RuntimeError("海淀专题分析需要明确月份，例如 2025年12月。")

        patches, rejected_patch_ids, omitted_patch_ids = self._select_patches(request, task_id, month)
        selection_source = str(patches[0].get("_agent_selection_source") or "") if patches else ""
        patch_results: list[dict[str, Any]] = []
        result_assets: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
        for patch in patches:
            patch_id = str(patch.get("patch_id") or "")
            try:
                result_asset = self._download_remote_asset(
                    self._task_result_url(patch_id, task_id, month),
                    request,
                    task_id,
                    f"task_result_{patch_id}",
                )
                image_stats = self._image_stats(result_asset, task_id, patch)
                if not image_stats:
                    raise RuntimeError("结果图无法读取或没有有效像素统计")
            except Exception as exc:
                rejected_patch_ids.append(patch_id)
                patch_results.append({
                    "patch_id": patch_id,
                    "status": "failed",
                    "error": str(exc),
                    "bounds_wgs84": patch.get("bounds_wgs84") or [],
                })
                continue
            result_assets.append((patch, result_asset, image_stats))
            patch_results.append({
                "patch_id": patch_id,
                "status": "ok",
                "bounds_wgs84": patch.get("bounds_wgs84") or [],
                "metrics": image_stats,
            })

        if not result_assets:
            rejected = ", ".join(rejected_patch_ids) or "所选范围"
            raise RuntimeError(f"海淀专题结果获取失败：{rejected}，请更换 patch 或月份后重试。")

        embedding_asset: Path | None = None
        try:
            first_patch_id = str(result_assets[0][0].get("patch_id") or "")
            embedding_asset = self._download_remote_asset(
                self._embedding_url(first_patch_id, month), request, task_id, "embedding"
            )
        except Exception:
            embedding_asset = None
        task_summary = self._get_json_optional(f"/regions/haidian/tasks/{task_id}/summary?version=v1")
        image_stats = self._aggregate_image_stats(task_id, [stats for _, _, stats in result_assets])
        task_display = TASK_DISPLAY.get(task_id, request.task)
        success_patches = [patch for patch, _, _ in result_assets]
        used_patch_ids = [str(patch.get("patch_id") or "") for patch in success_patches]
        basemap = basemap_chart(
            self._union_wgs84_bounds(success_patches), self.asset_dir, f"haidian-{sorted(used_patch_ids)}"
        )
        charts: list[ChartAsset] = [*([basemap] if basemap else [])]
        # Multi-patch: mosaic the result PNGs into one seamless map layer by
        # their UTM grid. Falls back to per-patch overlays when there's a single
        # patch or the tiles can't be stitched (mixed resolution / missing bounds).
        mosaic_asset = self._stitch_patches(result_assets, task_id) if len(result_assets) > 1 else None
        union_bounds = self._union_wgs84_bounds(success_patches) or []
        if mosaic_asset is not None:
            charts.append(ChartAsset(
                title=f"{task_display}专题结果（{len(used_patch_ids)} patch 拼接）",
                kind="image",
                url=self._asset_url(mosaic_asset),
                caption=(
                    f"海淀在线专题服务返回的 {task_display} 结果图，"
                    f"已按 UTM 网格将 {len(used_patch_ids)} 个 patch 拼接为一张连续图（{', '.join(used_patch_ids[:8])}）。"
                ),
                bounds_wgs84=[float(v) for v in union_bounds][:4],
                overlay=len(union_bounds) == 4,
                patch_id=",".join(used_patch_ids),
            ))
        else:
            for patch, result_asset, _ in result_assets:
                patch_id = str(patch.get("patch_id") or "")
                bounds = [float(v) for v in (patch.get("bounds_wgs84") or [])][:4]
                charts.append(ChartAsset(
                    title=f"{task_display}专题结果 · {patch_id}",
                    kind="image",
                    url=self._asset_url(result_asset),
                    caption=f"海淀在线专题服务返回的 {task_display} patch 级结果图（{patch_id}）。",
                    bounds_wgs84=bounds,
                    overlay=len(bounds) == 4,
                    patch_id=patch_id,
                ))
        if embedding_asset is not None:
            charts.append(ChartAsset(
                title="Embedding 可视化预览",
                kind="image",
                url=self._asset_url(embedding_asset),
                caption=f"代表性 patch（{used_patch_ids[0]}）的 embedding RGB 预览图。",
                patch_id=used_patch_ids[0],
            ))
        status_text = f"成功处理 {len(used_patch_ids)} 个 patch"
        if rejected_patch_ids:
            status_text += f"，{len(rejected_patch_ids)} 个 patch 获取失败"
        if omitted_patch_ids:
            status_text += f"，另有 {len(omitted_patch_ids)} 个超出处理上限"

        return AnalysisResult(
            task=task_display,
            region="北京市海淀区",
            time_range=request.time_range,
            headline=f"北京市海淀区{request.time_range}{task_display}遥感分析",
            summary=self._multi_summary_text(request, task_id, success_patches, image_stats, status_text),
            metrics=self._build_multi_metrics(request, task_display, task_id, month, image_stats, status_text),
            findings=self._build_multi_findings(request, task_id, success_patches, image_stats, task_summary),
            recommendations=self._build_recommendations(task_id),
            narrative_blocks=[
                {
                    "title": "区域与 Patch",
                    "text": (
                        f"Agent 已将请求标准化为 region=haidian、task={task_id}、month={month}，"
                        f"并定位到 {len(used_patch_ids)} 个 patch（{', '.join(used_patch_ids[:8])}）。"
                        f"{self._selection_label(selection_source)}。"
                    ),
                },
                {
                    "title": "专题结果",
                    "text": (
                        f"本次报告直接调用海淀在线专题结果接口，获得 {len(used_patch_ids)} 张 {task_display} PNG 结果图，"
                        "并按 patch 面积汇总指标；embedding 预览图仅展示代表性 patch。"
                    ),
                },
            ],
            risks=self._build_multi_risks(task_id, image_stats, rejected_patch_ids, omitted_patch_ids),
            method_notes=[
                f"Agent 将用户需求标准化为 region=haidian、task={task_id}、month={month}。",
                f"本次调用海淀 embedding-api：{self.config.base_url}。",
                self._selection_note(selection_source, ", ".join(used_patch_ids)),
                "海淀 system-models 推理接口当前未开放，本服务使用 patch 专题结果接口形成闭环。",
            ],
            limitations=[
                "面积和占比按成功获取结果的完整 patch 汇总；AOI 与 patch 边界不完全重合时，暂按整块 patch 估算。",
                "在线接口返回的是结果 PNG，暂无类别置信度、逐像素概率和完整图例元数据。",
                "多 patch 结果按 UTM 网格拼接为一张连续图；非相邻选区之间的空缺保持透明，不做插值填充。",
            ],
            confidence_notes=[
                "专题结果图来自在线海淀 embedding-api；embedding 服务可用时附带代表性 patch 预览。",
                "报告中的图像统计为 Agent 从 PNG 结果图中提取的轻量指标，正式业务评估仍需接入标签或评估接口。",
            ],
            data_table=image_stats.get("class_distribution") or [],
            data_table_title=(f"{task_display}·多 patch 类别覆盖占比" if image_stats.get("class_distribution") else ""),
            data_source="haidian_embedding_api",
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            aef_payload={
                "service": self.config.base_url,
                "region_id": "haidian",
                "task": task_id,
                "version": "v1",
                "month": month,
                "patches": success_patches,
                "patch_count": len(used_patch_ids),
                "patch_results": patch_results,
                "requested_patch_ids": list(request.selected_patch_ids),
                "used_patch_ids": used_patch_ids,
                "failed_patch_ids": rejected_patch_ids,
                "omitted_patch_ids": omitted_patch_ids,
                "selected_patch_ids": request.selected_patch_ids,
                "aoi": request.aoi,
                "patch_selection_source": selection_source,
                "task_api_status": "available",
                "task_summary": task_summary,
                "image_stats": image_stats,
                "fingerprint": self._fingerprint(
                    task_id,
                    ",".join(sorted(used_patch_ids)),
                    month,
                    {"summary": task_summary, "image": image_stats},
                ),
            },
            charts=charts,
            patch_results=patch_results,
        )

    def _normalize_task(self, task: str) -> str:
        task_id = TASK_TO_HAIDIAN.get(task)
        if task_id is None:
            supported = "建筑物提取、道路提取、施工识别、土地利用分类、土地覆盖分类、水体提取"
            raise RuntimeError(f"北京市海淀区暂不支持“{task}”，当前可用任务为：{supported}。")
        return task_id

    def _select_patches(
        self, request: ReportRequest, task_id: str, month: str
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        limit = max(1, int(self.config.max_selected_patches))
        if request.selected_patch_ids:
            unique_ids = list(dict.fromkeys(str(item) for item in request.selected_patch_ids if str(item).strip()))
            selected: list[dict[str, Any]] = []
            rejected: list[str] = []
            for patch_id in unique_ids[:limit]:
                try:
                    patch = self._get_json(f"/regions/haidian/patches/{patch_id}")
                except RuntimeError:
                    rejected.append(patch_id)
                    continue
                if self._is_usable_patch(patch, task_id, month):
                    patch["_agent_selection_source"] = "selected_patch_month_task_valid"
                    selected.append(patch)
                else:
                    rejected.append(patch_id)
            if selected:
                return selected, rejected, unique_ids[limit:]
            raise RuntimeError(
                f"前端选择的海淀 patch 均不支持 {month} / {task_id}，请重新选择月份、任务或 patch。"
            )

        aoi_patches = self._select_patches_from_aoi(request, task_id, month, limit)
        if aoi_patches:
            for patch in aoi_patches:
                patch["_agent_selection_source"] = "aoi_month_task_search"
            return aoi_patches, [], []

        patches = []
        page = 1
        while True:
            payload = self._get_json(f"/regions/haidian/patches?page={page}&page_size=100")
            for patch in payload.get("patches") or []:
                if self._is_usable_patch(patch, task_id, month):
                    patches.append(patch)
            if not payload.get("has_next"):
                break
            page += 1
        if not patches:
            raise RuntimeError(f"没有找到支持 {month} / {task_id} 的海淀 patch。")
        selected = _stable_pick(patches, f"{request.region}-{request.task}-{month}-{task_id}", limit)
        for patch in selected:
            patch["_agent_selection_source"] = "global_month_task_search"
        return selected, [], []

    def _select_patch(self, request: ReportRequest, task_id: str, month: str) -> dict[str, Any]:
        """Compatibility helper for callers that still expect one patch."""
        patches, _, _ = self._select_patches(request, task_id, month)
        return patches[0]

    def _select_patches_from_aoi(
        self, request: ReportRequest, task_id: str, month: str, limit: int
    ) -> list[dict[str, Any]]:
        bbox = self._aoi_bbox(request.aoi)
        if not bbox:
            return []
        candidates: list[dict[str, Any]] = []
        page = 1
        query_base = {
            "page_size": 100,
            "bbox": ",".join(f"{value:.8f}" for value in bbox),
        }
        while True:
            query = urlencode({**query_base, "page": page})
            payload = self._get_json(f"/regions/haidian/patches?{query}")
            for patch in payload.get("patches") or []:
                if not self._is_usable_patch(patch, task_id, month):
                    continue
                patch_bbox = patch.get("bounds_wgs84") or []
                score = bbox_intersection_score([float(v) for v in patch_bbox], bbox)
                item = dict(patch)
                item["_agent_aoi_score"] = round(score, 6)
                candidates.append(item)
            if not payload.get("has_next"):
                break
            page += 1
        if not candidates:
            return []
        candidates.sort(key=lambda item: (item.get("_agent_aoi_score", 0), item.get("patch_id", "")), reverse=True)
        return candidates[:limit]

    def _aoi_bbox(self, aoi: dict[str, Any]) -> list[float]:
        if not isinstance(aoi, dict):
            return []
        coordinates = aoi.get("coordinates")
        if aoi.get("type") != "bbox" or not isinstance(coordinates, list) or len(coordinates) != 4:
            return []
        try:
            bbox = [float(value) for value in coordinates]
        except (TypeError, ValueError):
            return []
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            return []
        return bbox

    def _is_usable_patch(self, patch: dict[str, Any], task_id: str, month: str) -> bool:
        if not patch.get("has_embedding"):
            return False
        months = [str(item) for item in patch.get("available_months") or []]
        if not any(item == month or item.startswith(month) for item in months):
            return False
        tasks = patch.get("available_tasks") or []
        return not tasks or task_id in tasks

    def _embedding_url(self, patch_id: str, month: str) -> str:
        query = urlencode({"format": "png", "version": "v1", "month": month})
        return f"/regions/haidian/patches/{patch_id}/embedding?{query}"

    def _task_result_url(self, patch_id: str, task_id: str, month: str) -> str:
        query = urlencode({"format": "png", "version": "v1", "month": month})
        return f"/regions/haidian/patches/{patch_id}/tasks/{task_id}/result?{query}"

    def _get_json(self, path: str) -> Any:
        return self.http.get_json(path)

    def _get_json_optional(self, path: str) -> dict[str, Any]:
        return self.http.get_json_optional(path)

    def _download_remote_asset(self, remote_url: str, request: ReportRequest, task_id: str, kind: str) -> Path:
        source_url = self.http._url(remote_url)
        digest = hashlib.sha1(f"{source_url}-{request.time_range}-{task_id}-{kind}".encode("utf-8")).hexdigest()[:12]
        out_path = self.asset_dir / f"haidian_{task_id}_{kind}_{digest}.png"
        return self.http.download(remote_url, out_path, asset_label="海淀专题图像")

    def _asset_url(self, path: Path) -> str:
        return f"/reports/assets/{path.name}"

    def _image_stats(self, image_path: Path, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        try:
            with Image.open(image_path) as image:
                rgb = image.convert("RGB")
                colors = rgb.getcolors(maxcolors=1_000_000) or []
                total = rgb.width * rgb.height
        except OSError:
            return {}

        stats: dict[str, Any] = {
            "width": rgb.width,
            "height": rgb.height,
            "unique_colors": len(colors),
        }
        if colors:
            colors.sort(reverse=True)
            dominant_count, dominant_rgb = colors[0]
            stats["dominant_color"] = f"rgb{dominant_rgb}"
            stats["dominant_ratio"] = round(dominant_count / max(total, 1), 4)
            # Binary tasks: fixed background per task + exact UTM area (P3.1 tools),
            # replacing the old "dominant colour = background" heuristic that flipped
            # once the target covered >50% of the patch.
            coverage = binary_coverage(task_id, colors, patch.get("bounds"))
            if coverage:
                stats.update({k: v for k, v in coverage.items() if v is not None})
            else:
                # Multiclass task: map colours to the authoritative API legend and
                # produce per-class shares (+ area). We report the model's output
                # faithfully under correct labels; model accuracy is upstream.
                distribution = class_distribution(colors, self._legend(task_id), patch.get("bounds"))
                if distribution:
                    stats["class_distribution"] = distribution
        return stats

    def _aggregate_image_stats(self, task_id: str, per_patch: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate image-derived metrics without averaging patch ratios."""
        if task_id in BINARY_TASKS:
            summary = aggregate_binary_coverage(per_patch)
            summary["patch_count"] = len(per_patch)
            return summary

        distribution = aggregate_class_distribution(
            [stats.get("class_distribution") or [] for stats in per_patch]
        )
        return {"patch_count": len(per_patch), "class_distribution": distribution}

    def _stitch_patches(
        self,
        result_assets: list[tuple[dict[str, Any], Path, dict[str, Any]]],
        task_id: str,
    ) -> Path | None:
        """Mosaic per-patch result PNGs into one image by their UTM grid.

        Haidian patches are regular 1280 m tiles on a fixed UTM grid, so this is
        a pure paste — no reprojection or resampling. Each patch is placed at
        its metre offset from the union's top-left corner, scaled by the shared
        pixels-per-metre of the result PNGs. Gaps (non-contiguous selections)
        stay transparent. Returns None if bounds/sizes are missing or
        inconsistent, so callers fall back to per-patch overlays.
        """
        tiles: list[tuple[list[float], Path, int, int]] = []
        for patch, asset_path, _ in result_assets:
            bounds = patch.get("bounds") or []
            if not (isinstance(bounds, list) and len(bounds) == 4):
                return None
            try:
                with Image.open(asset_path) as image:
                    w, h = image.size
            except OSError:
                return None
            if w <= 0 or h <= 0:
                return None
            tiles.append(([float(v) for v in bounds], asset_path, w, h))
        if len(tiles) < 2:
            return None

        # Shared resolution: every tile must agree on pixels-per-metre, else the
        # grid paste would misalign — bail to per-patch overlays if they differ.
        def _res(tile: tuple[list[float], Path, int, int]) -> tuple[float, float]:
            (xmin, ymin, xmax, ymax), _, w, h = tile
            return (w / (xmax - xmin), h / (ymax - ymin))

        px_per_m_x, px_per_m_y = _res(tiles[0])
        for tile in tiles[1:]:
            rx, ry = _res(tile)
            if abs(rx - px_per_m_x) > 1e-6 or abs(ry - px_per_m_y) > 1e-6:
                return None

        union_xmin = min(t[0][0] for t in tiles)
        union_ymin = min(t[0][1] for t in tiles)
        union_xmax = max(t[0][2] for t in tiles)
        union_ymax = max(t[0][3] for t in tiles)
        canvas_w = round((union_xmax - union_xmin) * px_per_m_x)
        canvas_h = round((union_ymax - union_ymin) * px_per_m_y)
        if canvas_w <= 0 or canvas_h <= 0 or canvas_w * canvas_h > 64_000_000:
            return None

        canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        for (xmin, ymin, xmax, ymax), asset_path, w, h in tiles:
            # Column offset from the union's left edge; row offset from its TOP
            # edge (image y grows downward, UTM y grows upward — hence ymax).
            left = round((xmin - union_xmin) * px_per_m_x)
            top = round((union_ymax - ymax) * px_per_m_y)
            try:
                with Image.open(asset_path) as image:
                    canvas.paste(image.convert("RGBA"), (left, top))
            except OSError:
                return None

        digest = hashlib.sha1(
            f"{task_id}:{sorted(str(t[1].name) for t in tiles)}".encode("utf-8")
        ).hexdigest()[:12]
        out_path = self.asset_dir / f"haidian_{task_id}_mosaic_{digest}.png"
        try:
            canvas.save(out_path)
        except OSError:
            return None
        return out_path

    @staticmethod
    def _union_wgs84_bounds(patches: list[dict[str, Any]]) -> list[float] | None:
        bounds = [patch.get("bounds_wgs84") or [] for patch in patches]
        valid = [
            [float(value) for value in item[:4]]
            for item in bounds
            if isinstance(item, list) and len(item) == 4
        ]
        if not valid:
            return None
        return [
            min(item[0] for item in valid),
            min(item[1] for item in valid),
            max(item[2] for item in valid),
            max(item[3] for item in valid),
        ]

    def _multi_summary_text(
        self,
        request: ReportRequest,
        task_id: str,
        patches: list[dict[str, Any]],
        image_stats: dict[str, Any],
        status_text: str,
    ) -> str:
        task_display = TASK_DISPLAY.get(task_id, request.task)
        ids = ", ".join(str(patch.get("patch_id") or "") for patch in patches[:6])
        if len(patches) > 6:
            ids += f" 等 {len(patches)} 个"
        if image_stats.get("coverage_ratio") is not None:
            ratio = float(image_stats["coverage_ratio"]) * 100
            covered = image_stats.get("covered_area_ha")
            total = image_stats.get("total_area_ha")
            measure = f"目标覆盖率约 {ratio:.2f}%"
            if covered is not None and total is not None:
                measure += f"，目标面积约 {float(covered):.2f} 公顷（总计 {float(total):.2f} 公顷）"
        elif image_stats.get("class_distribution"):
            top = image_stats["class_distribution"][0]
            measure = f"占比最高的类别为“{top.get('label', '未命名')}”，约 {float(top.get('ratio') or 0) * 100:.2f}%"
        else:
            measure = "当前结果图已获取，但缺少可汇总的面积字段"
        return (
            f"本次调用海淀在线 embedding-api，对 {request.time_range} 的 {task_display} 处理 {len(patches)} 个 patch"
            f"（{ids}）。{measure}。{status_text}。"
            "指标按各 patch 的结果和面积汇总，适合用于当前地图选区的整体判断。"
        )

    def _build_multi_metrics(
        self,
        request: ReportRequest,
        task_display: str,
        task_id: str,
        month: str,
        image_stats: dict[str, Any],
        status_text: str,
    ) -> list[MetricCard]:
        cards = [
            MetricCard("任务", task_display, "Agent 识别后的海淀专题任务"),
            MetricCard("地区", "北京市海淀区", "海淀 embedding-api 区域标识 haidian"),
            MetricCard("时间", request.time_range, f"接口月份参数为 {month}"),
            MetricCard("处理 patch", str(image_stats.get("patch_count", 0)), "本次报告实际完成结果统计的 patch 数"),
            MetricCard("处理状态", status_text, "包含成功、失败和上限截断情况"),
        ]
        if image_stats.get("total_area_ha") is not None:
            cards.append(MetricCard("合计面积", f"{float(image_stats['total_area_ha']):.2f} 公顷", "按成功 patch 的投影边界合计"))
        if image_stats.get("covered_area_ha") is not None:
            cards.append(MetricCard("目标面积", f"{float(image_stats['covered_area_ha']):.2f} 公顷", "按结果图像素比例估算"))
        if image_stats.get("coverage_ratio") is not None:
            cards.append(MetricCard("目标覆盖率", f"{float(image_stats['coverage_ratio']) * 100:.2f}%", "目标面积 / 合计面积"))
        if image_stats.get("class_distribution"):
            cards.append(MetricCard("类别数", str(len(image_stats["class_distribution"])), "结果图中可识别的类别数"))
        return cards

    def _build_multi_findings(
        self,
        request: ReportRequest,
        task_id: str,
        patches: list[dict[str, Any]],
        image_stats: dict[str, Any],
        task_summary: dict[str, Any],
    ) -> list[str]:
        task_display = TASK_DISPLAY.get(task_id, request.task)
        findings = [
            f"北京市海淀区 {request.time_range} 的 {task_display} 已完成 {len(patches)} 个 patch 的结果处理。",
            "结果指标按 patch 面积汇总，避免直接平均不同面积 patch 的覆盖率。",
            TASK_DESCRIPTIONS.get(task_id, f"{task_display} 适合用于城市遥感专题分析。"),
        ]
        if image_stats.get("coverage_ratio") is not None:
            findings.append(f"多 patch 汇总后的目标覆盖率约为 {float(image_stats['coverage_ratio']) * 100:.2f}%。")
        elif image_stats.get("class_distribution"):
            findings.append(f"多 patch 汇总后形成 {len(image_stats['class_distribution'])} 类地物分布，可在报告表格中查看。")
        if task_summary.get("total_patches") is not None:
            findings.append(
                f"接口可用性摘要显示该任务在全区覆盖 {task_summary.get('total_patches')} 个 patch；"
                "该数字只说明服务数据覆盖情况，不参与本次选区统计，也不能解释为选区空间特征。"
            )
        return findings

    def _build_multi_risks(
        self,
        task_id: str,
        image_stats: dict[str, Any],
        rejected_patch_ids: list[str],
        omitted_patch_ids: list[str],
    ) -> list[str]:
        risks = ["面积统计按完整 patch 边界估算，AOI 只覆盖 patch 一部分时可能高估或低估实际 AOI 面积。"]
        if rejected_patch_ids:
            risks.append(f"以下 patch 未能获取有效结果，未计入合计指标：{', '.join(rejected_patch_ids)}。")
        if omitted_patch_ids:
            risks.append(f"以下 patch 超出本次处理上限，未参与分析：{', '.join(omitted_patch_ids)}。")
        if task_id == "road_extraction":
            risks.append("道路提取容易受树荫、建筑边缘和细长纹理影响，正式应用需要和底图路网对照。")
        elif task_id == "construction":
            risks.append("施工地与裸土、拆迁地、硬化地表可能混淆，建议结合时间序列验证。")
        elif task_id in {"land_use_classification", "land_cover_classification"}:
            risks.append("多类别图目前缺少服务端置信度和精度指标，类别统计应作为研判参考。")
        return risks

    def _build_metrics(
        self,
        request: ReportRequest,
        task_display: str,
        task_id: str,
        patch: dict[str, Any],
        month: str,
        task_summary: dict[str, Any],
        image_stats: dict[str, Any],
    ) -> list[MetricCard]:
        bounds = patch.get("bounds_wgs84") or []
        bounds_text = "暂无"
        if len(bounds) == 4:
            bounds_text = f"{bounds[0]:.4f},{bounds[1]:.4f} - {bounds[2]:.4f},{bounds[3]:.4f}"
        cards = [
            MetricCard("任务", task_display, "Agent 识别后的海淀专题任务"),
            MetricCard("地区", "北京市海淀区", "海淀 embedding-api 区域标识 haidian"),
            MetricCard("时间", request.time_range, f"接口月份参数为 {month}"),
            MetricCard("接口状态", "专题结果可用", "当前使用海淀在线专题结果接口"),
            MetricCard("Patch", str(patch.get("patch_id") or "暂无"), self._selection_label(str(patch.get("_agent_selection_source") or ""))),
            MetricCard("可用月份", str(len(patch.get("available_months") or [])), "当前 patch 可查询的 embedding 月份数"),
            MetricCard("经纬度范围", bounds_text, "当前 patch 的 WGS84 边界"),
            MetricCard("专题 ID", task_id, "海淀在线 API 使用的任务标识"),
        ]
        if image_stats.get("width") and image_stats.get("height"):
            cards.append(
                MetricCard(
                    "结果尺寸",
                    f"{image_stats['width']}x{image_stats['height']}",
                    "专题结果 PNG 的像素尺寸",
                )
            )
        if image_stats.get("unique_colors") is not None:
            label = "颜色/类别"
            cards.append(MetricCard(label, str(image_stats["unique_colors"]), "从专题 PNG 统计得到的颜色数量"))
        if image_stats.get("foreground_ratio") is not None:
            cards.append(
                MetricCard(
                    "目标占比",
                    f"{float(image_stats['foreground_ratio']) * 100:.2f}%",
                    "二值专题图中目标（非背景）像素占该 patch 的比例",
                )
            )
        if image_stats.get("covered_area_ha") is not None:
            total_ha = image_stats.get("total_area_ha")
            desc = (
                f"按 patch 覆盖面积 {float(total_ha):.2f} 公顷估算"
                if total_ha is not None
                else "按 patch 覆盖面积估算"
            )
            cards.append(
                MetricCard(
                    "目标面积",
                    f"{float(image_stats['covered_area_ha']):.2f} 公顷",
                    desc,
                )
            )
        if task_summary:
            if task_summary.get("total_patches") is not None:
                cards.append(MetricCard("统计 Patch", str(task_summary.get("total_patches")), "专题结果覆盖的 patch 数"))
            if task_summary.get("positive_patches") is not None:
                cards.append(MetricCard("正样本 Patch", str(task_summary.get("positive_patches")), "专题结果中包含目标对象的 patch 数"))
        return cards

    def _summary_text(self, request: ReportRequest, task_id: str, patch: dict[str, Any], image_stats: dict[str, Any]) -> str:
        task_display = TASK_DISPLAY.get(task_id, request.task)
        stat_text = ""
        if image_stats.get("foreground_ratio") is not None:
            ratio_pct = float(image_stats["foreground_ratio"]) * 100
            covered_ha = image_stats.get("covered_area_ha")
            if covered_ha is not None:
                stat_text = (
                    f"结果图中目标覆盖约 {ratio_pct:.2f}%，"
                    f"对应该 patch 约 {float(covered_ha):.2f} 公顷。"
                )
            else:
                stat_text = f"结果图中目标像素占比约 {ratio_pct:.2f}%。"
        elif image_stats.get("unique_colors") is not None:
            stat_text = f"结果图包含 {image_stats['unique_colors']} 类颜色表达。"
        description = TASK_DESCRIPTIONS.get(task_id, "").rstrip("。")
        return (
            f"本次调用海淀在线 embedding-api，对 {request.time_range} 的 {patch.get('patch_id')} "
            f"执行{task_display}。{description}。{stat_text}"
            "可用于验证地图选区、patch 定位、专题结果获取和报告生成的完整闭环。"
        )

    def _build_findings(
        self,
        request: ReportRequest,
        task_id: str,
        patch: dict[str, Any],
        image_stats: dict[str, Any],
        task_summary: dict[str, Any],
    ) -> list[str]:
        task_display = TASK_DISPLAY.get(task_id, request.task)
        findings = [
            f"北京市海淀区 {request.time_range} 的 {task_display} 请求已成功路由到在线专题结果接口。",
            f"当前 patch 为 {patch.get('patch_id')}，可用月份示例包括 {', '.join((patch.get('available_months') or [])[:8])}。",
            TASK_DESCRIPTIONS.get(task_id, f"{task_display} 适合用于城市遥感专题分析。"),
        ]
        if image_stats.get("foreground_ratio") is not None:
            findings.append(f"专题 PNG 显示目标像素占比约 {float(image_stats['foreground_ratio']) * 100:.2f}%，适合快速判断当前 patch 是否包含明显目标。")
        elif image_stats.get("unique_colors") is not None:
            findings.append(f"专题 PNG 共统计到 {image_stats['unique_colors']} 种颜色表达，可作为多类别结果的图像级概览。")
        if task_summary.get("total_patches") is not None:
            findings.append(
                f"接口摘要显示该任务覆盖 {task_summary.get('total_patches')} 个 patch，"
                f"其中正样本 patch 为 {task_summary.get('positive_patches', '暂无')} 个。"
            )
        return findings

    def _build_recommendations(self, task_id: str) -> list[str]:
        common = [
            "建议优先结合地图图层和 patch 明细定位高值区域，再回到原始影像复核边界和疑似误检。",
            "需要严格 AOI 面积时，应进一步按 AOI 边界裁切 patch 像素后再统计。",
        ]
        if task_id == "building_extraction":
            return ["建筑物提取结果建议叠加地块、道路或行政边界，提高城市建设解读能力。", *common]
        if task_id == "road_extraction":
            return ["道路提取结果建议叠加底图路网核验连续性，重点检查断裂、阴影和细线误检。", *common]
        if task_id == "construction":
            return ["施工地检测建议结合多月份结果，区分短期扰动、裸土和稳定工地。", *common]
        if task_id == "water_extraction":
            return ["水体提取建议结合季节与降雨背景复核，区分稳定水面和临时积水。", *common]
        return ["多类别分类任务建议补充图例元数据和类别面积统计，让报告从图像展示升级为定量分析。", *common]

    def _build_risks(self, task_id: str, image_stats: dict[str, Any]) -> list[str]:
        risks = ["当前为单 patch 结果，不能直接代表整个海淀区。"]
        if task_id in BINARY_TASKS and image_stats.get("foreground_ratio") == 0:
            risks.append("当前 patch 的二值结果几乎没有目标像素，建议前端允许用户更换 patch 或扩大 AOI。")
        if task_id == "road_extraction":
            risks.append("道路提取容易受树荫、建筑边缘和细长纹理影响，正式应用需要和底图路网对照。")
        elif task_id == "construction":
            risks.append("施工地与裸土、拆迁地、硬化地表可能混淆，建议结合时间序列验证。")
        elif task_id in {"land_use_classification", "land_cover_classification"}:
            risks.append("多类别图目前缺少服务端图例和精度指标，颜色含义需要后续接口补充。")
        return risks

    def _selection_label(self, source: str) -> str:
        labels = {
            "selected_patch_month_task_valid": "前端地图选中的 patch，已通过最终月份和任务校验",
            "aoi_reselected_after_month_task": "前端候选 patch 月份或任务不匹配，已按同一 AOI 和最终条件重选",
            "aoi_month_task_search": "按前端 AOI、最终月份和最终任务选择的 patch",
            "global_month_task_search": "未提供 AOI 时按地区、月份和任务临时选择的 patch",
        }
        return labels.get(source, "最终条件校验后的 patch")

    def _selection_note(self, source: str, patch_id: str) -> str:
        return f"本次 patch 选择方式：{self._selection_label(source)}；最终使用 {patch_id}。"

    def _fingerprint(self, task_id: str, patch_id: str, month: str, result: dict[str, Any]) -> str:
        raw = json.dumps(
            {"task": task_id, "patch": patch_id, "month": month, "version": "v1", "result": result},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
