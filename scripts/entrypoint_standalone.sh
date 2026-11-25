#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${GEO_CLIMATE_DATA_DIR:-/var/lib/geoclimate}"
mkdir -p "${DATA_DIR}"

if [ ! -f "${DATA_DIR}/stac/catalog.json" ]; then
  echo "[entrypoint] Bootstrapping geospatial STAC + Iceberg V2 lakehouse..."
  geo-climate run
fi

echo "[entrypoint] Starting Geospatial & Climate Intelligence API on ${GEO_CLIMATE_API_HOST:-0.0.0.0}:${GEO_CLIMATE_API_PORT:-8070}"
exec geo-climate serve --host "${GEO_CLIMATE_API_HOST:-0.0.0.0}" --port "${GEO_CLIMATE_API_PORT:-8070}"
