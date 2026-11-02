import numpy as np
import pytest
from pyproj import CRS, Transformer

from wxtiles.cog import cog_bytes, mercator_target, read_cog, to_mercator
from wxtiles.grids import geos_from_xy, lambert_from_keys, latlon_from_keys, matches_registry


def ll_keys(lon0=0.0, lat0=90.0, south_to_north=False, nx=360, ny=181, d=1.0):
    return {"Ni": nx, "Nj": ny, "iDirectionIncrementInDegrees": d, "jDirectionIncrementInDegrees": d,
            "latitudeOfFirstGridPointInDegrees": lat0, "longitudeOfFirstGridPointInDegrees": lon0,
            "iScansNegatively": 0, "jScansPositively": 1 if south_to_north else 0}


def test_global_0_to_360_is_rolled_to_minus_180_with_a_dateline_column():
    lon = np.arange(360.0)
    vals = np.tile(lon, (181, 1))  # value = longitude in 0..359
    a, g = latlon_from_keys(ll_keys(), vals.ravel())
    assert g.transform.c == pytest.approx(-180.5)
    assert a[0, 0] == 180.0  # first column is -180, which is 180 in the file
    assert a.shape[1] == 361 and np.array_equal(a[:, -1], a[:, 0])
    assert g.transform.f <= 86.6  # rows beyond Web Mercator's reach are dropped


def test_south_to_north_rows_are_flipped():
    lat = np.linspace(-90, 90, 181)
    vals = np.repeat(lat[:, None], 360, axis=1)
    a, g = latlon_from_keys(ll_keys(lon0=-180.0, lat0=-90.0, south_to_north=True), vals.ravel())
    assert a[0, 0] > 80 and a[-1, 0] < -80  # north is row 0 after normalization


def test_lambert_first_point_lands_where_the_file_says():
    k = {"Nx": 450, "Ny": 265, "DxInMetres": 12000, "DyInMetres": 12000, "Latin1InDegrees": 38.5,
         "Latin2InDegrees": 38.5, "LoVInDegrees": 262.5, "LaDInDegrees": 38.5, "radius": 6371229,
         "latitudeOfFirstGridPointInDegrees": 21.138123, "longitudeOfFirstGridPointInDegrees": 237.280472,
         "jScansPositively": 1}
    vals = np.repeat(np.arange(265.0)[:, None], 450, axis=1)  # value = row number in file order (south first)
    a, g = lambert_from_keys(k, vals.ravel())
    assert a[-1, 0] == 0.0 and a[0, 0] == 264.0
    crs = CRS.from_proj4(g.crs)
    x, y = g.transform @ (0.5, a.shape[0] - 0.5)  # center of the south-west pixel
    lon, lat = Transformer.from_crs(crs, crs.geodetic_crs, always_xy=True).transform(x, y)
    assert lat == pytest.approx(21.138123, abs=1e-4) and lon == pytest.approx(237.280472 - 360, abs=1e-4)


def test_geostationary_grid_from_scan_angles():
    x = np.linspace(-0.101332, 0.038612, 56)
    y = np.linspace(0.128212, 0.044268, 34)
    proj = {"perspective_point_height": 35786023.0, "longitude_of_projection_origin": -75.0,
            "semi_major_axis": 6378137.0, "semi_minor_axis": 6356752.31414, "sweep_angle_axis": "x"}
    a, g = geos_from_xy(x, y, proj, np.full((34, 56), 250.0))
    assert "+proj=geos" in g.crs and g.transform.e < 0
    merc, tr, z = to_mercator(a.astype("float32"), g)
    assert np.isfinite(merc).any()


def test_registry_grid_mismatch_is_reported():
    _, g = latlon_from_keys(ll_keys(), np.zeros(360 * 181))
    assert matches_registry(g.signature, {"type": "latlon", "nx": 360, "ny": 181, "dlon": 1.0, "dlat": -1.0}) == []
    diffs = matches_registry(g.signature, {"type": "latlon", "nx": 1440, "ny": 721, "dlon": 0.25, "dlat": -0.25})
    assert any("nx" in d for d in diffs)


def test_cogs_from_the_same_grid_share_a_tile_aligned_extent():
    _, g = latlon_from_keys(ll_keys(), np.zeros(360 * 181))
    tr, shape = mercator_target(g, 2)
    assert shape == (1024, 1024)
    assert tr.c == pytest.approx(-20037508.34, abs=1) and tr.f == pytest.approx(20037508.34, abs=1)


def test_missing_values_stay_missing_through_the_warp():
    vals = np.full((100, 200), 40.0, "float32")
    vals[:, 100:] = np.nan  # no radar coverage on the eastern half
    k = ll_keys(lon0=230.0, lat0=55.0, nx=200, ny=100, d=0.35)
    a, g = latlon_from_keys(k, vals.ravel())
    merc, tr, _ = to_mercator(a, g)
    finite = merc[np.isfinite(merc)]
    assert finite.size and np.allclose(finite, 40.0)  # no blending of 40 dBZ into NaN at the edge
    back, tr2 = read_cog(cog_bytes(merc, tr))
    assert tr2 == tr and np.array_equal(np.isnan(back), np.isnan(merc))
