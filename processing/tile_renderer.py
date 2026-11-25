"""Reproducible RGBA PNG map tile renderer with ETag/content-hash caching and provenance headers."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
import rasterio
from PIL import Image

from geo_climate.metrics import METRICS
from ingestion.generator import NODATA_VALUE

matplotlib.use("Agg")
from matplotlib import colormaps as mpl_colormaps  # noqa: E402

VALID_LAYERS = ("ndvi", "lst", "flood", "qa")

# Layer color normalization bounds & colormaps
LAYER_SPECS: dict[str, tuple[str, float, float]] = {
    "ndvi": ("RdYlGn", -0.2, 0.9),
    "lst": ("inferno", -10.0, 48.0),
    "flood": ("YlGnBu", -0.3, 0.65),
    "qa": ("viridis", 0.0, 1.0),
}


@dataclass(frozen=True)
class RenderedTile:
    """Rendered PNG tile payload with caching and scientific provenance headers."""

    png_bytes: bytes
    etag: str
    cache_status: str  # HIT or MISS
    layer: str
    region_id: str
    z: int
    x: int
    y: int
    crs: str
    stac_snapshot: str
    scene_version: int
    valid_pixel_pct: float
    render_ms: float


class ReproducibleTileServer:
    """Renders real 256x256 colormapped PNG tiles from Silver/Mosaic GeoTIFFs with content-addressed caching."""

    def __init__(self, cache_dir: Path, mosaics_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.mosaics_dir = mosaics_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._mem_cache: dict[str, tuple[bytes, str, dict[str, Any]]] = {}
        self.hits = 0
        self.misses = 0
        self.audit_events: list[dict[str, Any]] = []

    def invalidate_region(self, region_id: str) -> int:
        """Invalidate both memory and disk tile cache entries for a region when a new scene version lands."""
        removed = 0
        with self._lock:
            keys_to_drop = [k for k in self._mem_cache if k.startswith(f"{region_id}::")]
            for k in keys_to_drop:
                del self._mem_cache[k]
                removed += 1
        for f in self.cache_dir.glob(f"{region_id}__*.png"):
            f.unlink(missing_ok=True)
            removed += 1
        for f in self.cache_dir.glob(f"{region_id}__*.json"):
            f.unlink(missing_ok=True)
        return removed

    def _tile_slice_from_array(self, arr: np.ndarray, z: int, x: int, y: int) -> np.ndarray:
        """Extract sub-quadrant corresponding to tile (z, x, y) using deterministic pyramid subdivision."""
        h, w = arr.shape
        # Map z to local pyramid depth 0..3 so z=0..2 shows full/quadrant region cleanly
        depth = max(0, min(z, 3))
        grid = 1 << depth
        tx = x % grid
        ty = y % grid
        y0 = (ty * h) // grid
        y1 = max(y0 + 1, ((ty + 1) * h) // grid)
        x0 = (tx * w) // grid
        x1 = max(x0 + 1, ((tx + 1) * w) // grid)
        return arr[y0:y1, x0:x1]

    def _colorize_to_png(self, layer: str, sub_arr: np.ndarray, tile_px: int = 256) -> tuple[bytes, float]:
        """Convert 2D scientific float32 array into a crisp RGBA PNG image."""
        valid = np.isfinite(sub_arr) & (~np.isclose(sub_arr, NODATA_VALUE, atol=1e-3))
        valid_pct = round(float(np.mean(valid) * 100.0), 2)

        if layer == "qa":
            rgba = np.zeros((sub_arr.shape[0], sub_arr.shape[1], 4), dtype=np.uint8)
            # 0 = valid clear sky (emerald green), 1 = masked cloud/nodata (amber/rose)
            clear = valid & (sub_arr < 0.5)
            masked = ~clear
            rgba[clear] = np.array([34, 197, 94, 235], dtype=np.uint8)
            rgba[masked] = np.array([244, 63, 94, 225], dtype=np.uint8)
        else:
            cmap_name, vmin, vmax = LAYER_SPECS[layer]
            cmap = mpl_colormaps[cmap_name]
            norm = np.clip((np.where(valid, sub_arr, vmin) - vmin) / max(vmax - vmin, 1e-6), 0.0, 1.0)
            rgba_float = cmap(norm)
            rgba = (rgba_float * 255.0).astype(np.uint8)
            # Render masked/cloud/nodata pixels in dark slate translucent
            rgba[~valid] = np.array([30, 41, 59, 210], dtype=np.uint8)

        img = Image.fromarray(rgba, mode="RGBA").resize((tile_px, tile_px), resample=Image.Resampling.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), valid_pct

    def render_tile(
        self,
        *,
        layer: str,
        region_id: str,
        z: int,
        x: int,
        y: int,
        stac_snapshot: str,
        scene_version: int = 1,
        tile_px: int = 256,
    ) -> RenderedTile:
        """Serve a cached or freshly rendered PNG map tile with full provenance headers."""
        if layer not in VALID_LAYERS:
            raise ValueError(f"Unsupported layer '{layer}'; expected one of {VALID_LAYERS}")

        t0 = time.perf_counter()
        cache_key = f"{region_id}::{layer}::{z}::{x}::{y}::{stac_snapshot}::v{scene_version}"
        disk_stem = f"{region_id}__{layer}__{z}_{x}_{y}__{stac_snapshot}_v{scene_version}"
        disk_png = self.cache_dir / f"{disk_stem}.png"
        disk_meta = self.cache_dir / f"{disk_stem}.json"

        with self._lock:
            if cache_key in self._mem_cache:
                png_bytes, etag, meta = self._mem_cache[cache_key]
                self.hits += 1
                elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
                METRICS.inc("geoclimate_tile_requests_total", layer=layer, cache_status="HIT")
                METRICS.observe("geoclimate_tile_render_duration_ms", elapsed_ms, layer=layer, cache_status="HIT")
                return RenderedTile(
                    png_bytes=png_bytes,
                    etag=etag,
                    cache_status="HIT",
                    layer=layer,
                    region_id=region_id,
                    z=z,
                    x=x,
                    y=y,
                    crs=str(meta["crs"]),
                    stac_snapshot=stac_snapshot,
                    scene_version=scene_version,
                    valid_pixel_pct=float(meta["valid_pixel_pct"]),
                    render_ms=elapsed_ms,
                )

        if disk_png.exists() and disk_meta.exists():
            png_bytes = disk_png.read_bytes()
            meta = json.loads(disk_meta.read_text(encoding="utf-8"))
            etag = str(meta["etag"])
            with self._lock:
                self._mem_cache[cache_key] = (png_bytes, etag, meta)
                self.hits += 1
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            METRICS.inc("geoclimate_tile_requests_total", layer=layer, cache_status="HIT")
            METRICS.observe("geoclimate_tile_render_duration_ms", elapsed_ms, layer=layer, cache_status="HIT")
            return RenderedTile(
                png_bytes=png_bytes,
                etag=etag,
                cache_status="HIT",
                layer=layer,
                region_id=region_id,
                z=z,
                x=x,
                y=y,
                crs=str(meta["crs"]),
                stac_snapshot=stac_snapshot,
                scene_version=scene_version,
                valid_pixel_pct=float(meta["valid_pixel_pct"]),
                render_ms=elapsed_ms,
            )

        mosaic_tif = self.mosaics_dir / f"{region_id}_mosaic.tif"
        if not mosaic_tif.exists():
            raise FileNotFoundError(f"Mosaic GeoTIFF not found for region '{region_id}': {mosaic_tif}")

        band_map = {"ndvi": 1, "flood": 2, "lst": 3, "qa": 4}
        with rasterio.open(mosaic_tif) as ds:
            band_arr = ds.read(band_map[layer]).astype(np.float32)
            crs_str = f"EPSG:{ds.crs.to_epsg()}" if ds.crs and ds.crs.to_epsg() else "EPSG:4326"

        sub_arr = self._tile_slice_from_array(band_arr, z=z, x=x, y=y)
        png_bytes, valid_pct = self._colorize_to_png(layer, sub_arr, tile_px=tile_px)
        etag = f'"{hashlib.sha256(png_bytes + cache_key.encode()).hexdigest()[:24]}"'

        meta = {
            "etag": etag,
            "crs": crs_str,
            "valid_pixel_pct": valid_pct,
            "stac_snapshot": stac_snapshot,
            "scene_version": scene_version,
            "rendered_at_utc": datetime.now(UTC).isoformat(),
        }
        disk_png.write_bytes(png_bytes)
        disk_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        with self._lock:
            self._mem_cache[cache_key] = (png_bytes, etag, meta)
            self.misses += 1
            self.audit_events.append(
                {
                    "tile_key": cache_key,
                    "region_id": region_id,
                    "layer": layer,
                    "z": z,
                    "x": x,
                    "y": y,
                    "etag": etag,
                    "cache_status": "MISS",
                    "render_ms": elapsed_ms,
                    "valid_pixel_pct": valid_pct,
                    "stac_snapshot": stac_snapshot,
                    "scene_version": scene_version,
                    "served_at_utc": meta["rendered_at_utc"],
                }
            )

        METRICS.inc("geoclimate_tile_requests_total", layer=layer, cache_status="MISS")
        METRICS.observe("geoclimate_tile_render_duration_ms", elapsed_ms, layer=layer, cache_status="MISS")

        return RenderedTile(
            png_bytes=png_bytes,
            etag=etag,
            cache_status="MISS",
            layer=layer,
            region_id=region_id,
            z=z,
            x=x,
            y=y,
            crs=crs_str,
            stac_snapshot=stac_snapshot,
            scene_version=scene_version,
            valid_pixel_pct=valid_pct,
            render_ms=elapsed_ms,
        )

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "cache_hits": self.hits,
                "cache_misses": self.misses,
                "total_requests": total,
                "hit_rate_pct": round((self.hits / total) * 100.0, 2) if total else 0.0,
                "cached_mem_entries": len(self._mem_cache),
                "cached_disk_pngs": len(list(self.cache_dir.glob("*.png"))),
            }
