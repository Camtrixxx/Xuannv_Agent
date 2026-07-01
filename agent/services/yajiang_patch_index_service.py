from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.config import AGENT_ROOT, PROJECT_ROOT
from agent.services.common import bbox_intersection_score


DEFAULT_YAJIANG_RAW_ROOT = PROJECT_ROOT / "downloads" / "xuannv_embeddings" / "extracted" / "raw" / "yajiang"
DEFAULT_INDEX_PATH = AGENT_ROOT / "runtime" / "yajiang_patch_index.json"
DEFAULT_PERIOD = "2025Q3"
YAJIANG_AVAILABLE_MONTHS = [
    f"{year}-{month:02d}"
    for year in range(2023, 2027)
    for month in range(1, 13)
    if not (year == 2026 and month > 3)
]

TIFF_TYPE_SIZE = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    6: 1,
    7: 1,
    8: 2,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
}
TIFF_TYPE_FORMAT = {
    1: "B",
    2: "c",
    3: "H",
    4: "I",
    5: "II",
    6: "b",
    7: "B",
    8: "h",
    9: "i",
    10: "ii",
    11: "f",
    12: "d",
}


@dataclass(slots=True)
class YajiangPatchIndexConfig:
    raw_root: Path = DEFAULT_YAJIANG_RAW_ROOT
    index_path: Path = DEFAULT_INDEX_PATH
    reference_source: str = "s2"
    reference_period: str = DEFAULT_PERIOD

    @classmethod
    def from_env(cls) -> "YajiangPatchIndexConfig":
        return cls(
            raw_root=Path(os.getenv("AGENT_YAJIANG_RAW_ROOT", str(DEFAULT_YAJIANG_RAW_ROOT))),
            index_path=Path(os.getenv("AGENT_YAJIANG_PATCH_INDEX", str(DEFAULT_INDEX_PATH))),
            reference_source=os.getenv("AGENT_YAJIANG_INDEX_SOURCE", "s2"),
            reference_period=os.getenv("AGENT_YAJIANG_INDEX_PERIOD", DEFAULT_PERIOD),
        )


class YajiangPatchIndexService:
    """Local spatial index for Yajiang raw GeoTIFF patches."""

    def __init__(self, config: YajiangPatchIndexConfig | None = None) -> None:
        self.config = config or YajiangPatchIndexConfig.from_env()
        self._patches: list[dict[str, Any]] | None = None

    def search(self, bbox: list[float], task_id: str, time_range: str, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for patch in self.patches:
            if time_range and time_range not in patch.get("available_months", []):
                continue
            if task_id not in patch.get("available_tasks", []):
                continue
            score = bbox_intersection_score([float(v) for v in patch["bounds_wgs84"]], bbox)
            if score <= 0:
                continue
            item = dict(patch)
            item["score"] = round(score, 6)
            item["task_available"] = True
            rows.append(item)
        rows.sort(key=lambda item: (item.get("score", 0), item.get("patch_id", "")), reverse=True)
        return rows[:limit]

    @property
    def patches(self) -> list[dict[str, Any]]:
        if self._patches is None:
            self._patches = self._load_or_build()
        return self._patches

    def _load_or_build(self) -> list[dict[str, Any]]:
        index_path = self.config.index_path
        if index_path.exists():
            with index_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            patches = payload.get("patches") or []
            if patches:
                return patches
        patches = self._build_index()
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with index_path.open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "region_id": "yajiang",
                    "raw_root": str(self.config.raw_root),
                    "reference_source": self.config.reference_source,
                    "reference_period": self.config.reference_period,
                    "patch_count": len(patches),
                    "patches": patches,
                },
                fh,
                ensure_ascii=False,
                indent=2,
            )
        return patches

    def _build_index(self) -> list[dict[str, Any]]:
        try:
            from pyproj import Transformer
        except ImportError as exc:
            raise RuntimeError("雅江 patch 空间索引需要 pyproj，请在服务环境安装 pyproj。") from exc

        source_root = self.config.raw_root / self.config.reference_source
        if not source_root.exists():
            raise FileNotFoundError(f"雅江原始 patch 目录不存在：{source_root}")

        patches: list[dict[str, Any]] = []
        transformers: dict[int, Any] = {}
        for patch_dir in sorted(source_root.glob("patch_*")):
            if not patch_dir.is_dir():
                continue
            tif_path = patch_dir / f"{self.config.reference_period}.tif"
            if not tif_path.exists():
                candidates = sorted(patch_dir.glob("*.tif"))
                if not candidates:
                    continue
                tif_path = candidates[0]
            meta = _read_geotiff_meta(tif_path)
            epsg = int(meta["epsg"])
            transformer = transformers.get(epsg)
            if transformer is None:
                transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
                transformers[epsg] = transformer
            bounds_wgs84 = _bounds_wgs84(meta["transform"], int(meta["width"]), int(meta["height"]), transformer)
            patch_index = int(patch_dir.name.split("_", 1)[1])
            patches.append(
                {
                    "patch_id": patch_dir.name,
                    "sample_index": patch_index,
                    "bounds_wgs84": [round(value, 6) for value in bounds_wgs84],
                    "crs": f"EPSG:{epsg}",
                    "source": self.config.reference_source,
                    "reference_period": tif_path.stem,
                    "has_embedding": True,
                    "available_months": YAJIANG_AVAILABLE_MONTHS,
                    "available_tasks": ["landcover", "water", "dem"],
                }
            )
        if not patches:
            raise RuntimeError(f"没有从 {source_root} 生成任何雅江 patch 索引。")
        return patches


