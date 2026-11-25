#!/usr/bin/env bash
set -euo pipefail

# Zero-Docker one-command runner for macOS ARM64 & Linux x86_64/ARM64
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

HOST="${GEO_CLIMATE_API_HOST:-127.0.0.1}"
PORT="${GEO_CLIMATE_API_PORT:-8070}"

echo "[run_portable] Checking Python 3.12 virtual environment..."
if command -v uv >/dev/null 2>&1; then
  uv sync --python 3.12 --extra dev
else
  python3.12 -m venv .venv
  .venv/bin/pip install -e ".[dev]"
fi

echo "[run_portable] Executing deterministic Bronze -> Silver -> Gold -> STAC -> Iceberg V2 -> Tile pipeline..."
.venv/bin/geo-climate run

echo "[run_portable] Starting FastAPI Spatial & Tile API + Control Tower at http://${HOST}:${PORT}"
echo "[run_portable] Press Ctrl+C to stop."
exec .venv/bin/geo-climate serve --host "${HOST}" --port "${PORT}"
