"""Contract tests for Avro v1->v2 backward compatibility and STAC 1.0/1.1 item contracts."""

from __future__ import annotations

from geo_climate.contracts import (
    load_stac_contract_schema,
    roundtrip_avro_evolution,
    validate_avro_record,
    validate_stac_item_contract,
)


def test_avro_v1_to_v2_backward_compatible_evolution() -> None:
    v1_record = {
        "observation_id": "obs-contract-01",
        "station_id": "STN-CA-01",
        "region_id": "california_central_valley",
        "basin_id": "california_central_valley__north_watershed",
        "event_time_utc": "2026-07-15T18:00:00Z",
        "ingest_time_utc": "2026-07-15T18:05:00Z",
        "latitude": 37.8,
        "longitude": -121.2,
        "air_temp_c": 34.5,
        "precip_mm": 0.0,
        "humidity_pct": 25.0,
        "event_type": "MEASUREMENT",
        "source_license": "CC-BY-4.0",
    }
    ok, err = validate_avro_record(v1_record, "weather_observation_v1.avsc")
    assert ok, err

    decoded_v2 = roundtrip_avro_evolution([v1_record])
    assert len(decoded_v2) == 1
    assert decoded_v2[0]["sensor_calibration_version"] == "v1.0-legacy"
    assert decoded_v2[0]["quality_flag"] == "UNVERIFIED"


def test_stac_item_contract_accepts_valid_and_rejects_invalid_items() -> None:
    schema = load_stac_contract_schema()
    assert "properties" in schema

    valid_item = {
        "type": "Feature",
        "stac_version": "1.1.0",
        "id": "ca_20260715_v1",
        "geometry": {"type": "Polygon", "coordinates": [[[-122, 36.5], [-119.5, 36.5], [-119.5, 39.0], [-122, 39.0], [-122, 36.5]]]},
        "bbox": [-122.0, 36.5, -119.5, 39.0],
        "properties": {
            "datetime": "2026-07-15T18:30:00Z",
            "proj:epsg": 32610,
            "eo:cloud_cover": 11.2,
            "license": "CC-BY-4.0",
            "geoclimate:region_id": "california_central_valley",
            "geoclimate:sha256": "a" * 64,
        },
        "assets": {"multispectral_cog": {"href": "scene.tif"}},
        "links": [],
    }
    assert validate_stac_item_contract(valid_item) == []

    bad_item = dict(valid_item)
    bad_item["bbox"] = [-200.0, 36.5, 195.0, 39.0]
    bad_props = dict(valid_item["properties"])
    bad_props["eo:cloud_cover"] = 140.0
    bad_props["geoclimate:sha256"] = "short"
    bad_item["properties"] = bad_props
    errors = validate_stac_item_contract(bad_item)
    assert len(errors) >= 3
