"""Zonal climate statistics, ground weather fusion, and deseasonalized z-score anomaly detection."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

import numpy as np
import xarray as xr
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from shapely.geometry import mapping

from geo_climate.config import REGION_BY_ID
from geo_climate.contracts import validate_avro_record
from geo_climate.metrics import METRICS
from processing.crs_and_geometry import geometry_to_unwrapped_pacific


def reconcile_weather_observations(
    raw_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Validate weather observations against Avro v2 + physical bounds, deduplicate, apply corrections & retractions."""
    quarantined: list[dict[str, Any]] = []
    valid_events: list[dict[str, Any]] = []
    seen_exact: set[tuple[str, str, str]] = set()
    duplicates_skipped = 0
    now_utc = datetime.now(UTC).isoformat()

    for rec in raw_records:
        ok, err = validate_avro_record(rec, "weather_observation_v2.avsc")
        if not ok:
            quarantined.append(
                {
                    "record_id": str(rec.get("observation_id", "unknown")),
                    "source_type": "weather_observation",
                    "region_id": str(rec.get("region_id", "unknown")),
                    "reason_code": "AVRO_SCHEMA_VIOLATION",
                    "detail": str(err),
                    "quarantined_at_utc": now_utc,
                    "payload_json": str(rec),
                }
            )
            METRICS.inc("geoclimate_quarantined_records_total", source_type="weather", reason="AVRO_SCHEMA_VIOLATION")
            continue

        event_ts = str(rec.get("event_time_utc", "")).strip()
        if not event_ts:
            quarantined.append(
                {
                    "record_id": str(rec["observation_id"]),
                    "source_type": "weather_observation",
                    "region_id": str(rec["region_id"]),
                    "reason_code": "MISSING_EVENT_TIMESTAMP",
                    "detail": "event_time_utc is empty",
                    "quarantined_at_utc": now_utc,
                    "payload_json": str(rec),
                }
            )
            METRICS.inc("geoclimate_quarantined_records_total", source_type="weather", reason="MISSING_EVENT_TIMESTAMP")
            continue

        try:
            datetime.fromisoformat(event_ts.replace("Z", "+00:00"))
        except ValueError:
            quarantined.append(
                {
                    "record_id": str(rec["observation_id"]),
                    "source_type": "weather_observation",
                    "region_id": str(rec["region_id"]),
                    "reason_code": "INVALID_TIMESTAMP_FORMAT",
                    "detail": f"Cannot parse ISO timestamp: {event_ts}",
                    "quarantined_at_utc": now_utc,
                    "payload_json": str(rec),
                }
            )
            continue

        air_temp = rec.get("air_temp_c")
        precip = rec.get("precip_mm")
        humidity = rec.get("humidity_pct")
        if (
            (air_temp is not None and not (-90.0 <= float(air_temp) <= 65.0))
            or (precip is not None and not (0.0 <= float(precip) <= 1000.0))
            or (humidity is not None and not (0.0 <= float(humidity) <= 100.0))
        ):
            quarantined.append(
                {
                    "record_id": str(rec["observation_id"]),
                    "source_type": "weather_observation",
                    "region_id": str(rec["region_id"]),
                    "reason_code": "PHYSICAL_RANGE_VIOLATION",
                    "detail": f"air_temp_c={air_temp}, precip_mm={precip}, humidity_pct={humidity}",
                    "quarantined_at_utc": now_utc,
                    "payload_json": str(rec),
                }
            )
            METRICS.inc("geoclimate_quarantined_records_total", source_type="weather", reason="PHYSICAL_RANGE_VIOLATION")
            continue

        dedup_key = (str(rec["observation_id"]), str(rec["ingest_time_utc"]), str(rec["event_type"]))
        if dedup_key in seen_exact:
            duplicates_skipped += 1
            continue
        seen_exact.add(dedup_key)
        valid_events.append(rec)

    # Resolve state per (station_id, event_date) ordered by ingest_time_utc:
    # CORRECTION supersedes earlier MEASUREMENT; RETRACTION removes the record from active Gold fusion.
    by_entity: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for ev in valid_events:
        ev_date = str(ev["event_time_utc"])[:10]
        by_entity[(str(ev["station_id"]), ev_date)].append(ev)

    resolved: list[dict[str, Any]] = []
    corrections_applied = 0
    retractions_applied = 0

    for _key, events in sorted(by_entity.items()):
        ordered = sorted(events, key=lambda x: str(x["ingest_time_utc"]))
        latest = ordered[-1]
        if latest["event_type"] == "RETRACTION":
            retractions_applied += 1
            continue
        if latest["event_type"] == "CORRECTION":
            corrections_applied += 1
        out_row = dict(latest)
        out_row["event_date"] = str(latest["event_time_utc"])[:10]
        resolved.append(out_row)

    stats = {
        "raw_count": len(raw_records),
        "resolved_active_count": len(resolved),
        "quarantined_count": len(quarantined),
        "duplicates_skipped": duplicates_skipped,
        "corrections_applied": corrections_applied,
        "retractions_applied": retractions_applied,
    }
    return resolved, quarantined, stats


