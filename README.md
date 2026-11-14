# bucketlens

[![ci](https://github.com/Pritom14/bucketlens/actions/workflows/ci.yml/badge.svg)](https://github.com/Pritom14/bucketlens/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-3776ab)](pyproject.toml)
[![license](https://img.shields.io/badge/license-Apache--2.0-4f46e5)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-server-111827)](https://modelcontextprotocol.io)

Let any AI agent read the documents and spreadsheets in your own S3 or R2 bucket, without
downloading them.

Agents work best the way a developer does: look at what's there, search, open just the part
that matters. That works on code, because code is plain text on a local disk. A company's
knowledge is PDFs, spreadsheets and contracts in a bucket. An agent with a shell downloads and
parses them on every run. An agent without one (a chat app, Claude Desktop, a support bot, an
MCP-only client) often can't use them at all.

bucketlens is an MCP server. It parses each file once, keeps the result next to the file in your
bucket, and lets the agent ask for exactly what it needs: an outline, pages 36 to 38, a search
across 100 contracts, or a SQL query over a 400,000-row sheet.

## Results on real files

Claude Code (Sonnet) answered 10 questions about real public files: Berkshire Hathaway's 2025
annual report and 2026 quarterly reports, the World Bank's 82 MB World Development Indicators
workbook, and 100 commercial contracts from the CUAD dataset. Each question ran three ways.

| Setup | Right answers | Tokens | Cost | Time |
|---|---|---|---|---|
| Downloads the files, has a shell and Python | 10/10 | 226,231 | $0.21 | 405 s |
| Downloads the files, no shell (like a chat app) | 6/10 | 433,144 | $0.59 | 370 s |
| bucketlens (no files, no shell) | **10/10** | **159,280** | $0.23 | **141 s** |
| bucketlens, re-run with the released v0.1 package | **10/10** | 182,630 | $0.30 | 165 s |

By dataset:

| Dataset | Shell | No shell | bucketlens |
|---|---|---|---|
| Berkshire reports (154-page annual report, 3 quarterlies) | 5/5, 67 s | 5/5, 2.9x the tokens | 5/5, 48 s |
| World Bank workbook (82 MB, 398,468 rows) | 3/3, 240 s | **0/3**, can't open .xlsx | 3/3, 52 s |
| 100 contracts (1,481 pages) | 2/2, 98 s | **1/2** | 2/2, 41 s |

What this says, honestly:

- **Agents without a shell are where bucketlens changes the outcome.** They can't open a
  spreadsheet at all, and across many files they give up or spend many turns reading.
- **Agents with a shell get the same answers either way.** bucketlens is about 2.5x faster for them
  (no downloads; the 82 MB workbook alone took 20 to 65 s to download) and uses fewer tokens. It is
  not cheaper: $0.23 and $0.30 across two runs, against $0.21, because tool results are fresh input
  while a shell agent's long prompt is mostly cached.

The answers were checked automatically against values taken from the files. The 2026 quarterly
figures are newer than the model's training data, so they had to be read, not remembered.
Details and earlier runs on synthetic data: [benchmarks/RESULTS.md](benchmarks/RESULTS.md).

## Quick start

```bash
pip install bucketlens            # or: uv tool install bucketlens
bucketlens setup                  # bucket + keys, typed hidden, saved to ~/.config/bucketlens/config.env
bucketlens index                  # parse every PDF, XLSX and CSV once (optional; files are also indexed on first use)
claude mcp add bucketlens -- bucketlens serve
```

Any MCP client works: run `bucketlens serve` over stdio.

Works with Cloudflare R2, AWS S3, MinIO and other S3-compatible storage. Give it a key that can
read the bucket and write under `.bucketlens/` (where the index lives).

## Tools the agent gets

| Tool | What it returns |
|---|---|
| `list_files(prefix)` | names, sizes, dates; nothing is downloaded |
| `outline(path, depth)` | PDF: pages, table of contents with page numbers, token cost of reading it whole. Spreadsheet: sheets, rows, columns and types |
| `search(path, text)` | pages of one PDF that contain the text, with snippets |
| `search_all(text, folder, files_only)` | every PDF in a folder at once, as compact hits or just file names and counts |
| `read_pages(path, pages)` | text of specific pages, e.g. `"36-38"` |
| `query(path, sql)` | read-only SQL over a spreadsheet or CSV; each sheet is a table |
| `read_rows(path, sheet, first, last)` | rows by the spreadsheet's own row numbers |

## How it works

The first time a file is touched (or when it's saved, with `watch`), bucketlens reads it once:

- **PDF**: each page's text goes into one `pages.txt` with byte offsets, plus the outline. Reading
  pages 600 to 602 of a 733-page PDF is then one range request (about 150 KB).
- **XLSX / CSV**: each sheet becomes a Parquet file with a `_row` column. DuckDB reads only the
  columns and row groups a query needs (rows 150,000 to 150,049 of a 20 MB workbook: about 190 KB).

Everything is stored under `.bucketlens/index/<file>/<etag>/`. When a file changes its ETag
changes, so an agent is never answered from an old version.

Why not just read parts of the original file? We measured it: opening a PDF or the first rows of
a workbook fetches 5 to 7% of the file, but deep pages and deep rows fetch 50 to 75%, because of
how PDF and XLSX are laid out inside. Parsing once and serving slices is what makes every read
small.

## Keeping the index fresh

```bash
bucketlens watch              # Cloudflare R2: event notifications -> Queue, pulled over HTTP
bucketlens watch --poll 300   # any S3: list the bucket every 5 minutes
```

With R2 event notifications, a new 492-page PDF was indexed 8.8 s after its upload started, an
edited workbook 4.4 s after it was saved, and a deleted file's index removed 3 s later. To set it
up: create a Queue, add an event notification on the bucket (object creation and deletion) that
sends to it, and add a Cloudflare API token with **Account > Queues > Edit** to the config as
`BUCKETLENS_CF_API_TOKEN`, with `BUCKETLENS_CF_QUEUE` set to the queue's name.

## Costs

bucketlens itself is free. On Cloudflare R2, the free tier (10 GB of storage, 1 million writes and
10 million reads a month, free downloads) covered everything in the benchmarks. The index adds
about 25% to stored bytes for these files. The watcher polls once a minute when idle, about
1,440 Queue calls a day against a free allowance of 10,000. R2 has no hard spending cap, so set a
billing alert.

## Security

- Keys stay on your machine, in a file only you can read. bucketlens never prints them.
- SQL from agents is read-only: one statement, and DuckDB is locked so it can't read or write local
  files or change its own settings.
- The agent can read anything in the bucket the key can read. Use a key scoped to one bucket.

## Limits

- Reads PDF, XLSX/XLSM and CSV. Word and PowerPoint are next.
- Scanned PDFs without a text layer aren't searchable yet (no OCR).
- The first touch of a file reads it whole; use `watch` or `index` so agents never wait on it.
- Benchmarks used one agent (Claude Code) and one model (Sonnet).

## Reproduce the benchmarks

See [benchmarks/README.md](benchmarks/README.md). The public files are fetched by a script, not
stored in this repo.

## License

Apache-2.0
