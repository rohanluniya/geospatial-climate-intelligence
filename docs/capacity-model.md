# Capacity Model & Scaling Analysis

## Local Measured Footprint vs Distributed Scale Design

We separate **measured local workstation figures** from **projected distributed cluster sizing** explicitly so no unmeasured claim is confused with local measurements.

### Measured Local Workload Profiles (Single Node, 14-Core ARM64)

Each scene consists of a 6-band `float32` tiled GeoTIFF (`red`, `green`, `nir`, `swir`, `lst_k`, `qa_mask`) with DEFLATE compression:

| Profile | Grid per Scene | Scenes | Raw Uncompressed Pixels (6 Bands) | Intended Use |
| --- | --- | --- | --- | --- |
| `smoke` | `64 × 64` | 22 | `540,672` | Fast PR CI checks & unit verification |
| `local` | `128 × 128` | 22 | `2,162,688` | Developer workstation & dashboard runtime |
| `stress` | `256 × 256` | 22 | `8,650,752` | Local memory, Dask chunking, & reprojection stress test |

### Memory & Storage Budget Formulas

For a regional cube of $T$ timestamps, height $H$, width $W$, and $B$ `float32` bands (4 bytes/pixel):
- **In-memory dense array footprint:** $M_{\text{dense}} = T \times H \times W \times B \times 4\text{ bytes}$.
- **Chunked Dask peak working set:** bounded by $C_{\text{workers}} \times c_y \times c_x \times T \times 4\text{ bytes}$ when reducing across time (`nanmedian`) with spatial chunk edge $c_y = c_x$.
- **Tile cache sizing:** Each 256×256 RGBA PNG tile occupies ~15–65 KB on disk (DEFLATE compressed). Caching 1,000 hot tiles consumes `< 65 MB` of RAM/disk.

### Scaling Path to Planet-Scale Production (Architecture Only — Not Measured Locally)

When scaling from regional 256×256 test grids to full Sentinel-2 (10,980 × 10,980 px tiles) or Landsat Collection 2 scenes:
1. **Object Storage & COG Byte-Range Reads:** Store Cloud-Optimized GeoTIFFs with internal overviews in RustFS/S3/GCS so Dask workers read only intersecting spatial tiles (`512 × 512` blocks) via HTTP range requests.
2. **Spatial Partitioning:** Partition Iceberg V2 Gold tables by `(region_id, acquisition_month)` and cluster GeoParquet vector boundaries via Hilbert/S2/H3 spatial keys for Apache Sedona KDB-Tree partitioned joins (`processing/spark/sedona_geospatial_job.py`).
3. **Compaction Cadence:** Run scheduled Iceberg V2 `rewrite_data_files` compaction (via `infra/kubernetes/cronjob-pipeline.yaml` or `processing/airflow/dags/geospatial_climate_dags.py`) to keep Parquet target file size at 128–512 MB and eliminate small-file metadata overhead.
4. **CDN + Content-Addressed Tile Tiering:** Front `/v1/tiles/{layer}/{z}/{x}/{y}.png` with a CDN honoring the immutable `ETag` and `X-STAC-Snapshot` version keys.
