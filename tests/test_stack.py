"""End to end on a cut-down registry: seed with faults, ingest until idle, then check what the map
would show, the tiles, the status API, and a clock advance. Needs TEST_DATABASE_URL. Tests run in
file order and the last one moves the clock."""

from __future__ import annotations

import io
import json

import numpy as np
import pytest
from PIL import Image

from wxtiles import db
from wxtiles.cog import read_cog
from wxtiles.storage import products


def run_row(source, run):
    return db.q1("select * from runs where source = %s and run = %s", (source, run))


def live(source):
    from wxtiles.manifests import live_run

    return live_run(source)


def test_gfs_steps_missing_on_aws_came_from_nomads_and_the_run_was_promoted(stack):
    r = run_row("gfs", "2026102812")
    assert r["state"] == "complete" and live("gfs") == "2026102812"
    rows = db.q("select step, mirror from steps where source = 'gfs' and run = '2026102812' order by fhour")
    by = {x["step"]: x["mirror"] for x in rows}
    assert by["f048"] == by["f054"] == by["f060"] == "noaa-nomads"
    assert by["f042"] == "noaa-aws"
    assert db.q1("select count(*) as n from events where source = 'gfs' and kind = 'failover'")["n"] == 3


def test_truncated_hrrr_file_failed_integrity_and_was_refetched(stack):
    row = db.q1("""select s.mirror, r.state from steps s join runs r using (source, run)
                   where s.source = 'hrrr' and s.step = 'f007' and s.mirror = 'noaa-nomads'""")
    assert row is not None and row["state"] == "complete"
    ev = db.q1("select detail from events where source = 'hrrr' and kind = 'integrity'")
    assert "noaa-aws" in ev["detail"] and ("declares" in ev["detail"] or "expected" in ev["detail"])


def test_icon_run_missing_an_hour_is_held_and_the_map_stays_on_the_previous_run(stack):
    r = run_row("icon", "2026102812")
    assert r["state"] == "held" and "f084 missing" in r["held_reason"]
    assert live("icon") == "2026102806"
    assert run_row("icon", "2026102818")["state"] == "in_progress"
    latest = json.loads(products().get("manifests/icon/latest.json"))
    assert latest["live_run"] == "2026102806"
    assert {x["run"]: x["state"] for x in latest["runs"]}["2026102812"] == "held"


def test_frozen_gdps_temperature_fails_qc_and_holds_the_run(stack):
    r = run_row("gdps", "2026102812")
    assert r["state"] == "held" and "f048 qc failed" in r["held_reason"]
    qc = db.q1("""select field, check_name from qc_results where source = 'gdps' and not passed""")
    assert (qc["field"], qc["check_name"]) == ("t2m", "frozen")
    assert live("gdps") == "2026102800"


def test_pangu_ran_from_the_gfs_analysis_and_has_no_precipitation(stack):
    assert db.q1("select count(*) as n from events where source = 'pangu' and kind = 'ai_run'")["n"] == 2
    fields = {x["field"] for x in db.q("select distinct field from products where source = 'pangu'")}
    assert fields == {"t2m", "mslp", "wind10"}
    assert live("pangu") == "2026102812"


def test_ensemble_statistics_are_published_and_members_are_not(stack):
    for sid in ("gefs", "ens"):
        fields = {x["field"] for x in db.q("select distinct field from products where source = %s", (sid,))}
        assert fields == {"t2m_mean", "t2m_spread", "qpf_mean", "qpf_p1in"}, sid
    p = db.q1("select vmin, vmax from products where source = 'gefs' and field = 'qpf_p1in' and fhour = 24")
    assert 0 <= p["vmin"] <= p["vmax"] <= 100


def test_precipitation_totals_never_decrease_through_a_run(stack):
    keys = {x["fhour"]: x["cog_key"] for x in
            db.q("select fhour, cog_key from products where source = 'gfs' and run = '2026102812' and field = 'qpf'")}
    assert sorted(keys) == [6, 12, 18, 24, 30, 36, 42, 48, 54, 60]
    prev = None
    for fh in sorted(keys):
        a, _ = read_cog(products().get(keys[fh]))
        if prev is not None:
            assert np.nanmin(a - prev) >= -0.02  # 6-hour buckets chained into run totals
        prev = a


