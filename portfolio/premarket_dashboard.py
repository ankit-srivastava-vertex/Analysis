"""
premarket_dashboard.py — pre-open snapshot for a positional Indian trader
==========================================================================

SUMMARY
-------
A single, tight one-page view delivered before 9:15 IST. Captures the
overnight & global cues that move Indian equities at the open:

  * Global indices  : S&P 500, Nasdaq, Dow, Nikkei 225, Hang Seng, FTSE
  * India          : Nifty 50, Bank Nifty, India VIX (latest close)
  * GIFT Nifty     : SGX-replacement Nifty futures on NSE-IX (live cue)
  * Currencies     : USDINR, DXY (US dollar index)
  * Commodities    : Brent crude, Gold (USD), Copper
  * Yields         : US 10-year Treasury
  * Breadth        : % of NIFTY 500 above 20/50/200-EMA, new 52w highs
                     vs lows (full NIFTY 500 universe — official NSE list)

WORKFLOW
--------
1. Define the asset list with stable yfinance tickers (long-term reliable
   public symbols).
2. Pull last ~5 sessions for each via data_provider.download (Angel for
   Indian symbols, yfinance for global).
3. Compute Last, Prev Close, Day %, 5-Day %.
4. Compute breadth on the official NIFTY 500 list (NSE
   ind_nifty500list.csv, cached weekly). For each constituent pull ~14
   months of OHLCV via data_provider, then aggregate:
     * % above 20-EMA, % above 50-EMA, % above 200-EMA
     * new 52w highs vs lows, hi/lo ratio
     * advance/decline (today's close > prev close)
   Append today's row to a persistent CSV history
   (portfolio/.cache/breadth_history.csv) for trend tracking. Trailing
   sessions where <95% of the universe has reported are dropped first, so
   an intraday run never publishes a half-reported day as the latest
   reading (see `_drop_partial_sessions`).
5. Render an interactive Plotly HTML chart
   (portfolio/premarket_dashboard_chart.html) with 4 panels:
     a. % above 50-EMA & 200-EMA (line)
     b. New 52w highs vs lows (bar)
     c. Advance / decline ratio (bar)
     d. Hi-lo ratio (line, log)
6. Compose four sheets (Markets, Currencies & Commodities, Breadth,
   Breadth History) and a Notes sheet documenting symbols + sources.

SECTOR BREADTH (public API)
---------------------------
`compute_sector_breadth()` produces a *per-sector* breadth time series over
two taxonomies at once:
  * the 20 official NSE ``Industry`` buckets from the constituent CSV, and
  * the 41 curated fine-grained sectors in ``index_constituents.json``
    (PSBs, Transformers, SpecialityChemicals …), labelled with a ``C:``
    prefix to match `rrg_chart`'s convention.
Because every metric in `_breadth_from_px()` is a row-wise reduction over the
close matrix, slicing that matrix by sector yields sector breadth with no
change to the maths. The custom sectors add ~450 symbols from outside the
NIFTY 500, so the download universe is roughly double this module's own
breadth run; pass ``include_custom=False`` to get the old NSE-only behaviour.
Consumed by `sector_breadth.py` (the "Breadth" tab of market_charts.html);
this module's own `run()` is unaffected.

DATA SOURCES
------------
- Global / FX / commodity / yield quotes : Yahoo Finance via
  data_provider.download fallback chain.
    ^GSPC, ^IXIC, ^DJI, ^N225, ^HSI, ^FTSE,
    ^NSEI, ^NSEBANK, ^INDIAVIX,
    INR=X, DX-Y.NYB,
    BZ=F (Brent), GC=F (Gold), HG=F (Copper),
    ^TNX (US 10Y yield * 10).
- GIFT Nifty (informational) : Yahoo symbol 'GIFTNIFTY' is unstable; we
  approximate via ^NSEI close + USDINR change. Skipped if not resolved.
- Indian equities : data_provider (Angel One primary).
- NIFTY 500 list  : https://archives.nseindia.com/content/indices/ind_nifty500list.csv
                    (cached 7d in portfolio/.cache/)

OUTPUT
------
Sheets returned by run():
  Pre-Market Markets         — global + India quotes table
  FX & Commodities           — INR, DXY, Brent, Gold, Copper, US 10Y
  Breadth (NIFTY500)         — today's breadth metrics
  Breadth History            — last ~120 sessions of breadth metrics
  Pre-Market Notes           — symbol map, source URLs, refresh cadence

Chart returned via run()['chart']:
  portfolio/premarket_dashboard_chart.html  — 4-panel Plotly chart

USAGE
-----
    from portfolio.premarket_dashboard import run
    result = run()
    # CLI:
    python3 -m portfolio.premarket_dashboard

DEPENDENCIES
------------
pandas, openpyxl, data_provider (parent package)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import data_provider  # noqa: E402

GLOBAL_INDICES = [
    ("S&P 500",     "^GSPC"),
    ("Nasdaq Comp", "^IXIC"),
    ("Dow Jones",   "^DJI"),
    ("Nikkei 225",  "^N225"),
    ("Hang Seng",   "^HSI"),
    ("FTSE 100",    "^FTSE"),
]
INDIA_INDICES = [
    ("Nifty 50",   "^NSEI"),
    ("Bank Nifty", "^NSEBANK"),
    ("India VIX",  "^INDIAVIX"),
]
FX_COMM = [
    ("USD/INR",    "INR=X",     "FX"),
    ("DXY",        "DX-Y.NYB",  "FX"),
    ("Brent",      "BZ=F",      "Commodity"),
    ("Gold",       "GC=F",      "Commodity"),
    ("Copper",     "HG=F",      "Commodity"),
    ("US 10Y",     "^TNX",      "Yield"),
]


# ─────────────────────────── helpers ────────────────────────────────────────

def _quote(ticker: str, days: int = 10) -> Optional[pd.DataFrame]:
    start = (dt.date.today() - dt.timedelta(days=days * 3)).isoformat()
    try:
        df = data_provider.download(ticker, start=start,
                                    end=dt.date.today().isoformat(),
                                    interval="1d", progress=False)
    except Exception:
        return None
    if df is None or df.empty or "Close" not in df.columns:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def _row_for(name: str, ticker: str) -> dict:
    df = _quote(ticker)
    if df is None or "Close" not in df.columns:
        return {"Name": name, "Ticker": ticker, "Last": None,
                "Prev Close": None, "Day %": None, "5-Day %": None}
    close = df["Close"].dropna()
    if len(close) < 1:
        return {"Name": name, "Ticker": ticker, "Last": None,
                "Prev Close": None, "Day %": None, "5-Day %": None}
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) >= 2 else None
    five = float(close.iloc[-6]) if len(close) >= 6 else None
    return {
        "Name": name, "Ticker": ticker,
        "Last": round(last, 2),
        "Prev Close": round(prev, 2) if prev else None,
        "Day %": round((last / prev - 1) * 100, 2) if prev else None,
        "5-Day %": round((last / five - 1) * 100, 2) if five else None,
    }


def _build_markets() -> pd.DataFrame:
    rows = []
    rows.append({"Name": "── GLOBAL ──", "Ticker": "", "Last": None,
                 "Prev Close": None, "Day %": None, "5-Day %": None})
    for n, t in GLOBAL_INDICES:
        rows.append(_row_for(n, t))
    rows.append({"Name": "── INDIA ──", "Ticker": "", "Last": None,
                 "Prev Close": None, "Day %": None, "5-Day %": None})
    for n, t in INDIA_INDICES:
        rows.append(_row_for(n, t))
    return pd.DataFrame(rows)


def _build_fx_comm() -> pd.DataFrame:
    rows = []
    for n, t, kind in FX_COMM:
        r = _row_for(n, t)
        r["Type"] = kind
        rows.append(r)
    df = pd.DataFrame(rows)
    cols = ["Type", "Name", "Ticker", "Last", "Prev Close", "Day %", "5-Day %"]
    return df[[c for c in cols if c in df.columns]]


# ─────────────────────────── breadth ────────────────────────────────────────

import io  # noqa: E402
import time  # noqa: E402

import requests  # noqa: E402

NIFTY500_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
CACHE_DIR = PORTFOLIO_DIR / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
NIFTY500_CACHE = CACHE_DIR / "ind_nifty500list.csv"
NIFTY500_TTL = 7 * 86400
BREADTH_HISTORY = CACHE_DIR / "breadth_history.csv"


def _load_nifty500_frame() -> pd.DataFrame:
    """Return the raw NIFTY 500 constituent table (cached 7d).

    The published CSV carries an ``Industry`` column alongside ``Symbol``;
    that is the sector map used by :func:`compute_sector_breadth`.
    """
    if NIFTY500_CACHE.exists() and (time.time() - NIFTY500_CACHE.stat().st_mtime) < NIFTY500_TTL:
        df = pd.read_csv(NIFTY500_CACHE)
    else:
        try:
            r = requests.get(NIFTY500_URL,
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            r.raise_for_status()
            NIFTY500_CACHE.write_bytes(r.content)
            df = pd.read_csv(io.BytesIO(r.content))
        except Exception as e:
            if NIFTY500_CACHE.exists():
                print(f"  [premarket] NIFTY 500 fetch failed ({e}); using cache")
                df = pd.read_csv(NIFTY500_CACHE)
            else:
                print(f"  [premarket] NIFTY 500 fetch failed ({e})")
                return pd.DataFrame()
    df.columns = [c.strip().upper() for c in df.columns]
    return df


def _fetch_nifty500() -> list:
    """Fetch the official NIFTY 500 constituent symbols (cached 7d)."""
    df = _load_nifty500_frame()
    if df.empty:
        return []
    sym_col = next((c for c in df.columns if "SYMBOL" in c), None)
    if not sym_col:
        return []
    return [s.strip().upper() for s in df[sym_col].dropna().astype(str)]


def _fetch_nifty500_sectors(min_stocks: int = 1) -> dict:
    """Map official NSE industry name -> list of NIFTY 500 symbols."""
    df = _load_nifty500_frame()
    if df.empty:
        return {}
    sym_col = next((c for c in df.columns if "SYMBOL" in c), None)
    ind_col = next((c for c in df.columns if "INDUSTRY" in c or "SECTOR" in c), None)
    if not sym_col or not ind_col:
        return {}
    sub = df[[sym_col, ind_col]].dropna()
    out = {}
    for industry, grp in sub.groupby(ind_col):
        syms = sorted({s.strip().upper() for s in grp[sym_col].astype(str)})
        if len(syms) >= min_stocks:
            out[str(industry).strip()] = syms
    return dict(sorted(out.items(), key=lambda kv: -len(kv[1])))


CUSTOM_SECTORS_FILE = PROJECT_ROOT / "index_constituents.json"
CUSTOM_SECTOR_PREFIX = "C: "


def _fetch_custom_sectors(min_stocks: int = 1) -> dict:
    """Map ``"C: <name>"`` -> symbols from the curated index_constituents.json.

    These are the hand-built fine-grained sectors (PSBs, Transformers,
    SpecialityChemicals …) that the 20-bucket NSE ``Industry`` column cannot
    express. The ``C:`` prefix matches ``rrg_chart``'s convention so the same
    sector reads identically across reports.
    """
    try:
        raw = json.loads(CUSTOM_SECTORS_FILE.read_text())
    except Exception as e:
        print(f"  [breadth] custom sector file unusable ({e}); NSE only")
        return {}
    out = {}
    for name, body in raw.items():
        syms = sorted({str(s).strip().upper()
                       for s in (body or {}).get("constituents", []) if s})
        if len(syms) >= min_stocks:
            out[CUSTOM_SECTOR_PREFIX + str(name).strip()] = syms
    return dict(sorted(out.items(), key=lambda kv: -len(kv[1])))


def _fetch_all_sectors(min_stocks: int = 1, include_custom: bool = True) -> dict:
    """NSE ``Industry`` buckets plus the curated custom sectors.

    The two taxonomies deliberately overlap: a stock can sit in one NSE macro
    bucket *and* one custom sector, so callers needing a symbol -> sector
    lookup must treat it as one-to-many. Within each taxonomy the mapping is
    still one-to-one.
    """
    out = dict(_fetch_nifty500_sectors(min_stocks=min_stocks))
    if include_custom:
        out.update(_fetch_custom_sectors(min_stocks=min_stocks))
    return out


def _download_closes(symbols: list, start: str, end: str,
                     verbose: bool = True, label: str = "breadth") -> dict:
    """Pull daily Close series for `symbols`, skipping anything too short."""
    closes = {}
    for i, sym in enumerate(symbols, 1):
        if verbose and i % 50 == 0:
            print(f"    {label} {i}/{len(symbols)}")
        try:
            df = data_provider.download(sym, start=start, end=end,
                                        interval="1d", progress=False)
        except Exception:
            continue
        if df is None or df.empty or "Close" not in df.columns:
            continue
        c = df["Close"].dropna()
        if len(c) < 50:
            continue
        c.index = pd.to_datetime(c.index).normalize()
        closes[sym] = c[~c.index.duplicated(keep="last")]
    return closes


def _px_from_closes(closes: dict) -> pd.DataFrame:
    """Assemble a numeric close matrix and drop non-trading rows."""
    px = pd.DataFrame(closes).sort_index()
    # Ensure all columns are numeric float (guards against object-dtype from
    # mixed data sources in pandas 2.x where rolling silently drops non-numeric).
    px = px.apply(pd.to_numeric, errors="coerce")
    # Drop rows where fewer than half the stocks have data — these are
    # weekends/holidays that crept in from yfinance or API quirks.  Keeping
    # them inflates the index and breaks the rolling window (50-row window
    # may span only ~35 actual trading days, failing min_periods=50).
    return px[px.notna().sum(axis=1) > len(closes) * 0.5]


def _drop_partial_sessions(px: pd.DataFrame,
                           min_last_coverage: float = 0.95,
                           verbose: bool = True,
                           label: str = "breadth",
                           max_drop: int = 5) -> pd.DataFrame:
    """Drop trailing rows where too few stocks have reported a bar.

    Run intraday, the newest bar is only partially populated, which shrinks
    every percentage's denominator — a "50%% above 50-EMA" reading taken off
    6 of 11 reporting stocks is noise, not breadth.

    Capped at `max_drop` rows. A partial session is a one- or two-row problem;
    coverage that stays low for longer means the universe itself is chronically
    sparse (illiquid microcaps that simply do not print every day), and eating
    the whole frame over that would be far worse than reporting it.
    """
    if px.empty:
        return px
    coverage = px.notna().sum(axis=1) / float(px.shape[1])
    dropped = 0
    while len(px) and coverage.iloc[-1] < min_last_coverage:
        if dropped >= max_drop:
            if verbose:
                print("  [%s] Coverage still %.0f%% after dropping %d session(s)"
                      " — treating as chronic sparsity, keeping the rest"
                      % (label, coverage.iloc[-1] * 100, dropped))
            break
        if verbose:
            print("  [%s] Dropping partial session %s (%.0f%% of stocks "
                  "reporting)" % (label, px.index[-1].date(),
                                  coverage.iloc[-1] * 100))
        px = px.iloc[:-1]
        coverage = coverage.iloc[:-1]
        dropped += 1
    return px


def _breadth_from_px(px: pd.DataFrame, universe_size: int) -> pd.DataFrame:
    """Vectorised daily breadth metrics across the columns of `px`.

    Every metric is a row-wise reduction, so passing a column subset yields
    the breadth of that subset — this is what makes per-sector breadth work.

    Each metric carries its own eligible-stock count (`*Base` columns) so
    callers percentage-ise against the stocks that could actually qualify that
    day rather than against the roster. A stock is eligible only if it printed
    a bar *and* has enough history for that metric's window: a stock with no
    bar today still has a non-NaN moving average from yesterday, so counting it
    would inflate the denominator and understate every percentage.

    The moving averages are **exponential**, not simple. An EMA weights recent
    closes more heavily, so a stock crosses it sooner after a turn and the
    breadth reading leads its SMA equivalent by a few sessions. The trade-off
    is that these numbers no longer line up with published "% above 200-DMA"
    statistics, which are conventionally simple averages.

    ``adjust=False`` is the recursive form charting platforms draw. Callers
    supply well over a year of warm-up history before the display window, so
    the 200-period EMA is fully converged by the first plotted bar.

    New highs/lows are strict: `hi252` is a rolling max that includes today,
    so ``px >= hi252`` means today's close IS the highest close of the
    trailing 252 sessions. No tolerance band — "within 0.1% of the high" is
    not a new high, and treating it as one inflates the count.

    New highs and the advance/decline counts use no moving average at all, so
    the SMA-to-EMA switch does not touch them.
    """
    import numpy as np
    valid = px.notna()
    ema20  = px.ewm(span=20,  adjust=False, min_periods=20).mean()
    ema50  = px.ewm(span=50,  adjust=False, min_periods=50).mean()
    ema200 = px.ewm(span=200, adjust=False, min_periods=200).mean()
    hi252  = px.rolling(252, min_periods=200).max()
    lo252  = px.rolling(252, min_periods=200).min()
    prev   = px.shift(1)

    ema20_ok  = ema20.notna()  & valid
    ema50_ok  = ema50.notna()  & valid
    ema200_ok = ema200.notna() & valid
    hilo_ok   = hi252.notna()  & valid
    advdec_ok = prev.notna()   & valid

    ema20_n  = ema20_ok.sum(axis=1)
    ema50_n  = ema50_ok.sum(axis=1)
    ema200_n = ema200_ok.sum(axis=1)
    above20_pct  = (((px > ema20) & ema20_ok).sum(axis=1).astype(float)
                    / ema20_n.replace(0, np.nan).astype(float) * 100)
    above50_pct  = (((px > ema50) & ema50_ok).sum(axis=1).astype(float)
                    / ema50_n.replace(0, np.nan).astype(float) * 100)
    above200_pct = (((px > ema200) & ema200_ok).sum(axis=1).astype(float)
                    / ema200_n.replace(0, np.nan).astype(float) * 100)
    new_highs = ((px >= hi252) & hilo_ok).sum(axis=1)
    new_lows  = ((px <= lo252) & hilo_ok).sum(axis=1)
    advances  = ((px > prev) & advdec_ok).sum(axis=1)
    declines  = ((px < prev) & advdec_ok).sum(axis=1)
    scanned   = valid.sum(axis=1)

    out = pd.DataFrame({
        "Date":        px.index.strftime("%Y-%m-%d"),
        "Universe":    universe_size,
        "Scanned":     scanned.values,
        "Above20EMA%": above20_pct.round(2).values,
        "Above50EMA%": above50_pct.round(2).values,
        "Above200EMA%":above200_pct.round(2).values,
        "New52wHighs": new_highs.values,
        "New52wLows":  new_lows.values,
        "Advances":    advances.values,
        "Declines":    declines.values,
        "Above20Base":  ema20_n.values,
        "Above50Base":  ema50_n.values,
        "Above200Base": ema200_n.values,
        "HiLoBase":     hilo_ok.sum(axis=1).values,
        "AdvDecBase":   advdec_ok.sum(axis=1).values,
    })
    out["HiLoRatio"]   = (out["New52wHighs"].astype(float) / out["New52wLows"].replace(0, np.nan).astype(float)).round(2)
    out["AdvDecRatio"] = (out["Advances"].astype(float)   / out["Declines"].replace(0, np.nan).astype(float)).round(2)
    return out


def _compute_breadth_history(verbose: bool = True,
                             lookback_days: int = 180,
                             min_last_coverage: float = 0.95) -> pd.DataFrame:
    """Scan full NIFTY 500 once, then compute a daily breadth time series.

    Returns a DataFrame with one row per trading day for ~the last
    `lookback_days` calendar days. Trailing rows covered by fewer than
    `min_last_coverage` of the universe are dropped so an intraday run does
    not publish a half-reported session as the latest reading.
    """
    universe = _fetch_nifty500()
    if not universe:
        return pd.DataFrame()
    if verbose:
        print(f"  [premarket] Breadth universe: NIFTY 500 ({len(universe)} names)")

    # Pull ~1.5y of history per stock so the 200-EMA & 52w windows are valid
    # at the start of the 6-month display window.
    start = (dt.date.today() - dt.timedelta(days=lookback_days + 400)).isoformat()
    end = dt.date.today().isoformat()

    closes = _download_closes(universe, start, end, verbose=verbose)
    if not closes:
        return pd.DataFrame()

    if verbose:
        print(f"  [premarket] Loaded {len(closes)}/{len(universe)} series; "
              f"computing daily breadth …")

    px = _drop_partial_sessions(_px_from_closes(closes), min_last_coverage,
                                verbose, label="premarket")
    if px.empty:
        return pd.DataFrame()

    out = _breadth_from_px(px, len(universe))

    # Keep only the requested display window (rolling stats need the
    # earlier history but we don't display it).
    cutoff = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    out = out[out["Date"] >= cutoff].reset_index(drop=True)

    cols = ["Date", "Universe", "Scanned", "Above20EMA%", "Above50EMA%",
            "Above200EMA%", "New52wHighs", "New52wLows", "HiLoRatio",
            "Advances", "Declines", "AdvDecRatio"]
    return out[cols]


def compute_sector_breadth(lookback_days: int = 180,
                           verbose: bool = True,
                           min_stocks: int = 5,
                           min_last_coverage: float = 0.95,
                           include_custom: bool = True) -> pd.DataFrame:
    """Per-sector daily breadth over both sector taxonomies.

    Sectors come from :func:`_fetch_all_sectors`: the 20 official NSE
    ``Industry`` buckets plus, when `include_custom`, the 41 curated
    fine-grained sectors from ``index_constituents.json`` (prefixed ``C:``).
    The close matrix is downloaded once and sliced per sector, so the same
    row-wise reductions produce both taxonomies for one download cost.

    Trailing rows whose coverage is below `min_last_coverage` of the
    universe are dropped: when this runs intraday the newest bar is only
    partially populated, which shrinks every percentage's denominator (a
    50%% reading off 6 of 11 stocks is noise, not breadth).

    Returns a long DataFrame keyed on (Date, Sector). ``Sector`` includes a
    synthetic ``"NIFTY 500"`` series for index-vs-breadth divergence checks;
    that panel stays restricted to genuine NIFTY 500 members even though the
    custom sectors pull in several hundred symbols from outside it.
    """
    sectors = _fetch_all_sectors(min_stocks=min_stocks,
                                 include_custom=include_custom)
    if not sectors:
        if verbose:
            print("  [breadth] No sector map available; skipping")
        return pd.DataFrame()

    universe = sorted({s for syms in sectors.values() for s in syms})
    if verbose:
        n_custom = sum(1 for k in sectors if k.startswith(CUSTOM_SECTOR_PREFIX))
        print(f"  [breadth] {len(sectors)} sectors "
              f"({len(sectors) - n_custom} NSE + {n_custom} custom), "
              f"{len(universe)} stocks (min {min_stocks} per sector)")

    start = (dt.date.today() - dt.timedelta(days=lookback_days + 400)).isoformat()
    end = dt.date.today().isoformat()

    closes = _download_closes(universe, start, end, verbose=verbose)
    if not closes:
        return pd.DataFrame()

    px = _px_from_closes(closes)
    px = _drop_partial_sessions(px, min_last_coverage, verbose)
    if px.empty:
        return pd.DataFrame()

    if verbose:
        print(f"  [breadth] Loaded {len(closes)}/{len(universe)} series; "
              f"computing per-sector breadth …")

    cutoff = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    n500 = set(_fetch_nifty500())
    bench_cols = [c for c in px.columns if c in n500] or list(px.columns)
    groups = [("NIFTY 500", bench_cols)]
    groups += [(name, [s for s in syms if s in px.columns])
               for name, syms in sectors.items()]

    frames = []
    for name, cols in groups:
        if len(cols) < min_stocks:
            continue
        part = _breadth_from_px(px[cols], len(cols))
        part = part[part["Date"] >= cutoff].reset_index(drop=True)
        if part.empty:
            continue
        part.insert(1, "Sector", name)
        part.insert(2, "Members", len(cols))
        part["ADLine"] = (part["Advances"] - part["Declines"]).cumsum()
        frames.append(part)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _merge_history(fresh: pd.DataFrame) -> pd.DataFrame:
    """Persist the latest series, preserving any older dates already on disk.

    Rows on disk newer than the freshest computed date are discarded: they can
    only be partial sessions written by an earlier intraday run, and keeping
    them would resurrect exactly what `_drop_partial_sessions` just removed.
    """
    if fresh is None or fresh.empty:
        if BREADTH_HISTORY.exists():
            return pd.read_csv(BREADTH_HISTORY)
        return pd.DataFrame()

    if BREADTH_HISTORY.exists():
        try:
            old = pd.read_csv(BREADTH_HISTORY)
            old = old[~old["Date"].isin(fresh["Date"])]
            old = old[old["Date"] <= fresh["Date"].max()]
            merged = pd.concat([old, fresh], ignore_index=True)
        except Exception:
            merged = fresh
    else:
        merged = fresh
    merged = merged.sort_values("Date").reset_index(drop=True)
    merged.to_csv(BREADTH_HISTORY, index=False)
    return merged


def _snapshot_from_history(history: pd.DataFrame) -> dict:
    if history is None or history.empty:
        return {}
    last = history.iloc[-1].to_dict()
    return {k: last.get(k) for k in last}


def _build_breadth_sheet(snapshot: dict) -> pd.DataFrame:
    """Today's breadth as a presentable Metric/Value sheet."""
    if not snapshot:
        return pd.DataFrame([{"Metric": "Breadth", "Value": "No data"}])
    return pd.DataFrame([
        {"Metric": "Date",                  "Value": snapshot["Date"]},
        {"Metric": "NIFTY 500 universe",    "Value": snapshot["Universe"]},
        {"Metric": "Successfully scanned",  "Value": snapshot["Scanned"]},
        {"Metric": "% above 20-EMA",        "Value": snapshot["Above20EMA%"]},
        {"Metric": "% above 50-EMA",        "Value": snapshot["Above50EMA%"]},
        {"Metric": "% above 200-EMA",       "Value": snapshot["Above200EMA%"]},
        {"Metric": "New 52w Highs",         "Value": snapshot["New52wHighs"]},
        {"Metric": "New 52w Lows",          "Value": snapshot["New52wLows"]},
        {"Metric": "Hi/Lo Ratio",           "Value": snapshot["HiLoRatio"]},
        {"Metric": "Advances",              "Value": snapshot["Advances"]},
        {"Metric": "Declines",              "Value": snapshot["Declines"]},
        {"Metric": "Adv/Dec Ratio",         "Value": snapshot["AdvDecRatio"]},
    ])


