"""Job handlers: ingest a step, derive accumulations and ensemble statistics, finalize a run, run
an AI model, warm the tile cache. Every handler is idempotent, so a job taken over from a dead
worker can simply run again."""

from __future__ import annotations

import io
import time
from datetime import datetime

import numpy as np

from wxtiles import db
from wxtiles.cog import cog_bytes, read_cog, to_mercator
from wxtiles.fetch import NotAvailable, fetch_step
from wxtiles.fields import quantize, to_display, wind_speed_mph
from wxtiles.gribio import IntegrityError, decode_grib, decode_netcdf_abi
from wxtiles.manifests import live_run, run_lock, write_index, write_latest, write_run
from wxtiles.qc import Check, check_field, check_grid, check_run_to_run
from wxtiles.registry import Source, Step, registry
from wxtiles.settings import settings
from wxtiles.storage import products, store


class RetryLater(Exception):
    def __init__(self, delay_s: float, reason: str):
        super().__init__(reason)
        self.delay_s = delay_s


def cog_key(source: str, run: str, field: str, fhour: int) -> str:
    return f"cogs/{source}/{run}/{field}/f{fhour:03d}.tif"


def _step_obj(src: Source, key: str) -> Step:
    for s in src.steps():
        if s.key == key:
            return s
    raise KeyError(key)


# ---------------------------------------------------------------------------- scratch arrays
def _scratch_key(source: str, run: str, name: str) -> str:
    return f"scratch/{source}/{run}/{name}.npz"


def put_scratch(source: str, run: str, name: str, a: np.ndarray, tr) -> None:
    buf = io.BytesIO()
    np.savez_compressed(buf, a=a.astype("float32"), tr=np.array(tr[:6]))
    products().put(_scratch_key(source, run, name), buf.getvalue())


def get_scratch(source: str, run: str, name: str):
    from affine import Affine

    from wxtiles.storage import NotFound

    try:
        data = products().get(_scratch_key(source, run, name))
    except NotFound:
        return None
    z = np.load(io.BytesIO(data))
    return z["a"], Affine(*z["tr"])


# ---------------------------------------------------------------------------- publishing
def publish(src: Source, run: str, field: str, fhour: int, merc: np.ndarray, tr, zoom: int) -> None:
    merc = quantize(field, merc)
    key = cog_key(src.id, run, field, fhour)
    products().put(key, cog_bytes(merc, tr), "image/tiff")
    finite = merc[np.isfinite(merc)]
    vmin = float(finite.min()) if finite.size else None
    vmax = float(finite.max()) if finite.size else None
    db.q("""insert into products (source, run, field, fhour, cog_key, zoom, vmin, vmax)
            values (%s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (source, run, field, fhour) do nothing""", (src.id, run, field, fhour, key, zoom, vmin, vmax))


def _display_limit(name: str, limit: float) -> float:
    return limit * 1.8 if name == "t2m" else limit / 100.0 if name == "mslp" else limit


