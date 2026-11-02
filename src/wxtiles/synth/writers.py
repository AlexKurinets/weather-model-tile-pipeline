"""Writes synthetic upstream files in each feed's real format: multi-message GRIB2 with a wgrib2
.idx or an ECMWF .index, bzip2 per-variable GRIB2 (DWD), per-variable GRIB2 (MSC), gzipped MRMS
GRIB2 on MRMS's local discipline 209, and GOES ABI L2 netCDF with scaled int16 values.

Files carry filler messages (upper-air fields nobody maps) so that byte-range subsetting has
something to skip. Filler is encoded once per grid and cloned with new header keys per file, which
keeps seeding fast; its values are meaningless.
"""

from __future__ import annotations

import bz2
import gzip
import json
import os
import tempfile
import zlib
from datetime import datetime, timedelta
from functools import lru_cache

import eccodes as ec
import numpy as np
from pyproj import CRS, Transformer

from wxtiles.fields import FIELDS
from wxtiles.grids import geos_crs, lambert_crs
from wxtiles.registry import Source, Step
from wxtiles.synth.atmosphere import View, accumulate, sample, texture

CENTERS = {"NCEP": 7, "ECMWF": 98, "DWD": 78, "MSC": 54, "in-house": 7, "NESDIS": 160}

# wgrib2 names and ECMWF params for the raw fields the writer emits.
NAMES = {
    "t2m": ("TMP", "2 m above ground", "2t", "sfc"),
    "mslp": ("PRMSL", "mean sea level", "msl", "sfc"),
    "qpf": ("APCP", "surface", "tp", "sfc"),
    "u10": ("UGRD", "10 m above ground", "10u", "sfc"),
    "v10": ("VGRD", "10 m above ground", "10v", "sfc"),
    "refc": ("REFC", "entire atmosphere", "", ""),
}
FILLER_PARAMS = [("HGT", 0, 3, 5, "gh"), ("TMP", 0, 0, 0, "t"), ("RH", 0, 1, 1, "r"), ("UGRD", 0, 2, 2, "u"),
                 ("VGRD", 0, 2, 3, "v"), ("VVEL", 0, 2, 8, "w"), ("ABSV", 0, 2, 10, "vo")]
FILLER_LEVELS = [925, 850, 700, 500, 300, 250, 200, 100]


# ---------------------------------------------------------------------------- grids
@lru_cache(maxsize=16)
def grid_lonlat(grid_key: str, grid_json: str) -> tuple[np.ndarray, np.ndarray]:
    """Longitude/latitude of every point in file scan order (row 0 = first row written)."""
    g = json.loads(grid_json)
    if g["type"] == "latlon":
        lon = g["lon0"] + np.arange(g["nx"]) * g["dlon"]
        lat = g["lat0"] + np.arange(g["ny"]) * g["dlat"]
        lon2, lat2 = np.meshgrid(((lon + 180) % 360) - 180, lat)
        return lon2, lat2
    if g["type"] == "lambert":
        crs = CRS.from_proj4(lambert_crs(g["latin1"], g["latin2"], g["lov"], g["radius"]))
        fwd = Transformer.from_crs(crs.geodetic_crs, crs, always_xy=True)
        inv = Transformer.from_crs(crs, crs.geodetic_crs, always_xy=True)
        x0, y0 = fwd.transform(((g["lo1"] + 180) % 360) - 180, g["la1"])
        xx, yy = np.meshgrid(x0 + np.arange(g["nx"]) * g["dx"], y0 + np.arange(g["ny"]) * g["dy"])
        lon, lat = inv.transform(xx, yy)
        return np.asarray(lon), np.asarray(lat)
    crs = CRS.from_proj4(geos_crs(g["lon_0"], g["h"]))
    inv = Transformer.from_crs(crs, crs.geodetic_crs, always_xy=True)
    x = np.linspace(g["x0"], g["x1"], g["nx"]) * g["h"]
    y = np.linspace(g["y0"], g["y1"], g["ny"]) * g["h"]
    xx, yy = np.meshgrid(x, y)
    lon, lat = inv.transform(xx, yy)
    return np.asarray(lon), np.asarray(lat)