# ─────────────────────────── chart ──────────────────────────────────────────

def _build_chart(history: pd.DataFrame, out_path: Path) -> Optional[Path]:
    """Render 4-panel Plotly chart from breadth history."""
    if history is None or history.empty:
        return None
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception as e:
        print(f"  [premarket] Plotly unavailable ({e}); chart skipped")
        return None

    h = history.copy()
    h["Date"] = pd.to_datetime(h["Date"])
    h = h.sort_values("Date")
    # Drop non-trading-day rows (weekends/holidays with negligible scans)
    if "Scanned" in h.columns:
        h = h[pd.to_numeric(h["Scanned"], errors="coerce") > 50].reset_index(drop=True)

    titles = [
        "% of NIFTY 500 above 50-EMA & 200-EMA"
        "<br><sup>Trend strength: >70% strong, <30% washout / oversold</sup>",
        "New 52-Week Highs vs Lows"
        "<br><sup>Risk-on when highs >> lows; warning when lows expand</sup>",
        "Advances vs Declines (daily close vs prev close)"
        "<br><sup>Daily participation \u2014 confirms or diverges from index move</sup>",
        "Hi/Lo Ratio (log)"
        "<br><sup>52w-Highs / 52w-Lows \u2014 distribution check</sup>",
    ]
    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.06,
        subplot_titles=titles, row_heights=[0.30, 0.25, 0.25, 0.20],
    )

    # Panel 1: % above 50/200 EMA
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["Above50EMA%"], name="% > 50-EMA",
        mode="lines+markers", line=dict(width=2, color="#1976D2"),
        marker=dict(size=4), connectgaps=True,
        hovertemplate="%{x|%d-%b-%Y}<br>%{y:.1f}%<extra>50-EMA</extra>",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["Above200EMA%"], name="% > 200-EMA",
        mode="lines+markers", line=dict(width=2, color="#7B1FA2"),
        marker=dict(size=4), connectgaps=True,
        hovertemplate="%{x|%d-%b-%Y}<br>%{y:.1f}%<extra>200-EMA</extra>",
    ), row=1, col=1)
    fig.add_hline(y=50, line_dash="dash", line_color="gray", row=1, col=1)
    fig.add_hline(y=70, line_dash="dot", line_color="#4CAF50", row=1, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="#F44336", row=1, col=1)

    # Panel 2: New 52w highs vs lows (lines)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["New52wHighs"], name="New 52w Highs",
        mode="lines+markers", line=dict(width=2, color="#4CAF50"),
        marker=dict(size=4), connectgaps=True,
        hovertemplate="%{x|%d-%b-%Y}<br>New Highs: %{y}<extra></extra>",
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["New52wLows"], name="New 52w Lows",
        mode="lines+markers", line=dict(width=2, color="#F44336"),
        marker=dict(size=4), connectgaps=True,
        hovertemplate="%{x|%d-%b-%Y}<br>New Lows: %{y}<extra></extra>",
    ), row=2, col=1)

    # Panel 3: Advances vs Declines (lines)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["Advances"], name="Advances",
        mode="lines+markers", line=dict(width=2, color="#4CAF50"),
        marker=dict(size=4), connectgaps=True,
        hovertemplate="%{x|%d-%b-%Y}<br>Advances: %{y}<extra></extra>",
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["Declines"], name="Declines",
        mode="lines+markers", line=dict(width=2, color="#F44336"),
        marker=dict(size=4), connectgaps=True,
        hovertemplate="%{x|%d-%b-%Y}<br>Declines: %{y}<extra></extra>",
    ), row=3, col=1)

    # Panel 4: Hi/Lo ratio (log)
    hl = h["HiLoRatio"].replace([float("inf")], pd.NA).astype(float)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=hl, name="Hi/Lo Ratio",
        mode="lines+markers", line=dict(width=2, color="#FF9800"),
        marker=dict(size=4), connectgaps=True,
        hovertemplate="%{x|%d-%b-%Y}<br>Ratio: %{y:.2f}<extra></extra>",
    ), row=4, col=1)
    fig.add_hline(y=1, line_dash="dash", line_color="gray", row=4, col=1)
    fig.update_yaxes(type="log", row=4, col=1)

    title = (f"Pre-Market Breadth Dashboard \u2014 NIFTY 500 "
             f"(last 6 months \u2014 as of {h['Date'].iloc[-1].strftime('%d-%b-%Y')})")
    fig.update_layout(
        title=dict(text=title, font=dict(size=20), x=0.02, xanchor="left",
                   y=0.98, yanchor="top"),
        hovermode="x unified",
        template="plotly_white",
        height=1100,
        margin=dict(t=110, l=60, r=40, b=110),
        legend=dict(orientation="h", yanchor="top", y=-0.08,
                    xanchor="center", x=0.5),
    )
    fig.update_xaxes(
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=3, label="3M", step="month", stepmode="backward"),
                dict(count=6, label="6M", step="month", stepmode="backward"),
                dict(step="all", label="All"),
            ],
        ),
        row=4, col=1,
    )

    fig.write_html(str(out_path), include_plotlyjs="cdn")
    return out_path


