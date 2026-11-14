"""Turn a file into what agents read: per-page text for PDFs, Parquet tables for spreadsheets."""

from __future__ import annotations

import io
import re
import threading
from pathlib import Path

import duckdb
import pypdfium2 as pdfium
from openpyxl import load_workbook

PDFIUM_LOCK = threading.RLock()  # pdfium must never run in two threads at once

PDF = (".pdf",)
SHEETS = (".xlsx", ".xlsm")
CSV = (".csv", ".tsv")
SUPPORTED = PDF + SHEETS + CSV


def kind_of(key: str) -> str | None:
    k = key.lower()
    if k.endswith(PDF):
        return "pdf"
    if k.endswith(SHEETS):
        return "xlsx"
    if k.endswith(CSV):
        return "csv"
    return None


def parse_pdf(data: bytes) -> dict:
    """Every page's text (back to back, with byte offsets), the outline, and the title."""
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(data)
        try:
            buf, offsets, tokens = bytearray(), [], []
            for i in range(len(doc)):
                text = doc[i].get_textpage().get_text_range().strip().encode()
                offsets.append([len(buf), len(buf) + len(text)])
                tokens.append(len(text) // 4)
                buf += text
            outline = []
            for item in doc.get_toc(max_depth=16):
                dest = item.get_dest()
                idx = dest.get_index() if dest else None
                outline.append(
                    {"title": item.get_title().strip(), "page": None if idx is None else idx + 1, "level": item.level}
                )
            title = (doc.get_metadata_dict(skip_empty=True) or {}).get("Title", "")
        finally:
            doc.close()
    return {
        "pages": len(offsets),
        "title": title,
        "outline": outline,
        "page_offsets": offsets,
        "page_tokens": tokens,
        "text": bytes(buf),
    }


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def duck() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL excel; LOAD excel; SET preserve_insertion_order = true;")
    return con


def sheet_select(con: duckdb.DuckDBPyConnection, src: Path, sheet: str, slot: int = 0) -> str:
    """SQL reading one sheet plus ``_row``, its Excel row number.

    DuckDB guesses column types from the first rows. Real workbooks break that guess (a column of
    numbers with text further down), so on failure every cell is read as text and each column gets
    the narrowest type that fits all of its values."""
    s = sheet.replace("'", "''")
    typed = f"SELECT row_number() OVER () + 1 AS _row, * FROM read_xlsx('{src}', sheet='{s}', all_varchar=false)"
    try:
        con.execute(f"SELECT count(*) FROM ({typed})").fetchone()
        return typed
    except duckdb.Error:
        pass
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE _raw{slot} AS SELECT row_number() OVER () + 1 AS _row, * "
        f"FROM read_xlsx('{src}', sheet='{s}', all_varchar=true)"
    )
    exprs = ["_row"]
    for (col,) in con.execute(f"SELECT column_name FROM (DESCRIBE _raw{slot}) WHERE column_name <> '_row'").fetchall():
        q = '"' + col.replace('"', '""') + '"'
        chosen = "VARCHAR"
        for typ in ("BIGINT", "DOUBLE", "DATE", "TIMESTAMP"):
            bad = con.execute(
                f"SELECT count(*) FROM _raw{slot} WHERE {q} IS NOT NULL AND trim({q}) <> '' "
                f"AND TRY_CAST({q} AS {typ}) IS NULL"
            ).fetchone()[0]
            if bad == 0:
                chosen = typ
                break
        exprs.append(q if chosen == "VARCHAR" else f"TRY_CAST(NULLIF(trim({q}), '') AS {chosen}) AS {q}")
    return f"SELECT {', '.join(exprs)} FROM _raw{slot}"


def parse_table_file(data: bytes, kind: str, workdir: Path, stem: str) -> list[dict]:
    """Write one Parquet file per sheet (or one for a CSV) into ``workdir``.

    Returns [{name, rows, columns, path}]. Each table has ``_row``: the row number as the user sees it
    (Excel's row number for sheets, the line number for CSV; row 1 is the header)."""
    con = duck()
    try:
        src = workdir / ("book.xlsx" if kind == "xlsx" else "data.csv")
        src.write_bytes(data)
        if kind == "xlsx":
            sheets = [
                (name, sheet_select(con, src, name, i))
                for i, name in enumerate(load_workbook(io.BytesIO(data), read_only=True).sheetnames)
            ]
        else:
            sheets = [(stem, f"SELECT row_number() OVER () + 1 AS _row, * FROM read_csv_auto('{src}', sample_size=-1)")]
        out = []
        for name, sql in sheets:
            path = workdir / f"{safe_name(name)}.parquet"
            con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 10000, COMPRESSION zstd)")
            cols = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()
            rows = con.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]
            out.append(
                {"name": name, "rows": rows, "columns": [[c[0], c[1]] for c in cols if c[0] != "_row"], "path": path}
            )
        return out
    finally:
        con.close()


def parse_pages(spec: str, count: int) -> list[int]:
    """'36-38,40' -> [36, 37, 38, 40] (1-based), clipped to the document."""
    out: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        out += [p for p in range(lo, hi + 1) if 1 <= p <= count]
    return out
