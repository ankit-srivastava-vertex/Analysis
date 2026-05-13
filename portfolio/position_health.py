"""
position_health.py — daily technical health check on every owned position
==========================================================================

SUMMARY
-------
For each holding, computes a small set of objective technical signals and
rolls them into an Action / Watch / OK flag. This is the daily protective
scan for a 3–9 month positional trader: catches names breaking trend,
losing relative strength, or showing distribution.

WORKFLOW
--------
1. holdings_loader.load_holdings()  -> positions df
2. For each unique (Symbol, Series), download ~280 trading days of OHLCV
   via data_provider.download() (Angel One -> jugaad-data -> yfinance).
3. Compute per-position signals:
      - Last close vs 50 / 100 / 200-DMA (above/below + % distance)
      - Distance from 52-week high (drawdown)
      - 3-month and 6-month price return
      - Mansfield Relative Strength vs NIFTY 500 (3M)
      - Volume spike: today's vol / 50-day avg vol
      - Down-day on volume flag (close < prev close & vol > 2x avg)
      - Drawdown from average buy cost
4. Apply rule-set to derive a colour flag:
      ACTION  — close < 200-DMA  OR  draw-down from cost > 25%
                OR  RS3M < 90  AND  close < 100-DMA
      WATCH   — close < 50-DMA  OR  RS3M < 100  OR  vol spike on down day
      OK      — none of the above
5. Return three sheets: 'Position Health', 'Action List' (subset to act on),
   and 'Health Notes' (rule documentation).

DATA SOURCES
------------
- holdings_loader.py                        — positions
- data_provider.download (Angel One primary) — OHLCV (5y of NIFTY 500 +
                                              ~14 months per holding)
- ^CRSLDX (NIFTY 500) via yfinance fallback  — RS benchmark
  (Angel symbol token resolved automatically by data_provider)

OUTPUT
------
Sheets returned by run():
  Position Health  — every position with all signals + Flag (OK/WATCH/ACTION)
  Action List      — only ACTION rows (the actual to-do for today)
  Health Notes     — rule definitions

USAGE
-----
    from portfolio.position_health import run
    result = run()         # {"sheets": {...}}
    # or CLI:
    python3 -m portfolio.position_health

DEPENDENCIES
------------
pandas, numpy, openpyxl, data_provider (parent package), holdings_loader
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Allow running as `python3 -m portfolio.position_health` from project root
PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import data_provider  # noqa: E402
from portfolio.holdings_loader import load_holdings  # noqa: E402

LOOKBACK_DAYS = 380       # ~14 months — enough for 200-DMA + 1y high
BENCHMARK = "^CRSLDX"     # NIFTY 500 Total Returns proxy on yfinance
RS_BENCH_FALLBACK = "^NSEI"  # Nifty 50 if 500 fails


# ─────────────────────────── helpers ────────────────────────────────────────

def _to_data_provider_ticker(symbol: str, series: str) -> str:
    """Convert (symbol, series) to the form data_provider understands."""
    if not symbol:
        return ""
    sym = symbol.upper()
    if series and series.upper() in ("SM", "ST", "SME"):
        # Angel SME suffix
        return f"{sym}-SM.NS"
    return f"{sym}.NS"


def _safe_close(df: Optional[pd.DataFrame]) -> Optional[pd.Series]:
    if df is None or df.empty or "Close" not in df.columns:
        return None
    s = df["Close"].dropna()
    return s if len(s) > 0 else None


def _pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def _fetch_ohlcv(ticker: str, start: dt.date) -> Optional[pd.DataFrame]:
    try:
        df = data_provider.download(ticker, start=start.isoformat(),
                                    end=dt.date.today().isoformat(),
                                    interval="1d", progress=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        # single-ticker request shouldn't be multi but be safe
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    return df


def _signals_for(symbol: str, series: str, avg_cost: float,
                 bench_close: Optional[pd.Series],
                 start: dt.date) -> dict:
    """Compute all technical signals for one ticker."""
    out = {
        "DataPts": 0, "Close": np.nan,
        "vs50DMA%": np.nan, "vs100DMA%": np.nan, "vs200DMA%": np.nan,
        "From52wHi%": np.nan, "Ret3M%": np.nan, "Ret6M%": np.nan,
        "RS3M": np.nan, "VolSpike": np.nan, "DownDayVolFlag": False,
        "FromCost%": np.nan,
    }
    tkr = _to_data_provider_ticker(symbol, series)
    if not tkr:
        return out
    df = _fetch_ohlcv(tkr, start)
    close = _safe_close(df)
    if close is None or len(close) < 30:
        return out

    out["DataPts"] = len(close)
    last = float(close.iloc[-1])
    out["Close"] = round(last, 2)

    if len(close) >= 50:
        out["vs50DMA%"] = round(_pct(last, close.tail(50).mean()), 2)
    if len(close) >= 100:
        out["vs100DMA%"] = round(_pct(last, close.tail(100).mean()), 2)
    if len(close) >= 200:
        out["vs200DMA%"] = round(_pct(last, close.tail(200).mean()), 2)

    hi52 = close.tail(252).max() if len(close) >= 60 else close.max()
    out["From52wHi%"] = round(_pct(last, hi52), 2)

    if len(close) >= 65:
        out["Ret3M%"] = round(_pct(last, close.iloc[-65]), 2)
    if len(close) >= 130:
        out["Ret6M%"] = round(_pct(last, close.iloc[-130]), 2)

    # Mansfield-style RS: (stock_price / bench_price) normalised to 100
    if bench_close is not None and len(bench_close) >= 65:
        try:
            joined = pd.concat([close.rename("s"), bench_close.rename("b")],
                               axis=1).dropna()
            if len(joined) >= 65:
                ratio = joined["s"] / joined["b"]
                rs_now = ratio.iloc[-1]
                rs_3m_ago = ratio.iloc[-65]
                out["RS3M"] = round((rs_now / rs_3m_ago) * 100, 1)
        except Exception:
            pass

    # Volume signals
    if df is not None and "Volume" in df.columns:
        vol = df["Volume"].dropna()
        if len(vol) >= 50:
            avg50 = vol.tail(50).mean()
            today_vol = float(vol.iloc[-1])
            if avg50:
                out["VolSpike"] = round(today_vol / avg50, 2)
            if len(close) >= 2:
                down_day = close.iloc[-1] < close.iloc[-2]
                if down_day and avg50 and today_vol > 2 * avg50:
                    out["DownDayVolFlag"] = True

    if avg_cost and avg_cost > 0:
        out["FromCost%"] = round(_pct(last, avg_cost), 2)

    return out


def _classify(row: dict) -> str:
    """OK / WATCH / ACTION based on signals."""
    vs200 = row.get("vs200DMA%")
    vs100 = row.get("vs100DMA%")
    vs50 = row.get("vs50DMA%")
    rs3m = row.get("RS3M")
    cost_dd = row.get("FromCost%")
    down_vol = row.get("DownDayVolFlag")

    # ACTION
    if vs200 is not None and not pd.isna(vs200) and vs200 < 0:
        return "ACTION"
    if cost_dd is not None and not pd.isna(cost_dd) and cost_dd <= -25:
        return "ACTION"
    if (rs3m is not None and not pd.isna(rs3m) and rs3m < 90
            and vs100 is not None and not pd.isna(vs100) and vs100 < 0):
        return "ACTION"

    # WATCH
    if vs50 is not None and not pd.isna(vs50) and vs50 < 0:
        return "WATCH"
    if rs3m is not None and not pd.isna(rs3m) and rs3m < 100:
        return "WATCH"
    if down_vol:
        return "WATCH"

    return "OK"


# ─────────────────────────── public API ─────────────────────────────────────

def _notes_df() -> pd.DataFrame:
    rows = [
        ("ACTION", "Close < 200-DMA  (long-term trend broken)"),
        ("ACTION", "Drawdown from average cost <= -25%  (stop-loss zone)"),
        ("ACTION", "RS3M < 90  AND  Close < 100-DMA  (relative + medium-term failure)"),
        ("WATCH",  "Close < 50-DMA  (short-term trend broken)"),
        ("WATCH",  "RS3M < 100  (under-performing benchmark over 3 months)"),
        ("WATCH",  "Down day on > 2x average volume  (distribution signal)"),
        ("OK",     "None of the above"),
        ("Note",   "Benchmark = ^CRSLDX (NIFTY 500). RS3M = (stock/bench ratio "
                   "today) / (ratio 65 trading days ago) * 100."),
        ("Note",   "DMA = Daily Moving Average of closing price."),
        ("Note",   "DataPts < 30 -> all signals NaN; flag defaults to OK."),
    ]
    return pd.DataFrame(rows, columns=["Flag", "Rule"])


def run(verbose: bool = True) -> dict:
    holdings = load_holdings(verbose=verbose)
    if holdings.empty:
        if verbose:
            print("  [position_health] No holdings — nothing to scan")
        return {"sheets": {}}

    start = dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS + 60)

    # Benchmark close (try NIFTY 500, fall back to NIFTY 50)
    if verbose:
        print(f"  [position_health] Fetching benchmark {BENCHMARK} …")
    bench = _fetch_ohlcv(BENCHMARK, start)
    bench_close = _safe_close(bench)
    if bench_close is None:
        if verbose:
            print(f"  [position_health] {BENCHMARK} failed; trying {RS_BENCH_FALLBACK}")
        bench = _fetch_ohlcv(RS_BENCH_FALLBACK, start)
        bench_close = _safe_close(bench)

    rows = []
    n = len(holdings)
    for i, h in enumerate(holdings.itertuples(index=False), 1):
        if verbose and (i % 10 == 0 or i == n):
            print(f"    {i:3d}/{n}  {h.Symbol or h.Company[:18]}")
        sig = _signals_for(h.Symbol, h.Series, h.AvgCost, bench_close, start)
        flag = _classify(sig)
        rows.append({
            "Symbol": h.Symbol, "Series": h.Series, "Company": h.Company,
            "Sector": h.Sector, "Quantity": h.Quantity,
            "AvgCost": round(h.AvgCost, 2), "LastClose": sig["Close"],
            "FromCost%": sig["FromCost%"],
            "vs50DMA%": sig["vs50DMA%"], "vs100DMA%": sig["vs100DMA%"],
            "vs200DMA%": sig["vs200DMA%"], "From52wHi%": sig["From52wHi%"],
            "Ret3M%": sig["Ret3M%"], "Ret6M%": sig["Ret6M%"],
            "RS3M": sig["RS3M"], "VolSpike": sig["VolSpike"],
            "DownDayVolFlag": sig["DownDayVolFlag"],
            "DataPts": sig["DataPts"], "Flag": flag,
        })

    health_df = pd.DataFrame(rows)
    # Sort: ACTION first, then WATCH, then OK; within group by Present desc
    flag_order = {"ACTION": 0, "WATCH": 1, "OK": 2}
    health_df["_o"] = health_df["Flag"].map(flag_order).fillna(3)
    health_df = (health_df.sort_values(["_o", "Symbol"])
                          .drop(columns="_o").reset_index(drop=True))

    action_df = health_df[health_df["Flag"] == "ACTION"].reset_index(drop=True)

    counts = health_df["Flag"].value_counts().to_dict()
    if verbose:
        print(f"  [position_health] OK={counts.get('OK', 0)}  "
              f"WATCH={counts.get('WATCH', 0)}  "
              f"ACTION={counts.get('ACTION', 0)}")

    return {"sheets": {
        "Position Health": health_df,
        "Action List": action_df,
        "Health Notes": _notes_df(),
    }}


def main():
    ap = argparse.ArgumentParser(description="Position Health — daily technical scan")
    ap.add_argument("--out", default=str(PORTFOLIO_DIR / "position_health.xlsx"))
    args = ap.parse_args()

    result = run()
    sheets = result["sheets"]
    if not sheets:
        return
    with pd.ExcelWriter(args.out, engine="openpyxl") as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name[:31], index=False)
    print(f"\n  ✓ Wrote {args.out}")


if __name__ == "__main__":
    main()
