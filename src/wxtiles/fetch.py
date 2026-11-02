"""Fetching one step from upstream: inventory, byte ranges, integrity, and mirror failover.

Mirrors are tried in registry order. A mirror that does not have the object yet, or whose bytes
fail an integrity check, is skipped for the next one, and the step is retried later if none has
it. A truncated object on the primary bucket is the common case this guards against: the index
was written for the whole file, so the last range comes back short or without its 7777 marker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import httpx
from botocore.exceptions import BotoCoreError, ClientError

from wxtiles.fields import raw_fields
from wxtiles.gribio import IntegrityError, decompress, split_messages
from wxtiles.idx import byte_ranges, parse, select
from wxtiles.registry import Source, Step
from wxtiles.storage import MirrorClient, NotFound, mirror_client


class NotAvailable(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass
class Fetched:
    mirror: str
    messages: list[bytes] = field(default_factory=list)  # GRIB2 messages
    netcdf: bytes | None = None
    bytes_fetched: int = 0
    bytes_full: int = 0
    posted: datetime | None = None
    skipped: list[str] = field(default_factory=list)  # mirrors tried first, with the reason


def index_key(src: Source, key: str) -> str:
    if src.index == "ecmwf":
        return key[: -len(".grib2")] + ".index"
    return key + ".idx"


def raw_for_step(src: Source, step: Step) -> list[str]:
    raw = raw_fields(src.decode_fields)
    if step.fhour == 0 and not src.is_obs:
        raw = [r for r in raw if r != "qpf"]  # no accumulation at the analysis time
    return raw


def vars_for_step(src: Source, step: Step) -> list[str]:
    skip = set(src.variables.get("qpf", [])) if step.fhour == 0 else set()
    return [v for v in src.variable_names() if v not in skip]


def _head(client: MirrorClient, key: str):
    info = client.head(key)
    if info is None:
        raise NotFound(key)
    return info


def fetch_from(client: MirrorClient, src: Source, run_time: datetime, step: Step) -> Fetched:
    out = Fetched(client.name)
    if src.layout == "indexed":
        key = src.key_for(run_time, step)
        info = _head(client, key)
        idx_text = client.get(index_key(src, key)).decode()
        members = list(range(src.members)) if src.kind == "ensemble" and src.member_layout == "in_file" else None
        try:
            chosen = select(parse(idx_text, src.index), raw_for_step(src, step), src.index, src.idx_names, members)
        except KeyError as e:
            raise IntegrityError(f"inventory is missing {e}") from e
        for start, end in byte_ranges([e for es in chosen.values() for e in es]):
            want = (end - start + 1) if end is not None else info.size - start
            data = client.get(key, start, end)
            out.bytes_fetched += len(data)
            out.messages += split_messages(data, want)
        out.bytes_fetched += len(idx_text)
        out.bytes_full, out.posted = info.size, info.modified
        return out
    if src.layout == "per_field":
        for var in vars_for_step(src, step):
            key = src.key_for(run_time, step, var)
            info = _head(client, key)
            data = client.get(key)
            if len(data) != info.size:
                raise IntegrityError(f"{key}: got {len(data)} of {info.size} bytes")
            out.bytes_fetched += len(data)
            out.bytes_full += info.size
            out.posted = max(out.posted, info.modified) if out.posted else info.modified
            out.messages += split_messages(decompress(data, src.compression))
        return out
    key = src.key_for(run_time, step)
    info = _head(client, key)
    data = client.get(key)
    if len(data) != info.size:
        raise IntegrityError(f"got {len(data)} of {info.size} bytes")
    out.bytes_fetched = out.bytes_full = len(data)
    out.posted = info.modified
    if src.format == "netcdf":
        if data[:8] != b"\x89HDF\r\n\x1a\n":
            raise IntegrityError("not an HDF5/netCDF-4 file")
        out.netcdf = data
    else:
        out.messages = split_messages(decompress(data, src.compression))
    return out


def fetch_step(src: Source, run_time: datetime, step: Step, clients: dict[str, MirrorClient] | None = None) -> Fetched:
    reasons: list[str] = []
    for name in src.mirrors:
        client = (clients or {}).get(name) or mirror_client(name)
        try:
            got = fetch_from(client, src, run_time, step)
            got.skipped = reasons
            return got
        except NotFound:
            reasons.append(f"{name}: not posted")
        except IntegrityError as e:
            reasons.append(f"{name}: {e}")
        except (httpx.HTTPError, OSError, BotoCoreError, ClientError) as e:
            reasons.append(f"{name}: {type(e).__name__} {e}")
    raise NotAvailable(reasons)