def _read_geotiff_meta(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    byte_order = ">" if data[:2] == b"MM" else "<"
    magic = struct.unpack(byte_order + "H", data[2:4])[0]
    if magic != 42:
        raise ValueError(f"暂不支持的 TIFF 格式：{path}")
    ifd_offset = struct.unpack(byte_order + "I", data[4:8])[0]
    tags = _read_ifd_tags(data, byte_order, ifd_offset)
    transform = tags.get(34264)
    geokeys = tags.get(34735)
    if not transform or not geokeys:
        raise ValueError(f"GeoTIFF 缺少空间标签：{path}")
    return {
        "width": tags[256],
        "height": tags[257],
        "transform": transform,
        "epsg": _epsg_from_geokeys(geokeys),
    }


def _read_ifd_tags(data: bytes, byte_order: str, ifd_offset: int) -> dict[int, Any]:
    tag_count = struct.unpack(byte_order + "H", data[ifd_offset : ifd_offset + 2])[0]
    tags: dict[int, Any] = {}
    for idx in range(tag_count):
        offset = ifd_offset + 2 + idx * 12
        tag, value_type, count = struct.unpack(byte_order + "HHI", data[offset : offset + 8])
        value_size = TIFF_TYPE_SIZE.get(value_type, 1) * count
        value_bytes = data[offset + 8 : offset + 12]
        if value_size <= 4:
            raw = value_bytes[:value_size]
        else:
            pointer = struct.unpack(byte_order + "I", value_bytes)[0]
            raw = data[pointer : pointer + value_size]
        tags[tag] = _decode_tiff_value(raw, byte_order, value_type, count)
    return tags


def _decode_tiff_value(raw: bytes, byte_order: str, value_type: int, count: int) -> Any:
    value_format = TIFF_TYPE_FORMAT.get(value_type)
    if value_type == 2:
        return raw.rstrip(b"\x00").decode("utf-8", "ignore")
    if value_format and value_type in {5, 10}:
        values = []
        for idx in range(count):
            a, b = struct.unpack(byte_order + value_format, raw[idx * 8 : idx * 8 + 8])
            values.append(a / b if b else None)
        return tuple(values)
    if value_format:
        if value_type in {1, 6, 7}:
            values = tuple(raw[:count])
        else:
            values = struct.unpack(byte_order + str(count) + value_format, raw)
        return values[0] if len(values) == 1 else values
    return raw


def _epsg_from_geokeys(geokeys: tuple[int, ...]) -> int:
    key_count = int(geokeys[3])
    for idx in range(key_count):
        base = 4 + idx * 4
        key_id, _, _, value = geokeys[base : base + 4]
        if key_id == 3072:
            return int(value)
    raise ValueError("GeoTIFF GeoKeyDirectory 中没有 ProjectedCSTypeGeoKey。")


def _bounds_wgs84(transform: tuple[float, ...], width: int, height: int, transformer: Any) -> list[float]:
    corners = [
        _apply_transform(transform, 0, 0),
        _apply_transform(transform, width, 0),
        _apply_transform(transform, width, height),
        _apply_transform(transform, 0, height),
    ]
    lonlat = [transformer.transform(x, y) for x, y in corners]
    lons = [pt[0] for pt in lonlat]
    lats = [pt[1] for pt in lonlat]
    return [min(lons), min(lats), max(lons), max(lats)]


def _apply_transform(transform: tuple[float, ...], col: float, row: float) -> tuple[float, float]:
    x = transform[0] * col + transform[1] * row + transform[3]
    y = transform[4] * col + transform[5] * row + transform[7]
    return x, y
