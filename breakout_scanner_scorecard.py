"""
Stock Scorecard v0.2 — Valuation × Momentum × Stage decision engine
====================================================================
(module: breakout_scanner_scorecard.py)

A post-processing module that turns the breakout shortlist produced by
``breakout_scanner_angel.py`` into a single, ranked, opinionated table.
Every name that broke out (in the MPD universe, the Screener.in universe,
or both) is scored on three orthogonal axes and reduced to one label +
one CompositeScore so the day's watchlist can be triaged at a glance.

It is *attached* to ``breakout_scanner_angel.py`` and depends on that
module's output: after the breakout workbook is written, the scanner calls
``breakout_scanner_scorecard.run(...)`` passing the breakout rows, the
already-downloaded OHLCV candles and the Nifty 500 benchmark series.
No candles are re-fetched
(the Angel One quota is preserved); only the Tickertape screener and, for a
small Stage-2-cheap shortlist, the deep forensic engine are hit over the
network.


WHY THREE AXES
--------------
A breakout tells you a stock is *moving*. It does not tell you whether the
move is (a) starting from a cheap base or a stretched one, (b) backed by a
genuine Stage-2 advance or a dead-cat bounce inside a Stage-4 decline, or
(c) clean of governance / accounting landmines. The scorecard answers all
three before you commit capital:

        ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
        │  VALUATION   │   │  MOMENTUM    │   │    STAGE      │
        │ (how cheap)  │   │ (how strong) │   │ (where in the │
        │              │   │              │   │  cycle)       │
        └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
               │                  │                  │
               └────────┬─────────┴─────────┬────────┘
                        │   QUALITY GATE     │
                        │ (pledge / forensic)│
                        └─────────┬──────────┘
                                  │
                          ┌───────▼────────┐
                          │ CompositeScore │
                          │   + Verdict    │
                          └────────────────┘


PIPELINE
--------

  breakout_scanner_angel.run()
        │  mpd_rows + scr_rows   (≈ union of breakout candidates)
        │  ohlcv  {ticker: OHLCV df}     bench  (Nifty500 close series)
        ▼
  ┌─────────────────────────── scorecard.run() ───────────────────────────┐
  │                                                                        │
  │  1. Tickertape bulk screener  ──►  PE, PB, ROE, ROCE, D/E, growth,     │
  │     (one paginated POST)           pledge, promoter, sector, mcap      │
  │                                    for the WHOLE equity universe       │
  │                                                                        │
  │  2. VALUATION  → ValuationScore (0-100, higher = cheaper)              │
  │       sector-relative percentile of the right metric per sector       │
  │       (PB-vs-ROE for financials, PE for compounders, normalized for    │
  │        cyclicals) + PEG overlay                                        │
  │                                                                        │
  │  3. MOMENTUM   → MomentumScore (0-100)                                 │
  │       Mansfield RS · 12-1 momentum · MA-stack · 52wk proximity · vol   │
  │       (computed from the passed-in daily candles vs the benchmark)     │
  │                                                                        │
  │  4. STAGE      → Stage {1,2,3,4} + Substage (Weinstein, weekly)        │
  │       weekly resample of the same candles, 30-week MA + slope          │
  │                                                                        │
  │  5. QUALITY    → QualityFlag {OK, WATCH, FAIL}                         │
  │       pledge / promoter gate (Tickertape) + deep forensic              │
  │       (Altman Z, Piotroski F, Beneish M, CFO/PAT) auto-run on the      │
  │       Stage-2-cheap shortlist only                                     │
  │                                                                        │
  │  6. REGIME     → RegimeTag {Tailwind, Neutral, Headwind}               │
  │       sector strength overlay; modulates the position-size tag only   │
  │                                                                        │
  │  7. COMPOSITE + VERDICT  →  one row per stock, ranked                  │
  │                                                                        │
  │  Output: a "Scorecard" sheet appended to breakout_watchlist.xlsx       │
  │          + a standalone HTML table next to it                          │
  └────────────────────────────────────────────────────────────────────────┘


VALUATION BLOCK  →  ValuationScore (0-100, higher = cheaper)
------------------------------------------------------------
The "right" cheapness metric is sector-dependent (``SECTOR_VAL_METHOD``):

  PB_ROE   Financials (Banks / NBFCs / Housing fin / Gen Insurance),
           Power, Transmission — book-value businesses; P/B judged
           against ROE (a 3x P/B is cheap at 25% ROE, dear at 8%).
  PE       IT, FMCG, Pharma, Auto, Chemicals, Retail, QSR, Telecom,
           Media, Logistics, Capital Goods — earnings compounders.
  PE_NORM  Steel, Metals, Cement — cyclicals; trailing PE is a trap at
           the peak, so a normalized (PE-pctile blended with P/B-pctile)
           proxy is used until mid-cycle EPS history is available.
  SPECIAL  Life Insurance, REITs, Airlines — deferred; ValMethod=SPECIAL,
           ValuationScore left NaN.

  ValPctile  = w_h · P_hist + (1 − w_h) · P_sector          (w_h = 0.6)
  ValuationScore = 100 · (1 − ValPctile)

``P_sector`` is the percentile of the stock's metric within its sector
across the *entire* listed universe (robust, ~hundreds of peers).
``P_hist`` is the percentile of today's multiple within the stock's own
history; until ≥ 60 stored observations exist for the name, w_h collapses
to 0 and ValPctile = P_sector. Each run appends today's PE/PB to
``data/scorecard_history.csv`` so the history term switches on over time.

  PEG overlay     PEG = PE / EPSGrowth5Y ;  PEG < 1 → +10 ,  PEG > 2 → −10
  Loss-makers     PE ≤ 0 → fall back to P/B percentile ; if P/B also
                  missing → ValMethod tagged *_NA, ValuationScore = NaN
  Cyclical risk   Cyclical_Peak_Risk flagged when a PE_NORM name sits in
                  the cheapest sector decile (classic peak-earnings trap)


QUALITY GATE  →  QualityFlag {OK, WATCH, FAIL}
----------------------------------------------
  HARD FAIL  (caps Verdict at "Avoid (quality)", CompositeScore → 0)
      • Pledged % > 25      OR  Promoter % < 30
      • (non-financials)  Altman Z < 1.8   OR  CFO/PAT(3y) < 0.5
      • auditor / going-concern flag                       [reserved]
  SOFT WATCH  (−15 to QualityScore each, does not block)
      • Piotroski F < 4
      • interest coverage < 2
      • D/E > sector 80th percentile
      • high accruals                                       [forensic]
      • Beneish M > −1.78  (earnings-manipulation suspicion)
  Financials carve-out: Altman Z and CFO/PAT gates are skipped (the
  ratios are not meaningful for banks / NBFCs).

The deep forensic engine (``forensic_accounting.ForensicAnalyzer``) is
expensive, so it is auto-run ONLY on the Stage-2-cheap shortlist
(Stage == 2 and ValuationScore ≥ ``forensic_val_cutoff``). For every other
name the gate uses the cheap Tickertape signals (pledge / promoter / D/E).


MOMENTUM BLOCK  →  MomentumScore (0-100), weights {30, 25, 20, 15, 10}
---------------------------------------------------------------------
  30  Relative strength   Mansfield RS = ((P/P_idx) / SMA₅₂w(P/P_idx) − 1)·100
                          (>0 = leadership) + 50-day RS-slope / zero-cross bonus
  25  12-1 momentum       price return from t−252d to t−21d (skip last month),
                          cross-sectionally percentile-ranked within the shortlist
  20  Trend / MA-stack    Close > 50DMA > 150DMA > 200DMA  (count of 4)
  15  52-week proximity   Close / 252-day high
  10  Volume / breadth    20-day volume vs 50-day base volume


STAGE CLASSIFIER  →  Stage {1,2,3,4} + Substage   (Weinstein, weekly)
--------------------------------------------------------------------
On the weekly (W-FRI) resample of the same candles:

  C   = weekly close                MA30 = 30-week SMA of C
  s   = (MA30ₜ − MA30ₜ₋₅) / MA30ₜ₋₅          (5-week slope of the MA30)
  θ   = 1.5 % per 5 weeks                     (flat-slope dead-band)

      Stage 2 (advance)   C > MA30  and  s > +θ
      Stage 4 (decline)   C < MA30  and  s < −θ
      Stage 1 (basing)    |s| ≤ θ  and prior ∈ {4,1}  (volume contracting)
      Stage 3 (top)       |s| ≤ θ  and prior ∈ {2,3}

  Hysteresis      a stage flip is emitted only after 2 consecutive weekly
                  confirmations (kills whipsaw).
  Breakout gate   a 1→2 transition with weekly volume ≥ 2 × SMA₃₀w(volume)
                  → Breakout_Confirmed ; otherwise → Bull_Trap_Risk.
  Substage        2A if ≤ 8 weeks into Stage 2 and within +10 % of MA30,
                  else 2B.  base_count ≥ 3 → Late_Stage_Base.
  Laggard         Stage 2 with Mansfield RS ≤ 0 → "Stage 2 (laggard)".
  Needs ≈ 35+ weekly bars; new listings → Stage = NA.

         price
           │           ___________            Stage 3 (top)
           │          /           \___
           │     ____/                 \      Stage 4 (decline)
   Stage 2 │    /                        \___
 (advance) │   /   ______                     ___
           │  /   /      \   Stage 1         /     ← new Stage 2
           │_/___/        \_____(base)______/
           └──────────────────────────────────────► time


REGIME OVERLAY  →  RegimeTag {Tailwind, Neutral, Headwind}
----------------------------------------------------------
A sector-strength overlay that modulates the position-size suffix only
(↑ / · / ↓), never the core Verdict. v1 proxy: a sector is Tailwind when
the median MomentumScore of its shortlist members is high, Headwind when
low, Neutral otherwise. (Phase-3 hook: replace with RRG quadrant + 12-week
net sector FII flow + macro-liquidity composite when those feeds are wired.)


VERDICT DECISION TREE
---------------------
      Quality == FAIL ─────────────────────────► Avoid (quality)
      else Stage == 4 ─────────────────────────► Avoid (Stage 4)
      else Stage == 3 ─────────────────────────► Trim / Hold-tight
      else Stage == 1 ─► cheap & base tightening ─► Accumulate
                          else ─────────────────► Watch
      else Stage == 2 ─► 2A & RS>0 & not dear ───► Buy
                          2B ────────────────────► Hold / Add on pullback
                          laggard (RS ≤ 0) ──────► Watch
      RegimeTag appends the size suffix:  Tailwind ↑ · Neutral · · Headwind ↓

  CompositeScore = 0.35·Momentum + 0.25·Valuation + 0.25·Stage + 0.15·Quality
  (hard Quality FAIL overrides CompositeScore to 0)


OUTPUT SCHEMA  ("Scorecard" sheet, ranked by CompositeScore desc)
-----------------------------------------------------------------
  symbol · Name · Sector · Universe · Mcap · Close · ValMethod · PE · PB ·
  EVEBITDA · ValuationScore · ROE · ROCE · DE · Pledged% · Promoter% ·
  QualityFlag · AltmanZ · PiotroskiF · BeneishM · CFO_PAT · RS_M · ret_12_1 ·
  MomentumScore · Stage · Substage · Breakout_Confirmed · base_count ·
  RegimeTag · CompositeScore · Verdict


PHASING
-------
  Phase 1  Sector-percentile valuation (PE / PB) · momentum · quality flags ·
           stage classifier · verdict tree              (no new data sources)
  Phase 2  Per-name multiple-history store · Mansfield RS · breadth ·
           deep forensic on the Stage-2-cheap shortlist
  Phase 3  EV/EBITDA at scale · regime overlay · base-count · cyclical
           normalization
All three phases are implemented here; the EV/EBITDA-at-scale and the
real RRG/FII/macro regime feed are best-effort and degrade to NaN /
Neutral when the upstream data is unavailable.


USAGE
-----
  # Called automatically by breakout_scanner_angel.py (preferred path).

  # Standalone (re-reads a breakout workbook and re-fetches candles):
  python3 breakout_scanner_scorecard.py --workbook Output/Week8-29Jun/breakout_watchlist.xlsx
  python3 breakout_scanner_scorecard.py --workbook <path> --no-forensic     # skip deep forensic
"""

