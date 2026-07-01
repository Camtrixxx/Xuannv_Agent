from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image

from agent.config import EmbeddingAPIConfig, ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportRequest
from agent.services.patch_selection_service import TASK_TO_HAIDIAN, _bbox_intersection_score


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
        self.opener = build_opener(ProxyHandler({}))

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        task_id = self._normalize_task(request.task)
        month = _api_month(request.time_range)
        if not month:
            raise RuntimeError("海淀专题分析需要明确月份，例如 2025年12月。")

        patch = self._select_patch(request, task_id, month)
        patch_id = str(patch["patch_id"])
        selection_source = str(patch.get("_agent_selection_source") or "")

        result_asset = self._download_remote_asset(
            self._task_result_url(patch_id, task_id, month),
            request,
            task_id,
            "task_result",
        )
        embedding_asset = self._download_remote_asset(
            self._embedding_url(patch_id, month),
            request,
            task_id,
            "embedding",
        )
        task_summary = self._get_json_optional(f"/regions/haidian/tasks/{task_id}/summary?version=v1")
        image_stats = self._image_stats(result_asset, task_id)
        task_display = TASK_DISPLAY.get(task_id, request.task)

        return AnalysisResult(
            task=task_display,
            region="北京市海淀区",
            time_range=request.time_range,
            headline=f"北京市海淀区{request.time_range}{task_display}遥感分析",
            summary=self._summary_text(request, task_id, patch, image_stats),
            metrics=self._build_metrics(request, task_display, task_id, patch, month, task_summary, image_stats),
            findings=self._build_findings(request, task_id, patch, image_stats, task_summary),
            recommendations=self._build_recommendations(task_id),
            narrative_blocks=[
                {
                    "title": "区域与 Patch",
                    "text": (
                        f"Agent 已将请求标准化为 region=haidian、task={task_id}、month={month}，"
                        f"并定位到 {patch_id}。{self._selection_label(selection_source)}。"
                    ),
                },
                {
                    "title": "专题结果",
                    "text": (
                        f"本次报告直接调用海淀在线专题结果接口，获得 {task_display} PNG 结果图，"
                        "同时保留 embedding 预览图用于解释模型输入表征。"
                    ),
                },
            ],
            risks=self._build_risks(task_id, image_stats),
            method_notes=[
                f"Agent 将用户需求标准化为 region=haidian、task={task_id}、month={month}。",
                f"本次调用海淀 embedding-api：{self.config.base_url}。",
                self._selection_note(selection_source, patch_id),
                "海淀 system-models 推理接口当前未开放，本服务使用 patch 专题结果接口形成闭环。",
            ],
            limitations=[
                "当前为 patch 级专题结果，尚未对完整行政区 AOI 做多 patch 拼接和面积汇总。",
                "在线接口返回的是结果 PNG，暂无类别置信度、逐像素概率和完整图例元数据。",
            ],
            confidence_notes=[
                "专题结果图和 embedding 预览图均来自在线海淀 embedding-api。",
                "报告中的图像统计为 Agent 从 PNG 结果图中提取的轻量指标，正式业务评估仍需接入标签或评估接口。",
            ],
            data_source="haidian_embedding_api",
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            aef_payload={
                "service": self.config.base_url,
                "region_id": "haidian",
                "task": task_id,
                "version": "v1",
                "month": month,
                "patch": patch,
                "selected_patch_ids": request.selected_patch_ids,
                "aoi": request.aoi,
                "patch_selection_source": selection_source,
                "task_api_status": "available",
                "task_summary": task_summary,
                "image_stats": image_stats,
                "fingerprint": self._fingerprint(task_id, patch_id, month, {"summary": task_summary, "image": image_stats}),
            },
            charts=[
                ChartAsset(
                    title=f"{task_display}专题结果",
                    kind="image",
                    url=self._asset_url(result_asset),
                    caption=f"海淀在线专题服务返回的 {task_display} patch 级结果图。",
                ),
                ChartAsset(
                    title="Embedding 可视化预览",
                    kind="image",
                    url=self._asset_url(embedding_asset),
                    caption="海淀 patch 的 embedding RGB 预览图，用于辅助理解模型输入表征。",
                ),
            ],
        )

    def _normalize_task(self, task: str) -> str:
        task_id = TASK_TO_HAIDIAN.get(task)
        if task_id is None:
            supported = "建筑物提取、道路提取、施工识别、土地利用分类、土地覆盖分类、水体提取"
            raise RuntimeError(f"北京市海淀区暂不支持“{task}”，当前可用任务为：{supported}。")
        return task_id

    def _select_patch(self, request: ReportRequest, task_id: str, month: str) -> dict[str, Any]:
        if request.selected_patch_ids:
            for patch_id in request.selected_patch_ids[: self.config.sample_count]:
                try:
                    patch = self._get_json(f"/regions/haidian/patches/{patch_id}")
                except RuntimeError:
                    continue
                if self._is_usable_patch(patch, task_id, month):
                    patch["_agent_selection_source"] = "selected_patch_month_task_valid"
                    return patch
            aoi_patch = self._select_patch_from_aoi(request, task_id, month)
            if aoi_patch:
                aoi_patch["_agent_selection_source"] = "aoi_reselected_after_month_task"
                return aoi_patch
            raise RuntimeError(
                f"前端选择的海淀 patch 不支持 {month} / {task_id}，且当前 AOI 内没有找到可用 patch，请重新选择月份、任务或框选区域。"
            )

        aoi_patch = self._select_patch_from_aoi(request, task_id, month)
        if aoi_patch:
            aoi_patch["_agent_selection_source"] = "aoi_month_task_search"
            return aoi_patch

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
        patch = _stable_pick(patches, f"{request.region}-{request.task}-{month}-{task_id}", self.config.sample_count)[0]
        patch["_agent_selection_source"] = "global_month_task_search"
        return patch

    def _select_patch_from_aoi(self, request: ReportRequest, task_id: str, month: str) -> dict[str, Any] | None:
        bbox = self._aoi_bbox(request.aoi)
        if not bbox:
            return None
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
                score = _bbox_intersection_score([float(v) for v in patch_bbox], bbox)
                item = dict(patch)
                item["_agent_aoi_score"] = round(score, 6)
                candidates.append(item)
            if not payload.get("has_next"):
                break
            page += 1
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.get("_agent_aoi_score", 0), item.get("patch_id", "")), reverse=True)
        return candidates[0]

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
        url = urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"海淀 embedding-api 调用失败：{url}，原因：{exc}") from exc

    def _get_json_optional(self, path: str) -> dict[str, Any]:
        try:
            payload = self._get_json(path)
        except RuntimeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _download_remote_asset(self, remote_url: str, request: ReportRequest, task_id: str, kind: str) -> Path:
        source_url = urljoin(self.config.base_url.rstrip("/") + "/", remote_url.lstrip("/"))
        digest = hashlib.sha1(f"{source_url}-{request.time_range}-{task_id}-{kind}".encode("utf-8")).hexdigest()[:12]
        out_path = self.asset_dir / f"haidian_{task_id}_{kind}_{digest}.png"
        if not out_path.exists():
            try:
                with self.opener.open(source_url, timeout=self.config.timeout) as response, out_path.open("wb") as fh:
                    shutil.copyfileobj(response, fh)
            except HTTPError as exc:
                raise RuntimeError(f"海淀专题图像下载失败：{source_url}，HTTP {exc.code}") from exc
            except OSError as exc:
                raise RuntimeError(f"海淀专题图像下载失败：{source_url}，原因：{exc}") from exc
        return out_path

    def _asset_url(self, path: Path) -> str:
        return f"/reports/assets/{path.name}"

    def _image_stats(self, image_path: Path, task_id: str) -> dict[str, Any]:
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
            if task_id in BINARY_TASKS:
                foreground = total - dominant_count
                stats["foreground_ratio"] = round(foreground / max(total, 1), 4)
        return stats

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
                    "二值专题图中非主背景颜色的像素占比",
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
            stat_text = f"结果图中目标像素占比约 {float(image_stats['foreground_ratio']) * 100:.2f}%。"
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
            "前端展示时建议同时呈现专题结果图和 embedding 预览图，帮助用户理解输入表征与输出结果。",
            "下一步应把单 patch 结果扩展为 AOI 多 patch 聚合，形成面积、占比和空间分布统计。",
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