def lonlat(src: Source) -> tuple[np.ndarray, np.ndarray]:
    return grid_lonlat(src.grid_name, json.dumps(src.grid, sort_keys=True))


@lru_cache(maxsize=32)
def _base_handle(grid_json: str, center: int, packing: str):
    g = json.loads(grid_json)
    h = ec.codes_grib_new_from_samples("GRIB2")
    ec.codes_set(h, "centre", center)  # ecCodes spells the key the WMO way
    if g["type"] == "latlon":
        nx, ny = g["nx"], g["ny"]
        ec.codes_set(h, "gridType", "regular_ll")
        ec.codes_set(h, "Ni", nx)
        ec.codes_set(h, "Nj", ny)
        lat_last = g["lat0"] + (ny - 1) * g["dlat"]
        lon_last = g["lon0"] + (nx - 1) * g["dlon"]
        lon0 = g["lon0"] % 360.0
        ec.codes_set(h, "jScansPositively", 1 if g["dlat"] > 0 else 0)
        ec.codes_set(h, "latitudeOfFirstGridPointInDegrees", g["lat0"])
        ec.codes_set(h, "longitudeOfFirstGridPointInDegrees", lon0)
        ec.codes_set(h, "latitudeOfLastGridPointInDegrees", lat_last)
        ec.codes_set(h, "longitudeOfLastGridPointInDegrees", lon_last % 360.0)
        ec.codes_set(h, "iDirectionIncrementInDegrees", abs(g["dlon"]))
        ec.codes_set(h, "jDirectionIncrementInDegrees", abs(g["dlat"]))
    else:
        nx, ny = g["nx"], g["ny"]
        ec.codes_set(h, "gridType", "lambert")
        for k, v in [("Nx", nx), ("Ny", ny), ("latitudeOfFirstGridPointInDegrees", g["la1"]),
                     ("longitudeOfFirstGridPointInDegrees", g["lo1"]), ("LaDInDegrees", g["latin1"]),
                     ("LoVInDegrees", g["lov"]), ("Latin1InDegrees", g["latin1"]),
                     ("Latin2InDegrees", g["latin2"]), ("DxInMetres", g["dx"]), ("DyInMetres", g["dy"]),
                     ("shapeOfTheEarth", 6 if g["radius"] == 6371229 else 1), ("jScansPositively", 1),
                     ("latitudeOfSouthernPoleInDegrees", -90), ("longitudeOfSouthernPoleInDegrees", 0)]:
            ec.codes_set(h, k, v)
        if g["radius"] != 6371229:
            ec.codes_set(h, "scaleFactorOfRadiusOfSphericalEarth", 0)
            ec.codes_set(h, "scaledValueOfRadiusOfSphericalEarth", int(g["radius"]))
    ec.codes_set(h, "packingType", packing)
    return h


