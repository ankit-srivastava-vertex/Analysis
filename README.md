# Indian Market Analysis & Portfolio Toolkit

Automated daily market analysis pipeline for Indian equities. Covers bulk/block deals, FII flows, sector momentum, breakout scanning, forensic accounting, macro indicators, and full portfolio management — all orchestrated with a single command and delivered via email.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Architecture](#architecture)
3. [Environment & Configuration](#environment--configuration)
   - [Replicating .venv / .vscode / .env](#replicating-venv--vscode--env)
4. [What Runs via run_all.py — and What Doesn't](#what-runs-via-run_allpy--and-what-doesnt)
5. [run_all.py — Main Orchestrator](#run_allpy--main-orchestrator)
6. [Standalone Analysis Scripts](#standalone-analysis-scripts)
7. [Portfolio System](#portfolio-system)
8. [Interactive Web Apps](#interactive-web-apps)
9. [Data Layer](#data-layer)
10. [Scheduling & Automation](#scheduling--automation)
11. [Output Files — Which Script Produces What](#output-files--which-script-produces-what)
12. [Inter-Module Dependencies](#inter-module-dependencies)
13. [Logging](#logging)
14. [Complete Command Reference](#complete-command-reference)

---

## Quick Start

```bash
cd /Users/ankit.srivastava/Documents/Analysis

# Create the venv (Python 3.11), upgrade the installer, and install EVERY
# requirements*.txt in the project (root + tradingcharts/ + any future ones)
# in a single pip resolver pass — avoids version conflicts from sequential
# installs:
python3.11 -m venv .venv && .venv/bin/python -m pip install -U pip setuptools wheel && \
  .venv/bin/python -m pip install $(find . -path ./.venv -prune -o -path ./venv -prune -o -name 'requirements*.txt' -print | sed 's/^/-r /')

source .venv/bin/activate

# Additional deps not in requirements.txt (install manually if missing):
pip install smartapi-python pyotp fpdf2 PyPDF2 httpx numpy

# Configure credentials — no .env.example is shipped; create .env yourself
# (see "Replicating .venv / .vscode / .env" below for a ready-to-paste template).

# Run everything (market closed, so no email):
python3 run_all.py --no-email

# Portfolio analysis:
python3 portfolio/portfolio_run_all.py --no-email
```

> **Virtualenv note:** the project's canonical environment is **`.venv`
> (Python 3.11)**. A legacy `venv/` (Python 3.9) may also be present as a
> fallback; prefer `.venv`. Both are gitignored.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATORS                                    │
│  run_all.py (8 market scenarios)   portfolio/portfolio_run_all.py (9)   │
└────────────┬───────────────────────────────────────┬────────────────────┘
             │                                       │
     ┌───────▼───────┐                     ┌────────▼────────┐
     │ Market Scripts │                     │ Portfolio Mods  │
     │  BulkBlock     │                     │ portfolio_tracker│
     │  custom_sector │                     │ position_health │
     │  fii_flows     │                     │ sl_target_tracker│
     │  fii_sector    │                     │ risk_metrics    │
     │  sector_mom    │                     │ corr_clusters   │
     │  nse_ready_sec │                     │ pledge_promoter │
     │  rrg_chart     │                     │ mf_overlap      │
     │  ipo_anchor    │                     │ events_calendar │
     └───────┬───────┘                     │ events_calendar │
             │                              │ premarket_dash  │
             │                              └────────┬────────┘
             │                                       │
     ┌───────▼───────────────────────────────────────▼──────┐
     │                   DATA LAYER                          │
     │  data_provider.py (Angel One → jugaad-data → yfinance)│
     │  angel_client.py  (SmartAPI session + scrip master)   │
     └──────────────────────────────────────────────────────┘
             │
     ┌───────▼───────┐
     │  email_sender  │  (SMTP delivery of reports)
     └───────────────┘
```

**Four independent subsystems** share the same data layer and email layer:

| Subsystem | Entry point | Cadence |
|---|---|---|
| **Daily market sweep** (8 scenarios) | `run_all.py` | Mon–Fri 18:00 IST (launchd) |
| **Breakout scanner** (+ attached scorecard) | `breakout_scanner_angel.py` | On demand |
| **Single-stock deep PDF** | `forensic_accounting.py` | On demand |
| **Portfolio analysis** (9 scenarios) | `portfolio/portfolio_run_all.py` | On demand |

---

## Repository Layout

```
Analysis/
├── run_all.py                    # Master orchestrator (8 scenarios)
├── scripts/
│   └── run_market_analysis.sh    # launchd wrapper: cd / venv / .env / log
│
├── BulkBlock.py                  # NSE+BSE bulk/block deals (scenario 1)
├── fii_stake_tracker.py          # FII quarterly stake streaks (standalone, run quarterly)
├── custom_sector_index.py        # Equal-weighted sector indices (scenario 2)
├── fii_flows.py                  # FII daily equity cash flows (scenario 3)
├── fii_sector_flows.py           # FII fortnightly sector flows (scenario 4)
├── sector_momentum.py            # Mansfield RS per sector (scenario 5)
├── nse_ready_sectors.py          # Mansfield RS on official NSE sector indices, self-contained provider (scenario 6)
├── rrg_chart.py                  # Relative Rotation Graph (scenario 7)
├── ipo_anchor_tracker.py         # IPO anchor investor tracking (scenario 8)
│
├── breakout_scanner_angel.py     # Pre-breakout scanner (standalone, includes multi_pct_down)
├── breakout_scanner_scorecard.py # Scorecard (Valuation×Momentum×Stage) — attached post-process to the scanner
├── multi_pct_down.py             # Pct-down screener (runs via breakout_scanner_angel)
├── fno_max_oi.py                 # F&O Max OI strike scanner (standalone)
├── india_macro.py                # India macro dashboard (standalone)
├── forensic_accounting.py        # Single-stock forensic PDF report (standalone)
├── ipo_listing_gainers.py        # IPO >=50% gainers + FULL anchor-investor lists (standalone)
├── breakout_review.py            # Walk-forward validation of breakout picks (standalone)
├── breakout_deep_analysis.py     # Rule-mining on review data → elite-subset filters (standalone)
├── breakout_scorecard_review.py  # Walk-forward validator for scorecard CompositeScore (standalone)
├── universe_review.py            # Does the scanner add value over the raw universe? (standalone)
├── universe_mining.py            # Mines the raw universe for an elite tradeable subset (standalone)
│
├── data_provider.py              # Unified OHLCV router (Angel→jugaad→yfinance)
├── angel_client.py               # Angel One SmartAPI session + scrip-master
├── ohlcv_cache.py                # 2-tier incremental daily-bar cache in front of Angel
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
│   ├── _prices.py                # Shared price-fetch helper
│   ├── holdings_meta.csv         # User SL/Target levels per position
│   └── mf_holdings.csv           # MF holdings context
│
├── tradingcharts/                # Browser charting dashboard (Flask, port 5050)
│   ├── app.py                    # REST API + live ticks (Angel WS) + state
│   ├── run.py / run.sh / run.bat # Cross-platform launchers
│   ├── requirements.txt          # flask, flask-cors, waitress, pandas
│   ├── README.md / RULES.md      # App docs + charting rules
│   ├── static/                   # Single-page frontend (index.html, drawing-tools.js)
│   ├── state/state.json          # Server-side persisted UI state
│   ├── logs/                     # Per-day app logs
│   └── multiscreen/              # Sidecar: 4 isolated workspaces (port 5051)
│       ├── server.py             # Reverse-proxy + per-workspace state
│       ├── run.py, shim.js       # Launcher + localStorage namespacing
│       └── state/<wsid>.json     # default / ws2 / ws3 / ws4
├── screener/                     # Screener.in-backed valuation/financials app (Flask, port 5052)
│   ├── app.py                    # Scrapes Screener.in → financial time-series API
│   ├── run.py                    # Launcher
│   ├── state/state.json          # Persisted UI state
│   └── static/                   # Frontend (reuses tradingcharts assets)
│
├── index_constituents.json       # Static sector → ticker mapping
├── fii_equity_cache.csv          # Cached FII daily flows (incremental)
├── fii_oi_cache.csv              # Cached FII derivatives OI (incremental)
├── .angel_scrip_master.json      # Cached Angel scrip master (~25 MB, weekly TTL)
│
├── requirements.txt              # Python dependencies
├── rules.md                      # Trading rules / methodology notes
├── TRADING_STRATEGY.md           # Strategy documentation
├── README.md                     # ← this file
│
├── data/                         # Data storage
│   ├── india_macro/              # 28 indicator CSVs (append-only)
│   ├── backtest/                 # Cached OHLCV parquets + backtest result JSONs
│   ├── ipo/                      # Append-only IPO feed ledgers (NSE + BSE)
│   └── bulkblock/                # Cached bulk/block deal data
├── logs/                         # Per-run pipeline logs (auto-pruned 30d)
├── Output/                       # Breakout scanner outputs, review archives
│   └── WeekN/                    # Weekly breakout snapshots
├── .cache/                       # Misc fetch caches (NSE API, Screener.in)
├── .vscode/settings.json         # Editor: python.terminal.useEnvFile + envFile
├── .venv/                        # Canonical virtualenv — Python 3.11 (gitignored)
├── venv/                         # Legacy virtualenv — Python 3.9, optional fallback (gitignored)
│
├── .github/workflows/scenarios.yml   # Optional cloud schedule (GH Actions)
└── .env                          # Secrets (gitignored): ANGEL_*, EMAIL_*
```

---

## Environment & Configuration

### `.env` File (Required)

| Variable | Purpose |
|----------|---------|
| `ANGEL_API_KEY` | Angel One SmartAPI key |
| `ANGEL_CLIENT_CODE` | Angel One client code |
| `ANGEL_PIN` | Angel One MPIN |
| `ANGEL_TOTP_SECRET` | Angel One TOTP secret (for pyotp) |
| `EMAIL_SMTP_SERVER` | SMTP server (default: `smtp.gmail.com`) |
| `EMAIL_SMTP_PORT` | SMTP port (default: `587`) |
| `EMAIL_USE_TLS` | `true` / `false` (default: `true`) |
| `EMAIL_FROM` | Sender email address |
| `EMAIL_SENDER_NAME` | Display name (default: `Market Analysis Bot`) |
| `EMAIL_TO` | Comma-separated recipient addresses |
| `EMAIL_USERNAME` | SMTP login (defaults to `EMAIL_FROM`) |
| `EMAIL_PASSWORD` | SMTP password / app-specific password |
| `EMAIL_SUBJECT_PREFIX` | Email subject prefix |
| `SCREENER_USER` | Screener.in login (for fii_stake_tracker fallback) |
| `SCREENER_PASS` | Screener.in password |
| `DATA_GOV_IN_API_KEY` | data.gov.in OGD API key (optional, for india_macro) |

### `requirements.txt`

```
requests, beautifulsoup4, pandas, openpyxl, nsepython, plotly,
jugaad-data, yfinance, xlrd>=2.0.1, pdfplumber, python-dotenv
```

Additional (install manually): `smartapi-python`, `pyotp`, `fpdf2`, `PyPDF2`, `httpx`, `numpy`

The web apps have their own deps in `tradingcharts/requirements.txt`
(`flask`, `flask-cors`, `waitress`, `pandas`). The Quick Start one-liner installs
every `requirements*.txt` in the project in a single resolver pass.

### `.env` Example

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

# Screener.in (for FII stake tracker fallback + breakout scanner)
SCREENER_USER=...
SCREENER_PASS=...

# data.gov.in (optional — for india_macro OGD fetcher)
DATA_GOV_IN_API_KEY=...
```

Generate a Gmail App Password at <https://myaccount.google.com/apppasswords>
(requires 2-Step Verification). If `EMAIL_*` is not set, `run_all.py`
still completes and writes all files — it just skips the email step.

### VS Code — Terminal `.env` Injection

`.vscode/settings.json` enables the Python extension to load `.env` variables
into integrated terminals automatically:

```json
{
  "python.terminal.useEnvFile": true,
  "python.envFile": "${workspaceFolder}/.env"
}
```

With this, running scripts directly in the terminal (e.g. `.venv/bin/python
run_all.py`) sees `ANGEL_*` / `EMAIL_*` / `SCREENER_*` without manual `export`.
Injection applies only to terminals opened **after** the setting is enabled —
reopen existing terminals to pick it up.

---

## Replicating .venv / .vscode / .env

None of these three are committed (all gitignored). Recreate them exactly as
follows on a fresh checkout.

### 1. `.venv/` — Virtual Environment (Python 3.11)

The canonical interpreter is **Python 3.11** (currently 3.11.15). Create the
venv, upgrade the installer, then install every `requirements*.txt` in the repo
in **one** pip resolver pass (root + `tradingcharts/`), plus the manual extras:

```bash
cd /Users/ankit.srivastava/Documents/Analysis

python3.11 -m venv .venv
.venv/bin/python -m pip install -U pip setuptools wheel
.venv/bin/python -m pip install \
  $(find . -path ./.venv -prune -o -path ./venv -prune -o -name 'requirements*.txt' -print | sed 's/^/-r /')

# Extras not pinned in any requirements file:
.venv/bin/python -m pip install smartapi-python pyotp fpdf2 PyPDF2 httpx numpy

source .venv/bin/activate
```

Requirements files installed by the one-liner:
- `requirements.txt` (root — analysis pipeline)
- `tradingcharts/requirements.txt` (`flask`, `flask-cors`, `waitress`, `pandas`)

> If `python3.11` is not on PATH, install it (`brew install python@3.11`) or use
> your 3.11 binary's full path. A legacy `venv/` (Python 3.9) may coexist as a
> fallback, but `.venv` is authoritative.

### 2. `.vscode/settings.json` — Editor `.env` Injection

Create `.vscode/settings.json` with exactly these two keys so the Python
extension auto-loads `.env` into integrated terminals and the interpreter:

```json
{
  "python.terminal.useEnvFile": true,
  "python.envFile": "${workspaceFolder}/.env"
}
```

Then select the interpreter `./.venv/bin/python` via **Python: Select
Interpreter**. Only terminals opened **after** enabling this pick up the vars —
reopen existing ones.

### 3. `.env` — Secrets (no `.env.example` is shipped)

Create `.env` in the project root. The **required** keys (present in the working
setup) are Angel One + Screener.in; `DATA_GOV_IN_API_KEY` is optional (only for
`india_macro` OGD fetches). Paste and fill:

```ini
# ── Angel One SmartAPI (required — primary OHLCV) ──
ANGEL_API_KEY=
ANGEL_CLIENT_CODE=
ANGEL_PIN=
ANGEL_TOTP_SECRET=

# ── Screener.in (required — fii_stake fallback, breakout scanner, screener app) ──
SCREENER_USER=
SCREENER_PASS=

# ── data.gov.in OGD (optional — india_macro only) ──
DATA_GOV_IN_API_KEY=
```

**Email is optional.** The current `.env` contains **no `EMAIL_*` keys**, so the
pipeline runs and writes all files but **skips email delivery**. To enable
email, add the `EMAIL_*` block from the [`.env` Example](#env-example) above
(Gmail requires a 16-char App Password, not your account password).

Quick verification after creating all three:

```bash
source .venv/bin/activate
python -c "import pandas, openpyxl, flask; print('deps OK')"
python -c "import os,dotenv; dotenv.load_dotenv(); print('env keys:', sorted(k for k in os.environ if k.startswith(('ANGEL_','SCREENER_','EMAIL_','DATA_GOV'))))"
```

---

## What Runs via run_all.py — and What Doesn't

The single most common source of confusion in this repo. **Only 8 of the 26 root
scripts execute inside `run_all.py`.** Everything else must be invoked
explicitly — nothing schedules it for you.

### Runs INSIDE `run_all.py` (8 scenarios)

Invoked as imported functions, not subprocesses. Each is wrapped in try/except,
so one failure never aborts the pipeline. Their standalone Excel files are
deleted after capture — only the unified workbook survives.

| # | Scenario name (`--skip`) | Module | Contributes |
|---|---|---|---|
| 1 | `bulk_block` | `BulkBlock.py` | 4 sheets (BB) |
| 2 | `sector_index` | `custom_sector_index.py` | 2 sheets + chart |
| 3 | `fii_flows` | `fii_flows.py` | 2 sheets + chart |
| 4 | `fii_sector_flows` | `fii_sector_flows.py` | 2 sheets + chart |
| 5 | `sector_momentum` | `sector_momentum.py` | 2 sheets + chart |
| 6 | `nse_sector_rs` | `nse_ready_sectors.py` | 1 sheet + chart |
| 7 | `rrg` | `rrg_chart.py` | 8 sheets + chart |
| 8 | `ipo_anchor` | `ipo_anchor_tracker.py` | IPO Anchor sheets + `.txt` |

### Runs INDIRECTLY (called by another script, never scheduled alone)

| Module | Called by | Note |
|---|---|---|
| `fii_stake_tracker.py` | `BulkBlock.py` | Runs inside scenario 1. Standalone use is quarterly/manual. |
| `multi_pct_down.py` | `breakout_scanner_angel.py` | Runs inline as Universe 1. Also runnable alone. |
| `breakout_scanner_scorecard.py` | `breakout_scanner_angel.py` | Attached post-process; reuses the scanner's candles. Has its own CLI for re-scoring an existing workbook. |

### Does NOT run via `run_all.py` — manual invocation only

| Script | Why it is separate | Cadence |
|---|---|---|
| `breakout_scanner_angel.py` | Heavy, long-running; its own pipeline | Weekly |
| `ipo_listing_gainers.py` | Needs you to supply anchor filings | On demand |
| `fno_max_oi.py` | Expiry-cycle specific | Weekly/monthly |
| `india_macro.py` | Monthly data cadence, not daily | Monthly |
| `forensic_accounting.py` | Single-stock, argument-driven | On demand |
| `breakout_review.py` | Needs matured weeks | Review day |
| `breakout_deep_analysis.py` | Consumes review output | Review day |
| `breakout_scorecard_review.py` | Needs matured snapshots | Review day |
| `universe_review.py` | Needs matured weeks | Review day |
| `universe_mining.py` | Consumes universe_review output | Review day |
| `portfolio/portfolio_run_all.py` | Separate 9-scenario orchestrator | On demand |
| `tradingcharts/`, `screener/` | Long-lived web servers | Always-on / on demand |

### Library modules (never run directly)

`data_provider.py`, `angel_client.py`, `ohlcv_cache.py`, `email_sender.py`,
`portfolio/holdings_loader.py`, `portfolio/_prices.py`. These have no `__main__`
entry point worth invoking — importing them is the only intended use.

> **`india_macro` naming trap:** older comments call it "Scenario 8". It is
> **not** in `run_all.py` and never has been — `ipo_anchor` is scenario 8. Run
> `india_macro.py` yourself.

---

## run_all.py — Main Orchestrator

The command-centre script that runs all market analysis scenarios in sequence.

### Usage

```bash
python3 run_all.py                           # run all 8 scenarios + send email
python3 run_all.py --no-email                # run all, skip email
python3 run_all.py --skip bulk_block rrg     # skip specific scenarios
```

### CLI Options

| Flag | Effect |
|------|--------|
| `--no-email` | Run analysis but do not send email |
| `--skip <names>` | Skip listed scenarios (space-separated) |

### Available Scenario Names (for `--skip`)

`bulk_block`, `sector_index`, `fii_flows`, `fii_sector_flows`, `sector_momentum`, `nse_sector_rs`, `rrg`, `ipo_anchor`

### Execution Order

| # | Scenario | Module | What It Does |
|---|----------|--------|--------------|
| 1 | `bulk_block` | `BulkBlock.py` | NSE+BSE bulk/block deals, FII stake tracker, HNI holdings |
| 2 | `sector_index` | `custom_sector_index.py` | Custom equal-weighted sector indices (chart only) |
| 3 | `fii_flows` | `fii_flows.py` | Daily FII equity cash flows (chart only) |
| 4 | `fii_sector_flows` | `fii_sector_flows.py` | Fortnightly FII sector-wise flows (chart only) |
| 5 | `sector_momentum` | `sector_momentum.py` | Mansfield RS on custom baskets (chart + "RS Ranking" sheet) |
| 6 | `nse_sector_rs` | `nse_ready_sectors.py` | Mansfield RS on official NSE sector indices (chart + "NSE Sector RS Ranking" sheet) |
| 7 | `rrg` | `rrg_chart.py` | Relative Rotation Graph (chart only) |
| 8 | `ipo_anchor` | `ipo_anchor_tracker.py` | IPO anchor investor matching ("IPO Anchor List" sheet) |

### Output

- **`market_analysis_report.xlsx`** — the unified workbook (~19 sheets), written
  to the project root. Sheets:
  - `NSE Bulk`, `NSE Block`, `BSE Bulk`, `BSE Block` — deals filtered to superstar clients
  - `Sector Idx Summary`, `Sector Idx Values` — from custom_sector_index
  - `FII Flow Summary`, `FII Daily Data` — from fii_flows
  - `FII Sector Net Flows`, `FII Sector Detail` — from fii_sector_flows
  - `RS Ranking`, `RS History` — from sector_momentum
  - `NSE Sector RS Ranking` — from nse_ready_sectors
  - `RRG 3 Day` … `RRG Quarterly` — 8 timeframe sheets from rrg_chart
  - `IPO Anchor ...` — recent IPOs with watchlist anchor matches
- 6 interactive Plotly HTML charts: `custom_sector_index_chart.html`,
  `fii_flows_chart.html`, `fii_sector_flows_chart.html`,
  `sector_momentum_chart.html`, `nse_sector_rs_chart.html`, `rrg_chart.html`
- `market_charts.html` — combined tabbed HTML embedding all 6 charts in iframes
- `ipo_anchor_report.txt` — TradingView watchlist from the ipo_anchor scenario
- Email with the workbook + charts attached (unless `--no-email`)

> **`BULK_BLOCK_Deals_<timestamp>.xlsx` is NOT produced by `run_all.py`.** That
> file comes from running `BulkBlock.py` **standalone**. Inside the pipeline a
> `_CapturingScraper` subclass suppresses it and folds the data into
> `market_analysis_report.xlsx` instead. Every sub-module's standalone Excel is
> deleted after capture, so the unified workbook is the only one left on disk.

### Notes

- `multi_pct_down` and `breakout_scanner_angel` are **not** part of run_all.py — run independently.
- `india_macro.py` runs independently (Scenario 8 in concept but separate invocation).
- `forensic_accounting.py` is always standalone.
- Each scenario is wrapped in try/except — a single failure does not abort the pipeline.

---

## Standalone Analysis Scripts

### BulkBlock.py — Bulk & Block Deal Scraper

**Purpose:** Scrapes NSE and BSE for the day's bulk and block deals, integrates FII stake tracker data, identifies superstar/HNI investors, and produces a consolidated Excel report.

**Workflow:**
1. Scrape NSE bulk deals (nseindia.com API) + BSE bulk deals (bseindia.com API)
2. Scrape NSE block deals + BSE block deals
3. Run `fii_stake_tracker.py` to get FII new entries + increasing stakes
4. Filter deals by known superstar investor names (HNI tracking)
5. Produce multi-sheet Excel workbook

**Output:** `BULK_BLOCK_Deals_<timestamp>.xlsx` (sheets: NSE Bulk, BSE Bulk, NSE Block, BSE Block, FII_Summary, FII_New_Entry, FII_1-4Q_Increasing, HNIs)

**CLI Options:**

| Flag | Effect |
|------|--------|
| `--dry-run` | Scrape and report without writing the Excel file |

> Parsed directly from `sys.argv` (not argparse), so it must be spelled exactly
> `--dry-run`.

**Usage:**
```bash
python3 BulkBlock.py              # standalone run
python3 BulkBlock.py --dry-run    # scrape only, no file written
```

---

### breakout_scanner_angel.py — Pre-Breakout Screener

**Purpose:** Dual-universe scanner that identifies stocks approaching fractal pivot resistance with volume compression (VCP/W-pattern/cup-handle), scored by Mansfield Relative Strength vs Nifty 500.

**Universes:**
1. **MPD Universe** — Stocks from `multi_pct_down.py` output (2-21% off 52W highs, above 200-DMA, RS > benchmark)
2. **Screener.in Universe** — Custom Screener.in query URL (configurable)

**Key Features:**
- Fractal pivot resistance detection (5-bar pivots)
- Pattern recognition: VCP (Volatility Contraction Pattern), W-Pattern, Cup-and-Handle
- Mansfield Relative Strength scoring vs Nifty 500 (^CRSLDX)
- Hard gates: Stage 2 trend, not extended from entry, recent R-test, base width, RS rising over 50 days

**CLI Options:**

| Flag | Default | Effect |
|------|---------|--------|
| `--max` | `0` (no cap) | Max symbols per universe |
| `--min-score` | `50` (`WATCHLIST_MIN_SCORE`) | Minimum breakout score to include |
| `--lookback` | `252` (`LOOKBACK_DAYS`) | Trading days of history considered (~1 year) |
| `--high-conviction` | off | Only show high-conviction setups |
| `--skip-mpd` | off | Skip MPD universe |
| `--skip-screener` | off | Skip Screener.in universe |
| `--screener-url` | built-in | Custom Screener.in query URL |
| `--symbols-csv` | `""` | CSV file with symbols to scan (bypass both universes) |
| `--out-tag` | `""` | Custom suffix for output files |
| `--no-strict` | off | Disable hard gate filtering |

**Output:**
- `breakout_watchlist.xlsx` (6 sheets: MPD Data, Screener Data, MPD Breakouts, Screener Breakouts, Combined, Parameters)
- 4 TradingView watchlist `.txt` files: `tv_breakouts_combined.txt`, `tv_common.txt`, `tv_unique_mpd.txt`, `tv_unique_screener.txt`

**Usage:**
```bash
python3 breakout_scanner_angel.py
python3 breakout_scanner_angel.py --high-conviction --min-score 60
python3 breakout_scanner_angel.py --max 300 --lookback 252
python3 breakout_scanner_angel.py --symbols-csv my_list.csv --no-strict
python3 breakout_scanner_angel.py --skip-screener --out-tag mpd_only
```

---

### breakout_scanner_scorecard.py — Valuation × Momentum × Stage Scorecard

**Purpose:** Post-processing engine **attached to** `breakout_scanner_angel.py`.
After the breakout workbook is written, the scanner calls `scorecard.run(...)`,
passing the breakout rows plus the **already-downloaded** OHLCV candles and the
Nifty 500 benchmark (no candles are re-fetched — the Angel One quota is
preserved). Every broken-out name is scored on three orthogonal axes and reduced
to one label + one `CompositeScore` for at-a-glance triage.

**Three axes + gate:**
- **Valuation** — how cheap the base is
- **Momentum** — how strong the move is
- **Stage** — where in the Weinstein cycle (genuine Stage-2 vs dead-cat bounce)
- **Quality gate** — pledge / forensic landmines (Tickertape screener + a deep
  forensic pass on a small Stage-2-cheap shortlist)

**Output:** appends a `Scorecard` sheet to `breakout_watchlist.xlsx` and writes
`breakout_watchlist_scorecard.html`; persists a dated row per name to
`data/scorecard_snapshots.csv` (feeds `breakout_scorecard_review.py`).

**CLI Options** (for re-scoring an existing workbook without re-running the scan):

| Flag | Default | Effect |
|------|---------|--------|
| `--workbook` | **required** | Path to an existing `breakout_watchlist.xlsx` |
| `--lookback` | `400` | Days of history to pull for scoring |
| `--no-forensic` | off | Skip the deep forensic pass on the Stage-2-cheap shortlist |

**Usage:**
```bash
# Normal path — nothing to do; the scanner invokes it automatically.
# Manual re-score of an existing workbook:
python3 breakout_scanner_scorecard.py --workbook Output/breakout_watchlist.xlsx
python3 breakout_scanner_scorecard.py --workbook Output/breakout_watchlist.xlsx --no-forensic
```

---

### multi_pct_down.py — Multi-Universe % Off Highs Screener

**Purpose:** Three-universe screener (NSE, NSE-SME, BSE-SME) finding stocks 2-21% off their 52-week highs with relative strength > Nifty 500, above 200-DMA, and making higher lows.

**Filters Applied:**
- Distance from 52W high: 2% to 21% (configurable)
- Above 200-DMA
- Relative Strength > Nifty 500 (^CRSLDX) over same period
- Higher lows pattern (last 3+ swing lows ascending)
- Market cap band filtering (configurable)

**CLI Options:**

| Flag | Default | Effect |
|------|---------|--------|
| `--min` | `2.0` | Minimum % off high |
| `--max` | `21.0` | Maximum % off high |
| `--skip` | `[]` | Skip universes (space-separated: `nse`, `nse-sme`, `bse-sme`) |
| `--workers` | `4` | Parallel download threads |
| `--max-symbols` | `0` (all) | Limit symbols per universe |
| `--out` | script dir | Output **directory** |
| `-o`, `--output-prefix` | — | Output filename prefix |

**Output:**
- `multi_pct_down.xlsx` (one sheet per universe + combined)
- `multi_pct_down.txt` (TradingView watchlist)

**Usage:**
```bash
python3 multi_pct_down.py
python3 multi_pct_down.py --min 5 --max 15 --skip bse-sme
python3 multi_pct_down.py --workers 8 --max-symbols 200
```

---

### custom_sector_index.py — Equal-Weighted Sector Indices

**Purpose:** Builds custom equal-weighted sector indices from a JSON constituents file, fetches 1 year of prices, normalises to base 1000, and produces a time-series chart.

**Input:** `index_constituents.json` — Defines sector names and their constituent symbols.

**CLI Options:**

| Flag | Default | Effect |
|------|---------|--------|
| `-c` | `index_constituents.json` | Path to constituents file |
| `-o` | — | Output file prefix |

**Output:** `custom_sector_index_chart.html` (interactive Plotly chart) + `.xlsx` workbook

**Usage:**
```bash
python3 custom_sector_index.py
python3 custom_sector_index.py -c my_sectors.json -o custom
```

---

### fii_flows.py — FII Daily Cash Flows

**Purpose:** Fetches daily FII/FPI equity cash flow data from NSDL and NSE, caches historically, and produces a 3-panel time-series chart (gross buy, gross sell, net).

**Data Sources:** NSDL FPI daily data + NSE FII activity

**CLI Options:**

| Flag | Effect |
|------|--------|
| `--refresh` | Force re-fetch (ignore cache) |
| `-o` | Output file prefix |

**Output:** `fii_flows_chart.html` (3-panel Plotly chart) + `.xlsx`

**Cache:** `fii_equity_cache.csv` (append-only, deduped by date)

---

### fii_sector_flows.py — FII Sector-Wise Fortnightly Flows

**Purpose:** Fetches fortnightly sector-wise FII/FPI allocation data from NSDL and produces a horizontal bar chart showing net flows per sector.

**CLI Options:**

| Flag | Effect |
|------|--------|
| `-o` | Output file prefix |

**Output:** `fii_sector_flows_chart.html` + `.xlsx`

---

### sector_momentum.py — Sector Mansfield RS Rankings

**Purpose:** Computes Mansfield Relative Strength for each custom sector index vs Nifty 50 (NIFTYBEES proxy). Ranks sectors by momentum and produces a multi-line RS time-series chart.

**Benchmark:** Nifty 50 (correct for sector-level comparison)

**CLI Options:**

| Flag | Effect |
|------|--------|
| `-o` | Output file prefix |

**Output:** `sector_momentum_chart.html` + `.xlsx` with RS Ranking sheet (appended to BulkBlock Excel by run_all.py)

---

### rrg_chart.py — Relative Rotation Graph

**Purpose:** Multi-timeframe RRG plotting 36 custom sectors across 8 timeframes (3d, 7d, 2w, 12d, 3w, weekly, monthly, quarterly) against Nifty 50.

**Benchmark:** Nifty 50

**CLI Options:**

| Flag | Effect |
|------|--------|
| `-o` | Output file prefix |

**CLI Options:**

| Flag | Effect |
|------|--------|
| `-o`, `--output` | Output file prefix (default `rrg_chart`) |

**Output:** `rrg_chart.html` (interactive scatter plot with rotation tails) + `rrg_chart.xlsx` (8 timeframe sheets)

> The chart is built as `prefix + ".html"`, so the default is `rrg_chart.html`.
> A stale `rrg_chart_chart.html` in `Output/` is from an older prefix.

---

### ipo_anchor_tracker.py — IPO Anchor Investor Tracker

**Purpose:** Fetches the last 15 months of IPOs from NSE, computes listing returns, and cross-references anchor investor allocations from chittorgarh.com against a ~85 name watchlist of quality anchors.

**CLI Options:**

| Flag | Default | Effect |
|------|---------|--------|
| `--months` | `14` | Months of IPO history to pull |
| `--limit` | `0` (all) | Debug: first N IPOs only |
| `--no-anchors` | off | Skip anchor scraping (listing returns only) |
| `--out` | built-in | Output path |

**Output:**
- `.xlsx` with IPO details + anchor matches
- TradingView watchlist `.txt` for IPOs held by quality anchors
- "IPO Anchor List" sheet appended to BulkBlock Excel by run_all.py

**Usage:**
```bash
python3 ipo_anchor_tracker.py                  # 14 months, with anchors
python3 ipo_anchor_tracker.py --months 24     # wider history
python3 ipo_anchor_tracker.py --no-anchors    # fast, listing returns only
```

> **Not the same as `ipo_listing_gainers.py`.** This one matches IPOs against a
> *watchlist* of ~85 known-quality anchors and runs inside `run_all.py`.
> `ipo_listing_gainers.py` extracts the **complete** anchor list from official
> filings and is standalone. See below.

---

### ipo_listing_gainers.py — IPO ≥50% Gainers + Full Anchor Lists

**Purpose:** Screens every IPO listed since a chosen date (default 2025-01-01)
for a ≥50% gain, then extracts the **complete** anchor-investor list for each
winner from its official "Allocation to Anchor Investors" filing — and ranks the
investors that recur across deals. The recurrence ranking is the real output.

**Two return measures** (either one qualifies, both on CLOSE not intraday high):
1. Listing-day return vs issue price
2. Peak CLOSE within 30 days of listing vs issue price

**CLI Options:**

| Flag | Default | Effect |
|------|---------|--------|
| `--start` | `2025-01-01` | Listing window start |
| `--end` | today | Listing window end |
| `--threshold` | `50.0` | Minimum qualifying gain % |
| `--window-days` | `30` | Peak lookback after listing |
| `--workers` | `5` | Price-fetch threads |
| `--limit` | `0` (all) | Debug: first N IPOs only |
| `--bse-recent` | off | Add BSE live-window supplement |
| `--anchor-dir` | `./anchor_pdfs` | Folder of anchor filings |
| `--freq-words` | `2` | Words used to key investor names |
| `--no-ocr` | off | Skip OCR; text-layer PDFs only |
| `--out` | `./ipo_listing_gainers.csv` | CSV path |
| `--xlsx` | `./ipo_listing_gainers.xlsx` | Excel path |

**Output (project root):**
- `ipo_listing_gainers.csv` — gainers table
- `ipo_listing_gainers.xlsx` — 3 sheets: `Gainers`, `Anchor Investors` (wide,
  one column per symbol), `Investor Frequency` (recurrence ranking)
- `data/ipo/*.json` — append-only NSE/BSE feed ledgers

**Anchor extraction:** digital PDFs via pdfplumber; scanned PDFs/images via
pdftoppm + tesseract across 4 render passes. Each pass is reconciled against the
filing's own stated share total — if none reconciles, the list is **rejected**
rather than returned partial.

**Resilience:** both exchange feeds are folded into append-only ledgers under
`data/ipo/`, and the ledger (not the live payload) drives the screen. If NSE
blocks the API, historical IPOs and their issue prices still work.

**Usage:**
```bash
python3 ipo_listing_gainers.py                     # 2025-01-01..today, >=50%
python3 ipo_listing_gainers.py --threshold 100     # only >=100% movers
python3 ipo_listing_gainers.py --window-days 60    # wider peak lookback
python3 ipo_listing_gainers.py --bse-recent        # add BSE live supplement
python3 ipo_listing_gainers.py --limit 10          # quick smoke test
```

> **Close the workbook first.** If `ipo_listing_gainers.xlsx` is open in Excel
> the write is clobbered; a stale `~$ipo_listing_gainers.xlsx` is the tell.
> Sheet 2 is merged, never overwritten, so hand-added columns survive re-runs.

**Requires for scanned filings:** `brew install tesseract poppler`

**Note:** Not part of `run_all.py` — always run independently.

---

### fii_stake_tracker.py — FII New Entry & Increasing Stakes

**Purpose:** Identifies stocks across all Indian bourses where FII/FPI have newly entered or increased stake quarter-on-quarter.

**Data Sources:**
- **Primary:** Tickertape Screener API (covers ~3,400 stocks, all segments)
- **Fallback:** Screener.in saved screen (requires login credentials)

**Classification Categories:**
- `New Entry` — FII stake was ~0 last quarter
- `Multi-Quarter Increasing` — FII increasing for 2+ consecutive quarters
- `Increased Stake` — Single-quarter increase

**CLI Options:**

| Flag | Effect |
|------|--------|
| `-o` | Output prefix |

**Output:** Multi-sheet Excel (Summary, FII Stake Increase, New_Entry, Multi-Quarter_Increasing, Increased_Stake)

**Note:** Integrated into `BulkBlock.py` — not typically run standalone.

---

### fno_max_oi.py — F&O Max Open Interest Scanner

**Purpose:** Scans all F&O contracts to find the strike prices with maximum open interest (call + put), identifying key support/resistance levels implied by the options market.

**Data Sources:**
- **Primary:** Angel One live OI data (SmartAPI)
- **Fallback:** NSE BhavCopy (end-of-day)

**CLI Options:**

| Flag | Default | Effect |
|------|---------|--------|
| `--expiry` | `weekly` | `weekly` or `monthly` expiry contracts |
| `--live` | off | Force Angel One live mode (skip BhavCopy) |
| `--new` | off | Create a new Excel file (default: **append** to existing) |

**Output:** `fno_<month>.xlsx` — e.g. `fno_aug.xlsx` (sheets: Equity F&O, Index F&O)

> **Naming is month-based, not `fno_max_oi.xlsx`.** By default the script
> **appends** to the most recent existing `fno_*.xlsx` in the project root.
> `--new` forces a fresh file named after the current month
> (`date.today().strftime("%b").lower()`).

**Usage:**
```bash
python3 fno_max_oi.py                    # weekly expiry, auto source, append
python3 fno_max_oi.py --expiry monthly   # monthly expiry contracts
python3 fno_max_oi.py --live             # force live Angel OI, skip BhavCopy
python3 fno_max_oi.py --new              # start a fresh workbook
```

---

### india_macro.py — India Macro Dashboard (28 Indicators)

**Purpose:** End-to-end macro/fiscal/financial-markets dashboard tracking 28 monthly indicators across 6 categories. Fetches from 10+ government/regulator sources, computes MoM and YoY growth rates, and produces an interactive HTML dashboard.

**Indicators (28 total, 6 categories):**

| Category | Indicators |
|----------|------------|
| Industrial (5) | Cement Production, Steel Production, Electricity Generation, Steel Dispatches, Fertilizer Dispatches |
| External Sector (3) | Forex Reserves Total, Forex FCA, Forex Gold |
| Energy (6) | Petroleum Consumption, Crude Oil Production, LPG Connections, PNG Connections, Renewable Capacity, State Power Generation |
| Banking (2) | SCB Total Credit, SCB Total Deposits |
| Capital Markets (12) | FPI Equity/Debt, MF AUM (Total/Equity/Debt/Hybrid), SIP Inflow, Folios (Equity/Debt/Hybrid), NSDL/CDSL Demat Accounts |

**Data Fetchers (12 direct):**

| Source | Indicators Updated |
|--------|-------------------|
| RBI WSS (DBIE Excel) | forex_reserves, forex_fca, forex_gold, bank_credit, bank_deposit |
| AMFI Monthly Report | MF AUM (4), SIP inflow, Folios (3) |
| CEA Executive Summary PDF | electricity_generation |
| PPAC Oil & Gas PDF | petroleum_consumption, crude_oil_production |
| NSDL FPI Monthly | fpi_equity, fpi_debt |
| Ministry of Steel PDF | steel_production, steel_dispatch |
| Dept of Fertilizers PDF | fertilizer_dispatch |
| NSDL Demat HTML | depository_demat_nsdl |
| CDSL Periodic PDF | depository_demat_cdsl |
| PPAC LPG XLSX | lpg_connections |
| PPAC PNG XLSX | png_connections |
| OEA Core-8 XLSX | cement_production |

**CLI Options:**

| Flag | Effect |
|------|--------|
| (no args) | Build dashboard from current CSVs (no fetch) |
| `--list` | List all indicators and their populated/pending status |
| `--add <id> <period> <value>` | Manually add a data point |
| `--print <id>` | Print one indicator's data table with growth rates |
| `--fetch-direct` | Run all 12 direct fetchers then rebuild dashboard |
| `--fetch-browser` | Run browser-based fetchers |
| `--ogd-test <uuid>` | Inspect a data.gov.in dataset |
| `--ogd-find <query>` | Search data.gov.in for a dataset |
| `--fetch <id>` | Pull single indicator from OGD |
| `--fetch-all` | Pull all OGD + direct + browser fetchers |

**Output:**
- `india_macro_data.xlsx` (Overview sheet + one data sheet per indicator)
- `india_macro_dashboard.html` (multi-tab Plotly HTML page, one chart per indicator)
- Data stored in `data/india_macro/<indicator_id>.csv` (append-only)

**Usage:**
```bash
python3 india_macro.py --fetch-direct    # production run (used by automation)
python3 india_macro.py --list            # check what's populated
python3 india_macro.py --add cement_production 2025-05 38.5
python3 india_macro.py --print cement_production
```

---

### forensic_accounting.py — Forensic & Deep Fundamental Analysis

**Purpose:** Comprehensive single-stock forensic + deep fundamental analysis generating a 40+ page professional PDF report with investment recommendation (BUY / HOLD / SELL / AVOID).

**Analysis Modules:**
- **Forensic Scores:** Beneish M-Score, Altman Z-Score, Piotroski F-Score, DuPont decomposition, Springate S-Score, Ohlson O-Score, Montier C-Score, Benford's Law digit analysis
- **Deep Fundamentals:** Shareholding trend, insider trading, peer comparison, relative strength, technical structure, Graham/Magic Formula valuation, capex cycle, tax sustainability, institutional holdings, credit rating intelligence

**Data Sources:**
- yfinance (financials, prices, MF holders, corporate actions)
- Screener.in (universal financials backfill — HTML scrape)
- NSE APIs (credit ratings, shareholding, SAST, delivery data, concalls, filings)
- Local PDF parsing (concall transcripts, investor presentations, annual reports)

**Resilience:**
- Ticker resolution: `.NS` → `.BO` → SME alias map → prefix-truncation → yf.Search
- Universal financials: Screener.in backfills when yfinance is sparse
- PDF-regex extraction as last-resort financial source
- Never refuses — always produces a report, even for data-poor stocks

**Output:** `forensic_report_<SYMBOL>_<timestamp>.pdf`

**CLI Options:**

| Flag | Default | Effect |
|------|---------|--------|
| `<SYMBOL>` (positional) | built-in `COMPANY_SYMBOL` | Ticker to analyse |
| `--compare`, `-c` | `None` | Comma-separated peer tickers to compare against |

**Usage:**
```bash
python3 forensic_accounting.py TCS
python3 forensic_accounting.py RELIANCE
python3 forensic_accounting.py TCS --compare INFY,WIPRO
python3 forensic_accounting.py              # uses default COMPANY_SYMBOL in file
python3 -c "from forensic_accounting import run; run('RELIANCE')"
```

**Note:** Not part of `run_all.py` — always run independently.

---

### breakout_review.py — Walk-Forward Validation

**Purpose:** Reviews weekly breakout scanner snapshots to evaluate prediction accuracy. Compares breakout candidates against actual post-scan price action.

**Classification of Outcomes:**
- `TRUE_BREAKOUT` — Closed above R for ≥2 sessions with volume confirmation
- `BREAKOUT_LOW_VOL` — Closed above R for ≥2 sessions, no volume spike
- `ATTEMPTED` — Touched/crossed R at least once
- `HOLDING` — Positive since scan but hasn't reached R
- `FALSE_SIGNAL` — Never reached R, negative since scan
- `NO_DATA` — Could not fetch price data

**Folder Structure:**
```
Output/Week1/breakout_watchlist.xlsx
Output/Week2/breakout_watchlist.xlsx
...
Output/review_YYYYMMDD_HHMMSS.xlsx    (review output)
Output/review_cumulative.csv           (running accuracy stats)
```

**CLI Options:**

| Flag | Effect |
|------|--------|
| (no args) | Review all available weeks |
| `--weeks 1 2` | Review specific weeks only |
| `--full` | Also check for missed breakouts in full universe |

**Usage:**
```bash
python3 breakout_review.py
python3 breakout_review.py --weeks 1 2 --full
```

---

### breakout_deep_analysis.py — Breakout Rule Mining

**Purpose:** Evidence-based pattern mining on the accumulated walk-forward
review data. Finds selection **rules** (feature thresholds + combinations) that
maximise the probability of a real, tradeable breakout — the highest-conviction
"elite" subset of scanner candidates. Analysis-only; does **not** modify the
scanner.

**Input:** `Output/review_*.xlsx` (sheet `All Results`, produced by
`breakout_review.py`).

**Outcome targets per candidate:**
- `true_bo` — status == `TRUE_BREAKOUT`
- `tradeable` — max gain ≥ 15%
- `big_win` — max gain ≥ 25%
- `dud` — max gain < 5% and ended negative

Gain-magnitude stats run on "mature" candidates only (≥ 15 sessions since scan).

**Usage:**
```bash
python3 breakout_deep_analysis.py                # latest review file
python3 breakout_deep_analysis.py <review.xlsx>  # specific file
```

---

### breakout_scorecard_review.py — Scorecard Walk-Forward Validator

**Purpose:** The feedback loop for the **scorecard** (mirrors what
`breakout_review.py` does for raw breakout signals). It checks whether a high
`CompositeScore` actually leads to better forward returns than a low one —
the evidence required before any composite re-weighting. Changes **no** scoring
logic; it only measures.

**Method:**
1. Loads dated snapshots from `data/scorecard_snapshots.csv` (written by the scorecard).
2. Keeps names old enough to have matured (`>= --min-days`).
3. Re-fetches post-snapshot OHLCV via Angel One (same downloader as the scanner / review).
4. Computes forward returns (1w / 4w / 12w), max-gain and max-drawdown from each snapshot's date.
5. Reports whether `CompositeScore` / `Verdict` / each axis ranked the winners.

**Outcome targets:** `tradeable = max_gain_pct >= --tradeable` (default 15%);
`dud = max_gain < 5% AND end_ret < 0`.

**Output:** `Output/scorecard_review_YYYYMMDD_HHMMSS.xlsx` + console summary.

**Usage:**
```bash
python3 breakout_scorecard_review.py                 # matured >= 7d
python3 breakout_scorecard_review.py --min-days 30   # only >= 30d matured
python3 breakout_scorecard_review.py --tradeable 15  # win bar = +15% max gain
```

---

### universe_review.py — Does the Scanner Add Value?

**Purpose:** Head-to-head validation — does the breakout scanner actually **add
value** over the raw filtered universe it selects from? For every matured week it
takes the two RAW universe sheets (`MPD Data`, `Screener Data`) and the two
breakout sheets from `breakout_watchlist.xlsx`, then measures the realised
outcome of every stock on the same yardstick (`tradeable = max_gain >= 15%`,
`big_win >= 25%`, `dud < 5%` & red). For each universe it compares three cohorts:
**ALL universe** vs **BREAKOUT (scanner-flagged)** vs **REJECTED**.

Imports shared helpers from `breakout_review.py`. Run alongside
`breakout_review.py` + `breakout_deep_analysis.py` on a review day.

**Output:** `Output/universe_review_YYYYMMDD.xlsx`.

**Usage:**
```bash
python3 universe_review.py                 # all matured weeks
python3 universe_review.py --weeks 1 2 3   # specific weeks
python3 universe_review.py --min-days 15   # maturity gate (default 15)
```

---

### universe_mining.py — Elite Raw-Universe Subset Miner

**Purpose:** Follow-up to `universe_review.py`. Mines the RAW universe
(`MPD Data` / `Screener Data`) for a cheap, **scanner-independent** feature
subset with a `>= 50%` tradeable rate — i.e. can a strong pool be pulled
straight from the raw universe, bypassing the breakout-timing penalty?
Runs univariate threshold sweeps then AND-combinations to surface the highest
tradeable% subset with adequate coverage. Does **not** touch the scanner.

Imports helpers from `breakout_review.py` **and** `universe_review.py`
(`_scan_close_from_ohlcv`, `_outcome`, `UNIVERSES`).

**Output:** `Output/universe_mining_YYYYMMDD.xlsx`.

**Usage:**
```bash
python3 universe_mining.py                        # all matured weeks
python3 universe_mining.py --weeks 1 2 3
python3 universe_mining.py --min-days 15 --min-cover 40
```

---

## Portfolio System

Located in `portfolio/`. A parallel analysis pipeline focused on owned positions rather than the broader market.

### portfolio_run_all.py — Portfolio Orchestrator

Runs all 9 portfolio scenarios in sequence, consolidates into a unified workbook, and optionally emails.

```bash
python3 portfolio/portfolio_run_all.py              # all + email
python3 portfolio/portfolio_run_all.py --no-email   # dry run
```

### Execution Order (9 Scenarios)

| # | Module | Purpose |
|---|--------|---------|
| 1 | `portfolio_tracker` | P&L, sector exposure, concentration (Top 5/10 weights) |
| 2 | `position_health` | DMA/RSI/drawdown technical scan, ACTION/WATCH/OK flags |
| 3 | `sl_target_tracker` | User-defined SL/Target hit alerts |
| 4 | `risk_metrics` | Portfolio beta, VaR (1d/5d 95%), Sharpe, max drawdown |
| 5 | `correlation_clusters` | Return-correlation pairs & greedy clusters (hidden concentration) |
| 6 | `pledge_promoter` | Pledge % + promoter holding red flags |
| 7 | `mf_overlap` | Mutual fund crowding analysis |
| 8 | `events_calendar` | Upcoming corporate events for owned names (30 days) |
| 9 | `premarket_dashboard` | Global cues, FX/commodities, NIFTY 500 breadth |

### Output

- `portfolio/portfolio_report.xlsx` — Unified workbook (~16 sheets)
- `portfolio/premarket_dashboard_chart.html` — 4-panel breadth chart
- Email with both attached (unless `--no-email`)

### Running Portfolio Modules Individually

**Every** portfolio module is independently runnable and each accepts a single
`--out` flag. Useful when you only want one answer and not the full 9-scenario
sweep. Run them from the project root:

```bash
python3 portfolio/portfolio_tracker.py       --out portfolio/portfolio_tracker.xlsx
python3 portfolio/position_health.py         --out portfolio/position_health.xlsx
python3 portfolio/sl_target_tracker.py       --out portfolio/sl_target.xlsx
python3 portfolio/risk_metrics.py            --out portfolio/risk_metrics.xlsx
python3 portfolio/correlation_clusters.py    --out portfolio/correlation_clusters.xlsx
python3 portfolio/pledge_promoter.py         --out portfolio/pledge_promoter.xlsx
python3 portfolio/mf_overlap.py              --out portfolio/mf_overlap.xlsx
python3 portfolio/events_calendar.py         --out portfolio/events_calendar.xlsx
python3 portfolio/premarket_dashboard.py     --out portfolio/premarket_dashboard.xlsx
```

The `--out` default for each is exactly the path shown above, so the flag can be
omitted entirely:

```bash
python3 portfolio/position_health.py     # writes portfolio/position_health.xlsx
```

**Orchestrator flags:**

| Flag | Effect |
|------|--------|
| `--no-email` | Run all 9 scenarios but skip email delivery |

> `portfolio_run_all.py` has **no** `--skip` flag — unlike `run_all.py`. To run a
> subset, invoke the individual modules above.

> **Prerequisite:** all modules need a broker holdings export discoverable by
> `holdings_loader.py` (searches `portfolio/` → project root → `~/Downloads`).
> Without it they exit early.

---

### Portfolio Modules — Detail

#### portfolio_tracker.py

P&L, sector exposure, and concentration metrics.

**Sheets:** Positions, Portfolio Summary, Sector Exposure, Concentration

**Data:** Broker holdings file only (no network calls).

---

#### position_health.py

Daily technical health check for every owned position.

**Signals Computed:**
- Last close vs 50/100/200-DMA (above/below + % distance)
- Distance from 52-week high (drawdown)
- 3-month and 6-month price return
- Mansfield RS vs Nifty 500 (^CRSLDX) — 3 months
- Volume spike (today vol / 50-day avg)
- Down-day on volume flag

**Flag Rules:**
- `ACTION` — Close < 200-DMA, or drawdown > 25%, or (RS3M < 90 and close < 100-DMA)
- `WATCH` — Close < 50-DMA, or RS3M < 100, or volume spike on down day
- `OK` — None of the above

**Sheets:** Position Health, Action List, Health Notes

---

#### sl_target_tracker.py

Monitors user-defined stop-loss and target levels.

**Input:** `portfolio/holdings_meta.csv` (user-maintained; auto-generates template if missing)

**Status Values:** `STOP_HIT` | `NEAR_STOP` (<3%) | `TARGET_HIT` | `NEAR_TARGET` (<3%) | `OK` | `NO_LEVELS`

---

#### risk_metrics.py

Portfolio-level risk dashboard.

**Metrics:**
- Per-position beta vs Nifty 50, annualised volatility, max drawdown
- Portfolio-weighted beta
- 1-day and 5-day Value-at-Risk (95%, parametric)
- Sharpe ratio
- Portfolio NAV time series (synthetic, 1 year)
- Best / worst single day

**Sheets:** Risk (per position), Risk Summary, Risk Notes

---

#### correlation_clusters.py

Hidden concentration via return correlations.

**Method:**
- 1-year daily-return correlation matrix across all holdings
- Flags pairs with |corr| >= 0.70 that represent meaningful weight
- Greedy clustering (>= cluster threshold within cluster)
- Reports cluster weight as "true thematic exposure"

---

#### pledge_promoter.py

Promoter pledge and holding red-flag scanner.

**Flags:**
- `RED` — Pledged > 25% OR promoter holding < 30%
- `AMBER` — Pledged > 10% OR promoter holding < 40%
- `OK` — Pledged ≤ 10% AND promoter holding ≥ 40%

**Data Source:** Tickertape Screener API (same as fii_stake_tracker.py)

---

#### mf_overlap.py

Mutual fund crowding detector.

**Method:**
1. Pull ETMoney shareholding sitemap (~2,400 stocks)
2. Resolve NSE Symbol → ETMoney URL via fuzzy slug match
3. Fetch scheme list for each owned stock (cached 30 days)
4. Aggregate: FundCount, AvgWeight%, MaxWeight%, CrowdingScore

**Sheets:** MF Holders Per Stock, MF Crowding Summary

**Note:** Reads `portfolio/mf_holdings.csv` for additional context.

---

#### events_calendar.py

Upcoming corporate events for the next 30 days.

**Event Types:**
- Board meetings (results, dividends, fund-raising, buy-back)
- Corporate actions (ex-dividend, split, bonus, AGM, record dates)
- Recent announcements (last 7 days)

**Data Source:** NSE public APIs (board-meetings, corporate-actions, announcements)

**Sheets:** Owned-Board-Meetings, Owned-Corp-Actions, Owned-Announcements, Notes

---

#### premarket_dashboard.py

Pre-open snapshot delivered before 9:15 IST.

**Coverage:**
- Global indices: S&P 500, Nasdaq, Dow, Nikkei 225, Hang Seng, FTSE
- India: Nifty 50, Bank Nifty, India VIX
- GIFT Nifty (SGX-replacement futures)
- Currencies: USDINR, DXY
- Commodities: Brent, Gold, Copper
- Yields: US 10-year Treasury
- Breadth: % of Nifty 500 above 50-DMA/200-DMA, new 52W highs vs lows

**Output:** `premarket_dashboard_chart.html` (4-panel breadth chart)

---

#### holdings_loader.py

Unified parser for broker holdings exports.

**Supported Formats:**
- Angel One `holdings.xlsx` (3 sheets: Equity / Mutual Funds / Combined)
- Groww `Stocks_Holdings_Statement.xlsx` (single sheet, company-name based)

**Auto-discovery:** Searches `portfolio/` → project root → `~/Downloads`

**ISIN Resolution:** NSE `EQUITY_L.csv` + `SME_EQUITY_L.csv` (cached 7 days)

---

#### _prices.py

Shared helper that fetches & caches daily Close prices for all holdings. Used by `risk_metrics` and `correlation_clusters` to avoid duplicate data pulls within a single orchestrator run.

**Library module — not runnable.**

---

## Interactive Web Apps

Three self-contained browser apps sit on top of the shared data layer. They are
**not** part of `run_all.py` — launch each independently. All run locally and
reuse the parent project's `.env` (Angel One) and `data_provider.py`.

| App | Port | Launch | Purpose |
|-----|------|--------|---------|
| **tradingcharts** | 5050 | `python3 tradingcharts/run.py` | Full charting dashboard |
| **multiscreen** (sidecar) | 5051 | `python3 tradingcharts/multiscreen/run.py` | 4 isolated workspace replicas of tradingcharts |
| **screener** | 5052 | `python3 screener/run.py` | Screener.in-backed valuation & financial charts |

### tradingcharts/ — Charting Dashboard (port 5050)

Flask + vanilla-JS single-page dashboard (lightweight-charts). Angel One
SmartAPI primary, jugaad-data / yfinance fallback — all via `../data_provider.py`.

**Launch:**
```bash
python3 tradingcharts/run.py     # cross-platform (recommended)
./tradingcharts/run.sh           # macOS/Linux
tradingcharts\run.bat            # Windows
PORT=5060 python3 tradingcharts/app.py   # custom port
```

**No CLI flags.** All three launchers do the same three things: activate the
parent venv, `pip install -q -r tradingcharts/requirements.txt`, then exec
`app.py`.

| Property | Value |
|---|---|
| Port | `FIXED_PORT = 5050`, overridable via `PORT` env var |
| Server | `waitress`, `host=127.0.0.1`, `threads=16` |
| Self-healing | Kills stale instances holding port 5050 on boot |
| State | `tradingcharts/state/state.json` (atomic `os.replace`, thread-locked) |
| Logs | `tradingcharts/logs/` (per-day subdirectories) |
| Env | `PORT` (optional), `ANGEL_*` from parent `../.env` |
| Extra deps | `flask`, `flask-cors`, `waitress` (in `tradingcharts/requirements.txt`) |

**REST API (14 routes):**

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Serve `static/index.html` |
| `/static/<path>` | GET | Frontend assets |
| `/api/symbols` | GET | Popular symbols + custom indices (`CIDX:*`) |
| `/api/search?q=` | GET | Symbol prefix search (min 3 chars) |
| `/api/historical` | GET | OHLCV candles (`symbol`, `interval`, `days`); 1m–1mo |
| `/api/quote?symbol=` | GET | Latest LTP/OHLC for one symbol |
| `/api/quotes?symbols=` | GET | Batched quotes, comma-separated |
| `/api/ticks?symbols=` | GET | Batched live-tick read (browser polls this) |
| `/api/subscribe` | POST | Start streaming live ticks for symbols |
| `/api/unsubscribe` | POST | Stop streaming live ticks |
| `/api/bulkblock?symbol=` | GET | Bulk/block institutional deals for a symbol |
| `/api/state` | GET | Read persisted UI state |
| `/api/state` | POST | Replace persisted UI state |
| `/api/health` | GET | Uptime, Angel session, WS connected, cache stats |

**Data sources:** Angel One SmartAPI (+ WebSocket for live ticks), jugaad-data,
yfinance, NSE `snapshot-capital-market-largedeal`, BSE bulk/block deal APIs.

**Features:** Candles/Bars/Heikin Ashi; Dark/Light; 1/2/4/6/8-pane layouts;
timeframes 5m–1mo; view ranges 1M–10Y. Indicators: SmartVPSG (gap/volume
markers, 52w stats, R.Vol, optional Volume Profile), SupResEPS (MAs
10/20/50/200 + pivots), RSI, MACD, Relative Strength vs 9 benchmarks, and
InstiAccum (institutional-accumulation composite). 14 TradingView-style drawing
tools with stable IDs. Alerts on Price/Volume/Drawing-Cross with browser
notification + audio. Watchlists up to 45 lists × 450 stocks with TV-style CSV
upload.

Full detail: [tradingcharts/README.md](tradingcharts/README.md) and
[tradingcharts/RULES.md](tradingcharts/RULES.md).

### multiscreen/ — 4 Isolated Workspaces (port 5051)

Sidecar server hosting four independent dashboards without touching the main app.

**Launch:**
```bash
python3 tradingcharts/multiscreen/run.py
MULTISCREEN_PORT=5055 python3 tradingcharts/multiscreen/server.py   # custom port
```

**No CLI flags.** `run.py` installs `flask`, `requests`, `waitress` then execs
`server.py`.

| Property | Value |
|---|---|
| Port | `MULTISCREEN_PORT` env var, default `5051` |
| Upstream | `MULTISCREEN_UPSTREAM` env var, default `http://127.0.0.1:5050` |
| Server | `waitress` (`threads=24`), falls back to Flask dev server |
| State | `tradingcharts/multiscreen/state/<wsid>.json` — one per workspace |
| Workspaces | `default`, `ws2`, `ws3`, `ws4` |

**Routes (8):**

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Redirect to `/w/default` |
| `/w/<wsid>` | GET | Chart UI for a workspace; injects the localStorage shim |
| `/multiscreen/shim.js` | GET | Namespaces every `localStorage` key as `tc:<wsid>:` |
| `/static/<path>` | GET | Serves `../static/` (same assets as main app) |
| `/api/state?wsid=` | GET | Read workspace-local state |
| `/api/state?wsid=` | POST | Write workspace-local state |
| `/api/<path>` | GET/POST/PUT/DELETE/PATCH | Reverse-proxy to port 5050 |
| `/multiscreen/health` | GET | `{ok, port, upstream, workspaces}` |

**Key design point:** every `/api/*` call except `/api/state` is proxied
upstream, so there is exactly **one** Angel WebSocket and **one** candle cache
shared by all four workspaces. The main server's `state/state.json` is never
touched, and killing the sidecar leaves port 5050 running.

> **Requires the main app to be running first** — the sidecar proxies to 5050.

Full detail: [tradingcharts/multiscreen/README.md](tradingcharts/multiscreen/README.md).

### screener/ — Valuation & Financials App (port 5052)

Flask app that scrapes **Screener.in** for company financials and falls back to
yfinance, exposing a financial time-series API rendered as charts.

**Launch:**
```bash
python3 screener/run.py
```

**No CLI flags.** `run.py` installs the **root** `requirements.txt` then execs
`app.py`.

| Property | Value |
|---|---|
| Port | Hardcoded `PORT = 5052` in `app.py` |
| Server | Flask dev server, `host=0.0.0.0` |
| State | `screener/state/state.json` (atomic `.tmp` → `replace()`) |
| Env | `SCREENER_USER`, `SCREENER_PASS` from root `.env` (optional) |
| Extra deps | **None** — no local `requirements.txt` |

**Routes (7):**

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Serve `static/index.html` |
| `/tc-static/<path>` | GET | Cross-link to `../tradingcharts/static/` assets |
| `/api/metrics` | GET | Metric catalog + metadata |
| `/api/search?q=` | GET | Company search → `{ticker, name, slug, url}` |
| `/api/stock` | GET | Financials for `symbol` + comma-separated `metrics` |
| `/api/state` | GET | Read persisted UI state |
| `/api/state` | POST | Persist UI state |
| `/api/health` | GET | `{ok, provider, auth_configured, auth_ok}` |

**Data:** Screener.in company pages (`/company/<TICKER>/consolidated/`, HTML
scrape, CSRF login) with yfinance fallback.

> Two caveats worth knowing. `run.py` sets `PORT=5052` in the environment but
> `app.py` **ignores it** and uses its hardcoded constant — changing the port
> means editing `app.py`. And unlike the other two apps, this one binds
> `0.0.0.0`, not `127.0.0.1`, so it is reachable from your local network.
> There is **no** `screener/README.md`.

---

## Data Layer

### data_provider.py — Unified OHLCV Provider

Drop-in replacement for `yfinance.download()` with a 3-tier fallback chain:

```
1. Angel One SmartAPI  (primary — free, complete NSE/BSE/SME coverage)
2. jugaad-data         (fallback — NSE only, scrapes nseindia.com)
3. yfinance            (last resort — broad coverage, sometimes flaky)
```

**API:**
```python
from data_provider import download

# Single ticker → flat DataFrame[Open,High,Low,Close,Volume]
df = download("RELIANCE.NS", period="1y")

# Multiple tickers → MultiIndex DataFrame
df = download(["RELIANCE.NS", "TCS.NS"], start="2024-01-01", end="2025-01-01")
```

All yfinance kwargs accepted and ignored for compatibility.

---

### angel_client.py — Angel One SmartAPI Adapter

Manages SmartAPI sessions, scrip master, and OHLCV fetching.

**Public API:**
- `angel_download(ticker, start, end, interval="1d")` → DataFrame
- `angel_download_many(tickers, start, end, max_workers=2)` → dict
- `get_angel_session()` → (api_key, jwt_token) — lazy, auto-relogin
- `refresh_token(force=False)` → bool

**Scrip Master:**
- ~25MB JSON file from Angel One, cached for 7 days at `.angel_scrip_master.json`
- Maps yfinance-style tickers (RELIANCE.NS) to Angel symboltoken + exchange

**Index Overrides:**
```python
INDEX_OVERRIDES = {
    "^NSEI":    ("NSE", "99926000", "Nifty 50"),
    "^CRSLDX":  ("NSE", "99926004", "Nifty 500"),
    "^NSEBANK": ("NSE", "99926009", "Nifty Bank"),
    "^BSESN":   ("BSE", "99919000", "Sensex"),
}
```

---

### email_sender.py — SMTP Email Utility

Shared module for sending consolidated reports with file attachments over SMTP/TLS.

**Usage (library only):**
```python
from email_sender import send_report
send_report(subject="...", body="...", attachments=["file1.xlsx", "chart.html"])
```

---

### ohlcv_cache.py — Incremental Daily-Bar Cache

Two-tier cache sitting in front of `angel_client`'s `getCandleData`, for **daily
bars only**. Intraday intervals (5m/15m/…) bypass it entirely, so live charting
behaviour is unchanged.

| Tier | Scope | Purpose |
|---|---|---|
| **L1** in-memory | Per-process dict, TTL-bounded | Deduplicates repeat fetches within one run |
| **L2** on-disk | One pickle per `(symbol, interval)` | Cross-run incremental history — a re-run pulls only new bars |

**Correctness guards** (why it is safe to trust):
1. **Closed-sessions-only** — today's in-progress bar is served to the caller but
   never written to disk, so a partial bar can never be persisted.
2. **Overlap-overwrite** — each run re-fetches the last `OVERLAP_DAYS` and merges
   with `keep="last"`, so provisional bars get finalised and split/adjustment
   restatements overwrite stale values.
3. **Validate-or-rebuild** — any integrity failure (bad columns, duplicate or
   unsorted index, NaN OHLC, `High < Low`, negative volume, unreadable file)
   discards the entry and forces a full re-fetch.

The cache is a performance layer, never a source of truth — deleting it costs
time, never correctness.

**Library module — not runnable.** Used by `ipo_listing_gainers.py` and the
breakout/review family via `data_provider`.

---

## Scheduling & Automation

### launchd (macOS)

The pipeline is scheduled via a launchd plist (`com.analysis.runall`) that triggers `scripts/run_market_analysis.sh` at **18:00 IST, Monday–Friday**.

### scripts/run_market_analysis.sh

Wrapper script responsibilities:
1. `cd` into project directory
2. Activate the venv — sources `venv/bin/activate` **if present** (currently the
   legacy Python 3.9 `venv/`), otherwise falls back to system `python3`. See note below.
3. Load `.env` (exports all EMAIL_*, ANGEL_* variables)
4. Run `python3 run_all.py` with output to timestamped log
5. Skip weekends defensively (re-checks day-of-week)
6. Prune logs older than 30 days

> **Venv mismatch note:** the wrapper activates `venv/` (3.9), while the
> canonical interactive environment is `.venv/` (3.11). If you consolidate on
> `.venv`, update line 39–41 of `scripts/run_market_analysis.sh` to source
> `.venv/bin/activate` so scheduled runs use the same interpreter and deps.

### launchd Setup & Operations

**Files:**
- `scripts/run_market_analysis.sh` — wrapper script
- `~/Library/LaunchAgents/com.analysis.runall.plist` — agent (5 calendar entries, one per weekday)

**Install (one-time):**

```bash
chmod +x scripts/run_market_analysis.sh

# Plist already lives in ~/Library/LaunchAgents
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.analysis.runall.plist
launchctl enable    gui/$(id -u)/com.analysis.runall
```

**TCC permission (one-time):** macOS blocks `/bin/bash` from accessing
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

### GitHub Actions (Cloud Schedule)

`.github/workflows/scenarios.yml` provides an alternate cloud schedule
(cron `0 13 * * 1-5` ≈ 18:30 IST). It checks out the repo, installs deps,
runs `run_all.py` with secrets injected from GitHub Actions secrets, uploads
the Excel + charts as artifacts, and dispatches a follow-on `send-email` job.

---

## Output Files — Which Script Produces What

Complete producer → output map. **Lifecycle**: *Overwritten* = same path every
run; *Timestamped* = new file per run, old ones accumulate; *Append-only* =
history grows and is never pruned.

### Orchestrators

| Producer | Output | Location | Lifecycle |
|---|---|---|---|
| `run_all.py` | `market_analysis_report.xlsx` (~19 sheets) | root | Overwritten |
| `run_all.py` | `market_charts.html` (6 charts, tabbed) | root | Overwritten |
| `run_all.py` | 6 × `*_chart.html` (see next table) | root | Overwritten |
| `portfolio/portfolio_run_all.py` | `portfolio_report.xlsx` (~16 sheets) | `portfolio/` | Overwritten |
| `portfolio/portfolio_run_all.py` | `premarket_dashboard_chart.html` | `portfolio/` | Overwritten |

### Scenario modules (when run standalone)

Under `run_all.py` the `.xlsx` files below are **deleted after capture** — their
data goes into `market_analysis_report.xlsx` and only the charts survive.

| Producer | Excel | Chart HTML |
|---|---|---|
| `BulkBlock.py` | `BULK_BLOCK_Deals_<timestamp>.xlsx` | — |
| `custom_sector_index.py` | `custom_sector_index.xlsx` | `custom_sector_index_chart.html` |
| `fii_flows.py` | `fii_flows.xlsx` | `fii_flows_chart.html` |
| `fii_sector_flows.py` | `fii_sector_flows.xlsx` | `fii_sector_flows_chart.html` |
| `sector_momentum.py` | `sector_momentum.xlsx` | `sector_momentum_chart.html` |
| `nse_ready_sectors.py` | `nse_sector_rs.xlsx` | `nse_sector_rs_chart.html` |
| `rrg_chart.py` | `rrg_chart.xlsx` | `rrg_chart.html` |
| `ipo_anchor_tracker.py` | `ipo_anchor_tracker.xlsx` | — (+ `ipo_anchor_report.txt`) |
| `fii_stake_tracker.py` | `fii_stake_tracker.xlsx` | — |

All of the above take `-o PREFIX` / `--output PREFIX`, which changes the stem of
both the `.xlsx` and the `_chart.html`.

> `rrg_chart.py` builds its chart as `prefix + ".html"`, so the file is
> **`rrg_chart.html`** — not `rrg_chart_chart.html`. A stale
> `Output/rrg_chart_chart.html` from an older prefix may still exist; ignore it.

### Breakout family

| Producer | Output | Location | Lifecycle |
|---|---|---|---|
| `breakout_scanner_angel.py` | `breakout_watchlist.xlsx` (6 sheets) | root | Overwritten |
| `breakout_scanner_angel.py` | `breakout_watchlist_<tag>.xlsx` (with `--out-tag`) | root | One per tag |
| `breakout_scanner_angel.py` | `screener_data.xlsx` | `Output/` | Overwritten |
| `breakout_scanner_angel.py` | `tv_breakouts_combined.txt`, `tv_common.txt`, `tv_unique_mpd.txt`, `tv_unique_screener.txt` | root | Overwritten |
| `breakout_scanner_angel.py` | `logs_breakout_scanner_angel_v35_<timestamp>.txt` | root | Timestamped |
| `breakout_scanner_scorecard.py` | `Scorecard` sheet appended to `breakout_watchlist.xlsx` | root | Overwritten |
| `breakout_scanner_scorecard.py` | `breakout_watchlist_scorecard.html` | root | Overwritten |
| `breakout_scanner_scorecard.py` | `data/scorecard_snapshots.csv` | `data/` | **Append-only** |
| `multi_pct_down.py` | `multi_pct_down.xlsx` (sheet per universe) | root | Overwritten |
| `multi_pct_down.py` | `multi_pct_down.txt` (TradingView) | root | Overwritten |

### Review family

| Producer | Output | Lifecycle |
|---|---|---|
| `breakout_review.py` | `Output/review_<YYYYMMDD_HHMMSS>.xlsx` | Timestamped |
| `breakout_review.py` | `Output/review_cumulative.csv` | **Append-only** |
| `breakout_deep_analysis.py` | Console report only — reads `Output/review_*.xlsx` | No file |
| `breakout_scorecard_review.py` | `Output/scorecard_review_<timestamp>.xlsx` | Timestamped |
| `universe_review.py` | `Output/universe_review_<YYYYMMDD>.xlsx` | One per day |
| `universe_mining.py` | `Output/universe_mining_<YYYYMMDD>.xlsx` | One per day |

**Input contract:** the review family reads weekly snapshots from
`Output/Week<N>-<DDMon>/breakout_watchlist.xlsx`. Copy the scanner's workbook
into a new `Week<N>` folder each week, or the reviewers find nothing to review.

### Standalone analysis

| Producer | Output | Location | Lifecycle |
|---|---|---|---|
| `ipo_listing_gainers.py` | `ipo_listing_gainers.csv` | root | Overwritten |
| `ipo_listing_gainers.py` | `ipo_listing_gainers.xlsx` (3 sheets) | root | **Merged** — Sheet 2 keeps existing names |
| `ipo_listing_gainers.py` | `nse_past_issues.json`, `bse_public_issues.json` | `data/ipo/` | **Append-only** |
| `fno_max_oi.py` | `fno_<month>.xlsx` e.g. `fno_aug.xlsx` | root | Appends to latest; `--new` starts fresh |
| `india_macro.py` | `india_macro_data.xlsx` (Overview + 1 sheet/indicator) | root | Overwritten |
| `india_macro.py` | `india_macro_dashboard.html` | root | Overwritten |
| `india_macro.py` | `<indicator_id>.csv` (28 files) | `data/india_macro/` | **Append-only** |
| `forensic_accounting.py` | `forensic_report_<SYMBOL>_<timestamp>.pdf` | root | Timestamped |

### Portfolio modules (when run standalone)

Each writes into `portfolio/` and is overwritten every run. Under
`portfolio_run_all.py` these become sheets inside `portfolio_report.xlsx` instead.

| Producer | Output |
|---|---|
| `portfolio_tracker.py` | `portfolio_tracker.xlsx` |
| `position_health.py` | `position_health.xlsx` |
| `sl_target_tracker.py` | `sl_target.xlsx` (also generates a `holdings_meta.csv` template if missing) |
| `risk_metrics.py` | `risk_metrics.xlsx` |
| `correlation_clusters.py` | `correlation_clusters.xlsx` |
| `pledge_promoter.py` | `pledge_promoter.xlsx` |
| `mf_overlap.py` | `mf_overlap.xlsx` |
| `events_calendar.py` | `events_calendar.xlsx` |
| `premarket_dashboard.py` | `premarket_dashboard.xlsx` + `premarket_dashboard_chart.html` |

### Web apps (state, not reports)

| Producer | Output | Contents |
|---|---|---|
| `tradingcharts/app.py` | `tradingcharts/state/state.json` | Watchlists, drawings, alerts, pane layout, theme |
| `tradingcharts/multiscreen/server.py` | `tradingcharts/multiscreen/state/<wsid>.json` | One per workspace: `default`, `ws2`, `ws3`, `ws4` |
| `screener/app.py` | `screener/state/state.json` | UI selections and settings |
| `tradingcharts/app.py` | `tradingcharts/logs/` | Per-day subdirectories |

### Caches (safe to delete — costs time, never correctness)

| Producer | Path | Purpose |
|---|---|---|
| `angel_client.py` | `.angel_scrip_master.json` | ~25 MB scrip master, 7-day TTL |
| `ohlcv_cache.py` | `.cache/` pickles | One per `(symbol, interval)`, daily bars only |
| `ipo_listing_gainers.py` | `.cache/ipo_gainers/` | TTL'd HTTP responses |
| `forensic_accounting.py` | `.cache/screener_<md5>.html` | Scraped Screener.in pages |
| `fii_flows.py` | `fii_equity_cache.csv` | **Append-only**, deduped by date |
| `fii_flows.py` | `fii_oi_cache.csv` | **Append-only** derivatives OI |
| `mf_overlap.py` | ETMoney scheme cache | 30-day TTL |
| `scripts/run_market_analysis.sh` | `logs/run_all_<timestamp>.log` | Auto-pruned after 30 days |

> **Do not delete `data/`.** `data/ipo/`, `data/india_macro/` and
> `data/scorecard_snapshots.csv` are append-only histories that **cannot be
> rebuilt** — the exchanges and regulators publish only current windows, so
> anything not banked at the time is gone permanently.

### Where everything lands

```
Analysis/
├── market_analysis_report.xlsx            ← run_all.py main report
├── market_charts.html                     ← 6 charts, tabbed
├── custom_sector_index_chart.html
├── fii_flows_chart.html
├── fii_sector_flows_chart.html
├── sector_momentum_chart.html
├── nse_sector_rs_chart.html
├── rrg_chart.html
├── breakout_watchlist.xlsx                ← scanner (+ Scorecard sheet)
├── breakout_watchlist_scorecard.html
├── multi_pct_down.{xlsx,txt}
├── tv_*.txt                               ← TradingView watchlists
├── ipo_listing_gainers.{csv,xlsx}         ← IPO gainers + anchor lists
├── ipo_anchor_report.txt
├── india_macro_data.xlsx
├── india_macro_dashboard.html
├── fno_<month>.xlsx
├── BULK_BLOCK_Deals_<ts>.xlsx             ← only when BulkBlock.py is run alone
├── forensic_report_<SYM>_<ts>.pdf
│
├── Output/
│   ├── screener_data.xlsx
│   ├── review_<ts>.xlsx  /  review_cumulative.csv
│   ├── scorecard_review_<ts>.xlsx
│   ├── universe_review_<date>.xlsx
│   ├── universe_mining_<date>.xlsx
│   └── Week<N>-<DDMon>/breakout_watchlist.xlsx   ← weekly snapshots (review INPUT)
│
├── portfolio/
│   ├── portfolio_report.xlsx              ← unified portfolio report
│   ├── premarket_dashboard_chart.html
│   ├── <module>.xlsx                      ← per-module standalone outputs
│   ├── holdings_meta.csv                  ← user-maintained SL/Target levels
│   └── mf_holdings.csv
│
├── data/                                  ← APPEND-ONLY, never delete
│   ├── ipo/{nse_past_issues,bse_public_issues}.json
│   ├── india_macro/<indicator>.csv        (28 files)
│   ├── scorecard_snapshots.csv
│   └── scorecard_history.csv
│
├── logs/<YYYY-MM-DD>/                     ← pruned after 30 days
└── .cache/, .angel_scrip_master.json      ← disposable
```

---

## Inter-Module Dependencies

```
run_all.py
 ├── BulkBlock.py
 │    └── fii_stake_tracker.py
 ├── custom_sector_index.py ──→ data_provider → angel_client
 ├── fii_flows.py
 ├── fii_sector_flows.py
 ├── sector_momentum.py ──→ data_provider → angel_client
 ├── rrg_chart.py ──→ data_provider → angel_client
 └── ipo_anchor_tracker.py

breakout_scanner_angel.py
 ├── multi_pct_down.py ──→ data_provider → angel_client
 └── breakout_scanner_scorecard.py (attached post-process; reuses scanner candles)
      └── data/scorecard_snapshots.csv ──→ breakout_scorecard_review.py

Review family (manual, run on a "let's review" day):
 breakout_review.py ──→ breakout_deep_analysis.py
                   └──→ universe_review.py ──→ universe_mining.py

portfolio/portfolio_run_all.py
 ├── holdings_loader.py (shared by all below)
 ├── portfolio_tracker.py
 ├── position_health.py ──→ data_provider → angel_client
 ├── sl_target_tracker.py
 ├── risk_metrics.py ──→ _prices.py → data_provider
 ├── correlation_clusters.py ──→ _prices.py → data_provider
 ├── pledge_promoter.py (Tickertape API)
 ├── mf_overlap.py (ETMoney API)
 ├── events_calendar.py (NSE API)
 └── premarket_dashboard.py ──→ data_provider

All network-data scripts → email_sender.py (when emailing)
```

---

## Logging

- **Location:** `logs/<YYYY-MM-DD>/` (date-stamped directories)
- **Source:** `scripts/run_market_analysis.sh` writes to `logs/run_all_<timestamp>.log`
- **Retention:** Logs older than 30 days are auto-pruned by the shell wrapper
- **Content:** Full stdout + stderr from the entire pipeline run

---

## Data Sources

All data flows through public/free sources. No paid market-data feeds.

| Source | Used by | Auth |
|---|---|---|
| **Angel One SmartAPI** | `data_provider.py` (primary OHLCV) | `.env`: `ANGEL_*` |
| **Angel One WebSocket** | `tradingcharts/app.py` (live ticks) | `.env`: `ANGEL_*` |
| **jugaad-data** (NSE scrape) | `data_provider.py` (fallback) | None |
| **yfinance** | `data_provider.py` (final fallback), indices | None |
| **NSE archives CSV** | `multi_pct_down.py` (universe seed, F&O list) | None |
| **NSE API** (large-deal snapshot) | `BulkBlock.py` | Cookie-managed session |
| **BSE JSON API** | `BulkBlock.py` (primary BSE) | None |
| **BSE HTML scrape** | `BulkBlock.py` (fallback) | None |
| **NSDL FPI fortnightly** | `fii_sector_flows.py` | None |
| **NSDL FPI monthly** | `fii_flows.py` | None |
| **NSE BhavCopy (F&O)** | `fno_max_oi.py` (default EOD source) | None |
| **Tickertape Screener API** | `fii_stake_tracker.py`, `pledge_promoter.py` | None |
| **screener.in** | `fii_stake_tracker.py` (fallback), `breakout_scanner_angel.py`, `forensic_accounting.py`, `screener/app.py` | `.env`: `SCREENER_*` |
| **chittorgarh.com** | `ipo_anchor_tracker.py` (anchor tables) | None |
| **ETMoney** | `mf_overlap.py` (MF scheme lists) | None |
| **RBI / AMFI / CEA / PPAC / NSDL / CDSL** | `india_macro.py` (28 indicators) | None |
| **NSE corporate APIs** | `events_calendar.py`, `forensic_accounting.py` | None |

---

## Relative Strength Benchmarks

Different scripts use different benchmarks depending on the analysis level:

| Script | Benchmark | Rationale |
|--------|-----------|-----------|
| `sector_momentum.py` | Nifty 50 | Correct for sector-level RS |
| `rrg_chart.py` | Nifty 50 | Correct for sector rotation |
| `breakout_scanner_angel.py` | Nifty 500 (^CRSLDX) | Individual stock RS — broader universe |
| `multi_pct_down.py` | Nifty 500 (^CRSLDX) | Individual stock RS |
| `position_health.py` | Nifty 500 (^CRSLDX) | Individual stock RS for owned names |

---

## Key Configuration Files

| File | Purpose |
|------|---------|
| `.env` | All credentials and SMTP config |
| `index_constituents.json` | Sector definitions for custom_sector_index, sector_momentum, rrg_chart |
| `portfolio/holdings_meta.csv` | User-maintained SL/Target levels per position |
| `portfolio/mf_holdings.csv` | MF holdings context for overlap analysis |
| `rules.md` | Trading rules reference |
| `TRADING_STRATEGY.md` | Trading strategy documentation |

---

## Troubleshooting

| Symptom | Where to look / Fix |
|---|---|
| Pipeline output / scenario errors | `logs/run_all_<timestamp>.log` |
| launchd refused to start | `logs/launchd.err.log` |
| "Operation not permitted" | TCC — grant Full Disk Access to `/bin/bash` |
| Email skipped | Check `EMAIL_PASSWORD` in `.env` |
| Angel rate-limit errors | Ensure single-threaded login (handled automatically) |
| BSE deals empty | BSE JSON API sometimes 0-rows pre-EOD; HTML fallback kicks in |
| Scrip master download hangs | Delete `.angel_scrip_master.json` and re-run |
| Delisted stock errors | Gracefully skipped — check logs for specific tickers |

---

## Complete Command Reference

Every runnable entry point in one place. All commands assume:

```bash
cd /Users/ankit.srivastava/Documents/Analysis && source .venv/bin/activate
```

### Orchestrators

```bash
python3 run_all.py                                  # 8 scenarios + email
python3 run_all.py --no-email                       # 8 scenarios, no email
python3 run_all.py --skip bulk_block rrg            # skip named scenarios
# scenario names: bulk_block sector_index fii_flows fii_sector_flows
#                 sector_momentum nse_sector_rs rrg ipo_anchor

python3 portfolio/portfolio_run_all.py              # 9 scenarios + email
python3 portfolio/portfolio_run_all.py --no-email   # 9 scenarios, no email
```

### Scenario modules (also runnable standalone)

```bash
python3 BulkBlock.py [--dry-run]                           # --dry-run via sys.argv
python3 custom_sector_index.py [-c FILE] [-o PREFIX]
python3 fii_flows.py            [-o PREFIX] [--refresh]
python3 fii_sector_flows.py     [-o PREFIX]
python3 sector_momentum.py      [-c FILE] [-o PREFIX]
python3 nse_ready_sectors.py    [-o PREFIX]
python3 rrg_chart.py            [-o PREFIX]
python3 ipo_anchor_tracker.py   [--months 14] [--limit 0] [--no-anchors] [--out PATH]
python3 fii_stake_tracker.py    [-o PREFIX]            # default prefix: fii_stake_tracker
```

### Breakout family

```bash
python3 breakout_scanner_angel.py \
    [--max 0] [--min-score 50] [--lookback 252] [--no-strict] \
    [--high-conviction] [--symbols-csv FILE] [--screener-url URL] \
    [--skip-mpd] [--skip-screener] [--out-tag TAG]

python3 breakout_scanner_scorecard.py --workbook PATH [--lookback 400] [--no-forensic]

python3 multi_pct_down.py \
    [--min 2.0] [--max 21.0] [--skip nse nse-sme bse-sme] \
    [--workers 4] [--max-symbols 0] [--out DIR] [-o PREFIX]
```

### Review family (run on a "let's review" day, in this order)

```bash
python3 breakout_review.py           [--weeks 1 2] [--full]
python3 breakout_deep_analysis.py    [REVIEW_XLSX]
python3 universe_review.py           [--weeks 1 2 3] [--min-days 15]
python3 universe_mining.py           [--weeks 1 2 3] [--min-days 15] [--min-cover 30]
python3 breakout_scorecard_review.py [--min-days 7] [--tradeable 15.0]
```

### Standalone analysis

```bash
python3 ipo_listing_gainers.py \
    [--start 2025-01-01] [--end TODAY] [--threshold 50] [--window-days 30] \
    [--workers 5] [--limit 0] [--bse-recent] [--anchor-dir ./anchor_pdfs] \
    [--freq-words 2] [--no-ocr] [--out CSV] [--xlsx XLSX]

python3 fno_max_oi.py       [--expiry weekly|monthly] [--live] [--new]

python3 forensic_accounting.py SYMBOL [--compare PEER1,PEER2]

python3 india_macro.py                          # rebuild dashboard, no fetch
python3 india_macro.py --fetch-direct           # production run
python3 india_macro.py --fetch-all              # OGD + direct + browser
python3 india_macro.py --fetch-browser
python3 india_macro.py --list
python3 india_macro.py --print INDICATOR_ID
python3 india_macro.py --add INDICATOR_ID 2025-05 38.5
python3 india_macro.py --ogd-test UUID
python3 india_macro.py --ogd-find QUERY
```

### Portfolio modules (each takes `--out`, all defaults are correct)

```bash
python3 portfolio/portfolio_tracker.py
python3 portfolio/position_health.py
python3 portfolio/sl_target_tracker.py
python3 portfolio/risk_metrics.py
python3 portfolio/correlation_clusters.py
python3 portfolio/pledge_promoter.py
python3 portfolio/mf_overlap.py
python3 portfolio/events_calendar.py
python3 portfolio/premarket_dashboard.py
```

### Web apps (long-running servers, no flags)

```bash
python3 tradingcharts/run.py                    # port 5050
python3 tradingcharts/multiscreen/run.py        # port 5051 (needs 5050 up)
python3 screener/run.py                         # port 5052

PORT=5060 python3 tradingcharts/app.py                        # override port
MULTISCREEN_PORT=5055 python3 tradingcharts/multiscreen/server.py
```

### Scheduling

```bash
launchctl kickstart -k gui/$(id -u)/com.analysis.runall   # trigger now
launchctl bootout   gui/$(id -u)/com.analysis.runall      # disable
tail -f logs/run_all_*.log                                # watch
```

### Not runnable (library modules — import only)

`data_provider.py` · `angel_client.py` · `ohlcv_cache.py` · `email_sender.py` ·
`portfolio/holdings_loader.py` · `portfolio/_prices.py`

---

## License & Disclaimer

For personal research use only. All scraped data is sourced from public
exchange/regulator endpoints. No financial advice. Use at your own risk.
