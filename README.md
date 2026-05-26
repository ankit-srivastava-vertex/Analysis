# Analysis — Daily Indian Equity Market Toolkit

End-to-end Python toolkit that scrapes, computes and emails a unified daily
view of the Indian equity market: bulk/block deals, FII flows & stake tracking,
HNI holdings, sector momentum / RRG, IPO anchor investors, and more.

A single command (`python3 run_all.py`) — or a launchd agent that fires
**Mon–Fri 18:00 IST** — runs the full pipeline, writes one Excel workbook +
one combined interactive HTML chart (`market_charts.html` with tabbed UI),
and emails both as attachments.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [High-Level Architecture](#high-level-architecture)
3. [Repository Layout](#repository-layout)
4. [run_all.py — The Daily Pipeline](#run_allpy--the-daily-pipeline)
5. [Standalone Scripts](#standalone-scripts)
6. [Portfolio System](#portfolio-system)
7. [Data Sources](#data-sources)
8. [Inter-Module Dependencies](#inter-module-dependencies)
9. [Output Files](#output-files)
10. [Configuration & Secrets](#configuration--secrets)
11. [Scheduling — launchd (macOS)](#scheduling--launchd-macos)
12. [Scheduling — GitHub Actions (cloud)](#scheduling--github-actions-cloud)
13. [Logging & Troubleshooting](#logging--troubleshooting)

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
python3 run_all.py --skip rrg      # skip specific scenarios

# 5. Run standalone scripts
python3 BulkBlock.py               # bulk/block + FII + HNI (standalone)
python3 breakout_scanner_angel.py  # breakout scan (includes multi_pct_down)
python3 fno_max_oi.py              # F&O max OI scanner
python3 india_macro.py --fetch-direct  # India macro dashboard
python3 forensic_accounting.py     # single-stock forensic PDF (prompts for ticker)
```

Outputs land in the **project root** (overwritten each run):
`BULK_BLOCK_Deals_<timestamp>.xlsx` and `market_charts.html`.
Per-run logs go to [logs/](logs/).

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
        │ NSDL FPI       │
        └────────────────┘
                  │
                  ▼
        BULK_BLOCK_Deals_<ts>.xlsx  +  market_charts.html  +  logs/*.log
                  │
                  ▼
                Email with attachments
```

**Four independent subsystems** share the same data layer (`data_provider.py`
+ `angel_client.py`) and email layer (`email_sender.py`):

| Subsystem | Entry point | Cadence |
|---|---|---|
| **Daily market sweep** (7 scenarios) | `run_all.py` | Mon–Fri 18:00 IST (launchd) |
| **Breakout scanner** | `breakout_scanner_angel.py` | On demand |
| **Single-stock deep PDF** | `forensic_accounting.py` | On demand |
| **Portfolio analysis** (9 scenarios) | `portfolio/portfolio_run_all.py` | On demand |

---

## Repository Layout

```
Analysis/
├── run_all.py                    # Master orchestrator (7 scenarios)
├── scripts/
│   └── run_market_analysis.sh    # launchd wrapper: cd / venv / .env / log
│
├── BulkBlock.py                  # NSE+BSE bulk/block + FII stake + HNI (scenario 1)
├── fii_stake_tracker.py          # FII quarterly stake streaks (runs via BulkBlock)
├── custom_sector_index.py        # Equal-weighted sector indices (scenario 2)
├── fii_flows.py                  # FII daily equity cash flows (scenario 3)
├── fii_sector_flows.py           # FII fortnightly sector flows (scenario 4)
├── sector_momentum.py            # Mansfield RS per sector (scenario 5)
├── rrg_chart.py                  # Relative Rotation Graph (scenario 6)
├── ipo_anchor_tracker.py         # IPO anchor investor tracking (scenario 7)
│
├── breakout_scanner_angel.py     # Pre-breakout scanner (standalone, includes multi_pct_down)
├── multi_pct_down.py             # Pct-down screener (runs via breakout_scanner_angel)
├── fno_max_oi.py                 # F&O Max OI strike scanner (standalone)
├── india_macro.py                # India macro dashboard (standalone)
├── forensic_accounting.py        # Single-stock forensic PDF report (standalone)
├── breakout_review.py            # Walk-forward validation of breakout picks (standalone)
│
├── data_provider.py              # Unified OHLCV router (Angel→jugaad→yfinance)
├── angel_client.py               # Angel One SmartAPI session + scrip-master
├── email_sender.py               # SMTP helper (Gmail App Password)
│
├── portfolio/                    # Portfolio analysis subsystem
│   ├── portfolio_run_all.py      # Portfolio orchestrator (9 scenarios)
│   ├── portfolio_tracker.py      # P&L, sector exposure, concentration
│   ├── position_health.py        # DMA/RSI/drawdown technical scan
│   ├── sl_target_tracker.py      # SL/Target hit alerts
│   ├── risk_metrics.py           # Beta, VaR, Sharpe, MDD
│   ├── correlation_clusters.py   # Return-correlation pairs & clusters
│   ├── pledge_promoter.py        # Pledge % + promoter holding flags
│   ├── mf_overlap.py             # MF crowding overlap
│   ├── events_calendar.py        # Corp events for owned names
│   ├── premarket_dashboard.py    # Global cues, FX, breadth
│   ├── holdings_loader.py        # Parse broker holdings xlsx
│   └── _prices.py                # Shared price-fetch helper
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
├── data/                         # Data storage (india_macro CSVs, etc.)
├── logs/                         # Per-run pipeline logs (auto-pruned 30d)
├── Output/                       # Breakout scanner outputs, review archives
├── .cache/                       # Misc fetch caches
├── venv/                         # Local virtualenv (gitignored)
│
├── .github/workflows/scenarios.yml   # Optional cloud schedule (GH Actions)
└── .env                          # Secrets (gitignored): ANGEL_*, EMAIL_*
```

---

## run_all.py — The Daily Pipeline

Runs 7 scenarios in sequence. Output is **one Excel + one combined chart**.

| # | Module | What it produces | Goes where |
|---|---|---|---|
| 1 | [BulkBlock.py](BulkBlock.py) | Bulk/block deals + FII Stake Tracker sheets + HNI holdings | `BULK_BLOCK_Deals_<ts>.xlsx` (the single output Excel) |
| 2 | [custom_sector_index.py](custom_sector_index.py) | Equal-weighted sector indices | Chart only |
| 3 | [fii_flows.py](fii_flows.py) | Daily FII equity cash flows + cumulative trend | Chart only |
| 4 | [fii_sector_flows.py](fii_sector_flows.py) | Fortnightly FII sector-wise flows | Chart only |
| 5 | [sector_momentum.py](sector_momentum.py) | Mansfield RS per sector | **"RS Ranking" sheet → appended to BulkBlock Excel** + chart |
| 6 | [rrg_chart.py](rrg_chart.py) | Relative Rotation Graph (8 timeframes) | Chart only |
| 7 | [ipo_anchor_tracker.py](ipo_anchor_tracker.py) | Last 14 months of IPOs with anchor investor matches | **"IPO Anchor List" sheet → appended to BulkBlock Excel** |

**BulkBlock Excel sheets** (final output):
- `nse_bulk`, `nse_block` — NSE deals filtered to superstar clients
- `Bulk Deals`, `Block Deals` — BSE deals filtered to superstar clients
- `FII_Summary` — Classification rules + per-sheet row counts
- `FII_New_Entry`, `FII_1Q_Increasing`, `FII_2Q_Increasing`, `FII_3Q_Increasing`, `FII_4Q_Increasing` — FII stake streak data
- `HNIs` — Superstar/HNI new buys + increased positions (sorted A-Z)
- `RS Ranking` — Sector relative strength ranking
- `IPO Anchor List` — Recent IPOs with watchlist anchor matches

**Combined Chart** (`market_charts.html` — tabbed HTML with iframe panels):
- Sector Index
- FII Flows
- FII Sector Flows
- Sector Momentum
- RRG Chart

Each scenario is wrapped in `try/except` — a single failure does not abort
the pipeline. Failed scenarios are reported in the email body and summary.

```bash
python3 run_all.py                         # run all + send email
python3 run_all.py --no-email              # run all, skip email
python3 run_all.py --skip bulk_block rrg   # skip arbitrary scenarios
```

Available `--skip` names: `bulk_block`, `sector_index`, `fii_flows`,
`fii_sector_flows`, `sector_momentum`, `rrg`, `ipo_anchor`

---

## Standalone Scripts

These scripts run independently and are **not** part of `run_all.py`:

| Script | Command | Output |
|---|---|---|
| [BulkBlock.py](BulkBlock.py) | `python3 BulkBlock.py` | `BULK_BLOCK_Deals_<ts>.xlsx` |
| [breakout_scanner_angel.py](breakout_scanner_angel.py) | `python3 breakout_scanner_angel.py` | `Output/breakout_watchlist.xlsx` + logs |
| [multi_pct_down.py](multi_pct_down.py) | `python3 multi_pct_down.py` | `multi_pct_down_<ts>.xlsx` + `.txt` watchlist |
| [fno_max_oi.py](fno_max_oi.py) | `python3 fno_max_oi.py` | `fno_max_oi.xlsx` |
| [india_macro.py](india_macro.py) | `python3 india_macro.py --fetch-direct` | `india_macro_data.xlsx` + `india_macro_dashboard.html` |
| [forensic_accounting.py](forensic_accounting.py) | `python3 forensic_accounting.py` | `forensic_report_<TICKER>_<ts>.pdf` |
| [breakout_review.py](breakout_review.py) | `python3 breakout_review.py` | `Output/review_<ts>.xlsx` |

**Key relationships:**
- `fii_stake_tracker.py` runs via `BulkBlock.py` (called as `get_sheets()`)
- `multi_pct_down.py` runs via `breakout_scanner_angel.py` (called inline)
- All scenarios in `run_all.py` can also run individually with their own
  standalone Excel + chart output

---

## Portfolio System

A separate orchestrator for portfolio-level analysis:

```bash
python3 portfolio/portfolio_run_all.py              # all 9 + email
python3 portfolio/portfolio_run_all.py --no-email   # dry run
```

| # | Module | Purpose |
|---|---|---|
| 1 | `portfolio_tracker.py` | P&L, sector exposure, concentration |
| 2 | `position_health.py` | DMA/RSI/drawdown technical scan |
| 3 | `sl_target_tracker.py` | SL/Target hit alerts |
| 4 | `risk_metrics.py` | Beta, VaR, Sharpe, MDD |
| 5 | `correlation_clusters.py` | Return-correlation pairs & clusters |
| 6 | `pledge_promoter.py` | Pledge % + promoter holding flags |
| 7 | `mf_overlap.py` | MF crowding overlap |
| 8 | `events_calendar.py` | Corp events for owned names |
| 9 | `premarket_dashboard.py` | Global cues, FX, breadth + chart |

Output: `portfolio/portfolio_report.xlsx` + `premarket_dashboard_chart.html`

---

## Data Sources

All data flows through public/free sources. No paid market-data feeds.

| Source | Used by | Auth |
|---|---|---|
| **Angel One SmartAPI** | `data_provider.py` (primary OHLCV) | `.env`: `ANGEL_*` |
| **jugaad-data** (NSE scrape) | `data_provider.py` (fallback) | None |
| **yfinance** | `data_provider.py` (final fallback), indices | None |
| **NSE archives CSV** | `multi_pct_down.py` (universe seed, F&O list) | None |
| **NSE API** `/api/snapshot-capital-market-largedeal` | `BulkBlock.py` | Cookie-managed session |
| **BSE JSON API** | `BulkBlock.py` (primary BSE) | None |
| **BSE HTML scrape** | `BulkBlock.py` (fallback) | None |
| **NSDL FPI fortnightly** | `fii_sector_flows.py` | None |
| **NSDL FPI monthly** | `fii_flows.py` | None |
| **NSE BhavCopy (F&O)** | `fno_max_oi.py` (EOD mode) | None |
| **screener.in** | `fii_stake_tracker.py`, `breakout_scanner_angel.py`, `forensic_accounting.py` | `.env`: `SCREENER_*` |
| **chittorgarh.com** | `ipo_anchor_tracker.py` (anchor tables) | None |
| **RBI / AMFI / CEA / PPAC** | `india_macro.py` (28 indicators) | None |

---

## Inter-Module Dependencies

```
run_all.py
  ├── BulkBlock.py ─── fii_stake_tracker.py ─── screener.in (HNI)
  │                    requests, bs4
  ├── custom_sector_index.py ── data_provider.py + index_constituents.json
  ├── fii_flows.py ── requests + fii_equity_cache.csv
  ├── fii_sector_flows.py ── requests (NSDL)
  ├── sector_momentum.py ── data_provider.py + index_constituents.json
  ├── rrg_chart.py ── data_provider.py + index_constituents.json
  ├── ipo_anchor_tracker.py ── requests (NSE + chittorgarh)
  └── email_sender.py ── smtplib

breakout_scanner_angel.py ── multi_pct_down.py + data_provider.py + screener.in
fno_max_oi.py ── angel_client.py + NSE BhavCopy
india_macro.py ── requests + pdfplumber + plotly
forensic_accounting.py ── data_provider.py + screener.in + fpdf2
portfolio/portfolio_run_all.py ── holdings_loader.py + data_provider.py + ...
```

`data_provider.py` is the **single source of truth for OHLCV**. Every module
that needs price history calls `data_provider.download(ticker, start, end)`,
which internally tries Angel → jugaad → yfinance.

---

## Output Files

| File | Producer | Lifecycle |
|---|---|---|
| `BULK_BLOCK_Deals_<timestamp>.xlsx` | `run_all.py` / `BulkBlock.py` | New each run |
| `market_charts.html` | `run_all.py` | Overwritten (5 tabs) |
| `Output/breakout_watchlist.xlsx` | `breakout_scanner_angel.py` | Overwritten |
| `fno_max_oi.xlsx` | `fno_max_oi.py` | Overwritten |
| `india_macro_data.xlsx` | `india_macro.py` | Overwritten |
| `india_macro_dashboard.html` | `india_macro.py` | Overwritten |
| `portfolio/portfolio_report.xlsx` | `portfolio_run_all.py` | Overwritten |
| `forensic_report_<TICKER>_<ts>.pdf` | `forensic_accounting.py` | New per run |
| `logs/run_all_<timestamp>.log` | `run_all.py` | Auto-pruned > 30 days |
| `ipo_anchor_report.txt` | `ipo_anchor_tracker.py` | TradingView watchlist |

The **email** sent by `run_all.py` attaches the BulkBlock Excel + `market_charts.html`.

---

## Configuration & Secrets

All secrets live in `.env` at the project root (gitignored). The wrapper
script auto-exports every line so child processes inherit them.

```ini
# Angel One (required for OHLCV via SmartAPI)
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

# Screener.in (for HNI scrape + breakout scanner)
SCREENER_USER=...
SCREENER_PASS=...
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
# Manual trigger
launchctl kickstart -k gui/$(id -u)/com.analysis.runall

# Tail today's log
tail -f logs/run_all_*.log

# Disable / uninstall
launchctl bootout gui/$(id -u)/com.analysis.runall
```

---

## Scheduling — GitHub Actions (cloud)

[.github/workflows/scenarios.yml](.github/workflows/scenarios.yml) provides
an alternate cloud schedule (cron `0 13 * * 1-5` ≈ 18:30 IST). It checks
out the repo, installs deps, runs `run_all.py` with secrets injected from
GitHub Actions secrets, uploads the Excel + charts as artifacts, and
dispatches a follow-on `send-email` job.

---

## Logging & Troubleshooting

| Symptom | Where to look |
|---|---|
| Pipeline output / scenario errors | `logs/run_all_<timestamp>.log` |
| launchd refused to start | `logs/launchd.err.log` |
| "Operation not permitted" | TCC — grant Full Disk Access to `/bin/bash` |
| Email skipped | Check `EMAIL_PASSWORD` in `.env` |
| Angel rate-limit errors | Ensure single-threaded login (handled automatically) |
| BSE deals empty | BSE JSON API sometimes 0-rows pre-EOD; HTML fallback kicks in |

Pipeline logs are rotated automatically — the wrapper deletes
`run_all_*.log` files older than 30 days.

---

## License & Disclaimer

For personal research use only. All scraped data is sourced from public
exchange/regulator endpoints. No financial advice. Use at your own risk.
