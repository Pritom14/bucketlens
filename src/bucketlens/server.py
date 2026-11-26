"""MCP server: agents read the part of a file they need, straight from your bucket, without downloading it."""

from __future__ import annotations

import re

from mcp.server.mcpserver import MCPServer

from . import config, sets
from .index import Index, NotIndexable
from .store import Stats, Store

INSTRUCTIONS = (
    "bucketlens gives you the documents and spreadsheets in an S3/R2 bucket without downloading them. "
    "Start with list_files, then outline. For PDFs: search or search_all to locate, read_pages to read. "
    "For spreadsheets and CSVs: query with SQL (each sheet is a table named after it) or read_rows. "
    "Before relying on a folder of files uploaded together, call set_status(folder): it says whether the whole "
    "upload is indexed, and gives each file's version to pass to the read tools to read exactly that upload."
)

mcp = MCPServer("bucketlens", instructions=INSTRUCTIONS)
_index: Index | None = None


def index() -> Index:
    global _index
    if _index is None:
        _index = Index(Store(config.load()))
    return _index


def _fetched(st: Stats) -> str:
    return f"\n\n[bucketlens: fetched {st.bytes_fetched / 1e3:.1f} KB in {st.requests} requests]"


def _guard(fn):
    """Turn expected failures into a short message the agent can act on."""
    import functools

    @functools.wraps(fn)
    def run(*a, **kw):
        try:
            return fn(*a, **kw)
        except (NotIndexable, sets.SetError) as e:
            return f"Can't do that: {e}"
        except Exception as e:  # noqa: BLE001 - the agent needs the reason, not a stack trace
            msg = str(e).split("\n")[0][:300]
            if "404" in msg or "Not Found" in msg or "NoSuchKey" in msg:
                return "No such file. Use list_files to see what is in the bucket."
            return f"Error: {type(e).__name__}: {msg}"

    return run


@mcp.tool()
@_guard
def list_files(prefix: str = "") -> str:
    """List files in the bucket under an optional folder prefix: name, size, last modified. Nothing is downloaded."""
    rows = [o for o in index().store.list(prefix) if not o["key"].startswith(".bucketlens/")]
    if len(rows) > 500:
        return f"{len(rows)} files; showing the first 500. Narrow it with a prefix.\n" + "\n".join(
            f"{o['key']}\t{o['size'] / 1e6:.2f} MB" for o in rows[:500]
        )
    return "\n".join(f"{o['key']}\t{o['size'] / 1e6:.2f} MB\t{o['modified'][:10]}" for o in rows) or "(no files)"


@mcp.tool()
@_guard
def outline(path: str, depth: int = 1, version: str = "") -> str:
    """Start here for any file. PDF: page count, title, table of contents down to `depth` levels (1 = top
    level) with page numbers, and the token cost of reading it whole. Spreadsheet or CSV: each sheet's
    name, row count, and column names and types. `version`: optional, from set_status; empty = latest."""
    st = Stats()
    o = index().outline(path, st, etag=version or None)
    if o["kind"] == "pdf":
        lines = [f"{o['title'] or path}: {o['pages']} pages (about {o['tokens_if_read_whole']:,} tokens if read whole)"]
        shown = [it for it in o["outline"] if it["level"] < max(1, depth)]
        lines += [f"{'  ' * it['level']}{it['title']}  (p. {it['page']})" for it in shown]
        hidden = len(o["outline"]) - len(shown)
        if hidden:
            lines.append(
                f"({hidden} deeper entries not shown: outline(path, depth={depth + 1}), or search() for a term)"
            )
    else:
        lines = [f"{path}: {len(o['sheets'])} table(s)"]
        for s in o["sheets"]:
            cols = ", ".join(f"{c} {t}" for c, t in s["columns"])
            lines.append(f'- "{s["name"]}": {s["rows"]:,} rows; columns: {cols}')
        lines.append(
            'Query with SQL, e.g. SELECT * FROM "' + o["sheets"][0]["name"] + '" LIMIT 5. Row numbers match '
            "the spreadsheet (row 1 is the header)."
        )
    return "\n".join(lines) + _fetched(st)


@mcp.tool()
@_guard
def search(path: str, text: str, version: str = "") -> str:
    """Find the pages of one PDF that contain some text (case-insensitive; regex allowed). Up to 20 hits,
    each with its page number and a short snippet. `version`: optional, from set_status; empty = latest."""
    st = Stats()
    r = index().search(path, text, st, etag=version or None)
    lines = [f"{r['matches']} matches" + (" (first 20 shown)" if r["matches"] > 20 else "")]
    lines += [f"p. {h['page']}: ...{h['snippet']}..." for h in r["hits"]]
    return "\n".join(lines) + _fetched(st)


