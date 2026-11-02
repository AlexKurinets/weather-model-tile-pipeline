"""Pipeline health for the status page and `wxtiles status`: what each feed is showing, how old it
is, whether it is late or held, where its files came from, and what it cost to fetch them."""

from __future__ import annotations

from datetime import timedelta

from wxtiles import db
from wxtiles.registry import registry

LATE_AFTER = timedelta(minutes=5)


def _hours(td: timedelta) -> float:
    return round(td.total_seconds() / 3600.0, 1)


def status() -> dict:
    now = db.now()
    reg = registry()
    per_source = {r["source"]: r for r in db.q("""
        select source,
               count(*) filter (where state = 'done') as done,
               count(*) filter (where state = 'missing') as missing,
               count(*) filter (where state = 'qc_failed') as qc_failed,
               (sum(bytes_fetched) filter (where state = 'done'))::float8 as bytes_fetched,
               (sum(bytes_full) filter (where state = 'done'))::float8 as bytes_full,
               percentile_cont(0.5) within group (order by ingest_ms) filter (where state = 'done') as ingest_p50
        from steps group by source""")}
    primary_use = db.q("select source, mirror, count(*) as n from steps where mirror is not null group by 1, 2")
    events_by = {(r["source"], r["kind"]): r["n"] for r in
                 db.q("select source, kind, count(*) as n from events where source is not null group by 1, 2")}
    sources = []
    for s in reg:
        live = db.q1("""select run, run_time from runs where source = %s and state = 'complete'
                        order by run_time desc limit 1""", (s.id,))
        newest = db.q1("""select r.run, r.run_time, r.state, r.held_reason,
                                 count(st.*) filter (where st.state = 'done') as done, count(st.*) as total
                          from runs r join steps st using (source, run) where r.source = %s
                          group by r.run, r.run_time, r.state, r.held_reason order by r.run_time desc limit 1""",
                       (s.id,))
        # Late: the newest step that should be upstream by now has not been ingested, LATE_AFTER past
        # its expected time. Old missing hours of held runs do not count; they are already holds.
        last_due = db.q1("""select state, expected_at from steps where source = %s and expected_at <= %s
                            order by expected_at desc limit 1""", (s.id, now))
        is_late = bool(last_due) and last_due["state"] != "done" and last_due["expected_at"] + LATE_AFTER < now
        held = db.q1("""select run, held_reason from runs where source = %s and state = 'held'
                        order by run_time desc limit 1""", (s.id,))
        ps = per_source.get(s.id, {})
        n_fallback = sum(r["n"] for r in primary_use if r["source"] == s.id and r["mirror"] != s.mirrors[0])
        bf, bt = ps.get("bytes_fetched") or 0, ps.get("bytes_full") or 0
        sources.append({
            "id": s.id, "name": s.name, "category": s.category, "kind": s.kind, "is_obs": s.is_obs,
            "live_run": live["run"] if live else None,
            "live_age_h": _hours(now - live["run_time"]) if live else None,
            "newest_run": newest["run"] if newest else None,
            "newest_state": newest["state"] if newest else None,
            "newest_done": newest["done"] if newest else 0,
            "newest_total": newest["total"] if newest else 0,
            "late": is_late,
            "late_since": db.utc(last_due["expected_at"]) if is_late else None,
            "held_run": held["run"] if held else None,
            "held_reason": held["held_reason"] if held else None,
            "steps_done": ps.get("done", 0), "steps_missing": ps.get("missing", 0),
            "steps_qc_failed": ps.get("qc_failed", 0), "from_fallback": n_fallback,
            "fetched_share": round(bf / bt, 3) if bt else None,
            "ingest_ms_p50": int(ps["ingest_p50"]) if ps.get("ingest_p50") is not None else None,
            "failovers": events_by.get((s.id, "failover"), 0) + events_by.get((s.id, "integrity"), 0),
        })
    tot = db.q1("""select count(*) filter (where state = 'done') as done,
                          count(*) filter (where state = 'missing') as missing,
                          count(*) filter (where state = 'qc_failed') as qc_failed,
                          coalesce((sum(bytes_fetched) filter (where state = 'done'))::float8, 0) as bf,
                          coalesce((sum(bytes_full) filter (where state = 'done'))::float8, 0) as bt,
                          percentile_cont(0.5) within group (order by ingest_ms) filter (where state = 'done') as p50,
                          percentile_cont(0.95) within group (order by ingest_ms) filter (where state = 'done') as p95
                   from steps""")
    seeded = db.q1("select max(at_wall) as t from events where kind = 'seeded'")
    live_lat = None
    if seeded and seeded["t"]:
        triggered = [s.id for s in reg if s.trigger]
        live_lat = db.q1("""select count(*) as n,
                       percentile_cont(0.5) within group (order by extract(epoch from done_wall - posted_wall)) as p50,
                       percentile_cont(0.95) within group (order by extract(epoch from done_wall - posted_wall)) as p95
                            from steps where state = 'done' and posted_wall > %s and not (source = any(%s))""",
                         (seeded["t"], triggered))
    runs = db.q1("""select count(*) filter (where state = 'complete') as complete,
                           count(*) filter (where state = 'held') as held,
                           count(*) filter (where state = 'in_progress') as in_progress from runs""")
    ev = {r["kind"]: r["n"] for r in db.q("select kind, count(*) as n from events group by kind")}
    return {
        "clock": db.utc(now),
        "totals": {
            "steps_done": tot["done"], "steps_missing": tot["missing"], "steps_qc_failed": tot["qc_failed"],
            "bytes_fetched": int(tot["bf"]), "bytes_full": int(tot["bt"]),
            "fetched_share": round(tot["bf"] / tot["bt"], 3) if tot["bt"] else None,
            "ingest_ms_p50": int(tot["p50"]) if tot["p50"] is not None else None,
            "ingest_ms_p95": int(tot["p95"]) if tot["p95"] is not None else None,
            "runs_complete": runs["complete"], "runs_held": runs["held"], "runs_in_progress": runs["in_progress"],
            "failovers": ev.get("failover", 0) + ev.get("integrity", 0), "integrity_failures": ev.get("integrity", 0),
            "lease_takeovers": ev.get("lease_expired", 0),
            "sources_late": sum(1 for s in sources if s["late"]),
            "time_to_map_n": int(live_lat["n"]) if live_lat else 0,
            "time_to_map_s_p50": round(live_lat["p50"], 1) if live_lat and live_lat["p50"] is not None else None,
            "time_to_map_s_p95": round(live_lat["p95"], 1) if live_lat and live_lat["p95"] is not None else None,
            "queue": db.queue_depth(),
        },
        "sources": sources,
    }


def events(limit: int = 60, source: str | None = None) -> list[dict]:
    rows = db.q("""select id, at_sim, at_wall, source, run, kind, detail from events
                   where kind not in ('idle') and (%s::text is null or source = %s)
                   order by id desc limit %s""", (source, source, limit))
    return [{"id": r["id"], "at": db.utc(r["at_sim"]), "at_wall": db.utc(r["at_wall"]), "source": r["source"],
             "run": r["run"], "kind": r["kind"], "detail": r["detail"]} for r in rows]


def run_list(source: str) -> list[dict]:
    rows = db.q("""select r.run, r.run_time, r.state, r.held_reason,
                          count(s.*) filter (where s.state = 'done') as done, count(s.*) as total,
                          count(s.*) filter (where s.mirror is not null and s.mirror <> %s) as fallback
                   from runs r join steps s using (source, run) where r.source = %s
                   group by 1, 2, 3, 4 order by r.run_time desc limit 24""",
                (registry()[source].mirrors[0], source))
    return [{"run": r["run"], "run_time": db.utc(r["run_time"]), "state": r["state"], "held_reason": r["held_reason"],
             "steps_done": r["done"], "steps_expected": r["total"], "from_fallback": r["fallback"]} for r in rows]
