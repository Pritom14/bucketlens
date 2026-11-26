"""bucketlens command line: setup, index, serve, watch, status, set."""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import time

from . import config
from .config import CONFIG_FILE, ConfigError


def _ask(prompt: str, default: str = "", secret: bool = False, pattern: str | None = None) -> str:
    while True:
        shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
        v = (getpass.getpass(shown) if secret else input(shown)).strip() or default
        if v and (pattern is None or re.fullmatch(pattern, v)):
            return v
        print("  That doesn't look right; try again.")


def cmd_setup(a) -> int:
    print("bucketlens setup. Keys are typed hidden and saved to", CONFIG_FILE, "(readable only by you).")
    provider = _ask("Storage: 'r2' (Cloudflare R2) or 's3' (AWS S3, MinIO, any S3-compatible)", "r2", pattern="r2|s3")
    values: dict[str, str] = {}
    if provider == "r2":
        values["BUCKETLENS_R2_ACCOUNT_ID"] = _ask("Cloudflare account ID (32 characters)", pattern="[0-9a-f]{32}")
        values["BUCKETLENS_ACCESS_KEY_ID"] = _ask("R2 Access Key ID", secret=True, pattern="[0-9a-f]{32}")
        values["BUCKETLENS_SECRET_ACCESS_KEY"] = _ask("R2 Secret Access Key", secret=True, pattern="[0-9a-f]{64}")
    else:
        values["BUCKETLENS_ENDPOINT"] = _ask("Endpoint URL", "https://s3.amazonaws.com", pattern=r"https?://\S+")
        values["BUCKETLENS_REGION"] = _ask("Region", "us-east-1")
        values["BUCKETLENS_ACCESS_KEY_ID"] = _ask("Access Key ID", secret=True)
        values["BUCKETLENS_SECRET_ACCESS_KEY"] = _ask("Secret Access Key", secret=True)
    values["BUCKETLENS_BUCKET"] = _ask("Bucket name", pattern=r"[a-z0-9][a-z0-9.\-]{1,62}")
    config.write_env_file(CONFIG_FILE, values)
    return _check()


def _check() -> int:
    from .store import Store

    try:
        cfg = config.load()
        n = len(Store(cfg).list())
    except Exception as e:  # never echo secrets; type and S3 error code are enough
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "") if hasattr(e, "response") else ""
        print(f"FAILED: {type(e).__name__} {code}".strip())
        return 1
    print(f"OK: connected to bucket '{cfg.bucket}' ({n} objects).")
    print("\nAdd it to Claude Code:\n  claude mcp add bucketlens -- bucketlens serve")
    print('Or to any MCP client (stdio): command `bucketlens`, args ["serve"].')
    return 0


def _index():
    from .index import Index
    from .store import Store

    return Index(Store(config.load()))


def cmd_index(a) -> int:
    from . import sets

    t = time.perf_counter()
    ix = _index()
    res = ix.index_all(a.prefix, workers=a.workers)
    bad = [(k, s) for k, s in res if s != "ok"]
    for k, s in bad:
        print(f"failed: {k}: {s}")
    print(f"indexed {len(res) - len(bad)} of {len(res)} files in {time.perf_counter() - t:.0f}s")
    for o in ix.store.list(a.prefix):
        if sets.is_marker(o["key"]):
            folder = sets.folder_of(o["key"])
            try:
                r = sets.complete(ix, folder)
                print(f"upload set {folder or '(bucket root)'}: ready, {len(r['files'])} files")
            except (sets.SetIncomplete, sets.SetError) as e:
                print(f"upload set {e}")
    return 1 if bad else 0


