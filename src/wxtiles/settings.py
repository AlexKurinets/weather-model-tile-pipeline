"""Runtime settings from the environment. Every variable is prefixed WX_ so nothing collides."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    database_url: str = ""
    sources_file: str = ""
    # Object storage: "s3" talks to any S3 API (SeaweedFS in Compose, S3 in AWS); "local" keeps a
    # directory per bucket, which the tests use.
    storage: str = ""
    local_root: str = ""
    s3_endpoint: str = ""
    http_mirror_base: str = ""
    products_bucket: str = ""
    # "sim" reads the clock from the database so a synthetic day replays the same way every time;
    # "system" is wall-clock UTC, as in production.
    clock: str = ""
    sim_start: str = ""
    manifest_ttl: int = 15
    retry_seconds: float = 20.0
    lease_seconds: int = 120
    warm_url: str = ""
    warm_zooms: str = ""
    web_dir: str = ""
    metrics_namespace: str = ""


def _from_env() -> dict:
    e = os.environ.get
    return {
        "database_url": e("DATABASE_URL", "postgresql://wx:wx@localhost:5522/wx"),
        "sources_file": e("WX_SOURCES_FILE", str(ROOT / "config" / "sources.yaml")),
        "storage": e("WX_STORAGE", "s3"),
        "local_root": e("WX_LOCAL_ROOT", str(ROOT / ".data")),
        "s3_endpoint": e("WX_S3_ENDPOINT", "http://localhost:9210"),
        "http_mirror_base": e("WX_HTTP_MIRROR_BASE", "http://localhost:9210"),
        "products_bucket": e("WX_PRODUCTS_BUCKET", "wx-products"),
        "clock": e("WX_CLOCK", "sim"),
        "sim_start": e("WX_SIM_START", "2026-10-28T20:40:00Z"),
        "manifest_ttl": int(e("WX_MANIFEST_TTL", "15")),
        "retry_seconds": float(e("WX_RETRY_SECONDS", "20")),
        "lease_seconds": int(e("WX_LEASE_SECONDS", "120")),
        "warm_url": e("WX_WARM_URL", ""),
        "warm_zooms": e("WX_WARM_ZOOMS", "3"),
        "web_dir": e("WX_WEB_DIR", str(ROOT / "web")),
        "metrics_namespace": e("WX_METRICS_NAMESPACE", ""),
    }


settings = Settings(**_from_env())


def reload() -> Settings:
    """Re-read the environment in place (tests change it between cases)."""
    fresh = _from_env()
    for f in fields(Settings):
        setattr(settings, f.name, fresh[f.name])
    return settings
