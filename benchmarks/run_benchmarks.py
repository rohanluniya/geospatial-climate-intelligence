"""Reproducible geospatial & climate performance benchmarks with raw JSON/CSV and Matplotlib visualization."""

from __future__ import annotations

import csv
import json
import os
import platform
import shutil
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np

from geo_climate.config import PROJECT_ROOT, REGION_BY_ID, Settings
from models.iceberg_store import IcebergLakehouseStore
from processing.climate_analytics import compute_zonal_statistics
from processing.crs_and_geometry import read_geoparquet, reproject_raster_to_canonical
from processing.pipeline import run_full_pipeline
from processing.raster_cube import build_spatiotemporal_cube, compute_cloud_free_mosaic
from processing.tile_renderer import VALID_LAYERS, ReproducibleTileServer

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RESULTS_DIR = PROJECT_ROOT / "results" / "benchmarks"
CHART_PATH = PROJECT_ROOT / "docs" / "assets" / "benchmark_geospatial_climate.png"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    return round(float(np.percentile(np.array(values, dtype=np.float64), pct)), 4)


def run_profile_benchmarks(work_root: Path) -> list[dict[str, Any]]:
    """Measure smoke (64x64), local (128x128), and stress (256x256) full-pipeline stage latencies."""
    profiles = (
        ("smoke", 64, 32),
        ("local", 128, 64),
        ("stress", 256, 64),
    )
    rows: list[dict[str, Any]] = []
    for name, dim, chunk in profiles:
        p_dir = work_root / f"profile_{name}"
        cfg = Settings(
            data_dir=p_dir,
            seed=20260801,
            raster_width=dim,
            raster_height=dim,
            dask_chunk_size=chunk,
        )
        summary = run_full_pipeline(cfg, warm_tiles=False, compact_tables=False)
        scenes = int(summary["stac_summary"]["validated_items"])
        band_pixels = scenes * dim * dim * 6
        total_s = max(float(summary["total_duration_ms"]) / 1000.0, 1e-6)
        st = summary["stage_timings_ms"]
        rows.append(
            {
                "profile": name,
                "raster_px": f"{dim}x{dim}",
                "dask_chunk": chunk,
                "validated_scenes": scenes,
                "total_band_pixels": band_pixels,
                "generate_sources_ms": st["generate_sources_ms"],
                "stac_bronze_ms": st["stac_bronze_ms"],
                "crs_reprojection_ms": st["crs_reprojection_ms"],
                "cube_zonal_anomaly_ms": st["cube_zonal_anomaly_ms"],
                "dq_ms": st["dq_ms"],
                "total_duration_ms": summary["total_duration_ms"],
                "band_pixels_per_sec": round(band_pixels / total_s, 1),
                "scenes_per_sec": round(scenes / total_s, 2),
                "dq_passed": summary["dq_passed"],
                "dq_total": summary["dq_total"],
            }
        )
    return rows


