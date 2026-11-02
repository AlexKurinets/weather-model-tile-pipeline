"""Cloud-Optimized GeoTIFFs in Web Mercator, aligned to the XYZ tile grid.

Each field is warped from its native grid to EPSG:3857 at one zoom level's resolution, on an extent
snapped outward to that zoom's tile boundaries. The COG's 256 px internal blocks then line up with
web tiles (one tile at the COG's zoom is one block read, and each overview is one zoom further
out), and every field from the same grid lands on the same extent, which is what lets QC compare
two runs pixel for pixel.
"""

from __future__ import annotations

import math
import os
import tempfile

import numpy as np
import rasterio
import rasterio.shutil
from affine import Affine
from rasterio.io import MemoryFile
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, reproject, transform_bounds

from wxtiles.grids import GridSpec

Z0_RES = 156_543.033928  # meters per pixel at zoom 0, 256 px tiles
MERC_EDGE = 20_037_508.342789244


def cog_zoom(grid: GridSpec) -> int:
    """The finest zoom whose pixels are not smaller than about half the native spacing."""
    return max(1, min(math.ceil(math.log2(Z0_RES / grid.res_m)), 8))


def mercator_target(grid: GridSpec, zoom: int) -> tuple[Affine, tuple[int, int]]:
    """Tile-aligned Web Mercator transform and shape covering the native grid at `zoom`."""
    res = Z0_RES / 2**zoom
    tile = 256 * res
    bounds = transform_bounds(grid.crs, "EPSG:3857", *array_bounds(*grid.shape, grid.transform), densify_pts=41)
    x0 = max(-MERC_EDGE, math.floor((bounds[0] + MERC_EDGE) / tile) * tile - MERC_EDGE)
    x1 = min(MERC_EDGE, math.ceil((bounds[2] + MERC_EDGE) / tile) * tile - MERC_EDGE)
    y0 = max(-MERC_EDGE, math.floor((bounds[1] + MERC_EDGE) / tile) * tile - MERC_EDGE)
    y1 = min(MERC_EDGE, math.ceil((bounds[3] + MERC_EDGE) / tile) * tile - MERC_EDGE)
    shape = (int(round((y1 - y0) / res)), int(round((x1 - x0) / res)))
    return Affine(res, 0.0, x0, 0.0, -res, y1), shape


def to_mercator(values: np.ndarray, grid: GridSpec, zoom: int | None = None) -> tuple[np.ndarray, Affine, int]:
    """Bilinear warp to the tile-aligned Mercator grid. NaN stays NaN, so a missing MRMS pixel or
    the space around a GOES disk never bleeds into its neighbors."""
    zoom = zoom or cog_zoom(grid)
    tr, shape = mercator_target(grid, zoom)
    dst = np.full(shape, np.nan, dtype="float32")
    reproject(values.astype("float32"), dst, src_transform=grid.transform, src_crs=grid.crs, dst_transform=tr,
              dst_crs="EPSG:3857", resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan)
    return dst, tr, zoom


def write_cog(merc: np.ndarray, transform: Affine, path: str) -> None:
    rows, cols = merc.shape
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", height=rows, width=cols, count=1, dtype="float32", crs="EPSG:3857",
                      transform=transform, nodata=float("nan")) as ds:
            ds.write(merc.astype("float32"), 1)
        with mem.open() as src:
            rasterio.shutil.copy(src, path, driver="COG", COMPRESS="DEFLATE", PREDICTOR="YES", LEVEL="6",
                                 BLOCKSIZE="256", OVERVIEW_RESAMPLING="BILINEAR", SPARSE_OK="TRUE")


def cog_bytes(merc: np.ndarray, transform: Affine) -> bytes:
    fd, path = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    try:
        write_cog(merc, transform, path)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.unlink(path)


def read_cog(path_or_bytes: str | bytes) -> tuple[np.ndarray, Affine]:
    """Full-resolution band 1 as float32 with NaN for nodata."""
    if isinstance(path_or_bytes, bytes):
        with MemoryFile(path_or_bytes) as mem, mem.open() as ds:
            return ds.read(1, masked=True).astype("float32").filled(np.nan), ds.transform
    with rasterio.open(path_or_bytes) as ds:
        return ds.read(1, masked=True).astype("float32").filled(np.nan), ds.transform
