# ADR-002: xarray + Dask Raster Cubes Paired with Apache Sedona Distributed Vector Joins

## Status
Accepted

## Context
Raster time-series compositing (cloud-free median mosaics, band math) is fundamentally multi-dimensional N-D array compute, while continental vector-to-point/polygon joins require R-Tree / KDB-Tree spatial partitioning.

## Decision
1. **Raster Compute:** Use **xarray + Dask** (`chunks=(1, chunk_size, chunk_size)`) for lazy spatiotemporal `(time, y, x)` cubes, QA cloud/nodata masking, and median compositing.
2. **Reprojection:** Use **GDAL via `rasterio.warp.reproject`** with **bilinear** resampling for continuous reflectance/LST bands and **nearest-neighbor** resampling for the categorical QA/cloud bitmask band so QA flags are never blended.
3. **Distributed Vector Joins:** Provide a Spark + **Apache Sedona 1.9.1** job (`processing/spark/sedona_geospatial_job.py`) for cluster-scale spatial joins while running vectorized `rasterio.features.geometry_mask` + `shapely` locally without requiring a host JVM.

## Tradeoffs & Consequences
- **Benefits:** Separating continuous vs categorical resampling prevents boundary QA corruption; Dask enables bounded-memory chunking that scales out to distributed workers without changing scientific logic.
- **Costs:** Over-chunking small scenes (`16 × 16` chunks on `256 × 256` grids) adds Dask task-graph scheduling overhead, as quantified in our chunk-size ablation.
