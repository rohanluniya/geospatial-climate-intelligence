"""Chunked xarray + Dask spatiotemporal raster cube, QA cloud masking, and cloud-free mosaics."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import dask.array as da
import numpy as np
import rasterio
import xarray as xr
from rasterio.crs import CRS as RasterioCRS
from rasterio.transform import Affine

from ingestion.generator import NODATA_VALUE
from processing.crs_and_geometry import NormalizedRaster


@dataclass(frozen=True)
class MosaicResult:
    """Cloud-free composite mosaic computed across overlapping acquisition dates."""

    region_id: str
    window_label: str
    scene_ids: list[str]
    ndvi: np.ndarray
    ndwi: np.ndarray
    lst_c: np.ndarray
    qa_valid_mask: np.ndarray
    valid_pixel_pct: float
    single_scene_min_valid_pct: float
    transform: Affine
    crs_epsg: int
    bounds: tuple[float, float, float, float]
    output_tif_path: Path | None = None


def apply_qa_and_nodata_mask(
    data: np.ndarray,
    nodata_value: float = NODATA_VALUE,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Mask cloud (qa==1), cloud shadow (qa==2), and nodata (qa==3 or band==nodata) pixels to NaN.

    Args:
        data: float32 array of shape (6, H, W) -> [red, green, nir, swir, lst_k, qa_mask].

    Returns:
        (masked_spectral_and_thermal [5, H, W], valid_bool_mask [H, W], valid_pixel_pct)
    """
    spectral = data[:5].astype(np.float32, copy=True)
    qa = np.rint(data[5])

    nodata_any = np.any(np.isclose(spectral, nodata_value, atol=1e-3) | (~np.isfinite(spectral)), axis=0)
    # Also enforce strictly positive reflectance on bands 0..3 and physical Kelvin on band 4
    invalid_phys = (
        np.any(spectral[:4] <= 0.0, axis=0)
        | (spectral[4] < 180.0)
        | (spectral[4] > 370.0)
    )
    qa_clear = np.isclose(qa, 0.0, atol=0.1)
    valid_mask = qa_clear & (~nodata_any) & (~invalid_phys)

    masked = np.where(valid_mask[None, :, :], spectral, np.nan).astype(np.float32)
    valid_pct = float(np.mean(valid_mask) * 100.0)
    return masked, valid_mask, round(valid_pct, 2)


def compute_indices_from_masked(masked_5band: np.ndarray) -> dict[str, np.ndarray]:
    """Compute NDVI, NDWI (McFeeters), MNDWI, and LST (C) with strict NaN propagation."""
    red = masked_5band[0]
    green = masked_5band[1]
    nir = masked_5band[2]
    swir = masked_5band[3]
    lst_k = masked_5band[4]

    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi_denom = nir + red
        ndvi = np.where(ndvi_denom > 1e-6, (nir - red) / ndvi_denom, np.nan)
        ndvi = np.clip(ndvi, -1.0, 1.0).astype(np.float32)

        ndwi_denom = green + nir
        ndwi = np.where(ndwi_denom > 1e-6, (green - nir) / ndwi_denom, np.nan)
        ndwi = np.clip(ndwi, -1.0, 1.0).astype(np.float32)

        mndwi_denom = green + swir
        mndwi = np.where(mndwi_denom > 1e-6, (green - swir) / mndwi_denom, np.nan)
        mndwi = np.clip(mndwi, -1.0, 1.0).astype(np.float32)

    lst_c = (lst_k - 273.15).astype(np.float32)
    return {"ndvi": ndvi, "ndwi": ndwi, "mndwi": mndwi, "lst_c": lst_c}


