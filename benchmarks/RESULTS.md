# bucketlens: development log and full results (14 Nov 2026)

Test files: NIST SP 800-53 r5 (492 pages, 6.07 MB), NIST SP 800-53A r5 (733 pages, 7.47 MB),
and a synthetic sales workbook saved the way Excel saves files (3 sheets, 300,000 rows, 20.54 MB).
The bucket is a local S3 server (`rclone serve s3`), so the timings say nothing about network
latency yet. The byte counts carry over to R2 unchanged.

## 1. Tokens: why the agent can't just read the file

| File | Whole file as text | What the agent asked for |
|---|---|---|
| 492-page PDF | ~396,000 tokens | pages 36-38: ~2,800 tokens |
| 733-page PDF | ~499,000 tokens | pages 600-602: ~2,500 tokens |

## 2. Reading in place, no index (pdfium for PDF, openpyxl for Excel)

| Read | Fetched |
|---|---|
| PDF: open, page count, table of contents | 5% |
| PDF: table of contents with page numbers | 74% |
| PDF: pages 36-38 | 18% |
| PDF: pages 600-602 of 733 | 72% |
| Excel: sheets, sizes, headers | 7% |
| Excel: first 50 rows, or the 2nd sheet | 7% |
| Excel: rows 150,000-150,049 | 54% |

Lesson: range reads alone help with opening files and their first parts. Deep pages of some PDFs
and deep rows of spreadsheets still pull most of the file. pypdf read 100% because it checks every
object when it opens a file, so it was replaced with pdfium.

## 3. Parse once, then serve slices (index stored in the bucket next to the file)

First touch reads the whole file once (6-21 MB, 0.6-3 s locally). After that:

| Read | Fetched | Requests |
|---|---|---|
| PDF outline (492 pages) | 37.5 KB (0.6%) | 2 |
| PDF pages 36-38 | 48.3 KB (0.8%) | 3 |
| PDF pages 600-602 of 733 | 151 KB (2.0%) | 3 |
| Excel outline | 1.1 KB (0.01%) | 2 |
| Excel rows 150,000-150,049 | 190 KB (0.9%) | 3 |
| Excel SQL: 2025 revenue by region | 1.99 MB (9.7%) | 62 |

The index is keyed by the file's ETag, so a changed file is re-indexed, never served stale.

## 4. Finder drive without a kernel extension

