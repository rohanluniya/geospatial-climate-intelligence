# Incident Report INC-001: Corrupt GeoTIFF Injection & Heavy Tropical Cloud Obscuration

## Incident Summary

During controlled fault-injection drills (`tests/chaos/test_chaos_drills.py`), two concurrent data-quality faults were introduced into the satellite ingestion feed:
1. A corrupted GeoTIFF payload (`california_central_valley_20260715_corrupt`) with mismatched SHA-256 digest and unreadable TIFF header.
2. Severe tropical cloud cover on `amazon_tapajos_basin_20260415_v1` leaving only `18.73%` valid clear-sky pixels, with freezing cloud-top brightness temperatures (`242 K / -31.15 °C`) in masked pixels.

## Detection & Containment

- **STAC Checksum Gate (`ingestion/stac_catalog.py`):** Rejected the corrupted scene before Bronze commit and routed an auditable record (`reason_code=SHA256_CHECKSUM_MISMATCH`) to `bronze.quarantine_dlq`, incrementing `geoclimate_quarantined_records_total`. All 22 healthy STAC scenes proceeded uninterrupted.
- **QA Cloud Mask & Mosaic Recovery (`processing/raster_cube.py`):** Masked cloud and cloud-shadow pixels to `NaN` prior to NDVI/LST computation. Minimum zonal LST in the affected Amazon scene remained physically realistic (above `+10 °C`, confirming zero `-31 °C` cloud-top thermal leakage).
- **Multi-Date Dask Median Mosaic:** Combining overlapping acquisition passes recovered valid-pixel coverage in `amazon_tapajos_basin` from `18.73%` (worst single scene) to `100.0%` in the composite mosaic.

## Recovery & Verification Evidence

- Executed via `pytest tests/chaos/test_chaos_drills.py -v` (`drill_01_corrupt_geotiff_checksum_quarantine` and `drill_03_heavy_cloud_80pct_mask_and_mosaic_fallback`).
- Raw drill metrics persisted to `results/chaos/chaos_results.json`.
- All `8/8` automated DQ checks remained `PASS`.

## Follow-Up Actions

1. Alert operators via Prometheus rule `QuarantineDLQSpike` when quarantined records exceed `10` per 15-minute window.
2. Trigger an admin backfill (`POST /v1/backfill`) with `inject_v2_calibration=true` once an upstream data provider publishes a replacement scene.
