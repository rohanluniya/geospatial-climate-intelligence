# System Architecture — Geospatial & Climate Intelligence Engine

## Component & Failure-Domain Diagram

```mermaid
flowchart LR
    subgraph Sources["Deterministic & External Sources (Untrusted Boundary)"]
        S1["6-Band GeoTIFF Scenes\n(UTM 32610, WGS84 4326, Polar 3413,\n180° Antimeridian Fiji)"]
        S2["Ground Weather Stations\n(Avro v1/v2 Events:\nMeasurements, Corrections, Retractions)"]
        S3["Hydrological Basin Vectors\n(GeoParquet v1.1.0 WKB)"]
    end

    subgraph Bronze["Bronze Layer (Immutable Provenance + DLQ)"]
        STAC["STAC 1.1 Catalog Builder\n+ SHA-256 & GDAL Gate"]
        DLQ["Iceberg V2:\nbronze.quarantine_dlq"]
        B1["Iceberg V2:\nbronze.stac_assets"]
        B2["Iceberg V2:\nbronze.weather_observations"]
    end

    subgraph Silver["Silver Layer (Harmonized Spatial-Temporal)"]
        WARP["CRS Reprojection (rasterio/pyproj)\n+ Antimeridian Split (shapely)"]
        CUBE["xarray + Dask Cube\n(time, y, x) + QA Cloud Mask"]
        MOS["Cloud-Free Median Mosaics\n(Canonical EPSG:4326 GeoTIFF)"]
        S_ICE["Iceberg V2:\nsilver.normalized_scenes\nsilver.vector_boundaries_meta"]
    end

    subgraph Gold["Gold & Quality Layer (Analytical Lakehouse)"]
        ZONAL["Zonal Statistics + 95% CI\n& Deseasonalized Z-Scores"]
        G_ICE["Iceberg V2:\ngold.zonal_climate_stats\ngold.temporal_anomalies\ngold.tile_cache_audit"]
        DQ["DQ Gate (8 Checks)\nquality.dq_results"]
    end

    subgraph Serving["Serving & Observability (127.0.0.1:8070)"]
        DUCK["DuckDB Vectorized SQL"]
        TILE["Reproducible PNG Tile Server\n(ETag + Memory/Disk Cache)"]
        API["FastAPI + RBAC\n(/v1/*, /health, /metrics)"]
        UI["Geospatial Control Tower UI"]
        OBS["Prometheus + OTel Spans\n+ Security Audit JSONL"]
    end

    S1 --> STAC
    STAC -->|Checksum/GDAL Fail| DLQ
    STAC -->|Verified| B1
    S2 -->|Schema/Range Fail| DLQ
    S2 -->|Reconciled| B2
    B1 --> WARP
    S3 --> WARP
    WARP --> CUBE
    WARP --> S_ICE
    CUBE --> MOS
    CUBE --> ZONAL
    B2 --> ZONAL
    ZONAL --> G_ICE
    G_ICE --> DQ
    G_ICE --> DUCK --> API
    MOS --> TILE --> API
    API --> UI
    API --> OBS
```

## Core Workflow Sequence (Ingestion -> Mosaic -> Zonal Stats -> Versioned Tile Serving)

```mermaid
sequenceDiagram
    participant Gen as Multi-CRS Scene & Weather Source
    participant STAC as STAC Gate & Bronze Iceberg
    participant Proc as GDAL/xarray/Dask Processor
    participant Gold as Gold Iceberg V2 & DQ Gate
    participant Tile as PNG Tile Server & FastAPI

    Gen->>STAC: Deliver GeoTIFF + SHA-256 + Weather JSONL
    STAC->>STAC: Verify SHA-256, GDAL bands, Avro v2 schema
    alt Corrupt GeoTIFF or Invalid Weather
        STAC->>STAC: Route to bronze.quarantine_dlq with reason_code
    else Valid Asset
        STAC->>STAC: Commit pystac Catalog + bronze.stac_assets snapshot
    end
    STAC->>Proc: Read verified scenes + GeoParquet basins
    Proc->>Proc: Reproject EPSG:32610/3413 -> EPSG:4326 (nearest QA, bilinear bands)
    Proc->>Proc: Mask cloud/shadow/nodata -> build chunked xarray/Dask cube
    Proc->>Proc: Compute cloud-free median mosaics + zonal stats + deseasonalized z-scores
    Proc->>Gold: Atomic Iceberg V2 overwrite + run 8 DQ checks
    Gold->>Tile: Expose snapshot IDs & mosaic rasters
    Tile->>Tile: Render 256x256 RGBA PNG tile with ETag + X-STAC-Snapshot + X-CRS + X-Valid-Pixel-Pct
```

## Table Grain, Keys & Partitioning Model

| Iceberg V2 Table | Grain | Primary / Natural Key | Temporal Semantics | Maintenance |
| --- | --- | --- | --- | --- |
| `bronze.stac_assets` | 1 row per validated satellite scene version | `(scene_id, sha256)` | `acquisition_time_utc` vs `ingest_time_utc` | Append/dedup, snapshot audit |
| `bronze.weather_observations` | 1 active row per station & date | `(station_id, event_date)` | Latest `ingest_time_utc` wins (`CORRECTION` / `RETRACTION`) | Deduplicated active state |
| `bronze.quarantine_dlq` | 1 row per rejected source record | `(record_id, reason_code, quarantined_at_utc)` | Processing time (`quarantined_at_utc`) | Immutable audit trail |
| `silver.normalized_scenes` | 1 row per canonicalized scene | `scene_id` | Acquisition date + processing timestamp | Reproject provenance |
| `silver.vector_boundaries_meta` | 1 row per GeoParquet basin polygon | `basin_id` | Static versioned boundary metadata | Points to GeoParquet v1.1.0 |
| `gold.zonal_climate_stats` | 1 row per `(basin_id, scene_id)` | `zonal_id` | `acquisition_date` | Supports time travel & compaction |
| `gold.temporal_anomalies` | 1 row per evaluated `(basin_id, scene_id)` | `anomaly_id` | `acquisition_date` deseasonalized | Compared vs naive delta |
| `gold.tile_cache_audit` | 1 row per cold tile render event | `tile_key` | `served_at_utc` | Tracks ETag + snapshot lineage |
| `quality.dq_results` | 1 row per `(run_id, check_name)` | `dq_id` | `evaluated_at_utc` | Feeds Prometheus gauges |
