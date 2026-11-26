"""Upload sets: tell agents when a group of files that belong together is fully indexed.

S3 has no multi-file transactions, so an agent reading a folder mid-upload can see some new files
and some old ones. The fix is a convention between the uploader and bucketlens:

1. The uploader writes all the files, then writes a marker LAST: ``<folder>/_bucketlens.json``.
   Either ``{"files": ["report.pdf", "numbers.xlsx"]}`` (paths relative to the folder), or ``{}``
   for "every file under this folder".
2. bucketlens indexes every file in the set and records the exact version (ETag) of each one in
   ``.bucketlens/sets/<folder><marker-etag>.json``. That record is written last, so a set is never
   reported ready while something is still being indexed.
3. Agents call ``set_status(folder)``: ready / processing (and what's missing) / failed / no marker.
   A ready set lists each file's pinned version; passing it to the read tools reads exactly that
   upload, even if a file has been overwritten since (bucketlens keeps the index of every version).
4. If a listed file can't be parsed (corrupt, encrypted...), the set is never reported ready, but it
   doesn't sit at "processing" forever either: once every file has been tried it is "failed", with
   which file and why, and the files that did index. Uploading a fixed copy completes the set.
"""

from __future__ import annotations

import json
import posixpath
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .index import Index, Unreadable
from .parse import kind_of
from .store import Stats, is_missing

MARKER = "_bucketlens.json"
SETS = ".bucketlens/sets"


class SetError(ValueError):
    """The marker itself is unusable (bad JSON, a path outside the folder)."""


class SetIncomplete(RuntimeError):
    """Some listed files are not in the bucket yet."""

    def __init__(self, folder: str, missing: list[str]) -> None:
        super().__init__(f"{folder or '(bucket root)'}: waiting for {len(missing)} file(s): {', '.join(missing[:10])}")
        self.folder, self.missing = folder, missing


class SetFailed(RuntimeError):
    """Every file is in the bucket, but some can't be parsed. Nothing changes until a fixed copy is uploaded."""

    def __init__(self, folder: str, failed: dict[str, str]) -> None:
        names = ", ".join(list(failed)[:10])
        super().__init__(f"{folder or '(bucket root)'}: {len(failed)} file(s) can't be read: {names}")
        self.folder, self.failed = folder, failed


def folder_of(path: str) -> str:
    """'reports/q3' or 'reports/q3/' or 'reports/q3/_bucketlens.json' -> 'reports/q3/' ('' is the bucket root)."""
    p = path.strip().lstrip("/")
    if p.rsplit("/", 1)[-1] == MARKER:
        p = p[: -len(MARKER)]
    return p if not p or p.endswith("/") else p + "/"


def is_marker(key: str) -> bool:
    return key.rsplit("/", 1)[-1] == MARKER and not key.startswith(".bucketlens/")


def _record_key(folder: str, marker_etag: str) -> str:
    return f"{SETS}/{folder}{marker_etag}.json"


def _read_marker(ix: Index, folder: str, stats: Stats | None = None) -> tuple[str, dict] | None:
    key = folder + MARKER
    try:
        meta = ix.store.head(key, stats)
        raw = ix.store.get(key, stats)
    except Exception as e:
        if is_missing(e):
            return None
        raise
    try:
        spec = json.loads(raw or b"{}")
    except ValueError as e:
        raise SetError(f"{key} is not valid JSON: {e}") from None
    if not isinstance(spec, dict):
        raise SetError(f'{key} must be a JSON object, e.g. {{"files": ["a.pdf"]}} or {{}}')
    files = spec.get("files")
    if files is not None:
        if not isinstance(files, list) or not all(isinstance(f, str) and f.strip() for f in files):
            raise SetError(f'{key}: "files" must be a list of file names')
        for f in files:
            norm = posixpath.normpath(f.strip())
            if f.startswith("/") or norm.startswith("..") or norm == ".":
                raise SetError(f"{key}: {f!r} is not a path inside {folder or 'the bucket root'}")
    return meta["etag"], spec


def _members(ix: Index, folder: str, spec: dict, stats: Stats | None = None) -> tuple[list[str], list[str]]:
    """(keys bucketlens can read, keys it can't). Explicit list, or everything under the folder."""
    if spec.get("files") is not None:
        keys = [folder + posixpath.normpath(f.strip()) for f in spec["files"]]
    else:
        keys = [o["key"] for o in ix.store.list(folder, stats) if not is_marker(o["key"])]
        keys = [k for k in keys if not k.startswith(".bucketlens/")]
    keys = sorted(dict.fromkeys(keys))
    return [k for k in keys if kind_of(k)], [k for k in keys if not kind_of(k)]


