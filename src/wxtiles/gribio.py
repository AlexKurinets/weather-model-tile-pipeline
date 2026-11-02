"""Splitting, integrity checks and decoding for GRIB2 bytes and GOES ABI netCDF."""

from __future__ import annotations

import bz2
import gzip
import io
import struct
from dataclasses import dataclass

import eccodes as ec
import numpy as np

from wxtiles.fields import identify
from wxtiles.grids import GridSpec, geos_from_xy, lambert_from_keys, latlon_from_keys


class IntegrityError(Exception):
    """Downloaded bytes are not whole, valid messages (truncated upload, short read, bad gzip)."""


def split_messages(data: bytes, expected_len: int | None = None) -> list[bytes]:
    """Split concatenated GRIB2 messages, checking each starts with GRIB, declares a length that
    fits, and ends with 7777. A short range read or a half-uploaded file fails here."""
    if expected_len is not None and len(data) != expected_len:
        raise IntegrityError(f"got {len(data)} bytes, expected {expected_len}")
    out, pos = [], 0
    while pos < len(data):
        if data[pos:pos + 4] != b"GRIB":
            raise IntegrityError(f"no GRIB marker at byte {pos}")
        if data[pos + 7] != 2:
            raise IntegrityError(f"GRIB edition {data[pos + 7]} at byte {pos}, expected 2")
        (length,) = struct.unpack(">Q", data[pos + 8:pos + 16])
        if pos + length > len(data):
            raise IntegrityError(f"message at byte {pos} declares {length} bytes, only {len(data) - pos} present")
        if data[pos + length - 4:pos + length] != b"7777":
            raise IntegrityError(f"message at byte {pos} does not end in 7777")
        out.append(data[pos:pos + length])
        pos += length
    if not out:
        raise IntegrityError("no messages")
    return out


def decompress(data: bytes, compression: str) -> bytes:
    try:
        if compression == "gzip":
            return gzip.decompress(data)
        if compression == "bzip2":
            return bz2.decompress(data)
    except (OSError, EOFError, ValueError) as e:
        raise IntegrityError(f"{compression} stream is damaged: {e}") from e
    return data


@dataclass
class Decoded:
    name: str  # raw field name: t2m, mslp, qpf, u10, v10, refc, refl, ir
    values: np.ndarray  # float32, north-up, NaN where missing
    grid: GridSpec
    units: str
    window: tuple[int, int] | None = None  # accumulation window in hours
    member: int | None = None


_GRID_KEYS_LL = ["Ni", "Nj", "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees",
                 "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
                 "iScansNegatively", "jScansPositively"]
_GRID_KEYS_LCC = ["Nx", "Ny", "DxInMetres", "DyInMetres", "Latin1InDegrees", "Latin2InDegrees", "LoVInDegrees",
                  "LaDInDegrees", "radius", "latitudeOfFirstGridPointInDegrees",
                  "longitudeOfFirstGridPointInDegrees", "jScansPositively"]


def decode_grib(msg: bytes) -> Decoded | None:
    """Decode one message into a normalized array, or None if it is not a field we map."""
    h = ec.codes_new_from_message(msg)
    try:
        g = lambda k: ec.codes_get_long(h, k)  # noqa: E731
        name = identify(g("discipline"), g("parameterCategory"), g("parameterNumber"),
                        g("typeOfFirstFixedSurface"), _level(h))
        if name is None:
            return None
        vals = ec.codes_get_values(h).astype("float32")
        if g("bitmapPresent"):
            missing = ec.codes_get_double(h, "missingValue")
            vals[vals == missing] = np.nan
        grid_type = ec.codes_get_string(h, "gridType")
        if grid_type == "regular_ll":
            keys = {k: ec.codes_get(h, k) for k in _GRID_KEYS_LL}
            arr, grid = latlon_from_keys(keys, vals)
        elif grid_type == "lambert":
            keys = {k: ec.codes_get(h, k) for k in _GRID_KEYS_LCC}
            arr, grid = lambert_from_keys(keys, vals)
        else:
            raise IntegrityError(f"unsupported grid {grid_type}")
        window = None
        if g("productDefinitionTemplateNumber") in (8, 11):
            window = (int(g("startStep")), int(g("endStep")))
        member = None
        if g("productDefinitionTemplateNumber") in (1, 11):
            member = int(g("perturbationNumber"))
        return Decoded(name, arr.astype("float32"), grid, ec.codes_get_string(h, "units"), window, member)
    finally:
        ec.codes_release(h)


def _level(h) -> int:
    try:
        return int(ec.codes_get_long(h, "level"))
    except ec.KeyValueNotFoundError:
        return 0


def decode_netcdf_abi(data: bytes) -> Decoded:
    """GOES ABI L2 CMIP: CMI stored as scaled int16 with a fill value, on the fixed grid."""
    import xarray as xr

    try:
        ds = xr.open_dataset(io.BytesIO(data), engine="h5netcdf", mask_and_scale=True)
    except Exception as e:  # truncated HDF5 raises a variety of errors
        raise IntegrityError(f"netCDF does not open: {e}") from e
    with ds:
        cmi = ds["CMI"].values.astype("float32")
        proj = dict(ds["goes_imager_projection"].attrs)
        arr, grid = geos_from_xy(ds["x"].values, ds["y"].values, proj, cmi)
    return Decoded("ir", arr, grid, "K")
