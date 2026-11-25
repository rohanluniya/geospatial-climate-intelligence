"""Command-line interface for running the Geospatial & Climate Intelligence Engine."""

from __future__ import annotations

import argparse
import json
import sys

import uvicorn

from geo_climate.config import Settings
from processing.pipeline import run_full_pipeline, run_incremental_backfill


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="geo-climate",
        description="Planet-Scale Geospatial & Climate Intelligence Engine CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run full Bronze -> Silver -> Gold -> DQ -> Tile pipeline")
    run_p.add_argument("--compact", action="store_true", help="Compact Iceberg V2 Gold tables after run")

    bf_p = sub.add_parser("backfill", help="Run idempotent incremental backfill for a region/date")
    bf_p.add_argument("--region", default="california_central_valley")
    bf_p.add_argument("--date", default="2026-07-15")
    bf_p.add_argument("--v2-calibration", action="store_true")

    serve_p = sub.add_parser("serve", help="Start the FastAPI Spatial & Tile server")
    serve_p.add_argument("--host", default=None)
    serve_p.add_argument("--port", type=int, default=None)
    serve_p.add_argument("--bootstrap", action="store_true", help="Run pipeline before serving if data missing")

    args = parser.parse_args(argv)
    settings = Settings()

    if args.command == "run":
        summary = run_full_pipeline(settings, warm_tiles=True, compact_tables=args.compact)
        print(json.dumps(summary, indent=2))
        return 0

    if args.command == "backfill":
        summary = run_incremental_backfill(
            settings,
            region_id=args.region,
            acquisition_date=args.date,
            inject_v2_calibration=args.v2_calibration,
        )
        print(json.dumps(summary, indent=2))
        return 0

    if args.command == "serve":
        if args.bootstrap and not (settings.stac_dir / "catalog.json").exists():
            run_full_pipeline(settings, warm_tiles=True)
        host = args.host or settings.api_host
        port = args.port or settings.api_port
        uvicorn.run("services.api.app:app", host=host, port=port, log_level="info")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
