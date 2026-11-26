"""Parse each file once, keep the result next to it in the bucket, and answer agents from it.

The index of a file lives under ``.bucketlens/index/<key>/<etag>/``. The ETag changes whenever the
file changes, so a changed file is re-indexed and never answered from an old index.

- PDF: ``pages.txt`` (every page's text, back to back) and ``manifest.json`` with each page's byte
  range and the outline. Reading pages 36-38 is one small range request.
- XLSX / CSV: one Parquet file per sheet with a ``_row`` column, so DuckDB fetches only the row groups
  and columns a query needs.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb

from .parse import kind_of, parse_pages, parse_pdf, parse_table_file
from .store import Stats, Store, is_missing

ROOT = ".bucketlens/index"


class NotIndexable(ValueError):
    pass


class Index:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._texts: dict[tuple[str, str], list[str]] = {}
        self._conns: dict[tuple[str, str], duckdb.DuckDBPyConnection] = {}
        self._lock = threading.Lock()

    # -- manifests --------------------------------------------------------------------------------

    @staticmethod
    def prefix(key: str, etag: str) -> str:
        return f"{ROOT}/{key}/{etag}/"

    def manifest(
        self, key: str, stats: Stats | None = None, build: bool = True, etag: str | None = None
    ) -> dict | None:
        """The index manifest for the current version of ``key``; built (one full read) if missing.

        With ``etag``, the manifest of that exact version instead (e.g. one pinned by an upload set).
        It is never built: an old version's bytes are gone, only its index can remain."""
        if key.startswith(".bucketlens/"):
            raise NotIndexable("that is bucketlens' own index data")
        if etag:
            try:
                return json.loads(self.store.get(self.prefix(key, etag) + "manifest.json", stats))
            except Exception as e:
                if is_missing(e):
                    raise NotIndexable(f"{key} has no index for version {etag}") from None
                raise
        meta = self.store.head(key, stats)
        try:
            return json.loads(self.store.get(self.prefix(key, meta["etag"]) + "manifest.json", stats))
        except Exception as e:
            if not is_missing(e):
                raise
        return self.build(key, meta, stats) if build else None

    def build(self, key: str, meta: dict | None = None, stats: Stats | None = None) -> dict:
        kind = kind_of(key)
        if kind is None:
            raise NotIndexable(f"{key}: bucketlens reads PDF, XLSX and CSV files")
        meta = meta or self.store.head(key, stats)
        t0 = time.perf_counter()
        data = self.store.get(key, stats)
        prefix = self.prefix(key, meta["etag"])
        if kind == "pdf":
            parsed = parse_pdf(data)
            self.store.put(prefix + "pages.txt", parsed.pop("text"), "text/plain; charset=utf-8")
            m = {"kind": "pdf", **parsed, "text_key": prefix + "pages.txt"}
        else:
            with tempfile.TemporaryDirectory() as tmp:
                tables = parse_table_file(data, kind, Path(tmp), Path(key).stem)
                sheets = []
                for t in tables:
                    pkey = prefix + f"sheets/{t['path'].name}"
                    self.store.put(pkey, t["path"].read_bytes(), "application/vnd.apache.parquet")
                    sheets.append({"name": t["name"], "rows": t["rows"], "columns": t["columns"], "parquet_key": pkey})
            m = {"kind": "table", "sheets": sheets}
        m.update(
            {
                "key": key,
                "etag": meta["etag"],
                "size": meta["size"],
                "built_seconds": round(time.perf_counter() - t0, 2),
            }
        )
        self.store.put(prefix + "manifest.json", json.dumps(m).encode(), "application/json")  # written last
        return m

    def remove(self, key: str) -> int:
        """Drop every index version of ``key`` (after the file is deleted)."""
        return self.store.delete_prefix(f"{ROOT}/{key}/")

    def index_all(self, prefix: str = "", workers: int = 8) -> list[tuple[str, str]]:
        keys = [
            o["key"] for o in self.store.list(prefix) if not o["key"].startswith(".bucketlens/") and kind_of(o["key"])
        ]

        def one(k):
            try:
                self.manifest(k)
                return k, "ok"
            except Exception as e:
                return k, f"{type(e).__name__}: {e}"

        with ThreadPoolExecutor(workers) as ex:
            return list(ex.map(one, keys))

    # -- PDFs -------------------------------------------------------------------------------------

    def _pdf(self, key: str, stats: Stats, etag: str | None = None) -> dict:
        m = self.manifest(key, stats, etag=etag)
        if m["kind"] != "pdf":
            raise NotIndexable(f"{key} is not a PDF")
        return m

    def outline(self, key: str, stats: Stats, etag: str | None = None) -> dict:
        m = self.manifest(key, stats, etag=etag)
        if m["kind"] == "pdf":
            return {
                "kind": "pdf",
                "pages": m["pages"],
                "title": m["title"],
                "outline": m["outline"],
                "tokens_if_read_whole": sum(m["page_tokens"]),
            }
        return {"kind": "table", "sheets": [{k: s[k] for k in ("name", "rows", "columns")} for s in m["sheets"]]}

    def read_pages(self, key: str, pages: str, stats: Stats, etag: str | None = None) -> dict:
        m = self._pdf(key, stats, etag)
        want = parse_pages(pages, m["pages"])
        runs: list[list[int]] = []
        for p in want:  # neighbouring pages come back in one range request
            if runs and runs[-1][1] == p - 1:
                runs[-1][1] = p
            else:
                runs.append([p, p])
        parts = []
        for a, b in runs:
            start, end = m["page_offsets"][a - 1][0], m["page_offsets"][b - 1][1]
            blob = self.store.get_range(m["text_key"], start, end - 1, stats) if end > start else b""
            for p in range(a, b + 1):
                s, e = m["page_offsets"][p - 1]
                parts.append(f"--- page {p} ---\n{blob[s - start : e - start].decode(errors='replace')}")
        return {"pages": want, "page_count": m["pages"], "text": "\n\n".join(parts)}

    def _page_texts(self, m: dict, stats: Stats) -> list[str]:
        k = (m["key"], m["etag"])
        if k not in self._texts:
            blob = self.store.get(m["text_key"], stats)
            self._texts[k] = [blob[s:e].decode(errors="replace") for s, e in m["page_offsets"]]
        return self._texts[k]

    @staticmethod
    def _pattern(text: str) -> re.Pattern:
        try:
            return re.compile(text, re.IGNORECASE)
        except re.error:
            return re.compile(re.escape(text), re.IGNORECASE)

    def search(
        self, key: str, text: str, stats: Stats, max_hits: int = 20, context: int = 60, etag: str | None = None
    ) -> dict:
        m = self._pdf(key, stats, etag)
        pat, hits, total = self._pattern(text), [], 0
        for i, page in enumerate(self._page_texts(m, stats), start=1):
            for mt in pat.finditer(page):
                total += 1
                if len(hits) < max_hits:
                    a, b = max(0, mt.start() - context), min(len(page), mt.end() + context)
                    hits.append({"page": i, "snippet": " ".join(page[a:b].split())})
        return {"matches": total, "hits": hits}

    def search_all(self, prefix: str, text: str, stats: Stats, max_hits: int = 100, context: int = 30) -> dict:
        """Search every PDF under ``prefix``. Files without an index are indexed first."""
        keys = [
            o["key"]
            for o in self.store.list(prefix, stats)
            if o["key"].lower().endswith(".pdf") and not o["key"].startswith(".bucketlens/")
        ]

        def load(k):
            st = Stats()
            m = self.manifest(k, st)
            return k, self._page_texts(m, st), st

        with ThreadPoolExecutor(16) as ex:
            loaded = list(ex.map(load, keys))
        pat, hits, total, per_file = self._pattern(text), [], 0, {}
        for k, texts, st in loaded:
            stats.add(st)
            for i, page in enumerate(texts, start=1):
                for mt in pat.finditer(page):
                    total += 1
                    per_file[k] = per_file.get(k, 0) + 1
                    if len(hits) < max_hits:
                        a, b = max(0, mt.start() - context), min(len(page), mt.end() + context)
                        hits.append({"file": k, "page": i, "snippet": " ".join(page[a:b].split())})
        return {
            "files_searched": len(keys),
            "files_matched": len(per_file),
            "matches": total,
            "hits": hits,
            "per_file": per_file,
        }

    # -- spreadsheets ------------------------------------------------------------------------------

    def _duck(self) -> duckdb.DuckDBPyConnection:
        cfg = self.store.cfg
        con = duckdb.connect()
        con.execute("INSTALL httpfs; LOAD httpfs; SET enable_http_metadata_cache = true;")
        con.execute(
            "CREATE SECRET bucketlens (TYPE S3, KEY_ID ?, SECRET ?, ENDPOINT ?, REGION ?, URL_STYLE 'path', USE_SSL ?)",
            [cfg.access_key_id, cfg.secret_access_key, cfg.endpoint_host, cfg.region, cfg.use_ssl],
        )
        return con

    def _table_conn(self, m: dict) -> duckdb.DuckDBPyConnection:
        k = (m["key"], m["etag"])
        with self._lock:
            con = self._conns.get(k)
            if con is None:
                con = self._duck()
                for s in m["sheets"]:
                    url = f"s3://{self.store.bucket}/{s['parquet_key']}".replace("'", "''")
                    name = s["name"].replace('"', '""')
                    con.execute(f"CREATE VIEW \"{name}\" AS SELECT * FROM read_parquet('{url}')")
                # agent-written SQL must not read or write files on this machine, or loosen these settings
                con.execute("SET disabled_filesystems = 'LocalFileSystem'; SET lock_configuration = true;")
                self._conns[k] = con
        return con

    def query(self, key: str, sql: str, stats: Stats, max_rows: int = 200, etag: str | None = None) -> dict:
        """Read-only SQL over the sheets of a workbook (each sheet is a table named after it)."""
        m = self.manifest(key, stats, etag=etag)
        if m["kind"] != "table":
            raise NotIndexable(f"{key} is not a spreadsheet")
        t0 = time.perf_counter()
        cur = self._table_conn(m).cursor()
        try:
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
        finally:
            cur.close()
        stats.seconds += time.perf_counter() - t0
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        table = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        table += ["| " + " | ".join("" if v is None else str(v).replace("|", "\\|") for v in r) + " |" for r in rows]
        return {"columns": cols, "rows": len(rows), "truncated": truncated, "table": "\n".join(table)}

    def read_rows(self, key: str, sheet: str, first: int, last: int, stats: Stats, etag: str | None = None) -> dict:
        last = min(int(last), int(first) + 499)
        name = sheet.replace('"', '""')
        sql = (
            f'SELECT * EXCLUDE (_row), _row AS row_number FROM "{name}" '
            f"WHERE _row BETWEEN {int(first)} AND {last} ORDER BY _row"
        )
        return self.query(key, sql, stats, max_rows=500, etag=etag)
