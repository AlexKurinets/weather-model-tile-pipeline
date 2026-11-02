"""Postgres access: a small connection pool, schema setup, the clock, the job queue and events."""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime, timedelta
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from wxtiles.settings import settings

_pool: ConnectionPool | None = None
_pool_pid: int | None = None


def pool() -> ConnectionPool:
    global _pool, _pool_pid
    if _pool is None or _pool_pid != os.getpid():
        _pool = ConnectionPool(settings.database_url, min_size=1, max_size=8, open=True,
                               kwargs={"row_factory": dict_row, "autocommit": True})
        _pool_pid = os.getpid()
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def conn():
    return pool().connection()


def q(sql: str, params: tuple | dict = ()) -> list[dict]:
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


def q1(sql: str, params: tuple | dict = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def init_schema() -> None:
    sql = resources.files("wxtiles").joinpath("schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(settings.database_url, autocommit=True) as c:
        c.execute(sql)


def reset_all() -> None:
    q("truncate runs, steps, products, qc_results, jobs, upstream_events, events, clock restart identity")


# ---------------------------------------------------------------------------- clock
def now() -> datetime:
    """Pipeline time. In sim mode the synthetic day's clock lives in the database so every
    process agrees on it; in system mode it is wall-clock UTC."""
    if settings.clock == "system":
        return datetime.now(UTC)
    row = q1("select sim_now from clock where id = 1")
    if row is None:
        return datetime.fromisoformat(settings.sim_start.replace("Z", "+00:00"))
    return row["sim_now"]


def set_clock(t: datetime) -> None:
    q("insert into clock (id, sim_now) values (1, %s) on conflict (id) do update set sim_now = excluded.sim_now",
      (t,))


# ---------------------------------------------------------------------------- events
def event(kind: str, detail: str, source: str | None = None, run: str | None = None) -> None:
    q("insert into events (at_sim, source, run, kind, detail) values (%s, %s, %s, %s, %s)",
      (now(), source, run, kind, detail))


# ---------------------------------------------------------------------------- jobs
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def enqueue(kind: str, payload: dict, dedupe: str | None = None, priority: int = 5,
            delay_s: float = 0.0) -> bool:
    """Queue a job unless an identical one is already queued or running."""
    dedupe = dedupe or f"{kind}:{json.dumps(payload, sort_keys=True)}"
    rows = q("""insert into jobs (kind, dedupe, payload, priority, run_after)
                values (%s, %s, %s, %s, now() + make_interval(secs => %s))
                on conflict (dedupe) where state in ('queued', 'running') do nothing
                returning id""",
             (kind, dedupe, json.dumps(payload), priority, delay_s))
    return bool(rows)


def claim(lease_s: int | None = None, kinds: list[str] | None = None) -> dict | None:
    """Take the next ready job. A job whose worker died (lease expired) is taken over, which is
    how a lost Spot instance's work gets finished elsewhere."""
    lease_s = lease_s or settings.lease_seconds
    with conn() as c, c.transaction():
        row = c.execute(
            """select id, kind, payload, state, worker, attempts from jobs
               where ((state = 'queued' and run_after <= now()) or (state = 'running' and lease_until < now()))
                 and (%s::text[] is null or kind = any(%s::text[]))
               order by priority, run_after, id
               for update skip locked limit 1""", (kinds, kinds)).fetchone()
        if row is None:
            return None
        if row["state"] == "running":
            c.execute("insert into events (kind, detail) values ('lease_expired', %s)",
                      (f"job {row['id']} ({row['kind']}) taken over from {row['worker']}",))
        c.execute("""update jobs set state = 'running', worker = %s, lease_until = now() + make_interval(secs => %s),
                     attempts = attempts + 1, updated_at = now() where id = %s""",
                  (WORKER_ID, lease_s, row["id"]))
    return row


def finish(job_id: int) -> None:
    q("update jobs set state = 'done', lease_until = null, updated_at = now() where id = %s", (job_id,))


def retry_later(job_id: int, delay_s: float, error: str = "") -> None:
    q("""update jobs set state = 'queued', lease_until = null, run_after = now() + make_interval(secs => %s),
         last_error = %s, updated_at = now() where id = %s""", (delay_s, error[:500], job_id))


def fail(job_id: int, error: str) -> None:
    q("update jobs set state = 'failed', lease_until = null, last_error = %s, updated_at = now() where id = %s",
      (error[:2000], job_id))


def queue_depth() -> int:
    row = q1("select count(*) as n from jobs where state = 'running' or (state = 'queued' and run_after <= now())")
    return int(row["n"]) if row else 0


def utc(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def minutes(td: timedelta) -> float:
    return round(td.total_seconds() / 60.0, 1)
