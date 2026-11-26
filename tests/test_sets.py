"""Upload sets: a _bucketlens.json marker tells agents when files uploaded together are all indexed."""

from __future__ import annotations

import json

import pytest
from conftest import make_pdf, make_xlsx

from bucketlens import sets, watch
from bucketlens.index import NotIndexable
from bucketlens.store import Stats


def put_marker(store, folder: str, files: list[str] | None = None) -> None:
    body = {} if files is None else {"files": files}
    store.put(folder + sets.MARKER, json.dumps(body).encode(), "application/json")


def event(key: str, action: str = "PutObject") -> dict:
    return {"action": action, "object": {"key": key}}


def test_explicit_list_ready_with_pinned_versions(store, ix):
    store.put("q3/report.pdf", make_pdf(["Revenue was 125 crore."]))
    store.put("q3/numbers.csv", b"region,amount\nNorth,10\nSouth,20\n")
    put_marker(store, "q3/", ["report.pdf", "numbers.csv"])
    watch.handle_event(ix, event("q3/_bucketlens.json"))
    r = sets.status(ix, "q3")
    assert r["state"] == "ready" and r["changed_since"] == [] and r["skipped"] == []
    current = {o["key"]: o["etag"] for o in store.list("q3/")}
    assert r["files"] == {"q3/numbers.csv": current["q3/numbers.csv"], "q3/report.pdf": current["q3/report.pdf"]}


def test_marker_first_then_files_trickle_in(store, ix):
    put_marker(store, "q4/", ["a.pdf", "b.csv"])
    watch.handle_event(ix, event("q4/_bucketlens.json"))  # nothing uploaded yet: logged, not raised
    r = sets.status(ix, "q4/")
    assert r["state"] == "processing" and r["indexed"] == 0 and r["missing"] == ["q4/a.pdf", "q4/b.csv"]

    store.put("q4/a.pdf", make_pdf(["first"]))
    watch.handle_event(ix, event("q4/a.pdf"))
    r = sets.status(ix, "q4/")
    assert r["state"] == "processing" and r["indexed"] == 1 and r["missing"] == ["q4/b.csv"]

    store.put("q4/b.csv", b"x\n1\n")
    watch.handle_event(ix, event("q4/b.csv"))  # the last file completes the set
    assert sets.status(ix, "q4/")["state"] == "ready"


def test_empty_marker_means_everything_in_the_folder(store, ix):
    store.put("batch/one.pdf", make_pdf(["one"]))
    store.put("batch/sub/two.csv", b"a\n1\n")
    store.put("batch/notes.docx", b"not readable by bucketlens")
    put_marker(store, "batch/")
    r = sets.complete(ix, "batch/")
    assert set(r["files"]) == {"batch/one.pdf", "batch/sub/two.csv"}
    assert r["skipped"] == ["batch/notes.docx"]  # listed, not blocking


def test_read_the_pinned_version_after_a_file_is_replaced(store, ix, monkeypatch):
    from bucketlens import server

    monkeypatch.setattr(server, "_index", ix)
    store.put("q1/report.pdf", make_pdf(["Profit: 10 crore (as uploaded)"]))
    store.put("q1/data.xlsx", make_xlsx(20))
    put_marker(store, "q1/", ["report.pdf", "data.xlsx"])
    pinned = sets.complete(ix, "q1/")["files"]

    store.put("q1/report.pdf", make_pdf(["Profit: 99 crore (edited later)"]))
    r = sets.status(ix, "q1/")
    assert r["state"] == "ready" and r["changed_since"] == ["q1/report.pdf"]

    v = pinned["q1/report.pdf"]
    assert "10 crore" in ix.read_pages("q1/report.pdf", "1", Stats(), etag=v)["text"]
    assert "99 crore" in ix.read_pages("q1/report.pdf", "1", Stats())["text"]
    assert "10 crore" in server.read_pages("q1/report.pdf", "1", version=v)
    assert ix.search("q1/report.pdf", "as uploaded", Stats(), etag=v)["matches"] == 1
    assert "ORD-000005" in server.read_rows("q1/data.xlsx", "Orders", 6, 6, version=pinned["q1/data.xlsx"])
    store.client.delete_object(Bucket=store.bucket, Key="q1/data.xlsx")
    watch.handle_event(ix, event("q1/data.xlsx", "DeleteObject"))
    r = sets.status(ix, "q1/")
    assert r["deleted_since"] == ["q1/data.xlsx"] and r["changed_since"] == ["q1/report.pdf"]
    assert "Deleted since this upload (no longer readable): q1/data.xlsx" in server.set_status("q1/")
    with pytest.raises(NotIndexable):
        ix.outline("q1/report.pdf", Stats(), etag="0" * 32)
    assert "has no index for version" in server.outline("q1/report.pdf", version="0" * 32)


