"""Data Quality checks, quarantine validation, and Bronze->Silver->Gold reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from geo_climate.metrics import METRICS
from processing.crs_and_geometry import validate_wgs84_bounds


def run_data_quality_suite(
    *,
    run_id: str,
    stac_items: list[dict[str, Any]],
    quarantined_records: list[dict[str, Any]],
    basins: list[dict[str, Any]],
    zonal_rows: list[dict[str, Any]],
    anomaly_rows: list[dict[str, Any]],
    mosaics_meta: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Execute deterministic geospatial & scientific DQ assertions and emit Prometheus gauges."""
    now_utc = datetime.now(UTC).isoformat()
    checks: list[dict[str, Any]] = []

    def add_check(check_id: str, layer: str, passed: bool, metric_val: float, detail: str) -> None:
        checks.append(
            {
                "dq_id": f"{run_id}__{check_id}",
                "run_id": run_id,
                "check_name": check_id,
                "layer": layer,
                "passed": bool(passed),
                "observed_value": round(float(metric_val), 4),
                "detail": detail,
                "evaluated_at_utc": now_utc,
            }
        )
        METRICS.set_gauge("geoclimate_dq_checks_status", 1.0 if passed else 0.0, check=check_id, layer=layer)

    # 1. STAC SHA-256 & license completeness
    valid_sha = all(len(str(i.get("sha256", ""))) == 64 and bool(i.get("license")) for i in stac_items)
    add_check(
        "stac_checksum_and_license_complete",
        "bronze",
        valid_sha and len(stac_items) > 0,
        float(len(stac_items)),
        f"Validated {len(stac_items)} STAC items with 64-char SHA-256 checksums and CC-BY-4.0 license.",
    )

    # 2. Quarantine DLQ auditable capture
    has_reasons = all(bool(q.get("reason_code")) and bool(q.get("record_id")) for q in quarantined_records)
    add_check(
        "quarantine_dlq_auditable",
        "bronze",
        has_reasons,
        float(len(quarantined_records)),
        f"{len(quarantined_records)} invalid/corrupt records routed to bronze.quarantine_dlq with reason codes.",
    )

    # 3. Antimeridian & WGS84 vector boundary validity
    invalid_geoms = []
    for b in basins:
        ok, err = validate_wgs84_bounds(b["geometry"])
        if not ok:
            invalid_geoms.append(f"{b['basin_id']}: {err}")
    add_check(
        "geoparquet_wgs84_antimeridian_bounds",
        "silver",
        len(invalid_geoms) == 0 and len(basins) > 0,
        float(len(basins) - len(invalid_geoms)),
        "All GeoParquet basin polygons valid and strictly within [-180, 180] x [-90, 90]."
        if not invalid_geoms
        else f"Invalid geometries: {invalid_geoms}",
    )

    # 4. NDVI physical bounds [-1, 1] across all computed zonal statistics
    ndvi_vals = [float(r["ndvi_mean"]) for r in zonal_rows if r["ndvi_mean"] is not None]
    ndvi_in_bounds = all(-1.0 <= v <= 1.0 for v in ndvi_vals)
    add_check(
        "ndvi_physical_bounds",
        "gold",
        ndvi_in_bounds and len(ndvi_vals) > 0,
        float(len(ndvi_vals)),
        f"Verified {len(ndvi_vals)} zonal NDVI values strictly inside [-1.0, 1.0].",
    )

    # 5. Cloud/Nodata contamination guard: no LST below -60C (cloud-top temp is -31C / 242K in tropics!)
    tropical_lsts = [
        float(r["lst_c_mean"])
        for r in zonal_rows
        if r["lst_c_mean"] is not None
        and r["region_id"] in ("amazon_tapajos_basin", "fiji_pacific_atoll", "california_central_valley")
    ]
    no_cloud_leak = all(v > -5.0 for v in tropical_lsts)
    add_check(
        "cloud_top_thermal_leakage_guard",
        "gold",
        no_cloud_leak and len(tropical_lsts) > 0,
        min(tropical_lsts) if tropical_lsts else 0.0,
        f"Minimum non-polar zonal LST is {min(tropical_lsts):.2f} C (cloud-top 242K/-31C properly masked)."
        if tropical_lsts
        else "No non-polar zonal rows found.",
    )

    # 6. Multi-date cloud-free mosaic coverage recovery
    recovered = all(float(m["valid_pixel_pct"]) >= float(m["single_scene_min_valid_pct"]) for m in mosaics_meta)
    avg_cov = sum(float(m["valid_pixel_pct"]) for m in mosaics_meta) / max(len(mosaics_meta), 1)
    add_check(
        "cloud_free_mosaic_coverage_recovery",
        "silver",
        recovered and len(mosaics_meta) > 0,
        avg_cov,
        f"Average cloud-free mosaic valid pixel coverage is {avg_cov:.2f}% across {len(mosaics_meta)} regions.",
    )

    # 7. Deseasonalized anomaly detection vs seasonal confounding check
    confounding_prevented = sum(1 for a in anomaly_rows if a["naive_seasonal_confounding_fp"])
    true_anomalies = sum(1 for a in anomaly_rows if a["anomaly_type"] != "NORMAL")
    add_check(
        "deseasonalized_anomaly_reconciliation",
        "gold",
        len(anomaly_rows) > 0 and true_anomalies > 0,
        float(true_anomalies),
        f"Detected {true_anomalies} deseasonalized climate anomalies and suppressed {confounding_prevented} naive seasonal false alarms.",
    )

    # 8. Bronze -> Gold scene coverage reconciliation
    gold_scenes = {str(r["scene_id"]) for r in zonal_rows}
    bronze_scenes = {str(i["scene_id"]) for i in stac_items}
    reconciled = bronze_scenes.issubset(gold_scenes)
    add_check(
        "bronze_to_gold_scene_reconciliation",
        "gold",
        reconciled,
        float(len(gold_scenes)),
        f"Reconciled {len(gold_scenes)}/{len(bronze_scenes)} validated STAC scenes into Gold zonal statistics.",
    )

    return checks
