"""Unit tests for scientific spectral indices, QA masking, weather reconciliation, and anomaly detection."""

from __future__ import annotations

import numpy as np
import pytest

from ingestion.generator import NODATA_VALUE
from processing.climate_analytics import (
    detect_deseasonalized_anomalies,
    reconcile_weather_observations,
)
from processing.raster_cube import apply_qa_and_nodata_mask, compute_indices_from_masked


def test_spectral_indices_on_known_reflectance_fixture() -> None:
    # 2x2 synthetic raster:
    # (0,0) dense vegetation: Red=0.05, Green=0.08, NIR=0.45, SWIR=0.12, LST=300K, QA=0
    # (0,1) open water:       Red=0.05, Green=0.12, NIR=0.03, SWIR=0.02, LST=295K, QA=0
    # (1,0) cloud pixel:      Red=0.90, Green=0.90, NIR=0.90, SWIR=0.70, LST=240K, QA=1 (must be masked!)
    # (1,1) nodata pixel:     all -9999, QA=3 (must be masked!)
    data = np.zeros((6, 2, 2), dtype=np.float32)
    data[:, 0, 0] = [0.05, 0.08, 0.45, 0.12, 300.15, 0.0]
    data[:, 0, 1] = [0.05, 0.12, 0.03, 0.02, 295.15, 0.0]
    data[:, 1, 0] = [0.90, 0.90, 0.90, 0.70, 240.15, 1.0]
    data[:, 1, 1] = [NODATA_VALUE, NODATA_VALUE, NODATA_VALUE, NODATA_VALUE, NODATA_VALUE, 3.0]

    masked, valid_mask, valid_pct = apply_qa_and_nodata_mask(data)
    assert valid_pct == pytest.approx(50.0, abs=0.01)
    assert bool(valid_mask[0, 0]) and bool(valid_mask[0, 1])
    assert not bool(valid_mask[1, 0]) and not bool(valid_mask[1, 1])

    idx = compute_indices_from_masked(masked)
    assert idx["ndvi"][0, 0] == pytest.approx((0.45 - 0.05) / (0.45 + 0.05), abs=1e-5)
    assert idx["ndwi"][0, 1] == pytest.approx((0.12 - 0.03) / (0.12 + 0.03), abs=1e-5)
    assert idx["lst_c"][0, 0] == pytest.approx(27.0, abs=1e-4)
    assert np.isnan(idx["ndvi"][1, 0])
    assert np.isnan(idx["lst_c"][1, 0])  # Cloud-top 240K (-33C) never leaks!
    assert np.isnan(idx["ndvi"][1, 1])


def test_weather_reconciliation_dedup_correction_retraction_and_quarantine() -> None:
    base = {
        "observation_id": "obs-001",
        "station_id": "STN-CA-01",
        "region_id": "california_central_valley",
        "basin_id": "california_central_valley__north_watershed",
        "event_time_utc": "2026-07-15T18:00:00+00:00",
        "ingest_time_utc": "2026-07-15T18:10:00+00:00",
        "latitude": 38.0,
        "longitude": -121.0,
        "air_temp_c": 34.0,
        "precip_mm": 0.0,
        "humidity_pct": 28.0,
        "event_type": "MEASUREMENT",
        "source_license": "CC-BY-4.0",
        "sensor_calibration_version": "v2.1",
        "quality_flag": "PASS",
    }
    dup = dict(base)
    correction = dict(base, ingest_time_utc="2026-07-16T09:00:00+00:00", air_temp_c=36.5, event_type="CORRECTION")
    retracted_meas = dict(
        base,
        observation_id="obs-002",
        station_id="STN-CA-02",
        air_temp_c=31.0,
    )
    retraction = dict(
        retracted_meas,
        ingest_time_utc="2026-07-16T10:00:00+00:00",
        event_type="RETRACTION",
    )
    bad_range = dict(base, observation_id="obs-bad", station_id="STN-BAD", air_temp_c=500.0)
    missing_ts = dict(base, observation_id="obs-nots", station_id="STN-BAD2", event_time_utc="")

    resolved, quarantine, stats = reconcile_weather_observations(
        [base, dup, correction, retracted_meas, retraction, bad_range, missing_ts]
    )
    assert len(resolved) == 1
    assert resolved[0]["air_temp_c"] == 36.5
    assert resolved[0]["event_type"] == "CORRECTION"
    assert len(quarantine) == 2
    assert stats["duplicates_skipped"] == 1
    assert stats["corrections_applied"] == 1
    assert stats["retractions_applied"] == 1


def test_deseasonalized_zscore_blocks_seasonal_confounding_but_flags_true_heatwave() -> None:
    # Construct 4 normal seasonal dates in California + 1 summer heatwave (+10C above seasonal expectation)
    # Normal winter is ~16C and normal summer is ~41C -> naive annual mean (~29C) falsely flags winter & normal summer!
    rows = [
        {"region_id": "california_central_valley", "basin_id": "ca_b1", "scene_id": "s1", "acquisition_date": "2025-10-15", "lst_c_mean": 28.4, "ndvi_mean": 0.42, "ndwi_mean": -0.18, "valid_pixel_pct": 92.0},
        {"region_id": "california_central_valley", "basin_id": "ca_b1", "scene_id": "s2", "acquisition_date": "2026-01-15", "lst_c_mean": 15.9, "ndvi_mean": 0.20, "ndwi_mean": -0.16, "valid_pixel_pct": 88.0},
        {"region_id": "california_central_valley", "basin_id": "ca_b1", "scene_id": "s3", "acquisition_date": "2026-04-15", "lst_c_mean": 28.3, "ndvi_mean": 0.42, "ndwi_mean": -0.18, "valid_pixel_pct": 95.0},
        {"region_id": "california_central_valley", "basin_id": "ca_b1", "scene_id": "s4", "acquisition_date": "2026-07-15", "lst_c_mean": 51.0, "ndvi_mean": 0.41, "ndwi_mean": -0.22, "valid_pixel_pct": 94.0},
        {"region_id": "california_central_valley", "basin_id": "ca_b1", "scene_id": "s5", "acquisition_date": "2026-10-15", "lst_c_mean": 28.5, "ndvi_mean": 0.43, "ndwi_mean": -0.17, "valid_pixel_pct": 91.0},
    ]
    anoms = detect_deseasonalized_anomalies(rows)
    by_scene = {a["scene_id"]: a for a in anoms}

    # Winter date s2 is ~15.9C (huge negative raw delta vs annual mean) -> must be NORMAL with naive FP suppressed!
    assert by_scene["s2"]["anomaly_type"] == "NORMAL"
    assert by_scene["s2"]["naive_seasonal_confounding_fp"] is True

    # Heatwave date s4 is +10C above summer baseline -> must flag HEAT_ANOMALY & VEGETATION_STRESS!
    assert "HEAT_ANOMALY" in by_scene["s4"]["anomaly_type"]
    assert by_scene["s4"]["deseasonalized_z_lst"] > 4.0
