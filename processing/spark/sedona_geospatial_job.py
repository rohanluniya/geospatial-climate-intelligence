"""Production Apache Sedona + PySpark distributed spatial join and GeoParquet pipeline job.

Designed for distributed execution on Spark 4.1.4 + Apache Sedona 1.9.1 (see docker-compose.yml
`analytics` profile). Pure-Python geometry & SQL helpers are kept importable without a host JVM
so unit tests can validate SQL contracts and transform parameters locally.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class SedonaJobConfig:
    """Configuration for distributed Apache Sedona spatial join over GeoParquet & Iceberg."""

    stac_bronze_table: str = "lakehouse.bronze.stac_assets"
    basins_geoparquet_uri: str = "s3a://geoclimate-silver/vectors/climate_basins.geoparquet"
    weather_bronze_table: str = "lakehouse.bronze.weather_observations"
    gold_spatial_join_table: str = "lakehouse.gold.sedona_basin_weather_join"
    canonical_crs: str = "EPSG:4326"
    spatial_grid_partitions: int = 64


def build_sedona_spatial_join_sql(cfg: SedonaJobConfig) -> str:
    """Generate the canonical Apache Sedona SQL for point-in-polygon station-to-basin spatial join."""
    return f"""
WITH normalized_basins AS (
    SELECT
        basin_id,
        basin_name,
        region_id,
        biome,
        antimeridian_split,
        polar_region,
        ST_MakeValid(ST_GeomFromWKB(geometry)) AS basin_geom
    FROM basins_geoparquet
),
station_points AS (
    SELECT
        observation_id,
        station_id,
        region_id,
        event_date,
        air_temp_c,
        precip_mm,
        humidity_pct,
        ST_Point(CAST(longitude AS DOUBLE), CAST(latitude AS DOUBLE)) AS station_geom
    FROM {cfg.weather_bronze_table}
    WHERE event_type != 'RETRACTION'
      AND longitude BETWEEN -180.0 AND 180.0
      AND latitude BETWEEN -90.0 AND 90.0
)
SELECT
    b.basin_id,
    b.region_id,
    s.event_date,
    COUNT(*) AS station_readings,
    ROUND(AVG(s.air_temp_c), 3) AS mean_air_temp_c,
    ROUND(AVG(s.precip_mm), 3) AS mean_precip_mm,
    ROUND(AVG(s.humidity_pct), 2) AS mean_humidity_pct,
    ROUND(ST_AreaSpheroid(b.basin_geom) / 1e6, 2) AS basin_area_km2
FROM normalized_basins b
JOIN station_points s
  ON ST_Contains(b.basin_geom, s.station_geom)
GROUP BY b.basin_id, b.region_id, s.event_date, b.basin_geom
""".strip()


def run_sedona_job(cfg: SedonaJobConfig) -> None:
    """Execute the Apache Sedona job inside a PySpark + Sedona cluster runtime."""
    from pyspark.sql import SparkSession  # type: ignore[import-not-found]
    from sedona.spark import SedonaContext  # type: ignore[import-not-found]

    config = (
        SedonaContext.builder()
        .appName("geospatial-climate-sedona-spatial-join")
        .config("spark.sql.shuffle.partitions", str(cfg.spatial_grid_partitions))
        .config("sedona.global.index", "true")
        .config("sedona.global.indextype", "rtree")
        .config("sedona.join.gridtype", "kdbtree")
        .getOrCreate()
    )
    sedona: SparkSession = SedonaContext.create(config)

    basins_df = sedona.read.format("geoparquet").load(cfg.basins_geoparquet_uri)
    basins_df.createOrReplaceTempView("basins_geoparquet")

    result_df = sedona.sql(build_sedona_spatial_join_sql(cfg))
    result_df.writeTo(cfg.gold_spatial_join_table).using("iceberg").createOrReplace()
    sedona.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Apache Sedona distributed geospatial join")
    parser.add_argument("--basins-uri", default=SedonaJobConfig.basins_geoparquet_uri)
    parser.add_argument("--partitions", type=int, default=SedonaJobConfig.spatial_grid_partitions)
    args = parser.parse_args()
    cfg = SedonaJobConfig(basins_geoparquet_uri=args.basins_uri, spatial_grid_partitions=args.partitions)
    run_sedona_job(cfg)


if __name__ == "__main__":
    main()
