# Indian Market Analysis & Portfolio Toolkit

Automated daily market analysis pipeline for Indian equities. Covers bulk/block deals, FII flows, sector momentum, breakout scanning, forensic accounting, macro indicators, and full portfolio management — all orchestrated with a single command and delivered via email.

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Architecture](#architecture)
3. [Environment & Configuration](#environment--configuration)
4. [run_all.py — Main Orchestrator](#run_allpy--main-orchestrator)
5. [Standalone Analysis Scripts](#standalone-analysis-scripts)
6. [Portfolio System](#portfolio-system)
7. [Data Layer](#data-layer)
8. [Scheduling & Automation](#scheduling--automation)
9. [Output Files & Directory Structure](#output-files--directory-structure)
10. [Inter-Module Dependencies](#inter-module-dependencies)
11. [Logging](#logging)

---

## Quick Start

```bash
cd /Users/ankit.srivastava/Documents/Analysis
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Additional deps not in requirements.txt (install manually):
pip install smartapi-python pyotp fpdf2 PyPDF2 httpx numpy

# Configure credentials
cp .env.example .env   # then fill in values (see Configuration below)

# Run everything (market closed, so no email):
python3 run_all.py --no-email

# Portfolio analysis:
python3 portfolio/portfolio_run_all.py --no-email
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATORS                                    │
│  run_all.py (7 market scenarios)   portfolio/portfolio_run_all.py (9)   │
└────────────┬───────────────────────────────────────┬────────────────────┘
             │                                       │
     ┌───────▼───────┐                     ┌────────▼────────┐
     │ Market Scripts │                     │ Portfolio Mods  │
     │  BulkBlock     │                     │ portfolio_tracker│
     │  custom_sector │                     │ position_health │
     │  fii_flows     │                     │ sl_target_tracker│
     │  fii_sector    │                     │ risk_metrics    │
     │  sector_mom    │                     │ corr_clusters   │
     │  rrg_chart     │                     │ pledge_promoter │
     │  ipo_anchor    │                     │ mf_overlap      │
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
│   ├── _prices.py                # Shared price-fetch helper
│   ├── holdings_meta.csv         # User SL/Target levels per position
│   └── mf_holdings.csv           # MF holdings context
│
├── index_constituents.json       # Static sector → ticker mapping
├── fii_equity_cache.csv          # Cached FII daily flows (incremental)
├── .angel_scrip_master.json      # Cached Angel scrip master (~25 MB, weekly TTL)
│
├── requirements.txt              # Python dependencies
├── rules.md                      # Trading rules / methodology notes
├── TRADING_STRATEGY.md           # Strategy documentation
├── README.md                     # ← this file
│
├── data/                         # Data storage (india_macro CSVs)
│   └── india_macro/              # 28 indicator CSVs (append-only)
├── logs/                         # Per-run pipeline logs (auto-pruned 30d)
├── Output/                       # Breakout scanner outputs, review archives
│   └── WeekN/                    # Weekly breakout snapshots
├── .cache/                       # Misc fetch caches (NSE API, Screener.in)
├── venv/                         # Local virtualenv (gitignored)
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

---

## run_all.py — Main Orchestrator

The command-centre script that runs all market analysis scenarios in sequence.

### Usage

```bash
python3 run_all.py                           # run all 7 scenarios + send email
python3 run_all.py --no-email                # run all, skip email
python3 run_all.py --skip bulk_block rrg     # skip specific scenarios
```

### CLI Options

| Flag | Effect |
|------|--------|
| `--no-email` | Run analysis but do not send email |
| `--skip <names>` | Skip listed scenarios (space-separated) |

### Available Scenario Names (for `--skip`)

`bulk_block`, `sector_index`, `fii_flows`, `fii_sector_flows`, `sector_momentum`, `rrg`, `ipo_anchor`

### Execution Order

| # | Scenario | Module | What It Does |
|---|----------|--------|--------------|
| 1 | `bulk_block` | `BulkBlock.py` | NSE+BSE bulk/block deals, FII stake tracker, HNI holdings |
| 2 | `sector_index` | `custom_sector_index.py` | Custom equal-weighted sector indices (chart only) |
| 3 | `fii_flows` | `fii_flows.py` | Daily FII equity cash flows (chart only) |
| 4 | `fii_sector_flows` | `fii_sector_flows.py` | Fortnightly FII sector-wise flows (chart only) |
| 5 | `sector_momentum` | `sector_momentum.py` | Mansfield RS per sector (chart + "RS Ranking" sheet) |
| 6 | `rrg` | `rrg_chart.py` | Relative Rotation Graph (chart only) |
| 7 | `ipo_anchor` | `ipo_anchor_tracker.py` | IPO anchor investor matching ("IPO Anchor List" sheet) |

### Output

- `BULK_BLOCK_Deals_<timestamp>.xlsx` — Unified workbook with sheets:
  - `nse_bulk`, `nse_block` — NSE deals filtered to superstar clients
  - `Bulk Deals`, `Block Deals` — BSE deals filtered to superstar clients
  - `FII_Summary` — Classification rules + per-sheet row counts
  - `FII_New_Entry` — Stocks with new FII entry
  - `FII_1Q_Increasing`, `FII_2Q_Increasing`, `FII_3Q_Increasing`, `FII_4Q_Increasing` — FII stake streak data
  - `HNIs` — Superstar/HNI new buys + increased positions (sorted A-Z)
  - `RS Ranking` — Sector relative strength ranking (from sector_momentum)
  - `IPO Anchor List` — Recent IPOs with watchlist anchor matches
- 5 interactive Plotly HTML charts (sector_index, fii_flows, fii_sector_flows, sector_momentum, rrg)
- `market_charts.html` — Combined tabbed HTML with all 5 charts in iframe panels
- Email with Excel + charts attached

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

**Usage:**
```bash
python3 BulkBlock.py              # standalone run
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
| `--max` | 300 | Max symbols per universe |
| `--min-score` | 3.0 | Minimum breakout score to include |
| `--high-conviction` | off | Only show high-conviction setups |
| `--skip-mpd` | off | Skip MPD universe |
| `--skip-screener` | off | Skip Screener.in universe |
| `--screener-url` | built-in | Custom Screener.in query URL |
| `--symbols-csv` | — | CSV file with symbols to scan (bypass both universes) |
| `--out-tag` | — | Custom suffix for output files |
| `--no-strict` | off | Disable hard gate filtering |

**Output:**
- `breakout_watchlist.xlsx` (6 sheets: MPD Data, Screener Data, MPD Breakouts, Screener Breakouts, Combined, Parameters)
- 4 TradingView watchlist `.txt` files: `tv_breakouts_combined.txt`, `tv_common.txt`, `tv_unique_mpd.txt`, `tv_unique_screener.txt`

**Usage:**
```bash
python3 breakout_scanner_angel.py
python3 breakout_scanner_angel.py --high-conviction --min-score 5
python3 breakout_scanner_angel.py --symbols-csv my_list.csv --no-strict
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
| `--min` | 2 | Minimum % off high |
| `--max` | 21 | Maximum % off high |
| `--skip` | — | Skip specific universes (nse, nse-sme, bse-sme) |
| `--workers` | 4 | Parallel download threads |
| `--max-symbols` | — | Limit symbols per universe |
| `-o` | — | Output prefix |

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

**Output:** `rrg_chart_chart.html` (interactive scatter plot with rotation tails) + `.xlsx`

---

### ipo_anchor_tracker.py — IPO Anchor Investor Tracker

**Purpose:** Fetches the last 15 months of IPOs from NSE, computes listing returns, and cross-references anchor investor allocations from chittorgarh.com against a ~85 name watchlist of quality anchors.

**Output:**
- `.xlsx` with IPO details + anchor matches
- TradingView watchlist `.txt` for IPOs held by quality anchors
- "IPO Anchor List" sheet appended to BulkBlock Excel by run_all.py

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
| `--eod` | off | Force BhavCopy mode (end-of-day data only) |

**Output:** `fno_max_oi.xlsx` (sheets: Equity F&O, Index F&O)

**Usage:**
```bash
python3 fno_max_oi.py                    # live Angel data, weekly expiry
python3 fno_max_oi.py --expiry monthly   # monthly expiry contracts
python3 fno_max_oi.py --eod              # use NSE BhavCopy instead of live
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
| `--ogd-test <uuid>` | Inspect a data.gov.in dataset |
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

**Usage:**
```bash
python3 forensic_accounting.py TCS
python3 forensic_accounting.py RELIANCE
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

## Scheduling & Automation

### launchd (macOS)

The pipeline is scheduled via a launchd plist (`com.analysis.runall`) that triggers `scripts/run_market_analysis.sh` at **18:00 IST, Monday–Friday**.

### scripts/run_market_analysis.sh

Wrapper script responsibilities:
1. `cd` into project directory
2. Activate venv
3. Load `.env` (exports all EMAIL_*, ANGEL_* variables)
4. Run `python3 run_all.py` with output to timestamped log
5. Skip weekends defensively (re-checks day-of-week)
6. Prune logs older than 30 days

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

## Output Files & Directory Structure

```
Analysis/
├── Output/
│   ├── BULK_BLOCK_Deals_<ts>.xlsx         ← main daily report
│   ├── custom_sector_index_chart.html
│   ├── fii_flows_chart.html
│   ├── fii_sector_flows_chart.html
│   ├── sector_momentum_chart.html
│   ├── rrg_chart_chart.html
│   ├── india_macro_dashboard.html
│   ├── ipo_anchor_report.txt
│   ├── tv_breakouts_combined.txt          ← TradingView watchlists
│   ├── tv_common.txt
│   ├── tv_unique_mpd.txt
│   ├── tv_unique_screener.txt
│   ├── review_cumulative.csv
│   └── Week1-11May/                       ← weekly breakout snapshots
│       └── breakout_watchlist.xlsx
├── portfolio/
│   ├── portfolio_report.xlsx              ← unified portfolio report
│   ├── premarket_dashboard_chart.html
│   ├── holdings_meta.csv                  ← user SL/Target levels
│   └── mf_holdings.csv
├── data/
│   └── india_macro/                       ← 28 indicator CSVs
│       ├── cement_production.csv
│       ├── bank_credit_total.csv
│       └── ... (one per indicator)
├── logs/
│   └── 2026-05-XX/                        ← daily run logs
└── .angel_scrip_master.json               ← cached scrip master (25MB)
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
 └── multi_pct_down.py ──→ data_provider → angel_client

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
| **jugaad-data** (NSE scrape) | `data_provider.py` (fallback) | None |
| **yfinance** | `data_provider.py` (final fallback), indices | None |
| **NSE archives CSV** | `multi_pct_down.py` (universe seed, F&O list) | None |
| **NSE API** (large-deal snapshot) | `BulkBlock.py` | Cookie-managed session |
| **BSE JSON API** | `BulkBlock.py` (primary BSE) | None |
| **BSE HTML scrape** | `BulkBlock.py` (fallback) | None |
| **NSDL FPI fortnightly** | `fii_sector_flows.py` | None |
| **NSDL FPI monthly** | `fii_flows.py` | None |
| **NSE BhavCopy (F&O)** | `fno_max_oi.py` (EOD mode) | None |
| **Tickertape Screener API** | `fii_stake_tracker.py`, `pledge_promoter.py` | None |
| **screener.in** | `fii_stake_tracker.py` (fallback), `breakout_scanner_angel.py`, `forensic_accounting.py` | `.env`: `SCREENER_*` |
| **chittorgarh.com** | `ipo_anchor_tracker.py` (anchor tables) | None |
| **ETMoney** | `mf_overlap.py` (MF scheme lists) | None |
| **RBI / AMFI / CEA / PPAC / NSDL / CDSL** | `india_macro.py` (28 indicators) | None |
| **NSE corporate APIs** | `events_calendar.py`, `forensic_accounting.py` | None |

---

## Output File Lifecycle

| File | Producer | Lifecycle |
|---|---|---|
| `BULK_BLOCK_Deals_<timestamp>.xlsx` | `run_all.py` / `BulkBlock.py` | New each run |
| `market_charts.html` | `run_all.py` | Overwritten |
| `Output/breakout_watchlist.xlsx` | `breakout_scanner_angel.py` | Overwritten |
| `fno_max_oi.xlsx` | `fno_max_oi.py` | Overwritten |
| `india_macro_data.xlsx` | `india_macro.py` | Overwritten |
| `india_macro_dashboard.html` | `india_macro.py` | Overwritten |
| `portfolio/portfolio_report.xlsx` | `portfolio_run_all.py` | Overwritten |
| `forensic_report_<TICKER>_<ts>.pdf` | `forensic_accounting.py` | New per run |
| `logs/run_all_<timestamp>.log` | `run_market_analysis.sh` | Auto-pruned > 30 days |
| `Output/review_<ts>.xlsx` | `breakout_review.py` | New per run |
| `Output/review_cumulative.csv` | `breakout_review.py` | Append-only |
| `ipo_anchor_report.txt` | `ipo_anchor_tracker.py` | TradingView watchlist, overwritten |
| `tv_breakouts_combined.txt` | `breakout_scanner_angel.py` | TradingView watchlist, overwritten |

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

## License & Disclaimer

For personal research use only. All scraped data is sourced from public
exchange/regulator endpoints. No financial advice. Use at your own risk.
