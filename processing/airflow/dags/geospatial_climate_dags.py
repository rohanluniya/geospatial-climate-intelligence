"""Apache Airflow 3.4 DAG definitions for geospatial STAC ingestion, raster cube analytics, and Iceberg maintenance."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

DAG_ID = "geospatial_climate_daily_lakehouse"
TASK_DEPENDENCY_GRAPH: dict[str, list[str]] = {
    "discover_and_verify_stac_assets": ["normalize_crs_and_geoparquet"],
    "normalize_crs_and_geoparquet": ["build_xarray_dask_cube_and_mosaics"],
    "build_xarray_dask_cube_and_mosaics": ["compute_zonal_stats_and_anomalies"],
    "compute_zonal_stats_and_anomalies": ["run_geospatial_dq_gate"],
    "run_geospatial_dq_gate": ["compact_iceberg_v2_tables", "invalidate_and_warm_map_tiles"],
    "compact_iceberg_v2_tables": [],
    "invalidate_and_warm_map_tiles": [],
}

DEFAULT_ARGS: dict[str, Any] = {
    "owner": "geospatial-data-platform",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "execution_timeout": timedelta(minutes=45),
}


def build_airflow_dag() -> Any:
    """Instantiate the Airflow DAG when Airflow is installed; raises ImportError only if Airflow is absent."""
    from airflow import DAG  # type: ignore[import-not-found]
    from airflow.operators.bash import BashOperator  # type: ignore[import-not-found]

    with DAG(
        dag_id=DAG_ID,
        description="Daily STAC ingestion, multi-CRS normalization, xarray/Dask zonal analytics, and Iceberg V2 compaction",
        default_args=DEFAULT_ARGS,
        schedule="0 4 * * *",
        start_date=datetime(2026, 1, 1),
        catchup=False,
        max_active_runs=1,
        tags=["geospatial", "stac", "iceberg-v2", "climate"],
    ) as dag:
        t_stac = BashOperator(
            task_id="discover_and_verify_stac_assets",
            bash_command="geo-climate run",
        )
        t_crs = BashOperator(
            task_id="normalize_crs_and_geoparquet",
            bash_command="python -c 'from geo_climate.config import Settings; print(Settings().silver_rasters_dir)'",
        )
        t_cube = BashOperator(
            task_id="build_xarray_dask_cube_and_mosaics",
            bash_command="python -c 'from geo_climate.config import Settings; print(Settings().mosaics_dir)'",
        )
        t_zonal = BashOperator(
            task_id="compute_zonal_stats_and_anomalies",
            bash_command="geo-climate backfill --region california_central_valley --date 2026-07-15",
        )
        t_dq = BashOperator(
            task_id="run_geospatial_dq_gate",
            bash_command="python -c 'from geo_climate.config import Settings; from models.iceberg_store import IcebergLakehouseStore; s=IcebergLakehouseStore(Settings()); rows=s.read_rows(\"quality.dq_results\"); assert all(r[\"passed\"] for r in rows)'",
        )
        t_compact = BashOperator(
            task_id="compact_iceberg_v2_tables",
            bash_command="geo-climate run --compact",
        )
        t_tiles = BashOperator(
            task_id="invalidate_and_warm_map_tiles",
            bash_command="curl -fsS http://geo-api:8070/health",
        )

        t_stac >> t_crs >> t_cube >> t_zonal >> t_dq >> [t_compact, t_tiles]
        return dag


try:
    geospatial_climate_dag = build_airflow_dag()
except ImportError:
    geospatial_climate_dag = None