def _message(src: Source, cycle: datetime, values: np.ndarray, d: int, c: int, n: int, lt: int, lev: int,
             lead: int, window: tuple[int, int] | None = None, member: int | None = None,
             missing: float | None = None, packing: str = "grid_complex_spatial_differencing",
             bits: int = 12, frame_minute: int = 0) -> bytes:
    base = _base_handle(json.dumps(src.grid, sort_keys=True), CENTERS.get(src.center, 7), packing)
    h = ec.codes_clone(base)
    try:
        if member is not None:
            ec.codes_set(h, "productDefinitionTemplateNumber", 11 if window else 1)
        elif window:
            ec.codes_set(h, "productDefinitionTemplateNumber", 8)
        ec.codes_set(h, "dataDate", int(cycle.strftime("%Y%m%d")))
        ec.codes_set(h, "dataTime", int(cycle.strftime("%H")) * 100 + frame_minute)
        ec.codes_set(h, "discipline", d)
        ec.codes_set(h, "parameterCategory", c)
        ec.codes_set(h, "parameterNumber", n)
        ec.codes_set(h, "typeOfFirstFixedSurface", lt)
        ec.codes_set(h, "scaleFactorOfFirstFixedSurface", 0)
        ec.codes_set(h, "scaledValueOfFirstFixedSurface", lev)
        if member is not None:
            ec.codes_set(h, "typeOfEnsembleForecast", 1 if member == 0 else 3)
            ec.codes_set(h, "perturbationNumber", member)
            ec.codes_set(h, "numberOfForecastsInEnsemble", 51 if src.center == "ECMWF" else 31)
        if window:
            ec.codes_set(h, "typeOfStatisticalProcessing", 1)
            ec.codes_set(h, "stepRange", f"{window[0]}-{window[1]}")
        else:
            ec.codes_set(h, "forecastTime", lead)
        if missing is not None:
            ec.codes_set(h, "bitmapPresent", 1)
            ec.codes_set(h, "missingValue", missing)
        ec.codes_set(h, "bitsPerValue", bits)
        ec.codes_set_values(h, values.astype("float64").ravel())
        return ec.codes_get_message(h)
    finally:
        ec.codes_release(h)


@lru_cache(maxsize=32)
def _filler_base(grid_json: str, center: int) -> bytes:
    g = json.loads(grid_json)
    shape = (g["ny"], g["nx"])
    rng = np.random.default_rng(3)
    vals = 5000.0 + np.cumsum(rng.normal(0, 1.0, shape), axis=1)
    h = ec.codes_clone(_base_handle(grid_json, center, "grid_complex_spatial_differencing"))
    ec.codes_set(h, "bitsPerValue", 12)
    ec.codes_set_values(h, vals.ravel())
    m = ec.codes_get_message(h)
    ec.codes_release(h)
    return m


def _filler(src: Source, cycle: datetime, lead: int, n: int) -> list[tuple[bytes, str, str, str]]:
    """n cloned filler messages: (bytes, wgrib2 var, level text, ECMWF param)."""
    base = _filler_base(json.dumps(src.grid, sort_keys=True), CENTERS.get(src.center, 7))
    out = []
    combos = [(p, lev) for lev in FILLER_LEVELS for p in FILLER_PARAMS][:n]
    for (var, d, c, num, ep), lev in combos:
        h = ec.codes_new_from_message(base)
        ec.codes_set(h, "dataDate", int(cycle.strftime("%Y%m%d")))
        ec.codes_set(h, "dataTime", int(cycle.strftime("%H")) * 100)
        ec.codes_set(h, "discipline", d)
        ec.codes_set(h, "parameterCategory", c)
        ec.codes_set(h, "parameterNumber", num)
        ec.codes_set(h, "typeOfFirstFixedSurface", 100)
        ec.codes_set(h, "scaledValueOfFirstFixedSurface", lev * 100)
        ec.codes_set(h, "forecastTime", lead)
        out.append((ec.codes_get_message(h), var, f"{lev} mb", ep))
        ec.codes_release(h)
    return out


# ---------------------------------------------------------------------------- model views
def view_for(src: Source, cycle: datetime, lead: float, member: int | None = None) -> View:
    s = src.synth
    noise = s.get("noise", [0.0, 0.0])
    seed = zlib.crc32(f"{src.id}|{cycle:%Y%m%d%H}|{int(lead)}|{member}".encode())
    return View(lead_h=lead, speed=s.get("speed", 1.0), t_bias=s.get("t_bias", 0.0),
                noise_k=noise[0] + noise[1] * lead, noise_seed=seed, precip_scale=s.get("precip_scale", 1.0),
                smooth=s.get("smooth", 0.0), member=member, member_spread=s.get("member_spread", 0.0))


