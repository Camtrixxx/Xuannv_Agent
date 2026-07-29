"""Single-date analysis for user-trained Haidian models."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from agent.config import EmbeddingAPIConfig, ReportConfig
from agent.schemas.report import AnalysisResult, MetricCard, ReportRequest
from agent.services.http_client import JsonHttpClient
from agent.services.model_registry_service import ModelInfo, ModelRegistryService
from agent.services.patch_selection_service import PatchSelectionService
from agent.services.satellite_basemap import basemap_chart
from agent.tools.aoi import aggregate_binary_coverage
from agent.tools.change import custom_model_mask
from agent.tools.mosaic import build_mosaic_overlay
from agent.tools.raster import area_ha_from_bounds


class CustomModelAnalysisService:
    """Run a ready custom model over the user's explicit Haidian selection."""

    def __init__(
        self,
        config: EmbeddingAPIConfig | None = None,
        report_config: ReportConfig | None = None,
        patch_selection: PatchSelectionService | None = None,
        registry: ModelRegistryService | None = None,
    ) -> None:
        self.config = config or EmbeddingAPIConfig()
        self.report_config = report_config or ReportConfig()
        self.asset_dir = self.report_config.asset_dir
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.patch_selection = patch_selection or PatchSelectionService(self.config)
        self.registry = registry or ModelRegistryService(self.config)
        self.http = JsonHttpClient(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            error_prefix="海淀自定义模型调用失败",
        )

    def analyze(self, request: ReportRequest) -> AnalysisResult:
        model_id = str(request.custom_model_id or "").strip()
        target = str(request.target_object or request.task or "自定义地物").strip()
        month = str(request.time_range or "").replace("-", "")
        if not model_id:
            raise RuntimeError("缺少已训练的自定义模型 ID。")
        if not month:
            raise RuntimeError("自定义模型分析需要明确月份，例如 2026年3月。")
        if "海淀" not in request.region and request.region.lower() not in {"haidian", "beijing_haidian"}:
            raise RuntimeError("当前自定义模型推理闭环仅支持北京市海淀区。")

        model = self.registry.model_status(model_id, "haidian")
        if model is not None and (not model.is_ready or model.type != "single_time_detection"):
            raise RuntimeError("该自定义模型尚未就绪，或不是单期识别模型。")

        patches, rejected_ids, omitted_ids = self._select_patches(request, month)
        infer_results = self._infer_batch(model_id, month, [str(p["patch_id"]) for p in patches])
        target_color = self._target_color(model, target)
        coverage_rows: list[dict[str, Any]] = []
        patch_results: list[dict[str, Any]] = []
        tiles: list[dict[str, Any]] = []

        for patch in patches:
            patch_id = str(patch.get("patch_id") or "")
            item = infer_results.get(patch_id) or {}
            result_url = str(item.get("result_url") or "")
            if str(item.get("status") or "").lower() not in {"ok", "ready", "completed", "success"} or not result_url:
                rejected_ids.append(patch_id)
                patch_results.append({
                    "patch_id": patch_id,
                    "status": "failed",
                    "error": str(item.get("error") or "模型未返回结果图"),
                    "bounds_wgs84": patch.get("bounds_wgs84") or [],
                })
                continue
            try:
                tile, coverage = self._prepare_result_tile(
                    patch, model_id, target, month, result_url, target_color
                )
            except Exception as exc:
                rejected_ids.append(patch_id)
                patch_results.append({
                    "patch_id": patch_id,
                    "status": "failed",
                    "error": str(exc),
                    "bounds_wgs84": patch.get("bounds_wgs84") or [],
                })
                continue
            tiles.append(tile)
            coverage_rows.append(coverage)
            patch_results.append({
                "patch_id": patch_id,
                "status": "ok",
                "bounds_wgs84": patch.get("bounds_wgs84") or [],
                "metrics": coverage,
            })

        if not tiles:
            raise RuntimeError("所选 Patch 的自定义模型结果均未能获取，请确认模型、月份和选区后重试。")

        aggregate = aggregate_binary_coverage(coverage_rows)
        used_ids = [str(tile["patch_id"]) for tile in tiles]
        union_bounds = self._union_wgs84([tile.get("bounds_wgs84") for tile in tiles])
        basemap = basemap_chart(union_bounds, self.asset_dir, f"custom-{model_id}-{sorted(used_ids)}")
        overlays = build_mosaic_overlay(
            tiles,
            self.asset_dir,
            stem=f"haidian_custom_{model_id}",
            fingerprint=f"{model_id}:{month}:{target}:{sorted(used_ids)}",
            merged_title=f"{target}识别结果（{{n}} patch 拼接）",
            merged_caption=f"自定义模型识别出的{target}区域；非目标区域透明，已将 {{n}} 个 patch 拼接为连续图层。",
            per_patch_title=f"{target}识别结果 · {{patch_id}}",
            per_patch_caption=f"自定义模型识别出的{target}区域；非目标区域透明。",
        )
        charts = ([basemap] if basemap else []) + overlays
        return self._build_result(
            request=request,
            model=model,
            model_id=model_id,
            target=target,
            month=month,
            aggregate=aggregate,
            used_ids=used_ids,
            rejected_ids=list(dict.fromkeys(rejected_ids)),
            omitted_ids=omitted_ids,
            patch_results=patch_results,
            charts=charts,
        )

    def _select_patches(
        self, request: ReportRequest, month: str
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        configured = int(self.config.max_selected_patches)
        limit = min(configured, 100) if configured > 0 else 100
        if request.selected_patch_ids:
            ids = list(dict.fromkeys(str(v) for v in request.selected_patch_ids if str(v).strip()))
            selected: list[dict[str, Any]] = []
            rejected: list[str] = []
            for patch_id in ids[:limit]:
                try:
                    patch = self.http.get_json(f"/regions/haidian/patches/{patch_id}")
                except RuntimeError:
                    rejected.append(patch_id)
                    continue
                if self._is_usable_patch(patch, month):
                    selected.append(patch)
                else:
                    rejected.append(patch_id)
            if selected:
                return selected, rejected, ids[limit:]
            raise RuntimeError(f"所选 Patch 均不支持 {month} 月份或缺少可用 embedding。")

        bbox = self._bbox(request.aoi)
        if not bbox:
            raise RuntimeError("请先在地图上框选区域并确认 Patch，不会自动从海淀全区随机选择。")
        search = self.patch_selection.search({
            "region": "北京市海淀区",
            "task": "",
            "time_range": month,
            "bbox": bbox,
            "limit": limit,
        })
        if search.status != "ok" or not search.patches:
            raise RuntimeError(search.message or "当前框选范围内没有可用于自定义模型推理的 Patch。")
        return search.patches[:limit], [], search.selected_patch_ids[limit:]

    def _infer_batch(self, model_id: str, month: str, patch_ids: list[str]) -> dict[str, dict[str, Any]]:
        try:
            payload = self.http.post_json(
                f"/models/{model_id}/infer_batch",
                {"region_id": "haidian", "patch_ids": patch_ids, "month": month},
            )
            rows = payload.get("results") if isinstance(payload, dict) else []
            if isinstance(rows, list):
                return {
                    str(row.get("patch_id")): row
                    for row in rows
                    if isinstance(row, dict) and row.get("patch_id")
                }
        except RuntimeError:
            pass

        results: dict[str, dict[str, Any]] = {}
        for patch_id in patch_ids:
            try:
                row = self.http.post_json(
                    f"/models/{model_id}/infer",
                    {"region_id": "haidian", "patch_id": patch_id, "month": month},
                )
                results[patch_id] = {
                    "patch_id": patch_id,
                    "status": "ok",
                    "result_url": row.get("result_url") if isinstance(row, dict) else "",
                }
            except RuntimeError as exc:
                results[patch_id] = {"patch_id": patch_id, "status": "failed", "error": str(exc)}
        return results

    def _prepare_result_tile(
        self,
        patch: dict[str, Any],
        model_id: str,
        target: str,
        month: str,
        result_url: str,
        target_color: tuple[int, int, int] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        content = self.http.fetch_bytes(result_url, asset_label="自定义模型结果图")
        with Image.open(BytesIO(content)) as image:
            rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        mask = self._class_mask(rgb, target_color)
        rgba = np.zeros((*mask.shape, 4), dtype=np.uint8)
        rgba[mask, :3] = rgb[mask]
        rgba[mask, 3] = 220

        patch_id = str(patch.get("patch_id") or "")
        digest = hashlib.sha1(
            b"\0".join([
                model_id.encode(), target.encode(), month.encode(), patch_id.encode(), content,
            ])
        ).hexdigest()[:12]
        out_path = self.asset_dir / f"haidian_custom_{model_id}_{patch_id}_{digest}.png"
        if not out_path.exists():
            Image.fromarray(rgba, mode="RGBA").save(out_path)

        ratio = round(float(np.count_nonzero(mask)) / max(mask.size, 1), 4)
        total_ha = area_ha_from_bounds(patch.get("bounds"), projected=True)
        coverage = {
            "foreground_ratio": ratio,
            "total_area_ha": total_ha,
            "covered_area_ha": round(total_ha * ratio, 2) if total_ha is not None else None,
        }
        bounds_wgs84 = [float(v) for v in (patch.get("bounds_wgs84") or [])][:4]
        bounds = [float(v) for v in (patch.get("bounds") or [])][:4]
        return ({
            "patch_id": patch_id,
            "path": out_path,
            "bounds_wgs84": bounds_wgs84,
            "bounds": bounds,
        }, coverage)

    @staticmethod
    def _class_mask(
        rgb: np.ndarray, target_color: tuple[int, int, int] | None
    ) -> np.ndarray:
        if target_color is None:
            return custom_model_mask(rgb)
        diff = np.abs(rgb.astype(np.int16) - np.asarray(target_color, dtype=np.int16))
        return (diff <= 24).all(axis=-1)

    @staticmethod
    def _target_color(model: ModelInfo | None, target: str) -> tuple[int, int, int] | None:
        if model is None:
            return None
        for item in model.classes:
            name = str(item.get("name") or "")
            if not name or not (target == name or target in name or name in target):
                continue
            value = str(item.get("color") or "").lstrip("#")
            if len(value) == 6:
                try:
                    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
                except ValueError:
                    return None
        return None

    @staticmethod
    def _is_usable_patch(patch: Any, month: str) -> bool:
        if not isinstance(patch, dict) or not patch.get("patch_id") or not patch.get("has_embedding"):
            return False
        months = [str(item) for item in patch.get("available_months") or []]
        return not months or any(item == month or item.startswith(month) for item in months)

    @staticmethod
    def _bbox(aoi: Any) -> list[float]:
        if not isinstance(aoi, dict) or aoi.get("type") != "bbox":
            return []
        values = aoi.get("coordinates")
        if not isinstance(values, list) or len(values) != 4:
            return []
        try:
            bbox = [float(v) for v in values]
        except (TypeError, ValueError):
            return []
        return bbox if bbox[0] < bbox[2] and bbox[1] < bbox[3] else []

    @staticmethod
    def _union_wgs84(items: list[Any]) -> list[float] | None:
        valid = [[float(v) for v in item[:4]] for item in items if isinstance(item, list) and len(item) == 4]
        if not valid:
            return None
        return [
            min(v[0] for v in valid), min(v[1] for v in valid),
            max(v[2] for v in valid), max(v[3] for v in valid),
        ]

    def _build_result(
        self,
        *,
        request: ReportRequest,
        model: ModelInfo | None,
        model_id: str,
        target: str,
        month: str,
        aggregate: dict[str, Any],
        used_ids: list[str],
        rejected_ids: list[str],
        omitted_ids: list[str],
        patch_results: list[dict[str, Any]],
        charts: list[Any],
    ) -> AnalysisResult:
        ratio = aggregate.get("coverage_ratio")
        covered = aggregate.get("covered_area_ha")
        total = aggregate.get("total_area_ha")
        ratio_text = f"{float(ratio) * 100:.2f}%" if ratio is not None else "—"
        area_text = f"{float(covered):.2f} 公顷" if covered is not None else "—"
        status = f"成功分析 {len(used_ids)} 个 Patch"
        if rejected_ids:
            status += f"，{len(rejected_ids)} 个失败"
        if omitted_ids:
            status += f"，{len(omitted_ids)} 个超出上限"
        method = model.resolved_training_method if model else ""
        feature = model.feature_source if model else ""
        metadata = "；".join(v for v in [f"特征：{feature}" if feature else "", f"算法：{method}" if method else ""] if v)
        return AnalysisResult(
            task=f"{target}识别",
            region="北京市海淀区",
            time_range=self._display_month(month),
            headline=f"北京市海淀区{self._display_month(month)}{target}识别分析",
            summary=(
                f"基于训练完成的自定义模型，在所选 {len(used_ids)} 个 Patch 中识别{target}，"
                f"估算覆盖面积约 {area_text}、覆盖率约 {ratio_text}。{status}。"
            ),
            metrics=[
                MetricCard(f"{target}覆盖面积", area_text, f"按 {len(used_ids)} 个成功 Patch 汇总"),
                MetricCard(f"{target}覆盖率", ratio_text, "按 Patch 投影面积加权"),
                MetricCard("成功 Patch", str(len(used_ids)), status),
            ],
            findings=[
                f"当前选区内{target}估算覆盖率约 {ratio_text}，覆盖面积约 {area_text}。",
                f"模型成功返回 {len(used_ids)} 个 Patch 的结果，并已拼接为可叠加地图图层。",
            ],
            recommendations=[
                f"建议结合高分辨率影像或现场资料抽查{target}集中区域，确认自定义模型的泛化效果。",
                "如发现漏检或误检，可回到标注页面补充代表性样本后重新训练。",
            ],
            narrative_blocks=[{
                "title": "模型与处理流程",
                "text": (
                    f"Agent 从模型服务中匹配到单期自定义模型 {model_id}，"
                    f"通过批量推理接口处理所选 Patch，只保留『{target}』类别并生成透明地图图层。"
                ),
            }],
            risks=[f"有 {len(rejected_ids)} 个 Patch 未成功返回结果，统计仅覆盖成功区域。"] if rejected_ids else [],
            method_notes=[
                f"调用海淀模型接口 POST /models/{model_id}/infer_batch，单次批量处理所选 Patch。",
                f"目标类别按模型类别颜色提取，非目标区域设为透明。{metadata}".rstrip("。") + "。",
            ],
            limitations=[
                "面积按完整 Patch 的 UTM 投影范围估算；AOI 与 Patch 边界不完全重合时暂未做边界裁剪。",
                "当前接口返回分类结果 PNG，未返回逐像素置信度；报告不能代替独立精度验证。",
            ],
            confidence_notes=[
                "模型状态和类别来自在线模型注册表，结果图来自同一模型服务的实时推理接口。",
                "覆盖率由 Agent 对目标类别像素进行面积加权汇总。",
            ],
            data_source="haidian_embedding_api",
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            aef_payload={
                "service": self.config.base_url,
                "region_id": "haidian",
                "custom_model_id": model_id,
                "custom_class": target,
                "model_type": model.type if model else "single_time_detection",
                "month": month,
                "aggregate": aggregate,
                "used_patch_ids": used_ids,
                "failed_patch_ids": rejected_ids,
                "omitted_patch_ids": omitted_ids,
                "patch_results": patch_results,
                "fingerprint": hashlib.sha1(
                    f"{model_id}:{month}:{target}:{sorted(used_ids)}".encode()
                ).hexdigest()[:12],
            },
            charts=charts,
            patch_results=patch_results,
        )

    @staticmethod
    def _display_month(month: str) -> str:
        return f"{month[:4]}-{month[4:6]}" if len(month) >= 6 else month