import os
import sys
import math
import argparse
import datetime
import warnings
from contextlib import contextmanager

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
HISTORY_CSV = os.path.join(DATA_DIR, "scorecard_history.csv")

TICKERTAPE_API = "https://api.tickertape.in/screener/query"
TICKERTAPE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Content-Type": "application/json",
}
TICKERTAPE_FIELDS = [
    "sid", "name", "ticker",
    "lastPrice", "mrktCapf",
    "ttmPe",        # TTM PE
    "pbr",          # P/B
    "roe", "roce",  # return ratios (3Y)
    "dbtEqt",       # debt / equity
    "rvng",         # 1Y revenue growth
    "epsGwth",      # 5Y EPS growth
    "incEps",       # EPS
    "promShrPled",  # pledged %
    "promHld",      # promoter holding %
    "sma200d",      # 200-day SMA
]


# ─────────────────────────────────────────────────────────────────────────────
# PARAMETERS  (single source of truth — see docstring)
# ─────────────────────────────────────────────────────────────────────────────
PARAMS = {
    # valuation
    "w_hist":            0.6,     # history weight (active once enough history)
    "hist_min_obs":      60,      # observations needed before w_hist kicks in
    "peg_cheap":         1.0,     # PEG < this → +peg_bonus
    "peg_dear":          2.0,     # PEG > this → −peg_bonus
    "peg_bonus":         10.0,
    # quality hard gates
    "pledge_fail":       25.0,    # pledged % >
    "promoter_fail":     30.0,    # promoter % <
    "altman_fail":       1.8,     # Altman Z <
    "cfo_pat_fail":      0.5,     # 3y avg CFO/PAT <
    "beneish_watch":     -1.78,   # Beneish M > → manipulation suspicion
    "piotroski_watch":   4,       # Piotroski F <
    "de_watch_pctile":   0.80,    # D/E above this sector percentile → WATCH
    "watch_penalty":     15.0,    # QualityScore points removed per soft flag
    # stage
    "theta_slope":       0.015,   # 1.5% per 5 weeks dead-band
    "stage_confirm_wk":  2,       # consecutive weekly confirmations to flip
    "breakout_vol_mult": 2.0,     # weekly vol ≥ x·SMA30w(vol) → confirmed breakout
    "stage2a_max_wk":    8,       # ≤ this many weeks → Substage 2A
    "stage2a_max_ext":   0.10,    # within +10% of MA30 → 2A
    "stage_min_weeks":   35,      # weekly bars needed to classify
    "late_base_count":   3,       # base_count ≥ → Late_Stage_Base
    # momentum weights (sum = 100)
    "w_rs":              30.0,
    "w_mom":             25.0,
    "w_trend":           20.0,
    "w_prox":            15.0,
    "w_vol":             10.0,
    # composite weights (sum = 1.0)
    "c_mom":             0.35,
    "c_val":             0.25,
    "c_stage":           0.25,
    "c_quality":         0.15,
    # forensic shortlist selection
    "forensic_val_cutoff": 55.0,  # ValuationScore ≥ → eligible for deep forensic
    "forensic_max":      40,      # hard cap on deep-forensic calls per run
    # verdict thresholds
    "buy_val_min":       45.0,    # min ValuationScore for a 2A "Buy"
    "accumulate_val_min": 60.0,   # min ValuationScore for a Stage-1 "Accumulate"
}