def run_dask_chunk_ablation(work_root: Path) -> list[dict[str, Any]]:
    """Compare Dask spatial chunk sizes (16, 32, 64, 128, 256) on a 256x256x6 multi-date region cube."""
    stress_dir = work_root / "profile_stress"
    cfg = Settings(data_dir=stress_dir, seed=20260801, raster_width=256, raster_height=256)
    store = IcebergLakehouseStore(cfg)
    basins, _ = read_geoparquet(cfg.vectors_dir / "climate_basins.geoparquet")
    weather = store.read_rows("bronze.weather_observations")

    region_id = "amazon_tapajos_basin"
    region = REGION_BY_ID[region_id]
    stac_items = [r for r in store.read_rows("bronze.stac_assets") if r["region_id"] == region_id]

    # Pre-reproject once so we isolate pure xarray/Dask cube + mosaic + zonal join scaling
    norm_scenes = []
    transform = None
    for item in stac_items:
        norm = reproject_raster_to_canonical(
            Path(item["asset_uri"]),
            region.wgs84_bbox,
            256,
            256,
            canonical_epsg=4326,
            antimeridian=False,
        )
        transform = norm.transform
        norm_scenes.append((str(item["scene_id"]), str(item["acquisition_date"]), norm))
    assert transform is not None

    baseline_ndvi_mean: float | None = None
    results: list[dict[str, Any]] = []
    for chunk_size in (16, 32, 64, 128, 256):
        runs_ms: list[float] = []
        last_ndvi_mean = 0.0
        n_chunks = 0
        for _ in range(5):
            t0 = time.perf_counter()
            cube = build_spatiotemporal_cube(region_id, norm_scenes, chunk_size=chunk_size)
            n_chunks = int(cube["ndvi"].data.npartitions)
            mosaic = compute_cloud_free_mosaic(cube, transform=transform)
            zonal = compute_zonal_statistics(cube, transform, basins, weather)
            runs_ms.append((time.perf_counter() - t0) * 1000.0)
            last_ndvi_mean = round(
                float(np.mean([r["ndvi_mean"] for r in zonal if r["ndvi_mean"] is not None])),
                6,
            )
            _ = mosaic.valid_pixel_pct

        if baseline_ndvi_mean is None:
            baseline_ndvi_mean = last_ndvi_mean
        parity_delta = round(abs(last_ndvi_mean - baseline_ndvi_mean), 8)
        results.append(
            {
                "chunk_size": chunk_size,
                "dask_partitions": n_chunks,
                "median_ms": round(statistics.median(runs_ms), 3),
                "p95_ms": _percentile(runs_ms, 95),
                "mean_ndvi_check": last_ndvi_mean,
                "parity_delta_vs_baseline": parity_delta,
            }
        )
    return results


def run_tile_cache_benchmark(work_root: Path) -> dict[str, Any]:
    """Compare Cold PNG render (MISS) vs Disk-Warm HIT vs Memory-Warm HIT across all regions & layers."""
    local_dir = work_root / "profile_local"
    mosaics_dir = local_dir / "mosaics"
    cache_dir = work_root / "tile_bench_cache"
    if cache_dir.exists():
        shutil.rmtree(cache_dir)

    regions = (
        "california_central_valley",
        "amazon_tapajos_basin",
        "fiji_pacific_atoll",
        "svalbard_arctic_fjord",
    )
    coords = [(z, x, y) for z in (0, 1) for x in range(1 << z) for y in range(1 << z)]  # 5 tiles per layer

    # 1. Cold renders (empty cache)
    cold_server = ReproducibleTileServer(cache_dir, mosaics_dir)
    cold_ms: list[float] = []
    for region_id in regions:
        for layer in VALID_LAYERS:
            for z, x, y in coords[:2]:  # 32 cold tiles
                t0 = time.perf_counter()
                tile = cold_server.render_tile(
                    layer=layer,
                    region_id=region_id,
                    z=z,
                    x=x,
                    y=y,
                    stac_snapshot="bench-snap-1",
                    scene_version=1,
                )
                cold_ms.append((time.perf_counter() - t0) * 1000.0)
                assert tile.cache_status == "MISS"

    # 2. Disk-warm renders (fresh server instance reading persisted PNGs from disk)
    disk_server = ReproducibleTileServer(cache_dir, mosaics_dir)
    disk_ms: list[float] = []
    for region_id in regions:
        for layer in VALID_LAYERS:
            for z, x, y in coords[:2]:
                t0 = time.perf_counter()
                tile = disk_server.render_tile(
                    layer=layer,
                    region_id=region_id,
                    z=z,
                    x=x,
                    y=y,
                    stac_snapshot="bench-snap-1",
                    scene_version=1,
                )
                disk_ms.append((time.perf_counter() - t0) * 1000.0)
                assert tile.cache_status == "HIT"

    # 3. Memory-warm renders (in-process LRU/dict hit)
    mem_ms: list[float] = []
    for _ in range(2):
        for region_id in regions:
            for layer in VALID_LAYERS:
                for z, x, y in coords[:2]:
                    t0 = time.perf_counter()
                    tile = disk_server.render_tile(
                        layer=layer,
                        region_id=region_id,
                        z=z,
                        x=x,
                        y=y,
                        stac_snapshot="bench-snap-1",
                        scene_version=1,
                    )
                    mem_ms.append((time.perf_counter() - t0) * 1000.0)
                    assert tile.cache_status == "HIT"

    cold_p50 = _percentile(cold_ms, 50)
    disk_p50 = _percentile(disk_ms, 50)
    mem_p50 = _percentile(mem_ms, 50)
    return {
        "cold_samples": len(cold_ms),
        "cold_p50_ms": cold_p50,
        "cold_p95_ms": _percentile(cold_ms, 95),
        "cold_p99_ms": _percentile(cold_ms, 99),
        "disk_warm_p50_ms": disk_p50,
        "disk_warm_p95_ms": _percentile(disk_ms, 95),
        "mem_warm_samples": len(mem_ms),
        "mem_warm_p50_ms": mem_p50,
        "mem_warm_p95_ms": _percentile(mem_ms, 95),
        "mem_warm_p99_ms": _percentile(mem_ms, 99),
        "disk_speedup_vs_cold_x": round(cold_p50 / max(disk_p50, 1e-4), 1),
        "mem_speedup_vs_cold_x": round(cold_p50 / max(mem_p50, 1e-4), 1),
    }