def test_set_status_tool_messages(store, ix, monkeypatch):
    from bucketlens import server

    monkeypatch.setattr(server, "_index", ix)
    assert "no upload marker" in server.set_status("nothing-here")
    put_marker(store, "up/", ["a.pdf"])
    assert "STILL PROCESSING, 0 of 1" in server.set_status("up/") and "up/a.pdf" in server.set_status("up/")
    store.put("up/a.pdf", make_pdf(["hi"]))
    sets.complete(ix, "up/")
    out = server.set_status("up")
    assert out.startswith("up/: READY, 1 files") and "up/a.pdf  version=" in out
    store.put("bad/" + sets.MARKER, b"{not json")
    assert "unusable" in server.set_status("bad/")


def test_invalid_markers(store, ix):
    store.put("x/" + sets.MARKER, b'{"files": ["../secrets.pdf"]}')
    with pytest.raises(sets.SetError):
        sets.complete(ix, "x/")
    store.put("y/" + sets.MARKER, b'["a.pdf"]')
    assert sets.status(ix, "y/")["state"] == "invalid"
    watch.handle_event(ix, event("x/_bucketlens.json"))  # logged and ignored, not raised


def test_poll_completes_sets_and_marker_delete_removes_records(store, ix):
    store.put("p/a.pdf", make_pdf(["a"]))
    put_marker(store, "p/", ["a.pdf", "b.pdf"])
    watch.run_poll(ix, 1, "p/", once=True)
    assert sets.status(ix, "p/")["state"] == "processing"
    store.put("p/b.pdf", make_pdf(["b"]))
    watch.run_poll(ix, 1, "p/", once=True)
    assert sets.status(ix, "p/")["state"] == "ready"
    assert store.list(".bucketlens/sets/p/")
    store.client.delete_object(Bucket=store.bucket, Key="p/" + sets.MARKER)
    watch.handle_event(ix, event("p/" + sets.MARKER, "DeleteObject"))
    assert store.list(".bucketlens/sets/p/") == []
    assert sets.status(ix, "p/")["state"] == "no_marker"


def test_new_marker_version_is_a_new_set(store, ix):
    store.put("r/a.pdf", make_pdf(["a"]))
    put_marker(store, "r/", ["a.pdf"])
    first = sets.complete(ix, "r/")
    store.put("r/b.pdf", make_pdf(["b"]))
    put_marker(store, "r/", ["a.pdf", "b.pdf"])  # next upload into the same folder
    assert sets.status(ix, "r/")["state"] == "processing"
    second = sets.complete(ix, "r/")
    assert first["marker_etag"] != second["marker_etag"] and set(second["files"]) == {"r/a.pdf", "r/b.pdf"}


def test_folder_normalisation():
    assert sets.folder_of("reports/q3") == "reports/q3/"
    assert sets.folder_of("/reports/q3/") == "reports/q3/"
    assert sets.folder_of("reports/q3/_bucketlens.json") == "reports/q3/"
    assert sets.folder_of("") == "" and sets.folder_of("_bucketlens.json") == ""
    assert sets.is_marker("a/_bucketlens.json") and not sets.is_marker(".bucketlens/sets/_bucketlens.json")


def test_cli_set_wait(store, ix, cfg, capsys):
    from bucketlens import cli

    store.put("c/a.csv", b"a\n1\n")
    put_marker(store, "c/", ["a.csv"])
    assert cli.main(["set", "c/", "--wait", "5"]) == 0  # completes it even without a watcher running
    assert "c/: ready, 1 files" in capsys.readouterr().out
    put_marker(store, "d/", ["missing.csv"])
    assert cli.main(["set", "d/"]) == 1
    assert "missing: d/missing.csv" in capsys.readouterr().out


