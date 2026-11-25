"""DuckDB analytical SQL engine over Apache Iceberg V2 snapshots and GeoParquet boundaries."""

from __future__ import annotations

from typing import Any

import duckdb

from models.iceberg_store import IcebergLakehouseStore


class ClimateQueryEngine:
    """Executes vectorized DuckDB SQL analytics directly over PyIceberg Arrow tables."""

    def __init__(self, store: IcebergLakehouseStore) -> None:
        self.store = store

    def _connect_with_views(self) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(database=":memory:")
        table_aliases = {
            "bronze_stac_assets": "bronze.stac_assets",
            "bronze_weather_observations": "bronze.weather_observations",
            "bronze_quarantine_dlq": "bronze.quarantine_dlq",
            "silver_normalized_scenes": "silver.normalized_scenes",
            "silver_vector_boundaries_meta": "silver.vector_boundaries_meta",
            "gold_zonal_climate_stats": "gold.zonal_climate_stats",
            "gold_temporal_anomalies": "gold.temporal_anomalies",
            "gold_tile_cache_audit": "gold.tile_cache_audit",
            "quality_dq_results": "quality.dq_results",
        }
        for alias, full_name in table_aliases.items():
            con.register(alias, self.store.read_arrow(full_name))
        return con

    def region_summary(self) -> list[dict[str, Any]]:
        con = self._connect_with_views()
        try:
            rel = con.sql(
                """
                SELECT
                    z.region_id,
                    COUNT(DISTINCT z.basin_id) AS basin_count,
                    COUNT(DISTINCT z.scene_id) AS scene_count,
                    ROUND(AVG(z.ndvi_mean), 4) AS avg_ndvi,
                    ROUND(AVG(z.ndwi_mean), 4) AS avg_ndwi,
                    ROUND(AVG(z.lst_c_mean), 2) AS avg_lst_c,
                    ROUND(AVG(z.valid_pixel_pct), 2) AS avg_valid_pixel_pct,
                    SUM(CASE WHEN a.anomaly_type != 'NORMAL' THEN 1 ELSE 0 END) AS anomaly_events,
                    SUM(CASE WHEN a.naive_seasonal_confounding_fp THEN 1 ELSE 0 END) AS seasonal_fp_suppressed
                FROM gold_zonal_climate_stats z
                LEFT JOIN gold_temporal_anomalies a
                  ON z.zonal_id = a.anomaly_id
                GROUP BY z.region_id
                ORDER BY z.region_id
                """
            )
            cols = [d[0] for d in rel.description]
            return [dict(zip(cols, row, strict=True)) for row in rel.fetchall()]
        finally:
            con.close()

    def top_anomalies(self, limit: int = 25, only_flagged: bool = False) -> list[dict[str, Any]]:
        con = self._connect_with_views()
        try:
            where_clause = "WHERE anomaly_type != 'NORMAL'" if only_flagged else ""
            rel = con.sql(
                f"""
                SELECT *
                FROM gold_temporal_anomalies
                {where_clause}
                ORDER BY GREATEST(ABS(deseasonalized_z_lst), ABS(deseasonalized_z_ndvi), ABS(deseasonalized_z_ndwi)) DESC
                LIMIT {int(limit)}
                """
            )
            cols = [d[0] for d in rel.description]
            return [dict(zip(cols, row, strict=True)) for row in rel.fetchall()]
        finally:
            con.close()

    def zonal_stats(
        self,
        *,
        region_id: str | None = None,
        acquisition_date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        con = self._connect_with_views()
        try:
            filters: list[str] = []
            params: list[Any] = []
            if region_id:
                filters.append("region_id = ?")
                params.append(region_id)
            if acquisition_date:
                filters.append("acquisition_date = ?")
                params.append(acquisition_date)
            where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
            sql = f"""
                SELECT *
                FROM gold_zonal_climate_stats
                {where_sql}
                ORDER BY acquisition_date DESC, region_id ASC, basin_id ASC
                LIMIT {int(limit)}
            """
            cur = con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
        finally:
            con.close()
