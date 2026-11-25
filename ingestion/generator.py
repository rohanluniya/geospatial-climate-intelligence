"""Deterministic synthetic satellite GeoTIFF, weather station, and GeoParquet vector generator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.crs import CRS as RasterioCRS
from rasterio.transform import from_bounds
from shapely.geometry import box

from geo_climate.config import DEFAULT_REGIONS, REGION_BY_ID, RegionSpec, Settings
from processing.crs_and_geometry import (
    compute_projected_bounds,
    split_antimeridian_bbox,
    write_geoparquet,
)

NODATA_VALUE = -9999.0
BAND_NAMES = ("red", "green", "nir", "swir", "lst_k", "qa_mask")

ACQUISITION_DATES: tuple[str, ...] = (
    "2025-10-15",
    "2026-01-15",
    "2026-04-15",
    "2026-04-16",
    "2026-07-15",
    "2026-07-16",
    "2026-10-15",
)


@dataclass(frozen=True)
class SceneManifestEntry:
    """Metadata emitted by the deterministic source generator prior to STAC registration."""

    scene_id: str
    region_id: str
    acquisition_date: str
    acquisition_time_utc: str
    source_epsg: int
    asset_path: str
    expected_sha256: str
    actual_sha256: str
    cloud_cover_pct: float
    nodata_pct: float
    scene_version: int
    is_corrupt: bool
    is_duplicate_delivery: bool
    antimeridian_crossing: bool
    polar_region: bool
    license: str = "CC-BY-4.0"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def deterministic_id(*parts: str) -> str:
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _seasonal_phase(date_str: str, is_southern_hemisphere: bool) -> float:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    doy = dt.timetuple().tm_yday
    # Peak growing/warm season ~ July 15 (doy ~196) in Northern Hemisphere, Jan 15 in Southern
    peak_doy = 15.0 if is_southern_hemisphere else 196.0
    return float(np.cos(2.0 * np.pi * (doy - peak_doy) / 365.25))


def _synthesize_scene_arrays(
    region: RegionSpec,
    date_str: str,
    width: int,
    height: int,
    rng: np.random.Generator,
    *,
    heavy_cloud: bool = False,
    nodata_stripes: bool = False,
    scene_version: int = 1,
) -> tuple[np.ndarray, float, float]:
    """Generate physically coherent 6-band surface reflectance, thermal LST, and QA mask."""
    yy, xx = np.meshgrid(
        np.linspace(0.0, 1.0, height, dtype=np.float32),
        np.linspace(0.0, 1.0, width, dtype=np.float32),
        indexing="ij",
    )
    center_lat = 0.5 * (region.wgs84_bbox[1] + region.wgs84_bbox[3])
    season = _seasonal_phase(date_str, is_southern_hemisphere=(center_lat < 0.0))

    # Build distinct, spatially recognizable terrain features per biome so map tiles look realistic
    if region.region_id == "california_central_valley":
        river = np.exp(-(((xx - (0.48 + 0.08 * np.sin(yy * 5.2))) / 0.045) ** 2))
        ag_grid = 0.09 * np.sin(xx * 24.0) * np.cos(yy * 24.0)
        foothills = 0.14 * np.clip((xx - 0.72) / 0.28, 0.0, 1.0)
        water_mask = river > 0.65
        ndvi_base = region.baseline_ndvi_mean + region.baseline_ndvi_amp * season + ag_grid - foothills
        lst_base = region.baseline_lst_k_mean + region.baseline_lst_k_amp * season + 4.0 * foothills - 5.5 * river
        ndwi_base = region.baseline_ndwi_mean + 0.55 * river
        # Inject real summer heatwave + drought stress on 2026-07-15 & 2026-07-16
        if date_str in ("2026-07-15", "2026-07-16"):
            hotspot = np.exp(-(((xx - 0.35) ** 2 + (yy - 0.55) ** 2) / 0.08))
            lst_base = lst_base + 7.5 + 4.5 * hotspot
            ndvi_base = ndvi_base - 0.17 - 0.08 * hotspot
    elif region.region_id == "amazon_tapajos_basin":
        river = np.exp(-(((xx - (0.35 + 0.15 * np.sin(yy * 4.0))) / 0.06) ** 2))
        clearing = np.exp(-(((xx - 0.72) ** 2 + (yy - 0.62) ** 2) / 0.035))
        canopy_tex = 0.04 * np.sin(xx * 18.0 + yy * 14.0)
        water_mask = river > 0.58
        ndvi_base = region.baseline_ndvi_mean + region.baseline_ndvi_amp * season + canopy_tex - 0.38 * clearing
        lst_base = region.baseline_lst_k_mean + region.baseline_lst_k_amp * season + 4.5 * clearing - 3.5 * river
        ndwi_base = region.baseline_ndwi_mean + 0.60 * river - 0.15 * clearing
        # Inject tropical floodplain inundation event on 2026-04-15 & 2026-04-16
        if date_str in ("2026-04-15", "2026-04-16"):
            floodplain = np.exp(-(((xx - (0.35 + 0.15 * np.sin(yy * 4.0))) / 0.16) ** 2))
            ndwi_base = ndwi_base + 0.42 * floodplain
            lst_base = lst_base - 2.2 * floodplain
            ndvi_base = ndvi_base - 0.12 * floodplain
    elif region.region_id == "fiji_pacific_atoll":
        # Two volcanic islands straddling the 180th meridian (xx=0.5 is 180° meridian!)
        island_w = np.exp(-(((xx - 0.32) ** 2 + (yy - 0.45) ** 2) / 0.032))
        island_e = np.exp(-(((xx - 0.68) ** 2 + (yy - 0.58) ** 2) / 0.028))
        land = np.clip(island_w + island_e, 0.0, 1.0)
        water_mask = land < 0.32
        ndvi_base = -0.15 + 0.82 * land + region.baseline_ndvi_amp * season * land
        lst_base = region.baseline_lst_k_mean + region.baseline_lst_k_amp * season + 2.5 * land
        ndwi_base = 0.58 - 0.72 * land
        # Marine heatwave anomaly on 2026-07-15 (cool season anomalous warming!)
        if date_str in ("2026-07-15", "2026-07-16"):
            lst_base = lst_base + 4.8
    else:  # svalbard_arctic_fjord (Polar Stereographic EPSG:3413)
        fjord = np.exp(-(((yy - (0.52 + 0.12 * np.cos(xx * 4.5))) / 0.09) ** 2))
        glacier = np.clip((yy - 0.68) / 0.30, 0.0, 1.0)
        tundra = np.clip(1.0 - fjord - glacier, 0.0, 1.0)
        water_mask = fjord > 0.55
        ndvi_base = region.baseline_ndvi_mean + region.baseline_ndvi_amp * season * tundra - 0.12 * glacier
        lst_base = region.baseline_lst_k_mean + region.baseline_lst_k_amp * season - 6.0 * glacier + 3.0 * fjord
        ndwi_base = region.baseline_ndwi_mean + 0.48 * fjord + 0.25 * glacier
        # Arctic heatwave + meltwater pulse on 2026-07-15 & 2026-07-16
        if date_str in ("2026-07-15", "2026-07-16"):
            lst_base = lst_base + 6.4
            ndwi_base = ndwi_base + 0.24 * glacier

    # Add subtle radiometric sensor noise (version 2 has improved calibration offset +0.008)
    calib_offset = 0.008 if scene_version >= 2 else 0.0
    noise = rng.normal(0.0, 0.018, size=(height, width)).astype(np.float32)

    target_ndvi = np.clip(ndvi_base + noise + calib_offset, -0.85, 0.92)
    target_ndwi = np.clip(ndwi_base + 0.5 * noise, -0.85, 0.92)

    # Invert target NDVI & NDWI into strictly positive physical surface reflectances [0.02, 0.95]
    # NDVI = (NIR - Red) / (NIR + Red) -> pick Red, solve NIR
    red = np.where(water_mask, 0.055, 0.14 - 0.08 * np.clip(target_ndvi, 0.0, 1.0)) + rng.normal(
        0.0, 0.006, size=(height, width)
    )
    red = np.clip(red, 0.02, 0.65).astype(np.float32)
    nir = red * (1.0 + target_ndvi) / np.maximum(1.0 - target_ndvi, 0.08)
    nir = np.clip(nir, 0.02, 0.95).astype(np.float32)

    # NDWI = (Green - NIR) / (Green + NIR) -> solve Green from NIR and target_ndwi
    green = nir * (1.0 + target_ndwi) / np.maximum(1.0 - target_ndwi, 0.08)
    green = np.clip(green, 0.02, 0.92).astype(np.float32)

    # SWIR responds inversely to moisture/water
    swir = np.clip(
        np.where(water_mask, 0.03, 0.22 - 0.12 * np.clip(target_ndwi, -0.5, 0.8))
        + rng.normal(0.0, 0.008, size=(height, width)),
        0.015,
        0.90,
    ).astype(np.float32)

    lst_k = np.clip(
        lst_base + rng.normal(0.0, 0.45, size=(height, width)),
        230.0,
        345.0,
    ).astype(np.float32)

    # QA Mask: 0=clear, 1=cloud, 2=cloud_shadow, 3=nodata
    qa = np.zeros((height, width), dtype=np.float32)

    # Generate realistic blob-like cloud formations
    cloud_field = (
        np.sin(xx * 6.5 + rng.uniform(0, 6.28)) * np.cos(yy * 5.8 + rng.uniform(0, 6.28))
        + 0.6 * np.sin((xx + yy) * 11.0 + rng.uniform(0, 6.28))
    )
    if heavy_cloud:
        # >72% cloud cover!
        cloud_thresh = float(np.percentile(cloud_field, 24))
    else:
        # Typical ~8-14% scattered cloud cover
        cloud_thresh = float(np.percentile(cloud_field, 89))

    cloud_mask = cloud_field > cloud_thresh
    qa[cloud_mask] = 1.0

    # Cloud shadow offset by 2 pixels
    shadow_mask = np.roll(cloud_mask, shift=(2, 2), axis=(0, 1)) & (~cloud_mask)
    qa[shadow_mask] = 2.0

    # Corrupt cloud pixels spectrally (bright visible/SWIR, cold cloud-top temperature)
    # This proves that if QA masking were skipped, NDVI & LST zonal statistics would be badly corrupted!
    red[cloud_mask] = 0.88
    green[cloud_mask] = 0.90
    nir[cloud_mask] = 0.86
    swir[cloud_mask] = 0.62
    lst_k[cloud_mask] = 242.0  # Freezing cloud-top brightness temp!

    # Nodata sensor scan-line drops
    if nodata_stripes:
        for row_idx in range(6, height, 11):
            qa[row_idx : row_idx + 2, :] = 3.0
            for arr in (red, green, nir, swir, lst_k):
                arr[row_idx : row_idx + 2, :] = NODATA_VALUE

    cloud_pct = float(np.mean((qa == 1.0) | (qa == 2.0)) * 100.0)
    nodata_pct = float(np.mean(qa == 3.0) * 100.0)
    stack = np.stack([red, green, nir, swir, lst_k, qa], axis=0).astype(np.float32)
    return stack, round(cloud_pct, 2), round(nodata_pct, 2)


def generate_vector_boundaries(settings: Settings) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    """Generate 12 hydrological/administrative basin polygons across all 4 regions as valid GeoParquet."""
    basin_records: list[dict[str, Any]] = []
    sub_names = ("north_watershed", "central_corridor", "south_delta")

    for region in DEFAULT_REGIONS:
        min_lon, min_lat, max_lon, max_lat = region.wgs84_bbox
        lat_edges = np.linspace(min_lat, max_lat, 4)

        for idx, sub in enumerate(sub_names):
            b_min_lat = float(lat_edges[idx])
            b_max_lat = float(lat_edges[idx + 1])
            # Slight interior margin so polygons sit cleanly inside scene coverage
            if region.antimeridian_crossing:
                geom = split_antimeridian_bbox(min_lon, b_min_lat, max_lon, b_max_lat)
            else:
                geom = box(min_lon, b_min_lat, max_lon, b_max_lat)

            basin_id = f"{region.region_id}__{sub}"
            basin_records.append(
                {
                    "basin_id": basin_id,
                    "basin_name": f"{region.region_id.replace('_', ' ').title()} — {sub.replace('_', ' ').title()}",
                    "region_id": region.region_id,
                    "biome": region.biome,
                    "source_epsg": region.source_epsg,
                    "antimeridian_split": region.antimeridian_crossing,
                    "polar_region": region.polar_region,
                    "area_deg2": round(float(geom.area), 6),
                    "license": "CC-BY-4.0",
                    "geometry": geom,
                }
            )

    output_path = settings.vectors_dir / "climate_basins.geoparquet"
    geo_meta = write_geoparquet(basin_records, output_path, crs_epsg=4326)
    return output_path, basin_records, geo_meta


def generate_satellite_scenes(
    settings: Settings,
    *,
    regions: tuple[RegionSpec, ...] = DEFAULT_REGIONS,
    dates: tuple[str, ...] = ACQUISITION_DATES,
    include_adversarial: bool = True,
) -> list[SceneManifestEntry]:
    """Generate real georeferenced 6-band GeoTIFF scenes in native mixed CRSs plus adversarial scenarios."""
    settings.ensure_dirs()
    manifest: list[SceneManifestEntry] = []

    for r_idx, region in enumerate(regions):
        proj_bounds = compute_projected_bounds(
            region.wgs84_bbox,
            region.source_epsg,
            antimeridian=region.antimeridian_crossing,
        )
        transform = from_bounds(
            proj_bounds[0],
            proj_bounds[1],
            proj_bounds[2],
            proj_bounds[3],
            settings.raster_width,
            settings.raster_height,
        )
        src_crs = RasterioCRS.from_epsg(region.source_epsg)

        for d_idx, date_str in enumerate(dates):
            # Only create next-day overlap scenes for regions where overlap/cloud recovery applies
            if date_str == "2026-04-16" and region.region_id != "amazon_tapajos_basin":
                continue
            if date_str == "2026-07-16" and region.region_id != "california_central_valley":
                continue

            scene_seed = settings.seed + r_idx * 1009 + d_idx * 97
            rng = np.random.default_rng(scene_seed)

            heavy_cloud = (
                include_adversarial
                and region.region_id == "amazon_tapajos_basin"
                and date_str == "2026-04-15"
            )
            nodata_stripes = (
                include_adversarial
                and date_str == "2026-01-15"
                and region.region_id in ("california_central_valley", "svalbard_arctic_fjord")
            )

            data, cloud_pct, nodata_pct = _synthesize_scene_arrays(
                region,
                date_str,
                settings.raster_width,
                settings.raster_height,
                rng,
                heavy_cloud=heavy_cloud,
                nodata_stripes=nodata_stripes,
                scene_version=1,
            )

            scene_id = f"{region.region_id}_{date_str.replace('-', '')}_v1"
            tif_path = settings.raw_scenes_dir / f"{scene_id}.tif"
            profile = {
                "driver": "GTiff",
                "height": settings.raster_height,
                "width": settings.raster_width,
                "count": len(BAND_NAMES),
                "dtype": "float32",
                "crs": src_crs,
                "transform": transform,
                "nodata": NODATA_VALUE,
                "tiled": True,
                "blockxsize": 32,
                "blockysize": 32,
                "compress": "DEFLATE",
            }
            with rasterio.open(tif_path, "w", **profile) as dst:
                dst.write(data)
                for b_idx, b_name in enumerate(BAND_NAMES, start=1):
                    dst.set_band_description(b_idx, b_name)

            digest = sha256_file(tif_path)
            acq_time = f"{date_str}T18:30:00Z"
            entry = SceneManifestEntry(
                scene_id=scene_id,
                region_id=region.region_id,
                acquisition_date=date_str,
                acquisition_time_utc=acq_time,
                source_epsg=region.source_epsg,
                asset_path=str(tif_path),
                expected_sha256=digest,
                actual_sha256=digest,
                cloud_cover_pct=cloud_pct,
                nodata_pct=nodata_pct,
                scene_version=1,
                is_corrupt=False,
                is_duplicate_delivery=False,
                antimeridian_crossing=region.antimeridian_crossing,
                polar_region=region.polar_region,
            )
            manifest.append(entry)

    if include_adversarial:
        # Adversarial 1: Corrupt GeoTIFF / SHA-256 mismatch (must quarantine to bronze.quarantine_dlq!)
        corrupt_path = settings.raw_scenes_dir / "california_central_valley_20260715_corrupt.tif"
        corrupt_path.write_bytes(b"NOT_A_VALID_GEOTIFF_CORRUPT_PAYLOAD_0000")
        actual_corrupt_sha = sha256_file(corrupt_path)
        manifest.append(
            SceneManifestEntry(
                scene_id="california_central_valley_20260715_corrupt",
                region_id="california_central_valley",
                acquisition_date="2026-07-15",
                acquisition_time_utc="2026-07-15T18:35:00Z",
                source_epsg=32610,
                asset_path=str(corrupt_path),
                expected_sha256="0" * 64,  # Intentional checksum mismatch
                actual_sha256=actual_corrupt_sha,
                cloud_cover_pct=0.0,
                nodata_pct=100.0,
                scene_version=1,
                is_corrupt=True,
                is_duplicate_delivery=False,
                antimeridian_crossing=False,
                polar_region=False,
            )
        )

        # Adversarial 2: Duplicate redelivery of an existing valid scene (must deduplicate idempotently!)
        first_valid = manifest[0]
        manifest.append(
            SceneManifestEntry(
                scene_id=first_valid.scene_id,
                region_id=first_valid.region_id,
                acquisition_date=first_valid.acquisition_date,
                acquisition_time_utc=first_valid.acquisition_time_utc,
                source_epsg=first_valid.source_epsg,
                asset_path=first_valid.asset_path,
                expected_sha256=first_valid.expected_sha256,
                actual_sha256=first_valid.actual_sha256,
                cloud_cover_pct=first_valid.cloud_cover_pct,
                nodata_pct=first_valid.nodata_pct,
                scene_version=first_valid.scene_version,
                is_corrupt=False,
                is_duplicate_delivery=True,
                antimeridian_crossing=first_valid.antimeridian_crossing,
                polar_region=first_valid.polar_region,
            )
        )

    manifest_path = settings.raw_scenes_dir / "scene_manifest.json"
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump([entry.__dict__ for entry in manifest], fh, indent=2)
    return manifest


def generate_scene_recalibration_v2(
    settings: Settings,
    region_id: str = "california_central_valley",
    date_str: str = "2026-07-15",
) -> SceneManifestEntry:
    """Generate a radiometric v2 correction scene for backfill & cache-invalidation testing."""
    region = REGION_BY_ID[region_id]
    proj_bounds = compute_projected_bounds(
        region.wgs84_bbox,
        region.source_epsg,
        antimeridian=region.antimeridian_crossing,
    )
    transform = from_bounds(
        proj_bounds[0],
        proj_bounds[1],
        proj_bounds[2],
        proj_bounds[3],
        settings.raster_width,
        settings.raster_height,
    )
    rng = np.random.default_rng(settings.seed + 99991)
    data, cloud_pct, nodata_pct = _synthesize_scene_arrays(
        region,
        date_str,
        settings.raster_width,
        settings.raster_height,
        rng,
        heavy_cloud=False,
        nodata_stripes=False,
        scene_version=2,
    )
    scene_id = f"{region_id}_{date_str.replace('-', '')}_v2"
    tif_path = settings.raw_scenes_dir / f"{scene_id}.tif"
    profile = {
        "driver": "GTiff",
        "height": settings.raster_height,
        "width": settings.raster_width,
        "count": len(BAND_NAMES),
        "dtype": "float32",
        "crs": RasterioCRS.from_epsg(region.source_epsg),
        "transform": transform,
        "nodata": NODATA_VALUE,
        "tiled": True,
        "blockxsize": 32,
        "blockysize": 32,
        "compress": "DEFLATE",
    }
    with rasterio.open(tif_path, "w", **profile) as dst:
        dst.write(data)
        for b_idx, b_name in enumerate(BAND_NAMES, start=1):
            dst.set_band_description(b_idx, b_name)

    digest = sha256_file(tif_path)
    return SceneManifestEntry(
        scene_id=scene_id,
        region_id=region_id,
        acquisition_date=date_str,
        acquisition_time_utc=f"{date_str}T18:30:00Z",
        source_epsg=region.source_epsg,
        asset_path=str(tif_path),
        expected_sha256=digest,
        actual_sha256=digest,
        cloud_cover_pct=cloud_pct,
        nodata_pct=nodata_pct,
        scene_version=2,
        is_corrupt=False,
        is_duplicate_delivery=False,
        antimeridian_crossing=region.antimeridian_crossing,
        polar_region=region.polar_region,
    )


def generate_weather_observations(
    settings: Settings,
    *,
    include_adversarial: bool = True,
) -> tuple[Path, list[dict[str, Any]]]:
    """Generate seeded synthetic ground weather observations with duplicates, late corrections, retractions, and bad records."""
    settings.ensure_dirs()
    rng = np.random.default_rng(settings.seed + 4242)
    records: list[dict[str, Any]] = []
    sub_names = ("north_watershed", "central_corridor", "south_delta")

    for region in DEFAULT_REGIONS:
        min_lon, min_lat, max_lon, max_lat = region.wgs84_bbox
        center_lat = 0.5 * (min_lat + max_lat)
        for idx, sub in enumerate(sub_names):
            station_id = f"STN-{region.region_id[:4].upper()}-{idx + 1:02d}"
            basin_id = f"{region.region_id}__{sub}"
            lat = round(min_lat + (idx + 0.5) * (max_lat - min_lat) / 3.0, 4)
            lon = 179.4 if region.antimeridian_crossing and idx == 0 else (
                -179.4 if region.antimeridian_crossing else round(0.5 * (min_lon + max_lon), 4)
            )
            for date_str in ("2025-10-15", "2026-01-15", "2026-04-15", "2026-07-15", "2026-10-15"):
                season = _seasonal_phase(date_str, is_southern_hemisphere=(center_lat < 0.0))
                air_temp_c = (
                    (region.baseline_lst_k_mean - 273.15)
                    + region.baseline_lst_k_amp * 0.85 * season
                    + float(rng.normal(0.0, 1.2))
                )
                if date_str == "2026-07-15" and region.region_id in (
                    "california_central_valley",
                    "svalbard_arctic_fjord",
                ):
                    air_temp_c += 6.2
                precip_mm = max(0.0, region.baseline_precip_mm + float(rng.normal(0.0, 4.0)))
                if date_str == "2026-04-15" and region.region_id == "amazon_tapajos_basin":
                    precip_mm += 55.0
                humidity = float(np.clip(58.0 + (22.0 if region.biome.startswith("tropical") else -8.0) + rng.normal(0, 6), 10.0, 100.0))

                event_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(hours=18)
                ingest_dt = event_dt + timedelta(minutes=12)
                obs_id = deterministic_id(station_id, date_str, "MEASUREMENT")
                records.append(
                    {
                        "observation_id": obs_id,
                        "station_id": station_id,
                        "region_id": region.region_id,
                        "basin_id": basin_id,
                        "event_time_utc": event_dt.isoformat(),
                        "ingest_time_utc": ingest_dt.isoformat(),
                        "latitude": lat,
                        "longitude": lon,
                        "air_temp_c": round(air_temp_c, 2),
                        "precip_mm": round(precip_mm, 2),
                        "humidity_pct": round(humidity, 1),
                        "event_type": "MEASUREMENT",
                        "source_license": "CC-BY-4.0",
                        "sensor_calibration_version": "v2.1",
                        "quality_flag": "PASS",
                    }
                )

    if include_adversarial:
        # 1. Exact duplicate event (must deduplicate)
        records.append(dict(records[0]))

        # 2. Late-arriving correction for a California station (+1.5C calibration correction arriving 3 days later)
        target = dict(records[3])
        target["ingest_time_utc"] = "2026-07-18T19:00:00+00:00"
        target["air_temp_c"] = round(float(target["air_temp_c"]) + 1.5, 2)
        target["event_type"] = "CORRECTION"
        target["quality_flag"] = "CORRECTED"
        records.append(target)

        # 3. Retraction event for a faulty reading
        retract = dict(records[6])
        retract["ingest_time_utc"] = "2026-01-16T08:00:00+00:00"
        retract["event_type"] = "RETRACTION"
        retract["quality_flag"] = "RETRACTED"
        records.append(retract)

        # 4. Missing timestamp -> invalid record quarantined to DLQ
        records.append(
            {
                "observation_id": deterministic_id("STN-BAD-01", "missing_ts"),
                "station_id": "STN-BAD-01",
                "region_id": "california_central_valley",
                "basin_id": "california_central_valley__north_watershed",
                "event_time_utc": "",  # Missing timestamp!
                "ingest_time_utc": "2026-07-15T19:10:00+00:00",
                "latitude": 37.5,
                "longitude": -120.5,
                "air_temp_c": 31.2,
                "precip_mm": 0.0,
                "humidity_pct": 25.0,
                "event_type": "MEASUREMENT",
                "source_license": "CC-BY-4.0",
                "sensor_calibration_version": "v2.1",
                "quality_flag": "BAD_TIMESTAMP",
            }
        )

        # 5. Physically impossible temperature (+999.0 C) -> quarantined to DLQ
        records.append(
            {
                "observation_id": deterministic_id("STN-BAD-02", "2026-07-15"),
                "station_id": "STN-BAD-02",
                "region_id": "amazon_tapajos_basin",
                "basin_id": "amazon_tapajos_basin__central_corridor",
                "event_time_utc": "2026-07-15T18:00:00+00:00",
                "ingest_time_utc": "2026-07-15T19:12:00+00:00",
                "latitude": -4.2,
                "longitude": -55.8,
                "air_temp_c": 999.0,  # Physically impossible
                "precip_mm": -15.0,  # Negative precipitation
                "humidity_pct": 140.0,
                "event_type": "MEASUREMENT",
                "source_license": "CC-BY-4.0",
                "sensor_calibration_version": "v2.1",
                "quality_flag": "OUT_OF_RANGE",
            }
        )

    out_path = settings.raw_weather_dir / "weather_observations.jsonl"
    with out_path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return out_path, records
