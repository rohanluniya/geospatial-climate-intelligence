# Principal Engineer Interview Deep Dive

## 1. Why Split Antimeridian Polygons for Storage Yet Unwrap for Rasterization?

STAC `1.0/1.1`, GeoJSON RFC 7946, PostGIS constraints, and GeoParquet v1.1.0 require polygon coordinates strictly inside `[-180, 180]`. A naive box `(179, -18.5, -179, -16)` spans 358 degrees around the wrong side of Earth unless split into a two-part `MultiPolygon` (`[179, 180]` and `[-180, -179]`). However, rasterizing two disconnected halves across opposite edges of a global grid introduces edge-sampling errors along the `±180°` seam. Our engine stores strictly valid split MultiPolygons in GeoParquet and STAC (verified by Hypothesis property tests), then unwraps the western half (`lon < 0 -> lon + 360`) onto the continuous `[179, 181]` Fiji raster grid during `geometry_mask` zonal aggregation — preserving both standards compliance and exact area/pixel conservation.

## 2. Why Separate Resampling Algorithms Between Spectral Bands and QA Masks?

Continuous physical measurements (surface reflectance, brightness temperature) require bilinear or cubic resampling during `rasterio.warp.reproject` to avoid stair-step artifacts when transforming UTM (`EPSG:32610`) or Polar Stereographic (`EPSG:3413`) grids to `EPSG:4326`. Applying bilinear interpolation to a categorical QA mask (`0=clear`, `1=cloud`, `2=shadow`, `3=nodata`) fabricates invalid intermediate codes (e.g., averaging `0` and `2` into `1`) and smears cloud edges unpredictably. Our pipeline strictly binds `Resampling.bilinear` to bands `1..5` and `Resampling.nearest` to band `6` (`qa_mask`).

## 3. How Does Deseasonalized Robust MAD Beat Naive Delta & Standard Z-Score?

- **vs. Naive Annual-Mean Delta:** Annual mean ignores the seasonal cycle, flagging normal California winters (`15.6 °C` vs `33.0 °C` annual mean -> `-17.4 °C` naive delta) as false alarms. Subtracting the seasonal harmonic expectation reduces that winter residual to `+0.13 °C` (`z = 0.093`, cleanly classified `NORMAL`).
- **vs. Ordinary Mean/Std Z-Score:** In short time series (6–12 observations), a single extreme `+10 °C` heatwave inflates the ordinary sample mean and standard deviation so heavily that the heatwave can mask itself below `z < 2.0`. Centering residuals on the **median** and scaling by `1.4826 * MAD` (with physical variability floors) stays robust to extreme events.

## 4. Why Apache Iceberg V2 Over Plain Parquet Files on Object Storage?

Plain Parquet directories have no atomic multi-file commit protocol, cannot support reproducible time-travel queries against a prior ingestion batch, and expose readers to partially written files during concurrent backfills or compaction. Iceberg V2 commits atomic metadata snapshots (`snapshot_id`), enables time-travel verification (`store.read_rows(..., snapshot_id=prior_id)`), links every rendered PNG map tile to its exact `X-STAC-Snapshot` header, and allows online compaction (`25 -> 1` file, `9.33×` faster scans) without blocking concurrent readers.

## 5. How Does Local Verification Translate to Distributed Cluster Execution?

The local zero-Docker profile executes the exact same GDAL/rasterio, Shapely, PyProj, xarray/Dask, pystac, PyIceberg V2, DuckDB, and FastAPI code paths on `64²–256²` scenes. Scaling to continental Sentinel-2/Landsat archives swaps three pluggable boundaries without altering scientific formulas: (1) local filesystem -> S3/RustFS COG byte-range reads, (2) SQL-SQLite Iceberg catalog -> Lakekeeper REST Catalog + PostGIS, and (3) single-node Dask/DuckDB -> distributed Dask cluster + Apache Sedona Spark joins + Trino SQL (`docker-compose.yml` `full` profile).
