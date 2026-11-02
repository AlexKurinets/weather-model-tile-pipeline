"""GRIB2 inventories: wgrib2-style .idx (NOAA) and JSON-lines .index (ECMWF open data).

Both say where each message starts, so a worker can download only the messages it maps with HTTP
range requests instead of the whole file. Messages are matched by variable and level text, never
by message number, because NCEP reorders files between model versions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from wxtiles.fields import FIELDS


@dataclass(frozen=True)
class IdxEntry:
    num: str  # "12" or "12.1" for a submessage
    start: int
    end: int | None  # inclusive; None = to end of file
    var: str  # "TMP", or ECMWF param "2t"
    level: str  # "2 m above ground", or ECMWF levtype "sfc"
    ftime: str  # "6 hour fcst", "0-6 hour acc fcst", or ECMWF step "6"
    member: int | None = None

    @property
    def window(self) -> tuple[int, int] | None:
        m = re.match(r"(\d+)-(\d+) hour acc", self.ftime)
        return (int(m.group(1)), int(m.group(2))) if m else None


def parse_wgrib2(text: str) -> list[IdxEntry]:
    rows = []
    for line in text.strip().splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        member = None
        for p in parts[6:]:
            m = re.match(r"ENS=([+-]\d+|low-res ctl|hi-res ctl)", p)
            if m:
                member = 0 if "ctl" in m.group(1) else abs(int(m.group(1)))
        rows.append([parts[0], int(parts[1]), parts[3], parts[4], parts[5], member])
    # A message runs to the byte before the next message that starts later. Submessages (12.1,
    # 12.2: U and V packed in one message) share an offset and therefore a range.
    starts = sorted({r[1] for r in rows})
    nxt = {s: (starts[i + 1] - 1 if i + 1 < len(starts) else None) for i, s in enumerate(starts)}
    return [IdxEntry(r[0], r[1], nxt[r[1]], r[2], r[3], r[4], r[5]) for r in rows]


def parse_ecmwf(text: str) -> list[IdxEntry]:
    out = []
    for i, line in enumerate(text.strip().splitlines()):
        d = json.loads(line)
        member = int(d["number"]) if "number" in d else None
        out.append(IdxEntry(str(i + 1), int(d["_offset"]), int(d["_offset"]) + int(d["_length"]) - 1,
                            d["param"], d.get("levtype", ""), str(d.get("step", "")), member))
    return out


def parse(text: str, style: str) -> list[IdxEntry]:
    return parse_ecmwf(text) if style == "ecmwf" else parse_wgrib2(text)


def select(entries: list[IdxEntry], raw: list[str], style: str, idx_names: dict | None = None,
           members: list[int] | None = None) -> dict[str, list[IdxEntry]]:
    """Pick the entries for each raw field. Raises KeyError naming any field the file lacks."""
    idx_names = idx_names or {}
    chosen: dict[str, list[IdxEntry]] = {}
    for name in raw:
        f = FIELDS[name]
        if style == "ecmwf":
            hits = [e for e in entries if e.var in f.ecmwf]
            if members is not None:
                hits = [e for e in hits if e.member in members]
        else:
            wanted = [idx_names[name]] if name in idx_names else list(f.idx)
            hits = [e for e in entries if f"{e.var}:{e.level}" in wanted]
            if members is not None:
                hits = [e for e in hits if e.member in members]
            if f.accumulated and hits:
                hits = _pick_window(hits)
        if not hits:
            raise KeyError(f"{name} not in inventory")
        chosen[name] = hits
    return chosen


def _pick_window(hits: list[IdxEntry]) -> list[IdxEntry]:
    """For an accumulation offered over several windows (HRRR has 0-7 h and 6-7 h at f07), take
    the run total if there is one, else the window ending at this step, one per member."""
    by_member: dict[int | None, list[IdxEntry]] = {}
    for h in hits:
        by_member.setdefault(h.member, []).append(h)
    out = []
    for _, hs in by_member.items():
        totals = [h for h in hs if h.window and h.window[0] == 0]
        if totals:
            out.append(max(totals, key=lambda h: h.window[1]))
        else:
            out.append(max(hs, key=lambda h: (h.window or (0, 0))[0]))
    return out


def byte_ranges(entries: list[IdxEntry]) -> list[tuple[int, int | None]]:
    """Merge the chosen messages into as few contiguous ranges as possible."""
    spans = sorted({(e.start, e.end) for e in entries}, key=lambda s: s[0])
    merged: list[list] = []
    for s, e in spans:
        if merged and merged[-1][1] is not None and s == merged[-1][1] + 1:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]
