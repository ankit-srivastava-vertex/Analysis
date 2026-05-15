"""
correlation_clusters.py — hidden concentration via return correlations
=======================================================================

SUMMARY
-------
Sector exposure (from portfolio_tracker) under-counts true concentration
when multiple holdings move together for non-sector reasons (e.g.,
crude-oil sensitives across Auto+Aviation+Paint, or rate-sensitives
across NBFC+Realty). This module:

  - Computes 1-year daily-return correlation matrix across all holdings.
  - Flags every PAIR with |corr| >= HIGH_CORR (default 0.70) that
    together represent a meaningful weight of the book.
  - Builds simple greedy correlation clusters (>= CLUSTER_CORR within
    cluster) and totals their portfolio weight — your "true" thematic
    exposure.

USAGE
-----
    python3 -m portfolio.correlation_clusters
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio.holdings_loader import load_holdings
from portfolio._prices import fetch_close_panel

LOOKBACK_DAYS = 365
HIGH_CORR = 0.70
CLUSTER_CORR = 0.60


def _high_corr_pairs(corr: pd.DataFrame, weights: pd.Series,
                     companies: pd.Series) -> pd.DataFrame:
    rows = []
    cols = corr.columns
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = cols[i], cols[j]
            c = corr.iloc[i, j]
            if pd.isna(c) or abs(c) < HIGH_CORR:
                continue
            wa = float(weights.get(a, 0))
            wb = float(weights.get(b, 0))
            rows.append({
                "A": a,
                "A_Company": companies.get(a, ""),
                "B": b,
                "B_Company": companies.get(b, ""),
                "Correlation": round(float(c), 3),
                "A_Weight%": round(wa * 100, 2),
                "B_Weight%": round(wb * 100, 2),
                "Combined_Weight%": round((wa + wb) * 100, 2),
            })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["Combined_Weight%", "Correlation"],
                            ascending=[False, False])
    return df


def _greedy_clusters(corr: pd.DataFrame, weights: pd.Series,
                     companies: pd.Series, sectors: pd.Series) -> pd.DataFrame:
    """Group symbols where every pair has corr >= CLUSTER_CORR."""
    remaining = set(corr.columns)
    # Seed by largest weight first
    order = weights.reindex(corr.columns).fillna(0).sort_values(ascending=False).index
    clusters = []
    for sym in order:
        if sym not in remaining:
            continue
        group = [sym]
        for other in order:
            if other == sym or other not in remaining:
                continue
            # Connected to ALL existing members at >= CLUSTER_CORR
            ok = all(
                pd.notna(corr.at[m, other]) and corr.at[m, other] >= CLUSTER_CORR
                for m in group
            )
            if ok:
                group.append(other)
        for m in group:
            remaining.discard(m)
        if len(group) >= 2:
            total_w = float(weights.reindex(group).sum() * 100)
            avg_corr = float(np.mean([
                corr.at[group[i], group[j]]
                for i in range(len(group))
                for j in range(i + 1, len(group))
            ]))
            clusters.append({
                "Cluster #": len(clusters) + 1,
                "Members": ", ".join(group),
                "Companies": " | ".join(companies.get(m, "") for m in group),
                "Sectors": " | ".join(set(sectors.get(m, "") for m in group)),
                "Size": len(group),
                "Total_Weight%": round(total_w, 2),
                "Avg_PairCorr": round(avg_corr, 3),
            })
    df = pd.DataFrame(clusters)
    if not df.empty:
        df = df.sort_values("Total_Weight%", ascending=False)
    return df


def _notes_df() -> pd.DataFrame:
    rows = [
        ("Lookback",         f"{LOOKBACK_DAYS} calendar days of daily Close returns"),
        ("Pairs threshold",  f"|corr| >= {HIGH_CORR} (high-correlation pairs)"),
        ("Cluster threshold",f"corr >= {CLUSTER_CORR} between every pair in a group"),
        ("Why this matters", "Two holdings with corr 0.85 ARE one bet, even if "
                             "their official sectors differ."),
        ("Action",           "If a cluster's Total_Weight% > 25–30%, you may have "
                             "concentration risk hidden by sector-level reporting."),
    ]
    return pd.DataFrame(rows, columns=["Field", "Value"])


def run(verbose: bool = True) -> dict:
    if verbose:
        print("  [corr] Loading holdings …")
    h = load_holdings(verbose=False)
    if h.empty:
        return {"sheets": {"Correlation": pd.DataFrame(
            [{"Note": "no holdings"}])}}
    h = h[h["PresentValue"].fillna(0) > 0].copy()
    # Some holdings may share Symbol across brokers — collapse
    h = (h.groupby("Symbol", as_index=False)
           .agg({"Company": "first", "Sector": "first", "Series": "first",
                 "PresentValue": "sum"}))
    total = h["PresentValue"].sum()
    h["Weight"] = h["PresentValue"] / total if total else 0

    if verbose:
        print(f"  [corr] Pulling {LOOKBACK_DAYS}d prices for {len(h)} positions …")
    panel = fetch_close_panel(zip(h["Symbol"], h.get("Series", "EQ")),
                              lookback_days=LOOKBACK_DAYS, verbose=verbose)
    if panel.empty or panel.shape[1] < 2:
        return {"sheets": {"Correlation": pd.DataFrame(
            [{"Note": "insufficient data"}])}}

    returns = panel.pct_change().dropna(how="all")
    corr = returns.corr()

    weights = h.set_index("Symbol")["Weight"]
    companies = h.set_index("Symbol")["Company"]
    sectors = h.set_index("Symbol")["Sector"].fillna("Unknown")

    pairs = _high_corr_pairs(corr, weights, companies)
    clusters = _greedy_clusters(corr, weights, companies, sectors)

    return {"sheets": {
        "Correlation Pairs": pairs if not pairs.empty else
            pd.DataFrame([{"Note": f"no pairs above {HIGH_CORR}"}]),
        "Correlation Clusters": clusters if not clusters.empty else
            pd.DataFrame([{"Note": f"no clusters above {CLUSTER_CORR}"}]),
        "Correlation Notes": _notes_df(),
    }}


def main():
    ap = argparse.ArgumentParser(description="Correlation & cluster analysis")
    ap.add_argument("--out", default=str(PORTFOLIO_DIR / "correlation_clusters.xlsx"))
    args = ap.parse_args()
    result = run()
    with pd.ExcelWriter(args.out, engine="openpyxl") as w:
        for name, df in result["sheets"].items():
            df.to_excel(w, sheet_name=name[:31], index=False)
    print(f"\n  ✓ Wrote {args.out}")


if __name__ == "__main__":
    main()
