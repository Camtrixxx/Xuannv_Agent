from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from agent.config import EmbeddingAPIConfig
from agent.services.common import bbox_intersection_score
from agent.services.harbin_embedding_service import aef_available
from agent.services.http_client import JsonHttpClient
from agent.services.yajiang_patch_index_service import YajiangPatchIndexService
from agent.taxonomy import REGION_IDS, STATIC_TASKS, normalize_task


@dataclass(slots=True)
class PatchSearchResult:
    status: str
    region: str
    region_id: str
    task: str
    time_range: str
    bbox: list[float]
    patches: list[dict[str, Any]]
    selected_patch_ids: list[str]
    message: str = ""


class PatchSelectionService:
    """Locate model patches from frontend map selections."""

    def __init__(
        self,
        config: EmbeddingAPIConfig | None = None,
        yajiang_index: YajiangPatchIndexService | None = None,
    ) -> None:
        self.config = config or EmbeddingAPIConfig()
        self.yajiang_index = yajiang_index or YajiangPatchIndexService()
        self.http = JsonHttpClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            error_prefix="Patch 检索失败",
        )

    def search(self, payload: dict[str, Any]) -> PatchSearchResult:
        region = str(payload.get("region") or "哈尔滨新区")
        region_id = REGION_IDS.get(region, "")
        task = str(payload.get("task") or "")
        time_range = str(payload.get("time_range") or "")
        bbox = self._parse_bbox(payload.get("bbox"))
        limit = self._parse_limit(payload.get("limit"), default=12)

        if region_id not in {"yajiang", "harbin", "haidian"}:
            return PatchSearchResult(
                status="unsupported",
                region=region,
                region_id=region_id,
                task=task,
                time_range=time_range,
                bbox=bbox,
                patches=[],
                selected_patch_ids=[],
                message="当前地图 patch 检索支持雅江区域、哈尔滨新区和北京市海淀区。",
            )

        task_id = self._normalize_task(region_id, task)
        patches = (
            self._search_yajiang(task_id, time_range, bbox, limit)
            if region_id == "yajiang"
            else self._search_region(region_id, task_id, time_range, bbox, limit)
        )
        message = f"已定位到 {len(patches)} 个候选 patch。" if patches else "当前框选范围没有找到可用 patch。"
        if not patches and region_id == "haidian" and time_range:
            patches = self._search_region(region_id, task_id, "", bbox, limit)
            if patches:
                available = self._collect_available_months(patches)
                message = (
                    f"当前输入月份 {time_range} 暂无可用海淀 embedding，"
                    "已先按空间范围列出候选 patch。"
                )
                if available:
                    message += f" 可用月份示例：{', '.join(available[:6])}。"
        return PatchSearchResult(
            status="ok",
            region=region,
            region_id=region_id,
            task=task_id,
            time_range=time_range,
            bbox=bbox,
            patches=patches,
            selected_patch_ids=[str(item["patch_id"]) for item in patches[: max(1, min(limit, len(patches)))]],
            message=message,
        )

    def _search_region(
        self,
        region_id: str,
        task_id: str,
        time_range: str,
        bbox: list[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        query_base = {
            "page_size": 100,
            "bbox": ",".join(f"{value:.8f}" for value in bbox),
        }
        while True:
            query = urlencode({**query_base, "page": page})
            payload = self._get_json(f"/regions/{region_id}/patches?{query}")
            batch = payload.get("patches") or []
            for patch in batch:
                if not self._is_usable_patch(region_id, patch, task_id, time_range):
                    continue
                patch_bbox = patch.get("bounds_wgs84") or []
                score = bbox_intersection_score([float(v) for v in patch_bbox], bbox)
                item = dict(patch)
                item["score"] = round(score, 6)
                item["task_available"] = self._task_available(region_id, patch, task_id)
                rows.append(item)
            if not payload.get("has_next"):
                break
            page += 1

        rows.sort(key=lambda item: (item.get("score", 0), item.get("patch_id", "")), reverse=True)
        return rows[:limit]

    def _search_yajiang(
        self,
        task_id: str,
        time_range: str,
        bbox: list[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        return self.yajiang_index.search(bbox=bbox, task_id=task_id, time_range=time_range, limit=limit)

    def _is_usable_patch(self, region_id: str, patch: dict[str, Any], task_id: str, time_range: str) -> bool:
        if not patch.get("has_embedding"):
            return False
        if time_range and not self._month_available(region_id, patch, time_range):
            return False
        if not self._task_available(region_id, patch, task_id):
            return False
        return True

    def _normalize_task(self, region_id: str, task: str) -> str:
        return normalize_task(region_id, task)

    def _task_available(self, region_id: str, patch: dict[str, Any], task_id: str) -> bool:
        if not task_id:
            return True
        if region_id == "yajiang":
            tasks = patch.get("available_tasks") or []
            return task_id in tasks
        if region_id == "haidian":
            tasks = patch.get("available_tasks") or []
            return not tasks or task_id in tasks
        return aef_available(task_id, patch) if task_id in STATIC_TASKS else True

    def _month_available(self, region_id: str, patch: dict[str, Any], time_range: str) -> bool:
        months = [str(item) for item in (patch.get("available_months") or [])]
        if region_id == "yajiang":
            return time_range in months
        if region_id == "haidian":
            target = time_range.replace("-", "")
            return any(month == target or month.startswith(target) for month in months)
        return time_range in months

    def _collect_available_months(self, patches: list[dict[str, Any]]) -> list[str]:
        values: set[str] = set()
        for patch in patches:
            for month in patch.get("available_months") or []:
                text = str(month)
                values.add(f"{text[:4]}-{text[4:6]}" if len(text) >= 6 else text)
        return sorted(values)

    def _get_json(self, path: str) -> Any:
        return self.http.get_json(path)

    def _parse_bbox(self, raw: Any) -> list[float]:
        if not isinstance(raw, list) or len(raw) != 4:
            raise ValueError("bbox 必须是 [min_lng, min_lat, max_lng, max_lat]")
        bbox = [float(value) for value in raw]
        if bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
            raise ValueError("bbox 范围无效")
        return bbox

    def _parse_limit(self, raw: Any, default: int) -> int:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, 50))
