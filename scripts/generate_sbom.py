"""Generate CycloneDX 1.5 JSON SBOM for installed Python dependencies."""

from __future__ import annotations

import importlib.metadata
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def generate_cyclonedx_sbom(output_path: Path) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for dist in sorted(importlib.metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = str(dist.metadata["Name"] or "unknown")
        version = str(dist.version)
        key = (name.lower(), version)
        if key in seen:
            continue
        seen.add(key)
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{name.lower().replace('_', '-')}@{version}",
            }
        )

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(UTC).isoformat(),
            "component": {
                "type": "application",
                "name": "geospatial-climate-intelligence",
                "version": "0.1.0",
                "licenses": [{"license": {"id": "Apache-2.0"}}],
            },
        },
        "components": components,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(sbom, indent=2), encoding="utf-8")
    return sbom


if __name__ == "__main__":
    out = PROJECT_ROOT / "results" / "sbom" / "sbom-cyclonedx.json"
    res = generate_cyclonedx_sbom(out)
    print(f"Generated CycloneDX 1.5 SBOM with {len(res['components'])} components -> {out}")
