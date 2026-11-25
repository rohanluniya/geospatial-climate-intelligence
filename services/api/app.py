"""FastAPI Spatial, STAC, Zonal Climate Analytics, and Reproducible PNG Map Tile Server."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from geo_climate.config import DEFAULT_REGIONS, PROJECT_ROOT, REGION_BY_ID, Settings
from geo_climate.logging_utils import get_logger, new_trace_id, record_security_audit, traced_span
from geo_climate.metrics import METRICS
from models.iceberg_store import IcebergLakehouseStore
from models.query_engine import ClimateQueryEngine
from processing.pipeline import run_full_pipeline, run_incremental_backfill
from processing.tile_renderer import VALID_LAYERS, ReproducibleTileServer

logger = get_logger("geo_climate.api")

ROLE_RANK = {"viewer": 1, "analyst": 2, "admin": 3}


class BackfillRequest(BaseModel):
    region_id: str = Field(default="california_central_valley")
    acquisition_date: str = Field(default="2026-07-15")
    inject_v2_calibration: bool = Field(default=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    cfg.ensure_dirs()
    store = IcebergLakehouseStore(cfg)
    query_engine = ClimateQueryEngine(store)
    tile_server = ReproducibleTileServer(cfg.tile_cache_dir, cfg.mosaics_dir)
    dashboard_path: Path = PROJECT_ROOT / "apps" / "climate-dashboard" / "index.html"

    app = FastAPI(
        title="Planet-Scale Geospatial & Climate Intelligence Engine",
        version="0.1.0",
        description=(
            "STAC 1.0/1.1 Catalog, Multi-CRS GeoTIFF/GeoParquet Lakehouse (Apache Iceberg V2), "
            "xarray/Dask Zonal Climate Analytics, Deseasonalized Anomalies, and Reproducible PNG Map Tiles."
        ),
    )
    app.state.settings = cfg
    app.state.store = store
    app.state.query_engine = query_engine
    app.state.tile_server = tile_server

    @app.middleware("http")
    async def observability_middleware(request: Request, call_next: Any) -> Response:
        trace_id = request.headers.get("X-Trace-Id") or new_trace_id()
        request.state.trace_id = trace_id
        t0 = time.perf_counter()
        response: Response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        response.headers["X-Trace-Id"] = trace_id
        response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
        METRICS.inc(
            "geoclimate_http_requests_total",
            path=request.url.path,
            method=request.method,
            status=str(response.status_code),
        )
        return response

    def _extract_token(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> str | None:
        if x_api_key:
            return x_api_key.strip()
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return None

    def require_role(min_role: str):
        def _dependency(
            request: Request,
            token: str | None = Depends(_extract_token),
        ) -> str:
            trace_id = getattr(request.state, "trace_id", new_trace_id())
            role = cfg.role_for_token(token)
            if role is None:
                METRICS.inc("geoclimate_auth_rejections_total", reason="unauthorized", endpoint=request.url.path)
                record_security_audit(
                    cfg.audit_log_path,
                    action="auth_rejected",
                    actor_role="anonymous",
                    endpoint=request.url.path,
                    status_code=401,
                    trace_id=trace_id,
                    detail="Missing or invalid API credentials",
                )
                raise HTTPException(status_code=401, detail="Unauthorized: valid Bearer token or X-API-Key required")

            if ROLE_RANK[role] < ROLE_RANK[min_role]:
                METRICS.inc("geoclimate_auth_rejections_total", reason="forbidden", endpoint=request.url.path)
                record_security_audit(
                    cfg.audit_log_path,
                    action="rbac_forbidden",
                    actor_role=role,
                    endpoint=request.url.path,
                    status_code=403,
                    trace_id=trace_id,
                    detail=f"Role '{role}' cannot access endpoint requiring '{min_role}'",
                )
                raise HTTPException(
                    status_code=403,
                    detail=f"Forbidden: role '{min_role}' required (authenticated as '{role}')",
                )

            record_security_audit(
                cfg.audit_log_path,
                action="authorized_access",
                actor_role=role,
                endpoint=request.url.path,
                status_code=200,
                trace_id=trace_id,
            )
            return role

        return _dependency

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def serve_dashboard() -> HTMLResponse:
        if dashboard_path.exists():
            return HTMLResponse(dashboard_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Geospatial & Climate Intelligence Engine</h1><p>Dashboard asset missing.</p>")

    @app.get("/health")
    def health() -> dict[str, Any]:
        tables = store.all_table_summaries()
        zonal_rows = next((t["row_count"] for t in tables if t["table"] == "gold.zonal_climate_stats"), 0)
        return {
            "status": "ok",
            "service": "geospatial-climate-intelligence",
            "timestamp_utc": datetime.now(UTC).isoformat(),
            "iceberg_tables_ready": len(tables),
            "gold_zonal_rows": zonal_rows,
            "stac_catalog_present": (cfg.stac_dir / "catalog.json").exists(),
        }

    @app.get("/metrics", response_class=PlainTextResponse)
    def prometheus_metrics() -> PlainTextResponse:
        return PlainTextResponse(METRICS.render_prometheus(), media_type="text/plain; version=0.0.4")

    @app.get("/v1/overview")
    def overview(
        request: Request,
        _role: str = Depends(require_role("viewer")),
    ) -> dict[str, Any]:
        trace_id = getattr(request.state, "trace_id", new_trace_id())
        with traced_span("api.overview", cfg.traces_path, trace_id):
            summary_path = cfg.data_dir / "latest_run_summary.json"
            run_meta = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
            region_summary = query_engine.region_summary()
            dq_rows = store.read_rows("quality.dq_results")
            quarantine_rows = store.read_rows("bronze.quarantine_dlq")
            stac_rows = store.read_rows("bronze.stac_assets")
            tables = store.all_table_summaries()
            return {
                "service": "geospatial-climate-intelligence",
                "generated_at_utc": datetime.now(UTC).isoformat(),
                "freshness_utc": run_meta.get("completed_at_utc"),
                "latest_run_id": run_meta.get("run_id"),
                "stac_snapshot_id": store.latest_snapshot_id("bronze.stac_assets"),
                "gold_snapshot_id": store.latest_snapshot_id("gold.zonal_climate_stats"),
                "kpis": {
                    "regions_monitored": len(DEFAULT_REGIONS),
                    "stac_validated_scenes": len(stac_rows),
                    "quarantined_dlq_records": len(quarantine_rows),
                    "dq_checks_passed": sum(1 for r in dq_rows if r["passed"]),
                    "dq_checks_total": len(dq_rows),
                    "zonal_stats_rows": next(
                        (t["row_count"] for t in tables if t["table"] == "gold.zonal_climate_stats"), 0
                    ),
                    "flagged_anomalies": run_meta.get("flagged_anomalies", 0),
                    "seasonal_fp_suppressed": run_meta.get("seasonal_confounding_fp_suppressed", 0),
                    "warehouse_disk_kb": round(store.warehouse_disk_bytes() / 1024.0, 1),
                },
                "regions": [
                    {
                        "region_id": r.region_id,
                        "display_name": r.display_name,
                        "source_epsg": r.source_epsg,
                        "canonical_epsg": r.canonical_epsg,
                        "antimeridian_crossing": r.antimeridian_crossing,
                        "polar_region": r.polar_region,
                        "biome": r.biome,
                    }
                    for r in DEFAULT_REGIONS
                ],
                "region_summary": region_summary,
                "mosaics": run_meta.get("mosaics", []),
                "tile_cache": tile_server.stats(),
                "iceberg_tables": tables,
            }

    @app.get("/v1/stac/catalog")
    def get_stac_catalog(_role: str = Depends(require_role("viewer"))) -> dict[str, Any]:
        cat_path = cfg.stac_dir / "catalog.json"
        if not cat_path.exists():
            raise HTTPException(status_code=404, detail="STAC catalog not generated yet")
        return json.loads(cat_path.read_text(encoding="utf-8"))

    @app.get("/v1/stac/items")
    def list_stac_items(
        region_id: str | None = Query(default=None),
        max_cloud_pct: float | None = Query(default=None, ge=0.0, le=100.0),
        source_epsg: int | None = Query(default=None),
        _role: str = Depends(require_role("viewer")),
    ) -> dict[str, Any]:
        rows = store.read_rows("bronze.stac_assets")
        filtered = []
        for r in rows:
            if region_id and r["region_id"] != region_id:
                continue
            if max_cloud_pct is not None and float(r["cloud_cover_pct"]) > max_cloud_pct:
                continue
            if source_epsg is not None and int(r["source_epsg"]) != source_epsg:
                continue
            filtered.append(
                {
                    "scene_id": r["scene_id"],
                    "collection_id": r["collection_id"],
                    "region_id": r["region_id"],
                    "acquisition_date": r["acquisition_date"],
                    "acquisition_time_utc": r["acquisition_time_utc"],
                    "source_epsg": r["source_epsg"],
                    "cloud_cover_pct": r["cloud_cover_pct"],
                    "nodata_pct": r["nodata_pct"],
                    "sha256": r["sha256"],
                    "scene_version": r["scene_version"],
                    "license": r["license"],
                    "antimeridian_crossing": r["antimeridian_crossing"],
                    "polar_region": r["polar_region"],
                    "bbox": json.loads(r["bbox_json"]),
                }
            )
        filtered.sort(key=lambda x: (x["acquisition_date"], x["region_id"], x["scene_id"]), reverse=True)
        return {
            "stac_snapshot_id": store.latest_snapshot_id("bronze.stac_assets"),
            "count": len(filtered),
            "items": filtered,
        }

    @app.get("/v1/zonal-stats")
    def get_zonal_stats(
        region_id: str | None = Query(default=None),
        acquisition_date: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
        _role: str = Depends(require_role("viewer")),
    ) -> dict[str, Any]:
        rows = query_engine.zonal_stats(region_id=region_id, acquisition_date=acquisition_date, limit=limit)
        return {
            "gold_snapshot_id": store.latest_snapshot_id("gold.zonal_climate_stats"),
            "count": len(rows),
            "rows": rows,
        }

    @app.get("/v1/anomalies")
    def get_anomalies(
        only_flagged: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=200),
        _role: str = Depends(require_role("viewer")),
    ) -> dict[str, Any]:
        rows = query_engine.top_anomalies(limit=limit, only_flagged=only_flagged)
        return {
            "gold_snapshot_id": store.latest_snapshot_id("gold.temporal_anomalies"),
            "count": len(rows),
            "flagged_count": sum(1 for r in rows if r["anomaly_type"] != "NORMAL"),
            "seasonal_confounding_suppressed": sum(1 for r in rows if r["naive_seasonal_confounding_fp"]),
            "rows": rows,
        }

    @app.get("/v1/tiles/stats")
    def get_tile_stats(_role: str = Depends(require_role("viewer"))) -> dict[str, Any]:
        audit_rows = store.read_rows("gold.tile_cache_audit")
        return {
            "live_stats": tile_server.stats(),
            "persisted_audit_count": len(audit_rows),
            "recent_audit": audit_rows[-20:],
        }

    @app.get("/v1/tiles/{layer}/{z}/{x}/{y}.png")
    def get_map_tile(
        request: Request,
        layer: str,
        z: int,
        x: int,
        y: int,
        region_id: str = Query(default="california_central_valley"),
        _role: str = Depends(require_role("viewer")),
    ) -> Response:
        if layer not in VALID_LAYERS:
            raise HTTPException(status_code=400, detail=f"Invalid layer '{layer}'. Valid: {VALID_LAYERS}")
        if region_id not in REGION_BY_ID:
            raise HTTPException(status_code=404, detail=f"Unknown region_id '{region_id}'")
        if z < 0 or z > 6 or x < 0 or y < 0:
            raise HTTPException(status_code=400, detail="Tile coordinates out of supported zoom range (0..6)")

        stac_snap = store.latest_snapshot_id("bronze.stac_assets")
        # Determine max scene_version for this region so new scene versions auto-bust cache keys
        stac_rows = store.read_rows("bronze.stac_assets")
        versions = [int(r["scene_version"]) for r in stac_rows if r["region_id"] == region_id]
        scene_version = max(versions) if versions else 1

        trace_id = getattr(request.state, "trace_id", new_trace_id())
        with traced_span(
            "api.render_tile",
            cfg.traces_path,
            trace_id,
            {"layer": layer, "region_id": region_id, "z": z, "x": x, "y": y},
        ):
            try:
                tile = tile_server.render_tile(
                    layer=layer,
                    region_id=region_id,
                    z=z,
                    x=x,
                    y=y,
                    stac_snapshot=stac_snap,
                    scene_version=scene_version,
                )
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

        headers = {
            "ETag": tile.etag,
            "Cache-Control": "public, max-age=300",
            "X-STAC-Snapshot": tile.stac_snapshot,
            "X-Valid-Pixel-Pct": str(tile.valid_pixel_pct),
            "X-CRS": tile.crs,
            "X-Cache-Status": tile.cache_status,
            "X-Scene-Version": str(tile.scene_version),
            "X-Render-Ms": str(tile.render_ms),
            "X-Region-Id": tile.region_id,
        }
        return Response(content=tile.png_bytes, media_type="image/png", headers=headers)

    @app.get("/v1/dq")
    def get_dq_and_quarantine(_role: str = Depends(require_role("viewer"))) -> dict[str, Any]:
        dq_rows = store.read_rows("quality.dq_results")
        dlq_rows = store.read_rows("bronze.quarantine_dlq")
        return {
            "passed": sum(1 for r in dq_rows if r["passed"]),
            "total": len(dq_rows),
            "checks": dq_rows,
            "quarantine_count": len(dlq_rows),
            "quarantine_records": dlq_rows,
        }

    @app.get("/v1/snapshots")
    def get_iceberg_snapshots(
        table: str = Query(default="gold.zonal_climate_stats"),
        _role: str = Depends(require_role("viewer")),
    ) -> dict[str, Any]:
        try:
            history = store.snapshot_history(table)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Table '{table}' not found: {exc}") from exc
        return {"table": table, "snapshot_count": len(history), "snapshots": history}

    @app.post("/v1/backfill")
    def trigger_backfill(
        req: BackfillRequest,
        request: Request,
        role: str = Depends(require_role("admin")),
    ) -> dict[str, Any]:
        if req.region_id not in REGION_BY_ID:
            raise HTTPException(status_code=400, detail=f"Unknown region_id '{req.region_id}'")
        trace_id = getattr(request.state, "trace_id", new_trace_id())
        with traced_span("api.backfill", cfg.traces_path, trace_id, req.model_dump()):
            tile_server.invalidate_region(req.region_id)
            summary = run_incremental_backfill(
                cfg,
                region_id=req.region_id,
                acquisition_date=req.acquisition_date,
                inject_v2_calibration=req.inject_v2_calibration,
            )
            record_security_audit(
                cfg.audit_log_path,
                action="backfill_executed",
                actor_role=role,
                endpoint="/v1/backfill",
                status_code=200,
                trace_id=trace_id,
                detail=f"region={req.region_id}, date={req.acquisition_date}, v2={req.inject_v2_calibration}",
            )
            return {
                "status": "completed",
                "run_id": summary["run_id"],
                "backfill": summary["backfill"],
                "zonal_stats_rows": summary["zonal_stats_rows"],
                "dq_passed": summary["dq_passed"],
                "dq_total": summary["dq_total"],
            }

    return app


def ensure_data_bootstrapped(settings: Settings | None = None) -> None:
    cfg = settings or Settings()
    if not (cfg.stac_dir / "catalog.json").exists():
        run_full_pipeline(cfg, warm_tiles=True)


app = create_app()
