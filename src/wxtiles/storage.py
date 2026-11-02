"""Object storage behind one small interface: S3 (SeaweedFS in Compose, S3 in AWS) or a local
directory per bucket (tests). Mirrors read upstream objects over S3 or plain HTTP range requests."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import httpx

from wxtiles.settings import settings


class NotFound(Exception):
    """The object is not on this store or mirror (yet)."""


@dataclass
class ObjInfo:
    size: int
    modified: datetime


class Store:
    bucket: str

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream",
            cache_control: str | None = None) -> None: ...
    def put_file(self, key: str, path: str, content_type: str = "application/octet-stream") -> None: ...
    def get(self, key: str, start: int | None = None, end: int | None = None) -> bytes: ...
    def head(self, key: str) -> ObjInfo | None: ...
    def list(self, prefix: str = "") -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def gdal_path(self, key: str) -> str: ...


class LocalStore(Store):
    def __init__(self, bucket: str, root: str | None = None):
        self.bucket = bucket
        self.root = Path(root or settings.local_root) / bucket
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        return self.root / key

    def put(self, key, data, content_type="application/octet-stream", cache_control=None):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def put_file(self, key, path, content_type="application/octet-stream"):
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, p)

    def get(self, key, start=None, end=None):
        p = self._p(key)
        if not p.exists():
            raise NotFound(key)
        with p.open("rb") as fh:
            if start is None:
                return fh.read()
            fh.seek(start)
            return fh.read(None if end is None else end - start + 1)

    def head(self, key):
        p = self._p(key)
        if not p.exists():
            return None
        st = p.stat()
        return ObjInfo(st.st_size, datetime.fromtimestamp(st.st_mtime, UTC))

    def list(self, prefix=""):
        base = self.root
        out = []
        for p in base.rglob("*"):
            if p.is_file() and not p.name.endswith(".part"):
                k = p.relative_to(base).as_posix()
                if k.startswith(prefix):
                    out.append(k)
        return sorted(out)

    def delete(self, key):
        p = self._p(key)
        if p.exists():
            p.unlink()

    def gdal_path(self, key):
        return str(self._p(key))


@lru_cache(maxsize=1)
def _s3_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint or None,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "wx"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "wx"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3, "mode": "standard"},
                      max_pool_connections=32),
    )


class S3Store(Store):
    def __init__(self, bucket: str):
        self.bucket = bucket
        self.s3 = _s3_client()

    def put(self, key, data, content_type="application/octet-stream", cache_control=None):
        extra = {"ContentType": content_type}
        if cache_control:
            extra["CacheControl"] = cache_control
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)

    def put_file(self, key, path, content_type="application/octet-stream"):
        self.s3.upload_file(path, self.bucket, key, ExtraArgs={"ContentType": content_type})

    def get(self, key, start=None, end=None):
        kw = {}
        if start is not None:
            kw["Range"] = f"bytes={start}-" + ("" if end is None else str(end))
        try:
            r = self.s3.get_object(Bucket=self.bucket, Key=key, **kw)
        except self.s3.exceptions.NoSuchKey as e:
            raise NotFound(key) from e
        except Exception as e:  # botocore ClientError for 404 / InvalidRange
            code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise NotFound(key) from e
            if code == "InvalidRange":  # range starts past the end of a short (truncated) object
                return b""
            raise
        return r["Body"].read()

    def head(self, key):
        try:
            r = self.s3.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return None
        return ObjInfo(int(r["ContentLength"]), r["LastModified"])

    def list(self, prefix=""):
        out, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = self.s3.list_objects_v2(**kw)
            out += [o["Key"] for o in r.get("Contents", [])]
            if not r.get("IsTruncated"):
                return out
            token = r["NextContinuationToken"]

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def ensure_bucket(self):
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except Exception:
            self.s3.create_bucket(Bucket=self.bucket)

    def gdal_path(self, key):
        return f"/vsis3/{self.bucket}/{key}"


def store(bucket: str) -> Store:
    if settings.storage == "local":
        return LocalStore(bucket)
    return S3Store(bucket)


def products() -> Store:
    return store(settings.products_bucket)


# ---------------------------------------------------------------------------- mirrors
class MirrorClient:
    """Read-only access to one upstream mirror."""

    name: str

    def head(self, key: str) -> ObjInfo | None: ...
    def get(self, key: str, start: int | None = None, end: int | None = None) -> bytes: ...


class StoreMirror(MirrorClient):
    def __init__(self, name: str, st: Store):
        self.name, self.st = name, st

    def head(self, key):
        return self.st.head(key)

    def get(self, key, start=None, end=None):
        return self.st.get(key, start, end)


class HttpMirror(MirrorClient):
    """Public HTTPS mirror read without credentials (NOMADS, data.ecmwf.int, opendata.dwd.de), with Range requests."""

    def __init__(self, name: str, base_url: str, transport: httpx.BaseTransport | None = None):
        self.name = name
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0), transport=transport)

    def head(self, key):
        r = self.client.head(f"{self.base}/{key}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        lm = r.headers.get("last-modified")
        mod = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC) if lm else datetime.now(UTC)
        return ObjInfo(int(r.headers.get("content-length", 0)), mod)

    def get(self, key, start=None, end=None):
        headers = {}
        if start is not None:
            headers["Range"] = f"bytes={start}-" + ("" if end is None else str(end))
        r = self.client.get(f"{self.base}/{key}", headers=headers)
        if r.status_code == 404:
            raise NotFound(key)
        if r.status_code == 416:
            # Range beyond the end of a shorter (truncated) object: report what is there.
            return b""
        r.raise_for_status()
        return r.content


def mirror_client(name: str) -> MirrorClient:
    from wxtiles.registry import registry

    m = registry().mirrors[name]
    if settings.storage == "local" or m.protocol == "s3":
        return StoreMirror(name, store(m.bucket))
    return HttpMirror(name, f"{settings.http_mirror_base}/{m.bucket}")