def _run_to_run(src: Source, run: str, run_time: datetime, name: str, fhour: int, merc: np.ndarray) -> Check | None:
    limit = registry().qc.get("run_to_run", {}).get(name)
    if limit is None or src.is_obs or src.kind == "ensemble":
        return None
    prev = db.q1("""select r.run, r.run_time from runs r where r.source = %s and r.run_time < %s
                    and r.state in ('complete', 'in_progress', 'held') order by r.run_time desc limit 1""",
                 (src.id, run_time))
    if not prev:
        return None
    lag = int((run_time - prev["run_time"]).total_seconds() // 3600)
    row = db.q1("select cog_key from products where source = %s and run = %s and field = %s and fhour = %s",
                (src.id, prev["run"], name, fhour + lag))
    if not row:
        return None
    old, _ = read_cog(products().get(row["cog_key"]))
    return check_run_to_run(name, merc, old, _display_limit(name, limit))


# ---------------------------------------------------------------------------- ingest
def ingest(source: str, run: str, step_key: str) -> str:
    reg = registry()
    src = reg[source]
    row = db.q1("select * from steps where source = %s and run = %s and step = %s", (source, run, step_key))
    if row is None or row["state"] != "expected":
        return "skip"
    step = _step_obj(src, step_key)
    run_time = src.run_time(run)
    t0 = time.perf_counter()
    db.q("update steps set attempts = attempts + 1 where source = %s and run = %s and step = %s",
         (source, run, step_key))
    try:
        got = fetch_step(src, run_time, step)
    except NotAvailable as e:
        if db.now() >= row["deadline"]:
            db.q("""update steps set state = 'missing', last_error = %s where source = %s and run = %s and step = %s""",
                 (str(e)[:500], source, run, step_key))
            db.event("missing", f"{step_key} not on any mirror by its deadline ({e})", source, run)
            maybe_finalize(src, run)
            return "missing"
        raise RetryLater(settings.retry_seconds, str(e)) from e
    if got.skipped:
        kind = "integrity" if any("not posted" not in r for r in got.skipped) else "failover"
        db.event(kind, f"{step_key} from {got.mirror} after " + "; ".join(got.skipped), source, run)

    try:
        decoded = [decode_netcdf_abi(got.netcdf)] if got.netcdf else [d for m in got.messages
                                                                       if (d := decode_grib(m))]
    except IntegrityError as e:
        raise RetryLater(settings.retry_seconds, f"decode failed: {e}") from e
    checks: list[Check] = []
    if decoded:
        checks.append(check_grid(decoded[0].grid.signature, src.grid))
    by_member: dict[int | None, dict[str, object]] = {}
    for d in decoded:
        by_member.setdefault(d.member if src.kind == "ensemble" else None, {})[d.name] = d
    limits = reg.qc
    outputs: list[tuple[str, int | None, np.ndarray, object]] = []  # (field, member, native display, grid)
    for member, raw in by_member.items():
        for name, d in raw.items():
            if name in ("u10", "v10"):
                continue
            checks += check_field(name, d.values, limits)
        if "u10" in raw and "v10" in raw:
            u, v = raw["u10"], raw["v10"]
            checks += check_field("wind10", np.hypot(u.values, v.values), limits)
            outputs.append(("wind10", member, wind_speed_mph(u.values, v.values), u))
        for name in ("t2m", "mslp", "refc", "refl", "ir"):
            if name in raw:
                d = raw[name]
                outputs.append((name, member, to_display(name, d.values, d.units), d))
        if "qpf" in raw:
            d = raw["qpf"]
            outputs.append(("qpf", member, to_display("qpf", d.values, d.units), d))

    merc_out = []
    for name, member, values, d in outputs:
        merc, tr, zoom = to_mercator(values, d.grid)
        merc_out.append((name, member, merc, tr, zoom, d))
        if member is None and name in ("t2m", "mslp"):
            c = _run_to_run(src, run, run_time, name, step.fhour, quantize(name, merc))
            if c:
                checks.append(c)

    failed = [c for c in checks if not c.passed]
    if checks:
        with db.conn() as c:
            c.cursor().executemany(
                """insert into qc_results (source, run, step, field, check_name, passed, value, detail)
                   values (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [(source, run, step_key, k.field, k.name, k.passed, k.value, k.detail) for k in checks])
    ms = int((time.perf_counter() - t0) * 1000)
    if failed:
        detail = "; ".join(f"{k.field} {k.name}: {k.detail}" for k in failed)
        db.q("""update steps set state = 'qc_failed', mirror = %s, bytes_fetched = %s, bytes_full = %s,
                posted_wall = %s, ingest_ms = %s, last_error = %s where source = %s and run = %s and step = %s""",
             (got.mirror, got.bytes_fetched, got.bytes_full, got.posted, ms, detail[:500], source, run, step_key))
        db.event("qc_failed", f"{step_key}: {detail}", source, run)
        maybe_finalize(src, run)
        return "qc_failed"

    needs_derive = False
    for name, member, merc, tr, zoom, d in merc_out:
        if src.kind == "ensemble":
            tag = f"m{member:02d}_f{step.fhour:03d}"
            if name == "qpf":
                kind = "tot" if d.window[0] == 0 else f"win{d.window[0]:03d}"
                put_scratch(source, run, f"qpf_{kind}_{tag}", quantize("qpf", merc), tr)
            else:
                put_scratch(source, run, f"{name}_{tag}", quantize(name, merc), tr)
            needs_derive = True
        elif name == "qpf":
            kind = "tot" if d.window[0] == 0 else f"win{d.window[0]:03d}"
            put_scratch(source, run, f"qpf_{kind}_f{step.fhour:03d}", quantize("qpf", merc), tr)
            needs_derive = True
        else:
            publish(src, run, name, step.fhour, merc, tr, zoom)
    ms = int((time.perf_counter() - t0) * 1000)
    db.q("""update steps set state = 'done', mirror = %s, bytes_fetched = %s, bytes_full = %s, posted_wall = %s,
            done_wall = now(), ingest_ms = %s, last_error = null where source = %s and run = %s and step = %s""",
         (got.mirror, got.bytes_fetched, got.bytes_full, got.posted, ms, source, run, step_key))
    if needs_derive:
        db.enqueue("derive", {"source": source, "run": run}, f"derive:{source}:{run}", priority=4)
    write_run(src, run)
    if step.fhour == 0 or src.is_obs:
        write_latest(src)
    for child in reg.triggered_by(source):
        if step_key == child.trigger["step"]:
            db.enqueue("ai_run", {"source": child.id, "run": run}, f"ai_run:{child.id}:{run}", priority=2)
    maybe_finalize(src, run)
    return "done"


# ---------------------------------------------------------------------------- derive
def derive(source: str, run: str) -> int:
    """Chain precipitation windows into run totals and compute ensemble statistics. Whatever is
    not ready yet is left for the next derive job, which every ingested step queues."""
    src = registry()[source]
    published = 0
    with run_lock(source, run):
        have = {(r["field"], r["fhour"]) for r in
                db.q("select field, fhour from products where source = %s and run = %s", (source, run))}
        fhours = [f for f in src.fhours if f > 0]
        members = [None] if src.kind != "ensemble" else list(range(src.members))

        def tot_name(m, fh):
            return f"qpf_tot_f{fh:03d}" if m is None else f"qpf_tot_m{m:02d}_f{fh:03d}"

        def win_name(m, fh, start):
            return f"qpf_win{start:03d}_f{fh:03d}" if m is None else f"qpf_win{start:03d}_m{m:02d}_f{fh:03d}"

        totals: dict[tuple, tuple] = {}
        if "qpf" in src.decode_fields:
            for m in members:
                prev_fh = 0
                for fh in fhours:
                    t = get_scratch(source, run, tot_name(m, fh))
                    if t is None:
                        start = prev_fh
                        w = get_scratch(source, run, win_name(m, fh, start))
                        base = (np.zeros_like(w[0]), w[1]) if (w is not None and start == 0) else \
                            totals.get((m, start))
                        if w is None or base is None:
                            break
                        t = (base[0] + w[0], w[1])
                        put_scratch(source, run, tot_name(m, fh), t[0], t[1])
                    totals[(m, fh)] = t
                    prev_fh = fh
            if src.kind != "ensemble":
                for (_m, fh), (a, tr) in totals.items():
                    if ("qpf", fh) not in have:
                        publish(src, run, "qpf", fh, a, tr, _zoom_of(tr))
                        published += 1
        if src.kind == "ensemble":
            for fh in src.fhours:
                if ("t2m_mean", fh) not in have:
                    arrs = [get_scratch(source, run, f"t2m_m{m:02d}_f{fh:03d}") for m in members]
                    if all(a is not None for a in arrs):
                        stack = np.stack([a[0] for a in arrs])
                        tr = arrs[0][1]
                        publish(src, run, "t2m_mean", fh, stack.mean(axis=0), tr, _zoom_of(tr))
                        publish(src, run, "t2m_spread", fh, stack.std(axis=0), tr, _zoom_of(tr))
                        published += 2
                if fh > 0 and ("qpf_mean", fh) not in have and all((m, fh) in totals for m in members):
                    stack = np.stack([totals[(m, fh)][0] for m in members])
                    tr = totals[(members[0], fh)][1]
                    publish(src, run, "qpf_mean", fh, stack.mean(axis=0), tr, _zoom_of(tr))
                    publish(src, run, "qpf_p1in", fh, (stack >= 1.0).mean(axis=0) * 100.0, tr, _zoom_of(tr))
                    published += 2
    if published:
        write_run(src, run)
    maybe_finalize(src, run)
    return published


def _zoom_of(tr) -> int:
    import math

    from wxtiles.cog import Z0_RES

    return int(round(math.log2(Z0_RES / tr.a)))


# ---------------------------------------------------------------------------- finalize
def maybe_finalize(src: Source, run: str) -> None:
    row = db.q1("select count(*) filter (where state = 'expected') as open from steps where source = %s and run = %s",
                (src.id, run))
    if row and row["open"] == 0:
        db.enqueue("finalize", {"source": src.id, "run": run}, f"finalize:{src.id}:{run}", priority=3, delay_s=0.5)


def _expected_products(src: Source) -> set[tuple[str, int]]:
    out = set()
    for f in src.fields:
        for fh in src.fhours:
            if fh == 0 and f in ("qpf", "qpf_mean", "qpf_p1in"):
                continue
            out.add((f, fh))
    return out


def finalize(source: str, run: str) -> str:
    """Decide a finished run's fate. Complete and clean: it becomes the default map if it is the
    newest such run. Otherwise it is held, with the reason, and the previous complete run stays."""
    reg = registry()
    src = reg[source]
    r = db.q1("select * from runs where source = %s and run = %s", (source, run))
    if r is None or r["state"] != "in_progress":
        return "skip"
    steps = db.q("select step, state, last_error from steps where source = %s and run = %s order by fhour, step",
                 (source, run))
    if any(s["state"] == "expected" for s in steps):
        return "open"
    bad = [s for s in steps if s["state"] in ("missing", "qc_failed")]
    have = {(p["field"], p["fhour"]) for p in
            db.q("select field, fhour from products where source = %s and run = %s", (source, run))}
    lacking = sorted(_expected_products(src) - have)
    if not bad and lacking:
        # Accumulations or ensemble statistics still being derived; check again shortly.
        db.enqueue("derive", {"source": source, "run": run}, f"derive:{source}:{run}", priority=4)
        raise RetryLater(2.0, f"{len(lacking)} derived products pending")
    if bad:
        reason = "; ".join(f"{s['step']} {s['state'].replace('_', ' ')}" for s in bad[:4])
        if len(bad) > 4:
            reason += f" and {len(bad) - 4} more"
        db.q("update runs set state = 'held', held_reason = %s, finalized_at = now() where source = %s and run = %s",
             (reason, source, run))
        live = live_run(source)
        db.event("held", f"held ({reason}); the map stays on {live or 'nothing yet'}", source, run)
        state = "held"
    else:
        live_before = live_run(source)
        db.q("update runs set state = 'complete', finalized_at = now() where source = %s and run = %s",
             (source, run))
        state = "complete"
        if live_run(source) == run:
            db.q("update runs set promoted_at = now() where source = %s and run = %s", (source, run))
            if not src.is_obs:
                db.event("promoted", f"now the default map (was {live_before or 'none'})", source, run)
            if settings.warm_url and not src.is_obs:
                db.enqueue("warm", {"source": source, "run": run}, f"warm:{source}:{run}", priority=9)
    for k in products().list(f"scratch/{source}/{run}/"):
        products().delete(k)
    write_run(src, run)
    write_latest(src)
    if not src.is_obs or state == "held":
        write_index()
    return state


# ---------------------------------------------------------------------------- AI model run
def ai_run(source: str, run: str) -> int:
    """Run an in-house AI model from the parent model's analysis and post its output where the
    normal ingest path picks it up. Production ran Pangu-Weather (ONNX, GPU) from GFS analyses on a
    g5.xlarge Spot instance; here an adapter writes synthetic output in the same GRIB2 layout."""
    from wxtiles.ai import runner_for

    src = registry()[source]
    run_time = src.run_time(run)
    st = store(registry().mirrors[src.mirrors[0]].bucket)
    t0 = time.perf_counter()
    n = 0
    for step, data, idx in runner_for(src).run(run_time):
        key = src.key_for(run_time, step)
        st.put(key + ".idx", idx.encode())
        st.put(key, data)
        db.q("insert into upstream_events (mirror, key) values (%s, %s)", (src.mirrors[0], key))
        n += 1
    db.event("ai_run", f"{src.name} produced {n} steps in {time.perf_counter() - t0:.1f} s", source, run)
    return n


# ---------------------------------------------------------------------------- cache warm-up
WARM_AREA = (-170.0, 15.0, -50.0, 72.0)  # North America, where the desk looks first


def warm(source: str, run: str) -> int:
    """Request the newly promoted run's default-field tiles at low zooms over North America through
    the CDN, so the first person to open it does not wait for renders."""
    import math
    from concurrent.futures import ThreadPoolExecutor

    import httpx
    import rasterio
    from pyproj import Transformer

    src = registry()[source]
    field = src.fields[0]
    rows = db.q("select fhour, cog_key from products where source = %s and run = %s and field = %s order by fhour",
                (source, run, field))
    if not rows:
        return 0
    with rasterio.open(products().gdal_path(rows[0]["cog_key"])) as ds:
        b = ds.bounds
    to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    ax0, ay0 = to_merc.transform(WARM_AREA[0], WARM_AREA[1])
    ax1, ay1 = to_merc.transform(WARM_AREA[2], WARM_AREA[3])
    left, right = max(b.left, ax0), min(b.right, ax1)
    bottom, top = max(b.bottom, ay0), min(b.top, ay1)
    if left >= right or bottom >= top:
        return 0
    edge = 20_037_508.342789244
    urls = []
    for z in [int(v) for v in settings.warm_zooms.split(",") if v]:
        size = 2 * edge / 2**z
        xs = range(max(0, int(math.floor((left + edge) / size))), min(2**z - 1, int((right - 1 + edge) // size)) + 1)
        ys = range(max(0, int(math.floor((edge - top) / size))), min(2**z - 1, int((edge - bottom - 1) // size)) + 1)
        urls += [f"{settings.warm_url}/tiles/{source}/{run}/{field}/{r['fhour']}/{z}/{x}/{y}.png"
                 for r in rows for x in xs for y in ys]
    with httpx.Client(timeout=15.0) as client, ThreadPoolExecutor(8) as pool:
        list(pool.map(client.get, urls))
    return len(urls)
