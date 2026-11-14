"""Tests run against a local fake S3 (moto), never a real bucket."""

from __future__ import annotations

import io
import socket

import boto3
import pytest
from moto.server import ThreadedMotoServer

BUCKET = "bl-test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def s3_endpoint():
    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture()
def cfg(s3_endpoint, monkeypatch, tmp_path):
    monkeypatch.setenv("BUCKETLENS_CONFIG", str(tmp_path / "none.env"))
    for k, v in {
        "BUCKETLENS_ENDPOINT": s3_endpoint,
        "BUCKETLENS_REGION": "us-east-1",
        "BUCKETLENS_BUCKET": BUCKET,
        "BUCKETLENS_ACCESS_KEY_ID": "testing",
        "BUCKETLENS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(k, v)
    s3 = boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        region_name="us-east-1",
    )
    try:
        s3.create_bucket(Bucket=BUCKET)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []):
        s3.delete_object(Bucket=BUCKET, Key=o["Key"])
    from bucketlens import config

    return config.load(tmp_path / "none.env")


@pytest.fixture()
def store(cfg):
    from bucketlens.store import Store

    return Store(cfg)


@pytest.fixture()
def ix(store):
    from bucketlens.index import Index

    return Index(store)


def make_pdf(pages: list[str]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_font("Helvetica", size=11)
    for text in pages:
        pdf.add_page()
        pdf.multi_cell(0, 6, text)
    return bytes(pdf.output())


def make_xlsx(rows: int = 30_000) -> bytes:
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Orders")
    ws.write_row(0, 0, ["order_id", "region", "amount"])
    for i in range(1, rows + 1):
        ws.write_row(i, 0, [f"ORD-{i:06d}", ["North", "South", "East"][i % 3], float(i)])
    for title in ("Notes", "More notes"):  # two sheets that both need the text fallback
        _mixed(wb.add_worksheet(title))
    wb.close()
    return buf.getvalue()


def _mixed(mixed) -> None:  # numbers first, text later: breaks DuckDB's type guess
    mixed.write_row(0, 0, ["code", "note"])
    for i in range(1, 40):
        mixed.write_row(i, 0, [i, i * 2])
    mixed.write_row(40, 0, [40, "see the appendix"])
