"""Hypothesis property-based tests for geospatial and scientific invariants."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ingestion.generator import NODATA_VALUE
from processing.crs_and_geometry import (
    roundtrip_crs_error_meters,
    split_antimeridian_bbox,
    validate_wgs84_bounds,
)
from processing.raster_cube import apply_qa_and_nodata_mask, compute_indices_from_masked


@settings(max_examples=60, deadline=None)
@given(
    red=st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False),
    green=st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False),
    nir=st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False),
    swir=st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False),
)
def test_property_ndvi_and_ndwi_strictly_bounded(
    red: float, green: float, nir: float, swir: float
) -> None:
    arr = np.array([[[red]], [[green]], [[nir]], [[swir]], [[298.0]]], dtype=np.float32)
    idx = compute_indices_from_masked(arr)
    ndvi = float(idx["ndvi"][0, 0])
    ndwi = float(idx["ndwi"][0, 0])
    mndwi = float(idx["mndwi"][0, 0])
    assert -1.0 <= ndvi <= 1.0
    assert -1.0 <= ndwi <= 1.0
    assert -1.0 <= mndwi <= 1.0


@settings(max_examples=50, deadline=None)
@given(
    lon_west=st.floats(min_value=165.0, max_value=179.8, allow_nan=False),
    lon_east=st.floats(min_value=-179.8, max_value=-165.0, allow_nan=False),
    lat_south=st.floats(min_value=-60.0, max_value=50.0, allow_nan=False),
    lat_span=st.floats(min_value=0.2, max_value=15.0, allow_nan=False),
)
def test_property_antimeridian_split_valid_and_area_preserving(
    lon_west: float,
    lon_east: float,
    lat_south: float,
    lat_span: float,
) -> None:
    lat_north = min(85.0, lat_south + lat_span)
    geom = split_antimeridian_bbox(lon_west, lat_south, lon_east, lat_north)
    ok, err = validate_wgs84_bounds(geom)
    assert ok, err
    expected_area = ((lon_east + 360.0) - lon_west) * (lat_north - lat_south)
    assert geom.area == pytest.approx(expected_area, rel=1e-5, abs=1e-5)


@settings(max_examples=40, deadline=None)
@given(
    clear_lst_k=st.floats(min_value=260.0, max_value=330.0, allow_nan=False),
    corrupt_cloud_lst_k=st.floats(min_value=190.0, max_value=360.0, allow_nan=False),
    corrupt_nir=st.floats(min_value=0.01, max_value=0.99, allow_nan=False),
)
def test_property_cloud_and_nodata_pixels_never_pollute_clear_mean(
    clear_lst_k: float,
    corrupt_cloud_lst_k: float,
    corrupt_nir: float,
) -> None:
    # 4x4 raster: top 2 rows clear, row 2 cloud (qa=1), row 3 nodata (-9999, qa=3)
    data = np.zeros((6, 4, 4), dtype=np.float32)
    data[0, :2, :] = 0.10
    data[1, :2, :] = 0.15
    data[2, :2, :] = 0.50
    data[3, :2, :] = 0.12
    data[4, :2, :] = clear_lst_k
    data[5, :2, :] = 0.0

    # Cloud row with arbitrary extreme values
    data[0, 2, :] = 0.90
    data[1, 2, :] = 0.90
    data[2, 2, :] = corrupt_nir
    data[3, 2, :] = 0.80
    data[4, 2, :] = corrupt_cloud_lst_k
    data[5, 2, :] = 1.0

    # Nodata row
    data[:5, 3, :] = NODATA_VALUE
    data[5, 3, :] = 3.0

    masked, _, valid_pct = apply_qa_and_nodata_mask(data)
    assert valid_pct == pytest.approx(50.0, abs=1e-4)
    idx = compute_indices_from_masked(masked)
    mean_lst_c = float(np.nanmean(idx["lst_c"]))
    expected_c = clear_lst_k - 273.15
    assert mean_lst_c == pytest.approx(expected_c, abs=1e-3)


@settings(max_examples=40, deadline=None)
@given(
    lon=st.floats(min_value=-125.0, max_value=-119.0, allow_nan=False),
    lat=st.floats(min_value=33.0, max_value=45.0, allow_nan=False),
)
def test_property_utm_roundtrip_error_bounded(lon: float, lat: float) -> None:
    err_m = roundtrip_crs_error_meters(lon, lat, 32610)
    assert err_m < 0.05


@settings(max_examples=40, deadline=None)
@given(
    lon=st.floats(min_value=-179.0, max_value=179.0, allow_nan=False),
    lat=st.floats(min_value=66.0, max_value=86.0, allow_nan=False),
)
def test_property_polar_stereographic_roundtrip_error_bounded(lon: float, lat: float) -> None:
    err_m = roundtrip_crs_error_meters(lon, lat, 3413)
    assert err_m < 0.05
