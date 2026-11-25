"""Configuration, deterministic study regions, CRS definitions, and RBAC settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RegionSpec:
    """Definition of a geographic study region with CRS and climate baseline characteristics."""

    region_id: str
    display_name: str
    source_epsg: int
    canonical_epsg: int
    # Bounds in WGS84 (min_lon, min_lat, max_lon, max_lat).
    # For antimeridian-crossing regions, min_lon > max_lon (e.g., 179.0 -> -179.0) or lon > 180 before normalization.
    wgs84_bbox: tuple[float, float, float, float]
    antimeridian_crossing: bool = False
    polar_region: bool = False
    biome: str = "temperate"
    baseline_ndvi_mean: float = 0.45
    baseline_ndvi_amp: float = 0.18
    baseline_lst_k_mean: float = 296.0
    baseline_lst_k_amp: float = 10.0
    baseline_ndwi_mean: float = -0.10
    baseline_precip_mm: float = 12.0


DEFAULT_REGIONS: tuple[RegionSpec, ...] = (
    RegionSpec(
        region_id="california_central_valley",
        display_name="California Central Valley (UTM Zone 10N)",
        source_epsg=32610,  # Mixed CRS: UTM Zone 10N -> reprojected to canonical EPSG:4326
        canonical_epsg=4326,
        wgs84_bbox=(-122.0, 36.5, -119.5, 39.0),
        antimeridian_crossing=False,
        polar_region=False,
        biome="agricultural_mediterranean",
        baseline_ndvi_mean=0.42,
        baseline_ndvi_amp=0.22,
        baseline_lst_k_mean=301.5,
        baseline_lst_k_amp=12.5,
        baseline_ndwi_mean=-0.18,
        baseline_precip_mm=6.5,
    ),
    RegionSpec(
        region_id="amazon_tapajos_basin",
        display_name="Amazon Tapajos Basin (Tropical Canopy)",
        source_epsg=4326,
        canonical_epsg=4326,
        wgs84_bbox=(-57.0, -5.5, -54.5, -3.0),
        antimeridian_crossing=False,
        polar_region=False,
        biome="tropical_rainforest",
        baseline_ndvi_mean=0.76,
        baseline_ndvi_amp=0.06,
        baseline_lst_k_mean=299.0,
        baseline_lst_k_amp=3.5,
        baseline_ndwi_mean=0.14,
        baseline_precip_mm=42.0,
    ),
    RegionSpec(
        region_id="fiji_pacific_atoll",
        display_name="Fiji & Pacific Antimeridian Reefs (179E -> 179W)",
        source_epsg=4326,
        canonical_epsg=4326,
        wgs84_bbox=(179.0, -18.5, -179.0, -16.0),  # Explicitly crosses the 180th meridian!
        antimeridian_crossing=True,
        polar_region=False,
        biome="tropical_maritime",
        baseline_ndvi_mean=0.34,
        baseline_ndvi_amp=0.07,
        baseline_lst_k_mean=300.2,
        baseline_lst_k_amp=2.8,
        baseline_ndwi_mean=0.38,
        baseline_precip_mm=28.0,
    ),
    RegionSpec(
        region_id="svalbard_arctic_fjord",
        display_name="Svalbard Arctic Fjord (Polar Stereographic EPSG:3413)",
        source_epsg=3413,  # NSIDC Sea Ice Polar Stereographic North
        canonical_epsg=4326,
        wgs84_bbox=(12.0, 77.5, 18.0, 79.5),
        antimeridian_crossing=False,
        polar_region=True,
        biome="arctic_tundra_glacier",
        baseline_ndvi_mean=0.14,
        baseline_ndvi_amp=0.12,
        baseline_lst_k_mean=269.0,
        baseline_lst_k_amp=14.0,
        baseline_ndwi_mean=0.22,
        baseline_precip_mm=9.0,
    ),
)

REGION_BY_ID: dict[str, RegionSpec] = {r.region_id: r for r in DEFAULT_REGIONS}


@dataclass
class Settings:
    """Runtime settings for storage paths, deterministic seed, and RBAC tokens."""

    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("GEO_CLIMATE_DATA_DIR", str(PROJECT_ROOT / ".local_data"))
        ).resolve()
    )
    seed: int = field(default_factory=lambda: int(os.environ.get("GEO_CLIMATE_SEED", "20260801")))
    api_host: str = field(default_factory=lambda: os.environ.get("GEO_CLIMATE_API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: int(os.environ.get("GEO_CLIMATE_API_PORT", "8070")))
    raster_width: int = 96
    raster_height: int = 96
    dask_chunk_size: int = 48
    viewer_token: str = field(
        default_factory=lambda: os.environ.get("GEO_CLIMATE_VIEWER_TOKEN", "dev-viewer-token-geo08")
    )
    analyst_token: str = field(
        default_factory=lambda: os.environ.get("GEO_CLIMATE_ANALYST_TOKEN", "dev-analyst-token-geo08")
    )
    admin_token: str = field(
        default_factory=lambda: os.environ.get("GEO_CLIMATE_ADMIN_TOKEN", "dev-admin-token-geo08")
    )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def raw_scenes_dir(self) -> Path:
        return self.raw_dir / "scenes"

    @property
    def raw_weather_dir(self) -> Path:
        return self.raw_dir / "weather"

    @property
    def vectors_dir(self) -> Path:
        return self.data_dir / "vectors"

    @property
    def stac_dir(self) -> Path:
        return self.data_dir / "stac"

    @property
    def silver_rasters_dir(self) -> Path:
        return self.data_dir / "silver_rasters"

    @property
    def mosaics_dir(self) -> Path:
        return self.data_dir / "mosaics"

    @property
    def warehouse_dir(self) -> Path:
        return self.data_dir / "warehouse"

    @property
    def catalog_db_path(self) -> Path:
        return self.data_dir / "iceberg_catalog.db"

    @property
    def tile_cache_dir(self) -> Path:
        return self.data_dir / "tile_cache"

    @property
    def traces_path(self) -> Path:
        return self.data_dir / "otel_traces.jsonl"

    @property
    def audit_log_path(self) -> Path:
        return self.data_dir / "security_audit.jsonl"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.raw_scenes_dir,
            self.raw_weather_dir,
            self.vectors_dir,
            self.stac_dir,
            self.silver_rasters_dir,
            self.mosaics_dir,
            self.warehouse_dir,
            self.tile_cache_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def role_for_token(self, token: str | None) -> str | None:
        if not token:
            return None
        if token == self.admin_token:
            return "admin"
        if token == self.analyst_token:
            return "analyst"
        if token == self.viewer_token:
            return "viewer"
        return None