@mcp.tool()
@_guard
def search_all(text: str, folder: str = "", files_only: bool = False) -> str:
    """Search every PDF under a folder at once (case-insensitive; regex allowed).
    Default: one short line per hit (file, page, the match with a little context), up to 100 hits.
    files_only=True: only the matching file names and hit counts (cheapest; use it to count or list files).
    Tip: put the value you want inside the pattern, e.g. "contract value is (USD|INR) [0-9,]+"."""
    st = Stats()
    r = index().search_all(folder, text, st)
    head = f"{r['matches']} matches in {r['files_matched']} of {r['files_searched']} files"
    if files_only:
        return "\n".join([head] + [f"{f}: {n}" for f, n in r["per_file"].items()])
    lines = [head + (" (first 100 shown)" if r["matches"] > 100 else "")]
    lines += [f"{h['file']} p{h['page']}: {h['snippet']}" for h in r["hits"]]
    return "\n".join(lines)


@mcp.tool()
@_guard
def read_pages(path: str, pages: str, version: str = "") -> str:
    """Text of specific PDF pages, e.g. pages="36-38" or "12,40-41" (counting the first page as 1). Max 20 pages.
    `version`: optional, from set_status; empty = latest."""
    st = Stats()
    r = index().read_pages(path, pages, st, etag=version or None)
    if len(r["pages"]) > 20:
        return "Ask for 20 pages or fewer per call."
    if not r["pages"]:
        return f"No such pages; the document has {r['page_count']} pages."
    return r["text"] + _fetched(st)


_READ_ONLY = re.compile(r"^\s*(select|with|describe|summarize|show|pivot|from)\b", re.IGNORECASE)


@mcp.tool()
@_guard
def query(path: str, sql: str, version: str = "") -> str:
    """Run read-only SQL (DuckDB dialect) over a spreadsheet or CSV. Each sheet is a table named after it;
    the column _row holds the spreadsheet row number. Prefer aggregates and filters; at most 200 rows return.
    `version`: optional, from set_status; empty = latest."""
    body = sql.strip().rstrip(";")
    if not _READ_ONLY.match(body) or ";" in body:
        return "Only a single read-only query (SELECT / WITH / DESCRIBE / SUMMARIZE) is allowed."
    st = Stats()
    r = index().query(path, body, st, etag=version or None)
    return r["table"] + (" (truncated to 200 rows)" if r["truncated"] else "") + f"\n\n[bucketlens: {st.seconds:.2f}s]"


@mcp.tool()
@_guard
def read_rows(path: str, sheet: str, first_row: int, last_row: int, version: str = "") -> str:
    """Rows first_row..last_row of a sheet, using the spreadsheet's row numbers (row 1 is the header). Max 500.
    `version`: optional, from set_status; empty = latest."""
    st = Stats()
    r = index().read_rows(path, sheet, first_row, last_row, st, etag=version or None)
    return r["table"] + f"\n\n[bucketlens: {st.seconds:.2f}s]"


@mcp.tool()
@_guard
def set_status(folder: str) -> str:
    """Is a group of files uploaded together fully indexed? Works when the uploader wrote a _bucketlens.json
    marker last in the folder. Says ready / still processing (and what's missing) / no marker. A ready upload
    lists each file's version: pass it as `version` to outline, search, read_pages, query or read_rows to read
    exactly that upload, even if a file was replaced afterwards."""
    st = Stats()
    r = sets.status(index(), folder, st)
    name = r["folder"] or "(bucket root)"
    if r["state"] == "no_marker":
        return (
            f"{name}: no upload marker ({sets.MARKER}), so there's no way to tell whether an upload into this "
            "folder is complete. Files here are still readable one by one."
        )
    if r["state"] == "invalid":
        return f"{name}: the upload marker is unusable: {r['error']}"
    if r["state"] == "processing":
        lines = [f"{name}: STILL PROCESSING, {r['indexed']} of {r['total']} readable files indexed."]
        if r["missing"]:
            lines.append("Not uploaded yet: " + ", ".join(r["missing"][:20]))
        lines.append("Call set_status again in a little while before relying on this folder.")
        return "\n".join(lines)
    lines = [f"{name}: READY, {len(r['files'])} files indexed (completed {r['completed']})."]
    lines += [f"- {k}  version={v}" for k, v in r["files"].items()]
    if r["skipped"]:
        lines.append("In the upload but not readable by bucketlens (not PDF/XLSX/CSV): " + ", ".join(r["skipped"]))
    if r["deleted_since"]:
        lines.append("Deleted since this upload (no longer readable): " + ", ".join(r["deleted_since"]))
    if r["changed_since"]:
        lines.append(
            "Replaced since this upload: "
            + ", ".join(r["changed_since"])
            + ". Pass version= to read the upload's copy; leave it empty for the newest."
        )
    return "\n".join(lines)


def main() -> None:
    mcp.run()
