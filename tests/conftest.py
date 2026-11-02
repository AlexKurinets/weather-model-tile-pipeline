"""Shared fixtures. Unit tests need nothing; the stack tests need a Postgres in TEST_DATABASE_URL.

The stack fixture seeds a cut-down copy of the real registry (same sources, faults and file
formats; coarser grids, shorter runs, fewer members) into local directories standing in for the
buckets, runs the worker until nothing is left to do, and is shared by every stack test.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from wxtiles import settings as settings_mod

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 10, 28, 20, 40, tzinfo=UTC)
KEEP = ["gfs", "ifs", "icon", "gdps", "pangu", "hrrr", "gefs", "ens", "rtma", "mrms", "goes_west"]


def small_registry(path: Path) -> Path:
    raw = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    g = raw["grids"]
    for name in ("global_1p0_0e_ns", "global_1p0_180w_ns", "global_1p0_180w_sn", "global_1p0_0e_sn"):
        g[name].update(nx=72, ny=37, dlon=5.0, dlat=5.0 * (1 if g[name]["dlat"] > 0 else -1))
    g["conus_lcc_12km"].update(nx=90, ny=53, dx=60000, dy=60000)
    g["conus_lcc_15km"].update(nx=72, ny=42, dx=75000, dy=75000)
    g["ndfd_lcc_15km"].update(nx=80, ny=54, dx=75000, dy=75000)
    g["mrms_conus_0p07"].update(nx=200, ny=100, dlon=0.35, dlat=-0.35, lon0=230.175, lat0=54.825)
    g["goes_west_pacus"].update(nx=112, ny=67)
    shorter = {"gfs": [0, 60, 6], "pangu": [0, 60, 6], "ifs": [0, 24, 6], "icon": [0, 84, 12], "gdps": [0, 48, 12],
               "hrrr": [0, 9, 1], "gefs": [0, 24, 6], "ens": [0, 24, 12]}
    sources = []
    for s in raw["sources"]:
        if s["id"] not in KEEP:
            continue
        if s["id"] in shorter:
            s["fhours"] = shorter[s["id"]]
        if s.get("members"):
            s["members"] = 3
        if s.get("keep_frames"):
            s["keep_frames"] = 6 if s["id"] == "goes_west" else 3
        s.setdefault("synth", {})["filler"] = min(s["synth"].get("filler", 0), 6)
        sources.append(s)
    raw["sources"] = sources
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return path


def configure(tmp: Path, sources_file: Path) -> None:
    os.environ.update({
        "WX_STORAGE": "local", "WX_LOCAL_ROOT": str(tmp / "buckets"), "WX_SOURCES_FILE": str(sources_file),
        "WX_CLOCK": "sim", "WX_RETRY_SECONDS": "30", "WX_LEASE_SECONDS": "120", "WX_WARM_URL": "",
        "DATABASE_URL": os.environ["TEST_DATABASE_URL"], "WX_WEB_DIR": str(ROOT / "web"),
    })
    settings_mod.reload()
    from wxtiles import db
    from wxtiles.registry import registry

    registry.cache_clear()
    db.close_pool()


def needs_db():
    return pytest.mark.skipif("TEST_DATABASE_URL" not in os.environ, reason="needs TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def stack(tmp_path_factory):
    if "TEST_DATABASE_URL" not in os.environ:
        pytest.skip("needs TEST_DATABASE_URL")
    tmp = tmp_path_factory.mktemp("stack")
    configure(tmp, small_registry(tmp / "sources.yaml"))
    from wxtiles import db
    from wxtiles.synth.publisher import seed
    from wxtiles.worker import run_worker

    db.init_schema()
    db.reset_all()
    counts = seed(T0, processes=1)
    run_worker(processes=2, until_idle=True, tick_s=0.3, stop_after=900)
    yield {"tmp": tmp, "counts": counts}
    db.close_pool()
