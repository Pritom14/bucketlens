"""Keep the index current as files are saved, changed or deleted.

Two ways to hear about changes:
- Cloudflare R2 event notifications delivered to a Queue, pulled over HTTP (`--queue`, near-instant).
- Polling: list the bucket every N seconds and index anything with a new ETag (`--poll`, works on any S3).
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import sets
from .config import Config
from .index import Index, Unreadable
from .parse import kind_of
from .store import Stats

API = "https://api.cloudflare.com/client/v4"
CREATE = {"PutObject", "CopyObject", "CompleteMultipartUpload"}
DELETE = {"DeleteObject", "LifecycleDeletion"}


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def skip(key: str) -> bool:
    base = key.rsplit("/", 1)[-1]
    return not key or key.startswith(".bucketlens/") or base.startswith("._") or base == ".DS_Store"


class QueueError(RuntimeError):
    pass


class CloudflareQueue:
    """HTTP pull consumer for a Cloudflare Queue (token needs Account > Queues > Edit)."""

    def __init__(self, cfg: Config) -> None:
        if not (cfg.r2_account_id and cfg.cf_api_token and cfg.cf_queue):
            raise QueueError(
                "queue mode needs BUCKETLENS_R2_ACCOUNT_ID, BUCKETLENS_CF_API_TOKEN and BUCKETLENS_CF_QUEUE"
            )
        self.account, self._token, self.name = cfg.r2_account_id, cfg.cf_api_token, cfg.cf_queue
        self.id = self._find_id()

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{API}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                errs = json.loads(e.read()).get("errors", [])
            except Exception:
                errs = []
            raise QueueError(
                f"HTTP {e.code}: " + ("; ".join(str(x.get("message")) for x in errs) or str(e.reason))
            ) from None

    def _find_id(self) -> str:
        page = 1
        while True:
            res = self._call("GET", f"/accounts/{self.account}/queues?per_page=100&page={page}").get("result") or []
            for q in res:
                if q.get("queue_name") == self.name:
                    return q["queue_id"]
            if len(res) < 100:
                raise QueueError(f"no queue named {self.name!r}")
            page += 1

    def ensure_http_pull(self) -> str:
        consumers = self._call("GET", f"/accounts/{self.account}/queues/{self.id}/consumers").get("result") or []
        types = [c.get("type") for c in consumers]
        if "http_pull" in types:
            return "already on"
        if types:
            raise QueueError(f"the queue already has a {types[0]} consumer; a queue can have only one")
        self._call(
            "POST",
            f"/accounts/{self.account}/queues/{self.id}/consumers",
            {"type": "http_pull", "settings": {"batch_size": 50, "max_retries": 5, "visibility_timeout_ms": 120000}},
        )
        return "turned on"

    def pull(self, batch_size: int = 50) -> list[dict]:
        res = (
            self._call(
                "POST",
                f"/accounts/{self.account}/queues/{self.id}/messages/pull",
                {"batch_size": batch_size, "visibility_timeout_ms": 120000},
            ).get("result")
            or {}
        )
        msgs = res.get("messages") or []
        for m in msgs:
            m["event"] = _decode(m.get("body"))
        return msgs

    def ack(self, done: list[str], retry: list[str]) -> None:
        if done or retry:
            self._call(
                "POST",
                f"/accounts/{self.account}/queues/{self.id}/messages/ack",
                {
                    "acks": [{"lease_id": i} for i in done],
                    "retries": [{"lease_id": i, "delay_seconds": 10} for i in retry],
                },
            )


def _decode(body) -> dict:
    if isinstance(body, dict):
        return body
    for f in (json.loads, lambda b: json.loads(base64.b64decode(b))):
        try:
            return f(body)
        except Exception:
            continue
    return {}


def _lag(event: dict) -> str:
    try:
        t = datetime.fromisoformat(event["eventTime"].replace("Z", "+00:00"))
        return f", {(datetime.now(timezone.utc) - t).total_seconds():.1f}s after the save"
    except Exception:
        return ""


def _log_set(record: dict) -> None:
    extra = f", {len(record['skipped'])} not readable" if record.get("skipped") else ""
    log(f"upload set {record['folder'] or '(bucket root)'} ready: {len(record['files'])} files indexed{extra}")


def handle_marker(ix: Index, key: str, action: str) -> None:
    folder = sets.folder_of(key)
    if action in CREATE:
        try:
            record, created = sets._complete(ix, folder)
            if created:
                _log_set(record)
        except sets.SetIncomplete as e:
            log(f"upload set {e} (each file completes the set when it lands)")
        except sets.SetFailed as e:
            log(f"upload set {e} (set_status reports it; uploading a fixed copy completes the set)")
        except sets.SetError as e:
            log(f"upload set ignored: {e}")
    elif action in DELETE:
        log(f"removed the upload set records of {folder or '(bucket root)'} ({sets.remove(ix, folder)} objects)")


def handle_event(ix: Index, event: dict) -> None:
    key, action = (event.get("object") or {}).get("key", ""), event.get("action", "")
    if skip(key):
        return
    if sets.is_marker(key):
        handle_marker(ix, key, action)
        return
    if action in CREATE and kind_of(key):
        t = time.perf_counter()
        try:
            m = ix.manifest(key, Stats())
        except Unreadable as e:  # recorded; retrying the event can't help, a new upload of the file will
            log(f"{e}")
            return
        what = f"{m['pages']} pages" if m["kind"] == "pdf" else f"{sum(s['rows'] for s in m['sheets']):,} rows"
        log(f"indexed {key} ({what}) in {time.perf_counter() - t:.1f}s{_lag(event)}")
    elif action in DELETE:
        log(f"removed the index of deleted {key} ({ix.remove(key)} objects)")
    if action in CREATE:  # a set that was waiting on this file can complete now
        for record in sets.refresh_for(ix, key):
            _log_set(record)


def run_queue(ix: Index, cfg: Config, once: bool = False) -> None:
    """Pull events: every 2 s while they arrive, backing off to once a minute when idle (~1,440 pulls a day)."""
    q = CloudflareQueue(cfg)
    log(f"HTTP pull {q.ensure_http_pull()} for queue {q.name}; watching bucket {cfg.bucket}")
    idle = 2.0

    def safe(m):
        try:
            handle_event(ix, m["event"])
            return True
        except Exception as e:  # the queue retries it later
            log(f"failed on {(m['event'].get('object') or {}).get('key')}: {type(e).__name__}: {str(e)[:200]}")
            return False

    with ThreadPoolExecutor(4) as ex:
        while True:
            msgs = q.pull()
            if msgs:
                idle = 2.0
                ok = list(ex.map(safe, msgs))
                q.ack(
                    [m["lease_id"] for m, good in zip(msgs, ok, strict=True) if good],
                    [m["lease_id"] for m, good in zip(msgs, ok, strict=True) if not good],
                )
            elif once:
                return
            else:
                time.sleep(idle)
                idle = min(idle * 2, 60.0)


def run_poll(ix: Index, seconds: int, prefix: str = "", once: bool = False) -> None:
    """List the bucket every ``seconds`` and index files whose ETag has no index yet (one LIST per 1,000 files)."""
    log(f"polling bucket {ix.store.bucket} every {seconds}s")
    seen: dict[str, str] = {}
    seen_markers: dict[str, str] = {}
    while True:
        listing = [o for o in ix.store.list(prefix) if not skip(o["key"])]
        current = {o["key"]: o["etag"] for o in listing if kind_of(o["key"])}
        touched, retry = [], set()  # files whose new version was handled this pass / hit a storage error
        for key, etag in current.items():
            if seen.get(key) != etag:
                try:
                    t = time.perf_counter()
                    ix.manifest(key, Stats())
                    if key in seen:
                        log(f"re-indexed changed {key} in {time.perf_counter() - t:.1f}s")
                except Unreadable as e:
                    log(f"{e}")  # recorded for this version; not retried until the file changes
                except Exception as e:
                    log(f"failed on {key}: {type(e).__name__}: {str(e)[:200]} (retrying next pass)")
                    retry.add(key)
                    continue
                touched.append(key)
        for key in set(seen) - set(current):
            log(f"removed the index of deleted {key} ({ix.remove(key)} objects)")
        seen = {k: ("" if k in retry else v) for k, v in current.items()}  # "" never matches: tried again
        markers = {o["key"]: o["etag"] for o in listing if sets.is_marker(o["key"])}
        for key, etag in markers.items():
            folder = sets.folder_of(key)
            if seen_markers.get(key) == etag and not any(k.startswith(folder) for k in touched):
                continue  # neither the marker nor any file under it changed
            try:
                record, created = sets._complete(ix, folder)
                if created:
                    _log_set(record)
                seen_markers[key] = etag
            except sets.SetIncomplete as e:
                log(f"upload set {e}")  # not marked seen: checked again next pass
            except sets.SetFailed as e:
                log(f"upload set {e} (uploading a fixed copy completes it)")
                seen_markers[key] = etag  # checked again when a file under it changes
            except sets.SetError as e:
                log(f"upload set ignored: {e}")
                seen_markers[key] = etag  # until the marker changes
            except Exception as e:  # storage hiccup: don't take the watcher down, try again next pass
                log(f"upload set {folder or '(bucket root)'}: {type(e).__name__}: {str(e)[:200]} (retrying next pass)")
        for key in set(seen_markers) - set(markers):
            folder = sets.folder_of(key)
            log(f"removed the upload set records of {folder or '(bucket root)'} ({sets.remove(ix, folder)} objects)")
            del seen_markers[key]
        if once:
            return
        time.sleep(seconds)
