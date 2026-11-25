"""Performance smoke test guarding pipeline latency and warm tile cache serve speed."""

from __future__ import annotations

import statistics
import time
from pathlib import Path

from geo_climate.config import Settings
from processing.pipeline import run_full_pipeline
from processing.tile_renderer import ReproducibleTileServer


def test_smoke_pipeline_and_warm_tile_latency(tmp_path: Path) -> None:
    cfg = Settings(data_dir=tmp_path / "perf_smoke", seed=20260801)
    summary = run_full_pipeline(cfg, warm_tiles=True)
    assert summary["total_duration_ms"] < 15_000.0
    assert summary["dq_passed"] == summary["dq_total"]

    server = ReproducibleTileServer(cfg.tile_cache_dir, cfg.mosaics_dir)
    # Prime memory cache
    server.render_tile(
        layer="ndvi",
        region_id="california_central_valley",
        z=0,
        x=0,
        y=0,
        stac_snapshot="smoke-snap",
        scene_version=1,
    )
    latencies_ms: list[float] = []
    for _ in range(30):
        t0 = time.perf_counter()
        tile = server.render_tile(
            layer="ndvi",
            region_id="california_central_valley",
            z=0,
            x=0,
            y=0,
            stac_snapshot="smoke-snap",
            scene_version=1,
        )
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)
        assert tile.cache_status == "HIT"

    assert statistics.median(latencies_ms) < 5.0
