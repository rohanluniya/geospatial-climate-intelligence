-- Trino 484 Analytical SQL over Apache Iceberg V2 Geospatial & Climate Lakehouse
-- Catalog: iceberg (backed by Lakekeeper REST Catalog in docker-compose analytics profile)

-- 1. Regional Multi-CRS Zonal Climate Rollup with Explicit Valid-Pixel Coverage Weighting
SELECT
    z.region_id,
    COUNT(DISTINCT z.basin_id) AS basin_count,
    COUNT(DISTINCT z.scene_id) AS scene_count,
    ROUND(SUM(z.ndvi_mean * z.valid_pixels) / NULLIF(SUM(z.valid_pixels), 0), 4) AS pixel_weighted_ndvi,
    ROUND(AVG(z.ndwi_mean), 4) AS avg_ndwi,
    ROUND(AVG(z.flood_extent_pct), 2) AS avg_flood_extent_pct,
    ROUND(AVG(z.lst_c_mean), 2) AS avg_lst_c,
    ROUND(AVG(z.valid_pixel_pct), 2) AS avg_valid_pixel_pct
FROM iceberg.gold.zonal_climate_stats z
WHERE z.valid_pixel_pct >= 30.0
GROUP BY z.region_id
ORDER BY avg_lst_c DESC;

-- 2. Deseasonalized Climate Anomaly Audit vs Naive Seasonal Confounding False Alarms
SELECT
    region_id,
    basin_id,
    acquisition_date,
    observed_lst_c,
    expected_seasonal_lst_c,
    naive_raw_delta_lst_c,
    deseasonalized_z_lst,
    deseasonalized_z_ndvi,
    deseasonalized_z_ndwi,
    anomaly_type,
    severity,
    naive_seasonal_confounding_fp
FROM iceberg.gold.temporal_anomalies
WHERE anomaly_type != 'NORMAL'
   OR naive_seasonal_confounding_fp = TRUE
ORDER BY GREATEST(ABS(deseasonalized_z_lst), ABS(deseasonalized_z_ndvi), ABS(deseasonalized_z_ndwi)) DESC;

-- 3. Quarantine DLQ Root-Cause Summary & SLA Audit
SELECT
    source_type,
    reason_code,
    COUNT(*) AS quarantined_records,
    MIN(quarantined_at_utc) AS first_seen_utc,
    MAX(quarantined_at_utc) AS last_seen_utc
FROM iceberg.bronze.quarantine_dlq
GROUP BY source_type, reason_code
ORDER BY quarantined_records DESC;

-- 4. Iceberg V2 Snapshot History & Time-Travel Audit Query
SELECT
    committed_at,
    snapshot_id,
    parent_id,
    operation
FROM iceberg.gold."zonal_climate_stats$snapshots"
ORDER BY committed_at DESC
LIMIT 10;
