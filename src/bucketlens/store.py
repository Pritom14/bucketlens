"""A thin S3 client (AWS S3, Cloudflare R2, MinIO, or anything S3-compatible) that counts its requests."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import Config


@dataclass
class Stats:
    """Requests made and bytes downloaded while answering one call."""

    requests: int = 0
    bytes_fetched: int = 0
    seconds: float = 0.0

    def add(self, other: Stats) -> None:
        self.requests += other.requests
        self.bytes_fetched += other.bytes_fetched
        self.seconds += other.seconds


_lock = threading.Lock()


def _count(stats: Stats | None, n: int, t0: float) -> None:
    if stats is not None:
        with _lock:
            stats.requests += 1
            stats.bytes_fetched += n
            stats.seconds += time.perf_counter() - t0


def is_missing(err: Exception) -> bool:
    if isinstance(err, ClientError):
        code = str(err.response.get("Error", {}).get("Code", ""))
        return code in ("404", "NoSuchKey", "NotFound")
    return False


class Store:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.bucket = cfg.bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            region_name=cfg.region,
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                max_pool_connections=32,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def list(self, prefix: str = "", stats: Stats | None = None) -> list[dict]:
        out, token = [], None
        while True:
            t0 = time.perf_counter()
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            r = self.client.list_objects_v2(**kw)
            _count(stats, 0, t0)
            out += [
                {
                    "key": o["Key"],
                    "size": o["Size"],
                    "etag": o["ETag"].strip('"'),
                    "modified": o["LastModified"].isoformat(),
                }
                for o in r.get("Contents", [])
            ]
            if not r.get("IsTruncated"):
                return out
            token = r["NextContinuationToken"]

    def head(self, key: str, stats: Stats | None = None) -> dict:
        t0 = time.perf_counter()
        r = self.client.head_object(Bucket=self.bucket, Key=key)
        _count(stats, 0, t0)
        return {"size": r["ContentLength"], "etag": r["ETag"].strip('"')}

    def get(self, key: str, stats: Stats | None = None) -> bytes:
        t0 = time.perf_counter()
        data = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        _count(stats, len(data), t0)
        return data

    def get_range(self, key: str, start: int, end: int, stats: Stats | None = None) -> bytes:
        """Bytes ``start``..``end`` inclusive."""
        t0 = time.perf_counter()
        data = self.client.get_object(Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}")["Body"].read()
        _count(stats, len(data), t0)
        return data

    def put(self, key: str, body: bytes, content_type: str = "application/octet-stream") -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)

    def delete_prefix(self, prefix: str) -> int:
        keys = [o["key"] for o in self.list(prefix)]
        for i in range(0, len(keys), 1000):
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys[i : i + 1000]]})
        return len(keys)