class PrecipBook:
    """Running precipitation totals per (source, run, member), so a 120 h run sums each hour once."""

    def __init__(self):
        self.totals: dict[tuple, dict[int, np.ndarray]] = {}

    def total(self, src: Source, cycle: datetime, fhour: int, member: int | None, lon, lat) -> np.ndarray:
        key = (src.id, cycle, member)
        book = self.totals.setdefault(key, {0: np.zeros(lon.shape)})
        if fhour in book:
            return book[fhour]
        prev = max(h for h in book if h < fhour)
        lead_view = view_for(src, cycle, fhour, member)
        dt_h = 3 if src.grid["type"] == "latlon" and src.grid["nx"] * abs(src.grid["dlon"]) >= 359 else 1
        add = accumulate(cycle + timedelta(hours=prev), cycle + timedelta(hours=fhour), lon, lat, lead_view, cycle,
                         dt_h=dt_h)
        book[fhour] = book[prev] + add
        return book[fhour]


def _raw_values(src: Source, cycle: datetime, step: Step, book: PrecipBook, raw: list[str]) -> dict:
    lon, lat = lonlat(src)
    valid = cycle + timedelta(hours=step.fhour)
    view = view_for(src, cycle, step.fhour, step.member) if not src.is_obs else View()
    want = [r for r in raw if r != "qpf"]
    out = sample(valid, lon, lat, view, tuple(want + (["rate"] if "refc" in want else []))) if want else {}
    if "qpf" in raw and step.fhour > 0:
        out["qpf_total"] = book.total(src, cycle, step.fhour, step.member, lon, lat)
    return out


