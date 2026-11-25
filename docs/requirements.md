# Functional, Scientific & Non-Functional Requirements

## Domain Objectives

1. **Multi-CRS Raster & Vector Harmonization:** Fuse heterogeneous satellite GeoTIFF/COG scenes (`EPSG:32610` UTM, `EPSG:4326` WGS84, `EPSG:3413` Polar Stereographic North) and vector hydrological basins (GeoParquet v1.1.0) onto a canonical coordinate reference system without antimeridian seam corruption or polar distortion.
2. **Immutable Provenance & Asset Catalog:** Register every verified satellite scene in a STAC Catalog with SHA-256 checksums, acquisition timestamps, source CRS (`proj:epsg`), cloud-cover fractions (`eo:cloud_cover`), and explicit license metadata (`CC-BY-4.0`). Corrupt assets must quarantine automatically to `bronze.quarantine_dlq`.
3. **Cloud/Nodata Isolation & Compositing:** Exclude cloud, cloud-shadow, and `-9999` nodata pixels before spectral index or thermal calculations, track explicit valid-pixel percentages, and produce cloud-free multi-date median mosaics via xarray and Dask.
4. **Scientific Indices & Deseasonalized Anomalies:** Compute NDVI, NDWI/MNDWI flood proxies, and Land Surface Temperature (LST °C) zonal statistics with p10/p50/p90 percentiles, 95% confidence half-widths, quality tiers, and explicit scientific caveats. Detect temporal climate anomalies via deseasonalized z-scores that explicitly avoid seasonal-cycle confounding.
5. **Versioned Lakehouse & Reproducible Tile Serving:** Persist Bronze, Silver, Gold, and Quality tables in Apache Iceberg V2 with snapshot history, time-travel queries, compaction, and idempotent backfills. Serve reproducible RGBA PNG map tiles with ETag caching, version-based invalidation, and provenance HTTP headers.

## Service-Level Objectives (SLOs)

| Capability | Target SLO (Local Profile) | Enforcement Mechanism |
| --- | --- | --- |
| STAC Checksum Gate | 100% corrupt scenes quarantined before Silver | `ingestion/stac_catalog.py` + `bronze.quarantine_dlq` |
| CRS Projection Round-Trip Accuracy | `< 0.05 m` error across UTM & Polar CRSs | Hypothesis property tests (`tests/unit/test_properties.py`) |
| Antimeridian Polygon Bounds | 100% vertices strictly inside `[-180, 180] × [-90, 90]` | `split_antimeridian_bbox` + DQ gate |
| NDVI Physical Bounds | 100% valid pixels strictly within `[-1.0, 1.0]` | Masked calculation + Hypothesis + DQ gate |
| Cloud-Free Mosaic Recovery | Mosaic valid pixel % ≥ worst single scene (target `>= 95%`) | Multi-date Dask median composite |
| Warm Map Tile Serve Latency | p50 `< 5 ms` (memory cache HIT) | `ReproducibleTileServer` + `tests/performance/` |
| RBAC Enforcement | 100% unauthenticated `/v1/*` rejected (`401`); non-admin backfill rejected (`403`) | FastAPI RBAC dependency + `security_audit.jsonl` |

## Consistency & Replay Guarantees

- **Bronze Ingestion:** At-least-once delivery is converted to idempotent catalog state via deterministic `(scene_id, sha256)` deduplication for STAC assets and `(observation_id, ingest_time_utc, event_type)` deduplication for weather stations.
- **Event Time vs Processing Time:** Weather observations separate `event_time_utc` from `ingest_time_utc`. Late-arriving `CORRECTION` records supersede earlier `MEASUREMENT` rows for the same `(station_id, event_date)`, while `RETRACTION` rows remove faulty measurements from active Gold fusion.
- **Atomic Snapshot Boundaries:** Each Bronze/Silver/Gold/Quality table write commits an atomic Apache Iceberg V2 snapshot; downstream queries and PNG tile provenance headers pin to concrete snapshot IDs.
