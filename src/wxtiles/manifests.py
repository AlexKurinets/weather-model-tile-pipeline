"""Small JSON manifests in the products bucket that tell a map what exists.

  manifests/index.json            every source, its category, fields and live run
  manifests/{source}/latest.json  the live run plus recent runs (or frames) with their state
  manifests/{source}/{run}.json   one run: fields, forecast hours per field, tile URL template

Tile URLs carry the run, so a tile never changes once rendered and can be cached for a year. Only
these manifests expire, every WX_MANIFEST_TTL seconds (60 in production), which is how a new run
reaches every open map without a CDN invalidation.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

from wxtiles import db
from wxtiles.colormaps import legend
from wxtiles.fields import FIELDS
from wxtiles.registry import Source, registry
from wxtiles.settings import settings
from wxtiles.storage import products

RECENT_RUNS = 6


@contextmanager
def run_lock(source: str, run: str):
    with db.conn() as c:
        c.execute("select pg_advisory_lock(hashtext(%s))", (f"{source}/{run}",))
        try:
            yield c
        finally:
            c.execute("select pg_advisory_unlock(hashtext(%s))", (f"{source}/{run}",))


def _put(key: str, doc: dict) -> None:
    products().put(key, json.dumps(doc, separators=(",", ":")).encode(), "application/json",
                   cache_control=f"public, max-age={settings.manifest_ttl}")


def live_run(source: str) -> str | None:
    row = db.q1("select run from runs where source = %s and state = 'complete' order by run_time desc limit 1",
                (source,))
    return row["run"] if row else None


def field_doc(name: str) -> dict:
    f = FIELDS[name]
    return {"label": f.label, "units": f.units, "colormap": f.colormap, "legend": legend(f.colormap)}


def run_doc(src: Source, run: str) -> dict:
    r = db.q1("select * from runs where source = %s and run = %s", (src.id, run))
    prods = db.q("select field, fhour, vmin, vmax from products where source = %s and run = %s order by fhour",
                 (src.id, run))
    steps = db.q1("""select count(*) filter (where state = 'done') as done,
                            count(*) filter (where state in ('missing', 'qc_failed')) as bad,
                            count(*) as total from steps where source = %s and run = %s""", (src.id, run))
    fields: dict[str, dict] = {}
    for p in prods:
        fields.setdefault(p["field"], {"fhours": []})["fhours"].append(p["fhour"])
    return {
        "source": src.id, "run": run, "run_time": db.utc(r["run_time"]) if r else None,
        "state": r["state"] if r else "in_progress", "held_reason": r["held_reason"] if r else None,
        "steps_done": steps["done"], "steps_bad": steps["bad"], "steps_expected": steps["total"],
        "fields": fields,
        "tiles": f"/tiles/{src.id}/{run}/{{field}}/{{fhour}}/{{z}}/{{x}}/{{y}}.png",
    }


def write_run(src: Source, run: str) -> dict:
    with run_lock(src.id, run):
        doc = run_doc(src, run)
        _put(f"manifests/{src.id}/{run}.json", doc)
    return doc


def latest_doc(src: Source) -> dict:
    n = src.keep_frames if src.is_obs else RECENT_RUNS
    runs = db.q("""select r.run, r.run_time, r.state, r.held_reason,
                          count(s.*) filter (where s.state = 'done') as done, count(s.*) as total
                   from runs r join steps s using (source, run)
                   where r.source = %s group by r.run, r.run_time, r.state, r.held_reason
                   order by r.run_time desc limit %s""", (src.id, n))
    return {
        "source": src.id, "name": src.name, "live_run": live_run(src.id), "is_obs": src.is_obs,
        "runs": [{"run": r["run"], "run_time": db.utc(r["run_time"]), "state": r["state"],
                  "held_reason": r["held_reason"], "steps_done": r["done"], "steps_expected": r["total"]}
                 for r in reversed(runs) if r["done"] > 0 or r["state"] != "in_progress"],
    }


def write_latest(src: Source) -> dict:
    doc = latest_doc(src)
    _put(f"manifests/{src.id}/latest.json", doc)
    return doc


def write_index() -> dict:
    reg = registry()
    order = ["global", "ai", "regional", "ensemble", "observed"]
    sources = []
    for s in sorted(reg, key=lambda s: (order.index(s.category), list(reg.sources).index(s.id))):
        sources.append({"id": s.id, "name": s.name, "center": s.center, "category": s.category, "kind": s.kind,
                        "is_obs": s.is_obs, "live_run": live_run(s.id),
                        "fields": {f: field_doc(f) for f in s.fields}})
    doc = {"generated": db.utc(db.now()), "sources": sources}
    _put("manifests/index.json", doc)
    return doc