# Sector → valuation method.  Keys are matched as case-insensitive substrings
# against the Tickertape macro-sector name (``stock.info.sector``).
SECTOR_VAL_METHOD = {
    "financial services":        "PB_ROE",
    "bank":                      "PB_ROE",
    "nbfc":                      "PB_ROE",
    "power":                     "PB_ROE",
    "utilities":                 "PB_ROE",
    "information technology":    "PE",
    "fast moving consumer":      "PE",
    "fmcg":                      "PE",
    "healthcare":                "PE",
    "pharma":                    "PE",
    "automobile":                "PE",
    "auto":                      "PE",
    "chemical":                  "PE",
    "consumer durables":         "PE",
    "consumer services":         "PE",
    "telecommunication":         "PE",
    "media":                     "PE",
    "services":                  "PE",
    "capital goods":             "PE",
    "textiles":                  "PE",
    "metals & mining":           "PE_NORM",
    "metal":                     "PE_NORM",
    "construction materials":    "PE_NORM",
    "cement":                    "PE_NORM",
    "oil gas":                   "PE_NORM",
    "realty":                    "SPECIAL",
    "insurance":                 "SPECIAL",
}
FINANCIAL_SECTORS = ("financial services", "bank", "nbfc", "insurance")
DEFAULT_VAL_METHOD = "PE"


# ─────────────────────────────────────────────────────────────────────────────
# small helpers
# ─────────────────────────────────────────────────────────────────────────────
def _f(v):
    """Coerce to float, NaN on failure / None."""
    try:
        if v is None:
            return float("nan")
        x = float(v)
        return x
    except (TypeError, ValueError):
        return float("nan")


def _isnan(v):
    try:
        return v is None or (isinstance(v, float) and math.isnan(v))
    except Exception:
        return True


def _clip(x, lo, hi):
    if _isnan(x):
        return float("nan")
    return max(lo, min(hi, x))


def _scale(x, lo, hi):
    """Linear map x∈[lo,hi] → [0,100], clipped."""
    if _isnan(x) or hi == lo:
        return float("nan")
    return _clip((x - lo) / (hi - lo) * 100.0, 0.0, 100.0)


def _base_symbol(yahoo_ticker):
    """'RELIANCE.NS' → 'RELIANCE' ; '534109.BO' → '534109'."""
    s = str(yahoo_ticker).strip().upper()
    for suf in (".NS", ".BO"):
        if s.endswith(suf):
            return s[:-len(suf)]
    return s


@contextmanager
def _suppress_stdout():
    """Silence chatty sub-modules (forensic engine prints heavily)."""
    saved = sys.stdout
    try:
        sys.stdout = open(os.devnull, "w")
        yield
    finally:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.stdout = saved


def _val_method_for_sector(sector):
    s = (sector or "").strip().lower()
    for key, method in SECTOR_VAL_METHOD.items():
        if key in s:
            return method
    return DEFAULT_VAL_METHOD


def _is_financial(sector):
    s = (sector or "").strip().lower()
    return any(k in s for k in FINANCIAL_SECTORS)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tickertape bulk screener  (valuation + quality at scale)
# ─────────────────────────────────────────────────────────────────────────────
def fetch_tickertape_universe(verbose=True):
    """Bulk-fetch the full listed-equity universe from Tickertape.

    Returns a DataFrame indexed by NSE ticker with valuation / quality /
    sector columns, or an empty DataFrame on failure.
    """
    try:
        import requests
    except ImportError:
        if verbose:
            print("  [scorecard] 'requests' not installed — valuation skipped")
        return pd.DataFrame()

    s = requests.Session()
    s.headers.update(TICKERTAPE_HEADERS)
    match = {"mrktCapf": {"g": 0}}
    offset, rows, total = 0, [], None
    page_size = 200
    try:
        while True:
            payload = {"match": match, "sortBy": "mrktCapf", "sortOrder": -1,
                       "project": TICKERTAPE_FIELDS, "offset": offset,
                       "count": page_size}
            r = s.post(TICKERTAPE_API, json=payload, timeout=30)
            r.raise_for_status()
            data = r.json()
            if not data.get("success"):
                raise RuntimeError("Tickertape success=false")
            page = data["data"]
            results = page.get("results", [])
            if total is None:
                total = page.get("stats", {}).get("count", 0)
                if verbose:
                    print(f"  [scorecard] Tickertape universe: {total} stocks")
            rows.extend(results)
            if len(results) < page_size or len(rows) >= (total or 0):
                break
            offset += page_size
    except Exception as e:
        if verbose:
            print(f"  [scorecard] Tickertape fetch failed: {e}")
        return pd.DataFrame()

    flat = []
    for it in rows:
        stock = it.get("stock", {}) or {}
        info = stock.get("info", {}) or {}
        adv = stock.get("advancedRatios", {}) or {}
        flat.append({
            "ticker":    (info.get("ticker") or "").strip().upper(),
            "Name":      info.get("name"),
            "Sector":    info.get("sector") or "",
            "Mcap":      _f(adv.get("mrktCapf")),
            "lastPrice": _f(adv.get("lastPrice")),
            "PE":        _f(adv.get("ttmPe")),
            "PB":        _f(adv.get("pbr")),
            "ROE":       _f(adv.get("roe")),
            "ROCE":      _f(adv.get("roce")),
            "DE":        _f(adv.get("dbtEqt")),
            "RevGrowth": _f(adv.get("rvng")),
            "EPSGrowth": _f(adv.get("epsGwth")),
            "EPS":       _f(adv.get("incEps")),
            "Pledged%":  _f(adv.get("promShrPled")),
            "Promoter%": _f(adv.get("promHld")),
            "SMA200":    _f(adv.get("sma200d")),
        })
    df = pd.DataFrame(flat)
    if df.empty:
        return df
    df = df[df["ticker"] != ""].drop_duplicates(subset=["ticker"])
    return df.set_index("ticker")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Valuation block  →  ValuationScore (0-100, higher = cheaper)
# ─────────────────────────────────────────────────────────────────────────────
def _pctile_le(series, value):
    """Fraction of valid series ≤ value  (0-1).  NaN if no peers / value."""
    if _isnan(value):
        return float("nan")
    arr = series.dropna()
    arr = arr[np.isfinite(arr)]
    if len(arr) < 5:
        return float("nan")
    return float((arr <= value).mean())


