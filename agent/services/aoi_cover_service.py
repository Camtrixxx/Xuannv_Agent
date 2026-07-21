"""AOI coverage service (Scenario A foundation).

Given a map selection (bbox) + a binary task + month, resolve every patch that
intersects the AOI, read each patch's binary result PNG, compute per-patch
coverage with the raster tool, and aggregate to an AOI-level hectare total.

Only the reliable binary tasks are supported (building/water/road/construction);
multiclass land-cover ratios are deferred until the legend arrives
(see the land_cover legend memo). Green-space/impervious ratios come later.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from agent.config import EmbeddingAPIConfig
from agent.services.http_client import JsonHttpClient
from agent.services.patch_selection_service import PatchSelectionService
from agent.tools.aoi import aggregate_binary_coverage
from agent.tools.raster import BINARY_TASK_BACKGROUND, binary_coverage


@dataclass(slots=True)
class AoiCoverResult:
    status: str
    region: str
    region_id: str
    task: str
    time_range: str
    bbox: list[float]
    summary: dict[str, Any] = field(default_factory=dict)
    per_patch: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""


class AoiCoverService:
    """Aggregate binary-task coverage over an area of interest."""

    def __init__(
        self,
        config: EmbeddingAPIConfig | None = None,
        patch_selection: PatchSelectionService | None = None,
    ) -> None:
        self.config = config or EmbeddingAPIConfig()
        self.patch_selection = patch_selection or PatchSelectionService(self.config)
        self.http = JsonHttpClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            error_prefix="AOI 覆盖统计失败",
        )

    def analyze(self, payload: dict[str, Any]) -> AoiCoverResult:
        region = str(payload.get("region") or "北京市海淀区")
        task = str(payload.get("task") or "")
        time_range = str(payload.get("time_range") or "")
        limit = int(payload.get("limit") or 60)

        search = self.patch_selection.search(
            {"region": region, "task": task, "time_range": time_range, "bbox": payload.get("bbox"), "limit": limit}
        )
        task_id = search.task
        base = AoiCoverResult(
            status=search.status,
            region=search.region,
            region_id=search.region_id,
            task=task_id,
            time_range=search.time_range,
            bbox=search.bbox,
        )

        if task_id not in BINARY_TASK_BACKGROUND:
            base.status = "unsupported"
            base.message = (
                f"片区覆盖统计目前仅支持二值任务（建筑/水体/道路/施工）。"
                f"任务「{task_id}」暂不支持（多类土地覆盖等待官方图例）。"
            )
            return base
        if search.status != "ok" or not search.patches:
            base.message = search.message or "当前框选范围没有可统计的 patch。"
            return base

        month = time_range.replace("-", "")
        per_patch = self._collect_patch_stats(search.region_id, task_id, month, search.patches)
        if not per_patch:
            base.message = "候选 patch 均无法获取有效专题结果，暂时无法统计。"
            return base

        base.summary = aggregate_binary_coverage(per_patch)
        base.per_patch = per_patch
        base.message = self._describe(task_id, base.summary)
        return base

    def _collect_patch_stats(
        self, region_id: str, task_id: str, month: str, patches: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for patch, counts in self.iter_patch_colors(region_id, task_id, month, patches):
            coverage = binary_coverage(task_id, counts, patch.get("bounds"))
            if not coverage:
                continue
            rows.append({"patch_id": str(patch.get("patch_id") or ""), **coverage})
        return rows

    def iter_patch_colors(
        self, region_id: str, task_id: str, month: str, patches: list[dict[str, Any]]
    ):
        """Yield ``(patch, color_counts)`` for each fetchable result PNG.

        Shared plumbing so callers (binary coverage here, multiclass distribution
        in the checkup) resolve + fetch once without duplicating the download
        loop. Patches whose result can't be fetched are silently skipped.
        """
        for patch in patches:
            patch_id = str(patch.get("patch_id") or "")
            if not patch_id:
                continue
            counts = self._result_colors(region_id, patch_id, task_id, month)
            if counts is None:
                continue
            yield patch, counts

    def _result_colors(
        self, region_id: str, patch_id: str, task_id: str, month: str
    ) -> list[tuple[int, tuple[int, int, int]]] | None:
        from urllib.parse import urlencode

        query = urlencode({"format": "png", "version": "v1", "month": month})
        url = f"/regions/{region_id}/patches/{patch_id}/tasks/{task_id}/result?{query}"
        try:
            data = self.http.fetch_bytes(url, asset_label="AOI 专题结果")
            with Image.open(io.BytesIO(data)) as image:
                rgb = image.convert("RGB")
                return rgb.getcolors(maxcolors=1_000_000) or []
        except (RuntimeError, OSError):
            return None

    def fetch_result_array(self, region_id: str, patch_id: str, task_id: str, month: str):
        """Fetch a result PNG as an HxWx3 uint8 numpy array (or None on failure).

        Shared by change detection, which needs pixel positions (not just a
        colour histogram) to diff two dates.
        """
        import numpy as np
        from urllib.parse import urlencode

        query = urlencode({"format": "png", "version": "v1", "month": month})
        url = f"/regions/{region_id}/patches/{patch_id}/tasks/{task_id}/result?{query}"
        try:
            data = self.http.fetch_bytes(url, asset_label="AOI 变化检测结果")
            with Image.open(io.BytesIO(data)) as image:
                return np.asarray(image.convert("RGB"))
        except (RuntimeError, OSError):
            return None

    def _describe(self, task_id: str, summary: dict[str, Any]) -> str:
        from agent.services.haidian_embedding_service import TASK_DISPLAY

        name = TASK_DISPLAY.get(task_id, task_id)
        ratio = summary.get("coverage_ratio")
        covered = summary.get("covered_area_ha")
        total = summary.get("total_area_ha")
        n = summary.get("area_patch_count")
        if ratio is None or covered is None:
            return f"已统计 {summary.get('patch_count', 0)} 个 patch，但缺少可用面积信息。"
        return (
            f"框选片区内 {n} 个 patch（合计约 {total:.2f} 公顷）：{name}覆盖约 "
            f"{ratio * 100:.2f}%，面积约 {covered:.2f} 公顷。"
        )
