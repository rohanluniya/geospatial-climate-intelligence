# Engineering Case Study — Designing a Scientific Geospatial & Climate Lakehouse

## Problem Statement

Earth observation and climate analytics fail in predictable, subtle ways when treated as generic tabular ETL:
1. **Silent CRS & Antimeridian Corruption:** Joining UTM Zone 10N (`EPSG:32610`), Polar Stereographic (`EPSG:3413`), and Pacific antimeridian (`179°E -> -179°W`) datasets without strict projection normalization and 180° polygon splitting either drops Pacific basins or stretches geometries globally across `[-180, 180]`.
2. **Cloud-Top Thermal Contamination:** Averaging raw Land Surface Temperature (LST) bands over cloud-covered tropical scenes blends `-31 °C` cloud tops with `+27 °C` canopy surfaces, creating phantom cooling trends.
3. **Seasonal Confounding:** Thresholding raw temperature deltas against an annual mean flags normal California winters (`-17 °C` below annual mean) and Svalbard winters (`-15 °C`) as severe anomalies.
4. **Stale Map Tiles After Reprocessing:** Serving map tiles behind URL-only caches returns stale `v1` imagery even after radiometric recalibration (`v2`) lands in the lakehouse.

## Solution Architecture & Key Technical Decisions

1. **Checkpointed Provenance Before Compute:** Every incoming GeoTIFF passes SHA-256 verification and GDAL band validation in `ingestion/stac_catalog.py` before Bronze Iceberg V2 registration. Corrupt files route atomically to `bronze.quarantine_dlq` without blocking healthy scenes.
2. **Continuous vs. Categorical Reprojection:** `processing/crs_and_geometry.py` reprojects spectral and thermal bands with **bilinear** interpolation while forcing **nearest-neighbor** resampling on the categorical QA bitmask band so cloud/nodata flags are never blurred.
3. **Zero-Seam Antimeridian Handling:** Published STAC Items and GeoParquet v1.1.0 polygons strictly enforce `[-180, 180]` MultiPolygons via `split_antimeridian_bbox()`, while raster masking temporarily unwraps negative Pacific longitudes (`lon + 360`) onto continuous `[179, 181]` raster grids so zero pixels are lost on the 180th meridian.
4. **Deseasonalized Robust MAD Z-Scores:** Subtracting hemisphere-aware seasonal harmonics and scaling by `1.4826 * MAD` (with physical sigma floors) detected **14** true heatwave, vegetation-stress, and flood-surge events while suppressing **9** naive seasonal false positives.
5. **Snapshot-Pinned Reproducible Tile Serving:** Tile cache keys bind `(region_id, layer, z, x, y, stac_snapshot, scene_version)`, cutting tile p50 latency from `37.60 ms` to `0.179 ms` (disk) and `0.0038 ms` (memory) while guaranteeing instant invalidation on `POST /v1/backfill`.

## Measured Outcomes

- **End-to-End Reliability:** `32/32` tests PASS, `8/8` DQ gates PASS, `8/8` automated chaos drills PASS.
- **Mosaic Recovery:** Heavy tropical cloud coverage (`18.73%` valid pixels on a single Amazon pass) recovered to `100.0%` valid coverage in the multi-date Dask median composite with zero cloud-top thermal leakage.
- **Lakehouse Optimization:** Apache Iceberg V2 compaction reduced `gold.zonal_climate_stats` scan p50 latency from `29.42 ms` (`25` files) to `3.15 ms` (`1` file) — a **9.33×** speedup.