def _build_sector_pools(tick_df):
    """Pre-group sector metric arrays for fast percentile lookups."""
    pools = {}
    if tick_df.empty:
        return pools
    for sector, g in tick_df.groupby("Sector"):
        pe = g["PE"][(g["PE"] > 0)]
        pb = g["PB"][(g["PB"] > 0)]
        # P/B per unit ROE (financials / book businesses): lower = cheaper
        roe = g["ROE"]
        pb_roe = (g["PB"] / (roe / 100.0)).where((roe > 0) & (g["PB"] > 0))
        pools[sector] = {
            "PE": pe, "PB": pb,
            "PB_ROE": pb_roe.dropna(),
        }
    return pools


def _load_history():
    if not os.path.exists(HISTORY_CSV):
        return pd.DataFrame(columns=["date", "symbol", "PE", "PB"])
    try:
        return pd.read_csv(HISTORY_CSV)
    except Exception:
        return pd.DataFrame(columns=["date", "symbol", "PE", "PB"])


def _hist_percentile(hist, symbol, metric, value):
    """Percentile of today's multiple within the stock's own history."""
    if hist.empty or _isnan(value):
        return float("nan")
    h = hist[hist["symbol"] == symbol][metric].dropna()
    h = h[np.isfinite(h)]
    if len(h) < PARAMS["hist_min_obs"]:
        return float("nan")
    return float((h <= value).mean())


def compute_valuation(symbol, row, sector_pools, hist):
    """Return a dict of valuation fields for one stock.

    ``row`` is the merged Tickertape record (a Series / dict-like)."""
    sector = row.get("Sector", "") if hasattr(row, "get") else row["Sector"]
    method = _val_method_for_sector(sector)
    pe = _f(row.get("PE") if hasattr(row, "get") else row["PE"])
    pb = _f(row.get("PB") if hasattr(row, "get") else row["PB"])
    eps_g = _f(row.get("EPSGrowth") if hasattr(row, "get") else row["EPSGrowth"])
    pools = sector_pools.get(sector, {})

    out = {"ValMethod": method, "ValuationScore": float("nan"),
           "Cyclical_Peak_Risk": False}

    p_sector = float("nan")
    if method == "SPECIAL":
        out["ValMethod"] = "SPECIAL"
        return out

    if method == "PB_ROE":
        roe = _f(row.get("ROE") if hasattr(row, "get") else row["ROE"])
        if not _isnan(pb) and pb > 0 and not _isnan(roe) and roe > 0:
            metric = pb / (roe / 100.0)
            p_sector = _pctile_le(pools.get("PB_ROE", pd.Series(dtype=float)),
                                  metric)
        if _isnan(p_sector) and not _isnan(pb) and pb > 0:
            p_sector = _pctile_le(pools.get("PB", pd.Series(dtype=float)), pb)
            out["ValMethod"] = "PB_ROE_PBfallback"

    elif method == "PE_NORM":
        p_pe = _pctile_le(pools.get("PE", pd.Series(dtype=float)), pe) \
            if (not _isnan(pe) and pe > 0) else float("nan")
        p_pb = _pctile_le(pools.get("PB", pd.Series(dtype=float)), pb) \
            if (not _isnan(pb) and pb > 0) else float("nan")
        vals = [v for v in (p_pe, p_pb) if not _isnan(v)]
        if vals:
            p_sector = sum(vals) / len(vals)
        # cyclical peak trap: optically cheap PE in the cheapest decile
        if not _isnan(p_pe) and p_pe <= 0.10:
            out["Cyclical_Peak_Risk"] = True

    else:  # PE
        if not _isnan(pe) and pe > 0:
            p_sector = _pctile_le(pools.get("PE", pd.Series(dtype=float)), pe)
        elif not _isnan(pb) and pb > 0:  # loss-maker → P/B fallback
            p_sector = _pctile_le(pools.get("PB", pd.Series(dtype=float)), pb)
            out["ValMethod"] = "PE_PBfallback"

    if _isnan(p_sector):
        out["ValMethod"] = out["ValMethod"] + "_NA"
        return out

    # history blend (w_hist switches on once enough observations exist)
    p_hist = _hist_percentile(hist, symbol,
                              "PE" if "PE" in out["ValMethod"] else "PB", pe)
    if _isnan(p_hist):
        val_pctile = p_sector
    else:
        w = PARAMS["w_hist"]
        val_pctile = w * p_hist + (1 - w) * p_sector

    score = 100.0 * (1.0 - val_pctile)

    # PEG overlay
    if not _isnan(pe) and pe > 0 and not _isnan(eps_g) and eps_g > 0:
        peg = pe / eps_g
        if peg < PARAMS["peg_cheap"]:
            score += PARAMS["peg_bonus"]
        elif peg > PARAMS["peg_dear"]:
            score -= PARAMS["peg_bonus"]

    out["ValuationScore"] = round(_clip(score, 0.0, 100.0), 1)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 3. Momentum block  →  MomentumScore (0-100)
# ─────────────────────────────────────────────────────────────────────────────
def _weekly(df):
    """Resample a daily OHLCV frame to weekly (W-FRI)."""
    d = df.copy()
    if not isinstance(d.index, pd.DatetimeIndex):
        try:
            d.index = pd.to_datetime(d.index)
        except Exception:
            return pd.DataFrame()
    agg = {}
    for c, how in (("Open", "first"), ("High", "max"), ("Low", "min"),
                   ("Close", "last"), ("Volume", "sum")):
        if c in d.columns:
            agg[c] = how
    if "Close" not in agg:
        return pd.DataFrame()
    wk = d.resample("W-FRI").agg(agg).dropna(subset=["Close"])
    return wk


def compute_momentum(df, bench):
    """Return raw momentum components for one stock (daily candles)."""
    out = {"RS_M": float("nan"), "ret_12_1": float("nan"),
           "rs_score": float("nan"), "trend_score": float("nan"),
           "prox_score": float("nan"), "vol_score": float("nan")}
    if df is None or df.empty or "Close" not in df.columns:
        return out
    close = df["Close"].astype(float)
    n = len(close)

    # Mansfield RS vs benchmark
    if bench is not None and len(bench) > 0:
        b = bench.copy()
        if not isinstance(b.index, pd.DatetimeIndex):
            try:
                b.index = pd.to_datetime(b.index)
            except Exception:
                b = None
        if b is not None:
            cidx = close.copy()
            if not isinstance(cidx.index, pd.DatetimeIndex):
                try:
                    cidx.index = pd.to_datetime(cidx.index)
                except Exception:
                    cidx = None
            if cidx is not None:
                joined = pd.concat([cidx.rename("p"), b.rename("idx")],
                                   axis=1).dropna()
                if len(joined) >= 60:
                    rs_line = joined["p"] / joined["idx"]
                    win = min(252, len(rs_line))
                    sma = rs_line.rolling(win).mean()
                    if not _isnan(sma.iloc[-1]) and sma.iloc[-1] != 0:
                        out["RS_M"] = round(
                            (rs_line.iloc[-1] / sma.iloc[-1] - 1) * 100, 2)
                    # 50-day slope + zero-cross bonus
                    mans = (rs_line / sma - 1) * 100
                    mans = mans.dropna()
                    slope_up = len(mans) > 50 and mans.iloc[-1] > mans.iloc[-50]
                    cross_up = (len(mans) > 20 and mans.iloc[-1] > 0
                                and mans.iloc[-20] < 0)
                    base = _scale(out["RS_M"], -10.0, 10.0)
                    if not _isnan(base):
                        if slope_up:
                            base = min(100.0, base + 10.0)
                        if cross_up:
                            base = min(100.0, base + 10.0)
                    out["rs_score"] = base

    # 12-1 momentum (t-252 → t-21)
    if n >= 252:
        p_start, p_end = close.iloc[-252], close.iloc[-21]
    elif n >= 60:
        p_start, p_end = close.iloc[0], close.iloc[-21]
    else:
        p_start = p_end = float("nan")
    if not _isnan(p_start) and p_start > 0 and not _isnan(p_end):
        out["ret_12_1"] = round((p_end / p_start - 1) * 100, 2)

    # MA-stack
    if n >= 50:
        sma50 = close.rolling(50).mean().iloc[-1]
        sma150 = close.rolling(min(150, n)).mean().iloc[-1]
        sma200 = close.rolling(min(200, n)).mean().iloc[-1]
        c = close.iloc[-1]
        cnt = sum([c > sma50, sma50 > sma150, sma150 > sma200, c > sma200])
        out["trend_score"] = cnt / 4.0 * 100.0

    # 52-week-high proximity
    if n >= 60:
        hi = close.iloc[-min(252, n):].max()
        if hi > 0:
            prox = close.iloc[-1] / hi
            out["prox_score"] = _scale(prox, 0.70, 1.0)

    # volume 20d vs 50d base
    if "Volume" in df.columns and n >= 70:
        vol = df["Volume"].astype(float)
        v20 = vol.iloc[-20:].mean()
        v50 = vol.iloc[-70:-20].mean()
        if v50 and v50 > 0:
            out["vol_score"] = _scale(v20 / v50, 0.8, 1.6)
    return out


