"""
portfolio_tracker.py — daily portfolio P&L, exposure & concentration
=====================================================================

SUMMARY
-------
The "what do I own and how is it doing" module. Reads broker holdings,
computes per-position MTM (using broker-reported previous close),
portfolio totals, sector exposure, and concentration. This is the
foundation used by position_health.py and events_calendar.py.

WORKFLOW
--------
1. holdings_loader.load_holdings()  -> unified positions DataFrame
2. Compute portfolio summary:
      Invested, Present, Realised gain/loss (P&L), Day-MTM
3. Compute per-position weight (% of present value).
4. Compute sector exposure (sum of present value per sector).
5. Compute concentration: Top 5 / Top 10 holdings as % of book.
6. Write four sheets ('Positions', 'Portfolio Summary',
   'Sector Exposure', 'Concentration') and return them via run().

DATA SOURCES
------------
- holdings_loader.py (broker xlsx files) — provides position rows.
- No live network calls in this module. Last-close comes from the
  broker file's "Previous Closing Price" / "Closing price" column,
  which both Angel and Groww update on T-1 EOD.

OUTPUT
------
Sheets returned by run():
  Positions          — full per-row holdings + weight %
  Portfolio Summary  — single-row totals (Invested, Present, P&L, P&L%)
  Sector Exposure    — sector | invested | present | weight%
  Concentration      — top-N rows + cumulative weight

USAGE
-----
    from portfolio.portfolio_tracker import run
    result = run()      # returns {'sheets': {...}}
    # or CLI:
    python3 -m portfolio.portfolio_tracker

DEPENDENCIES
------------
pandas, openpyxl, holdings_loader (sibling module)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from portfolio.holdings_loader import load_holdings

PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent


# ─────────────────────────── computation ────────────────────────────────────

def _build_positions(df: pd.DataFrame) -> pd.DataFrame:
    """Add Weight% column, order columns for the Positions sheet."""
    if df.empty:
        return df
    total_present = df["PresentValue"].sum()
    df = df.copy()
    df["Weight%"] = (df["PresentValue"] / total_present * 100) if total_present else 0.0
    cols = ["Symbol", "Series", "Company", "ISIN", "Sector", "Source",
            "Quantity", "AvgCost", "LastClose",
            "InvestedValue", "PresentValue", "PnL", "PnLPct", "Weight%"]
    return df[[c for c in cols if c in df.columns]]


def _build_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    invested = df["InvestedValue"].sum()
    present = df["PresentValue"].sum()
    pnl = present - invested
    pnl_pct = (pnl / invested * 100) if invested else 0.0
    winners = df[df["PnL"] > 0]
    losers = df[df["PnL"] < 0]
    return pd.DataFrame([{
        "Positions": len(df),
        "Invested (Rs)": round(invested, 2),
        "Present (Rs)": round(present, 2),
        "Unrealised P&L (Rs)": round(pnl, 2),
        "Unrealised P&L %": round(pnl_pct, 2),
        "Winners": len(winners),
        "Losers": len(losers),
        "Win Rate %": round(len(winners) / len(df) * 100, 1) if len(df) else 0,
        "Best (P&L Rs)": round(df["PnL"].max(), 2) if len(df) else 0,
        "Worst (P&L Rs)": round(df["PnL"].min(), 2) if len(df) else 0,
    }])


def _build_sector_exposure(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["Sector"] = d["Sector"].replace("", "UNKNOWN").fillna("UNKNOWN")
    g = d.groupby("Sector").agg(
        Positions=("Symbol", "count"),
        Invested=("InvestedValue", "sum"),
        Present=("PresentValue", "sum"),
        PnL=("PnL", "sum"),
    ).reset_index()
    total = g["Present"].sum()
    g["Weight%"] = (g["Present"] / total * 100).round(2) if total else 0.0
    g["PnL%"] = (g["PnL"] / g["Invested"].replace(0, pd.NA) * 100).round(2)
    g[["Invested", "Present", "PnL"]] = g[["Invested", "Present", "PnL"]].round(2)
    return g.sort_values("Present", ascending=False).reset_index(drop=True)


def _build_concentration(df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    d = df.sort_values("PresentValue", ascending=False).head(top_n).copy()
    total = df["PresentValue"].sum()
    d["Weight%"] = (d["PresentValue"] / total * 100).round(2) if total else 0.0
    d["CumWeight%"] = d["Weight%"].cumsum().round(2)
    return d[["Symbol", "Company", "Sector", "PresentValue",
              "PnL", "PnLPct", "Weight%", "CumWeight%"]].reset_index(drop=True)


# ─────────────────────────── public API ─────────────────────────────────────

def run(verbose: bool = True) -> dict:
    """Build all portfolio_tracker sheets.

    Returns:
        {"sheets": {"Positions": df, "Portfolio Summary": df,
                    "Sector Exposure": df, "Concentration": df},
         "holdings": <raw unified holdings df>}
    """
    holdings = load_holdings(verbose=verbose)
    if holdings.empty:
        if verbose:
            print("  [portfolio_tracker] No holdings — nothing to compute")
        return {"sheets": {}, "holdings": holdings}

    sheets = {
        "Positions": _build_positions(holdings),
        "Portfolio Summary": _build_summary(holdings),
        "Sector Exposure": _build_sector_exposure(holdings),
        "Concentration": _build_concentration(holdings, top_n=10),
    }

    if verbose:
        s = sheets["Portfolio Summary"].iloc[0]
        print(f"  [portfolio_tracker] Positions={s['Positions']}  "
              f"Invested=₹{s['Invested (Rs)']:,.0f}  "
              f"Present=₹{s['Present (Rs)']:,.0f}  "
              f"P&L=₹{s['Unrealised P&L (Rs)']:,.0f} "
              f"({s['Unrealised P&L %']:+.2f}%)")
    return {"sheets": sheets, "holdings": holdings}


def main():
    ap = argparse.ArgumentParser(description="Portfolio Tracker — daily MTM & exposure")
    ap.add_argument("--out", default=str(PORTFOLIO_DIR / "portfolio_tracker.xlsx"),
                    help="Output Excel path (CLI only)")
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