def build_spatiotemporal_cube(
    region_id: str,
    scenes: list[tuple[str, str, NormalizedRaster]],
    *,
    chunk_size: int = 48,
) -> xr.Dataset:
    """Build a lazy Dask-backed xarray.Dataset with dims (time, y, x) and masked climate indices.

    Args:
        region_id: Region identifier.
        scenes: List of (scene_id, acquisition_date, NormalizedRaster) sorted by date.
        chunk_size: Spatial Dask chunk edge length in pixels.
    """
    if not scenes:
        raise ValueError(f"No scenes provided for cube {region_id}")

    ordered = sorted(scenes, key=lambda item: (item[1], item[0]))
    first_raster = ordered[0][2]
    _, height, width = first_raster.data.shape
    minx, miny, maxx, maxy = first_raster.bounds

    xs = np.linspace(minx, maxx, width, dtype=np.float64)
    ys = np.linspace(maxy, miny, height, dtype=np.float64)  # north-up raster ordering

    ndvi_slices: list[np.ndarray] = []
    ndwi_slices: list[np.ndarray] = []
    mndwi_slices: list[np.ndarray] = []
    lst_slices: list[np.ndarray] = []
    valid_slices: list[np.ndarray] = []
    qa_slices: list[np.ndarray] = []
    times: list[str] = []
    scene_ids: list[str] = []

    for scene_id, acq_date, norm_raster in ordered:
        masked, valid_mask, _ = apply_qa_and_nodata_mask(norm_raster.data, norm_raster.nodata)
        idx = compute_indices_from_masked(masked)
        ndvi_slices.append(idx["ndvi"])
        ndwi_slices.append(idx["ndwi"])
        mndwi_slices.append(idx["mndwi"])
        lst_slices.append(idx["lst_c"])
        valid_slices.append(valid_mask.astype(np.uint8))
        qa_slices.append(np.rint(norm_raster.data[5]).astype(np.uint8))
        times.append(acq_date)
        scene_ids.append(scene_id)

    chunks = (1, min(chunk_size, height), min(chunk_size, width))
    ds = xr.Dataset(
        data_vars={
            "ndvi": (("time", "y", "x"), da.from_array(np.stack(ndvi_slices, axis=0), chunks=chunks)),
            "ndwi": (("time", "y", "x"), da.from_array(np.stack(ndwi_slices, axis=0), chunks=chunks)),
            "mndwi": (("time", "y", "x"), da.from_array(np.stack(mndwi_slices, axis=0), chunks=chunks)),
            "lst_c": (("time", "y", "x"), da.from_array(np.stack(lst_slices, axis=0), chunks=chunks)),
            "valid_mask": (("time", "y", "x"), da.from_array(np.stack(valid_slices, axis=0), chunks=chunks)),
            "qa_mask": (("time", "y", "x"), da.from_array(np.stack(qa_slices, axis=0), chunks=chunks)),
        },
        coords={
            "time": times,
            "scene_id": ("time", scene_ids),
            "y": ys,
            "x": xs,
        },
        attrs={
            "region_id": region_id,
            "crs_epsg": first_raster.crs_epsg,
            "antimeridian_unwrapped": int(first_raster.antimeridian_unwrapped),
            "bounds": list(first_raster.bounds),
            "dask_chunk_size": chunk_size,
        },
    )
    return ds


