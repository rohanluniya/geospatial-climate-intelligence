# ADR-003: Apache Iceberg V2 Medallion Lakehouse with DuckDB & Trino SQL

## Status
Accepted

## Context
Geospatial analytics require reproducible snapshots so that map tiles, zonal statistics, and climate anomaly reports can be audited against the exact set of satellite scenes and weather corrections present when they were published.

## Decision
1. **Table Format:** Store all 9 Bronze, Silver, Gold, and Quality tables in **Apache Iceberg V2** (`format-version=2`), managed locally via PyIceberg SQL-SQLite catalog and in distributed mode via **Lakekeeper REST Catalog + PostGIS + RustFS**.
2. **Query Engines:** Serve low-latency local analytical queries via **DuckDB** zero-copy over PyIceberg Arrow tables, and ship Trino 484 SQL (`sql/trino/climate_zonal_analytics.sql`) for multi-node federation.
3. **Maintenance:** Implement atomic snapshot time travel (`snapshot_id` reads) and Parquet small-file compaction (`compact_table`).

## Tradeoffs & Consequences
- **Benefits:** Reproducible time-travel queries, atomic overwrites preventing partial-read anomalies, and measurable scan speedups after coalescing micro-batch Parquet files.
- **Costs:** Streaming/micro-batch appends accumulate small Parquet files unless compaction is scheduled regularly.
