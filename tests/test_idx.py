import json

import pytest

from wxtiles.idx import byte_ranges, parse_ecmwf, parse_wgrib2, select

WGRIB2 = """1:0:d=2026102812:HGT:500 mb:6 hour fcst:
2:1000:d=2026102812:TMP:2 m above ground:6 hour fcst:
3:1800:d=2026102812:PRMSL:mean sea level:6 hour fcst:
4:2600:d=2026102812:APCP:surface:0-6 hour acc fcst:
5:3000:d=2026102812:APCP:surface:5-6 hour acc fcst:
6.1:3400:d=2026102812:UGRD:10 m above ground:6 hour fcst:
6.2:3400:d=2026102812:VGRD:10 m above ground:6 hour fcst:
7:4100:d=2026102812:RH:850 mb:6 hour fcst:
"""


def test_wgrib2_ranges_end_before_next_message_and_last_is_open():
    e = {x.num: x for x in parse_wgrib2(WGRIB2)}
    assert (e["2"].start, e["2"].end) == (1000, 1799)
    assert e["7"].end is None
    # Submessages share an offset, so U and V come back as the same range.
    assert (e["6.1"].start, e["6.1"].end) == (e["6.2"].start, e["6.2"].end) == (3400, 4099)


def test_select_matches_by_text_not_position():
    shuffled = "\n".join(reversed(WGRIB2.strip().splitlines()))
    a = select(parse_wgrib2(WGRIB2), ["t2m"], "wgrib2")["t2m"][0]
    b = select(parse_wgrib2(shuffled), ["t2m"], "wgrib2")["t2m"][0]
    assert a.var == b.var == "TMP" and a.start == b.start == 1000


def test_accumulation_prefers_run_total_over_last_hour():
    chosen = select(parse_wgrib2(WGRIB2), ["qpf"], "wgrib2")["qpf"]
    assert len(chosen) == 1 and chosen[0].window == (0, 6)


def test_bucket_window_is_taken_when_there_is_no_total():
    idx = "1:0:d=2026102812:APCP:surface:6-12 hour acc fcst:\n2:500:d=2026102812:TMP:2 m above ground:12 hour fcst:\n"
    assert select(parse_wgrib2(idx), ["qpf"], "wgrib2")["qpf"][0].window == (6, 12)


def test_source_override_for_hrrr_mslma():
    idx = "1:0:d=2026102812:MSLMA:mean sea level:anl:\n2:900:d=2026102812:TMP:2 m above ground:anl:\n"
    with pytest.raises(KeyError):
        select(parse_wgrib2(idx), ["mslp"], "wgrib2", {"mslp": "PRMSL:mean sea level"})
    assert select(parse_wgrib2(idx), ["mslp"], "wgrib2", {"mslp": "MSLMA:mean sea level"})["mslp"][0].start == 0


def test_adjacent_messages_merge_into_one_request():
    entries = parse_wgrib2(WGRIB2)
    chosen = select(entries, ["t2m", "mslp", "u10", "v10"], "wgrib2")
    ranges = byte_ranges([e for es in chosen.values() for e in es])
    assert ranges == [(1000, 2599), (3400, 4099)]


def test_ecmwf_index_with_members():
    lines = [json.dumps({"param": "2t", "levtype": "sfc", "step": "12", "number": str(m), "_offset": 100 * m,
                         "_length": 100}) for m in range(4)]
    lines.append(json.dumps({"param": "t", "levtype": "pl", "step": "12", "_offset": 400, "_length": 50}))
    entries = parse_ecmwf("\n".join(lines))
    chosen = select(entries, ["t2m"], "ecmwf", members=[0, 1, 2])["t2m"]
    assert [e.member for e in chosen] == [0, 1, 2]
    assert byte_ranges(chosen) == [(0, 299)]


def test_missing_field_names_the_field():
    with pytest.raises(KeyError, match="refc"):
        select(parse_wgrib2(WGRIB2), ["refc"], "wgrib2")
