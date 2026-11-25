"""Unit tests for CRS reprojection, antimeridian splitting, polar bounds, and GeoParquet I/O."""

from __future__ import annotations

from pathlib import Path

import pytest
from shapely.geometry import MultiPolygon, box

from processing.crs_and_geometry import (
    compute_projected_bounds,
    geometry_to_unwrapped_pacific,
    read_geoparquet,
    roundtrip_crs_error_meters,
    split_antimeridian_bbox,
    validate_wgs84_bounds,
    write_geoparquet,
)


def test_antimeridian_split_produces_valid_multipolygon_and_conserves_area() -> None:
    geom = split_antimeridian_bbox(179.0, -18.5, -179.0, -16.0)
    assert isinstance(geom, MultiPolygon)
    assert len(geom.geoms) == 2
    ok, err = validate_wgs84_bounds(geom)
    assert ok, err
    assert geom.area == pytest.approx(5.0, abs=1e-6)

    # Unwrapped Pacific shift brings both halves seamlessly into [179.0, 181.0]
    unwrapped = geometry_to_unwrapped_pacific(geom)
    minx, miny, maxx, maxy = unwrapped.bounds
    assert minx == pytest.approx(179.0, abs=1e-6)
    assert maxx == pytest.approx(181.0, abs=1e-6)
    assert miny == pytest.approx(-18.5, abs=1e-6)
    assert maxy == pytest.approx(-16.0, abs=1e-6)


@pytest.mark.parametrize(
    ("lon", "lat", "epsg"),
    [
        (-121.0, 37.8, 32610),  # California UTM Zone 10N
        (-55.5, -4.2, 3857),    # Amazon Web Mercator
        (15.2, 78.4, 3413),     # Svalbard Polar Stereographic North
    ],
)
def test_crs_roundtrip_projection_accuracy(lon: float, lat: float, epsg: int) -> None:
    err_m = roundtrip_crs_error_meters(lon, lat, epsg)
    assert err_m < 0.01


def test_polar_projected_bounds_non_degenerate() -> None:
    minx, miny, maxx, maxy = compute_projected_bounds((12.0, 77.5, 18.0, 79.5), target_epsg=3413)
    assert maxx > minx + 50_000.0
    assert maxy > miny + 50_000.0


def test_geoparquet_write_and_read_metadata_roundtrip(tmp_path: Path) -> None:
    out_path = tmp_path / "basins.geoparquet"
    fiji_poly = split_antimeridian_bbox(179.2, -18.0, -179.2, -16.5)
    ca_poly = box(-122.0, 36.5, -119.5, 39.0)

    records = [
        {"basin_id": "fiji_01", "region_id": "fiji_pacific_atoll", "geometry": fiji_poly},
        {"basin_id": "ca_01", "region_id": "california_central_valley", "geometry": ca_poly},
    ]
    written_meta = write_geoparquet(records, out_path)
    assert written_meta["version"] == "1.1.0"
    assert written_meta["columns"]["geometry"]["encoding"] == "WKB"

    loaded_rows, loaded_meta = read_geoparquet(out_path)
    assert len(loaded_rows) == 2
    assert loaded_meta["primary_column"] == "geometry"
    for row in loaded_rows:
        ok, err = validate_wgs84_bounds(row["geometry"])
        assert ok, err