def compute_zonal_statistics(
    cube: xr.Dataset,
    transform: Affine,
    basins: list[dict[str, Any]],
    weather_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute cloud/nodata-free zonal NDVI, NDWI/flood proxy, and LST statistics per basin and date."""
    region_id = str(cube.attrs["region_id"])
    antimeridian_unwrapped = bool(cube.attrs.get("antimeridian_unwrapped", 0))
    region_basins = [b for b in basins if b["region_id"] == region_id]

    height = int(cube.sizes["y"])
    width = int(cube.sizes["x"])

    # Index resolved weather observations by (basin_id, event_date)
    weather_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for w in weather_rows:
        if w["region_id"] == region_id:
            weather_lookup[(str(w["basin_id"]), str(w["event_date"]))] = w

    # Precompute rasterized boolean masks per basin
    basin_masks: dict[str, np.ndarray] = {}
    for b in region_basins:
        geom = b["geometry"]
        if antimeridian_unwrapped:
            geom = geometry_to_unwrapped_pacific(geom)
        inside_mask = geometry_mask(
            [mapping(geom)],
            out_shape=(height, width),
            transform=transform,
            invert=True,
            all_touched=True,
        )
        basin_masks[str(b["basin_id"])] = inside_mask

    # Materialize cube arrays once via Dask
    ndvi_arr = cube["ndvi"].compute().values
    ndwi_arr = cube["ndwi"].compute().values
    lst_arr = cube["lst_c"].compute().values
    valid_arr = cube["valid_mask"].compute().values > 0
    times = [str(t) for t in cube["time"].values.tolist()]
    scene_ids = [str(s) for s in cube["scene_id"].values.tolist()]

    results: list[dict[str, Any]] = []
    for t_idx, acq_date in enumerate(times):
        scene_id = scene_ids[t_idx]
        for b in region_basins:
            basin_id = str(b["basin_id"])
            mask = basin_masks[basin_id]
            total_px = int(np.sum(mask))
            if total_px == 0:
                continue
            valid_in_basin = mask & valid_arr[t_idx]
            valid_px = int(np.sum(valid_in_basin))
            valid_pct = round((valid_px / total_px) * 100.0, 2)

            if valid_px > 0:
                ndvi_vals = ndvi_arr[t_idx][valid_in_basin]
                ndwi_vals = ndwi_arr[t_idx][valid_in_basin]
                lst_vals = lst_arr[t_idx][valid_in_basin]
                # Double-check finite guard
                finite = np.isfinite(ndvi_vals) & np.isfinite(ndwi_vals) & np.isfinite(lst_vals)
                ndvi_vals = ndvi_vals[finite]
                ndwi_vals = ndwi_vals[finite]
                lst_vals = lst_vals[finite]
                valid_px = int(ndvi_vals.size)
                valid_pct = round((valid_px / total_px) * 100.0, 2)

            if valid_px == 0:
                ndvi_mean = ndvi_p10 = ndvi_p50 = ndvi_p90 = ndvi_std = ndvi_ci95 = None
                ndwi_mean = flood_pct = lst_mean = lst_p90 = lst_ci95 = None
                quality_tier = "NO_VALID_PIXELS"
                caveat = (
                    "100% cloud/nodata obscuration in basin; zonal metrics omitted to prevent "
                    "cloud-top brightness contamination. Use multi-date mosaic fallback."
                )
            else:
                ndvi_mean = round(float(np.mean(ndvi_vals)), 4)
                ndvi_p10 = round(float(np.percentile(ndvi_vals, 10)), 4)
                ndvi_p50 = round(float(np.percentile(ndvi_vals, 50)), 4)
                ndvi_p90 = round(float(np.percentile(ndvi_vals, 90)), 4)
                ndvi_std = round(float(np.std(ndvi_vals)), 4)
                ndvi_ci95 = round(float(1.96 * ndvi_std / max(np.sqrt(valid_px), 1.0)), 4)

                ndwi_mean = round(float(np.mean(ndwi_vals)), 4)
                flood_pct = round(float(np.mean(ndwi_vals > 0.25) * 100.0), 2)

                lst_mean = round(float(np.mean(lst_vals)), 2)
                lst_p90 = round(float(np.percentile(lst_vals, 90)), 2)
                lst_std = float(np.std(lst_vals))
                lst_ci95 = round(float(1.96 * lst_std / max(np.sqrt(valid_px), 1.0)), 3)

                if valid_pct >= 70.0:
                    quality_tier = "HIGH"
                    caveat = (
                        "Clear-sky optical/thermal proxy (surface skin LST differs from 2m air temp "
                        "and synthetic reflectance omits atmospheric aerosol scattering)."
                    )
                elif valid_pct >= 30.0:
                    quality_tier = "DEGRADED"
                    caveat = (
                        f"Partial cloud/nodata masking ({100.0 - valid_pct:.1f}% masked); "
                        "sampling bias possible over obscured sub-catchments."
                    )
                else:
                    quality_tier = "LOW_CONFIDENCE"
                    caveat = (
                        f"Severe cloud cover ({100.0 - valid_pct:.1f}% masked, n={valid_px} valid px); "
                        "treat zonal estimates as provisional and rely on multi-date cloud-free mosaic."
                    )

            w_obs = weather_lookup.get((basin_id, acq_date))
            results.append(
                {
                    "zonal_id": f"{basin_id}__{scene_id}",
                    "region_id": region_id,
                    "basin_id": basin_id,
                    "basin_name": str(b["basin_name"]),
                    "scene_id": scene_id,
                    "acquisition_date": acq_date,
                    "total_pixels": total_px,
                    "valid_pixels": valid_px,
                    "valid_pixel_pct": valid_pct,
                    "ndvi_mean": ndvi_mean,
                    "ndvi_p10": ndvi_p10,
                    "ndvi_p50": ndvi_p50,
                    "ndvi_p90": ndvi_p90,
                    "ndvi_std": ndvi_std,
                    "ndvi_ci95_halfwidth": ndvi_ci95,
                    "ndwi_mean": ndwi_mean,
                    "flood_extent_pct": flood_pct,
                    "lst_c_mean": lst_mean,
                    "lst_c_p90": lst_p90,
                    "lst_ci95_halfwidth": lst_ci95,
                    "station_air_temp_c": w_obs["air_temp_c"] if w_obs else None,
                    "station_precip_mm": w_obs["precip_mm"] if w_obs else None,
                    "quality_tier": quality_tier,
                    "scientific_caveat": caveat,
                }
            )
    return results


def _expected_seasonal_value(
    date_str: str,
    center_lat: float,
    mean_val: float,
    amp_val: float,
) -> float:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    doy = dt.timetuple().tm_yday
    peak_doy = 15.0 if center_lat < 0.0 else 196.0
    phase = float(np.cos(2.0 * np.pi * (doy - peak_doy) / 365.25))
    return mean_val + amp_val * phase


def detect_deseasonalized_anomalies(
    zonal_rows: list[dict[str, Any]],
    *,
    z_threshold: float = 2.0,
) -> list[dict[str, Any]]:
    """Compute deseasonalized z-score anomalies vs naive raw-delta baseline to eliminate seasonal confounding.

    For each basin:
      1. Compute the basin's climatological seasonal harmonic expectation from non-extreme baseline dates
         (using regional seasonal amplitude anchored to the basin's observed seasonal curve).
      2. Compare against a naive annual-mean delta (`obs - annual_mean`), explicitly surfacing cases where
         naive delta flags normal summer/winter swings as false alarms (seasonal confounding!).
    """
    by_basin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in zonal_rows:
        if row["lst_c_mean"] is not None and row["ndvi_mean"] is not None and row["valid_pixel_pct"] >= 15.0:
            by_basin[str(row["basin_id"])].append(row)

    anomalies: list[dict[str, Any]] = []
    for basin_id, rows in sorted(by_basin.items()):
        region = REGION_BY_ID[str(rows[0]["region_id"])]
        center_lat = 0.5 * (region.wgs84_bbox[1] + region.wgs84_bbox[3])

        # Robust basin offset relative to regional climatology using median residual across all dates
        lst_seasonal_prior = [
            _expected_seasonal_value(
                r["acquisition_date"],
                center_lat,
                region.baseline_lst_k_mean - 273.15,
                region.baseline_lst_k_amp,
            )
            for r in rows
        ]
        ndvi_seasonal_prior = [
            _expected_seasonal_value(
                r["acquisition_date"],
                center_lat,
                region.baseline_ndvi_mean,
                region.baseline_ndvi_amp,
            )
            for r in rows
        ]
        lst_raw_res = np.array([float(r["lst_c_mean"]) - p for r, p in zip(rows, lst_seasonal_prior, strict=True)])
        ndvi_raw_res = np.array([float(r["ndvi_mean"]) - p for r, p in zip(rows, ndvi_seasonal_prior, strict=True)])
        ndwi_vals = np.array([float(r["ndwi_mean"]) for r in rows])

        # Median-centered robust baseline & MAD scale (prevents extreme heatwave from inflating baseline std!)
        lst_offset = float(np.median(lst_raw_res))
        ndvi_offset = float(np.median(ndvi_raw_res))
        ndwi_med = float(np.median(ndwi_vals))

        lst_residuals = lst_raw_res - lst_offset
        ndvi_residuals = ndvi_raw_res - ndvi_offset
        ndwi_residuals = ndwi_vals - ndwi_med

        lst_mad = float(np.median(np.abs(lst_residuals))) * 1.4826
        ndvi_mad = float(np.median(np.abs(ndvi_residuals))) * 1.4826
        ndwi_mad = float(np.median(np.abs(ndwi_residuals))) * 1.4826

        # Physical floor on standard deviation to avoid division by near-zero noise
        lst_sigma = max(lst_mad, 1.35)
        ndvi_sigma = max(ndvi_mad, 0.045)
        ndwi_sigma = max(ndwi_mad, 0.055)

        annual_lst_mean = float(np.mean([float(r["lst_c_mean"]) for r in rows]))

        for idx, row in enumerate(rows):
            expected_lst = round(lst_seasonal_prior[idx] + lst_offset, 2)
            expected_ndvi = round(ndvi_seasonal_prior[idx] + ndvi_offset, 4)

            lst_resid = float(lst_residuals[idx])
            ndvi_resid = float(ndvi_residuals[idx])
            ndwi_resid = float(ndwi_residuals[idx])

            z_lst = round(lst_resid / lst_sigma, 3)
            z_ndvi = round(ndvi_resid / ndvi_sigma, 3)
            z_ndwi = round(ndwi_resid / ndwi_sigma, 3)

            naive_raw_delta_lst = round(float(row["lst_c_mean"]) - annual_lst_mean, 2)
            # Naive detector falsely alarms whenever |raw_delta| > 5C (normal winter/summer swing!)
            naive_false_alarm = abs(naive_raw_delta_lst) >= 5.0 and abs(z_lst) < z_threshold

            flags: list[str] = []
            if z_lst >= z_threshold:
                flags.append("HEAT_ANOMALY")
            elif z_lst <= -z_threshold:
                flags.append("COLD_ANOMALY")
            if z_ndvi <= -z_threshold:
                flags.append("VEGETATION_STRESS")
            if z_ndwi >= z_threshold:
                flags.append("FLOOD_SURGE")

            anomaly_type = "+".join(flags) if flags else "NORMAL"
            severity = "NONE"
            max_z = max(abs(z_lst), abs(z_ndvi), abs(z_ndwi))
            if flags:
                severity = "HIGH" if max_z >= 3.0 else "MODERATE"

            anomalies.append(
                {
                    "anomaly_id": f"{basin_id}__{row['scene_id']}",
                    "region_id": str(row["region_id"]),
                    "basin_id": basin_id,
                    "scene_id": str(row["scene_id"]),
                    "acquisition_date": str(row["acquisition_date"]),
                    "observed_lst_c": float(row["lst_c_mean"]),
                    "expected_seasonal_lst_c": expected_lst,
                    "naive_raw_delta_lst_c": naive_raw_delta_lst,
                    "deseasonalized_z_lst": z_lst,
                    "observed_ndvi": float(row["ndvi_mean"]),
                    "expected_seasonal_ndvi": expected_ndvi,
                    "deseasonalized_z_ndvi": z_ndvi,
                    "observed_ndwi": float(row["ndwi_mean"]),
                    "deseasonalized_z_ndwi": z_ndwi,
                    "anomaly_type": anomaly_type,
                    "severity": severity,
                    "naive_seasonal_confounding_fp": bool(naive_false_alarm),
                    "valid_pixel_pct": float(row["valid_pixel_pct"]),
                    "uncertainty_note": (
                        f"Deseasonalized via robust MAD harmonic baseline (sigma_lst={lst_sigma:.2f}C); "
                        f"valid_pixel_pct={row['valid_pixel_pct']}%"
                    ),
                }
            )
    return anomalies
