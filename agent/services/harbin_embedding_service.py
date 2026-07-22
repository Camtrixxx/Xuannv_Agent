from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from agent.config import EmbeddingAPIConfig, ReportConfig
from agent.schemas.report import AnalysisResult, ChartAsset, MetricCard, ReportRequest
from agent.services.common import bbox_intersection_score
from agent.services.http_client import JsonHttpClient
from agent.services.satellite_basemap import basemap_chart
from agent.taxonomy import (
    HARBIN_MONTHS,
    STATIC_TASKS,
    SYSTEM_MODEL_TASKS,
    TASK_TO_HARBIN,
)

# Harbin-only display mapping (backend id -> zh label); not part of the shared
# vocabulary, so it stays local to this service.
TASK_DISPLAY = {
    "water_extraction": "水体提取",
    "building_extraction": "建筑物提取",
    "land_use_classification": "土地利用分类",
}


def _stable_pick(items: list[dict[str, Any]], key: str, count: int) -> list[dict[str, Any]]:
    count = max(1, min(count, len(items)))
    return sorted(
        items,
        key=lambda item: hashlib.sha1(f"{key}:{item.get('patch_id')}".encode("utf-8")).hexdigest(),
    )[:count]


def aef_available(task_id: str, patch: dict[str, Any]) -> bool:
    return task_id in (patch.get("available_tasks") or [])


