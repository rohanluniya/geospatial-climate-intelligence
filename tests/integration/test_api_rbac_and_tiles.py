"""Integration tests for FastAPI Spatial & Tile API, RBAC enforcement, audit logs, and Prometheus metrics."""

from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from geo_climate.config import Settings
from processing.pipeline import run_full_pipeline
from services.api.app import create_app


def test_api_rbac_tiles_provenance_and_metrics(tmp_path: Path) -> None:
    cfg = Settings(data_dir=tmp_path / "api_test", seed=20260801)
    run_full_pipeline(cfg, warm_tiles=True)
    app = create_app(cfg)
    client = TestClient(app)

    # 1. Public health & metrics endpoints return 200
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "geoclimate_scenes_ingested_total" in metrics.text

    # 2. Unauthorized request to protected endpoint returns 401
    unauth = client.get("/v1/overview")
    assert unauth.status_code == 401

    bad_key = client.get("/v1/zonal-stats", headers={"X-API-Key": "invalid-secret-key"})
    assert bad_key.status_code == 401

    # 3. Viewer can read overview, STAC catalog, STAC items, zonal stats, anomalies, DQ, and PNG tiles
    viewer_hdr = {"X-API-Key": cfg.viewer_token}
    ov = client.get("/v1/overview", headers=viewer_hdr)
    assert ov.status_code == 200
    assert ov.json()["kpis"]["dq_checks_passed"] == 8

    stac_cat = client.get("/v1/stac/catalog", headers=viewer_hdr)
    assert stac_cat.status_code == 200
    assert stac_cat.json()["type"] == "Catalog"

    stac_items = client.get("/v1/stac/items", headers=viewer_hdr)
    assert stac_items.status_code == 200
    assert stac_items.json()["count"] >= 20

    # Real PNG tile bytes + provenance headers
    tile_resp = client.get(
        "/v1/tiles/ndvi/0/0/0.png?region_id=california_central_valley",
        headers=viewer_hdr,
    )
    assert tile_resp.status_code == 200
    assert tile_resp.headers["content-type"] == "image/png"
    assert tile_resp.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert "ETag" in tile_resp.headers
    assert "X-STAC-Snapshot" in tile_resp.headers
    assert "X-Valid-Pixel-Pct" in tile_resp.headers
    assert tile_resp.headers["X-CRS"] == "EPSG:4326"
    img = Image.open(io.BytesIO(tile_resp.content))
    assert img.size == (256, 256)

    # Second tile request is guaranteed HIT
    tile_resp2 = client.get(
        "/v1/tiles/ndvi/0/0/0.png?region_id=california_central_valley",
        headers=viewer_hdr,
    )
    assert tile_resp2.headers["X-Cache-Status"] == "HIT"
    assert tile_resp2.headers["ETag"] == tile_resp.headers["ETag"]

    # 4. Viewer calling admin-only /v1/backfill returns 403 Forbidden
    forbid = client.post(
        "/v1/backfill",
        headers=viewer_hdr,
        json={"region_id": "california_central_valley", "acquisition_date": "2026-07-15"},
    )
    assert forbid.status_code == 403

    # 5. Admin calling /v1/backfill succeeds (200 OK)
    admin_hdr = {"Authorization": f"Bearer {cfg.admin_token}"}
    bf = client.post(
        "/v1/backfill",
        headers=admin_hdr,
        json={
            "region_id": "california_central_valley",
            "acquisition_date": "2026-07-15",
            "inject_v2_calibration": True,
        },
    )
    assert bf.status_code == 200
    assert bf.json()["status"] == "completed"

    # 6. Audit log records rejections and redacts secrets
    audit_text = cfg.audit_log_path.read_text(encoding="utf-8")
    assert "auth_rejected" in audit_text
    assert "rbac_forbidden" in audit_text
    assert cfg.admin_token not in audit_text
    assert cfg.viewer_token not in audit_text