def _notes_df() -> pd.DataFrame:
    rows = [
        ("Equity quotes",  "Yahoo Finance via data_provider (Angel→jugaad→yf)"),
        ("Indian indices", "^NSEI (Nifty 50), ^NSEBANK (Bank Nifty), ^INDIAVIX"),
        ("Global indices", "^GSPC, ^IXIC, ^DJI, ^N225, ^HSI, ^FTSE"),
        ("Currencies",     "INR=X (USD/INR), DX-Y.NYB (DXY)"),
        ("Commodities",    "BZ=F (Brent), GC=F (Gold), HG=F (Copper)"),
        ("Yields",         "^TNX (US 10Y * 10 — divide by 10 for actual yield)"),
        ("Breadth",        "Full NIFTY 500 from NSE ind_nifty500list.csv (cached 7d)"),
        ("History",        "Appended daily to portfolio/.cache/breadth_history.csv"),
        ("Chart",          "portfolio/premarket_dashboard_chart.html (4 panels)"),
        ("Run cadence",    "Recommended: 8:30 IST every market day"),
        ("Caveat",         "GIFT Nifty live cue not included (no stable free symbol)."),
    ]
    return pd.DataFrame(rows, columns=["Field", "Value"])


# ─────────────────────────── public API ─────────────────────────────────────

