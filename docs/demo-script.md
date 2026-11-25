# Live Demo Script (5-Minute Walkthrough)

## 1. Start the Portable Engine (Zero Docker Required)

```bash
./scripts/run_portable.sh
```

Wait for `[run_portable] Starting FastAPI Spatial & Tile API + Control Tower at http://127.0.0.1:8070`.

## 2. Inspect Live Control Tower UI

Open `http://127.0.0.1:8070/` in a browser:
1. **Top KPI Strip:** Verify `4` regions across 3 CRSs (`EPSG:32610`, `4326`, `3413`, `180°`), `22` validated STAC scenes, `66` zonal records, `14` deseasonalized anomalies, `9` naive seasonal false positives suppressed, `3` quarantine DLQ records, and `8/8` DQ PASS.
2. **Live RGBA PNG Map Tile Viewer:** Click through `California Central Valley`, `Amazon Tapajos Basin`, `Fiji & Pacific Antimeridian Reefs`, and `Svalbard Arctic Fjord` to view real colormapped NDVI, LST, Flood/NDWI, and QA tiles served with `ETag`, `X-STAC-Snapshot`, `X-CRS`, `X-Valid-Pixel-Pct`, and `X-Cache-Status: HIT` badges.
3. **Deseasonalized Anomaly Panel:** Compare true `HEAT_ANOMALY+VEGETATION_STRESS` rows against `NORMAL (FP Blocked)` winter rows where naive raw delta exceeds `-15 °C` yet deseasonalized z-score stays near `0.0`.
4. **STAC Browser, DQ & Quarantine DLQ:** Inspect `SHA256_CHECKSUM_MISMATCH` (corrupt GeoTIFF), `MISSING_EVENT_TIMESTAMP`, and `PHYSICAL_RANGE_VIOLATION` isolated cleanly in `bronze.quarantine_dlq`.

## 3. Demonstrate RBAC & Admin Backfill Cache Invalidation via CLI

```bash
# Anonymous call -> 401 Unauthorized
curl -i http://127.0.0.1:8070/v1/overview | head -n 5

# Viewer attempting mutation -> 403 Forbidden
curl -i -X POST -H "X-API-Key: dev-viewer-token-geo08" -H "Content-Type: application/json" \
  -d '{"region_id":"california_central_valley"}' http://127.0.0.1:8070/v1/backfill | head -n 5

# Admin triggers v2 radiometric calibration backfill -> invalidates v1 tiles & advances version
curl -s -X POST -H "Authorization: Bearer dev-admin-token-geo08" -H "Content-Type: application/json" \
  -d '{"region_id":"california_central_valley","acquisition_date":"2026-07-15","inject_v2_calibration":true}' \
  http://127.0.0.1:8070/v1/backfill | python3 -m json.tool

# Verify updated X-Scene-Version: 2 and fresh ETag on tile response
curl -s -D - -o /dev/null -H "X-API-Key: dev-viewer-token-geo08" \
  "http://127.0.0.1:8070/v1/tiles/ndvi/0/0/0.png?region_id=california_central_valley" | grep -iE "^(etag|x-scene-version|x-cache-status)"
```

## 4. Run Automated Chaos Drills & Benchmarks

```bash
make test chaos benchmark
```
