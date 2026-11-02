"""Synthetic files in each real format, decoded back, and fetched through mirror failover."""

from datetime import UTC, datetime

import httpx
import numpy as np
import pytest

from wxtiles.fetch import NotAvailable, fetch_step, index_key
from wxtiles.gribio import IntegrityError, decode_grib, decode_netcdf_abi, decompress, split_messages
from wxtiles.registry import Step, load_registry
from wxtiles.storage import HttpMirror, LocalStore, StoreMirror
from wxtiles.synth.publisher import _truncate_last_mapped
from wxtiles.synth.writers import PrecipBook, abi_file, indexed_file, mrms_file, per_field_file

REG = load_registry()
CYCLE = datetime(2026, 10, 28, 12, tzinfo=UTC)


def step(src, fhour, member=None):
    for s in src.steps():
        if s.fhour == fhour and s.member == member:
            return s
    raise KeyError(fhour)


def test_gfs_file_round_trips_and_subsetting_skips_most_bytes():
    src = REG["gfs"]
    data, idx = indexed_file(src, CYCLE, step(src, 12), PrecipBook())
    msgs = [d for m in split_messages(data) if (d := decode_grib(m))]
    names = {d.name: d for d in msgs}
    assert set(names) == {"t2m", "mslp", "qpf", "u10", "v10"}
    assert names["qpf"].window == (6, 12)  # GFS buckets reset every 6 hours
    assert 200 < np.nanmean(names["t2m"].values) < 320
    assert names["t2m"].grid.shape == (173, 361)
    assert idx.count("\n") == len(split_messages(data))


def test_hrrr_lambert_grid_and_mslma():
    src = REG["hrrr"]
    data, idx = indexed_file(src, CYCLE, step(src, 3), PrecipBook())
    assert ":MSLMA:mean sea level:" in idx and ":PRMSL:" not in idx
    decoded = [x for m in split_messages(data) if (x := decode_grib(m))]
    d = {x.name: x for x in decoded}
    assert d["mslp"].grid.kind == "lambert" and "refc" in d
    # Like the real HRRR, f03 carries the run total and the last hour; the inventory picks the total.
    assert sorted(x.window for x in decoded if x.name == "qpf") == [(0, 3), (2, 3)]


def test_icon_bzip2_per_variable_and_mrms_local_discipline():
    src = REG["icon"]
    b = per_field_file(src, CYCLE, step(src, 12), "TOT_PREC", PrecipBook())
    d = decode_grib(split_messages(decompress(b, "bzip2"))[0])
    assert d.name == "qpf" and d.window == (0, 12)
    m = mrms_file(REG["mrms"], datetime(2026, 10, 28, 20, 30, tzinfo=UTC))
    r = decode_grib(split_messages(decompress(m, "gzip"))[0])
    assert r.name == "refl" and np.isnan(r.values).any() and np.nanmax(r.values) > 30


def test_goes_abi_netcdf():
    d = decode_netcdf_abi(abi_file(REG["goes_east"], datetime(2026, 10, 28, 20, 30, tzinfo=UTC)))
    assert d.grid.kind == "geos" and 180 < np.nanmin(d.values) < np.nanmax(d.values) < 320


def test_integrity_checks_catch_truncation_and_bad_bytes():
    src = REG["hrrr"]
    data, idx = indexed_file(src, CYCLE, step(src, 7), PrecipBook())
    cut = _truncate_last_mapped(src, data, idx)
    with pytest.raises(IntegrityError):
        split_messages(cut)
    with pytest.raises(IntegrityError, match="expected"):
        split_messages(data[:100], expected_len=200)
    with pytest.raises(IntegrityError, match="GRIB marker"):
        split_messages(b"HTML" + data[4:])
    with pytest.raises(IntegrityError):
        decompress(b"not gzip", "gzip")


def _mirrors(tmp_path, src):
    return {name: StoreMirror(name, LocalStore(REG.mirrors[name].bucket, root=str(tmp_path))) for name in src.mirrors}