def _finalize_momentum(per_symbol):
    """Cross-sectional percentile of ret_12_1 → mom_score, then weight."""
    rets = pd.Series({s: d["ret_12_1"] for s, d in per_symbol.items()})
    valid = rets.dropna()
    ranks = valid.rank(pct=True) * 100.0 if len(valid) else valid
    for s, d in per_symbol.items():
        mom_score = ranks.get(s, float("nan"))
        comps = {
            "rs": d.get("rs_score"), "mom": mom_score,
            "trend": d.get("trend_score"), "prox": d.get("prox_score"),
            "vol": d.get("vol_score"),
        }
        weights = {"rs": PARAMS["w_rs"], "mom": PARAMS["w_mom"],
                   "trend": PARAMS["w_trend"], "prox": PARAMS["w_prox"],
                   "vol": PARAMS["w_vol"]}
        num = den = 0.0
        for k, w in weights.items():
            v = comps[k]
            if not _isnan(v):
                num += w * v
                den += w
        d["MomentumScore"] = round(num / den, 1) if den > 0 else float("nan")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Stage classifier  (Weinstein, weekly)
# ─────────────────────────────────────────────────────────────────────────────
def classify_stage(df):
    """Return stage fields for one stock from daily candles."""
    out = {"Stage": "NA", "Substage": "", "Breakout_Confirmed": False,
           "Bull_Trap_Risk": False, "base_count": 0}
    wk = _weekly(df)
    if wk.empty or len(wk) < PARAMS["stage_min_weeks"]:
        return out
    c = wk["Close"].astype(float).reset_index(drop=True)
    vol = wk["Volume"].astype(float).reset_index(drop=True) \
        if "Volume" in wk.columns else pd.Series([np.nan] * len(c))
    ma30 = c.rolling(30).mean()
    vol30 = vol.rolling(30).mean()
    theta = PARAMS["theta_slope"]

    # raw per-week stage with prior-state disambiguation
    raw = [None] * len(c)
    prior = None
    for i in range(len(c)):
        if _isnan(ma30.iloc[i]) or i < 5 or _isnan(ma30.iloc[i - 5]) \
                or ma30.iloc[i - 5] == 0:
            raw[i] = prior
            continue
        s = (ma30.iloc[i] - ma30.iloc[i - 5]) / ma30.iloc[i - 5]
        ci = c.iloc[i]
        m = ma30.iloc[i]
        if ci > m and s > theta:
            st = 2
        elif ci < m and s < -theta:
            st = 4
        else:  # flat slope → basing (1) or topping (3) by prior
            if prior in (2, 3):
                st = 3
            elif prior in (4, 1):
                st = 1
            else:
                st = 3 if ci >= m else 1
        raw[i] = st
        prior = st

    # hysteresis: emit a flip only after N consecutive identical raw stages
    confirm = PARAMS["stage_confirm_wk"]
    emitted = [None] * len(c)
    cur = None
    run_val, run_len = None, 0
    for i in range(len(c)):
        v = raw[i]
        if v == run_val:
            run_len += 1
        else:
            run_val, run_len = v, 1
        if v is not None and run_len >= confirm:
            cur = v
        emitted[i] = cur if cur is not None else v
    stage = emitted[-1]
    if stage is None:
        return out
    out["Stage"] = int(stage)

    # base_count: number of distinct Stage-1 basing episodes in history
    bc, in_base = 0, False
    for v in emitted:
        if v == 1 and not in_base:
            bc += 1
            in_base = True
        elif v != 1:
            in_base = False
    out["base_count"] = bc

    # weeks in current stage + breakout / substage logic
    weeks_in = 1
    for i in range(len(emitted) - 2, -1, -1):
        if emitted[i] == stage:
            weeks_in += 1
        else:
            break
    last_vol = vol.iloc[-1]
    avg_vol = vol30.iloc[-1]
    vol_confirmed = (not _isnan(last_vol) and not _isnan(avg_vol)
                     and avg_vol > 0
                     and last_vol >= PARAMS["breakout_vol_mult"] * avg_vol)
    prior_stage = None
    for i in range(len(emitted) - weeks_in - 1, -1, -1):
        if emitted[i] is not None and emitted[i] != stage:
            prior_stage = emitted[i]
            break

    if stage == 2:
        ext = (c.iloc[-1] - ma30.iloc[-1]) / ma30.iloc[-1] \
            if not _isnan(ma30.iloc[-1]) and ma30.iloc[-1] else float("nan")
        if (weeks_in <= PARAMS["stage2a_max_wk"] and not _isnan(ext)
                and ext <= PARAMS["stage2a_max_ext"]):
            out["Substage"] = "2A"
        else:
            out["Substage"] = "2B"
        if out["base_count"] >= PARAMS["late_base_count"]:
            out["Substage"] += " / Late_Stage_Base"
        just_broke_out = prior_stage in (1, None) and weeks_in <= PARAMS["stage_confirm_wk"] + 1
        if vol_confirmed:
            out["Breakout_Confirmed"] = True
        elif just_broke_out:
            out["Bull_Trap_Risk"] = True
    elif stage == 1:
        # contracting volume confirms a constructive base
        out["Substage"] = "basing"
        if not _isnan(last_vol) and not _isnan(avg_vol) and last_vol < avg_vol:
            out["Substage"] = "basing (vol contracting)"
    elif stage == 3:
        out["Substage"] = "topping"
    elif stage == 4:
        out["Substage"] = "declining"
    return out


def _stage_score(stage, substage, rs_m):
    """Map Stage/Substage → 0-100 for the composite."""
    if stage == 2:
        if not _isnan(rs_m) and rs_m <= 0:
            return 55.0          # Stage 2 laggard
        if str(substage).startswith("2A"):
            return 100.0
        return 80.0              # 2B
    if stage == 1:
        return 60.0
    if stage == 3:
        return 40.0
    if stage == 4:
        return 0.0
    return 50.0                  # NA