def _windows(src: Source, fhour: int) -> list[tuple[int, int]]:
    style = src.precip.get("style")
    if style == "total":
        w = [(0, fhour)]
        if src.synth.get("hourly_window") and fhour > 1:
            w.append((fhour - 1, fhour))
        return w
    hours_ = src.precip["hours"]
    start = ((fhour - 1) // hours_) * hours_
    return [(start, fhour)]


def _ftime(lead: int, window: tuple[int, int] | None, is_obs: bool) -> str:
    if window:
        return f"{window[0]}-{window[1]} hour acc fcst"
    return "anl" if lead == 0 else f"{lead} hour fcst"


def indexed_file(src: Source, cycle: datetime, step: Step, book: PrecipBook,
                 members: list[int] | None = None, frozen: bool = False) -> tuple[bytes, str]:
    """A multi-message GRIB2 file and its inventory text (.idx or .index)."""
    raw = ["t2m", "mslp", "qpf", "u10", "v10", "refc"]
    mapped = set(src.member_fields if src.kind == "ensemble" else src.fields)
    want = [r for r in raw if (r == "t2m" and ("t2m" in mapped)) or (r == "mslp" and "mslp" in mapped)
            or (r == "qpf" and "qpf" in mapped) or (r in ("u10", "v10") and "wind10" in mapped)
            or (r == "refc" and "refc" in mapped)]
    msgs: list[tuple[bytes, dict]] = []
    lead = step.fhour
    mem_list = members if members is not None else [step.member]
    for m in mem_list:
        st = Step(step.key, step.fhour, m, step.index)
        vals = _raw_values(src, cycle, st, book, want)
        for r in want:
            var, level, ep, levtype = NAMES[r]
            gid = FIELDS[r].grib[0]
            if r == "mslp" and src.id == "hrrr":
                var, gid = "MSLMA", FIELDS["mslp"].grib[2]
            if r == "mslp" and src.center == "ECMWF":
                gid = FIELDS["mslp"].grib[1]
            if r == "qpf":
                if lead == 0:
                    continue
                total = vals["qpf_total"]
                for w in _windows(src, lead):
                    if w[0] == 0:
                        a = total
                    else:
                        a = total - book.total(src, cycle, w[0], m, *lonlat(src))
                    member = m if src.kind == "ensemble" else None
                    b = _message(src, cycle, np.maximum(a, 0.0), gid.discipline, gid.category, gid.number,
                                 gid.level_type, gid.level, lead, window=w, member=member, bits=14)
                    msgs.append((b, {"var": var, "level": level, "ftime": _ftime(lead, w, src.is_obs), "param": ep,
                                     "levtype": levtype, "member": m}))
                continue
            a = vals[r] if r != "refc" else vals["refc"]
            if r == "t2m" and frozen:
                a = np.full(a.shape, 273.15)
            b = _message(src, cycle, a, gid.discipline, gid.category, gid.number, gid.level_type, gid.level, lead,
                         member=m if src.kind == "ensemble" else None, bits=12 if r != "mslp" else 16)
            msgs.append((b, {"var": var, "level": level, "ftime": _ftime(lead, None, src.is_obs), "param": ep,
                             "levtype": levtype, "member": m}))
    # Mix filler in so mapped messages sit scattered through the file, in the same order for
    # every step of a source, as in real files.
    filler = [(f[0], {"var": f[1], "level": f[2], "ftime": _ftime(lead, None, False), "param": f[3],
                      "levtype": "pl", "member": None, "filler": True})
              for f in _filler(src, cycle, lead, src.synth.get("filler", 0))]
    ordered = filler + msgs
    order = np.random.default_rng(zlib.crc32(src.id.encode())).permutation(len(ordered))
    ordered = [ordered[i] for i in order]
    data = b"".join(b for b, _ in ordered)
    lines, off = [], 0
    for n, (b, meta) in enumerate(ordered, start=1):
        if src.index == "ecmwf":
            d = {"domain": "g", "date": cycle.strftime("%Y%m%d"), "time": cycle.strftime("%H%M"), "expver": "0001",
                 "class": "od", "stream": "enfo" if src.kind == "ensemble" else "oper", "step": str(lead),
                 "levtype": meta["levtype"], "param": meta["param"], "_offset": off, "_length": len(b)}
            if src.kind == "ensemble" and not meta.get("filler"):
                d["type"] = "cf" if meta["member"] == 0 else "pf"
                d["number"] = str(meta["member"])
            else:
                d["type"] = "fc"
            if meta.get("filler"):
                d["levelist"] = meta["level"].split()[0]
            lines.append(json.dumps(d))
        else:
            ens = ""
            if src.kind == "ensemble" and not meta.get("filler"):
                ens = "ENS=low-res ctl:" if meta["member"] == 0 else f"ENS=+{meta['member']}:"
            lines.append(f"{n}:{off}:d={cycle:%Y%m%d%H}:{meta['var']}:{meta['level']}:{meta['ftime']}:{ens}")
        off += len(b)
    return data, "\n".join(lines) + "\n"


PER_FIELD_VARS = {
    # variable name -> (raw field, GRIB2 id index in FIELDS[raw].grib)
    "T_2M": ("t2m", 0), "PMSL": ("mslp", 0), "TOT_PREC": ("qpf", 1), "U_10M": ("u10", 0), "V_10M": ("v10", 0),
    "TMP_TGL_2": ("t2m", 0), "PRMSL_MSL_0": ("mslp", 0), "APCP_SFC_0": ("qpf", 0), "UGRD_TGL_10": ("u10", 0),
    "VGRD_TGL_10": ("v10", 0),
}


def per_field_file(src: Source, cycle: datetime, step: Step, var: str, book: PrecipBook,
                   frozen: bool = False) -> bytes | None:
    raw, gi = PER_FIELD_VARS[var]
    if raw == "qpf" and step.fhour == 0:
        return None
    vals = _raw_values(src, cycle, step, book, [raw])
    gid = FIELDS[raw].grib[gi]
    if raw == "qpf":
        b = _message(src, cycle, np.maximum(vals["qpf_total"], 0), gid.discipline, gid.category, gid.number,
                     gid.level_type, gid.level, step.fhour, window=(0, step.fhour), bits=14)
    else:
        a = vals[raw]
        if raw == "t2m" and frozen:
            a = np.full(a.shape, 273.15)
        b = _message(src, cycle, a, gid.discipline, gid.category, gid.number, gid.level_type, gid.level,
                     step.fhour, bits=16 if raw == "mslp" else 12)
    return bz2.compress(b, 5) if src.compression == "bzip2" else b


def mrms_file(src: Source, frame: datetime) -> bytes:
    lon, lat = lonlat(src)
    refc = sample(frame, lon, lat, View(), ("refc", "rate"))["refc"]
    refc = np.where(refc < 0.0, -30.0, refc)
    covered = np.zeros(lon.shape, bool)
    from wxtiles.synth.atmosphere import land_fraction

    for dx in (-1.6, 0.0, 1.6):
        for dy in (-1.2, 0.0, 1.2):
            covered |= land_fraction(lon + dx, lat + dy) > 0.35
    covered &= (lat > 24.0) & (lat < 50.5) & (lon > -127.0) & (lon < -65.0)
    refc = np.where(covered, refc, -999.0)
    gid = FIELDS["refl"].grib[0]
    b = _message(src, frame, refc, gid.discipline, gid.category, gid.number, gid.level_type, gid.level, 0,
                 missing=-999.0, packing="grid_simple", bits=10, frame_minute=frame.minute)
    return gzip.compress(b, 5)


def abi_file(src: Source, frame: datetime) -> bytes:
    import xarray as xr

    g = src.grid
    lon, lat = lonlat(src)
    ok = np.isfinite(lon) & np.isfinite(lat)
    bt = np.full(lon.shape, np.nan)
    vals = sample(frame, np.where(ok, lon, 0.0), np.where(ok, lat, 0.0), View(), ("t2m", "ir"))["ir"]
    bt[ok] = vals[ok]
    bt += texture(np.where(ok, lon, 0), np.where(ok, lat, 0), 17, 0.6) * 0.4
    x = np.linspace(g["x0"], g["x1"], g["nx"])
    y = np.linspace(g["y0"], g["y1"], g["ny"])
    ds = xr.Dataset(
        {"CMI": (("y", "x"), bt.astype("float32"), {"long_name": "ABI L2+ Cloud and Moisture Imagery brightness "
                                                                "temperature", "units": "K",
                                                     "grid_mapping": "goes_imager_projection"}),
         "goes_imager_projection": ((), np.int32(-2147483647), {
             "grid_mapping_name": "geostationary", "perspective_point_height": g["h"], "semi_major_axis": 6378137.0,
             "semi_minor_axis": 6356752.31414, "inverse_flattening": 298.2572221,
             "latitude_of_projection_origin": 0.0, "longitude_of_projection_origin": g["lon_0"],
             "sweep_angle_axis": "x"})},
        coords={"x": ("x", x, {"units": "rad", "axis": "X"}), "y": ("y", y, {"units": "rad", "axis": "Y"})},
        attrs={"title": "ABI L2 Cloud and Moisture Imagery, band 13 (synthetic)", "platform_ID": src.satellite[-2:],
               "time_coverage_start": frame.strftime("%Y-%m-%dT%H:%M:%S.0Z"), "scene_id": "CONUS"},
    )
    sx = (g["x1"] - g["x0"]) / (g["nx"] - 1)
    sy = (g["y1"] - g["y0"]) / (g["ny"] - 1)
    enc = {"CMI": {"dtype": "int16", "scale_factor": 0.013, "add_offset": 150.0, "_FillValue": -1, "zlib": True},
           "x": {"dtype": "int16", "scale_factor": sx, "add_offset": g["x0"]},
           "y": {"dtype": "int16", "scale_factor": sy, "add_offset": g["y0"]}}
    fd, path = tempfile.mkstemp(suffix=".nc")
    os.close(fd)
    try:
        ds.to_netcdf(path, engine="h5netcdf", encoding=enc)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)

