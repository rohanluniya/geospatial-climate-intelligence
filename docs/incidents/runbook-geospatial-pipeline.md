# Operator Runbook — Geospatial & Climate Intelligence Pipeline

## 1. Alert: `GeospatialDataQualityCheckFailed`
1. Inspect failing checks via API:
   ```bash
   curl -s -H "X-API-Key: $GEO_CLIMATE_VIEWER_TOKEN" http://127.0.0.1:8070/v1/dq | jq '.checks[] | select(.passed == false)'
   ```
2. Inspect quarantined records in `bronze.quarantine_dlq`:
   ```bash
   curl -s -H "X-API-Key: $GEO_CLIMATE_VIEWER_TOKEN" http://127.0.0.1:8070/v1/dq | jq '.quarantine_records'
   ```
3. If caused by a corrupt upstream scene or calibration defect, execute a targeted backfill once the clean asset is available:
   ```bash
   curl -X POST -H "Authorization: Bearer $GEO_CLIMATE_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"region_id":"california_central_valley","acquisition_date":"2026-07-15","inject_v2_calibration":true}' \
     http://127.0.0.1:8070/v1/backfill
   ```

## 2. Alert: `TileRenderLatencyHigh`
1. Check tile cache hit rate and cache entries:
   ```bash
   curl -s -H "X-API-Key: $GEO_CLIMATE_VIEWER_TOKEN" http://127.0.0.1:8070/v1/tiles/stats | jq .
   ```
2. Verify whether a recent backfill triggered cache invalidation (expected transient MISS burst).
3. Pre-warm zoom-0 tiles across all regions by running `geo-climate run` or iterating `/v1/tiles/{layer}/0/0/0.png`.

## 3. Rebuilding Derived Serving State from Durable Sources
All Silver rasters, Gold Iceberg V2 tables, DuckDB query views, and cached PNG map tiles are deterministically rebuildable from raw Bronze STAC assets, GeoParquet boundaries, and weather observations:
```bash
geo-climate run --compact
```

## 4. Iceberg V2 Small-File Compaction & Time-Travel Rollback
- Inspect snapshots: `GET /v1/snapshots?table=gold.zonal_climate_stats`
- Compact fragmented Parquet files: `geo-climate run --compact`
- Audit prior snapshot rows in Python: `store.read_rows("gold.zonal_climate_stats", snapshot_id=<prior_id>)`
