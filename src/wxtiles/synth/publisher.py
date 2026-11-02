"""Posts synthetic upstream files to the mirror buckets on the posting schedule, with faults.

`seed` sets the simulated clock and posts everything that would be upstream by then. `advance`
moves the clock forward and posts what falls in that window, which is how a demo watches a new
cycle arrive. Faults are deterministic so the stack comes up the same way every time:

  gfs        newest complete run: f048, f054 and f060 never reach the AWS bucket (NOMADS has them)
  hrrr       newest complete run: f07 on the AWS bucket is cut off partway through a mapped message
  icon       newest complete run: f084 is never posted anywhere, so the run is held at its deadline
  gdps       newest complete run: 2 m temperature at f048 is a frozen field of 273.15 K
  goes_west  frames in the last 45 minutes before the seed time are missing (the feed is late)

`advance --fault primary-down` keeps every new object off the AWS bucket for that window.
"""

from __future__ import annotations

import multiprocessing as mp
from dataclasses import dataclass
from datetime import datetime, timedelta

from wxtiles import db
from wxtiles.fetch import index_key
from wxtiles.fields import raw_fields
from wxtiles.idx import parse_wgrib2, select
from wxtiles.registry import Source, Step, registry
from wxtiles.storage import S3Store, store
from wxtiles.synth.writers import PrecipBook, abi_file, indexed_file, mrms_file, per_field_file


@dataclass
class Faults:
    seed_time: datetime | None = None
    primary_down: bool = False


def newest_complete(src: Source, now: datetime) -> datetime | None:
    done = [r for r in src.runs_at(now) if src.last_expected_at(r) <= now]
    return max(done) if done else None


def _put(src: Source, mirror: str, key: str, data: bytes, notify: bool) -> None:
    st = store(registry().mirrors[mirror].bucket)
    st.put(key, data)
    if notify and src.notify and mirror == src.mirrors[0] and not key.endswith((".idx", ".index")):
        db.q("insert into upstream_events (mirror, key) values (%s, %s)", (mirror, key))


def post_step(src: Source, run_time: datetime, step: Step, book: PrecipBook, faults: Faults) -> int:
    """Write one step's objects to the source's mirrors. Returns the number of objects written."""
    seed_run = newest_complete(src, faults.seed_time) if faults.seed_time else None
    on_seed_run = seed_run is not None and run_time == seed_run
    mirrors = list(src.mirrors)
    if faults.primary_down and src.mirrors[0] == "noaa-aws":
        mirrors = mirrors[1:]
    if src.id == "icon" and on_seed_run and step.fhour == 84:
        return 0
    if src.id == "gfs" and on_seed_run and step.fhour in (48, 54, 60):
        mirrors = [m for m in mirrors if m != "noaa-aws"]
    if src.id == "goes_west" and faults.seed_time and run_time > faults.seed_time - timedelta(minutes=45):
        return 0
    n = 0
    if src.layout == "per_field":
        for var in src.variable_names():
            key = src.key_for(run_time, step, var)
            frozen = src.id == "gdps" and on_seed_run and step.fhour == 48 and var == "TMP_TGL_2"
            data = per_field_file(src, run_time, step, var, book, frozen=frozen)
            if data is None:
                continue
            for m in mirrors:
                _put(src, m, key, data, True)
                n += 1
        return n
    key = src.key_for(run_time, step)
    if src.id == "mrms":
        data, idx = mrms_file(src, run_time), None
    elif src.format == "netcdf":
        data, idx = abi_file(src, run_time), None
    else:
        members = list(range(src.members)) if src.kind == "ensemble" and src.member_layout == "in_file" else None
        data, idx = indexed_file(src, run_time, step, book, members=members)
    for m in mirrors:
        body = data
        if src.id == "hrrr" and on_seed_run and step.fhour == 7 and m == "noaa-aws":
            body = _truncate_last_mapped(src, data, idx)
        if idx is not None:
            _put(src, m, index_key(src, key), idx.encode(), False)
        _put(src, m, key, body, True)
        n += 1
    return n


def _truncate_last_mapped(src: Source, data: bytes, idx: str) -> bytes:
    """Cut the file 40% of the way into the last message a worker would fetch, so the index (which
    was written for the whole file) promises bytes that are not there."""
    chosen = select(parse_wgrib2(idx), raw_fields(src.decode_fields), "wgrib2", src.idx_names)
    last = max((e for es in chosen.values() for e in es), key=lambda e: e.start)
    end = last.end if last.end is not None else len(data) - 1
    return data[: last.start + int((end - last.start) * 0.4)]


def _post_source(args: tuple) -> tuple[str, int]:
    sid, t_from, t_to, faults = args
    db.close_pool()
    src = registry()[sid]
    if src.trigger:
        return sid, 0
    book = PrecipBook()
    n = 0
    for rt in src.runs_at(t_to):
        for step in src.steps():
            exp = src.expected_at(rt, step)
            if (t_from is None or exp > t_from) and exp <= t_to:
                n += post_step(src, rt, step, book, faults)
    return sid, n


def ensure_buckets() -> None:
    reg = registry()
    names = {m.bucket for m in reg.mirrors.values()}
    from wxtiles.settings import settings

    names.add(settings.products_bucket)
    for b in sorted(names):
        st = store(b)
        if isinstance(st, S3Store):
            st.ensure_bucket()


def post_window(t_from: datetime | None, t_to: datetime, faults: Faults, sources: list[str] | None = None,
                processes: int = 4) -> dict[str, int]:
    ids = sources or [s.id for s in registry()]
    tasks = [(sid, t_from, t_to, faults) for sid in ids]
    if processes <= 1:
        return dict(_post_source(t) for t in tasks)
    # Heaviest sources first so the pool finishes together.
    weight = {"gefs": 0, "mrms": 1, "goes_east": 2, "goes_west": 3, "hrrr": 4}
    tasks.sort(key=lambda t: weight.get(t[0], 9))
    with mp.get_context("spawn").Pool(processes) as pool:
        return dict(pool.map(_post_source, tasks, chunksize=1))


def seed(t0: datetime, processes: int = 4) -> dict[str, int]:
    ensure_buckets()
    db.set_clock(t0)
    counts = post_window(None, t0, Faults(seed_time=t0), processes=processes)
    db.event("seeded", f"posted {sum(counts.values())} upstream objects up to {db.utc(t0)}")
    return counts


def advance(minutes: float, primary_down: bool = False, processes: int = 4) -> tuple[datetime, dict[str, int]]:
    t_from = db.now()
    t_to = t_from + timedelta(minutes=minutes)
    # Objects are posted before the clock moves: moving it first would let the poller look for
    # objects that are not there yet and park them on a retry the notification cannot shortcut.
    counts = post_window(t_from, t_to, Faults(primary_down=primary_down), processes=processes)
    db.set_clock(t_to)
    db.event("clock", f"advanced to {db.utc(t_to)}, posted {sum(counts.values())} objects"
             + (" with the AWS bucket down" if primary_down else ""))
    return t_to, counts