class HarbinEmbeddingAnalysisService:
    """Analysis service backed by the Harbin regional embedding API."""

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
            error_prefix="哈尔滨 embedding-api 调用失败",
        )

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        task_id = self._normalize_task(request.task)
        self._validate_month(request.time_range)
        patches = self._select_patches(request, task_id)
        patch = patches[0]
        patch_id = str(patch["patch_id"])
        selection_source = str(patch.get("_agent_selection_source") or "")

        result = self._infer_system_model(task_id, patch_id, request.time_range) if task_id in SYSTEM_MODEL_TASKS else {}
        classes = self._get_classes(task_id) if task_id in SYSTEM_MODEL_TASKS else []
        task_summary = self._get_task_summary(task_id) if task_id in STATIC_TASKS else {}

        task_display = TASK_DISPLAY.get(task_id, request.task)
        charts = self._build_charts(request, task_id, task_display, patch_id, result)
        basemap = basemap_chart(patch.get("bounds_wgs84"), self.asset_dir, f"harbin-{patch_id}")
        if basemap:
            charts = [basemap, *charts]

        class_names = [str(item.get("name") or item.get("id")) for item in classes]
        metrics = self._build_metrics(request, task_display, task_id, patch, class_names, task_summary)
        findings = self._build_findings(request, task_id, patch, class_names, task_summary)
        return AnalysisResult(
            task=task_display,
            region="哈尔滨新区",
            time_range=request.time_range,
            headline=f"哈尔滨新区{request.time_range}{task_display}遥感分析",
            summary=self._summary_text(request, task_id, patch, class_names),
            metrics=metrics,
            findings=findings,
            recommendations=self._build_recommendations(task_id),
            narrative_blocks=self._build_narratives(request, task_id, patch),
            risks=self._build_risks(task_id),
            method_notes=[
                f"Agent 将用户需求标准化为 region=harbin、task={task_id}、month={request.time_range}。",
                f"本次调用哈尔滨新区 embedding-api：{self.config.base_url}。",
                self._selection_note(selection_source, patch_id),
            ],
            limitations=[
                "当前接入的是 patch 级系统模型推理结果，尚未按完整行政区 AOI 汇总。",
                "报告指标主要来自 patch 元数据和模型输出图，精度评估需结合更完整的标签或评估接口补充。",
            ],
            confidence_notes=[
                f"可用类别：{', '.join(class_names[:8]) if class_names else '暂无类别信息'}。",
                f"当前月份 {request.time_range} 在哈尔滨 embedding-api 可用月份范围内。",
            ],
            data_source="harbin_embedding_api",
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            aef_payload={
                "service": self.config.base_url,
                "region_id": "harbin",
                "task": task_id,
                "version": self.config.version,
                "month": request.time_range,
                "patch": patch,
                "selected_patch_ids": request.selected_patch_ids,
                "aoi": request.aoi,
                "patch_selection_source": selection_source,
                "classes": classes,
                "system_model_result": result,
                "task_summary": task_summary,
                "fingerprint": self._fingerprint(task_id, patch_id, request.time_range, {"system": result, "summary": task_summary}),
            },
            charts=charts,
        )

    def _normalize_task(self, task: str) -> str:
        task_id = TASK_TO_HARBIN.get(task)
        if task_id is None:
            supported = "建筑物提取、土地利用分类、水体提取"
            raise RuntimeError(f"哈尔滨新区暂不支持“{task}”，当前可用任务为：{supported}。")
        return task_id

    def _validate_month(self, month: str) -> None:
        if month not in HARBIN_MONTHS:
            raise RuntimeError(
                f"哈尔滨新区当前可用月份为 {', '.join(HARBIN_MONTHS)}，收到的月份是 {month}。"
            )

    def _select_patches(self, request: ReportRequest, task_id: str) -> list[dict[str, Any]]:
        if request.selected_patch_ids:
            selected = []
            for patch_id in request.selected_patch_ids[: self.config.sample_count]:
                try:
                    patch = self._get_json(f"/regions/harbin/patches/{patch_id}")
                except RuntimeError:
                    continue
                if self._is_usable_patch(patch, task_id, request.time_range):
                    patch["_agent_selection_source"] = "selected_patch_month_task_valid"
                    selected.append(patch)
            if selected:
                return selected
            aoi_selected = self._select_patches_from_aoi(request, task_id)
            if aoi_selected:
                for item in aoi_selected:
                    item["_agent_selection_source"] = "aoi_reselected_after_month_task"
                return aoi_selected
            raise RuntimeError(
                f"前端选择的 patch 不支持 {request.time_range} / {task_id}，且当前 AOI 内没有找到可用 patch，请重新选择月份、任务或框选区域。"
            )

        aoi_selected = self._select_patches_from_aoi(request, task_id)
        if aoi_selected:
            for item in aoi_selected:
                item["_agent_selection_source"] = "aoi_month_task_search"
            return aoi_selected

        patches: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._get_json(f"/regions/harbin/patches?page={page}&page_size=100")
            batch = payload.get("patches") or []
            patches.extend(
                item
                for item in batch
                if self._is_usable_patch(item, task_id, request.time_range)
            )
            if not payload.get("has_next"):
                break
            page += 1
        if not patches:
            raise RuntimeError(f"没有找到支持 {request.time_range} 的哈尔滨 patch。")
        selected = _stable_pick(patches, f"{request.region}-{request.task}-{request.time_range}-{task_id}", self.config.sample_count)
        for item in selected:
            item["_agent_selection_source"] = "global_month_task_search"
        return selected

    def _select_patches_from_aoi(self, request: ReportRequest, task_id: str) -> list[dict[str, Any]]:
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
            payload = self._get_json(f"/regions/harbin/patches?{query}")
            for patch in payload.get("patches") or []:
                if not self._is_usable_patch(patch, task_id, request.time_range):
                    continue
                patch_bbox = patch.get("bounds_wgs84") or []
                score = bbox_intersection_score([float(v) for v in patch_bbox], bbox)
                item = dict(patch)
                item["_agent_aoi_score"] = round(score, 6)
                candidates.append(item)
            if not payload.get("has_next"):
                break
            page += 1
        candidates.sort(key=lambda item: (item.get("_agent_aoi_score", 0), item.get("patch_id", "")), reverse=True)
        return candidates[: self.config.sample_count]

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

    def _is_usable_patch(self, patch: dict[str, Any], task_id: str, time_range: str) -> bool:
        if not patch.get("has_embedding"):
            return False
        if time_range not in (patch.get("available_months") or []):
            return False
        if task_id in STATIC_TASKS and not aef_available(task_id, patch):
            return False
        return True

    def _infer_system_model(self, task_id: str, patch_id: str, month: str) -> dict[str, Any]:
        query = urlencode(
            {
                "region_id": "harbin",
                "patch_id": patch_id,
                "month": month,
                "version": self.config.version,
            }
        )
        return self._post_json(f"/system-models/{task_id}/infer?{query}")

    def _get_classes(self, task_id: str) -> list[dict[str, Any]]:
        query = urlencode({"region_id": "harbin", "version": self.config.version})
        payload = self._get_json(f"/system-models/{task_id}/classes?{query}")
        return payload if isinstance(payload, list) else []

    def _get_task_summary(self, task_id: str) -> dict[str, Any]:
        payload = self._get_json(f"/regions/harbin/tasks/{task_id}/summary?version=v1")
        return payload if isinstance(payload, dict) else {}

    def _get_json(self, path: str) -> Any:
        return self._request_json(path, method="GET")

    def _post_json(self, path: str) -> dict[str, Any]:
        return self._request_json(path, method="POST")

    def _request_json(self, path: str, method: str) -> Any:
        return self.http.request_json(path, method=method)

    def _copy_remote_asset(self, remote_url: str, request: ReportRequest, task_id: str, kind: str) -> str:
        source_url = self.http._url(remote_url)
        digest = hashlib.sha1(f"{source_url}-{request.time_range}-{task_id}-{kind}".encode("utf-8")).hexdigest()[:12]
        out_path = self.asset_dir / f"harbin_{task_id}_{kind}_{digest}.png"
        self.http.download(remote_url, out_path, asset_label="哈尔滨 embedding-api 图像")
        return f"/reports/assets/{out_path.name}"

    def _build_charts(
        self,
        request: ReportRequest,
        task_id: str,
        task_display: str,
        patch_id: str,
        system_result: dict[str, Any],
    ) -> list[ChartAsset]:
        charts: list[ChartAsset] = []
        if task_id in STATIC_TASKS:
            static_url = (
                f"/regions/harbin/patches/{patch_id}/tasks/{task_id}/result?"
                f"{urlencode({'format': 'png', 'version': 'v1'})}"
            )
            charts.append(
                ChartAsset(
                    title=f"{task_display}专题结果",
                    kind="image",
                    url=self._copy_remote_asset(static_url, request, task_id, "static_result"),
                    caption="哈尔滨 embedding-api 预生成专题结果，适合做稳定报告展示和业务解读。",
                )
            )
        if system_result.get("result_url"):
            charts.append(
                ChartAsset(
                    title=f"{task_display}实时推理结果",
                    kind="image",
                    url=self._copy_remote_asset(system_result["result_url"], request, task_id, "system_result"),
                    caption="系统预训练模型基于指定月份 embedding 生成的 patch 级实时推理结果。",
                )
            )
        return charts

    def _build_metrics(
        self,
        request: ReportRequest,
        task_display: str,
        task_id: str,
        patch: dict[str, Any],
        class_names: list[str],
        task_summary: dict[str, Any],
    ) -> list[MetricCard]:
        bounds = patch.get("bounds_wgs84") or []
        bounds_text = "暂无"
        if len(bounds) == 4:
            bounds_text = f"{bounds[0]:.4f},{bounds[1]:.4f} - {bounds[2]:.4f},{bounds[3]:.4f}"
        cards = [
            MetricCard("任务", task_display, "Agent 识别后的哈尔滨专题任务"),
            MetricCard("地区", "哈尔滨新区", "哈尔滨 embedding-api 区域标识 harbin"),
            MetricCard("时间", request.time_range, "用户指定的分析月份"),
            MetricCard("接口模式", self._task_mode(task_id), "当前专题使用的哈尔滨 API 能力"),
            MetricCard(
                "Patch",
                str(patch.get("patch_id") or "暂无"),
                self._selection_label(str(patch.get("_agent_selection_source") or "")),
            ),
            MetricCard("可用月份", str(len(patch.get("available_months") or [])), "当前 patch 可查询的 embedding 月份数"),
            MetricCard("经纬度范围", bounds_text, "当前 patch 的 WGS84 边界"),
        ]
        if class_names:
            cards.append(MetricCard("可用类别", str(len(class_names)), "系统模型返回的类别数量"))
        if task_summary:
            cards.extend(
                [
                    MetricCard("统计 Patch", str(task_summary.get("total_patches") or "暂无"), "专题结果覆盖的 patch 数"),
                    MetricCard("正样本 Patch", str(task_summary.get("positive_patches") or "暂无"), "专题结果中包含目标对象的 patch 数"),
                ]
            )
        return cards

    def _summary_text(self, request: ReportRequest, task_id: str, patch: dict[str, Any], class_names: list[str]) -> str:
        task_display = TASK_DISPLAY.get(task_id, request.task)
        if task_id == "building_extraction":
            return (
                f"本次调用哈尔滨新区 embedding-api，对 {request.time_range} 的 {patch.get('patch_id')} "
                "执行建筑物提取。报告同时使用预生成建筑物专题结果和系统模型实时推理图，"
                "适合观察建设区、工地和高密度人工地表。"
            )
        if task_id == "land_use_classification":
            return (
                f"本次调用哈尔滨新区 embedding-api，对 {request.time_range} 的 {patch.get('patch_id')} "
                "执行土地利用分类。报告使用预生成土地利用专题结果，适合分析耕地、建设用地等用地结构。"
            )
        class_text = "、".join(class_names[:5]) if class_names else "Non-water、Water"
        return (
            f"本次调用哈尔滨新区 embedding-api，对 {request.time_range} 的 {patch.get('patch_id')} "
            f"执行水体提取。系统模型返回了可视化推理结果，并提供 {class_text} 等类别定义，"
            "适合观察松花江水系、湿地和水陆边界。"
        )

    def _build_findings(
        self,
        request: ReportRequest,
        task_id: str,
        patch: dict[str, Any],
        class_names: list[str],
        task_summary: dict[str, Any],
    ) -> list[str]:
        task_display = TASK_DISPLAY.get(task_id, request.task)
        findings = [
            f"哈尔滨新区 {request.time_range} 的 {task_display} 请求已成功路由到区域 embedding-api。",
            f"当前 patch 为 {patch.get('patch_id')}，可用月份包括 {', '.join((patch.get('available_months') or [])[:10])}。",
        ]
        if task_id == "water_extraction":
            findings.append("水体提取结果适合观察河道、湖泊、湿地及水陆边界的空间连续性。")
        elif task_id == "building_extraction":
            findings.append("建筑物提取结果适合观察城市建设区、工地和高密度人工地表的分布。")
        else:
            findings.append("土地利用分类结果适合观察耕地、建设用地及其他用地结构的空间差异。")
        if class_names:
            findings.append(f"系统模型类别定义已同步，前端可据此绘制图例：{', '.join(class_names[:8])}。")
        if task_summary:
            findings.append(
                f"专题统计显示该任务覆盖 {task_summary.get('total_patches', '暂无')} 个 patch，"
                f"其中正样本 patch 为 {task_summary.get('positive_patches', '暂无')} 个。"
            )
        return findings

    def _build_recommendations(self, task_id: str) -> list[str]:
        common = [
            "后续应把区域选择替换为 AOI 到 patch 的空间检索，并对多个 patch 做汇总统计。",
            "建议将模型结果图与业务底图叠加解读，重点核对目标边界和疑似误检区域。",
        ]
        if task_id == "water_extraction":
            return ["水体边界建议结合多月份结果复核，区分季节性水位变化与稳定水面。", *common]
        if task_id == "building_extraction":
            return ["建筑物结果建议叠加道路、地块或施工区域边界，提升城市更新场景解释力。", *common]
        return ["土地利用分类建议结合多月份结果和建设用地边界，识别耕地、建设用地等结构变化。", *common]

    def _build_narratives(self, request: ReportRequest, task_id: str, patch: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {
                "title": "区域 API 接入",
                "text": f"Agent 已识别哈尔滨新区请求，并调用 {self.config.base_url} 完成 {task_id} 专题分析。",
            },
            {
                "title": "Patch 选择",
                "text": (
                    f"当前使用 {patch.get('patch_id')}，选择方式为：{self._selection_label(str(patch.get('_agent_selection_source') or ''))}。"
                ),
            },
        ]

    def _build_risks(self, task_id: str) -> list[str]:
        if task_id == "water_extraction":
            return ["当前为单 patch 结果，不能直接代表整个哈尔滨新区水体面积变化。"]
        if task_id == "building_extraction":
            return ["建筑物提取容易受阴影、工地裸土和密集纹理影响，正式报告需要结合置信度或人工抽检。"]
        return ["土地利用分类在用地边界和混合像元区域可能不稳定，建议后续补充多 patch 汇总和人工核验。"]

    def _task_mode(self, task_id: str) -> str:
        if task_id == "building_extraction":
            return "预生成专题 + 实时推理"
        if task_id == "land_use_classification":
            return "预生成专题"
        return "实时推理"

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
            {"task": task_id, "patch": patch_id, "month": month, "version": self.config.version, "result": result},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
