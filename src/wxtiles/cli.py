"""Command line: wxtiles seed | worker | advance | status | events | runs | qc."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from wxtiles import db
from wxtiles.settings import settings


def _seeded() -> bool:
    return db.q1("select 1 as x from events where kind = 'seeded'") is not None


def cmd_seed(a) -> int:
    from wxtiles.synth.publisher import seed

    db.init_schema()
    if _seeded() and not a.force:
        print("already seeded; the synthetic clock is at", db.utc(db.now()))
        return 0
    t0 = datetime.fromisoformat((a.at or settings.sim_start).replace("Z", "+00:00"))
    counts = seed(t0, processes=a.processes)
    for sid, n in sorted(counts.items()):
        print(f"  {sid:10s} {n:5d} objects")
    print(f"seeded {sum(counts.values())} upstream objects; clock at {db.utc(t0)}")
    return 0


def cmd_worker(a) -> int:
    from wxtiles.worker import run_worker

    db.init_schema()
    kinds = a.kinds.split(",") if a.kinds else None
    run_worker(processes=a.processes, until_idle=a.until_idle, stop_after=a.stop_after, kinds=kinds)
    return 0


def cmd_advance(a) -> int:
    from wxtiles.synth.publisher import advance

    t, counts = advance(a.minutes, primary_down=a.primary_down, processes=a.processes)
    posted = {k: v for k, v in counts.items() if v}
    print(f"clock now {db.utc(t)}; posted " + (", ".join(f"{k} {v}" for k, v in sorted(posted.items())) or "nothing")
          + (" (AWS bucket down)" if a.primary_down else ""))
    return 0


def cmd_status(a) -> int:
    from wxtiles.status import status

    s = status()
    t = s["totals"]
    print(f"clock {s['clock']}   queue {t['queue']}   runs: {t['runs_complete']} complete, {t['runs_held']} held, "
          f"{t['runs_in_progress']} in progress")
    share = f"{t['fetched_share']:.0%}" if t["fetched_share"] is not None else "-"
    print(f"steps: {t['steps_done']} done, {t['steps_missing']} missing, {t['steps_qc_failed']} failed QC; "
          f"downloaded {t['bytes_fetched'] / 1e6:.0f} MB of {t['bytes_full'] / 1e6:.0f} MB ({share}); "
          f"failovers {t['failovers']}; ingest p50 {t['ingest_ms_p50']} ms")
    if t["time_to_map_n"]:
        print(f"posted to on the map (steps posted after the seed): median {t['time_to_map_s_p50']} s, "
              f"p95 {t['time_to_map_s_p95']} s over {t['time_to_map_n']} steps")
    print()
    print(f"{'source':10s} {'live run':13s} {'age h':>6s}  {'newest':13s} {'state':12s} {'steps':>9s} "
          f"{'fetched':>8s} {'fallback':>8s}  notes")
    for r in s["sources"]:
        notes = []
        if r["late"]:
            notes.append(f"LATE since {r['late_since'][11:16]}")
        if r["held_run"]:
            notes.append(f"held {r['held_run']}: {r['held_reason']}")
        share = f"{r['fetched_share']:.0%}" if r["fetched_share"] is not None else "-"
        age = f"{r['live_age_h']:.1f}" if r["live_age_h"] is not None else "-"
        print(f"{r['id']:10s} {r['live_run'] or '-':13s} {age:>6s}  {r['newest_run'] or '-':13s} "
              f"{r['newest_state'] or '-':12s} {r['newest_done']:>4d}/{r['newest_total']:<4d} {share:>8s} "
              f"{r['from_fallback']:>8d}  {'; '.join(notes)}")
    return 0


def cmd_events(a) -> int:
    from wxtiles.status import events

    for e in reversed(events(a.limit, a.source)):
        print(f"{(e['at'] or '')[5:16]}  {e['kind']:13s} {e['source'] or '':10s} {e['run'] or '':13s} {e['detail']}")
    return 0


def cmd_runs(a) -> int:
    from wxtiles.status import run_list

    for r in run_list(a.source):
        print(f"{r['run']:13s} {r['state']:12s} {r['steps_done']:>4d}/{r['steps_expected']:<4d} "
              f"fallback {r['from_fallback']:<3d} {r['held_reason'] or ''}")
    return 0


def cmd_qc(a) -> int:
    rows = db.q("""select source, run, step, field, check_name, value, detail from qc_results
                   where not passed and (%s::text is null or source = %s) order by id""", (a.source, a.source))
    for r in rows:
        print(f"{r['source']:10s} {r['run']:13s} {r['step']:9s} {r['field']:7s} {r['check_name']:11s} {r['detail']}")
    if not rows:
        print("no failed checks")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wxtiles")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="create the schema and buckets, set the clock, post synthetic upstream files")
    s.add_argument("--at", help="synthetic clock start, ISO 8601 (default WX_SIM_START)")
    s.add_argument("--processes", type=int, default=4)
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("worker", help="run the scheduler and ingest workers")
    s.add_argument("--processes", type=int, default=4)
    s.add_argument("--until-idle", action="store_true", help="exit once nothing is left to do")
    s.add_argument("--stop-after", type=float, default=None, help="exit after this many seconds")
    s.add_argument("--kinds", help="only take these job kinds, comma separated (GPU nodes: ai_run)")
    s.set_defaults(fn=cmd_worker)
    s = sub.add_parser("advance", help="move the synthetic clock forward and post what falls in the window")
    s.add_argument("--minutes", type=float, required=True)
    s.add_argument("--primary-down", action="store_true", help="keep new objects off the AWS bucket")
    s.add_argument("--processes", type=int, default=4)
    s.set_defaults(fn=cmd_advance)
    s = sub.add_parser("status", help="what every feed is showing, and the pipeline's numbers")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("events", help="failovers, holds, promotions and other events")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--source")
    s.set_defaults(fn=cmd_events)
    s = sub.add_parser("runs", help="runs of one source")
    s.add_argument("source")
    s.set_defaults(fn=cmd_runs)
    s = sub.add_parser("qc", help="failed QC checks")
    s.add_argument("--source")
    s.set_defaults(fn=cmd_qc)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