BROKEN = b"%PDF-1.7 this is not really a PDF"


def test_unreadable_file_fails_the_set_instead_of_hanging(store, ix, monkeypatch):
    from bucketlens import server

    monkeypatch.setattr(server, "_index", ix)
    store.put("f/good.pdf", make_pdf(["fine"]))
    store.put("f/broken.pdf", BROKEN)
    put_marker(store, "f/", ["good.pdf", "broken.pdf"])
    watch.handle_event(ix, event("f/broken.pdf"))  # logged and acknowledged, not raised (no endless queue retries)
    watch.handle_event(ix, event("f/_bucketlens.json"))
    r = sets.status(ix, "f/")
    assert r["state"] == "failed" and r["total"] == 2 and r["indexed"] == 1
    assert list(r["failed"]) == ["f/broken.pdf"] and "PdfiumError" in r["failed"]["f/broken.pdf"]
    good = {o["key"]: o["etag"] for o in store.list("f/")}["f/good.pdf"]
    assert r["files"] == {"f/good.pdf": good}  # the rest is still readable, pinned
    out = server.set_status("f/")
    assert "FAILED" in out and "f/broken.pdf" in out and f"f/good.pdf  version={good}" in out
    assert "can't be read" in server.outline("f/broken.pdf")

    store.put("f/broken.pdf", make_pdf(["fixed"]))  # a fixed copy completes the set on its own
    watch.handle_event(ix, event("f/broken.pdf"))
    assert sets.status(ix, "f/")["state"] == "ready"


def test_failure_is_remembered_per_version_and_not_reparsed(store, ix, monkeypatch):
    from bucketlens import index as index_mod

    calls = []
    real = index_mod.parse_pdf
    monkeypatch.setattr(index_mod, "parse_pdf", lambda data: calls.append(1) or real(data))
    store.put("m/x.pdf", BROKEN)
    for _ in range(3):
        with pytest.raises(index_mod.Unreadable):
            ix.manifest("m/x.pdf")
    assert len(calls) == 1  # one download and parse; later calls answer from the recorded failure
    monkeypatch.setattr(index_mod, "__version__", "9.9.9")  # a new bucketlens release tries again
    with pytest.raises(index_mod.Unreadable):
        ix.manifest("m/x.pdf")
    assert len(calls) == 2


def test_storage_errors_are_retried_not_recorded_as_unreadable(store, ix, monkeypatch):
    from bucketlens import index as index_mod

    store.put("t/a.pdf", make_pdf(["a"]))
    real_get, flaky = store.get, {"t/a.pdf"}

    def get(key, stats=None):
        if key in flaky:
            flaky.discard(key)
            raise ConnectionError("network blip")
        return real_get(key, stats)

    monkeypatch.setattr(store, "get", get)
    with pytest.raises(ConnectionError):
        ix.manifest("t/a.pdf")
    assert not isinstance(ConnectionError(), index_mod.Unreadable)
    assert ix.manifest("t/a.pdf")["pages"] == 1  # next try works: nothing was recorded


def test_poll_survives_an_unreadable_file_and_recovers(store, ix):
    store.put("pf/good.csv", b"a\n1\n")
    store.put("pf/bad.pdf", BROKEN)
    put_marker(store, "pf/", ["good.csv", "bad.pdf"])
    watch.run_poll(ix, 1, "pf/", once=True)  # used to crash the whole watcher
    assert sets.status(ix, "pf/")["state"] == "failed"
    store.put("pf/bad.pdf", make_pdf(["fixed"]))
    watch.run_poll(ix, 1, "pf/", once=True)
    assert sets.status(ix, "pf/")["state"] == "ready"


def test_cli_set_exits_2_when_a_file_cant_be_read(store, ix, cfg, capsys):
    from bucketlens import cli

    store.put("cf/bad.pdf", BROKEN)
    put_marker(store, "cf/", ["bad.pdf"])
    assert cli.main(["set", "cf/", "--wait", "5"]) == 2
    out = capsys.readouterr().out
    assert "cf/: failed" in out and "cf/bad.pdf" in out
