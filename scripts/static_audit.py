"""Static repository audit: verifies pinned container tags, schema JSON/YAML validity, and zero leaked secrets."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_TAG = ":" + "latest"
_GH_FINE_TOKEN_PREFIX = "github_" + "pat_"
SECRET_PATTERNS = (
    re.compile(rf"{_GH_FINE_TOKEN_PREFIX}[A-Za-z0-9_]+"),
    re.compile(r"ghp_[A-Za-z0-9]+"),
    re.compile(r"-----BEGIN (?:RSA|EC|OPENSSH) PRIVATE KEY-----"),
)
SKIP_DIRS = {".git", ".venv", ".local_data", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis"}


def run_static_audit() -> list[str]:
    errors: list[str] = []

    for path in PROJECT_ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue

        rel = path.relative_to(PROJECT_ROOT)
        suffix = path.suffix.lower()

        if suffix in {".png", ".jpg", ".tif", ".geoparquet", ".parquet", ".db", ".lock"}:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        if FORBIDDEN_TAG in text:
            errors.append(f"Unpinned tag reference found in {rel}")

        for pat in SECRET_PATTERNS:
            if pat.search(text):
                errors.append(f"Potential secret pattern matched in {rel}")

        if suffix in {".yaml", ".yml"}:
            try:
                list(yaml.safe_load_all(text))
            except Exception as exc:
                errors.append(f"Invalid YAML in {rel}: {exc}")

        if suffix in {".json", ".avsc"}:
            try:
                json.loads(text)
            except Exception as exc:
                errors.append(f"Invalid JSON in {rel}: {exc}")

    return errors


if __name__ == "__main__":
    issues = run_static_audit()
    if issues:
        for item in issues:
            print(f"[AUDIT FAIL] {item}")
        sys.exit(1)
    print("[AUDIT PASS] All YAML/JSON/Avro files valid, zero unpinned image tags, zero leaked secrets.")
