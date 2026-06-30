from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode, urljoin
from urllib.request import ProxyHandler, Request, build_opener

from agent.config import EmbeddingAPIConfig
from agent.services.harbin_embedding_service import STATIC_TASKS, TASK_TO_HARBIN, aef_available


REGION_IDS = {
    "哈尔滨新区": "harbin",
    "harbin": "harbin",
    "harbin_new_area": "harbin",
}


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


def _bbox_intersection_score(a: list[float], b: list[float]) -> float:
    if len(a) != 4 or len(b) != 4:
        return 0.0
    left = max(a[0], b[0])
    bottom = max(a[1], b[1])
    right = min(a[2], b[2])
    top = min(a[3], b[3])
    if right <= left or top <= bottom:
        return 0.0
    inter = (right - left) * (top - bottom)
    patch_area = max((a[2] - a[0]) * (a[3] - a[1]), 1e-12)
    query_area = max((b[2] - b[0]) * (b[3] - b[1]), 1e-12)
    return float(inter / min(patch_area, query_area))


class PatchSelectionService:
    """Locate model patches from frontend map selections."""

    def __init__(self, config: EmbeddingAPIConfig | None = None) -> None:
        self.config = config or EmbeddingAPIConfig()
        self.opener = build_opener(ProxyHandler({}))

    def search(self, payload: dict[str, Any]) -> PatchSearchResult:
        region = str(payload.get("region") or "哈尔滨新区")
        region_id = REGION_IDS.get(region, "")
        task = str(payload.get("task") or "")
        time_range = str(payload.get("time_range") or "")
        bbox = self._parse_bbox(payload.get("bbox"))
        limit = self._parse_limit(payload.get("limit"), default=12)

        if region_id != "harbin":
            return PatchSearchResult(
                status="unsupported",
                region=region,
                region_id=region_id,
                task=task,
                time_range=time_range,
                bbox=bbox,
                patches=[],
                selected_patch_ids=[],
                message="当前地图 patch 检索先支持哈尔滨新区；雅江区域需要补充本地 patch 空间索引。",
            )

        task_id = TASK_TO_HARBIN.get(task, task)
        patches = self._search_harbin(region_id, task_id, time_range, bbox, limit)
        return PatchSearchResult(
            status="ok",
            region=region,
            region_id=region_id,
            task=task_id,
            time_range=time_range,
            bbox=bbox,
            patches=patches,
            selected_patch_ids=[str(item["patch_id"]) for item in patches[: max(1, min(limit, len(patches)))]],
            message=f"已定位到 {len(patches)} 个候选 patch。" if patches else "当前框选范围没有找到可用 patch。",
        )

    def _search_harbin(
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
                if not self._is_usable_patch(patch, task_id, time_range):
                    continue
                patch_bbox = patch.get("bounds_wgs84") or []
                score = _bbox_intersection_score([float(v) for v in patch_bbox], bbox)
                item = dict(patch)
                item["score"] = round(score, 6)
                item["task_available"] = aef_available(task_id, patch) if task_id in STATIC_TASKS else True
                rows.append(item)
            if not payload.get("has_next"):
                break
            page += 1

        rows.sort(key=lambda item: (item.get("score", 0), item.get("patch_id", "")), reverse=True)
        return rows[:limit]

    def _is_usable_patch(self, patch: dict[str, Any], task_id: str, time_range: str) -> bool:
        if not patch.get("has_embedding"):
            return False
        if time_range and time_range not in (patch.get("available_months") or []):
            return False
        if task_id in STATIC_TASKS and not aef_available(task_id, patch):
            return False
        return True

    def _get_json(self, path: str) -> Any:
        url = urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with self.opener.open(request, timeout=self.config.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Patch 检索失败：{url}，原因：{exc}") from exc

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
