"""Fetch a high-resolution satellite basemap for a patch footprint.

Given a patch's WGS84 bounds, download Esri World Imagery tiles (WGS84 /
Web-Mercator, no GCJ-02 offset), stitch and crop them to the exact footprint,
and expose it as a report chart placed next to the model's thematic result for
a direct visual comparison. Everything is best-effort: any failure returns
None and the report is generated without the basemap.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from agent.schemas.report import ChartAsset

TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TILE_SIZE = 256
_MAX_TILES = 64  # safety cap on how many tiles we stitch
_MAX_OUTPUT_PX = 1024
_OPENER = build_opener(ProxyHandler({}))


def _lonlat_to_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n * TILE_SIZE
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
    return x, y


def _extent_px(bounds: list[float], zoom: int) -> tuple[float, float]:
    min_lng, min_lat, max_lng, max_lat = bounds
    left, top = _lonlat_to_pixel(min_lng, max_lat, zoom)
    right, bottom = _lonlat_to_pixel(max_lng, min_lat, zoom)
    return abs(right - left), abs(bottom - top)


def _pick_zoom(bounds: list[float], max_px: int = 1200, hi: int = 19, lo: int = 3) -> int:
    """Highest zoom whose footprint still fits within max_px on the long side."""
    for zoom in range(hi, lo - 1, -1):
        w, h = _extent_px(bounds, zoom)
        if max(w, h) <= max_px:
            return zoom
    return lo


def _valid_bounds(bounds: list[float]) -> bool:
    return (
        len(bounds) == 4
        and bounds[0] < bounds[2]
        and bounds[1] < bounds[3]
        and -180 <= bounds[0] <= 180
        and -180 <= bounds[2] <= 180
        and -85.05 <= bounds[1] <= 85.05
        and -85.05 <= bounds[3] <= 85.05
    )


def _download_tile(zoom: int, x: int, y: int, timeout: int):
    from PIL import Image  # local import so PIL stays optional at module load

    url = TILE_URL.format(z=zoom, y=y, x=x)
    request = Request(url, headers={"User-Agent": "Xuannv-Agent/1.0"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            import io

            return Image.open(io.BytesIO(response.read())).convert("RGB")
    except Exception:
        return None


def fetch_basemap(bounds: list[float], out_path: Path, timeout: int = 15) -> Path | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    bounds = [float(v) for v in bounds]
    if not _valid_bounds(bounds):
        return None
    if out_path.exists():
        return out_path

    zoom = _pick_zoom(bounds)
    min_lng, min_lat, max_lng, max_lat = bounds
    px_left, py_top = _lonlat_to_pixel(min_lng, max_lat, zoom)
    px_right, py_bottom = _lonlat_to_pixel(max_lng, min_lat, zoom)
    tx0, tx1 = int(px_left // TILE_SIZE), int(px_right // TILE_SIZE)
    ty0, ty1 = int(py_top // TILE_SIZE), int(py_bottom // TILE_SIZE)
    ncols, nrows = tx1 - tx0 + 1, ty1 - ty0 + 1
    if ncols < 1 or nrows < 1 or ncols * nrows > _MAX_TILES:
        return None

    canvas = Image.new("RGB", (ncols * TILE_SIZE, nrows * TILE_SIZE))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            tile = _download_tile(zoom, tx, ty, timeout)
            if tile is None:
                return None  # any missing tile -> abort (partial mosaics look broken)
            canvas.paste(tile, ((tx - tx0) * TILE_SIZE, (ty - ty0) * TILE_SIZE))

    crop = (
        int(px_left - tx0 * TILE_SIZE),
        int(py_top - ty0 * TILE_SIZE),
        int(round(px_right - tx0 * TILE_SIZE)),
        int(round(py_bottom - ty0 * TILE_SIZE)),
    )
    image = canvas.crop(crop)
    if image.width < 1 or image.height < 1:
        return None
    if max(image.size) > _MAX_OUTPUT_PX:
        ratio = _MAX_OUTPUT_PX / max(image.size)
        image = image.resize((max(1, int(image.width * ratio)), max(1, int(image.height * ratio))))
    try:
        image.save(out_path, "PNG")
    except OSError:
        return None
    return out_path


def basemap_chart(bounds: list[float] | None, asset_dir: Path, cache_key: str) -> ChartAsset | None:
    """Build the satellite-basemap ChartAsset for a patch footprint, or None."""
    if not bounds:
        return None
    digest = hashlib.sha1(f"{cache_key}-{list(bounds)}".encode("utf-8")).hexdigest()[:12]
    out_path = Path(asset_dir) / f"basemap_{digest}.png"
    path = fetch_basemap(list(bounds), out_path)
    if path is None:
        return None
    return ChartAsset(
        title="卫星影像（框选区域）",
        kind="image",
        url=f"/reports/assets/{path.name}",
        caption="所选区域的高清卫星影像，可与下方模型专题结果直接对比。",
    )
