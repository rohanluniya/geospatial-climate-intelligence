"""Apache Iceberg V2 SQL-SQLite lakehouse store with snapshots, time travel, and compaction."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pyarrow as pa
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
from pyiceberg.table import Table

from geo_climate.config import Settings
from geo_climate.metrics import METRICS

TABLE_SCHEMAS: dict[str, pa.Schema] = {
    "bronze.stac_assets": pa.schema(
        [
            pa.field("scene_id", pa.string(), nullable=False),
            pa.field("collection_id", pa.string(), nullable=False),
            pa.field("region_id", pa.string(), nullable=False),
            pa.field("acquisition_date", pa.string(), nullable=False),
            pa.field("acquisition_time_utc", pa.string(), nullable=False),
            pa.field("ingest_time_utc", pa.string(), nullable=False),
            pa.field("source_epsg", pa.int64(), nullable=False),
            pa.field("cloud_cover_pct", pa.float64(), nullable=False),
            pa.field("nodata_pct", pa.float64(), nullable=False),
            pa.field("asset_uri", pa.string(), nullable=False),
            pa.field("sha256", pa.string(), nullable=False),
            pa.field("scene_version", pa.int64(), nullable=False),
            pa.field("license", pa.string(), nullable=False),
            pa.field("antimeridian_crossing", pa.bool_(), nullable=False),
            pa.field("polar_region", pa.bool_(), nullable=False),
            pa.field("bbox_json", pa.string(), nullable=False),
            pa.field("stac_item_json", pa.string(), nullable=False),
        ]
    ),
    "bronze.weather_observations": pa.schema(
        [
            pa.field("observation_id", pa.string(), nullable=False),
            pa.field("station_id", pa.string(), nullable=False),
            pa.field("region_id", pa.string(), nullable=False),
            pa.field("basin_id", pa.string(), nullable=False),
            pa.field("event_date", pa.string(), nullable=False),
            pa.field("event_time_utc", pa.string(), nullable=False),
            pa.field("ingest_time_utc", pa.string(), nullable=False),
            pa.field("latitude", pa.float64(), nullable=False),
            pa.field("longitude", pa.float64(), nullable=False),
            pa.field("air_temp_c", pa.float64(), nullable=True),
            pa.field("precip_mm", pa.float64(), nullable=True),
            pa.field("humidity_pct", pa.float64(), nullable=True),
            pa.field("event_type", pa.string(), nullable=False),
            pa.field("source_license", pa.string(), nullable=False),
            pa.field("sensor_calibration_version", pa.string(), nullable=False),
            pa.field("quality_flag", pa.string(), nullable=False),
        ]
    ),
    "bronze.quarantine_dlq": pa.schema(
        [
            pa.field("record_id", pa.string(), nullable=False),
            pa.field("source_type", pa.string(), nullable=False),
            pa.field("region_id", pa.string(), nullable=False),
            pa.field("reason_code", pa.string(), nullable=False),
            pa.field("detail", pa.string(), nullable=False),
            pa.field("quarantined_at_utc", pa.string(), nullable=False),
            pa.field("payload_json", pa.string(), nullable=False),
        ]
    ),
    "silver.normalized_scenes": pa.schema(
        [
            pa.field("scene_id", pa.string(), nullable=False),
            pa.field("region_id", pa.string(), nullable=False),
            pa.field("acquisition_date", pa.string(), nullable=False),
            pa.field("source_epsg", pa.int64(), nullable=False),
            pa.field("canonical_epsg", pa.int64(), nullable=False),
            pa.field("scene_version", pa.int64(), nullable=False),
            pa.field("valid_pixel_pct", pa.float64(), nullable=False),
            pa.field("cloud_cover_pct", pa.float64(), nullable=False),
            pa.field("nodata_pct", pa.float64(), nullable=False),
            pa.field("reproject_ms", pa.float64(), nullable=False),
            pa.field("silver_tif_uri", pa.string(), nullable=False),
            pa.field("antimeridian_unwrapped", pa.bool_(), nullable=False),
            pa.field("processed_at_utc", pa.string(), nullable=False),
        ]
    ),
    "silver.vector_boundaries_meta": pa.schema(
        [
            pa.field("basin_id", pa.string(), nullable=False),
            pa.field("basin_name", pa.string(), nullable=False),
            pa.field("region_id", pa.string(), nullable=False),
            pa.field("biome", pa.string(), nullable=False),
            pa.field("source_epsg", pa.int64(), nullable=False),
            pa.field("antimeridian_split", pa.bool_(), nullable=False),
            pa.field("polar_region", pa.bool_(), nullable=False),
            pa.field("area_deg2", pa.float64(), nullable=False),
            pa.field("geoparquet_uri", pa.string(), nullable=False),
            pa.field("geoparquet_version", pa.string(), nullable=False),
            pa.field("license", pa.string(), nullable=False),
        ]
    ),
    "gold.zonal_climate_stats": pa.schema(
        [
            pa.field("zonal_id", pa.string(), nullable=False),
            pa.field("region_id", pa.string(), nullable=False),
            pa.field("basin_id", pa.string(), nullable=False),
            pa.field("basin_name", pa.string(), nullable=False),
            pa.field("scene_id", pa.string(), nullable=False),
            pa.field("acquisition_date", pa.string(), nullable=False),
            pa.field("total_pixels", pa.int64(), nullable=False),
            pa.field("valid_pixels", pa.int64(), nullable=False),
            pa.field("valid_pixel_pct", pa.float64(), nullable=False),
            pa.field("ndvi_mean", pa.float64(), nullable=True),
            pa.field("ndvi_p10", pa.float64(), nullable=True),
            pa.field("ndvi_p50", pa.float64(), nullable=True),
            pa.field("ndvi_p90", pa.float64(), nullable=True),
            pa.field("ndvi_std", pa.float64(), nullable=True),
            pa.field("ndvi_ci95_halfwidth", pa.float64(), nullable=True),
            pa.field("ndwi_mean", pa.float64(), nullable=True),
            pa.field("flood_extent_pct", pa.float64(), nullable=True),
            pa.field("lst_c_mean", pa.float64(), nullable=True),
            pa.field("lst_c_p90", pa.float64(), nullable=True),
            pa.field("lst_ci95_halfwidth", pa.float64(), nullable=True),
            pa.field("station_air_temp_c", pa.float64(), nullable=True),
            pa.field("station_precip_mm", pa.float64(), nullable=True),
            pa.field("quality_tier", pa.string(), nullable=False),
            pa.field("scientific_caveat", pa.string(), nullable=False),
        ]
    ),
    "gold.temporal_anomalies": pa.schema(
        [
            pa.field("anomaly_id", pa.string(), nullable=False),
            pa.field("region_id", pa.string(), nullable=False),
            pa.field("basin_id", pa.string(), nullable=False),
            pa.field("scene_id", pa.string(), nullable=False),
            pa.field("acquisition_date", pa.string(), nullable=False),
            pa.field("observed_lst_c", pa.float64(), nullable=False),
            pa.field("expected_seasonal_lst_c", pa.float64(), nullable=False),
            pa.field("naive_raw_delta_lst_c", pa.float64(), nullable=False),
            pa.field("deseasonalized_z_lst", pa.float64(), nullable=False),
            pa.field("observed_ndvi", pa.float64(), nullable=False),
            pa.field("expected_seasonal_ndvi", pa.float64(), nullable=False),
            pa.field("deseasonalized_z_ndvi", pa.float64(), nullable=False),
            pa.field("observed_ndwi", pa.float64(), nullable=False),
            pa.field("deseasonalized_z_ndwi", pa.float64(), nullable=False),
            pa.field("anomaly_type", pa.string(), nullable=False),
            pa.field("severity", pa.string(), nullable=False),
            pa.field("naive_seasonal_confounding_fp", pa.bool_(), nullable=False),
            pa.field("valid_pixel_pct", pa.float64(), nullable=False),
            pa.field("uncertainty_note", pa.string(), nullable=False),
        ]
    ),
    "gold.tile_cache_audit": pa.schema(
        [
            pa.field("tile_key", pa.string(), nullable=False),
            pa.field("region_id", pa.string(), nullable=False),
            pa.field("layer", pa.string(), nullable=False),
            pa.field("z", pa.int64(), nullable=False),
            pa.field("x", pa.int64(), nullable=False),
            pa.field("y", pa.int64(), nullable=False),
            pa.field("etag", pa.string(), nullable=False),
            pa.field("cache_status", pa.string(), nullable=False),
            pa.field("render_ms", pa.float64(), nullable=False),
            pa.field("valid_pixel_pct", pa.float64(), nullable=False),
            pa.field("stac_snapshot", pa.string(), nullable=False),
            pa.field("scene_version", pa.int64(), nullable=False),
            pa.field("served_at_utc", pa.string(), nullable=False),
        ]
    ),
    "quality.dq_results": pa.schema(
        [
            pa.field("dq_id", pa.string(), nullable=False),
            pa.field("run_id", pa.string(), nullable=False),
            pa.field("check_name", pa.string(), nullable=False),
            pa.field("layer", pa.string(), nullable=False),
            pa.field("passed", pa.bool_(), nullable=False),
            pa.field("observed_value", pa.float64(), nullable=False),
            pa.field("detail", pa.string(), nullable=False),
            pa.field("evaluated_at_utc", pa.string(), nullable=False),
        ]
    ),
}


class IcebergLakehouseStore:
    """Manages Apache Iceberg V2 Bronze/Silver/Gold/Quality tables over a local SQL-SQLite catalog."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_dirs()
        self.warehouse_uri = f"file://{self.settings.warehouse_dir}"
        self.catalog_uri = f"sqlite:///{self.settings.catalog_db_path}"
        self.catalog = SqlCatalog(
            "geoclimate",
            **{
                "uri": self.catalog_uri,
                "warehouse": self.warehouse_uri,
            },
        )
        self._ensure_namespaces_and_tables()

    def _ensure_namespaces_and_tables(self) -> None:
        for ns in ("bronze", "silver", "gold", "quality"):
            try:
                self.catalog.create_namespace(ns)
            except NamespaceAlreadyExistsError:
                pass

        for full_name, arrow_schema in TABLE_SCHEMAS.items():
            try:
                self.catalog.load_table(full_name)
            except NoSuchTableError:
                self.catalog.create_table(
                    identifier=full_name,
                    schema=arrow_schema,
                    properties={"format-version": "2"},
                )

    def load_table(self, table_name: str) -> Table:
        return self.catalog.load_table(table_name)

    def _rows_to_arrow(self, table_name: str, rows: list[dict[str, Any]]) -> pa.Table:
        schema = TABLE_SCHEMAS[table_name]
        if not rows:
            arrays = [pa.array([], type=field.type) for field in schema]
            return pa.Table.from_arrays(arrays, schema=schema)
        cols: dict[str, list[Any]] = {field.name: [] for field in schema}
        for row in rows:
            for field in schema:
                cols[field.name].append(row.get(field.name))
        arrays = [pa.array(cols[field.name], type=field.type) for field in schema]
        return pa.Table.from_arrays(arrays, schema=schema)

    def overwrite_rows(self, table_name: str, rows: list[dict[str, Any]]) -> int:
        """Atomically overwrite an Iceberg V2 table snapshot."""
        tbl = self.load_table(table_name)
        pa_tbl = self._rows_to_arrow(table_name, rows)
        tbl.overwrite(pa_tbl)
        tbl.refresh()
        snap_count = len(list(tbl.snapshots()))
        METRICS.set_gauge("geoclimate_active_iceberg_snapshots", float(snap_count), table=table_name)
        return len(rows)

    def append_rows(self, table_name: str, rows: list[dict[str, Any]]) -> int:
        """Append a new data file to an Iceberg V2 table."""
        if not rows:
            return 0
        tbl = self.load_table(table_name)
        pa_tbl = self._rows_to_arrow(table_name, rows)
        tbl.append(pa_tbl)
        tbl.refresh()
        snap_count = len(list(tbl.snapshots()))
        METRICS.set_gauge("geoclimate_active_iceberg_snapshots", float(snap_count), table=table_name)
        return len(rows)

    def read_arrow(self, table_name: str, snapshot_id: int | None = None) -> pa.Table:
        tbl = self.load_table(table_name)
        tbl.refresh()
        if tbl.current_snapshot() is None:
            return self._rows_to_arrow(table_name, [])
        scan = tbl.scan(snapshot_id=snapshot_id) if snapshot_id is not None else tbl.scan()
        return scan.to_arrow()

    def read_rows(self, table_name: str, snapshot_id: int | None = None) -> list[dict[str, Any]]:
        return self.read_arrow(table_name, snapshot_id=snapshot_id).to_pylist()

    def data_file_count(self, table_name: str) -> int:
        tbl = self.load_table(table_name)
        tbl.refresh()
        if tbl.current_snapshot() is None:
            return 0
        return len(list(tbl.scan().plan_files()))

    def snapshot_history(self, table_name: str) -> list[dict[str, Any]]:
        tbl = self.load_table(table_name)
        tbl.refresh()
        out: list[dict[str, Any]] = []
        for snap in tbl.snapshots():
            summary_dict = dict(snap.summary.additional_properties) if snap.summary else {}
            out.append(
                {
                    "table": table_name,
                    "snapshot_id": int(snap.snapshot_id),
                    "parent_snapshot_id": int(snap.parent_snapshot_id) if snap.parent_snapshot_id else None,
                    "timestamp_ms": int(snap.timestamp_ms),
                    "operation": snap.summary.operation.value if snap.summary and snap.summary.operation else "unknown",
                    "total_records": summary_dict.get("total-records"),
                    "total_data_files": summary_dict.get("total-data-files"),
                }
            )
        return out

    def latest_snapshot_id(self, table_name: str) -> str:
        tbl = self.load_table(table_name)
        tbl.refresh()
        snap = tbl.current_snapshot()
        return str(snap.snapshot_id) if snap else "empty"

    def compact_table(self, table_name: str) -> dict[str, Any]:
        """Coalesce fragmented Iceberg V2 Parquet data files into a single compacted file snapshot."""
        t0 = time.perf_counter()
        files_before = self.data_file_count(table_name)
        pa_tbl = self.read_arrow(table_name)
        rows = pa_tbl.num_rows
        if rows > 0 and files_before > 1:
            # Combine chunks into single contiguous Arrow table before atomic Iceberg V2 overwrite
            coalesced = pa_tbl.combine_chunks()
            tbl = self.load_table(table_name)
            tbl.overwrite(coalesced)
            tbl.refresh()
        files_after = self.data_file_count(table_name)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        return {
            "table": table_name,
            "rows": rows,
            "files_before": files_before,
            "files_after": files_after,
            "compaction_ms": elapsed_ms,
        }

    def all_table_summaries(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for table_name in TABLE_SCHEMAS:
            tbl = self.load_table(table_name)
            tbl.refresh()
            snap = tbl.current_snapshot()
            summaries.append(
                {
                    "table": table_name,
                    "format_version": 2,
                    "current_snapshot_id": str(snap.snapshot_id) if snap else None,
                    "snapshot_count": len(list(tbl.snapshots())),
                    "data_files": self.data_file_count(table_name),
                    "row_count": self.read_arrow(table_name).num_rows,
                }
            )
        return summaries

    def warehouse_disk_bytes(self) -> int:
        root: Path = self.settings.warehouse_dir
        return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