def cmd_set(a) -> int:
    """Status of an upload set; with --wait, block until it's ready (for upload scripts).
    Exit 0: ready. 1: not ready (yet). 2: a file in it can't be read, so waiting won't help."""
    from . import sets

    ix = _index()
    deadline = time.monotonic() + a.wait
    while True:
        r = sets.status(ix, a.folder)
        if r["state"] == "processing" and time.monotonic() < deadline:
            try:  # don't rely on a watcher: finish the set here if every file is in the bucket
                sets.complete(ix, r["folder"])
                continue
            except sets.SetIncomplete:
                time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
                continue
            except sets.SetFailed:
                r = sets.status(ix, r["folder"])
        break
    name = r["folder"] or "(bucket root)"
    if r["state"] == "ready":
        print(f"{name}: ready, {len(r['files'])} files (completed {r['completed']})")
        for k, v in r["files"].items():
            print(f"  {k}  version={v}")
        if r["changed_since"]:
            print("  replaced since: " + ", ".join(r["changed_since"]))
        if r["deleted_since"]:
            print("  deleted since: " + ", ".join(r["deleted_since"]))
        return 0
    if r["state"] == "failed":
        print(f"{name}: failed, {len(r['failed'])} of {r['total']} files can't be read (upload fixed copies)")
        for k, why in r["failed"].items():
            print(f"  {k}: {why}")
        return 2
    if r["state"] == "processing":
        print(f"{name}: processing, {r['indexed']} of {r['total']} indexed; missing: {', '.join(r['missing']) or '-'}")
    elif r["state"] == "invalid":
        print(f"{name}: invalid marker: {r['error']}")
    else:
        print(f"{name}: no {sets.MARKER} marker")
    return 1


def cmd_serve(a) -> int:
    from .server import main

    config.load()  # fail fast with a clear message
    main()
    return 0


def cmd_watch(a) -> int:
    from . import watch

    ix = _index()
    if a.poll:
        watch.run_poll(ix, a.poll, a.prefix, once=a.once)
    else:
        watch.run_queue(ix, ix.store.cfg, once=a.once)
    return 0


def cmd_status(a) -> int:
    from .index import ROOT

    ix = _index()
    objs = ix.store.list()
    files = [o for o in objs if not o["key"].startswith(".bucketlens/")]
    idx = [o for o in objs if o["key"].startswith(".bucketlens/")]
    indexed = {o["key"][len(ROOT) + 1 :].rsplit("/", 2)[0] for o in idx if o["key"].endswith("manifest.json")}
    from .parse import kind_of

    readable = [o for o in files if kind_of(o["key"])]
    gb = sum(o["size"] for o in objs) / 1e9
    print(
        f"bucket {ix.store.bucket}: {len(files)} files, {len(readable)} readable (PDF/XLSX/CSV), "
        f"{len({o['key'] for o in readable} & indexed)} indexed; {gb:.3f} GB stored including the index"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bucketlens", description="Let AI agents read the documents and spreadsheets in your bucket."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="save bucket credentials and test them").set_defaults(fn=cmd_setup)
    sub.add_parser("check", help="test the saved credentials").set_defaults(fn=lambda a: _check())
    s = sub.add_parser("index", help="index every PDF/XLSX/CSV under a prefix now")
    s.add_argument("--prefix", default="")
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(fn=cmd_index)
    sub.add_parser("serve", help="run the MCP server (stdio)").set_defaults(fn=cmd_serve)
    s = sub.add_parser("watch", help="index files as they change (Cloudflare queue, or --poll SECONDS)")
    s.add_argument("--poll", type=int, default=0, help="poll the bucket every N seconds instead of using a queue")
    s.add_argument("--prefix", default="")
    s.add_argument("--once", action="store_true")
    s.set_defaults(fn=cmd_watch)
    sub.add_parser("status", help="files, index coverage and storage used").set_defaults(fn=cmd_status)
    s = sub.add_parser("set", help="is an upload set (folder with a _bucketlens.json marker) fully indexed?")
    s.add_argument("folder")
    s.add_argument(
        "--wait",
        type=int,
        default=0,
        help="wait up to N seconds for it to be ready (exit 0 ready, 1 not yet, 2 a file can't be read)",
    )
    s.set_defaults(fn=cmd_set)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except ConfigError as e:
        print(f"bucketlens: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