# ─────────────────────────────────────────────────────────────────────────────
# 5. Quality gate  (Tickertape cheap signals + deep forensic)
# ─────────────────────────────────────────────────────────────────────────────
def _deep_forensic(symbol):
    """Run the forensic engine for one NSE symbol; extract core scores.

    Returns {AltmanZ, PiotroskiF, BeneishM, CFO_PAT} (NaN on failure).
    Heavy (yfinance + Screener.in + NSE); call only on the shortlist.
    """
    res = {"AltmanZ": float("nan"), "PiotroskiF": float("nan"),
           "BeneishM": float("nan"), "CFO_PAT": float("nan")}
    try:
        import forensic_accounting as fa
        with _suppress_stdout():
            data = fa.fetch_financial_data(symbol)
            analyzer = fa.ForensicAnalyzer(data)
            analyzer.run_all()
        r = analyzer.results
        res["BeneishM"] = _f(r.get("beneish", {}).get("m_score"))
        res["AltmanZ"] = _f(r.get("altman", {}).get("z_score"))
        res["PiotroskiF"] = _f(r.get("piotroski", {}).get("f_score"))
        cf_rows = r.get("cashflow", {}).get("rows", [])
        ratios = [_f(x.get("cfo_to_ni")) for x in cf_rows[:3]]
        ratios = [x for x in ratios if not _isnan(x)]
        if ratios:
            res["CFO_PAT"] = round(sum(ratios) / len(ratios), 2)
    except Exception:
        pass
    return res


def compute_quality(row, sector, de_pctile_hi, forensic=None):
    """Return QualityFlag + QualityScore for one stock.

    ``forensic`` (optional) carries the deep-forensic scores when the stock
    was on the Stage-2-cheap shortlist.
    """
    pledged = _f(row.get("Pledged%") if hasattr(row, "get") else row["Pledged%"])
    promoter = _f(row.get("Promoter%") if hasattr(row, "get") else row["Promoter%"])
    de = _f(row.get("DE") if hasattr(row, "get") else row["DE"])
    is_fin = _is_financial(sector)
    fr = forensic or {}

    hard_fail = False
    reasons = []
    # governance hard gates
    if not _isnan(pledged) and pledged > PARAMS["pledge_fail"]:
        hard_fail = True
        reasons.append("pledge>%g" % PARAMS["pledge_fail"])
    if not _isnan(promoter) and promoter < PARAMS["promoter_fail"]:
        hard_fail = True
        reasons.append("promoter<%g" % PARAMS["promoter_fail"])
    # solvency / cash hard gates (non-financials, only if forensic ran)
    if not is_fin:
        z = _f(fr.get("AltmanZ"))
        cfo_pat = _f(fr.get("CFO_PAT"))
        if not _isnan(z) and z < PARAMS["altman_fail"]:
            hard_fail = True
            reasons.append("AltmanZ<%g" % PARAMS["altman_fail"])
        if not _isnan(cfo_pat) and cfo_pat < PARAMS["cfo_pat_fail"]:
            hard_fail = True
            reasons.append("CFO/PAT<%g" % PARAMS["cfo_pat_fail"])

    # soft penalties
    score = 100.0
    pen = PARAMS["watch_penalty"]
    soft = 0
    f_sc = _f(fr.get("PiotroskiF"))
    if not _isnan(f_sc) and f_sc < PARAMS["piotroski_watch"]:
        score -= pen; soft += 1
    b_sc = _f(fr.get("BeneishM"))
    if not _isnan(b_sc) and b_sc > PARAMS["beneish_watch"]:
        score -= pen; soft += 1
    if not _isnan(de) and not _isnan(de_pctile_hi) and de > de_pctile_hi and not is_fin:
        score -= pen; soft += 1

    if hard_fail:
        return {"QualityFlag": "FAIL", "QualityScore": 0.0,
                "QualityReason": ",".join(reasons)}
    flag = "WATCH" if soft > 0 else "OK"
    return {"QualityFlag": flag, "QualityScore": round(max(0.0, score), 1),
            "QualityReason": ""}


# ─────────────────────────────────────────────────────────────────────────────
# 6. Regime overlay  (sector-strength proxy)
# ─────────────────────────────────────────────────────────────────────────────
def compute_regime(records):
    """Assign RegimeTag per row from sector median MomentumScore.

    v1 proxy (self-contained). Phase-3 hook: swap for RRG quadrant +
    12-week net sector FII flow + macro-liquidity composite.
    """
    by_sector = {}
    for r in records:
        sec = r.get("Sector") or "Unknown"
        m = r.get("MomentumScore")
        if not _isnan(m):
            by_sector.setdefault(sec, []).append(m)
    med = {s: float(np.median(v)) for s, v in by_sector.items() if v}
    for r in records:
        sec = r.get("Sector") or "Unknown"
        m = med.get(sec)
        if m is None:
            r["RegimeTag"] = "Neutral"
        elif m >= 60:
            r["RegimeTag"] = "Tailwind"
        elif m <= 40:
            r["RegimeTag"] = "Headwind"
        else:
            r["RegimeTag"] = "Neutral"


_SIZE_SUFFIX = {"Tailwind": " ↑", "Neutral": " ·", "Headwind": " ↓"}


# ─────────────────────────────────────────────────────────────────────────────
# 7. Verdict + composite
# ─────────────────────────────────────────────────────────────────────────────
def decide_verdict(r):
    """Apply the decision tree; return (Verdict, CompositeScore)."""
    stage = r.get("Stage")
    sub = str(r.get("Substage") or "")
    qf = r.get("QualityFlag")
    val = _f(r.get("ValuationScore"))
    rs_m = _f(r.get("RS_M"))
    mom = _f(r.get("MomentumScore"))
    qs = _f(r.get("QualityScore"))
    stage_sc = _stage_score(stage, sub, rs_m)

    # composite (hard quality FAIL overrides to 0)
    parts = {"mom": (mom, PARAMS["c_mom"]), "val": (val, PARAMS["c_val"]),
             "stage": (stage_sc, PARAMS["c_stage"]),
             "quality": (qs, PARAMS["c_quality"])}
    num = den = 0.0
    for v, w in parts.values():
        if not _isnan(v):
            num += v * w
            den += w
    composite = round(num / den, 1) if den > 0 else float("nan")
    if qf == "FAIL":
        composite = 0.0

    # verdict tree
    if qf == "FAIL":
        verdict = "Avoid (quality)"
    elif stage == 4:
        verdict = "Avoid (Stage 4)"
    elif stage == 3:
        verdict = "Trim / Hold-tight"
    elif stage == 1:
        tightening = "contracting" in sub
        if not _isnan(val) and val >= PARAMS["accumulate_val_min"] and tightening:
            verdict = "Accumulate"
        else:
            verdict = "Watch"
    elif stage == 2:
        laggard = not _isnan(rs_m) and rs_m <= 0
        if laggard:
            verdict = "Watch (laggard)"
        elif sub.startswith("2A") and (not _isnan(rs_m) and rs_m > 0) \
                and (not _isnan(val) and val >= PARAMS["buy_val_min"]):
            verdict = "Buy"
        elif sub.startswith("2A"):
            verdict = "Buy (rich)"
        else:
            verdict = "Hold / Add on pullback"
    else:
        verdict = "Watch (stage NA)"

    verdict += _SIZE_SUFFIX.get(r.get("RegimeTag", "Neutral"), " ·")
    return verdict, composite