def compute_cloud_free_mosaic(
    cube: xr.Dataset,
    *,
    transform: Affine,
    window_label: str = "all_dates_median",
    time_indices: list[int] | None = None,
    output_tif_path: Path | None = None,
) -> MosaicResult:
    """Compute a Dask-evaluated cloud-free median composite across overlapping dates in a cube."""
    sub = cube.isel(time=time_indices) if time_indices is not None else cube
    # Compute Dask arrays lazily and materialize once
    ndvi_stack = sub["ndvi"].compute().values
    ndwi_stack = sub["ndwi"].compute().values
    lst_stack = sub["lst_c"].compute().values
    valid_stack = sub["valid_mask"].compute().values

    with np.errstate(all="ignore"):
        ndvi_med = np.nanmedian(ndvi_stack, axis=0).astype(np.float32)
        ndwi_med = np.nanmedian(ndwi_stack, axis=0).astype(np.float32)
        lst_med = np.nanmedian(lst_stack, axis=0).astype(np.float32)

    valid_any = np.isfinite(ndvi_med) & np.isfinite(lst_med)
    valid_pct = round(float(np.mean(valid_any) * 100.0), 2)
    single_valid_pcts = [float(np.mean(valid_stack[i] > 0) * 100.0) for i in range(valid_stack.shape[0])]
    min_single = round(min(single_valid_pcts), 2) if single_valid_pcts else 0.0

    scene_ids = [str(s) for s in sub["scene_id"].values.tolist()]
    crs_epsg = int(cube.attrs["crs_epsg"])
    bounds_tuple = tuple(float(v) for v in cube.attrs["bounds"])
    bounds4 = (bounds_tuple[0], bounds_tuple[1], bounds_tuple[2], bounds_tuple[3])

    if output_tif_path is not None:
        output_tif_path.parent.mkdir(parents=True, exist_ok=True)
        # Write 4-band mosaic GeoTIFF: ndvi, ndwi, lst_c, valid_mask
        qa_out = np.where(valid_any, 0.0, 1.0).astype(np.float32)
        out_stack = np.stack(
            [
                np.where(np.isfinite(ndvi_med), ndvi_med, NODATA_VALUE),
                np.where(np.isfinite(ndwi_med), ndwi_med, NODATA_VALUE),
                np.where(np.isfinite(lst_med), lst_med, NODATA_VALUE),
                qa_out,
            ],
            axis=0,
        ).astype(np.float32)
        profile = {
            "driver": "GTiff",
            "height": out_stack.shape[1],
            "width": out_stack.shape[2],
            "count": 4,
            "dtype": "float32",
            "crs": RasterioCRS.from_epsg(crs_epsg),
            "transform": transform,
            "nodata": NODATA_VALUE,
            "tiled": True,
            "blockxsize": 32,
            "blockysize": 32,
            "compress": "DEFLATE",
        }
        with rasterio.open(output_tif_path, "w", **profile) as dst:
            dst.write(out_stack)
            for idx, name in enumerate(("ndvi", "ndwi", "lst_c", "qa_invalid"), start=1):
                dst.set_band_description(idx, name)

    return MosaicResult(
        region_id=str(cube.attrs["region_id"]),
        window_label=window_label,
        scene_ids=scene_ids,
        ndvi=ndvi_med,
        ndwi=ndwi_med,
        lst_c=lst_med,
        qa_valid_mask=valid_any,
        valid_pixel_pct=valid_pct,
        single_scene_min_valid_pct=min_single,
        transform=transform,
        crs_epsg=crs_epsg,
        bounds=bounds4,
        output_tif_path=output_tif_path,
    )


def process_tiles_with_partial_failure_isolation(
    array_2d: np.ndarray,
    tile_size: int = 32,
    *,
    fail_tile_coords: set[tuple[int, int]] | None = None,
) -> dict[str, Any]:
    """Process a raster in spatial tiles while isolating individual tile failures without losing sibling tiles."""
    height, width = array_2d.shape
    failed_tiles: list[dict[str, Any]] = []
    succeeded_tiles = 0
    out = np.full_like(array_2d, np.nan, dtype=np.float32)
    injected = fail_tile_coords or set()

    for ty, y0 in enumerate(range(0, height, tile_size)):
        for tx, x0 in enumerate(range(0, width, tile_size)):
            y1 = min(y0 + tile_size, height)
            x1 = min(x0 + tile_size, width)
            try:
                if (ty, tx) in injected:
                    raise RuntimeError(f"Injected partial tile I/O failure at tile ({ty}, {tx})")
                tile = array_2d[y0:y1, x0:x1]
                out[y0:y1, x0:x1] = tile
                succeeded_tiles += 1
            except Exception as exc:
                failed_tiles.append(
                    {"tile_y": ty, "tile_x": tx, "bounds_px": [y0, x0, y1, x1], "error": str(exc)}
                )

    total_tiles = succeeded_tiles + len(failed_tiles)
    return {
        "output": out,
        "total_tiles": total_tiles,
        "succeeded_tiles": succeeded_tiles,
        "failed_tiles": failed_tiles,
        "tile_success_pct": round((succeeded_tiles / max(total_tiles, 1)) * 100.0, 2),
    }
