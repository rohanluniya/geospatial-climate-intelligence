"""Avro v1/v2 contract validation and STAC 1.0 contract verification."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, cast

import fastavro

from geo_climate.config import PROJECT_ROOT

SCHEMAS_DIR = PROJECT_ROOT / "schemas"


def load_avro_schema(name: str) -> dict[str, Any]:
    """Load and parse an Avro schema from schemas/avro/."""
    path = SCHEMAS_DIR / "avro" / name
    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return cast(dict[str, Any], fastavro.parse_schema(raw))


def validate_avro_record(record: dict[str, Any], schema_name: str) -> tuple[bool, str | None]:
    """Validate a single record against a named Avro schema."""
    schema = load_avro_schema(schema_name)
    try:
        fastavro.validation.validate(record, schema, strict=False)
        return True, None
    except Exception as exc:
        return False, str(exc)


def roundtrip_avro_evolution(
    records_v1: list[dict[str, Any]],
    writer_schema_name: str = "weather_observation_v1.avsc",
    reader_schema_name: str = "weather_observation_v2.avsc",
) -> list[dict[str, Any]]:
    """Serialize records with v1 writer schema and deserialize with v2 reader schema to prove compatibility."""
    writer_schema = load_avro_schema(writer_schema_name)
    reader_schema = load_avro_schema(reader_schema_name)
    buf = io.BytesIO()
    fastavro.writer(buf, writer_schema, records_v1)
    buf.seek(0)
    return [cast(dict[str, Any], rec) for rec in fastavro.reader(buf, reader_schema=reader_schema)]


def validate_stac_item_contract(item_dict: dict[str, Any]) -> list[str]:
    """Check required STAC 1.0.0 fields, CRS, cloud cover bounds, SHA-256, and license."""
    errors: list[str] = []
    if item_dict.get("type") != "Feature":
        errors.append("STAC item type must be 'Feature'")
    if item_dict.get("stac_version") not in ("1.0.0", "1.1.0"):
        errors.append(f"Expected stac_version 1.0.0 or 1.1.0, got {item_dict.get('stac_version')}")
    bbox = item_dict.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        errors.append("STAC bbox must be a 4-element list")
    else:
        minx, miny, maxx, maxy = bbox
        if not (-180.0 <= minx <= 180.0 and -180.0 <= maxx <= 180.0):
            errors.append(f"STAC bbox longitudes out of [-180, 180]: {bbox}")
        if not (-90.0 <= miny <= 90.0 and -90.0 <= maxy <= 90.0):
            errors.append(f"STAC bbox latitudes out of [-90, 90]: {bbox}")
    props = item_dict.get("properties", {})
    for req in ("datetime", "proj:epsg", "eo:cloud_cover", "license", "geoclimate:region_id", "geoclimate:sha256"):
        if req not in props or props[req] is None:
            errors.append(f"Missing required STAC property: {req}")
    cloud = props.get("eo:cloud_cover")
    if isinstance(cloud, (int, float)) and not (0.0 <= float(cloud) <= 100.0):
        errors.append(f"eo:cloud_cover out of [0, 100]: {cloud}")
    sha = props.get("geoclimate:sha256")
    if isinstance(sha, str) and len(sha) != 64:
        errors.append(f"Invalid SHA-256 hex length: {len(sha)}")
    assets = item_dict.get("assets", {})
    if "multispectral_cog" not in assets:
        errors.append("Missing required asset 'multispectral_cog'")
    return errors


def load_stac_contract_schema() -> dict[str, Any]:
    path: Path = SCHEMAS_DIR / "stac" / "stac_item_contract.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