# ─────────────────────────────────────────────────────────────────────────────
# HTML output
# ─────────────────────────────────────────────────────────────────────────────
_VERDICT_COLORS = {
    "Buy": "#1b8a3a", "Accumulate": "#2ea043", "Hold": "#3fb950",
    "Watch": "#9e8c1e", "Trim": "#d29922", "Avoid": "#cf222e",
}


def _verdict_color(verdict):
    v = str(verdict)
    for key, col in _VERDICT_COLORS.items():
        if v.startswith(key):
            return col
    return "#57606a"


def _write_html(df, path, asof):
    cols = [c for c in df.columns]
    rows_html = []
    for _, r in df.iterrows():
        tds = []
        for c in cols:
            v = r[c]
            if isinstance(v, float) and not _isnan(v):
                v = f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}"
            elif _isnan(v) if isinstance(v, float) else (v is None):
                v = ""
            style = ""
            if c == "Verdict":
                style = f' style="color:#fff;background:{_verdict_color(r[c])};' \
                        'font-weight:600;border-radius:4px;padding:2px 6px"'
            elif c == "QualityFlag":
                qc = {"OK": "#2ea043", "WATCH": "#d29922",
                      "FAIL": "#cf222e"}.get(str(r[c]), "#57606a")
                style = f' style="color:{qc};font-weight:600"'
            tds.append(f"<td{style}>{v}</td>")
        rows_html.append("<tr>" + "".join(tds) + "</tr>")
    head = "".join(f"<th>{c}</th>" for c in cols)
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Stock Scorecard — {asof}</title>
<style>
 body{{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
   margin:18px;background:#0d1117;color:#e6edf3}}
 h1{{font-size:18px;margin:0 0 4px}}
 .sub{{color:#8b949e;font-size:12px;margin-bottom:14px}}
 table{{border-collapse:collapse;font-size:12px;width:100%}}
 th,td{{border:1px solid #30363d;padding:4px 8px;text-align:right;white-space:nowrap}}
 th{{background:#161b22;position:sticky;top:0;cursor:pointer;text-align:right}}
 td:first-child,th:first-child,td:nth-child(2),th:nth-child(2){{text-align:left}}
 tr:nth-child(even){{background:#11161d}}
 tr:hover{{background:#1c2330}}
</style></head><body>
<h1>Stock Scorecard — Valuation × Momentum × Stage</h1>
<div class="sub">As of {asof} · {len(df)} breakout names · ranked by CompositeScore</div>
<table id="t"><thead><tr>{head}</tr></thead><tbody>
{''.join(rows_html)}
</tbody></table>
<script>
const t=document.getElementById('t');
t.querySelectorAll('th').forEach((th,i)=>th.addEventListener('click',()=>{{
 const tb=t.tBodies[0];const rows=[...tb.rows];
 const num=v=>{{const n=parseFloat(String(v).replace(/[,%]/g,''));return isNaN(n)?null:n;}};
 const asc=th.dataset.asc=th.dataset.asc==='1'?'0':'1';
 rows.sort((a,b)=>{{let x=a.cells[i].innerText,y=b.cells[i].innerText;
   const nx=num(x),ny=num(y);let r;
   if(nx!==null&&ny!==null)r=nx-ny;else r=x.localeCompare(y);
   return asc==='1'?r:-r;}});
 rows.forEach(r=>tb.appendChild(r));
}}));
</script>
</body></html>"""
    with open(path, "w") as f:
        f.write(html)


# ─────────────────────────────────────────────────────────────────────────────
# History store
# ─────────────────────────────────────────────────────────────────────────────
def _append_history(records, asof):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        new = pd.DataFrame([
            {"date": asof, "symbol": r["symbol"], "PE": r.get("PE"),
             "PB": r.get("PB")}
            for r in records
        ])
        if os.path.exists(HISTORY_CSV):
            new.to_csv(HISTORY_CSV, mode="a", header=False, index=False)
        else:
            new.to_csv(HISTORY_CSV, index=False)
    except Exception as e:
        print(f"  [scorecard] history append failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────
OUTPUT_COLS = [
    "symbol", "Name", "Sector", "Universe", "Mcap", "Close", "ValMethod",
    "PE", "PB", "EVEBITDA", "ValuationScore", "ROE", "ROCE", "DE",
    "Pledged%", "Promoter%", "QualityFlag", "AltmanZ", "PiotroskiF",
    "BeneishM", "CFO_PAT", "RS_M", "ret_12_1", "MomentumScore", "Stage",
    "Substage", "Breakout_Confirmed", "base_count", "RegimeTag",
    "CompositeScore", "Verdict",
]


def run(excel_path, mpd_rows=None, scr_rows=None, ohlcv=None, bench=None,
        out_tag="", deep_forensic=True, verbose=True):
    """Build the scorecard and append it to ``excel_path``.

    Parameters
    ----------
    excel_path : path to the breakout workbook to append the Scorecard sheet to.
    mpd_rows, scr_rows : breakout row dict-lists from breakout_scanner_angel.
    ohlcv : {yahoo_ticker: daily OHLCV DataFrame} already fetched by the scanner.
    bench : Nifty 500 close Series (benchmark for Mansfield RS).
    deep_forensic : run the forensic engine on the Stage-2-cheap shortlist.
    """
    asof = datetime.date.today().strftime("%d-%b-%Y")
    mpd_rows = mpd_rows or []
    scr_rows = scr_rows or []
    ohlcv = ohlcv or {}

    # universe (union, tag membership)
    mpd_syms = {str(r["symbol"]).strip() for r in mpd_rows if r.get("symbol")}
    scr_syms = {str(r["symbol"]).strip() for r in scr_rows if r.get("symbol")}
    all_syms = sorted(mpd_syms | scr_syms)
    if not all_syms:
        if verbose:
            print("  [scorecard] no breakout symbols — nothing to score")
        return None

    if verbose:
        print("\n" + "=" * 70)
        print(f"  STOCK SCORECARD — {asof}  ({len(all_syms)} breakout names)")
        print("=" * 70)

    # Tickertape valuation/quality universe
    tick = fetch_tickertape_universe(verbose=verbose)
    sector_pools = _build_sector_pools(tick)
    hist = _load_history()
    # sector D/E 80th percentile (for the soft WATCH gate)
    de_hi = {}
    if not tick.empty:
        for sec, g in tick.groupby("Sector"):
            de = g["DE"].dropna()
            de = de[np.isfinite(de)]
            de_hi[sec] = float(np.percentile(de, PARAMS["de_watch_pctile"] * 100)) \
                if len(de) >= 5 else float("nan")

    # ── pass 1: per-symbol valuation + momentum + stage ──
    per_mom = {}
    records = []
    for sym in all_syms:
        base = _base_symbol(sym)
        trow = tick.loc[base] if (not tick.empty and base in tick.index) else None
        sector = trow["Sector"] if trow is not None else ""

        df = ohlcv.get(sym)
        mom = compute_momentum(df, bench)
        per_mom[sym] = mom
        stage = classify_stage(df) if df is not None else {
            "Stage": "NA", "Substage": "", "Breakout_Confirmed": False,
            "Bull_Trap_Risk": False, "base_count": 0}

        if trow is not None:
            val = compute_valuation(base, trow, sector_pools, hist)
            rec = {
                "symbol": sym,
                "Name": trow.get("Name"),
                "Sector": sector,
                "Mcap": trow.get("Mcap"),
                "PE": trow.get("PE"), "PB": trow.get("PB"),
                "ROE": trow.get("ROE"), "ROCE": trow.get("ROCE"),
                "DE": trow.get("DE"),
                "Pledged%": trow.get("Pledged%"),
                "Promoter%": trow.get("Promoter%"),
                "EVEBITDA": float("nan"),  # Phase-3 best-effort (not in feed)
            }
            rec.update(val)
        else:
            rec = {"symbol": sym, "Name": "", "Sector": "", "Mcap": float("nan"),
                   "PE": float("nan"), "PB": float("nan"), "ROE": float("nan"),
                   "ROCE": float("nan"), "DE": float("nan"),
                   "Pledged%": float("nan"), "Promoter%": float("nan"),
                   "EVEBITDA": float("nan"), "ValMethod": "NA",
                   "ValuationScore": float("nan"), "Cyclical_Peak_Risk": False}

        rec["Universe"] = ("both" if sym in mpd_syms and sym in scr_syms
                           else "mpd" if sym in mpd_syms else "screener")
        # close: prefer candle, fall back to Tickertape lastPrice
        if df is not None and not df.empty:
            rec["Close"] = round(float(df["Close"].iloc[-1]), 2)
        elif trow is not None:
            rec["Close"] = trow.get("lastPrice")
        else:
            rec["Close"] = float("nan")
        rec["RS_M"] = mom["RS_M"]
        rec["ret_12_1"] = mom["ret_12_1"]
        rec.update({k: stage[k] for k in
                    ("Stage", "Substage", "Breakout_Confirmed", "base_count")})
        rec["AltmanZ"] = float("nan")
        rec["PiotroskiF"] = float("nan")
        rec["BeneishM"] = float("nan")
        rec["CFO_PAT"] = float("nan")
        records.append(rec)

    # finalize momentum (cross-sectional 12-1 percentile) and attach
    _finalize_momentum(per_mom)
    for rec in records:
        rec["MomentumScore"] = per_mom[rec["symbol"]]["MomentumScore"]

    # ── deep forensic on the Stage-2-cheap shortlist ──
    if deep_forensic:
        shortlist = [r for r in records
                     if r.get("Stage") == 2
                     and not _isnan(_f(r.get("ValuationScore")))
                     and _f(r["ValuationScore"]) >= PARAMS["forensic_val_cutoff"]]
        shortlist = shortlist[:PARAMS["forensic_max"]]
        if verbose and shortlist:
            print(f"  [scorecard] deep forensic on {len(shortlist)} "
                  "Stage-2-cheap names …")
        for r in shortlist:
            fr = _deep_forensic(_base_symbol(r["symbol"]))
            r.update(fr)

    # ── quality gate ──
    for r in records:
        fr = {k: r.get(k) for k in ("AltmanZ", "PiotroskiF", "BeneishM", "CFO_PAT")}
        q = compute_quality(r, r.get("Sector", ""),
                            de_hi.get(r.get("Sector"), float("nan")), forensic=fr)
        r.update(q)

    # ── regime overlay ──
    compute_regime(records)

    # ── verdict + composite ──
    for r in records:
        verdict, composite = decide_verdict(r)
        r["Verdict"] = verdict
        r["CompositeScore"] = composite

    # ── assemble, rank, write ──
    df_out = pd.DataFrame(records)
    for c in OUTPUT_COLS:
        if c not in df_out.columns:
            df_out[c] = float("nan")
    df_out = df_out[OUTPUT_COLS].sort_values(
        "CompositeScore", ascending=False, na_position="last").reset_index(drop=True)

    # append Scorecard sheet to the breakout workbook
    try:
        with pd.ExcelWriter(excel_path, engine="openpyxl", mode="a",
                            if_sheet_exists="replace") as w:
            df_out.to_excel(w, sheet_name="Scorecard", index=False)
        if verbose:
            print(f"  [scorecard] Scorecard sheet → {excel_path}")
    except Exception as e:
        print(f"  [scorecard] failed to append sheet: {e}")

    # HTML next to the workbook
    try:
        html_path = os.path.splitext(excel_path)[0] + "_scorecard.html"
        _write_html(df_out, html_path, asof)
        if verbose:
            print(f"  [scorecard] HTML → {html_path}")
    except Exception as e:
        print(f"  [scorecard] failed to write HTML: {e}")

    _append_history(records, datetime.date.today().strftime("%Y-%m-%d"))

    if verbose:
        n_buy = (df_out["Verdict"].str.startswith("Buy")).sum()
        n_acc = (df_out["Verdict"].str.startswith("Accumulate")).sum()
        n_avoid = (df_out["Verdict"].str.startswith("Avoid")).sum()
        print(f"  [scorecard] Buy={n_buy}  Accumulate={n_acc}  Avoid={n_avoid}")
        show = ["symbol", "Sector", "ValuationScore", "MomentumScore",
                "Stage", "Substage", "QualityFlag", "CompositeScore", "Verdict"]
        top = df_out[show].head(15)
        print("\n  Top 15 by CompositeScore:")
        print(top.to_string(index=False))
    return df_out


# ─────────────────────────────────────────────────────────────────────────────
# Standalone entry point  (re-reads a workbook, re-fetches candles)
# ─────────────────────────────────────────────────────────────────────────────
def _rows_from_workbook(path):
    """Reconstruct mpd_rows / scr_rows from a breakout workbook."""
    xl = pd.ExcelFile(path)
    mpd_rows, scr_rows = [], []
    if "MPD Breakouts" in xl.sheet_names:
        d = xl.parse("MPD Breakouts")
        if "symbol" in d.columns:
            mpd_rows = d.to_dict("records")
    if "Screener Breakouts" in xl.sheet_names:
        d = xl.parse("Screener Breakouts")
        if "symbol" in d.columns:
            scr_rows = d.to_dict("records")
    return mpd_rows, scr_rows


def main():
    p = argparse.ArgumentParser(description="Stock Scorecard v0.2")
    p.add_argument("--workbook", required=True,
                   help="path to a breakout_watchlist.xlsx to score")
    p.add_argument("--no-forensic", action="store_true",
                   help="skip the deep forensic value-trap filter")
    p.add_argument("--lookback", type=int, default=400,
                   help="calendar days of candle history to fetch")
    args = p.parse_args()

    if not os.path.exists(args.workbook):
        raise SystemExit(f"workbook not found: {args.workbook}")

    mpd_rows, scr_rows = _rows_from_workbook(args.workbook)
    syms = sorted({str(r["symbol"]).strip()
                   for r in (mpd_rows + scr_rows) if r.get("symbol")})
    if not syms:
        raise SystemExit("no breakout symbols found in workbook")

    # re-fetch candles + benchmark via the scanner's downloaders
    print(f"  [scorecard] standalone: fetching candles for {len(syms)} names …")
    import breakout_scanner_angel as bsa
    ohlcv = bsa.fetch_ohlcv(syms, args.lookback)
    bench = bsa.fetch_benchmark(args.lookback)

    run(args.workbook, mpd_rows=mpd_rows, scr_rows=scr_rows,
        ohlcv=ohlcv, bench=bench, deep_forensic=not args.no_forensic)


if __name__ == "__main__":
    main()
