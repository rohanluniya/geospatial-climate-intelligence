#!/usr/bin/env bash
set -euo pipefail

# Capture headless Chrome screenshots of the live Geospatial & Climate Control Tower
# Uses the mandatory /tmp/chrome_screenshot_lock concurrency guard.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${1:-http://127.0.0.1:8070/}"
OUT_DIR="${ROOT_DIR}/docs/assets"
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
mkdir -p "${OUT_DIR}"

while ! mkdir /tmp/chrome_screenshot_lock 2>/dev/null; do
  sleep 1
done
trap 'rmdir /tmp/chrome_screenshot_lock 2>/dev/null || true' EXIT

"${CHROME_BIN}" \
  --headless=new \
  --disable-gpu \
  --no-sandbox \
  --hide-scrollbars \
  --virtual-time-budget=8000 \
  --window-size=1440,980 \
  --screenshot="${OUT_DIR}/climate_control_tower_top.png" \
  "${URL}"

sleep 1.5

"${CHROME_BIN}" \
  --headless=new \
  --disable-gpu \
  --no-sandbox \
  --hide-scrollbars \
  --virtual-time-budget=8000 \
  --window-size=1440,2550 \
  --screenshot="${OUT_DIR}/climate_control_tower_full.png" \
  "${URL}"

echo "[screenshots] Saved ${OUT_DIR}/climate_control_tower_top.png and climate_control_tower_full.png"
