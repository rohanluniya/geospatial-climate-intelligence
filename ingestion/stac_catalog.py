"""STAC Catalog, Collection, and Item registration with SHA-256 checksum & provenance validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pystac
import rasterio

from geo_climate.config import DEFAULT_REGIONS, REGION_BY_ID, Settings
from geo_climate.contracts import validate_stac_item_contract
from geo_climate.metrics import METRICS
from ingestion.generator import SceneManifestEntry, sha256_file
from processing.crs_and_geometry import geom_to_geojson_and_bbox, split_antimeridian_bbox


@dataclass(frozen=True)
class StacIngestionResult:
    """Outcome of STAC registration including validated items, deduplicated skips, and quarantined defects."""

    catalog_path: Path
    validated_items: list[dict[str, Any]]
    quarantined_records: list[dict[str, Any]]
    duplicate_count: int
    catalog_summary: dict[str, Any]


def build_and_validate_stac_catalog(
    settings: Settings,
    manifest: list[SceneManifestEntry],
) -> StacIngestionResult:
    """Validate GeoTIFF checksums & readability, build a genuine pystac Catalog/Collections/Items, and quarantine corrupt assets."""
    settings.ensure_dirs()

    catalog = pystac.Catalog(
        id="geospatial-climate-intelligence-stac",
        description=(
            "Versioned SpatioTemporal Asset Catalog (STAC) for multi-CRS satellite scenes "
            "(UTM EPSG:32610, WGS84 EPSG:4326, Polar Stereographic EPSG:3413, and antimeridian Pacific)."
        ),
        title="Planet-Scale Geospatial & Climate Intelligence STAC Catalog",
    )

    collections: dict[str, pystac.Collection] = {}
    for region in DEFAULT_REGIONS:
        geom = split_antimeridian_bbox(*region.wgs84_bbox)
        _, bbox = geom_to_geojson_and_bbox(geom)
        col = pystac.Collection(
            id=f"collection-{region.region_id}",
            title=region.display_name,
            description=f"Multi-band synthetic satellite reflectance and thermal LST scenes for {region.display_name}.",
            extent=pystac.Extent(
                spatial=pystac.SpatialExtent([bbox]),
                temporal=pystac.TemporalExtent(
                    [[datetime(2025, 10, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC)]]
                ),
            ),
            license="CC-BY-4.0",
        )
        collections[region.region_id] = col
        catalog.add_child(col)

    seen_scene_keys: set[tuple[str, str]] = set()
    validated_items: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    duplicate_count = 0
    ingest_now = datetime.now(UTC).isoformat()

    for entry in manifest:
        scene_key = (entry.scene_id, entry.expected_sha256)
        if scene_key in seen_scene_keys:
            duplicate_count += 1
            METRICS.inc("geoclimate_scenes_ingested_total", status="duplicate_skipped", region=entry.region_id)
            continue

        asset_file = Path(entry.asset_path)
        if not asset_file.exists():
            quarantined.append(
                {
                    "record_id": entry.scene_id,
                    "source_type": "stac_scene",
                    "region_id": entry.region_id,
                    "reason_code": "MISSING_ASSET",
                    "detail": f"Asset file does not exist: {entry.asset_path}",
                    "quarantined_at_utc": ingest_now,
                    "payload_json": json.dumps(entry.__dict__),
                }
            )
            METRICS.inc("geoclimate_quarantined_records_total", source_type="stac_scene", reason="MISSING_ASSET")
            continue

        computed_sha = sha256_file(asset_file)
        if computed_sha != entry.expected_sha256:
            quarantined.append(
                {
                    "record_id": entry.scene_id,
                    "source_type": "stac_scene",
                    "region_id": entry.region_id,
                    "reason_code": "SHA256_CHECKSUM_MISMATCH",
                    "detail": f"Expected {entry.expected_sha256[:12]}... got {computed_sha[:12]}...",
                    "quarantined_at_utc": ingest_now,
                    "payload_json": json.dumps(entry.__dict__),
                }
            )
            METRICS.inc(
                "geoclimate_quarantined_records_total",
                source_type="stac_scene",
                reason="SHA256_CHECKSUM_MISMATCH",
            )
            continue

        # Verify actual GDAL/rasterio readability & band count
        try:
            with rasterio.open(asset_file) as ds:
                if ds.count < 6:
                    raise ValueError(f"Expected 6 bands, found {ds.count}")
                epsg_code = ds.crs.to_epsg() if ds.crs else None
                if epsg_code is None:
                    raise ValueError("Missing CRS EPSG metadata in GeoTIFF")
                width, height = ds.width, ds.height
        except Exception as exc:
            quarantined.append(
                {
                    "record_id": entry.scene_id,
                    "source_type": "stac_scene",
                    "region_id": entry.region_id,
                    "reason_code": "CORRUPT_GEOTIFF",
                    "detail": str(exc),
                    "quarantined_at_utc": ingest_now,
                    "payload_json": json.dumps(entry.__dict__),
                }
            )
            METRICS.inc("geoclimate_quarantined_records_total", source_type="stac_scene", reason="CORRUPT_GEOTIFF")
            continue

        region = REGION_BY_ID[entry.region_id]
        geom = split_antimeridian_bbox(*region.wgs84_bbox)
        geojson_geom, bbox = geom_to_geojson_and_bbox(geom)

        acq_dt = datetime.fromisoformat(entry.acquisition_time_utc.replace("Z", "+00:00"))
        item = pystac.Item(
            id=entry.scene_id,
            geometry=geojson_geom,
            bbox=bbox,
            datetime=acq_dt,
            properties={
                "proj:epsg": int(epsg_code),
                "proj:shape": [int(height), int(width)],
                "eo:cloud_cover": float(entry.cloud_cover_pct),
                "license": entry.license,
                "geoclimate:region_id": entry.region_id,
                "geoclimate:sha256": computed_sha,
                "geoclimate:nodata_pct": float(entry.nodata_pct),
                "geoclimate:scene_version": int(entry.scene_version),
                "geoclimate:antimeridian_crossing": bool(entry.antimeridian_crossing),
                "geoclimate:polar_region": bool(entry.polar_region),
                "geoclimate:ingest_time_utc": ingest_now,
            },
        )
        item.add_asset(
            "multispectral_cog",
            pystac.Asset(
                href=str(asset_file),
                media_type=pystac.MediaType.COG,
                roles=["data"],
                title="6-Band Multispectral + Thermal LST + QA GeoTIFF",
                extra_fields={
                    "file:checksum": computed_sha,
                    "file:size": asset_file.stat().st_size,
                },
            ),
        )

        item_dict = item.to_dict()
        contract_errors = validate_stac_item_contract(item_dict)
        if contract_errors:
            quarantined.append(
                {
                    "record_id": entry.scene_id,
                    "source_type": "stac_scene",
                    "region_id": entry.region_id,
                    "reason_code": "STAC_CONTRACT_VIOLATION",
                    "detail": "; ".join(contract_errors),
                    "quarantined_at_utc": ingest_now,
                    "payload_json": json.dumps(entry.__dict__),
                }
            )
            METRICS.inc(
                "geoclimate_quarantined_records_total",
                source_type="stac_scene",
                reason="STAC_CONTRACT_VIOLATION",
            )
            continue

        collections[entry.region_id].add_item(item)
        seen_scene_keys.add(scene_key)
        validated_items.append(
            {
                "scene_id": entry.scene_id,
                "collection_id": f"collection-{entry.region_id}",
                "region_id": entry.region_id,
                "acquisition_date": entry.acquisition_date,
                "acquisition_time_utc": entry.acquisition_time_utc,
                "ingest_time_utc": ingest_now,
                "source_epsg": int(epsg_code),
                "cloud_cover_pct": float(entry.cloud_cover_pct),
                "nodata_pct": float(entry.nodata_pct),
                "asset_uri": str(asset_file),
                "sha256": computed_sha,
                "scene_version": int(entry.scene_version),
                "license": entry.license,
                "antimeridian_crossing": bool(entry.antimeridian_crossing),
                "polar_region": bool(entry.polar_region),
                "bbox_json": json.dumps(bbox),
                "stac_item_json": json.dumps(item_dict),
            }
        )
        METRICS.inc("geoclimate_scenes_ingested_total", status="validated", region=entry.region_id)

    catalog.normalize_hrefs(str(settings.stac_dir))
    catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
    catalog_path = settings.stac_dir / "catalog.json"

    summary = {
        "catalog_id": catalog.id,
        "stac_version": pystac.get_stac_version(),
        "collections": len(collections),
        "validated_items": len(validated_items),
        "quarantined_items": len(quarantined),
        "duplicates_skipped": duplicate_count,
        "crs_epsg_codes": sorted({int(i["source_epsg"]) for i in validated_items}),
    }
    return StacIngestionResult(
        catalog_path=catalog_path,
        validated_items=validated_items,
        quarantined_records=quarantined,
        duplicate_count=duplicate_count,
        catalog_summary=summary,
    )