def _post(clients, name, key, data, idx=None, src=None):
    st = clients[name].st
    st.put(key, data)
    if idx is not None:
        st.put(index_key(src, key), idx.encode())


def test_fallback_mirror_when_primary_lacks_the_object(tmp_path):
    src = REG["gfs"]
    s = step(src, 48)
    data, idx = indexed_file(src, CYCLE, s, PrecipBook())
    clients = _mirrors(tmp_path, src)
    _post(clients, "noaa-nomads", src.key_for(CYCLE, s), data, idx, src)
    got = fetch_step(src, CYCLE, s, clients)
    assert got.mirror == "noaa-nomads" and got.skipped == ["noaa-aws: not posted"]
    assert got.bytes_fetched < 0.25 * got.bytes_full


def test_truncated_primary_fails_integrity_and_falls_back(tmp_path):
    src = REG["hrrr"]
    s = step(src, 7)
    data, idx = indexed_file(src, CYCLE, s, PrecipBook())
    clients = _mirrors(tmp_path, src)
    key = src.key_for(CYCLE, s)
    _post(clients, "noaa-aws", key, _truncate_last_mapped(src, data, idx), idx, src)
    _post(clients, "noaa-nomads", key, data, idx, src)
    got = fetch_step(src, CYCLE, s, clients)
    assert got.mirror == "noaa-nomads" and "noaa-aws" in got.skipped[0] and "not posted" not in got.skipped[0]


def test_nothing_anywhere_raises_with_reasons(tmp_path):
    src = REG["ifs"]
    with pytest.raises(NotAvailable) as e:
        fetch_step(src, CYCLE, step(src, 6), _mirrors(tmp_path, src))
    assert len(e.value.reasons) == 2


def test_http_mirror_uses_range_requests():
    blob = bytes(range(256)) * 4
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.headers.get("range"))
        if req.url.path.endswith("missing"):
            return httpx.Response(404)
        if req.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(blob)),
                                                "last-modified": "Wed, 28 Oct 2026 16:05:00 GMT"})
        start, end = req.headers["range"].split("=")[1].split("-")
        return httpx.Response(206, content=blob[int(start):int(end) + 1])

    m = HttpMirror("noaa-nomads", "http://mirror.example.com/x", transport=httpx.MockTransport(handler))
    assert m.head("missing") is None
    assert m.head("f").size == 1024
    assert m.get("f", 10, 13) == bytes([10, 11, 12, 13])
    assert "bytes=10-13" in seen


def test_ensemble_in_file_selects_members_by_number(tmp_path):
    src = REG["ens"]
    s = step(src, 12)
    data, idx = indexed_file(src, CYCLE, s, PrecipBook(), members=list(range(src.members)))
    clients = _mirrors(tmp_path, src)
    _post(clients, "ecmwf-portal", src.key_for(CYCLE, s), data, idx, src)
    got = fetch_step(src, CYCLE, s, clients)
    members = {d.member for m in got.messages if (d := decode_grib(m))}
    assert members == set(range(src.members))


def test_keys_parse_back_to_run_and_step():
    for src in REG:
        rt = datetime(2026, 10, 28, 12, 30 if src.is_obs and src.cadence < 60 else 0, tzinfo=UTC)
        s = src.steps()[min(3, len(src.steps()) - 1)]
        var = src.variable_names()[0] if src.layout == "per_field" else None
        key = src.key_for(rt, s, var)
        hit = REG.match_key(src.mirrors[0], key)
        assert hit is not None, (src.id, key)
        assert hit[0].id == src.id and hit[1] == rt and hit[2] == s.key, (src.id, key, hit)


def test_runs_at_tracks_complete_runs_and_the_one_still_posting():
    icon = REG["icon"]
    t = datetime(2026, 10, 28, 20, 40, tzinfo=UTC)
    runs = icon.runs_at(t)
    assert [r.hour for r in runs] == [6, 12, 18]  # 18Z started posting at 20:30
    assert icon.last_expected_at(runs[-1]) > t >= icon.expected_at(runs[-1], Step("f000", 0, None, 0))