def run_iceberg_compaction_benchmark(work_root: Path) -> dict[str, Any]:
    """Measure Apache Iceberg V2 small-file fragmentation vs compacted Parquet scan latency."""
    comp_dir = work_root / "iceberg_compaction"
    cfg = Settings(data_dir=comp_dir, seed=20260801, raster_width=64, raster_height=64)
    run_full_pipeline(cfg, warm_tiles=False)
    store = IcebergLakehouseStore(cfg)

    seed_rows = store.read_rows("gold.zonal_climate_stats")
    # Append 24 micro-batches to create realistic streaming small-file fragmentation
    for batch_idx in range(24):
        tagged = [
            dict(r, zonal_id=f"{r['zonal_id']}__mb{batch_idx:02d}")
            for r in seed_rows[:12]
        ]
        store.append_rows("gold.zonal_climate_stats", tagged)

    files_before = store.data_file_count("gold.zonal_climate_stats")
    rows_before = store.read_arrow("gold.zonal_climate_stats").num_rows

    before_ms: list[float] = []
    for _ in range(20):
        t0 = time.perf_counter()
        arr = store.read_arrow("gold.zonal_climate_stats")
        assert arr.num_rows == rows_before
        before_ms.append((time.perf_counter() - t0) * 1000.0)

    comp_meta = store.compact_table("gold.zonal_climate_stats")

    files_after = store.data_file_count("gold.zonal_climate_stats")
    rows_after = store.read_arrow("gold.zonal_climate_stats").num_rows
    assert rows_after == rows_before

    after_ms: list[float] = []
    for _ in range(20):
        t0 = time.perf_counter()
        arr = store.read_arrow("gold.zonal_climate_stats")
        assert arr.num_rows == rows_after
        after_ms.append((time.perf_counter() - t0) * 1000.0)

    p50_before = _percentile(before_ms, 50)
    p50_after = _percentile(after_ms, 50)
    return {
        "table": "gold.zonal_climate_stats",
        "rows_preserved": rows_after,
        "files_before": files_before,
        "files_after": files_after,
        "compaction_duration_ms": comp_meta["compaction_ms"],
        "scan_p50_before_ms": p50_before,
        "scan_p95_before_ms": _percentile(before_ms, 95),
        "scan_p50_after_ms": p50_after,
        "scan_p95_after_ms": _percentile(after_ms, 95),
        "scan_speedup_x": round(p50_before / max(p50_after, 1e-4), 2),
    }


