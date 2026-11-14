from __future__ import annotations

import pytest
from conftest import make_pdf, make_xlsx

from bucketlens import config
from bucketlens.store import Stats

PAGES = [
    "Introduction. Nothing to see here.",
    "Section two: the annual contract value is USD 125,000.",
    "Section three: governing law is the State of Arizona.",
]


def test_config_from_env_and_repr_hides_secrets(cfg):
    assert cfg.bucket == "bl-test"
    assert "testing" not in repr(cfg)


def test_config_missing_settings(monkeypatch, tmp_path):
    for k in config.KEYS:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(config.ConfigError):
        config.load(tmp_path / "absent.env")


def test_write_env_file_is_private(tmp_path):
    p = tmp_path / "c.env"
    config.write_env_file(p, {"BUCKETLENS_BUCKET": "x"})
    config.write_env_file(p, {"BUCKETLENS_REGION": "auto"})
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert config.read_env_file(p) == {"BUCKETLENS_BUCKET": "x", "BUCKETLENS_REGION": "auto"}


def test_pdf_index_outline_pages_search(store, ix):
    store.put("docs/a.pdf", make_pdf(PAGES))
    st = Stats()
    o = ix.outline("docs/a.pdf", st)
    assert o["pages"] == 3 and o["tokens_if_read_whole"] > 0
    r = ix.read_pages("docs/a.pdf", "2", Stats())
    assert "--- page 2 ---" in r["text"] and "125,000" in r["text"] and "Arizona" not in r["text"]
    hits = ix.search("docs/a.pdf", r"governing law", Stats())
    assert hits["matches"] == 1 and hits["hits"][0]["page"] == 3


def test_reading_pages_after_indexing_is_small(store, ix):
    store.put("docs/big.pdf", make_pdf([f"Page {i} " + "lorem ipsum " * 300 for i in range(60)]))
    m = ix.manifest("docs/big.pdf")
    st = Stats()
    ix.read_pages("docs/big.pdf", "30-31", st)
    whole_text = m["page_offsets"][-1][1]
    assert st.bytes_fetched < whole_text / 10  # 2 of 60 pages, plus the small manifest


def test_search_all_and_files_only(store, ix):
    store.put("contracts/c1.pdf", make_pdf(["governing law is the State of Arizona."]))
    store.put("contracts/c2.pdf", make_pdf(["governing law is the State of New York."]))
    store.put("contracts/c3.pdf", make_pdf(["this one is governed by Arizona law too", "and Arizona again"]))
    r = ix.search_all("contracts/", "arizona", Stats())
    assert r["files_searched"] == 3 and r["files_matched"] == 2 and r["matches"] == 3
    assert set(r["per_file"]) == {"contracts/c1.pdf", "contracts/c3.pdf"}


def test_changed_file_gets_a_new_index_and_delete_removes_it(store, ix):
    store.put("docs/v.pdf", make_pdf(["version one"]))
    m1 = ix.manifest("docs/v.pdf")
    store.put("docs/v.pdf", make_pdf(["version two"]))
    m2 = ix.manifest("docs/v.pdf")
    assert m1["etag"] != m2["etag"]
    assert "version two" in ix.read_pages("docs/v.pdf", "1", Stats())["text"]
    assert ix.remove("docs/v.pdf") >= 4
    assert store.list(".bucketlens/index/docs/v.pdf/") == []


def test_spreadsheet_rows_and_sql(store, ix):
    store.put("data/orders.xlsx", make_xlsx())
    o = ix.outline("data/orders.xlsx", Stats())
    names = {s["name"]: s for s in o["sheets"]}
    assert names["Orders"]["rows"] == 30_000
    rows = ix.read_rows("data/orders.xlsx", "Orders", 101, 103, Stats())["table"]
    assert "ORD-000100" in rows and "ORD-000102" in rows and "ORD-000103" not in rows
    total = ix.query("data/orders.xlsx", "SELECT sum(amount) AS s FROM \"Orders\" WHERE region = 'East'", Stats())
    assert str(float(sum(i for i in range(1, 30_001) if i % 3 == 2))) in total["table"]


def test_mixed_type_column_falls_back_to_text(store, ix):
    store.put("data/orders.xlsx", make_xlsx(10))
    r = ix.query("data/orders.xlsx", 'SELECT note FROM "Notes" WHERE code = 40', Stats())
    assert "see the appendix" in r["table"]


def test_agent_sql_cannot_read_local_files(store, ix):
    store.put("data/orders.xlsx", make_xlsx(10))
    with pytest.raises(Exception, match="(?i)disabled|permission"):
        ix.query("data/orders.xlsx", "SELECT * FROM read_csv('/etc/hosts')", Stats())


def test_csv(store, ix):
    store.put("data/people.csv", b"name,age\nAsha,31\nRavi,42\nMeera,29\n")
    r = ix.query("data/people.csv", 'SELECT name FROM "people" WHERE age > 30 ORDER BY age', Stats())
    assert r["table"].splitlines()[2:] == ["| Asha |", "| Ravi |"]


def test_server_tools(store, ix, monkeypatch):
    from bucketlens import server

    monkeypatch.setattr(server, "_index", ix)
    store.put("docs/a.pdf", make_pdf(PAGES))
    store.put("data/orders.xlsx", make_xlsx(50))
    assert "docs/a.pdf" in server.list_files()
    assert "3 pages" in server.outline("docs/a.pdf")
    assert "125,000" in server.read_pages("docs/a.pdf", "2")
    assert "1 matches in 1 of 1 files" in server.search_all("Arizona", "docs/", files_only=True)
    assert "Only a single read-only query" in server.query("data/orders.xlsx", "DELETE FROM Orders")
    assert "Only a single read-only query" in server.query("data/orders.xlsx", "SELECT 1; SELECT 2")
    assert "No such file" in server.outline("docs/missing.pdf")


def test_watch_events_and_poll(store, ix):
    from bucketlens import watch

    store.put("in/a.pdf", make_pdf(["hello"]))
    watch.handle_event(ix, {"action": "PutObject", "object": {"key": "in/a.pdf"}})
    assert ix.manifest("in/a.pdf", build=False) is not None
    watch.handle_event(ix, {"action": "PutObject", "object": {"key": "in/._a.pdf"}})  # macOS junk: ignored
    store.client.delete_object(Bucket=store.bucket, Key="in/a.pdf")
    watch.handle_event(ix, {"action": "DeleteObject", "object": {"key": "in/a.pdf"}})
    assert store.list(".bucketlens/index/in/a.pdf/") == []
    store.put("in/b.pdf", make_pdf(["world"]))
    watch.run_poll(ix, 1, "in/", once=True)
    assert ix.manifest("in/b.pdf", build=False) is not None


def test_two_sheets_needing_the_fallback(store, ix):
    store.put("data/orders.xlsx", make_xlsx(10))
    for sheet in ("Notes", "More notes"):
        r = ix.query("data/orders.xlsx", f'SELECT note FROM "{sheet}" WHERE code = 40', Stats())
        assert "see the appendix" in r["table"]


def test_release_versions_match():
    import json
    import re
    from pathlib import Path

    import bucketlens

    root = Path(__file__).resolve().parent.parent
    version = re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M).group(1)
    server = json.loads((root / "server.json").read_text())
    assert server["version"] == server["packages"][0]["version"] == version == bucketlens.__version__
    assert server["name"] in (root / "README.md").read_text()  # the registry's PyPI ownership check
