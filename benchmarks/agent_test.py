"""Claude Code answers the same questions three ways and we compare right answers, tokens, cost and time.

- download:   the file(s) are downloaded into the agent's folder; it has Read, Bash, Grep, Glob, plus
              python3 with openpyxl/pypdf/pypdfium2/duckdb and pdftotext.
- read_only:  the file(s) are downloaded, but the agent only has Read, Grep and Glob (no shell), like a
              chat app or an MCP-only client.
- bucketlens: no files and no shell; only the bucketlens MCP server pointed at the bucket.

Usage:
  python benchmarks/agent_test.py --questions benchmarks/questions/real.json \
      --conditions download,read_only,bucketlens --parallel 4 [--only R1,W2] [--model sonnet]

Needs the `claude` CLI and a bucketlens config (`bucketlens setup`). Each run is capped by
AGENT_TEST_BUDGET dollars (default 0.40).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bucketlens import config
from bucketlens.store import Store

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "runs"
TAIL = (
    "\n\nWork it out from the file; do not answer from memory. End your reply with one line that starts with 'ANSWER:'."
)
TOOLS = ["list_files", "outline", "search", "search_all", "read_pages", "read_rows", "query"]


def _download(key: str, dest: Path) -> float:
    store, t = Store(config.load()), time.perf_counter()
    keys = [o["key"] for o in store.list(key)] if key.endswith("/") else [key]
    for k in keys:
        (dest / k).parent.mkdir(parents=True, exist_ok=True)
        (dest / k).write_bytes(store.get(k))
    return time.perf_counter() - t


def run_one(q: dict, condition: str, model: str) -> dict:
    key = q["path"]
    what = f"The folder {key}" if key.endswith("/") else f"The file {key}"
    with tempfile.TemporaryDirectory(prefix=f"bl-{q['id']}-{condition}-") as d:
        tmp, env, download_s = Path(d), {**os.environ}, 0.0
        if condition in ("download", "read_only"):
            download_s = _download(key, tmp)
            if condition == "download":
                env["PATH"] = f"{Path(sys.executable).parent}:{env['PATH']}"
                prompt = (
                    f"{what} has been downloaded into the current directory. {q['question']}\n\nTools available: "
                    "python3 (with openpyxl, pypdf, pypdfium2 and duckdb installed) and pdftotext." + TAIL
                )
                tools = ["Read", "Bash", "Grep", "Glob"]
            else:
                prompt = f"{what} has been downloaded into the current directory. {q['question']}" + TAIL
                tools = ["Read", "Grep", "Glob"]
            extra = ["--tools", ",".join(tools), "--allowedTools", ",".join(tools), "--strict-mcp-config"]
        else:
            exe = shutil.which("bucketlens") or str(Path(sys.executable).parent / "bucketlens")
            cfg = tmp / "mcp.json"
            cfg.write_text(json.dumps({"mcpServers": {"bucketlens": {"command": exe, "args": ["serve"]}}}))
            prompt = f"{what} is in the bucket that the bucketlens tools read. Use them. {q['question']}" + TAIL
            extra = [
                "--tools",
                "",
                "--mcp-config",
                str(cfg),
                "--strict-mcp-config",
                "--allowedTools",
                ",".join(f"mcp__bucketlens__{t}" for t in TOOLS),
            ]
        cmd = [
            "claude",
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
            "--max-turns",
            "40",
            "--max-budget-usd",
            os.environ.get("AGENT_TEST_BUDGET", "0.40"),
            *extra,
        ]
        t = time.perf_counter()
        try:
            raw = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, timeout=900).stdout
        except subprocess.TimeoutExpired:
            raw = ""
        agent_s = time.perf_counter() - t
    try:
        res = json.loads(raw)
    except Exception:
        res = {"result": raw[-2000:], "is_error": True}
    text = res.get("result") or ""
    ans = next(
        (ln.split(":", 1)[1].strip() for ln in reversed(text.splitlines()) if ln.strip().upper().startswith("ANSWER:")),
        text[-300:],
    )
    u = res.get("usage") or {}
    row = {
        "id": q["id"],
        "condition": condition,
        "path": key,
        "model": model,
        "answer": ans[:300],
        "correct": all(re.search(c, ans, re.I) for c in q["expect"]),
        "total_input_tokens": u.get("input_tokens", 0)
        + u.get("cache_creation_input_tokens", 0)
        + u.get("cache_read_input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "turns": res.get("num_turns"),
        "cost_usd": res.get("total_cost_usd"),
        "agent_seconds": round(agent_s, 1),
        "download_seconds": round(download_s, 1),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{q['id']}-{condition}.json").write_text(json.dumps({**row, "raw_result": text[-4000:]}, indent=2))
    print(
        f"{q['id']} {condition:10s} correct={row['correct']!s:5s} tokens={row['total_input_tokens'] + row['output_tokens']:>9,} "
        f"turns={row['turns']} {agent_s:6.1f}s (+{download_s:.1f}s download) ${row['cost_usd']}",
        flush=True,
    )
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(HERE / "questions" / "real.json"))
    ap.add_argument("--conditions", default="download,read_only,bucketlens")
    ap.add_argument("--only", default="")
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args()
    only = set(filter(None, a.only.split(",")))
    qs = [q for q in json.loads(Path(a.questions).read_text()) if not only or q["id"] in only]
    jobs = [(q, c) for q in qs for c in a.conditions.split(",")]
    with ThreadPoolExecutor(a.parallel) as ex:
        rows = list(ex.map(lambda j: run_one(j[0], j[1], a.model), jobs))
    for c in a.conditions.split(","):
        rs = [r for r in rows if r["condition"] == c]
        print(
            f"{c:10s} {sum(r['correct'] for r in rs)}/{len(rs)} right, "
            f"{sum(r['total_input_tokens'] + r['output_tokens'] for r in rs):,} tokens, "
            f"${sum(r['cost_usd'] or 0 for r in rs):.2f}, {sum(r['agent_seconds'] + r['download_seconds'] for r in rs):.0f}s"
        )


if __name__ == "__main__":
    main()
