"""CRS normalization, antimeridian polygon splitting, polar validation, and GeoParquet I/O."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from pyproj import CRS, Transformer
from rasterio.crs import CRS as RasterioCRS
from rasterio.transform import Affine, from_bounds
from rasterio.warp import Resampling, reproject
from shapely import wkb
from shapely.affinity import translate
from shapely.geometry import MultiPolygon, Polygon, box, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


@dataclass(frozen=True)
class NormalizedRaster:
    """In-memory representation of a CRS-normalized multi-band raster."""

    data: np.ndarray  # shape: (bands, height, width), float32
    transform: Affine
    crs_epsg: int
    bounds: tuple[float, float, float, float]
    nodata: float
    antimeridian_unwrapped: bool = False


def split_antimeridian_bbox(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
) -> MultiPolygon | Polygon:
    """Split a bounding box that crosses the 180th meridian into strictly valid [-180, 180] geometry.

    Accepts either:
      - wrapped crossing coordinates where ``min_lon > max_lon`` (e.g., 179.0 to -179.0), OR
      - unwrapped longitudes exceeding 180.0 (e.g., 179.0 to 181.0) or below -180.0.
    """
    if not (-90.0 <= min_lat < max_lat <= 90.0):
        raise ValueError(f"Invalid latitude bounds: ({min_lat}, {max_lat})")

    if min_lon > max_lon:
        # Wrapped antimeridian convention (e.g. 179.0 -> -179.0)
        unwrapped_min = min_lon
        unwrapped_max = max_lon + 360.0
    else:
        unwrapped_min = min_lon
        unwrapped_max = max_lon

    raw_poly = box(unwrapped_min, min_lat, unwrapped_max, max_lat)
    return split_antimeridian_geometry(raw_poly)


def split_antimeridian_geometry(geom: BaseGeometry) -> MultiPolygon | Polygon:
    """Normalize any planar lon/lat polygon possibly extending past +-180 into valid [-180, 180] parts."""
    if geom.is_empty:
        raise ValueError("Cannot split empty geometry")

    minx, miny, maxx, maxy = geom.bounds
    if miny < -90.0 or maxy > 90.0:
        raise ValueError(f"Latitude out of [-90, 90]: ({miny}, {maxy})")

    if -180.0 <= minx and maxx <= 180.0:
        fixed = geom if geom.is_valid else geom.buffer(0)
        if isinstance(fixed, (Polygon, MultiPolygon)):
            return fixed
        raise ValueError(f"Unsupported geometry type: {fixed.geom_type}")

    parts: list[Polygon] = []
    # Slice across 360-degree bands from -540 to +540 to cover any unwrapped range
    for shift in (-360.0, 0.0, 360.0):
        band = box(-180.0 + shift, -90.0, 180.0 + shift, 90.0)
        clipped = geom.intersection(band)
        if clipped.is_empty:
            continue
        shifted = translate(clipped, xoff=-shift, yoff=0.0)
        if isinstance(shifted, Polygon):
            if shifted.area > 0:
                parts.append(shifted)
        elif isinstance(shifted, MultiPolygon):
            for p in shifted.geoms:
                if p.area > 0:
                    parts.append(p)
        else:
            for sub in getattr(shifted, "geoms", []):
                if isinstance(sub, Polygon) and sub.area > 0:
                    parts.append(sub)

    if not parts:
        raise ValueError("Antimeridian split produced no valid polygon parts")

    merged = unary_union(parts)
    if isinstance(merged, Polygon):
        return merged
    if isinstance(merged, MultiPolygon):
        return merged
    polys = [g for g in getattr(merged, "geoms", []) if isinstance(g, Polygon) and g.area > 0]
    return MultiPolygon(polys) if len(polys) > 1 else polys[0]


def geometry_to_unwrapped_pacific(geom: BaseGeometry) -> BaseGeometry:
    """Shift western-hemisphere (<0 lon) parts of an antimeridian polygon by +360 for seamless raster masking."""
    if isinstance(geom, Polygon):
        minx, _, _, _ = geom.bounds
        return translate(geom, xoff=360.0) if minx < 0.0 else geom
    if isinstance(geom, MultiPolygon):
        shifted = [
            translate(p, xoff=360.0) if p.bounds[0] < 0.0 else p for p in geom.geoms
        ]
        return unary_union(shifted)
    return geom


def validate_wgs84_bounds(geom: BaseGeometry) -> tuple[bool, str]:
    """Verify geometry validity and strict [-180, 180] x [-90, 90] coordinate containment."""
    if geom.is_empty:
        return False, "Geometry is empty"
    if not geom.is_valid:
        return False, "Geometry is topologically invalid"
    minx, miny, maxx, maxy = geom.bounds
    if minx < -180.0 - 1e-9 or maxx > 180.0 + 1e-9:
        return False, f"Longitude out of [-180, 180]: ({minx}, {maxx})"
    if miny < -90.0 - 1e-9 or maxy > 90.0 + 1e-9:
        return False, f"Latitude out of [-90, 90]: ({miny}, {maxy})"
    return True, "ok"


def roundtrip_crs_error_meters(lon: float, lat: float, target_epsg: int) -> float:
    """Measure forward + inverse CRS projection round-trip displacement error in meters."""
    fwd = Transformer.from_crs("EPSG:4326", f"EPSG:{target_epsg}", always_xy=True)
    inv = Transformer.from_crs(f"EPSG:{target_epsg}", "EPSG:4326", always_xy=True)
    x, y = fwd.transform(lon, lat)
    lon2, lat2 = inv.transform(x, y)
    # Approximate geodesic distance error in meters
    dlat_m = (lat2 - lat) * 111_320.0
    dlon_m = (lon2 - lon) * 111_320.0 * max(np.cos(np.radians(lat)), 1e-6)
    return float(np.hypot(dlat_m, dlon_m))


def compute_projected_bounds(
    wgs84_bbox: tuple[float, float, float, float],
    target_epsg: int,
    antimeridian: bool = False,
) -> tuple[float, float, float, float]:
    """Project a WGS84 bounding box into target_epsg (or continuous [179, 181] if antimeridian EPSG:4326)."""
    min_lon, min_lat, max_lon, max_lat = wgs84_bbox
    if antimeridian and target_epsg == 4326:
        unwrapped_max = max_lon + 360.0 if min_lon > max_lon else max_lon
        return (min_lon, min_lat, unwrapped_max, max_lat)
    if target_epsg == 4326:
        return (min_lon, min_lat, max_lon, max_lat)

    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{target_epsg}", always_xy=True)
    # Sample boundary points to preserve curved edges at high latitudes (e.g. EPSG:3413 polar stereographic)
    lons = np.linspace(min_lon, max_lon, 17)
    lats = np.linspace(min_lat, max_lat, 17)
    xs: list[float] = []
    ys: list[float] = []
    for lon_val in lons:
        for lat_edge in (min_lat, max_lat):
            x, y = transformer.transform(float(lon_val), float(lat_edge))
            xs.append(x)
            ys.append(y)
    for lat_val in lats:
        for lon_edge in (min_lon, max_lon):
            x, y = transformer.transform(float(lon_edge), float(lat_val))
            xs.append(x)
            ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


def reproject_raster_to_canonical(
    src_path: Path,
    target_wgs84_bbox: tuple[float, float, float, float],
    target_width: int,
    target_height: int,
    *,
    canonical_epsg: int = 4326,
    antimeridian: bool = False,
    nodata_value: float = -9999.0,
    dst_path: Path | None = None,
) -> NormalizedRaster:
    """Reproject a multi-band GeoTIFF from any source CRS (UTM, Polar Stereographic, WGS84) onto a canonical grid.

    Continuous spectral/thermal bands (1..N-1) use bilinear resampling; the final QA/cloud bitmask band uses
    nearest-neighbor resampling so categorical QA flags are never blended.
    """
    min_lon, min_lat, max_lon, max_lat = target_wgs84_bbox
    if antimeridian and min_lon > max_lon:
        max_lon = max_lon + 360.0

    dst_transform = from_bounds(min_lon, min_lat, max_lon, max_lat, target_width, target_height)
    dst_crs = RasterioCRS.from_epsg(canonical_epsg)

    with rasterio.open(src_path) as src:
        band_count = src.count
        src_nodata = src.nodata if src.nodata is not None else nodata_value
        dst_array = np.full((band_count, target_height, target_width), nodata_value, dtype=np.float32)

        for band_idx in range(1, band_count + 1):
            src_band = src.read(band_idx).astype(np.float32)
            # Last band is QA/Cloud categorical mask -> nearest neighbor; spectral bands -> bilinear
            resampling_alg = Resampling.nearest if band_idx == band_count else Resampling.bilinear
            reproject(
                source=src_band,
                destination=dst_array[band_idx - 1],
                src_transform=src.transform,
                src_crs=src.crs,
                src_nodata=src_nodata,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                dst_nodata=nodata_value,
                resampling=resampling_alg,
            )

        if dst_path is not None:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            profile = {
                "driver": "GTiff",
                "height": target_height,
                "width": target_width,
                "count": band_count,
                "dtype": "float32",
                "crs": dst_crs,
                "transform": dst_transform,
                "nodata": nodata_value,
                "tiled": True,
                "blockxsize": 32,
                "blockysize": 32,
                "compress": "DEFLATE",
            }
            with rasterio.open(dst_path, "w", **profile) as dst:
                dst.write(dst_array)
                for idx, desc in enumerate(src.descriptions, start=1):
                    if desc:
                        dst.set_band_description(idx, desc)

    return NormalizedRaster(
        data=dst_array,
        transform=dst_transform,
        crs_epsg=canonical_epsg,
        bounds=(min_lon, min_lat, max_lon, max_lat),
        nodata=nodata_value,
        antimeridian_unwrapped=antimeridian,
    )


def write_geoparquet(
    records: list[dict[str, Any]],
    output_path: Path,
    *,
    geometry_column: str = "geometry",
    crs_epsg: int = 4326,
) -> dict[str, Any]:
    """Write vector polygons with ISO WKB encoding and compliant GeoParquet v1.1.0 schema metadata."""
    if not records:
        raise ValueError("Cannot write empty GeoParquet table")

    wkb_values: list[bytes] = []
    geom_types: set[str] = set()
    all_bounds: list[tuple[float, float, float, float]] = []

    non_geom_keys = [k for k in records[0].keys() if k != geometry_column]
    columns: dict[str, list[Any]] = {k: [] for k in non_geom_keys}

    for rec in records:
        geom = rec[geometry_column]
        if not isinstance(geom, BaseGeometry):
            raise TypeError(f"Expected Shapely geometry in '{geometry_column}', got {type(geom)}")
        ok, err = validate_wgs84_bounds(geom)
        if not ok:
            raise ValueError(f"Invalid WGS84 geometry in GeoParquet write: {err}")
        wkb_values.append(wkb.dumps(geom))
        geom_types.add(geom.geom_type)
        all_bounds.append(geom.bounds)
        for k in non_geom_keys:
            columns[k].append(rec[k])

    minx = min(b[0] for b in all_bounds)
    miny = min(b[1] for b in all_bounds)
    maxx = max(b[2] for b in all_bounds)
    maxy = max(b[3] for b in all_bounds)

    crs_json = CRS.from_epsg(crs_epsg).to_json_dict()
    geo_metadata = {
        "version": "1.1.0",
        "primary_column": geometry_column,
        "columns": {
            geometry_column: {
                "encoding": "WKB",
                "geometry_types": sorted(geom_types),
                "crs": crs_json,
                "bbox": [round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)],
                "edges": "planar",
            }
        },
    }

    arrays: dict[str, pa.Array] = {k: pa.array(v) for k, v in columns.items()}
    arrays[geometry_column] = pa.array(wkb_values, type=pa.binary())
    table = pa.table(arrays)

    existing_meta = dict(table.schema.metadata or {})
    existing_meta[b"geo"] = json.dumps(geo_metadata).encode("utf-8")
    table = table.replace_schema_metadata(existing_meta)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, output_path, compression="zstd")
    return geo_metadata


def read_geoparquet(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read a GeoParquet file, returning records with Shapely geometries and parsed `geo` metadata."""
    table = pq.read_table(path)
    raw_meta = table.schema.metadata or {}
    if b"geo" not in raw_meta:
        raise ValueError(f"Parquet file {path} is missing mandatory GeoParquet 'geo' metadata")
    geo_meta = json.loads(raw_meta[b"geo"].decode("utf-8"))
    primary_col = geo_meta["primary_column"]

    py_rows = table.to_pylist()
    for row in py_rows:
        row[primary_col] = wkb.loads(row[primary_col])
    return py_rows, geo_meta


def geom_to_geojson_and_bbox(geom: BaseGeometry) -> tuple[dict[str, Any], list[float]]:
    """Return GeoJSON mapping and 4-element bbox for STAC Item serialization."""
    minx, miny, maxx, maxy = geom.bounds
    return dict(mapping(geom)), [round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)]