def complete(ix: Index, folder: str, workers: int = 8) -> dict:
    """Index every file of the folder's set and write the set record. Raises SetIncomplete if files are missing."""
    return _complete(ix, folder, workers)[0]


def _complete(ix: Index, folder: str, workers: int = 8) -> tuple[dict, bool]:
    """(record, True if this call completed the set, False if it was already complete)."""
    folder = folder_of(folder)
    found = _read_marker(ix, folder)
    if found is None:
        raise SetError(f"no {MARKER} in {folder or 'the bucket root'}")
    marker_etag, spec = found
    rkey = _record_key(folder, marker_etag)
    try:
        return json.loads(ix.store.get(rkey)), False
    except Exception as e:
        if not is_missing(e):
            raise
    readable, skipped = _members(ix, folder, spec)
    present = {o["key"] for o in ix.store.list(folder)}
    missing = [k for k in readable + skipped if k not in present]
    if missing:
        raise SetIncomplete(folder, missing)

    def one(k):
        try:
            return ix.manifest(k), None
        except Unreadable as e:  # anything else (storage, network) propagates and is retried
            return None, e.reason

    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(one, readable))
    failed = {k: why for k, (_, why) in zip(readable, results, strict=True) if why is not None}
    if failed:
        raise SetFailed(folder, failed)  # no record: a set is only ever recorded when all of it is readable
    manifests = [m for m, _ in results]
    record = {
        "folder": folder,
        "marker_etag": marker_etag,
        "files": {m["key"]: m["etag"] for m in manifests},
        "skipped": skipped,
        "completed": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    ix.store.put(rkey, json.dumps(record).encode(), "application/json")  # written last
    return record, True


def refresh_for(ix: Index, key: str) -> list[dict]:
    """A file just arrived: complete any pending set in its folder or a parent folder that was waiting on it.
    Returns the sets this call completed."""
    done = []
    parts = key.split("/")[:-1]
    for depth in range(len(parts), -1, -1):
        folder = "/".join(parts[:depth])
        folder = folder + "/" if folder else ""
        try:
            record, created = _complete(ix, folder)
        except (SetError, SetIncomplete, SetFailed):
            continue
        if created:
            done.append(record)
    return done


def remove(ix: Index, folder: str) -> int:
    """The marker was deleted: drop the folder's set records."""
    return ix.store.delete_prefix(f"{SETS}/{folder_of(folder)}")


def status(ix: Index, folder: str, stats: Stats | None = None) -> dict:
    """ready / processing / failed / no_marker / invalid, with counts, pinned versions and what changed since."""
    folder = folder_of(folder)
    try:
        found = _read_marker(ix, folder, stats)
    except SetError as e:
        return {"state": "invalid", "folder": folder, "error": str(e)}
    if found is None:
        return {"state": "no_marker", "folder": folder}
    marker_etag, spec = found
    current = {o["key"]: o["etag"] for o in ix.store.list(folder, stats)}
    try:
        record = json.loads(ix.store.get(_record_key(folder, marker_etag), stats))
    except Exception as e:
        if not is_missing(e):
            raise
        record = None
    if record:
        pinned = record["files"]
        return {
            "state": "ready",
            "folder": folder,
            "completed": record["completed"],
            "files": pinned,
            "skipped": record.get("skipped", []),
            # replaced: the upload's copy is still readable with version=; deleted: its index went with it
            "changed_since": [k for k, v in pinned.items() if k in current and current[k] != v],
            "deleted_since": [k for k in pinned if k not in current],
        }
    readable, skipped = _members(ix, folder, spec, stats)
    missing = [k for k in readable + skipped if k not in current]
    indexed, failed = {}, {}
    for k in readable:
        if k not in current:
            continue
        if ix.manifest(k, stats, build=False) is not None:
            indexed[k] = current[k]
        elif (why := ix.failure(k, current[k], stats)) is not None:
            failed[k] = why
    out = {
        "state": "processing",
        "folder": folder,
        "total": len(readable),
        "indexed": len(indexed),
        "missing": missing,
        "failed": failed,
        "skipped": skipped,
    }
    if failed and not missing and len(indexed) + len(failed) == len(readable):
        # every file has been tried: say so instead of leaving the agent waiting
        out.update(state="failed", files=indexed)
    return out
