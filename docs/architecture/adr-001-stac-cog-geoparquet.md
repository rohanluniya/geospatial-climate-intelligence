# ADR-001: STAC 1.1, Tiled GeoTIFF/COG, and GeoParquet v1.1.0 Storage Formats

## Status
Accepted

## Context
Earth observation pipelines frequently fail silently when rasters lack standardized metadata, checksums, or projection provenance, or when vector boundaries are stored in legacy Shapefiles that truncate column names and break across the 180th meridian.

## Decision
1. **Asset Catalog:** Adopt STAC (`pystac==1.16.0`, emitting STAC `1.1.0` with `proj:epsg`, `eo:cloud_cover`, `license`, and `geoclimate:sha256` properties) as the immutable Bronze source index.
2. **Raster Format:** Store 6-band multi-spectral, thermal LST, and QA mask scenes as tiled, DEFLATE-compressed `float32` GeoTIFFs (`32 × 32` blocks locally, `512 × 512` COG overviews in cloud object storage).
3. **Vector Boundaries:** Store hydrological basins in **GeoParquet v1.1.0** (`pyarrow` + ISO WKB geometry + PROJJSON `geo` metadata) with antimeridian polygons pre-split into valid `[-180, 180]` MultiPolygons.

## Tradeoffs & Consequences
- **Benefits:** Cloud-native byte-range reads, self-describing CRS and bbox metadata, direct interoperability with Apache Sedona, DuckDB, Trino, and GDAL.
- **Costs:** Requires explicit antimeridian unwrapping (`lon < 0 -> lon + 360`) when rasterizing Pacific split polygons onto continuous `[179, 181]` raster grids.
