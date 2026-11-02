"""Native grids, built from file metadata and normalized so row 0 is the northern edge.

Three kinds cover every feed here: regular latitude/longitude (with 0..360 or -180..180 longitudes
and either scan direction), Lambert conformal (HRRR, NAM, NBM, RTMA) and the GOES ABI fixed grid.
Each comes out as a CRS plus an affine transform, which is all GDAL needs to warp it to Web
Mercator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from affine import Affine
from pyproj import CRS, Transformer

MERC_LAT = 85.0511


@dataclass
class GridSpec:
    kind: str
    crs: str
    transform: Affine
    shape: tuple[int, int]  # (rows, cols) after normalization
    signature: dict = field(default_factory=dict)  # what QC compares with the registry

    @property
    def res_m(self) -> float:
        """Approximate native spacing in meters, used to pick the COG zoom."""
        a = abs(self.transform.a)
        return a * 111_320.0 if self.kind == "latlon" else a


def latlon_from_keys(k: dict, values: np.ndarray) -> tuple[np.ndarray, GridSpec]:
    """k holds Ni, Nj, first-point lat/lon, increments and scan flags (ecCodes key names)."""
    ni, nj = int(k["Ni"]), int(k["Nj"])
    a = values.reshape(nj, ni)
    dlon, dlat = float(k["iDirectionIncrementInDegrees"]), float(k["jDirectionIncrementInDegrees"])
    lat1, lon1 = float(k["latitudeOfFirstGridPointInDegrees"]), float(k["longitudeOfFirstGridPointInDegrees"])
    if int(k.get("iScansNegatively", 0)):
        a = a[:, ::-1]
        lon1 = lon1 - (ni - 1) * dlon
    north = lat1
    if int(k.get("jScansPositively", 0)):
        a = a[::-1, :]
        north = lat1 + (nj - 1) * dlat
    sig = {"type": "latlon", "nx": ni, "ny": nj, "dlon": dlon, "dlat": dlat,
           "lon0": round(((lon1 + 180) % 360) - 180, 4), "lat_north": round(north, 4)}
    lon1 = ((lon1 + 180.0) % 360.0) - 180.0
    is_global = abs(ni * dlon - 360.0) < 1e-6
    if is_global:
        # Roll so the first column is the westernmost at -180, then repeat it at +180 so that
        # interpolation across the dateline has data on both sides instead of a one-column seam.
        lons = ((lon1 + np.arange(ni) * dlon + 180.0) % 360.0) - 180.0
        order = np.argsort(lons, kind="stable")
        a = a[:, order]
        lon_w = float(lons[order[0]])
        a = np.concatenate([a, a[:, :1]], axis=1)
    else:
        lon_w = lon1
    # Drop rows Web Mercator cannot show, which also keeps the poles out of the warp.
    lats = north - np.arange(a.shape[0]) * dlat
    keep = np.abs(lats) <= MERC_LAT + dlat
    a = a[keep]
    north = float(lats[keep][0])
    tr = Affine(dlon, 0.0, lon_w - dlon / 2.0, 0.0, -dlat, north + dlat / 2.0)
    return a, GridSpec("latlon", "EPSG:4326", tr, a.shape, sig)


def lambert_crs(latin1: float, latin2: float, lov: float, radius: float, lad: float | None = None) -> str:
    lov = ((lov + 180.0) % 360.0) - 180.0
    lat0 = latin1 if lad is None else lad
    return (f"+proj=lcc +lat_1={latin1} +lat_2={latin2} +lat_0={lat0} +lon_0={lov} "
            f"+x_0=0 +y_0=0 +R={radius} +units=m +no_defs")


def lambert_from_keys(k: dict, values: np.ndarray) -> tuple[np.ndarray, GridSpec]:
    nx, ny = int(k["Nx"]), int(k["Ny"])
    a = values.reshape(ny, nx)
    dx, dy = float(k["DxInMetres"]), float(k["DyInMetres"])
    crs = lambert_crs(float(k["Latin1InDegrees"]), float(k["Latin2InDegrees"]), float(k["LoVInDegrees"]),
                      float(k["radius"]), float(k.get("LaDInDegrees", k["Latin1InDegrees"])))
    lat1, lon1 = float(k["latitudeOfFirstGridPointInDegrees"]), float(k["longitudeOfFirstGridPointInDegrees"])
    x0, y0 = Transformer.from_crs(CRS.from_proj4(crs).geodetic_crs, CRS.from_proj4(crs), always_xy=True) \
        .transform(((lon1 + 180) % 360) - 180, lat1)
    if int(k.get("jScansPositively", 1)):
        # The first point is the south-west corner and rows run north; flip so row 0 is north.
        a = a[::-1, :]
        top = y0 + (ny - 1) * dy
    else:
        top = y0
    tr = Affine(dx, 0.0, x0 - dx / 2.0, 0.0, -dy, top + dy / 2.0)
    sig = {"type": "lambert", "nx": nx, "ny": ny, "dx": dx, "la1": round(lat1, 4),
           "lo1": round(lon1 % 360.0, 4), "lov": round(float(k["LoVInDegrees"]) % 360.0, 4)}
    return a, GridSpec("lambert", crs, tr, a.shape, sig)


def geos_crs(lon_0: float, h: float, a: float = 6378137.0, b: float = 6356752.31414, sweep: str = "x") -> str:
    return f"+proj=geos +h={h} +lon_0={lon_0} +sweep={sweep} +a={a} +b={b} +units=m +no_defs"


def geos_from_xy(x_rad: np.ndarray, y_rad: np.ndarray, proj: dict, values: np.ndarray) -> tuple[np.ndarray, GridSpec]:
    """GOES ABI fixed grid: x/y are scan angles in radians; meters = angle * perspective height."""
    h = float(proj["perspective_point_height"])
    xm, ym = np.asarray(x_rad, "float64") * h, np.asarray(y_rad, "float64") * h
    dx, dy = xm[1] - xm[0], ym[1] - ym[0]
    a = values
    if dy > 0:  # rows run south to north
        a, ym, dy = a[::-1, :], ym[::-1], -dy
    crs = geos_crs(float(proj["longitude_of_projection_origin"]), h, float(proj["semi_major_axis"]),
                   float(proj["semi_minor_axis"]), str(proj.get("sweep_angle_axis", "x")))
    tr = Affine(dx, 0.0, xm[0] - dx / 2.0, 0.0, dy, ym[0] - dy / 2.0)
    sig = {"type": "geos", "nx": int(a.shape[1]), "ny": int(a.shape[0]),
           "lon_0": float(proj["longitude_of_projection_origin"])}
    return a, GridSpec("geos", crs, tr, a.shape, sig)


def matches_registry(sig: dict, grid: dict) -> list[str]:
    """Differences between a decoded grid and the registry's grid, as readable strings."""
    diffs = []
    if sig.get("type") != grid["type"]:
        return [f"grid type {sig.get('type')} != {grid['type']}"]
    if grid["type"] == "latlon":
        checks = {"nx": grid["nx"], "ny": grid["ny"], "dlon": abs(grid["dlon"]), "dlat": abs(grid["dlat"])}
    elif grid["type"] == "lambert":
        checks = {"nx": grid["nx"], "ny": grid["ny"], "dx": grid["dx"], "la1": grid["la1"],
                  "lov": grid["lov"] % 360.0}
    else:
        checks = {"nx": grid["nx"], "ny": grid["ny"], "lon_0": grid["lon_0"]}
    for k, want in checks.items():
        got = sig.get(k)
        if got is None or not math.isclose(float(got), float(want), rel_tol=0, abs_tol=1e-3):
            diffs.append(f"{k} {got} != {want}")
    return diffs
