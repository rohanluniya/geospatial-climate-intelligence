# Known Limitations & Local-vs-Production Boundaries

We disclose all current scope boundaries and unexecuted external integrations plainly:

1. **No Host Docker / Podman / JVM Runtime on Benchmark Machine:**
   - The local verification host (`Darwin 24.6.0`, Apple M4 Pro) has no Docker daemon or host JVM installed.
   - Consequently, the multi-container `docker-compose.yml` services (Apache Sedona `1.9.1`, Spark `4.1.4`, Trino `484`, Airflow `3.4.0`, RustFS `1.0.6`, Lakekeeper `v0.15.1`, PostGIS `17-3.6.1`, Prometheus, Grafana, Loki, OTel Collector, Marquez) were validated statically (`scripts/static_audit.py`, YAML/SQL/DAG contract unit tests) rather than started as live containers on this host.
   - All core geospatial, STAC, GeoParquet, xarray/Dask, Iceberg V2, DuckDB, FastAPI, RBAC, PNG tile serving, chaos drills, and benchmarks executed natively on real Python 3.12 ARM64 wheels.
2. **Synthetic Multi-CRS Imagery vs. Atmospheric Radiative Transfer:**
   - Local scenes are deterministically synthesized 6-band GeoTIFFs licensed `CC-BY-4.0`. While physically bounded (`NDVI` in `[-1, 1]`, realistic cloud-top temperatures, UTM/Polar/Antimeridian projections), they omit full atmospheric radiative transfer (aerosol optical depth, BRDF viewing-angle corrections, and 30-year WMO climatology baselines).
   - Every Gold zonal record explicitly surfaces `valid_pixel_pct`, 95% confidence half-widths, `quality_tier`, and a `scientific_caveat` string.
3. **Observability & Lineage Transport:**
   - Prometheus metrics are served live at `/metrics`, structured JSON logs emit to stdout, security audits persist to `.local_data/security_audit.jsonl`, and OpenTelemetry-compatible spans persist to `.local_data/otel_traces.jsonl`.
   - Automatic OpenLineage emission to Marquez requires enabling the `observability` Compose profile with a live Marquez server; local runs track explicit snapshot lineage via Iceberg V2 snapshot IDs and `X-STAC-Snapshot` headers instead of claiming automatic Marquez integration.
4. **Authentication Mode:**
   - Local loopback (`127.0.0.1:8070`) uses environment-configured static Bearer/API-key tokens (`viewer`, `analyst`, `admin`) with RBAC enforcement and redacted audit logs. Production deployments should front the API with OIDC/JWT identity verification and TLS termination.
5. **Measured Scale Scope:**
   - Measured benchmark throughput (`4.01M band-pixels/sec` at `256×256` per scene) reflects single-node local workstation performance and is not extrapolated to planet-scale petabyte throughput.
