"""End-to-end geospatial & climate intelligence pipeline orchestrator (Bronze -> Silver -> Gold -> Tiles)."""

from __future__ import annotations

import json
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from geo_climate.config import REGION_BY_ID, Settings
from geo_climate.logging_utils import get_logger, new_trace_id, traced_span
from geo_climate.metrics import METRICS
from ingestion.generator import (
    SceneManifestEntry,
    generate_satellite_scenes,
    generate_scene_recalibration_v2,
    generate_vector_boundaries,
    generate_weather_observations,
)
from ingestion.stac_catalog import build_and_validate_stac_catalog
from models.iceberg_store import IcebergLakehouseStore
from processing.climate_analytics import (
    compute_zonal_statistics,
    detect_deseasonalized_anomalies,
    reconcile_weather_observations,
)
from processing.crs_and_geometry import (
    NormalizedRaster,
    read_geoparquet,
    reproject_raster_to_canonical,
)
from processing.dq_engine import run_data_quality_suite
from processing.raster_cube import (
    apply_qa_and_nodata_mask,
    build_spatiotemporal_cube,
    compute_cloud_free_mosaic,
)
from processing.tile_renderer import VALID_LAYERS, ReproducibleTileServer

logger = get_logger("geo_climate.pipeline")


def run_full_pipeline(
    settings: Settings,
    *,
    extra_scenes: list[SceneManifestEntry] | None = None,
    warm_tiles: bool = True,
    compact_tables: bool = False,
) -> dict[str, Any]:
    """Execute the deterministic Bronze -> Silver -> Gold -> DQ -> Tile pipeline end to end."""
    t_pipeline_start = time.perf_counter()
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    trace_id = new_trace_id()
    settings.ensure_dirs()
    store = IcebergLakehouseStore(settings)

    stage_timings_ms: dict[str, float] = {}

    with traced_span("pipeline.full_run", settings.traces_path, trace_id, {"run_id": run_id}):
        # 1. Source Generation (Real GeoTIFFs, GeoParquet boundaries, Weather JSONL)
        t0 = time.perf_counter()
        with traced_span("stage.generate_sources", settings.traces_path, trace_id):
            geoparquet_path, _, geo_meta = generate_vector_boundaries(settings)
            manifest = generate_satellite_scenes(settings, include_adversarial=True)
            if extra_scenes:
                manifest.extend(extra_scenes)
            _, raw_weather = generate_weather_observations(settings, include_adversarial=True)
        stage_timings_ms["generate_sources_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        METRICS.observe("geoclimate_pipeline_stage_duration_ms", stage_timings_ms["generate_sources_ms"], stage="generate")

        # 2. STAC 1.0/1.1 Catalog Build, Checksum Validation & Bronze Persistence
        t0 = time.perf_counter()
        with traced_span("stage.stac_and_bronze", settings.traces_path, trace_id):
            stac_result = build_and_validate_stac_catalog(settings, manifest)
            resolved_weather, weather_quarantine, weather_stats = reconcile_weather_observations(raw_weather)
            all_quarantine = list(stac_result.quarantined_records) + list(weather_quarantine)

            store.overwrite_rows("bronze.stac_assets", stac_result.validated_items)
            store.overwrite_rows("bronze.weather_observations", resolved_weather)
            store.overwrite_rows("bronze.quarantine_dlq", all_quarantine)
        stage_timings_ms["stac_bronze_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        METRICS.observe("geoclimate_pipeline_stage_duration_ms", stage_timings_ms["stac_bronze_ms"], stage="stac_bronze")

        # 3. Read GeoParquet Vector Boundaries & Persist Silver Boundary Metadata
        t0 = time.perf_counter()
        basins, read_geo_meta = read_geoparquet(geoparquet_path)
        vector_meta_rows = [
            {
                "basin_id": str(b["basin_id"]),
                "basin_name": str(b["basin_name"]),
                "region_id": str(b["region_id"]),
                "biome": str(b["biome"]),
                "source_epsg": int(b["source_epsg"]),
                "antimeridian_split": bool(b["antimeridian_split"]),
                "polar_region": bool(b["polar_region"]),
                "area_deg2": float(b["area_deg2"]),
                "geoparquet_uri": str(geoparquet_path),
                "geoparquet_version": str(read_geo_meta.get("version", "1.1.0")),
                "license": str(b["license"]),
            }
            for b in basins
        ]
        store.overwrite_rows("silver.vector_boundaries_meta", vector_meta_rows)

        # 4. CRS Reprojection & Normalization to Canonical EPSG:4326 Grid
        with traced_span("stage.crs_reprojection", settings.traces_path, trace_id):
            # If multiple versions exist for the same (region_id, acquisition_date), keep highest version for analytics
            # while retaining all validated versions in Bronze STAC catalog for full lineage!
            latest_by_region_date: dict[tuple[str, str], dict[str, Any]] = {}
            for item in stac_result.validated_items:
                key = (str(item["region_id"]), str(item["acquisition_date"]))
                prev = latest_by_region_date.get(key)
                if prev is None or int(item["scene_version"]) >= int(prev["scene_version"]):
                    latest_by_region_date[key] = item

            # Also ensure all validated scenes are normalized into Silver metadata
            silver_scene_rows: list[dict[str, Any]] = []
            cubes_input: dict[str, list[tuple[str, str, NormalizedRaster]]] = defaultdict(list)
            region_transforms: dict[str, Any] = {}
            region_max_version: dict[str, int] = defaultdict(lambda: 1)

            for item in stac_result.validated_items:
                region = REGION_BY_ID[str(item["region_id"])]
                t_rep = time.perf_counter()
                silver_tif = settings.silver_rasters_dir / f"{item['scene_id']}_canonical.tif"
                norm = reproject_raster_to_canonical(
                    Path(item["asset_uri"]),
                    region.wgs84_bbox,
                    settings.raster_width,
                    settings.raster_height,
                    canonical_epsg=region.canonical_epsg,
                    antimeridian=region.antimeridian_crossing,
                    dst_path=silver_tif,
                )
                rep_ms = round((time.perf_counter() - t_rep) * 1000.0, 2)
                _, _, valid_pct = apply_qa_and_nodata_mask(norm.data, norm.nodata)

                silver_scene_rows.append(
                    {
                        "scene_id": str(item["scene_id"]),
                        "region_id": region.region_id,
                        "acquisition_date": str(item["acquisition_date"]),
                        "source_epsg": int(item["source_epsg"]),
                        "canonical_epsg": region.canonical_epsg,
                        "scene_version": int(item["scene_version"]),
                        "valid_pixel_pct": valid_pct,
                        "cloud_cover_pct": float(item["cloud_cover_pct"]),
                        "nodata_pct": float(item["nodata_pct"]),
                        "reproject_ms": rep_ms,
                        "silver_tif_uri": str(silver_tif),
                        "antimeridian_unwrapped": bool(norm.antimeridian_unwrapped),
                        "processed_at_utc": datetime.now(UTC).isoformat(),
                    }
                )
                region_transforms[region.region_id] = norm.transform
                region_max_version[region.region_id] = max(
                    region_max_version[region.region_id], int(item["scene_version"])
                )
                cubes_input[region.region_id].append(
                    (str(item["scene_id"]), str(item["acquisition_date"]), norm)
                )

            store.overwrite_rows("silver.normalized_scenes", silver_scene_rows)
        stage_timings_ms["crs_reprojection_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        METRICS.observe("geoclimate_pipeline_stage_duration_ms", stage_timings_ms["crs_reprojection_ms"], stage="reproject")

        # 5. xarray + Dask Spatiotemporal Cubes, Cloud-Free Mosaics, Zonal Stats & Anomalies
        t0 = time.perf_counter()
        all_zonal_rows: list[dict[str, Any]] = []
        mosaics_meta: list[dict[str, Any]] = []
        with traced_span("stage.cube_mosaic_and_zonal", settings.traces_path, trace_id):
            for region_id, scene_list in sorted(cubes_input.items()):
                cube = build_spatiotemporal_cube(
                    region_id,
                    scene_list,
                    chunk_size=settings.dask_chunk_size,
                )
                mosaic_path = settings.mosaics_dir / f"{region_id}_mosaic.tif"
                mosaic = compute_cloud_free_mosaic(
                    cube,
                    transform=region_transforms[region_id],
                    window_label="multi_date_cloud_free_median",
                    output_tif_path=mosaic_path,
                )
                mosaics_meta.append(
                    {
                        "region_id": mosaic.region_id,
                        "window_label": mosaic.window_label,
                        "scenes_composited": len(mosaic.scene_ids),
                        "valid_pixel_pct": mosaic.valid_pixel_pct,
                        "single_scene_min_valid_pct": mosaic.single_scene_min_valid_pct,
                        "output_tif_path": str(mosaic.output_tif_path),
                    }
                )
                zonal_rows = compute_zonal_statistics(
                    cube,
                    region_transforms[region_id],
                    basins,
                    resolved_weather,
                )
                all_zonal_rows.extend(zonal_rows)

            anomaly_rows = detect_deseasonalized_anomalies(all_zonal_rows)
            store.overwrite_rows("gold.zonal_climate_stats", all_zonal_rows)
            store.overwrite_rows("gold.temporal_anomalies", anomaly_rows)
        stage_timings_ms["cube_zonal_anomaly_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
        METRICS.observe("geoclimate_pipeline_stage_duration_ms", stage_timings_ms["cube_zonal_anomaly_ms"], stage="zonal_analytics")

        # 6. Data Quality Suite & Reconciliation
        t0 = time.perf_counter()
        with traced_span("stage.dq_suite", settings.traces_path, trace_id):
            dq_rows = run_data_quality_suite(
                run_id=run_id,
                stac_items=stac_result.validated_items,
                quarantined_records=all_quarantine,
                basins=basins,
                zonal_rows=all_zonal_rows,
                anomaly_rows=anomaly_rows,
                mosaics_meta=mosaics_meta,
            )
            store.overwrite_rows("quality.dq_results", dq_rows)
        stage_timings_ms["dq_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)

        # 7. Warm Initial Reproducible PNG Map Tiles & Audit Log
        t0 = time.perf_counter()
        tile_server = ReproducibleTileServer(settings.tile_cache_dir, settings.mosaics_dir)
        stac_snap = store.latest_snapshot_id("bronze.stac_assets")
        if warm_tiles:
            with traced_span("stage.warm_tiles", settings.traces_path, trace_id):
                for region_id in sorted(cubes_input.keys()):
                    for layer in VALID_LAYERS:
                        tile_server.render_tile(
                            layer=layer,
                            region_id=region_id,
                            z=0,
                            x=0,
                            y=0,
                            stac_snapshot=stac_snap,
                            scene_version=region_max_version[region_id],
                        )
            if tile_server.audit_events:
                store.overwrite_rows("gold.tile_cache_audit", tile_server.audit_events)
        stage_timings_ms["tile_warm_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)

        compaction_results: list[dict[str, Any]] = []
        if compact_tables:
            for tbl_name in ("gold.zonal_climate_stats", "gold.temporal_anomalies"):
                compaction_results.append(store.compact_table(tbl_name))

    total_ms = round((time.perf_counter() - t_pipeline_start) * 1000.0, 2)
    summary = {
        "run_id": run_id,
        "trace_id": trace_id,
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "total_duration_ms": total_ms,
        "stage_timings_ms": stage_timings_ms,
        "stac_summary": stac_result.catalog_summary,
        "weather_stats": weather_stats,
        "geoparquet_meta": {
            "path": str(geoparquet_path),
            "version": geo_meta["version"],
            "basin_count": len(basins),
        },
        "mosaics": mosaics_meta,
        "zonal_stats_rows": len(all_zonal_rows),
        "anomaly_rows": len(anomaly_rows),
        "flagged_anomalies": sum(1 for a in anomaly_rows if a["anomaly_type"] != "NORMAL"),
        "seasonal_confounding_fp_suppressed": sum(
            1 for a in anomaly_rows if a["naive_seasonal_confounding_fp"]
        ),
        "quarantined_total": len(all_quarantine),
        "dq_passed": sum(1 for c in dq_rows if c["passed"]),
        "dq_total": len(dq_rows),
        "iceberg_tables": store.all_table_summaries(),
        "tile_cache_stats": tile_server.stats(),
        "compaction_results": compaction_results,
    }
    (settings.data_dir / "latest_run_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    logger.info(
        "Geospatial climate pipeline completed",
        extra={
            "run_id": run_id,
            "trace_id": trace_id,
            "component": "pipeline",
            "extra_fields": {
                "total_duration_ms": total_ms,
                "zonal_rows": len(all_zonal_rows),
                "dq_passed": summary["dq_passed"],
                "dq_total": summary["dq_total"],
            },
        },
    )
    return summary


def run_incremental_backfill(
    settings: Settings,
    *,
    region_id: str = "california_central_valley",
    acquisition_date: str = "2026-07-15",
    inject_v2_calibration: bool = False,
) -> dict[str, Any]:
    """Run an idempotent incremental backfill for a specific region/date and invalidate affected map tiles."""
    if region_id not in REGION_BY_ID:
        raise ValueError(f"Unknown region_id: {region_id}")

    extra: list[SceneManifestEntry] = []
    if inject_v2_calibration:
        extra.append(generate_scene_recalibration_v2(settings, region_id=region_id, date_str=acquisition_date))

    tile_server = ReproducibleTileServer(settings.tile_cache_dir, settings.mosaics_dir)
    invalidated = tile_server.invalidate_region(region_id)

    summary = run_full_pipeline(settings, extra_scenes=extra or None, warm_tiles=True)
    summary["backfill"] = {
        "region_id": region_id,
        "acquisition_date": acquisition_date,
        "inject_v2_calibration": inject_v2_calibration,
        "tiles_invalidated": invalidated,
    }
    return summary
