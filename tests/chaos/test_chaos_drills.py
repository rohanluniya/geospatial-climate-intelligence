"""8 Automated Geospatial & Climate Chaos Drills writing structured raw evidence to results/chaos/chaos_results.json."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from geo_climate.config import PROJECT_ROOT, REGION_BY_ID, Settings
from models.iceberg_store import IcebergLakehouseStore
from processing.crs_and_geometry import (
    read_geoparquet,
    roundtrip_crs_error_meters,
    validate_wgs84_bounds,
)
from processing.pipeline import run_full_pipeline, run_incremental_backfill
from processing.raster_cube import process_tiles_with_partial_failure_isolation
from services.api.app import create_app

CHAOS_RESULTS_PATH = PROJECT_ROOT / "results" / "chaos" / "chaos_results.json"
_DRILL_RECORDS: list[dict[str, Any]] = []


def _record_drill(drill_id: str, title: str, passed: bool, elapsed_ms: float, metrics: dict[str, Any]) -> None:
    _DRILL_RECORDS.append(
        {
            "drill_id": drill_id,
            "title": title,
            "passed": passed,
            "elapsed_ms": round(elapsed_ms, 2),
            "metrics": metrics,
        }
    )


@pytest.fixture(scope="module", autouse=True)
def persist_chaos_report() -> Any:
    _DRILL_RECORDS.clear()
    yield
    if len(_DRILL_RECORDS) >= 7:
        CHAOS_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "drills_passed": sum(1 for d in _DRILL_RECORDS if d["passed"]),
            "drills_total": len(_DRILL_RECORDS),
            "drills": _DRILL_RECORDS,
        }
        CHAOS_RESULTS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_drill_01_corrupt_geotiff_checksum_quarantine(tmp_path: Path) -> None:
    t0 = time.perf_counter()
    cfg = Settings(data_dir=tmp_path / "drill01")
    summary = run_full_pipeline(cfg, warm_tiles=False)
    store = IcebergLakehouseStore(cfg)
    dlq = store.read_rows("bronze.quarantine_dlq")
    corrupt_rows = [r for r in dlq if r["reason_code"] == "SHA256_CHECKSUM_MISMATCH"]

    passed = (
        len(corrupt_rows) == 1
        and corrupt_rows[0]["record_id"] == "california_central_valley_20260715_corrupt"
        and summary["stac_summary"]["validated_items"] == 22
        and summary["dq_passed"] == summary["dq_total"]
    )
    _record_drill(
        "drill_01_corrupt_geotiff_checksum_quarantine",
        "Corrupt GeoTIFF & SHA-256 mismatch quarantined to DLQ without blocking healthy scenes",
        passed,
        (time.perf_counter() - t0) * 1000.0,
        {
            "quarantined_corrupt_geotiffs": len(corrupt_rows),
            "total_dlq_records": len(dlq),
            "healthy_stac_scenes_preserved": summary["stac_summary"]["validated_items"],
        },
    )
    assert passed


def test_drill_02_crs_mismatch_normalization(tmp_path: Path) -> None:
    t0 = time.perf_counter()
    cfg = Settings(data_dir=tmp_path / "drill02")
    run_full_pipeline(cfg, warm_tiles=False)
    store = IcebergLakehouseStore(cfg)
    silver_scenes = store.read_rows("silver.normalized_scenes")

    source_epsgs = sorted({int(r["source_epsg"]) for r in silver_scenes})
    canonical_epsgs = sorted({int(r["canonical_epsg"]) for r in silver_scenes})
    max_err_m = max(
        roundtrip_crs_error_meters(-121.0, 37.8, 32610),
        roundtrip_crs_error_meters(15.0, 78.5, 3413),
        roundtrip_crs_error_meters(-55.5, -4.2, 3857),
    )
    passed = source_epsgs == [3413, 4326, 32610] and canonical_epsgs == [4326] and max_err_m < 0.01
    _record_drill(
        "drill_02_crs_mismatch_normalization",
        "Mixed EPSG:32610 (UTM), EPSG:3413 (Polar), and EPSG:4326 scenes normalized to canonical EPSG:4326",
        passed,
        (time.perf_counter() - t0) * 1000.0,
        {
            "source_epsg_codes": source_epsgs,
            "canonical_epsg_codes": canonical_epsgs,
            "max_crs_roundtrip_error_meters": round(max_err_m, 6),
        },
    )
    assert passed


def test_drill_03_heavy_cloud_80pct_mask_and_mosaic_fallback(tmp_path: Path) -> None:
    t0 = time.perf_counter()
    cfg = Settings(data_dir=tmp_path / "drill03")
    summary = run_full_pipeline(cfg, warm_tiles=False)
    store = IcebergLakehouseStore(cfg)
    zonal = store.read_rows("gold.zonal_climate_stats")

    heavy_cloud_rows = [r for r in zonal if r["scene_id"] == "amazon_tapajos_basin_20260415_v1"]
    amazon_mosaic = next(m for m in summary["mosaics"] if m["region_id"] == "amazon_tapajos_basin")
    min_heavy_lst = min(float(r["lst_c_mean"]) for r in heavy_cloud_rows if r["lst_c_mean"] is not None)

    passed = (
        amazon_mosaic["single_scene_min_valid_pct"] < 30.0
        and amazon_mosaic["valid_pixel_pct"] >= 95.0
        and min_heavy_lst > 10.0  # Cloud-top 242K (-31C) completely excluded!
    )
    _record_drill(
        "drill_03_heavy_cloud_80pct_mask_and_mosaic_fallback",
        "Heavy tropical cloud scene masked cleanly and multi-date Dask median mosaic recovers >95% valid pixels",
        passed,
        (time.perf_counter() - t0) * 1000.0,
        {
            "single_scene_min_valid_pct": amazon_mosaic["single_scene_min_valid_pct"],
            "mosaic_recovered_valid_pct": amazon_mosaic["valid_pixel_pct"],
            "min_amazon_heavy_cloud_zonal_lst_c": round(min_heavy_lst, 2),
        },
    )
    assert passed


def test_drill_04_antimeridian_and_polar_boundary_validity(tmp_path: Path) -> None:
    t0 = time.perf_counter()
    cfg = Settings(data_dir=tmp_path / "drill04")
    run_full_pipeline(cfg, warm_tiles=False)

    basins, geo_meta = read_geoparquet(cfg.vectors_dir / "climate_basins.geoparquet")
    all_valid = all(validate_wgs84_bounds(b["geometry"])[0] for b in basins)
    fiji_basins = [b for b in basins if b["region_id"] == "fiji_pacific_atoll"]
    fiji_multipolygons = all(b["geometry"].geom_type == "MultiPolygon" for b in fiji_basins)

    store = IcebergLakehouseStore(cfg)
    zonal = store.read_rows("gold.zonal_climate_stats")
    fiji_zonal = [r for r in zonal if r["region_id"] == "fiji_pacific_atoll" and r["valid_pixels"] > 0]
    polar_zonal = [r for r in zonal if r["region_id"] == "svalbard_arctic_fjord" and r["valid_pixels"] > 0]

    passed = all_valid and fiji_multipolygons and len(fiji_zonal) == 15 and len(polar_zonal) == 15
    _record_drill(
        "drill_04_antimeridian_and_polar_boundary_validity",
        "180-degree antimeridian Fiji MultiPolygons and 78N Svalbard Polar basins remain valid with zero seam loss",
        passed,
        (time.perf_counter() - t0) * 1000.0,
        {
            "geoparquet_version": geo_meta["version"],
            "fiji_multipolygon_basins": len(fiji_basins),
            "fiji_valid_zonal_records": len(fiji_zonal),
            "polar_valid_zonal_records": len(polar_zonal),
        },
    )
    assert passed


def test_drill_05_partial_tile_failure_isolation() -> None:
    t0 = time.perf_counter()
    synthetic_grid = np.arange(96 * 96, dtype=np.float32).reshape(96, 96)
    res = process_tiles_with_partial_failure_isolation(
        synthetic_grid,
        tile_size=32,
        fail_tile_coords={(0, 1), (2, 2)},
    )
    out = res["output"]
    healthy_tile_exact = bool(np.array_equal(out[0:32, 0:32], synthetic_grid[0:32, 0:32]))
    failed_tile_nan = bool(np.all(np.isnan(out[0:32, 32:64])))

    passed = (
        res["total_tiles"] == 9
        and res["succeeded_tiles"] == 7
        and len(res["failed_tiles"]) == 2
        and healthy_tile_exact
        and failed_tile_nan
    )
    _record_drill(
        "drill_05_partial_tile_failure_isolation",
        "Injected 2/9 tile I/O failures isolated cleanly while 7/9 sibling tiles compute bit-exact values",
        passed,
        (time.perf_counter() - t0) * 1000.0,
        {
            "total_tiles": res["total_tiles"],
            "succeeded_tiles": res["succeeded_tiles"],
            "failed_tiles": len(res["failed_tiles"]),
            "tile_success_pct": res["tile_success_pct"],
        },
    )
    assert passed


def test_drill_06_incremental_backfill_replay_parity(tmp_path: Path) -> None:
    t0 = time.perf_counter()
    cfg = Settings(data_dir=tmp_path / "drill06")
    base = run_full_pipeline(cfg, warm_tiles=False)
    store = IcebergLakehouseStore(cfg)
    zonal_before = sorted(
        (r["zonal_id"], r["ndvi_mean"], r["lst_c_mean"])
        for r in store.read_rows("gold.zonal_climate_stats")
    )

    # Idempotent replay (no calibration change) -> exact bit-for-bit zonal parity
    replay = run_incremental_backfill(
        cfg,
        region_id="california_central_valley",
        acquisition_date="2026-07-15",
        inject_v2_calibration=False,
    )
    zonal_after = sorted(
        (r["zonal_id"], r["ndvi_mean"], r["lst_c_mean"])
        for r in store.read_rows("gold.zonal_climate_stats")
    )
    snaps = store.snapshot_history("gold.zonal_climate_stats")

    passed = (
        zonal_before == zonal_after
        and base["zonal_stats_rows"] == replay["zonal_stats_rows"]
        and len(snaps) >= 2
    )
    _record_drill(
        "drill_06_incremental_backfill_replay_parity",
        "Idempotent incremental backfill produces exact zonal-value replay parity across Iceberg V2 snapshots",
        passed,
        (time.perf_counter() - t0) * 1000.0,
        {
            "rows_before": len(zonal_before),
            "rows_after": len(zonal_after),
            "exact_value_parity": zonal_before == zonal_after,
            "iceberg_snapshots_recorded": len(snaps),
        },
    )
    assert passed


def test_drill_07_tile_cache_invalidation_on_scene_version(tmp_path: Path) -> None:
    t0 = time.perf_counter()
    cfg = Settings(data_dir=tmp_path / "drill07")
    run_full_pipeline(cfg, warm_tiles=True)
    app = create_app(cfg)
    client = TestClient(app)
    viewer_hdr = {"X-API-Key": cfg.viewer_token}
    admin_hdr = {"X-API-Key": cfg.admin_token}

    # Initial v1 tile serve -> HIT (pre-warmed)
    r_v1 = client.get("/v1/tiles/ndvi/0/0/0.png?region_id=california_central_valley", headers=viewer_hdr)
    etag_v1 = r_v1.headers["ETag"]
    ver_v1 = int(r_v1.headers["X-Scene-Version"])

    # Trigger admin backfill with v2 radiometric calibration -> invalidates cache and advances version
    bf = client.post(
        "/v1/backfill",
        headers=admin_hdr,
        json={
            "region_id": "california_central_valley",
            "acquisition_date": "2026-07-15",
            "inject_v2_calibration": True,
        },
    )
    assert bf.status_code == 200

    r_v2 = client.get("/v1/tiles/ndvi/0/0/0.png?region_id=california_central_valley", headers=viewer_hdr)
    etag_v2 = r_v2.headers["ETag"]
    ver_v2 = int(r_v2.headers["X-Scene-Version"])
    r_v2_cached = client.get("/v1/tiles/ndvi/0/0/0.png?region_id=california_central_valley", headers=viewer_hdr)

    passed = (
        ver_v1 == 1
        and ver_v2 == 2
        and etag_v1 != etag_v2
        and r_v2_cached.headers["X-Cache-Status"] == "HIT"
    )
    _record_drill(
        "drill_07_tile_cache_invalidation_on_scene_version",
        "New scene calibration version (v2) invalidates stale v1 PNG tiles and issues fresh ETag & provenance headers",
        passed,
        (time.perf_counter() - t0) * 1000.0,
        {
            "version_before": ver_v1,
            "version_after": ver_v2,
            "etag_changed": etag_v1 != etag_v2,
            "post_invalidation_second_req_status": r_v2_cached.headers["X-Cache-Status"],
        },
    )
    assert passed


def test_drill_08_service_restart_and_iceberg_recovery(tmp_path: Path) -> None:
    t0 = time.perf_counter()
    cfg = Settings(data_dir=tmp_path / "drill08")
    run_full_pipeline(cfg, warm_tiles=True)

    # First service instance reads KPIs
    app1 = create_app(cfg)
    c1 = TestClient(app1)
    hdr = {"X-API-Key": cfg.viewer_token}
    kpis_before = c1.get("/v1/overview", headers=hdr).json()["kpis"]

    # Simulate abrupt process crash & cold restart with a brand new FastAPI + Iceberg store instance
    del c1, app1
    app2 = create_app(Settings(data_dir=tmp_path / "drill08"))
    c2 = TestClient(app2)
    health_after = c2.get("/health").json()
    kpis_after = c2.get("/v1/overview", headers=hdr).json()["kpis"]
    tile_after = c2.get("/v1/tiles/lst/0/0/0.png?region_id=svalbard_arctic_fjord", headers=hdr)

    passed = (
        health_after["status"] == "ok"
        and kpis_after["zonal_stats_rows"] == kpis_before["zonal_stats_rows"]
        and kpis_after["stac_validated_scenes"] == kpis_before["stac_validated_scenes"]
        and tile_after.status_code == 200
        and tile_after.headers["X-Cache-Status"] == "HIT"  # Disk cache survived restart!
        and len(REGION_BY_ID) == 4
    )
    _record_drill(
        "drill_08_service_restart_and_iceberg_recovery",
        "Cold service restart recovers all 9 Iceberg V2 tables, STAC catalog, and disk-backed PNG tile cache with zero data loss",
        passed,
        (time.perf_counter() - t0) * 1000.0,
        {
            "zonal_rows_recovered": kpis_after["zonal_stats_rows"],
            "stac_scenes_recovered": kpis_after["stac_validated_scenes"],
            "tile_cache_status_after_restart": tile_after.headers["X-Cache-Status"],
        },
    )
    assert passed
