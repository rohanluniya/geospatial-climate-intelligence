-- PostGIS 17-3.6.1 Basin Boundary & Station Spatial Indexing DDL
-- Supports antimeridian-safe MultiPolygon geometries in EPSG:4326 and spherical geography area calculations.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS climate_basins (
    basin_id TEXT PRIMARY KEY,
    basin_name TEXT NOT NULL,
    region_id TEXT NOT NULL,
    biome TEXT NOT NULL,
    source_epsg INTEGER NOT NULL,
    antimeridian_split BOOLEAN NOT NULL DEFAULT FALSE,
    polar_region BOOLEAN NOT NULL DEFAULT FALSE,
    license TEXT NOT NULL DEFAULT 'CC-BY-4.0',
    geom GEOMETRY(MultiPolygon, 4326) NOT NULL,
    CHECK (ST_IsValid(geom)),
    CHECK (ST_XMin(geom) >= -180.0 AND ST_XMax(geom) <= 180.0),
    CHECK (ST_YMin(geom) >= -90.0 AND ST_YMax(geom) <= 90.0)
);

CREATE INDEX IF NOT EXISTS idx_climate_basins_geom_gist
    ON climate_basins USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_climate_basins_geog_gist
    ON climate_basins USING GIST ((geom::geography));

-- Spherical area and antimeridian-safe inspection view
CREATE OR REPLACE VIEW v_climate_basins_metrics AS
SELECT
    basin_id,
    region_id,
    biome,
    antimeridian_split,
    polar_region,
    ROUND((ST_Area(geom::geography) / 1000000.0)::numeric, 2) AS geodesic_area_km2,
    ST_NumGeometries(geom) AS polygon_parts
FROM climate_basins;
