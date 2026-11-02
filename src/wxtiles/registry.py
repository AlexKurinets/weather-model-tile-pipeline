"""Source registry: what each feed is, where it lives, when it posts, and which steps a run has."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

import yaml

from wxtiles.settings import settings

FORECAST_KINDS = ("forecast", "ensemble")
OBS_KINDS = ("analysis", "observation")


@dataclass(frozen=True)
class Mirror:
    name: str
    protocol: str  # "s3" or "http"
    bucket: str


@dataclass(frozen=True)
class Step:
    """One unit of ingest: a forecast hour, one member's forecast hour, or one observation frame."""

    key: str  # "f006", "m03/f006", "f000" for a frame
    fhour: int
    member: int | None
    index: int  # position in the posting order, drives the expected time


@dataclass
class Source:
    id: str
    name: str
    center: str
    category: str
    kind: str
    layout: str
    grid_name: str
    grid: dict
    mirrors: list[str]
    path: str
    fields: list[str]
    posting: dict
    notify: bool = False
    index: str = "none"
    compression: str = "none"
    format: str = "grib2"
    cycles: list[int] = field(default_factory=list)
    fhours_spec: list[int] = field(default_factory=list)
    keep_cycles: int = 2
    cadence: int = 0
    keep_frames: int = 0
    members: int = 0
    member_layout: str = ""
    member_fields: list[str] = field(default_factory=list)
    precip: dict = field(default_factory=dict)
    variables: dict = field(default_factory=dict)
    idx_names: dict = field(default_factory=dict)
    trigger: dict | None = None
    satellite: str = ""
    synth: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ steps and runs
    @property
    def is_obs(self) -> bool:
        return self.kind in OBS_KINDS

    @property
    def fhours(self) -> list[int]:
        if self.is_obs:
            return [0]
        start, stop, step = self.fhours_spec
        return list(range(start, stop + 1, step))

    @property
    def decode_fields(self) -> list[str]:
        """Fields the ingest step decodes (members carry raw fields, the run publishes statistics)."""
        return self.member_fields if self.kind == "ensemble" else self.fields

    def steps(self) -> list[Step]:
        out = []
        for i, fh in enumerate(self.fhours):
            if self.kind == "ensemble" and self.member_layout == "per_file":
                for m in range(self.members):
                    out.append(Step(f"m{m:02d}/f{fh:03d}", fh, m, i))
            else:
                out.append(Step(f"f{fh:03d}", fh, None, i))
        return out

    def run_id(self, t: datetime) -> str:
        return t.strftime("%Y%m%d%H%M") if self.is_obs else t.strftime("%Y%m%d%H")

    def run_time(self, run: str) -> datetime:
        fmt = "%Y%m%d%H%M" if self.is_obs else "%Y%m%d%H"
        return datetime.strptime(run, fmt).replace(tzinfo=UTC)

    def expected_at(self, run_time: datetime, step: Step) -> datetime:
        p = self.posting
        return run_time + timedelta(minutes=p["first"] + step.index * p["step"])

    def deadline(self, run_time: datetime, step: Step) -> datetime:
        return self.expected_at(run_time, step) + timedelta(minutes=self.posting["grace"])

    def last_expected_at(self, run_time: datetime) -> datetime:
        return self.expected_at(run_time, self.steps()[-1])

    def runs_at(self, now: datetime) -> list[datetime]:
        """Run times the system tracks at `now`: the newest `keep` complete runs plus any newer
        run that has started posting. Oldest first."""
        if self.is_obs:
            cad = timedelta(minutes=self.cadence)
            t = now.replace(second=0, microsecond=0)
            t -= timedelta(minutes=t.minute % self.cadence)
            frames = []
            while len(frames) < self.keep_frames:
                if t + timedelta(minutes=self.posting["first"]) <= now:
                    frames.append(t)
                t -= cad
            return sorted(frames)
        t = now.replace(minute=0, second=0, microsecond=0)
        complete, started = [], []
        for _ in range(24 * 4):
            if t.hour in self.cycles and self.expected_at(t, self.steps()[0]) <= now:
                if self.last_expected_at(t) <= now:
                    complete.append(t)
                    if len(complete) >= self.keep_cycles:
                        break
                elif not complete:
                    started.append(t)
            t -= timedelta(hours=1)
        return sorted(complete + started)

    # ------------------------------------------------------------------ object keys
    def key_for(self, run_time: datetime, step: Step, var: str | None = None) -> str:
        t = run_time
        tokens = {
            "ymd": t.strftime("%Y%m%d"),
            "hh": t.strftime("%H"),
            "mm": t.strftime("%M"),
            "year": t.strftime("%Y"),
            "doy": t.strftime("%j"),
            "fff": f"{step.fhour:03d}",
            "ff": f"{step.fhour:02d}",
            "f": str(step.fhour),
            "mem": ("c00" if step.member == 0 else f"p{step.member:02d}") if step.member is not None else "",
            "VAR": var or "",
            "var_lc": (var or "").lower(),
        }
        return self.path.format(**tokens)

    def variable_names(self) -> list[str]:
        """Per-field layouts: the file variables behind the mapped fields, in order."""
        seen: list[str] = []
        for f in self.decode_fields:
            for var in self.variables.get(f, []):
                if var not in seen:
                    seen.append(var)
        return seen

    def keys_for(self, run_time: datetime, step: Step) -> list[str]:
        """Every upstream object one step needs (one per variable for per-field layouts)."""
        if self.layout == "per_field":
            return [self.key_for(run_time, step, v) for v in self.variable_names()]
        return [self.key_for(run_time, step)]

    @property
    def key_regex(self) -> re.Pattern:
        return _template_regex(self.path)

    def parse_key(self, key: str) -> tuple[datetime, str] | None:
        """Map an upstream object key (from a new-object notification) to (run time, step key)."""
        m = self.key_regex.fullmatch(key)
        if not m:
            return None
        g = m.groupdict()
        if self.is_obs:
            t = datetime.strptime(g["ymd"] if "ymd" in g else _ydoy(g), "%Y%m%d").replace(tzinfo=UTC)
            t = t.replace(hour=int(g["hh"]), minute=int(g.get("mm") or 0))
            return t, "f000"
        t = datetime.strptime(g["ymd"], "%Y%m%d").replace(tzinfo=UTC, hour=int(g["hh"]))
        fh = int(g.get("fff") or g.get("ff") or g.get("f"))
        if self.kind == "ensemble" and self.member_layout == "per_file":
            mem = g["mem"]
            m_i = 0 if mem == "c00" else int(mem[1:])
            return t, f"m{m_i:02d}/f{fh:03d}"
        return t, f"f{fh:03d}"


