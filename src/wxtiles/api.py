"""Tile server and status API.

In production this ran as a container-image Lambda behind CloudFront. Tiles are rendered on demand
from the COGs with rio-tiler and colored here, so a color table change needs no re-ingest. A tile
URL names its run, so its response is marked immutable for a year; manifests carry a short TTL.
"""

from __future__ import annotations

import io
import time
from functools import lru_cache

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image
from rio_tiler.errors import PointOutsideBounds, TileOutsideBounds
from rio_tiler.io import Reader

from wxtiles import db
from wxtiles.colormaps import colorize, legend
from wxtiles.fields import FIELDS
from wxtiles.pipeline import cog_key
from wxtiles.registry import registry
from wxtiles.settings import settings
from wxtiles.status import events, run_list, status
from wxtiles.storage import NotFound, products

IMMUTABLE = "public, max-age=31536000, immutable"


@lru_cache(maxsize=1)
def _empty_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


_product_cache: dict[tuple, str] = {}


def _product(source: str, run: str, field: str, fhour: int) -> str | None:
    """GDAL path of a published COG. The key follows from the URL, so tiles need no database (the
    Lambda tiler reads only S3). Positive answers are cached, since a published COG never changes;
    negative ones are not, because the step may publish a second later."""
    key = (source, run, field, fhour)
    if key in _product_cache:
        return _product_cache[key]
    obj = cog_key(source, run, field, fhour)
    if products().head(obj) is None:
        return None
    val = products().gdal_path(obj)
    if len(_product_cache) > 50_000:
        _product_cache.clear()
    _product_cache[key] = val
    return val


def render_tile(source: str, run: str, field: str, fhour: int, z: int, x: int, y: int) -> bytes | None:
    path = _product(source, run, field, fhour)
    if path is None:
        return None
    try:
        with Reader(path) as src:
            img = src.tile(x, y, z, tilesize=256, resampling_method="bilinear", reproject_method="bilinear")
    except TileOutsideBounds:
        return _empty_png()
    arr = img.array
    values = np.ma.getdata(arr[0]).astype("float32")
    mask = np.ma.getmaskarray(arr[0]) | ~np.isfinite(values)
    rgba = colorize(values, mask, FIELDS[field].colormap, alpha=235)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, "PNG", compress_level=6)
    return buf.getvalue()


def create_app() -> FastAPI:
    app = FastAPI(title="wxtiles", version="1.4.0")

    @app.get("/healthz")
    def healthz():
        db.q1("select 1 as ok")
        return {"ok": True}

    @app.get("/tiles/{source}/{run}/{field}/{fhour}/{z}/{x}/{y}.png")
    def tile(source: str, run: str, field: str, fhour: int, z: int, x: int, y: int):
        if source not in registry().sources or field not in FIELDS or not (0 <= z <= 12):
            raise HTTPException(404, "unknown source, field or zoom")
        t0 = time.perf_counter()
        png = render_tile(source, run, field, fhour, z, x, y)
        if png is None:
            # Not published (yet). Cache briefly so a client polling for a new step is not stuck.
            return Response(status_code=404, headers={"Cache-Control": "public, max-age=15"})
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": IMMUTABLE,
                                 "Server-Timing": f"render;dur={(time.perf_counter() - t0) * 1000:.1f}"})

    @app.get("/api/point")
    def point(source: str, run: str, field: str, fhour: int, lon: float = Query(..., ge=-180, le=180),
              lat: float = Query(..., ge=-85.05, le=85.05)):
        path = _product(source, run, field, fhour)
        if path is None:
            raise HTTPException(404, "no such product")
        try:
            with Reader(path) as src:
                pt = src.point(lon, lat)
        except PointOutsideBounds:
            return {"value": None, "units": FIELDS[field].units}
        v = float(np.ma.getdata(pt.data)[0])
        masked = bool(np.ma.getmaskarray(pt.data)[0]) if hasattr(pt.data, "mask") else False
        ok = np.isfinite(v) and not masked
        return {"value": round(v, 2) if ok else None, "units": FIELDS[field].units, "label": FIELDS[field].label}

    @app.get("/api/legend/{colormap}")
    def legend_(colormap: str):
        try:
            return legend(colormap)
        except KeyError as e:
            raise HTTPException(404, "unknown colormap") from e

    @app.get("/api/status")
    def status_():
        return JSONResponse(status(), headers={"Cache-Control": "no-store"})

    @app.get("/api/events")
    def events_(limit: int = 60, source: str | None = None):
        return JSONResponse(events(limit, source), headers={"Cache-Control": "no-store"})

    @app.get("/api/runs/{source}")
    def runs_(source: str):
        if source not in registry().sources:
            raise HTTPException(404, "unknown source")
        return run_list(source)

    @app.get("/manifests/{path:path}")
    def manifest(path: str):
        if ".." in path or not path.endswith(".json"):
            raise HTTPException(404)
        try:
            data = products().get(f"manifests/{path}")
        except NotFound as e:
            raise HTTPException(404, "no manifest") from e
        return Response(data, media_type="application/json",
                        headers={"Cache-Control": f"public, max-age={settings.manifest_ttl}"})

    app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app


app = create_app()
