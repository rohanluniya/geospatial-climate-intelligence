"""Detect host hardware, Python geospatial wheels, and container runtime availability."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import sys
from typing import Any


def detect_environment() -> dict[str, Any]:
    packages = [
        "rasterio",
        "shapely",
        "pyproj",
        "xarray",
        "dask",
        "pystac",
        "pyiceberg",
        "duckdb",
        "pyarrow",
        "fastavro",
        "fastapi",
        "uvicorn",
        "pillow",
        "numpy",
        "scipy",
        "matplotlib",
    ]
    versions: dict[str, str | None] = {}
    for pkg in packages:
        try:
            versions[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            versions[pkg] = None

    gdal_ver = None
    try:
        import rasterio

        gdal_ver = rasterio.__gdal_version__
    except Exception:
        pass

    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python_version": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "gdal_version": gdal_ver,
        "tools": {
            "uv": shutil.which("uv") is not None,
            "docker": shutil.which("docker") is not None,
            "podman": shutil.which("podman") is not None,
            "java": shutil.which("java") is not None,
        },
        "packages": versions,
    }


if __name__ == "__main__":
    print(json.dumps(detect_environment(), indent=2))