def render_benchmark_chart(
    profiles: list[dict[str, Any]],
    dask_ablation: list[dict[str, Any]],
    tile_bench: dict[str, Any],
    compaction: dict[str, Any],
    out_path: Path,
) -> None:
    """Render a publication-grade 2x2 dark-theme Matplotlib chart of measured geospatial benchmarks."""
    plt.style.use("dark_background")
    fig, axes = plt.subplots(2, 2, figsize=(13.5, 8.6), dpi=160)
    fig.patch.set_facecolor("#0b1220")
    for ax in axes.flat:
        ax.set_facecolor("#111c30")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color("#334155")
        ax.spines["bottom"].set_color("#334155")
        ax.grid(axis="y", color="#1e293b", linestyle="--", linewidth=0.7, alpha=0.8)

    # Panel 1: Stacked stage breakdown across smoke / local / stress
    ax = axes[0, 0]
    labels = [f"{r['profile']}\n({r['raster_px']})" for r in profiles]
    stages = [
        ("Generate", [r["generate_sources_ms"] for r in profiles], "#38bdf8"),
        ("STAC+Bronze", [r["stac_bronze_ms"] for r in profiles], "#818cf8"),
        ("CRS Reproject", [r["crs_reprojection_ms"] for r in profiles], "#fbbf24"),
        ("Cube+Zonal", [r["cube_zonal_anomaly_ms"] for r in profiles], "#34d399"),
        ("DQ Gate", [r["dq_ms"] for r in profiles], "#fb7185"),
    ]
    bottom = np.zeros(len(profiles))
    x = np.arange(len(profiles))
    for s_label, vals, color in stages:
        arr = np.array(vals)
        ax.bar(x, arr, bottom=bottom, label=s_label, color=color, width=0.52)
        bottom += arr
    for idx, total in enumerate(bottom):
        ax.text(idx, total + max(bottom) * 0.025, f"{total:.0f} ms", ha="center", va="bottom", fontsize=9, fontweight="bold", color="#f8fafc")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=9)
    ax.set_ylabel("Wall Time (ms)", fontsize=9)
    ax.set_title("End-to-End Pipeline Stage Latency (22 Scenes × 6 Bands)", fontsize=10.5, fontweight="bold", pad=10)
    ax.set_ylim(0, max(bottom) * 1.18)
    ax.legend(fontsize=7.8, loc="upper left", frameon=False, ncol=2)

    # Panel 2: Dask chunk size ablation
    ax = axes[0, 1]
    c_labels = [f"{r['chunk_size']}px\n({r['dask_partitions']}p)" for r in dask_ablation]
    meds = [r["median_ms"] for r in dask_ablation]
    p95s = [r["p95_ms"] for r in dask_ablation]
    cx = np.arange(len(dask_ablation))
    bars = ax.bar(cx - 0.16, meds, width=0.32, label="Median (ms)", color="#34d399")
    ax.bar(cx + 0.16, p95s, width=0.32, label="p95 (ms)", color="#38bdf8", alpha=0.85)
    for bar, val in zip(bars, meds, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, val + max(p95s) * 0.02, f"{val:.1f}", ha="center", va="bottom", fontsize=8, color="#e2e8f0")
    ax.set_xticks(cx)
    ax.set_xticklabels(c_labels, fontsize=8.5)
    ax.set_ylabel("Cube + Mosaic + Zonal Time (ms)", fontsize=9)
    ax.set_title("Dask Spatial Chunk-Size Ablation (256×256×6 Cube)", fontsize=10.5, fontweight="bold", pad=10)
    ax.set_ylim(0, max(p95s) * 1.22)
    ax.legend(fontsize=8.2, frameon=False)

    # Panel 3: Cold vs Disk-Warm vs Memory-Warm PNG Map Tile Latency (log scale)
    ax = axes[1, 0]
    t_cats = ["Cold Render\n(MISS)", "Disk Cache\n(HIT)", "Memory Cache\n(HIT)"]
    t_p50 = [tile_bench["cold_p50_ms"], tile_bench["disk_warm_p50_ms"], tile_bench["mem_warm_p50_ms"]]
    t_p95 = [tile_bench["cold_p95_ms"], tile_bench["disk_warm_p95_ms"], tile_bench["mem_warm_p95_ms"]]
    tx = np.arange(len(t_cats))
    b1 = ax.bar(tx - 0.16, t_p50, width=0.32, label="p50 (ms)", color="#fb7185")
    ax.bar(tx + 0.16, t_p95, width=0.32, label="p95 (ms)", color="#fbbf24", alpha=0.85)
    ax.set_yscale("log")
    for bar, val in zip(b1, t_p50, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, val * 1.25, f"{val:.3f} ms", ha="center", va="bottom", fontsize=8.2, fontweight="bold", color="#f8fafc")
    ax.set_xticks(tx)
    ax.set_xticklabels(t_cats, fontsize=9)
    ax.set_ylabel("Latency (ms, log scale)", fontsize=9)
    ax.set_title(
        f"PNG Tile Cache Speedup ({tile_bench['mem_speedup_vs_cold_x']}× Memory / {tile_bench['disk_speedup_vs_cold_x']}× Disk)",
        fontsize=10.5,
        fontweight="bold",
        pad=10,
    )
    ax.set_ylim(min(t_p50) * 0.4, max(t_p95) * 4.0)
    ax.legend(fontsize=8.2, frameon=False)

    # Panel 4: Apache Iceberg V2 Compaction Before vs After
    ax = axes[1, 1]
    c_cats = [
        f"Before Compaction\n({compaction['files_before']} Parquet files)",
        f"After Compaction\n({compaction['files_after']} Parquet file)",
    ]
    c_p50 = [compaction["scan_p50_before_ms"], compaction["scan_p50_after_ms"]]
    c_p95 = [compaction["scan_p95_before_ms"], compaction["scan_p95_after_ms"]]
    ix = np.arange(len(c_cats))
    b_p50 = ax.bar(ix - 0.16, c_p50, width=0.32, label="Scan p50 (ms)", color="#a78bfa")
    ax.bar(ix + 0.16, c_p95, width=0.32, label="Scan p95 (ms)", color="#38bdf8", alpha=0.85)
    for bar, val in zip(b_p50, c_p50, strict=True):
        ax.text(bar.get_x() + bar.get_width() / 2, val + max(c_p95) * 0.03, f"{val:.2f} ms", ha="center", va="bottom", fontsize=9, fontweight="bold", color="#f8fafc")
    ax.set_xticks(ix)
    ax.set_xticklabels(c_cats, fontsize=9)
    ax.set_ylabel("Full Table Arrow Scan Latency (ms)", fontsize=9)
    ax.set_title(
        f"Apache Iceberg V2 Compaction ({compaction['scan_speedup_x']}× Faster Scan)",
        fontsize=10.5,
        fontweight="bold",
        pad=10,
    )
    ax.set_ylim(0, max(c_p95) * 1.25)
    ax.legend(fontsize=8.2, frameon=False)

    fig.suptitle(
        "Geospatial & Climate Intelligence Engine — Measured Local Benchmarks (Apple M4 Pro · Python 3.12 · GDAL/xarray/Dask/Iceberg V2)",
        fontsize=12,
        fontweight="bold",
        color="#f8fafc",
        y=0.98,
    )
    plt.tight_layout(rect=(0, 0, 1, 0.94))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor=fig.get_facecolor())
    plt.close(fig)


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="geoclimate_bench_") as tmp:
        work_root = Path(tmp)
        profiles = run_profile_benchmarks(work_root)
        dask_ablation = run_dask_chunk_ablation(work_root)
        tile_bench = run_tile_cache_benchmark(work_root)
        compaction = run_iceberg_compaction_benchmark(work_root)

    payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "hardware": {
            "os": f"{platform.system()} {platform.release()}",
            "arch": platform.machine(),
            "cpu_count": os.cpu_count(),
            "python_version": platform.python_version(),
        },
        "profiles": profiles,
        "dask_chunk_ablation": dask_ablation,
        "tile_cache": tile_bench,
        "iceberg_compaction": compaction,
    }

    json_path = RESULTS_DIR / "benchmark_summary.json"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Write CSV artifacts for clean spreadsheet/CI inspection
    with (RESULTS_DIR / "profiles.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(profiles[0].keys()))
        writer.writeheader()
        writer.writerows(profiles)

    with (RESULTS_DIR / "dask_chunk_ablation.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(dask_ablation[0].keys()))
        writer.writeheader()
        writer.writerows(dask_ablation)

    render_benchmark_chart(profiles, dask_ablation, tile_bench, compaction, CHART_PATH)
    print(json.dumps(payload, indent=2))
    print(f"\nSaved benchmark artifacts to {RESULTS_DIR} and chart to {CHART_PATH}")


if __name__ == "__main__":
    main()