def test_late_feed_and_byte_savings_in_status(stack):
    from wxtiles.status import status

    s = status()
    by = {x["id"]: x for x in s["sources"]}
    assert by["goes_west"]["late"] and by["goes_west"]["steps_missing"] >= 3
    assert not by["gfs"]["late"]
    assert s["totals"]["fetched_share"] < 0.6
    assert s["totals"]["runs_held"] >= 2


def test_lease_expiry_hands_a_dead_workers_job_to_another(stack):
    db.q("""insert into jobs (kind, dedupe, payload, priority, state, lease_until, worker)
            values ('derive', 'derive:test:lease', '{"source": "gfs", "run": "2026102812"}', 0, 'running',
                    now() - interval '1 second', 'gone:1')""")
    job = db.claim()
    assert job["kind"] == "derive" and job["worker"] == "gone:1"
    db.finish(job["id"])
    assert db.q1("select count(*) as n from events where kind = 'lease_expired'")["n"] >= 1


# ---------------------------------------------------------------------------- API and dashboard
@pytest.fixture(scope="module")
def client(stack):
    from fastapi.testclient import TestClient

    from wxtiles.api import create_app

    return TestClient(create_app())


def test_tile_is_a_transparent_capable_png_cached_for_a_year(client):
    r = client.get("/tiles/gfs/2026102812/t2m/24/3/1/2.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert "immutable" in r.headers["cache-control"] and "max-age=31536000" in r.headers["cache-control"]
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (256, 256) and img.mode == "RGBA"
    assert np.asarray(img)[..., 3].max() > 0


def test_unpublished_hour_is_a_short_lived_404_and_outside_tiles_are_empty(client):
    r = client.get("/tiles/gfs/2026102812/t2m/66/3/1/2.png")
    assert r.status_code == 404 and "max-age=15" in r.headers["cache-control"]
    r = client.get("/tiles/hrrr/2026102819/t2m/3/3/6/2.png")  # East Asia, outside the HRRR domain
    assert r.status_code == 200 and np.asarray(Image.open(io.BytesIO(r.content)))[..., 3].max() == 0


def test_point_value_matches_the_cog(client):
    r = client.get("/api/point", params={"source": "rtma", "run": db.q1(
        "select run from runs where source = 'rtma' and state = 'complete' order by run_time desc")["run"],
        "field": "t2m", "fhour": 0, "lon": -95.0, "lat": 40.0}).json()
    assert r["units"] == "°F" and 0 < r["value"] < 100


def test_manifests_expire_quickly_and_list_every_source(client):
    r = client.get("/manifests/index.json")
    assert r.status_code == 200 and "max-age=" in r.headers["cache-control"]
    ids = [s["id"] for s in r.json()["sources"]]
    assert {"gfs", "icon", "gefs", "mrms", "goes_west"} <= set(ids)
    m = client.get("/manifests/gfs/2026102812.json").json()
    assert m["tiles"].startswith("/tiles/gfs/2026102812/") and m["fields"]["qpf"]["fhours"][0] == 6


def test_dashboard_pages_are_served_and_wired_to_the_api(client):
    page = client.get("/").text
    assert 'id="map"' in page and "maplibre-gl@4.7.1" in page and "app.js" in page
    js = client.get("/app.js").text
    assert "/manifests/" in js and "/tiles/" in js and "/api/point" in js
    st = client.get("/status.html").text
    assert 'id="feeds"' in st and "status.js" in st
    s = client.get("/api/status").json()
    assert {"clock", "totals", "sources"} <= set(s) and len(s["sources"]) >= 10
    assert client.get("/api/events?limit=5").status_code == 200


# ---------------------------------------------------------------------------- clock advance (last)
def test_advancing_the_clock_completes_the_next_icon_run_and_measures_time_to_map(stack):
    from wxtiles.status import status
    from wxtiles.synth.publisher import advance
    from wxtiles.worker import run_worker

    advance(60, processes=1)
    run_worker(processes=2, until_idle=True, tick_s=0.3, stop_after=600)
    assert run_row("icon", "2026102818")["state"] == "complete"
    assert live("icon") == "2026102818"  # the held 12Z run is skipped over, not waited on
    t = status()["totals"]
    assert t["time_to_map_n"] > 0 and t["time_to_map_s_p50"] is not None
