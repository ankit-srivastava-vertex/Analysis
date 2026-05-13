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
  * Breadth        : % of NIFTY 500 above 50-DMA / 200-DMA, new 52w highs
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
     * % above 50-DMA, % above 200-DMA
     * new 52w highs vs lows, hi/lo ratio
     * advance/decline (today's close > prev close)
   Append today's row to a persistent CSV history
   (portfolio/.cache/breadth_history.csv) for trend tracking.
5. Render an interactive Plotly HTML chart
   (portfolio/premarket_dashboard_chart.html) with 4 panels:
     a. % above 50-DMA & 200-DMA (line)
     b. New 52w highs vs lows (bar)
     c. Advance / decline ratio (bar)
     d. Hi-lo ratio (line, log)
6. Compose four sheets (Markets, Currencies & Commodities, Breadth,
   Breadth History) and a Notes sheet documenting symbols + sources.

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


def _fetch_nifty500() -> list:
    """Fetch the official NIFTY 500 constituent symbols (cached 7d)."""
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
                return []
    df.columns = [c.strip().upper() for c in df.columns]
    sym_col = next((c for c in df.columns if "SYMBOL" in c), None)
    if not sym_col:
        return []
    return [s.strip().upper() for s in df[sym_col].dropna().astype(str)]


def _compute_breadth_history(verbose: bool = True,
                             lookback_days: int = 180) -> pd.DataFrame:
    """Scan full NIFTY 500 once, then compute a daily breadth time series.

    Returns a DataFrame with one row per trading day for ~the last
    `lookback_days` calendar days.
    """
    universe = _fetch_nifty500()
    if not universe:
        return pd.DataFrame()
    if verbose:
        print(f"  [premarket] Breadth universe: NIFTY 500 ({len(universe)} names)")

    # Pull ~1.5y of history per stock so 200-DMA & 52w windows are valid
    # at the start of the 6-month display window.
    start = (dt.date.today() - dt.timedelta(days=lookback_days + 400)).isoformat()
    end = dt.date.today().isoformat()

    closes = {}
    for i, sym in enumerate(universe, 1):
        if verbose and i % 50 == 0:
            print(f"    breadth {i}/{len(universe)}")
        try:
            df = data_provider.download(f"{sym}.NS", start=start, end=end,
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

    if not closes:
        return pd.DataFrame()

    if verbose:
        print(f"  [premarket] Loaded {len(closes)}/{len(universe)} series; "
              f"computing daily breadth …")

    px = pd.DataFrame(closes).sort_index()

    # Vectorised rolling stats per stock.
    sma50  = px.rolling(50,  min_periods=50).mean()
    sma200 = px.rolling(200, min_periods=200).mean()
    hi252  = px.rolling(252, min_periods=200).max()
    lo252  = px.rolling(252, min_periods=200).min()
    prev   = px.shift(1)

    import numpy as np
    valid = px.notna()
    sma50_n  = sma50.notna().sum(axis=1).replace(0, np.nan).astype(float)
    sma200_n = sma200.notna().sum(axis=1).replace(0, np.nan).astype(float)
    above50_pct  = (((px > sma50)  & sma50.notna()).sum(axis=1).astype(float)  / sma50_n  * 100)
    above200_pct = (((px > sma200) & sma200.notna()).sum(axis=1).astype(float) / sma200_n * 100)
    new_highs = ((px >= hi252 * 0.999) & hi252.notna()).sum(axis=1)
    new_lows  = ((px <= lo252 * 1.001) & lo252.notna()).sum(axis=1)
    advances  = ((px > prev)  & prev.notna()).sum(axis=1)
    declines  = ((px < prev)  & prev.notna()).sum(axis=1)
    scanned   = valid.sum(axis=1)

    out = pd.DataFrame({
        "Date":        px.index.strftime("%Y-%m-%d"),
        "Universe":    len(universe),
        "Scanned":     scanned.values,
        "Above50DMA%": above50_pct.round(2).values,
        "Above200DMA%":above200_pct.round(2).values,
        "New52wHighs": new_highs.values,
        "New52wLows":  new_lows.values,
        "Advances":    advances.values,
        "Declines":    declines.values,
    })
    out["HiLoRatio"]   = (out["New52wHighs"].astype(float) / out["New52wLows"].replace(0, np.nan).astype(float)).round(2)
    out["AdvDecRatio"] = (out["Advances"].astype(float)   / out["Declines"].replace(0, np.nan).astype(float)).round(2)

    # Keep only the requested display window (rolling stats need the
    # earlier history but we don't display it).
    cutoff = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    out = out[out["Date"] >= cutoff].reset_index(drop=True)

    cols = ["Date", "Universe", "Scanned", "Above50DMA%", "Above200DMA%",
            "New52wHighs", "New52wLows", "HiLoRatio",
            "Advances", "Declines", "AdvDecRatio"]
    return out[cols]


def _merge_history(fresh: pd.DataFrame) -> pd.DataFrame:
    """Persist the latest series, preserving any older dates already on disk."""
    if fresh is None or fresh.empty:
        if BREADTH_HISTORY.exists():
            return pd.read_csv(BREADTH_HISTORY)
        return pd.DataFrame()

    if BREADTH_HISTORY.exists():
        try:
            old = pd.read_csv(BREADTH_HISTORY)
            old = old[~old["Date"].isin(fresh["Date"])]
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
        {"Metric": "% above 50-DMA",        "Value": snapshot["Above50DMA%"]},
        {"Metric": "% above 200-DMA",       "Value": snapshot["Above200DMA%"]},
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

    titles = [
        "% of NIFTY 500 above 50-DMA & 200-DMA"
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

    # Panel 1: % above 50/200 DMA
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["Above50DMA%"], name="% > 50-DMA",
        mode="lines+markers", line=dict(width=2, color="#1976D2"),
        marker=dict(size=4),
        hovertemplate="%{x|%d-%b-%Y}<br>%{y:.1f}%<extra>50-DMA</extra>",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["Above200DMA%"], name="% > 200-DMA",
        mode="lines+markers", line=dict(width=2, color="#7B1FA2"),
        marker=dict(size=4),
        hovertemplate="%{x|%d-%b-%Y}<br>%{y:.1f}%<extra>200-DMA</extra>",
    ), row=1, col=1)
    fig.add_hline(y=50, line_dash="dash", line_color="gray", row=1, col=1)
    fig.add_hline(y=70, line_dash="dot", line_color="#4CAF50", row=1, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="#F44336", row=1, col=1)

    # Panel 2: New 52w highs vs lows (lines)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["New52wHighs"], name="New 52w Highs",
        mode="lines+markers", line=dict(width=2, color="#4CAF50"),
        marker=dict(size=4),
        hovertemplate="%{x|%d-%b-%Y}<br>New Highs: %{y}<extra></extra>",
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["New52wLows"], name="New 52w Lows",
        mode="lines+markers", line=dict(width=2, color="#F44336"),
        marker=dict(size=4),
        hovertemplate="%{x|%d-%b-%Y}<br>New Lows: %{y}<extra></extra>",
    ), row=2, col=1)

    # Panel 3: Advances vs Declines (lines)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["Advances"], name="Advances",
        mode="lines+markers", line=dict(width=2, color="#4CAF50"),
        marker=dict(size=4),
        hovertemplate="%{x|%d-%b-%Y}<br>Advances: %{y}<extra></extra>",
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=h["Declines"], name="Declines",
        mode="lines+markers", line=dict(width=2, color="#F44336"),
        marker=dict(size=4),
        hovertemplate="%{x|%d-%b-%Y}<br>Declines: %{y}<extra></extra>",
    ), row=3, col=1)

    # Panel 4: Hi/Lo ratio (log)
    hl = h["HiLoRatio"].replace([float("inf")], pd.NA).astype(float)
    fig.add_trace(go.Scatter(
        x=h["Date"], y=hl, name="Hi/Lo Ratio",
        mode="lines+markers", line=dict(width=2, color="#FF9800"),
        marker=dict(size=4),
        hovertemplate="%{x|%d-%b-%Y}<br>Ratio: %{y:.2f}<extra></extra>",
    ), row=4, col=1)
    fig.add_hline(y=1, line_dash="dash", line_color="gray", row=4, col=1)
    fig.update_yaxes(type="log", row=4, col=1)

    title = (f"Pre-Market Breadth Dashboard \u2014 NIFTY 500 "
             f"(last 6 months \u2014 as of {h['Date'].iloc[-1].strftime('%d-%b-%Y')})")
    fig.update_layout(
        title=dict(text=title, font=dict(size=20)),
        hovermode="x unified",
        template="plotly_white",
        height=1000,
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
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
