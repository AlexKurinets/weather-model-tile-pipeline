"""Scheduler and worker loop.

The scheduler registers the runs each source should have at the current time, turns new-object
notifications into ingest jobs, and polls for steps whose expected time has passed without a
notification (sources that publish none, and objects that reached only a fallback mirror). Workers
take jobs from the Postgres queue with SKIP LOCKED; a job whose worker dies is taken over when its
lease expires.
"""

from __future__ import annotations

import multiprocessing as mp
import time
import traceback
from datetime import timedelta

from wxtiles import db, pipeline
from wxtiles.manifests import write_index
from wxtiles.registry import registry
from wxtiles.settings import settings

NOTIFY_GRACE = timedelta(minutes=5)  # how long to wait for a notification before polling


def ensure_run(src, rt) -> bool:
    """Register a run and its steps (expected time and deadline per step). True if it was new."""
    run = src.run_id(rt)
    steps = src.steps()
    rows = db.q("""insert into runs (source, run, run_time, steps_expected) values (%s, %s, %s, %s)
                   on conflict do nothing returning run""", (src.id, run, rt, len(steps)))
    with db.conn() as c:
        c.cursor().executemany(
            """insert into steps (source, run, step, fhour, member, expected_at, deadline)
               values (%s, %s, %s, %s, %s, %s, %s) on conflict do nothing""",
            [(src.id, run, s.key, s.fhour, s.member, src.expected_at(rt, s), src.deadline(rt, s)) for s in steps])
    return bool(rows)


def ensure_runs() -> int:
    now = db.now()
    created = 0
    for src in registry():
        known = {r["run"] for r in db.q("select run from runs where source = %s", (src.id,))}
        for rt in src.runs_at(now):
            if src.run_id(rt) not in known:
                created += ensure_run(src, rt)
    return created


def consume_events(limit: int = 1000) -> int:
    rows = db.q("""update upstream_events set consumed = true where id in (
                       select id from upstream_events where not consumed order by id limit %s
                       for update skip locked) returning mirror, key""", (limit,))
    reg = registry()
    n = 0
    for r in rows:
        hit = reg.match_key(r["mirror"], r["key"])
        if not hit:
            continue
        src, rt, step = hit
        run = src.run_id(rt)
        if not db.q1("select 1 as x from runs where source = %s and run = %s", (src.id, run)):
            ensure_run(src, rt)  # the notification beat the scheduler to this run
        if db.q1("select 1 as x from steps where source = %s and run = %s and step = %s and state = 'expected'",
                 (src.id, run, step)):
            n += db.enqueue("ingest", {"source": src.id, "run": run, "step": step},
                            f"ingest:{src.id}:{run}:{step}", priority=3)
    return n


def poll_due() -> int:
    now = db.now()
    reg = registry()
    rows = db.q("""select s.source, s.run, s.step, s.fhour, s.expected_at from steps s
                   where s.state = 'expected' and s.expected_at <= %s
                     and not exists (select 1 from jobs j where j.dedupe = 'ingest:' || s.source || ':' || s.run
                                     || ':' || s.step and j.state in ('queued', 'running'))
                   order by s.expected_at limit 2000""", (now,))
    n = 0
    for r in rows:
        src = reg[r["source"]]
        if src.trigger:
            continue  # produced by our own job, which announces every file it writes
        if src.notify and r["expected_at"] + NOTIFY_GRACE > now:
            continue
        priority = 4 if r["fhour"] == 0 else 5
        n += db.enqueue("ingest", {"source": r["source"], "run": r["run"], "step": r["step"]},
                        f"ingest:{r['source']}:{r['run']}:{r['step']}", priority=priority)
    return n


def tick() -> dict:
    return {"runs": ensure_runs(), "events": consume_events(), "polled": poll_due()}


HANDLERS = {
    "ingest": lambda p: pipeline.ingest(p["source"], p["run"], p["step"]),
    "derive": lambda p: pipeline.derive(p["source"], p["run"]),
    "finalize": lambda p: pipeline.finalize(p["source"], p["run"]),
    "ai_run": lambda p: pipeline.ai_run(p["source"], p["run"]),
    "warm": lambda p: pipeline.warm(p["source"], p["run"]),
}


def run_one(kinds: list[str] | None = None) -> bool:
    job = db.claim(kinds=kinds)
    if job is None:
        return False
    try:
        HANDLERS[job["kind"]](job["payload"])
        db.finish(job["id"])
    except pipeline.RetryLater as e:
        db.retry_later(job["id"], e.delay_s, str(e))
    except Exception as e:  # a bug or an outage: retry a few times, then park it as failed
        detail = f"{type(e).__name__}: {e}"
        if job["attempts"] < 3:
            db.retry_later(job["id"], 5.0 * job["attempts"], detail)
        else:
            db.fail(job["id"], detail + "\n" + traceback.format_exc())
            p = job["payload"]
            db.event("job_failed", f"{job['kind']} failed: {detail}", p.get("source"), p.get("run"))
    return True


def publish_metrics() -> None:
    """Late feeds and held runs as CloudWatch metrics, which the alarms in deploy/aws watch."""
    if not settings.metrics_namespace:
        return
    import boto3

    from wxtiles.status import status

    t = status()["totals"]
    boto3.client("cloudwatch").put_metric_data(Namespace=settings.metrics_namespace, MetricData=[
        {"MetricName": "SourcesLate", "Value": t["sources_late"], "Unit": "Count"},
        {"MetricName": "RunsHeld", "Value": t["runs_held"], "Unit": "Count"},
        {"MetricName": "QueueReady", "Value": t["queue"], "Unit": "Count"}])


def _loop(index: int, until_idle: bool, tick_s: float, stop_after: float | None,
          kinds: list[str] | None = None) -> None:
    db.close_pool()
    try:
        _work(index, until_idle, tick_s, stop_after, kinds)
    finally:
        db.close_pool()


def _work(index: int, until_idle: bool, tick_s: float, stop_after: float | None, kinds: list[str] | None) -> None:
    started = time.monotonic()
    last_tick = 0.0
    last_metrics = 0.0
    scheduler = index == 0 and not kinds
    idle_ticks = 0
    while True:
        if stop_after and time.monotonic() - started > stop_after:
            return
        if scheduler and time.monotonic() - last_tick >= tick_s:
            t = tick()
            last_tick = time.monotonic()
            if time.monotonic() - last_metrics >= 60:
                publish_metrics()
                last_metrics = time.monotonic()
            if until_idle:
                busy = any(t.values()) or db.queue_depth() > 0
                idle_ticks = 0 if busy else idle_ticks + 1
                if idle_ticks >= 3:
                    db.q("insert into events (kind, detail) values ('idle', 'queue drained')")
                    return
        if not run_one(kinds):
            if until_idle and index > 0 and db.queue_depth() == 0 and _drained():
                return
            time.sleep(0.3)


def _drained() -> bool:
    return db.q1("select 1 as x from events where kind = 'idle' and at_wall > now() - interval '5 seconds'") \
        is not None


def run_worker(processes: int = 4, until_idle: bool = False, tick_s: float = 2.0,
               stop_after: float | None = None, kinds: list[str] | None = None) -> None:
    write_index()
    if processes <= 1:
        _loop(0, until_idle, tick_s, stop_after, kinds)
        return
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_loop, args=(i, until_idle, tick_s, stop_after, kinds), daemon=False)
             for i in range(processes)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
