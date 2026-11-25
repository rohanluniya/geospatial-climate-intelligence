# Component Compatibility & Verified Stack Matrix

## Verified Local Runtime (macOS ARM64 & Linux x86_64/ARM64)

Every geospatial, scientific, and lakehouse library below was installed and executed locally via native ARM64 wheels on Python 3.12.8 without requiring Docker, a host JVM, or paid cloud services.

| Component | Pinned Version | Role | License | Host Verification |
| --- | --- | --- | --- | --- |
| Python | `3.12.8` | Core runtime | PSF-2.0 | Verified native ARM64 |
| `rasterio` (bundled GDAL `3.12.2`) | `1.5.2` | Multi-band GeoTIFF / COG I/O & CRS reprojection | BSD-3-Clause | Verified read/write/warp |
| `shapely` | `2.2.0` | Vector polygon geometry & 180° antimeridian splitting | BSD-3-Clause | Verified valid MultiPolygons |
| `pyproj` | `3.8.0` | CRS transforms (`EPSG:4326`, `32610`, `3857`, `3413`) | MIT | Verified `<0.01 m` round-trip |
| `xarray` | `2026.9.0` | Spatiotemporal `(time, y, x)` climate cube | Apache-2.0 | Verified lazy Dataset |
| `dask` | `2026.8.0` | Chunked out-of-core raster mosaic & index compute | BSD-3-Clause | Verified chunk ablation |
| `pystac` | `1.16.0` | STAC `1.1.0` Catalog, Collections, and Items | Apache-2.0 | Verified self-contained export |
| `pyiceberg` (`sql-sqlite`, `pyiceberg-core`) | `0.12.0` | Apache Iceberg V2 lakehouse, snapshots, compaction | Apache-2.0 | Verified 9 V2 tables |
| `duckdb` | `2.0.0` | Vectorized SQL analytics over Iceberg Arrow views | MIT | Verified SQL rollups |
| `pyarrow` | `25.0.1` | Zero-copy Arrow buffers & GeoParquet v1.1.0 writer | Apache-2.0 | Verified `geo` metadata |
| `fastavro` | `1.13.1` | Avro v1/v2 event contract & evolution validation | MIT | Verified v1->v2 evolution |
| `fastapi` / `uvicorn` | `0.163.2` / `0.54.0` | Spatial, STAC, Zonal, and PNG Map Tile API | MIT / BSD-3-Clause | Verified live on `:8070` |
| `pillow` / `matplotlib` | `13.0.0` / `3.11.2` | RGBA PNG tile rendering & benchmark visualization | MIT-CMU / PSF | Verified real 256×256 PNGs |

## Distributed Container Stack (`docker-compose.yml`)

All container tags in `docker-compose.yml` are explicitly pinned to verified releases. On hosts without Docker or a local JVM, the zero-Docker portable profile (`scripts/run_portable.sh`) executes the complete geospatial, STAC, GeoParquet, xarray/Dask, Iceberg V2, and FastAPI tile-serving stack natively.

| Service | Pinned Image Tag | Profile(s) | Architecture Notes |
| --- | --- | --- | --- |
| `geo-api` | `geospatial-climate-intelligence:0.1.0` (`python:3.12.8-slim-bookworm`) | `core`, `analytics`, `observability`, `full` | Native `linux/arm64` & `linux/amd64` |
| `rustfs` | `rustfs/rustfs:1.0.6` | `core`, `analytics`, `full` | S3-compatible object store (Apache-2.0 alternative to MinIO) |
| `postgis` | `imresamu/postgis:17-3.6.1-alpine3.23` | `core`, `analytics`, `full` | Native `arm64`/`amd64` PostGIS 3.6.1 |
| `lakekeeper` | `quay.io/lakekeeper/catalog:v0.15.1` | `core`, `analytics`, `full` | Apache Iceberg REST Catalog backed by PostgreSQL |
| `spark-master` | `apache/spark:4.1.4-scala2.13-java17-python3-ubuntu` | `analytics`, `full` | Distributed Spark SQL & PySpark engine |
| `sedona` | `apache/sedona:1.9.1` | `analytics`, `full` | Distributed spatial joins & GeoParquet |
| `trino` | `trinodb/trino:484` | `analytics`, `full` | Federated SQL over Iceberg V2 |
| `airflow` | `apache/airflow:slim-3.4.0-python3.12` | `analytics`, `full` | Scheduled DAG orchestration |
| `prometheus` | `prom/prometheus:v3.16.0` | `observability`, `full` | Metrics scraping & alert evaluation |
| `grafana` | `grafana/grafana:13.0.14` | `observability`, `full` | Operational SLO dashboards |
| `loki` | `grafana/loki:3.6.22` | `observability`, `full` | Structured JSON log aggregation |
| `otel-collector` | `otel/opentelemetry-collector-contrib:0.166.0-${OTEL_ARCH:-arm64}` | `observability`, `full` | OTLP trace ingestion |
| `marquez` | `marquezproject/marquez:0.51.1` | `observability`, `full` | `platform: linux/amd64` (OpenLineage metadata) |

## Known Compatibility Notes

- **Matplotlib 3.11+ colormap lookup:** `matplotlib.cm.get_cmap` was removed; `processing/tile_renderer.py` uses `matplotlib.colormaps[name]`.
- **pystac 1.16.0 default version:** Emits STAC `1.1.0` JSON by default; contracts validate both `1.0.0` and `1.1.0`.
- **Marquez ARM64:** Runs with explicit `platform: linux/amd64` under emulation on ARM64 hosts.
- **Zero pretrained ML model weights:** No external ML weights are downloaded; all indices and anomaly detections are deterministic physical and statistical computations.