def _ydoy(g: dict) -> str:
    d = datetime.strptime(f"{g['year']}{g['doy']}", "%Y%j")
    return d.strftime("%Y%m%d")


_TOKEN_RE = {
    "ymd": r"\d{8}", "hh": r"\d{2}", "mm": r"\d{2}", "year": r"\d{4}", "doy": r"\d{3}",
    "fff": r"\d{3}", "ff": r"\d{2,3}", "f": r"\d{1,3}", "mem": r"c00|p\d{2}", "VAR": r"[A-Z0-9_]+",
    "var_lc": r"[a-z0-9_]+",
}


@lru_cache(maxsize=64)
def _template_regex(template: str) -> re.Pattern:
    out, seen, pos = [], set(), 0
    for m in re.finditer(r"\{(\w+)\}", template):
        out.append(re.escape(template[pos:m.start()]))
        name = m.group(1)
        if name in seen:
            out.append(f"(?P={name})")
        else:
            out.append(f"(?P<{name}>{_TOKEN_RE[name]})")
            seen.add(name)
        pos = m.end()
    out.append(re.escape(template[pos:]))
    return re.compile("".join(out))


@dataclass
class Registry:
    sources: dict[str, Source]
    mirrors: dict[str, Mirror]
    grids: dict[str, dict]
    qc: dict

    def __getitem__(self, sid: str) -> Source:
        return self.sources[sid]

    def __iter__(self):
        return iter(self.sources.values())

    def triggered_by(self, sid: str) -> list[Source]:
        return [s for s in self if s.trigger and s.trigger["source"] == sid]

    def match_key(self, mirror_name: str, key: str) -> tuple[Source, datetime, str] | None:
        for s in self:
            if mirror_name in s.mirrors:
                hit = s.parse_key(key)
                if hit:
                    return s, hit[0], hit[1]
        return None


def load_registry(path: str | Path | None = None) -> Registry:
    path = Path(path or settings.sources_file)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    grids = raw["grids"]
    mirrors = {n: Mirror(n, m["protocol"], m["bucket"]) for n, m in raw["mirrors"].items()}
    sources = {}
    for e in raw["sources"]:
        e = dict(e)
        sid = e.pop("id")
        grid_name = e.pop("grid")
        fhours = e.pop("fhours", [])
        sources[sid] = Source(id=sid, grid_name=grid_name, grid=grids[grid_name], fhours_spec=fhours, **e)
    return Registry(sources=sources, mirrors=mirrors, grids=grids, qc=raw["qc"])


@lru_cache(maxsize=1)
def registry() -> Registry:
    return load_registry()
