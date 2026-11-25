"""Unit tests for real PNG map tile rendering, ETag determinism, and cache invalidation."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.crs import CRS
from rasterio.transform import from_bounds

from processing.tile_renderer import VALID_LAYERS, ReproducibleTileServer


def _write_dummy_mosaic(path: Path) -> None:
    arr = np.zeros((4, 32, 32), dtype=np.float32)
    arr[0] = np.linspace(-0.1, 0.85, 32 * 32, dtype=np.float32).reshape(32, 32)  # ndvi
    arr[1] = np.linspace(-0.2, 0.55, 32 * 32, dtype=np.float32).reshape(32, 32)  # ndwi
    arr[2] = np.linspace(5.0, 42.0, 32 * 32, dtype=np.float32).reshape(32, 32)   # lst_c
    arr[3] = 0.0                                                                  # qa_invalid
    profile = {
        "driver": "GTiff",
        "height": 32,
        "width": 32,
        "count": 4,
        "dtype": "float32",
        "crs": CRS.from_epsg(4326),
        "transform": from_bounds(-122.0, 36.5, -119.5, 39.0, 32, 32),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr)


def test_tile_renderer_produces_valid_png_and_caches_and_invalidates(tmp_path: Path) -> None:
    mosaics_dir = tmp_path / "mosaics"
    mosaics_dir.mkdir()
    _write_dummy_mosaic(mosaics_dir / "california_central_valley_mosaic.tif")

    server = ReproducibleTileServer(tmp_path / "cache", mosaics_dir)
    for layer in VALID_LAYERS:
        t1 = server.render_tile(
            layer=layer,
            region_id="california_central_valley",
            z=0,
            x=0,
            y=0,
            stac_snapshot="snap-100",
            scene_version=1,
        )
        assert t1.cache_status == "MISS"
        assert t1.png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        img = Image.open(io.BytesIO(t1.png_bytes))
        assert img.size == (256, 256)
        assert img.mode == "RGBA"

        t2 = server.render_tile(
            layer=layer,
            region_id="california_central_valley",
            z=0,
            x=0,
            y=0,
            stac_snapshot="snap-100",
            scene_version=1,
        )
        assert t2.cache_status == "HIT"
        assert t2.etag == t1.etag

    removed = server.invalidate_region("california_central_valley")
    assert removed > 0

    t3 = server.render_tile(
        layer="ndvi",
        region_id="california_central_valley",
        z=0,
        x=0,
        y=0,
        stac_snapshot="snap-101",
        scene_version=2,
    )
    assert t3.cache_status == "MISS"
    assert t3.scene_version == 2
