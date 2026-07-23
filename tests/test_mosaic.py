"""Tests for UTM-grid PNG mosaic stitching (agent/tools/mosaic.py)."""

from __future__ import annotations

from PIL import Image

from agent.tools.mosaic import stitch_tiles


def _png(path, size, colour):
    Image.new("RGBA", size, colour).save(path)
    return path


def test_stitch_two_vertical_tiles(tmp_path):
    # Two 128x128 tiles, same resolution, stacked north/south → 128x256 canvas.
    south = [0.0, 0.0, 1280.0, 1280.0]
    north = [0.0, 1280.0, 1280.0, 2560.0]
    tiles = [
        (south, _png(tmp_path / "s.png", (128, 128), (255, 0, 0, 255))),
        (north, _png(tmp_path / "n.png", (128, 128), (0, 0, 255, 255))),
    ]
    out = stitch_tiles(tiles, tmp_path / "mosaic.png")
    assert out is not None
    with Image.open(out) as im:
        assert im.size == (128, 256)
        # North tile pasted at the top (UTM y grows up, image y grows down).
        assert im.getpixel((64, 10))[:3] == (0, 0, 255)
        assert im.getpixel((64, 200))[:3] == (255, 0, 0)


def test_stitch_single_tile_returns_none(tmp_path):
    tiles = [([0.0, 0.0, 1280.0, 1280.0], _png(tmp_path / "a.png", (128, 128), (0, 0, 0, 0)))]
    assert stitch_tiles(tiles, tmp_path / "o.png") is None


def test_stitch_mismatched_resolution_returns_none(tmp_path):
    tiles = [
        ([0.0, 0.0, 1280.0, 1280.0], _png(tmp_path / "a.png", (128, 128), (0, 0, 0, 0))),
        ([0.0, 1280.0, 1280.0, 2560.0], _png(tmp_path / "b.png", (64, 64), (0, 0, 0, 0))),
    ]
    assert stitch_tiles(tiles, tmp_path / "o.png") is None


def test_stitch_missing_bounds_returns_none(tmp_path):
    tiles = [
        ([0.0, 0.0, 1280.0], _png(tmp_path / "a.png", (128, 128), (0, 0, 0, 0))),
        ([0.0, 1280.0, 1280.0, 2560.0], _png(tmp_path / "b.png", (128, 128), (0, 0, 0, 0))),
    ]
    assert stitch_tiles(tiles, tmp_path / "o.png") is None
