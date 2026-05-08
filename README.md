# Analysis — Daily Indian Equity Market Toolkit

End-to-end Python toolkit that scrapes, computes and emails a unified daily
view of the Indian equity market: FII flows, custom sector indices, sector
momentum / RRG, a multi-universe pull-back screener, NSE+BSE bulk/block
deals, and a deep single-stock forensic + fundamental PDF report.

A single command (`python3 run_all.py`) — or a launchd agent that fires
**Mon–Fri 18:00 IST** — runs the full pipeline, writes one Excel workbook +
five interactive HTML charts, and emails everything as attachments.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [High-Level Architecture](#high-level-architecture)
3. [Repository Layout](#repository-layout)
4. [The Seven Daily Scenarios](#the-seven-daily-scenarios)
5. [Forensic Report (Standalone)](#forensic-report-standalone)
6. [Data Sources](#data-sources)
7. [Inter-Module Dependencies](#inter-module-dependencies)
8. [Output Files](#output-files)
9. [Configuration & Secrets](#configuration--secrets)
10. [Scheduling — launchd (macOS)](#scheduling--launchd-macos)
11. [Scheduling — GitHub Actions (cloud)](#scheduling--github-actions-cloud)
12. [Logging & Troubleshooting](#logging--troubleshooting)
13. [Adding a New Scenario](#adding-a-new-scenario)

---

## Quick Start

```bash
# 1. Clone & enter
cd /Users/ankit.srivastava/Documents/Analysis

# 2. Create venv + install deps (one-time)
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Configure secrets
cp .env.example .env   # if present, else create manually
# Edit .env — set ANGEL_* and EMAIL_* keys (see "Configuration" below)

# 4. Run the full daily pipeline
python3 run_all.py                 # all 7 scenarios + email
python3 run_all.py --no-email      # dry run (no email)
python3 run_all.py --skip bulk_block multi_pct_down   # skip slow ones

# 5. Run a single-stock forensic PDF on demand
python3 forensic_accounting.py     # prompts for ticker
```

Outputs land in the **project root** (overwritten each run):
[market_analysis_report.xlsx](market_analysis_report.xlsx) and five
`*_chart.html` files. Per-run logs go to [logs/](logs/).

---

## High-Level Architecture

```
                 ┌─────────────────────────────────────────┐
                 │    Scheduler  (launchd / GH Actions)    │
                 └────────────────┬────────────────────────┘
                                  │ 18:00 IST  Mon–Fri
                                  ▼
                       scripts/run_market_analysis.sh
                       (cd, venv, .env, log, prune)
                                  │
                                  ▼
                              run_all.py                 ◄── orchestrator
                  ┌───────────────┼───────────────────┐
                  ▼               ▼                   ▼
        7 scenario modules   data_provider.py    email_sender.py
                  │               │                   │
                  ▼               ▼                   ▼
        ┌────────────────┐  angel_client.py     SMTP (Gmail)
        │ NSE / BSE APIs │  jugaad-data
        │ Angel SmartAPI │  yfinance
        │ yfinance       │
        └────────────────┘
                  │
                  ▼
        market_analysis_report.xlsx  +  5 *_chart.html  +  logs/*.log
                  │
                  ▼
                Email with attachments
```

**Three independent subsystems** share the same data layer (`data_provider.py`
+ `angel_client.py`) and email layer (`email_sender.py`):

| Subsystem | Entry point | Cadence |
|---|---|---|
| **Daily market sweep** (7 scenarios) | `run_all.py` | Mon–Fri 18:00 IST (launchd) |
| **Single-stock deep PDF** | `forensic_accounting.py` | On demand |
| **Bulk/Block live scan** | `BulkBlock.py` (also embedded as scenario in run_all) | On demand or via run_all |

---

## Repository Layout

```
Analysis/
├── run_all.py                    # Master orchestrator (7 scenarios)
├── scripts/
│   └── run_market_analysis.sh    # launchd wrapper: cd / venv / .env / log
│
├── BulkBlock.py                  # NSE+BSE bulk/block deals (scenario 1)
├── multi_pct_down.py             # Multi-universe pct-down screener (scenario 2)
├── custom_sector_index.py        # Equal-weighted sector indices (scenario 3)
├── fii_flows.py                  # FII daily equity cash flows (scenario 4)
├── fii_sector_flows.py           # FII fortnightly sector flows (scenario 5)
├── sector_momentum.py            # Mansfield RS per sector (scenario 6)
├── rrg_chart.py                  # Relative Rotation Graph (scenario 7)
│
├── forensic_accounting.py        # Standalone deep single-stock PDF report
│
├── data_provider.py              # Unified OHLCV router (Angel→jugaad→yfinance)
├── angel_client.py               # Angel One SmartAPI session + scrip-master
├── email_sender.py               # SMTP helper (Gmail App Password)
│
├── index_constituents.json       # Static sector → ticker mapping
├── fii_equity_cache.csv          # Cached FII daily flows (incremental)
├── fii_oi_cache.csv              # Cached FII derivatives OI
├── .angel_scrip_master.json      # Cached Angel scrip master (~25 MB, weekly TTL)
│
├── requirements.txt              # Python dependencies
├── rules.md                      # Trading rules / methodology notes
├── TRADING_STRATEGY.md           # Strategy documentation
├── README.md                     # ← this file
│
├── logs/                         # Per-run pipeline logs (auto-pruned 30d)
│   ├── run_all_<timestamp>.log
│   ├── launchd.out.log
│   └── launchd.err.log
├── Output/                       # Manual / archival outputs (PDF reports etc.)
├── .cache/                       # Misc fetch caches
├── venv/                         # Local virtualenv (gitignored)
│
├── .github/workflows/scenarios.yml   # Optional cloud schedule (GH Actions)
└── .env                          # Secrets (gitignored): ANGEL_*, EMAIL_*
```

---

## The Seven Daily Scenarios

Sheets land in [market_analysis_report.xlsx](market_analysis_report.xlsx) in the
order below (~23 sheets total).

| # | Module | Sheets (prefix) | What it does |
|---|---|---|---|
| 1 | [BulkBlock.py](BulkBlock.py) | `BB NSE Bulk`, `BB NSE Block`, `BB BSE Bulk`, `BB BSE Block` | Scrapes today's bulk + block deals from NSE & BSE, filters to a hardcoded "superstar" client list (Kacholia, Kedia, Madhusudan Kela, Goldman, Smallcap World Fund, etc.). |
| 2 | [multi_pct_down.py](multi_pct_down.py) | `MPD NSE 12M`, `MPD NSE_SME 12M`, `MPD BSE_SME 12M` | Multi-universe pull-back screener: 2–21% off 52-wk highs, > 200-DMA, RS > NIFTY 500 over 3M, base-building, mcap band. Also emits a TradingView watchlist. |
| 3 | [custom_sector_index.py](custom_sector_index.py) | `Sector Idx Summary`, `Sector Idx Values` | Builds equal-weighted sector indices from `index_constituents.json` and computes 1D / 1W / 1M / 3M / 6M / 1Y returns. |
| 4 | [fii_flows.py](fii_flows.py) | `FII Flow Summary`, `FII Daily Data` | Daily FII equity cash market net flows with cumulative trend (incremental cache). |
| 5 | [fii_sector_flows.py](fii_sector_flows.py) | `FII Sector Net Flows`, `FII Sector Detail` | NSDL fortnightly FII sector-wise net buy/sell breakdown. |
| 6 | [sector_momentum.py](sector_momentum.py) | `RS Ranking`, `RS History` | Mansfield Relative Strength per sector vs NIFTY 500. |
| 7 | [rrg_chart.py](rrg_chart.py) | `RRG 3 Day` … `RRG Quarterly` (8 timeframes) | Relative Rotation Graph: classifies sectors into Leading / Weakening / Lagging / Improving quadrants across 8 lookback windows. |

Each scenario is wrapped in `try/except` inside `run_all.py` — a single
failure does not abort the pipeline, and failed scenarios are reported in
the email body and final summary.

---

## Forensic Report (Standalone)

[forensic_accounting.py](forensic_accounting.py) is a separate ~8.7K-line
program that builds a **single-stock deep forensic + fundamental PDF**.

- Inputs : a ticker (NSE / BSE / SME).
- Outputs: `forensic_report_<TICKER>_<timestamp>.pdf` (40+ sections).
- Sections include: Springate Z-score, Ohlson O-score, Montier C-score,
  DuPont, working-capital trend, SGR, Benford's Law, ESM checks,
  promoter analysis, deep DCF, valuation bands, capital allocation,
  earnings quality, debt stress, moat signals, quarterly momentum,
  concall/annual-report NLP, peer comparison, technical, Graham,
  capex, institutional flow, corporate actions, credit intelligence,
  shareholding, insider trading, relative strength, plus a **clickable
  Table of Contents** (rendered via `fpdf2.insert_toc_placeholder` after
  the cover page) and full PDF outline bookmarks.

Run on demand:

```bash
python3 forensic_accounting.py     # interactive ticker prompt
```

---

## Data Sources

All data flows through public/free sources. No paid market-data feeds.

| Source | Used by | Auth |
|---|---|---|
| **Angel One SmartAPI** (smartapi-python) | `data_provider.py` (primary OHLCV) | `.env`: `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET` |
| **jugaad-data** (NSE scrape) | `data_provider.py` (fallback for NSE main board) | None |
| **yfinance** | `data_provider.py` (final fallback), market-cap, indices | None |
| **NSE archives CSV** | `multi_pct_down.py` (universe seed, F&O list) | None |
| **NSE API** `/api/snapshot-capital-market-largedeal` | `BulkBlock.py` | Cookie-managed session |
| **BSE JSON API** `api.bseindia.com/.../BulkDeal_Beta` & `BlockDeal_Beta` | `BulkBlock.py` (primary) | None |
| **BSE HTML scrape** `bseindia.com/markets/equity/EQReports/` | `BulkBlock.py` (fallback) | None |
| **NSDL FPI fortnightly** | `fii_sector_flows.py` | None |
| **NSE FII/DII daily** | `fii_flows.py` | None |
| **screener.in** | `forensic_accounting.py` (fundamentals) | None |
| **moneycontrol / trendlyne** | `forensic_accounting.py` (peer, shareholding) | None |

**Caching** keeps the pipeline fast and resilient:
- [fii_equity_cache.csv](fii_equity_cache.csv) — incremental daily FII flows.
- [fii_oi_cache.csv](fii_oi_cache.csv) — FII derivatives OI cache.
- `.angel_scrip_master.json` — Angel scrip master (~25 MB, weekly TTL).
- `.cache/` — misc per-fetcher caches.

---

## Inter-Module Dependencies

```
run_all.py
  ├── BulkBlock.py             ── requests, bs4, (optional) nsepython
  ├── multi_pct_down.py        ── data_provider.py ─┬─ angel_client.py
  │                                                 ├─ jugaad-data
  │                                                 └─ yfinance
  ├── custom_sector_index.py   ── data_provider.py + index_constituents.json
  ├── fii_flows.py             ── requests + fii_equity_cache.csv
  ├── fii_sector_flows.py      ── requests
  ├── sector_momentum.py       ── data_provider.py + index_constituents.json
  ├── rrg_chart.py             ── data_provider.py + index_constituents.json
  └── email_sender.py          ── smtplib (uses EMAIL_* env vars)

forensic_accounting.py         ── data_provider.py + screener.in scrapes
                                  + fpdf2 (clickable TOC + bookmarks)
```

`data_provider.py` is the **single source of truth for OHLCV**. Every module
that needs price history calls `data_provider.download(ticker, start, end)`,
which internally tries Angel → jugaad → yfinance and returns a normalized
pandas DataFrame.

`angel_client.py` owns the SmartAPI session: single-threaded login (Angel
rate-limits parallel TOTP), cached scrip-master lookup, daily candle fetch
via `getCandleData()`. `run_all.py` pre-warms the session before any worker
threads spawn.

---

## Output Files

| File | Lifecycle |
|---|---|
| [market_analysis_report.xlsx](market_analysis_report.xlsx) | Overwritten each run (~23 sheets) |
| [custom_sector_index_chart.html](custom_sector_index_chart.html) | Overwritten |
| [fii_flows_chart.html](fii_flows_chart.html) | Overwritten |
| [fii_sector_flows_chart.html](fii_sector_flows_chart.html) | Overwritten |
| [sector_momentum_chart.html](sector_momentum_chart.html) | Overwritten |
| [rrg_chart_chart.html](rrg_chart_chart.html) | Overwritten |
| `logs/run_all_<timestamp>.log` | New per run, auto-pruned > 30 days |
| `logs/launchd.{out,err}.log` | Append-only (launchd-level errors only) |
| `forensic_report_<TICKER>_<timestamp>.pdf` | New per forensic run (manual) |
| [Output/](Output/) | Manual archive of historical PDFs and snapshots |

The **email** sent by `run_all.py` attaches the Excel + 5 charts.
`BulkBlock.py` standalone-mode emits its own `BULK_BLOCK_Deals_*.xlsx`
+ HTML email; inside `run_all.py` this is suppressed (data is captured
into the unified workbook instead).

---

## Configuration & Secrets

All secrets live in `.env` at the project root (gitignored). The wrapper
script auto-exports every line so child processes inherit them.

```ini
# Angel One (required for Angel-routed OHLCV in multi_pct_down + others)
ANGEL_API_KEY=...
ANGEL_CLIENT_CODE=...
ANGEL_PIN=...
ANGEL_TOTP_SECRET=...

# Email (Gmail App Password — NOT your account password)
EMAIL_FROM=you@gmail.com
EMAIL_TO=recipient1@example.com,recipient2@example.com
EMAIL_USERNAME=you@gmail.com
EMAIL_PASSWORD=<16-char-app-password>
EMAIL_SMTP_SERVER=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_USE_TLS=true
EMAIL_SUBJECT_PREFIX=Daily Market Analysis Report
```

Generate a Gmail App Password at <https://myaccount.google.com/apppasswords>
(requires 2-Step Verification). If `EMAIL_*` is not set, `run_all.py`
still completes and writes all files — it just skips the email step.

---

## Scheduling — launchd (macOS)

A persistent launchd agent runs the pipeline **Mon–Fri at 18:00 IST**
(post-EOD, after bhavcopy/FII data settles).

**Files:**
- [scripts/run_market_analysis.sh](scripts/run_market_analysis.sh) — wrapper
  (cd, weekend-guard, load `.env`, activate venv, run, log, prune).
- `~/Library/LaunchAgents/com.analysis.runall.plist` — agent (5 calendar
  entries, one per weekday).

**Install (one-time):**

```bash
chmod +x scripts/run_market_analysis.sh

# Plist already lives in ~/Library/LaunchAgents
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.analysis.runall.plist
launchctl enable    gui/$(id -u)/com.analysis.runall
```

**One-time TCC permission:** macOS blocks `/bin/bash` from accessing
`~/Documents` unless granted. Go to **System Settings → Privacy & Security
→ Full Disk Access → +**, type `/bin/bash` (⌘⇧G), enable the toggle.

**Daily ops:**

```bash
# Manual trigger / sanity check
launchctl kickstart -k gui/$(id -u)/com.analysis.runall

# Inspect status / next-fire
launchctl print gui/$(id -u)/com.analysis.runall | head -40

# Tail today's log
tail -f logs/run_all_*.log

# Disable / uninstall
launchctl bootout gui/$(id -u)/com.analysis.runall
```

If the laptop is asleep at 18:00, launchd fires at the next wake (Apple-
documented). The wrapper re-checks day-of-week so an accidental manual
run on Sat/Sun no-ops cleanly.

---

## Scheduling — GitHub Actions (cloud)

[.github/workflows/scenarios.yml](.github/workflows/scenarios.yml) provides
an alternate cloud schedule (cron `0 13 * * 1-5` = ~16:37 IST). It checks
out the repo, installs deps, runs `run_all.py` with secrets injected from
GitHub Actions secrets (`ANGEL_*`), uploads the Excel + charts as
artifacts, and dispatches a follow-on `send-email` job.

Use this if your laptop is unreliable. Use the launchd path for fastest
turnaround and to avoid storing market-data state in cloud artifacts.

---

## Logging & Troubleshooting

| Symptom | Where to look |
|---|---|
| Pipeline output / scenario errors | `logs/run_all_<timestamp>.log` |
| launchd refused to start the script | `logs/launchd.err.log` |
| "Operation not permitted" in launchd.err.log | TCC — grant Full Disk Access to `/bin/bash` |
| Email skipped | Check `EMAIL_PASSWORD` etc. in `.env` |
| Angel "Access denied because of exceeding access rate" | Angel rate-limits parallel TOTP; ensure single-threaded login (already handled in `multi_pct_down.py` pre-warm) |
| `multi_pct_down` slow | It dominates runtime (~50 min); use `--skip multi_pct_down` for fast iterations |
| BSE deals empty | BSE JSON API sometimes 0-rows pre-EOD; HTML fallback kicks in automatically |

Pipeline logs are rotated automatically — the wrapper deletes
`run_all_*.log` files older than 30 days.

---

## Adding a New Scenario

1. **Create a module** with a top-level `run(output_prefix=...)` function
   returning a tuple whose first element is a `dict[str, DataFrame]` of
   sheet-name → data, and whose later elements include any chart paths.
2. **Add a wrapper** in `run_all.py`:
   ```python
   def run_my_scenario():
       from my_scenario import run as ms_run
       result = ms_run(output_prefix=os.path.join(SCRIPT_DIR, "my_scenario"))
       sheets, chart_path = ..., ...
       return sheets, chart_path
   ```
3. **Append the name** to `ALL_SCENARIOS` (controls sheet ordering and
   `--skip` choices).
4. **Insert a banner block** in `main()` matching the existing pattern
   (try/except, errors list).
5. **Run `python3 run_all.py --no-email`** to validate; `python3 -m
   py_compile run_all.py` to check syntax.

---

## License & Disclaimer

For personal research use only. All scraped data is sourced from public
exchange/regulator endpoints. No financial advice. Use at your own risk.
