"""Integration tests for deterministic replay, Iceberg V2 time travel, and Parquet compaction."""

from __future__ import annotations

from pathlib import Path

from geo_climate.config import Settings
from models.iceberg_store import IcebergLakehouseStore
from processing.pipeline import run_full_pipeline


def test_deterministic_seed_produces_identical_stac_ids_and_zonal_means(tmp_path: Path) -> None:
    s1 = Settings(data_dir=tmp_path / "run_a", seed=20260801)
    s2 = Settings(data_dir=tmp_path / "run_b", seed=20260801)

    res1 = run_full_pipeline(s1, warm_tiles=False)
    res2 = run_full_pipeline(s2, warm_tiles=False)

    assert res1["zonal_stats_rows"] == res2["zonal_stats_rows"]
    assert res1["dq_passed"] == res1["dq_total"] == 8

    store1 = IcebergLakehouseStore(s1)
    store2 = IcebergLakehouseStore(s2)

    stac1 = sorted((r["scene_id"], r["sha256"]) for r in store1.read_rows("bronze.stac_assets"))
    stac2 = sorted((r["scene_id"], r["sha256"]) for r in store2.read_rows("bronze.stac_assets"))
    assert stac1 == stac2

    z1 = sorted((r["zonal_id"], r["ndvi_mean"], r["lst_c_mean"]) for r in store1.read_rows("gold.zonal_climate_stats"))
    z2 = sorted((r["zonal_id"], r["ndvi_mean"], r["lst_c_mean"]) for r in store2.read_rows("gold.zonal_climate_stats"))
    assert z1 == z2


def test_iceberg_v2_time_travel_and_compaction(tmp_path: Path) -> None:
    cfg = Settings(data_dir=tmp_path / "iceberg_tt", seed=20260801)
    run_full_pipeline(cfg, warm_tiles=False)
    store = IcebergLakehouseStore(cfg)

    rows_initial = store.read_rows("gold.zonal_climate_stats")
    snap_initial = int(store.latest_snapshot_id("gold.zonal_climate_stats"))

    # Append 5 fragmented small batches to simulate streaming micro-batch small-file sprawl
    sample_batch = rows_initial[:4]
    for _ in range(5):
        store.append_rows("gold.zonal_climate_stats", sample_batch)

    assert store.data_file_count("gold.zonal_climate_stats") >= 6
    # Time travel back to initial snapshot returns exact initial row count
    tt_rows = store.read_rows("gold.zonal_climate_stats", snapshot_id=snap_initial)
    assert len(tt_rows) == len(rows_initial)

    # Compaction coalesces all small files down to 1 single data file while preserving row count
    total_before = len(store.read_rows("gold.zonal_climate_stats"))
    comp = store.compact_table("gold.zonal_climate_stats")
    assert comp["files_before"] >= 6
    assert comp["files_after"] == 1
    assert len(store.read_rows("gold.zonal_climate_stats")) == total_before
