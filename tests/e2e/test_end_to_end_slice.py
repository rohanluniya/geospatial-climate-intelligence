"""End-to-end vertical slice test from fresh workspace through STAC, xarray/Dask, Iceberg V2, and FastAPI."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from geo_climate.config import Settings
from processing.pipeline import run_full_pipeline
from services.api.app import create_app


def test_fresh_workspace_end_to_end_vertical_slice(tmp_path: Path) -> None:
    cfg = Settings(data_dir=tmp_path / "e2e_fresh", seed=20260801)
    summary = run_full_pipeline(cfg, warm_tiles=True, compact_tables=True)

    assert summary["dq_passed"] == summary["dq_total"] == 8
    assert summary["stac_summary"]["validated_items"] == 22
    assert summary["stac_summary"]["quarantined_items"] == 1
    assert summary["quarantined_total"] == 3
    assert summary["flagged_anomalies"] > 0
    assert summary["seasonal_confounding_fp_suppressed"] > 0
    assert cfg.traces_path.exists()
    assert len(cfg.traces_path.read_text(encoding="utf-8").splitlines()) >= 5

    app = create_app(cfg)
    client = TestClient(app)
    hdr = {"X-API-Key": cfg.viewer_token}

    # Dashboard HTML serves cleanly
    ui = client.get("/")
    assert ui.status_code == 200
    assert "Planet-Scale Geospatial &amp; Climate Intelligence Engine" in ui.text

    # All 4 regions serve real PNG tiles across all 4 layers
    for region_id in (
        "california_central_valley",
        "amazon_tapajos_basin",
        "fiji_pacific_atoll",
        "svalbard_arctic_fjord",
    ):
        for layer in ("ndvi", "lst", "flood", "qa"):
            r = client.get(f"/v1/tiles/{layer}/0/0/0.png?region_id={region_id}", headers=hdr)
            assert r.status_code == 200
            assert r.content.startswith(b"\x89PNG\r\n\x1a\n")

    # Anomalies endpoint confirms deseasonalized detection and confounding suppression
    anoms = client.get("/v1/anomalies", headers=hdr).json()
    assert anoms["flagged_count"] > 0
    assert anoms["seasonal_confounding_suppressed"] > 0