def run(verbose: bool = True) -> dict:
    if verbose:
        print("  [premarket] Markets …")
    markets = _build_markets()
    if verbose:
        print("  [premarket] FX & commodities …")
    fxc = _build_fx_comm()

    fresh = _compute_breadth_history(verbose=verbose, lookback_days=180)
    history = _merge_history(fresh)
    snapshot = _snapshot_from_history(history)
    breadth_sheet = _build_breadth_sheet(snapshot)

    chart_path = PORTFOLIO_DIR / "premarket_dashboard_chart.html"
    chart_out = _build_chart(history, chart_path)
    if verbose and chart_out:
        print(f"  [premarket] Chart written: {chart_out}")

    return {
        "sheets": {
            "Pre-Market Markets": markets,
            "FX & Commodities": fxc,
            "Breadth (NIFTY500)": breadth_sheet,
            "Breadth History": history if history is not None else pd.DataFrame(),
            "Pre-Market Notes": _notes_df(),
        },
        "chart": str(chart_out) if chart_out else None,
    }


def main():
    ap = argparse.ArgumentParser(description="Pre-Market Dashboard")
    ap.add_argument("--out", default=str(PORTFOLIO_DIR / "premarket_dashboard.xlsx"))
    args = ap.parse_args()

    result = run()
    sheets = result["sheets"]
    with pd.ExcelWriter(args.out, engine="openpyxl") as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name[:31], index=False)
    print(f"\n  ✓ Wrote {args.out}")
    if result.get("chart"):
        print(f"  ✓ Chart {result['chart']}")


if __name__ == "__main__":
    main()
