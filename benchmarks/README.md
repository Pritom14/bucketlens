# Benchmarks

`agent_test.py` asks Claude Code the same questions three ways (shell + downloads, no shell +
downloads, bucketlens only) and checks each answer against a value taken from the files.

```bash
pip install -e ".[dev]"
bucketlens setup                      # point it at a test bucket
python benchmarks/fetch_real.py       # downloads the public files and uploads them under real/
bucketlens index --prefix real/
python benchmarks/agent_test.py --questions benchmarks/questions/real.json --parallel 4
```

Each run is capped at `AGENT_TEST_BUDGET` dollars (default 0.40). The full real-file comparison
(30 runs) cost about $1.

- `questions/real.json`: 10 questions on Berkshire Hathaway reports, the World Bank WDI workbook and
  100 CUAD contracts.
- `questions/synthetic.json`: 17 questions on NIST SP 800-53 PDFs and generated spreadsheets and
  contracts (`gen/`).
- `results/`: every run, with answers, tokens, cost and time. `RESULTS.md` is the full log.

Data sources: Berkshire Hathaway reports (berkshirehathaway.com), World Bank World Development
Indicators (CC BY 4.0), CUAD v1 by The Atticus Project (CC BY 4.0). They are downloaded by the
script, not stored here.
