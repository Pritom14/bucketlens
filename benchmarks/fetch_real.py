"""Download the public files used by benchmarks/questions/real.json and upload them to your bucket under real/.

- Berkshire Hathaway: 2025 annual report and 2026 first-, second- and third-quarter reports (berkshirehathaway.com).
- World Bank World Development Indicators, Excel edition (CC BY 4.0).
- CUAD v1 contracts (The Atticus Project, CC BY 4.0): 100 picked with a fixed seed, renamed contract-001..100.

Usage: python benchmarks/fetch_real.py [--workdir /tmp/bucketlens-bench] [--no-upload]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import urllib.request
import zipfile
from pathlib import Path

import pypdfium2 as pdfium

UA = {"User-Agent": "Mozilla/5.0 (bucketlens benchmark)"}
BRK = ["2025ar/2025ar.pdf", "qtrly/1stqtr26.pdf", "qtrly/2ndqtr26.pdf", "qtrly/3rdqtr26.pdf"]
WDI = "https://databank.worldbank.org/data/download/WDI_EXCEL.zip"
CUAD = "https://zenodo.org/records/4595826/files/CUAD_v1.zip?download=1"


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=300) as r, dest.open("wb") as f:
            shutil.copyfileobj(r, f)
    return dest


def pick_contracts(cuad_dir: Path, out: Path, n: int = 100, seed: int = 5) -> list[dict]:
    """Same selection as the published results: shuffle with seed 5, keep text PDFs with 3+ pages."""
    rows = {r["Filename"]: r for r in csv.DictReader((cuad_dir / "master_clauses.csv").open(encoding="utf-8"))}
    pdfs = {p.name: p for p in (cuad_dir / "full_contract_pdf").rglob("*") if p.suffix.lower() == ".pdf"}
    cands = sorted(set(pdfs) & set(rows))
    random.Random(seed).shuffle(cands)
    chosen = []
    for fn in cands:
        try:
            d = pdfium.PdfDocument(str(pdfs[fn]))
            ok = len(d) >= 3 and len(d[0].get_textpage().get_text_range()) > 800
            d.close()
        except Exception:
            continue
        if ok:
            chosen.append(fn)
        if len(chosen) == n:
            break
    out.mkdir(parents=True, exist_ok=True)
    meta = []
    for i, fn in enumerate(chosen, 1):
        shutil.copy(pdfs[fn], out / f"contract-{i:03d}.pdf")
        meta.append({"file": f"contract-{i:03d}.pdf", "source": fn, "governing_law": rows[fn]["Governing Law-Answer"]})
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="/tmp/bucketlens-bench")
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args()
    w = Path(a.workdir)
    files: list[tuple[Path, str]] = []
    for p in BRK:
        files.append(
            (
                fetch(f"https://www.berkshirehathaway.com/{p}", w / "reports" / Path(p).name),
                f"real/reports/{Path(p).name}",
            )
        )
    wz = fetch(WDI, w / "wdi" / "WDI_EXCEL.zip")
    with zipfile.ZipFile(wz) as z:
        z.extract("WDIEXCEL.xlsx", w / "wdi")
    files.append((w / "wdi" / "WDIEXCEL.xlsx", "real/wdi/WDIEXCEL.xlsx"))
    cz = fetch(CUAD, w / "cuad" / "CUAD_v1.zip")
    if not (w / "cuad" / "CUAD_v1").exists():
        with zipfile.ZipFile(cz) as z:
            z.extractall(w / "cuad")
    meta = pick_contracts(w / "cuad" / "CUAD_v1", w / "contracts")
    (w / "contracts_meta.json").write_text(json.dumps(meta, indent=1))
    files += [(w / "contracts" / m["file"], f"real/contracts/{m['file']}") for m in meta]
    print(f"{len(files)} files ready in {w}")
    if not a.no_upload:
        from bucketlens import config
        from bucketlens.store import Store

        s = Store(config.load())
        for src, key in files:
            s.client.upload_file(str(src), s.bucket, key)
        print(f"uploaded to bucket {s.bucket} under real/. Next: bucketlens index --prefix real/")


if __name__ == "__main__":
    main()