`rclone nfsmount` mounted the bucket as "bucketlens" with no macFUSE, no admin password and no
reboot. With rclone's defaults it read whole files (16 MB read-ahead). With `--buffer-size 0
--vfs-read-ahead 0`: opening the PDF and reading page 37 fetched 1.65 MB of 6.07 MB, and reading
1 MB from the middle of the workbook fetched 1.08 MB of 20.54 MB.

JuiceFS was not used: on Apple Silicon its Mac mount needs macFUSE's kernel extension (Recovery
mode, lower startup security). Its kernel-extension-free route is unreleased and showed stale reads
(juicedata/juicefs#7814).

## 5. MCP server

`src/bucketlens/server.py`: list_files, outline, read_pages, read_rows, query. Each answer ends
with how much was fetched. Tested over stdio with the MCP client (`src/try_mcp.py`).

## 6. Cloudflare R2 (bucket location APAC, from India)

One small read takes about 200-240 ms. Uploading the 34 MB of test files took about 9 s.

| Read | No index | From the index |
|---|---|---|
| PDF outline (492 pages) | 6.2 s (74% of file) | 0.41 s (37 KB) |
| PDF pages 36-38 | 1.5 s (18%) | 0.59 s (48 KB) |
| PDF pages 600-602 of 733 | 6.3 s (72%) | 0.59 s (151 KB) |
| Excel outline | 3.3 s (7%) | 0.39 s (1 KB) |
| Excel rows 150,000-150,049 | 14.4 s (54%) | 0.48 s cold, 0.18 s warm |
| Excel SQL: 2025 revenue by region | n/a | 0.99 s cold, 0.18 s warm |
| Download the whole file instead | 1.6-7.1 s | |

Without an index, every request costs a round trip, so many small reads lose to one download.
With the index, each read is 2-3 requests. Building an index is a one-time full read
(4-13 s per file here).

Finder drive on R2 (`mount_r2.sh`): listing instant; opening the 492-page PDF 1.06 s, page 37
ready at 2.33 s with 1.65 MB of 6.07 MB fetched; 1 MB from the middle of the workbook in 1.42 s
(1.08 MB fetched); the same page again from cache in 13 ms.

## 7. Agent test: Claude Code (Sonnet), 10 questions, files on R2

- download: the file is downloaded into the agent's folder; Read, Bash, Grep, Glob, python3 with
  openpyxl/pypdf/pypdfium2/duckdb, and pdftotext.
- bucketlens: no file or shell tools at all, only the MCP server (outline, search, read_pages,
  read_rows, query).

| Q | download: right / tokens / cost / time | bucketlens: right / tokens / cost / time |
|---|---|---|
| P1 AC-17 start page | yes / 31,120 / $0.024 / 41s | yes / 9,181 / $0.021 / 11s |
| P2 SI-2 start page | yes / 22,965 / $0.021 / 25s | yes / 9,315 / $0.022 / 12s |
| P3 SI-04 start page (733 pp) | yes / 45,838 / $0.041 / 80s | yes / 15,588 / $0.030 / 15s |
| P4 what is on page 600 | yes / 15,753 / $0.019 / 14s | yes / 10,330 / $0.026 / 9s |
| P5 CP-09 start page | yes / 31,086 / $0.026 / 85s | yes / 22,538 / $0.036 / 14s |
| X1 East 2025 revenue | yes / 24,492 / $0.026 / 60s | yes / 14,731 / $0.023 / 17s |
| X2 returned orders | yes / 23,666 / $0.022 / 36s | yes / 13,417 / $0.015 / 13s |
| X3 top product by qty | yes / 24,366 / $0.025 / 35s | yes / 18,945 / $0.021 / 17s |
| X4 one order lookup | yes / 14,861 / $0.016 / 24s | yes / 13,408 / $0.015 / 12s |
| X5 rep with most cancels 2024 | yes / 24,378 / $0.026 / 32s | yes / 18,673 / $0.019 / 16s |
| **Total** | **10/10, 258,525 tokens, $0.25, 432 s** | **10/10, 146,126 tokens, $0.23, 135 s** |

Times include the download (4-8 s per file). Before the search tool was added, the bucketlens PDF
runs used 171,806 tokens ($0.28) because the only way to locate a page was the full table of
contents; with search() they used 66,952 tokens ($0.14).

Reading of the result: both agents got everything right. The download agent never hit a token
limit because it had a shell, pdftotext and Python. The drive's win here is speed (3.2x) and
needing no tools, disk or setup in the sandbox; token use fell 43% but cost was about equal.

## 8. Three setups, bigger files, many files (Claude Code, Sonnet, R2)

- download: file(s) downloaded; shell, Python with PDF/Excel libraries, pdftotext.
- read_only: file(s) downloaded, but only Read/Grep/Glob, no shell (like a chat app or an MCP-only client).
- bucketlens: no file or shell tools; only the MCP server.

| Test | download | read_only | bucketlens |
|---|---|---|---|
| 5 PDF questions (492 and 733 pages) | 5/5, 146,762 tok, $0.13, 245 s | 5/5, 357,035 tok, $0.47, 138 s | 5/5, 66,952 tok, $0.14, 60 s |
| 5 spreadsheet questions (300k rows) | 5/5, 111,763 tok, $0.12, 187 s | **0/5** (can't open .xlsx) | 5/5, 79,174 tok, $0.09, 75 s |
| 3 questions on a 74 MB, 1M-row workbook | 3/3, 90,667 tok, $0.09, 245 s | not run (can't open .xlsx) | 3/3, 51,636 tok, $0.07, 69 s |
| 4 questions across 80 contracts | 4/4, 212,254 tok, $0.23, 142 s | **2/4**, 1,325,570 tok, $1.71, 474 s | 4/4, 179,138 tok, $0.26, 87 s |

Times include downloads (15-34 s for the 74 MB workbook, about 16 s for the 80 contracts).
In the many-files test the no-shell agent spent 85+ turns and about $0.70 on each of the two it got
right, and gave up on the other two after reading 3-5 of the 80 contracts.

Indexing (one-time, sequential, from India): 74 MB workbook 65 s; 80 contracts 135 s.
Cross-file search cold: 3.8 s with 16 parallel fetches (48 s sequential).

Reading of the result:
- Agents without a shell are where the drive changes the outcome: 0/5 -> 5/5 on spreadsheets,
  2/4 -> 4/4 across many files, and 3-7x fewer tokens on PDFs.
- Agents with a shell get the same answers either way; the drive is 2-4x faster and uses fewer
  tokens on single files. Across many files its cost was about equal or slightly higher, because
  search_all returns up to 100 snippets; it needs a more compact answer format.

Compact cross-file search (one short line per hit, or file names and counts only): the four
80-contract questions went from 179,138 to 147,355 tokens (download: 212,254), still 4/4 right;
cost stayed about equal ($0.26 vs $0.23 for download).

Agent runs so far: 57 runs, $3.91.

## 9. Index on save (R2 event notifications -> Queue -> watcher)

R2 sends object-create/delete events to the queue `bucketlens-events`; `watch.sh` pulls them over
HTTP and indexes each new or changed PDF/XLSX, and drops the index of deleted files.

| Event | Time until indexed |
|---|---|
| New 492-page PDF uploaded (6 MB) | 8.8 s after the upload started (upload itself 2.8 s) |
| New small workbook | 5.1 s |
| Same workbook overwritten with new numbers | 4.4 s; an agent query then returned the new value |
| File deleted | its index removed 3.0 s later |
| PDF copied into the Finder drive | the copy took 49 s; indexed 3.7 s after rclone's final upload |

Saving through Finder needs work: the copy blocked for 49 s, rclone uploaded an incomplete file
first (the indexer failed on it, then succeeded on the final upload), and macOS added `._` files.
The mount now excludes `._*` and `.DS_Store`, and the watcher ignores them.

Cost guard: the watcher polls every 2 s while events arrive and backs off to once a minute when idle
(at most ~1,440 pulls a day; the free Queues plan includes 10,000 operations a day). Bucket size
0.139 GB of the 10 GB free. R2 has no hard spending cap, so a billing notification is the backstop.

## 10. Real files (published results)

Berkshire Hathaway 2025 annual report and 2026 quarterly reports, the World Bank WDI workbook
(82 MB, 398,468 rows in its main sheet), and 100 CUAD contracts (1,481 pages). 10 questions, Sonnet.

| Dataset | download (shell) | read_only (no shell) | bucketlens |
|---|---|---|---|
| Berkshire reports, 5 Qs | 5/5, 83,682 tok, $0.09, 67 s | 5/5, 239,315 tok, $0.34, 94 s | 5/5, 59,058 tok, $0.10, 48 s |
| World Bank workbook, 3 Qs | 3/3, 69,553 tok, $0.07, 240 s | 0/3 (can't open .xlsx) | 3/3, 54,621 tok, $0.07, 52 s |
| 100 contracts, 2 Qs | 2/2, 72,996 tok, $0.06, 98 s | 1/2, 140,373 tok, $0.19, 118 s | 2/2, 45,601 tok, $0.06, 41 s |
| **All 10** | **10/10, 226,231 tok, $0.21, 405 s** | **6/10, 433,144 tok, $0.59, 370 s** | **10/10, 159,280 tok, $0.23, 141 s** |

Re-run with the released package (bucketlens v0.1 MCP server): 10/10, 182,630 tokens, $0.30, 165 s.

Two bugs found only on real files, both fixed with tests: DuckDB's column-type guess failed on the
World Bank "Country" sheet (text below numbers), and pdfium crashed when 12 files were indexed in
parallel (pdfium is not thread-safe; parsing is now serialized).
