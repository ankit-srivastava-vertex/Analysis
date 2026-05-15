"""
risk_metrics.py — portfolio risk dashboard
===========================================

SUMMARY
-------
Computes portfolio-level risk metrics that the existing
portfolio_tracker (exposure) does not provide:

  - Per-position beta vs ^NSEI (Nifty 50)
  - Per-position annualised volatility & max drawdown
  - Portfolio-weighted beta
  - Portfolio NAV time series (synthetic, 1y) and its max drawdown
  - 1-day and 5-day Value-at-Risk (95%, parametric)
  - Sharpe ratio (vs Nifty 50 as risk-free proxy at 0%)
  - Best / worst single day for the portfolio

WORKFLOW
--------
1. Load holdings (current weights = PresentValue / TotalPresent).
2. Pull 1y daily Close for each holding + ^NSEI via _prices helper.
3. Build daily returns matrix; compute per-position stats (beta, vol,
   MDD) by regressing on benchmark.
4. Build portfolio NAV = sum(weight_i * cumulative_return_i) starting
   from 100, then portfolio-level VaR / Sharpe / MDD.
5. Return three sheets: Risk (per position), Risk Summary, Risk Notes.

USAGE
-----
    from portfolio.risk_metrics import run
    result = run()
    # CLI:  python3 -m portfolio.risk_metrics
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio.holdings_loader import load_holdings
from portfolio._prices import fetch_close_panel, fetch_benchmark

LOOKBACK_DAYS = 365


def _per_position_stats(returns: pd.DataFrame,
                        bench_ret: pd.Series) -> pd.DataFrame:
    """Per-stock beta, annualised vol, max drawdown."""
    rows = []
    bench_var = bench_ret.var()
    for sym in returns.columns:
        r = returns[sym].dropna()
        if len(r) < 30:
            continue
        aligned = pd.concat([r, bench_ret], axis=1, join="inner").dropna()
        if len(aligned) < 30 or bench_var == 0:
            beta = np.nan
        else:
            cov = aligned.iloc[:, 0].cov(aligned.iloc[:, 1])
            beta = cov / bench_var
        ann_vol = r.std() * np.sqrt(252) * 100
        nav = (1 + r).cumprod()
        mdd = ((nav / nav.cummax()) - 1).min() * 100
        rows.append({
            "Symbol": sym,
            "DataPts": len(r),
            "Beta_vs_Nifty": round(float(beta), 2) if pd.notna(beta) else np.nan,
            "AnnVol%": round(ann_vol, 1),
            "MaxDD%": round(mdd, 1),
        })
    return pd.DataFrame(rows)


def _portfolio_nav(returns: pd.DataFrame, weights: pd.Series) -> pd.Series:
    """Synthetic portfolio daily return = sum(w_i * r_i_t)."""
    aligned = returns.reindex(columns=weights.index).dropna(how="all")
    w = weights.reindex(aligned.columns).fillna(0)
    if w.sum() == 0:
        return pd.Series(dtype=float)
    w = w / w.sum()
    port_ret = aligned.fillna(0).dot(w)
    return port_ret


def _summary_metrics(port_ret: pd.Series, bench_ret: pd.Series,
                     port_beta: float) -> dict:
    if port_ret.empty:
        return {}
    nav = (1 + port_ret).cumprod() * 100
    mdd = ((nav / nav.cummax()) - 1).min() * 100
    ann_vol = port_ret.std() * np.sqrt(252) * 100
    ann_ret = ((1 + port_ret).prod() ** (252 / len(port_ret)) - 1) * 100
    sharpe = (ann_ret / ann_vol) if ann_vol else np.nan
    var1d = -np.percentile(port_ret.dropna(), 5) * 100         # historical 95%
    var5d = -np.percentile(port_ret.rolling(5).sum().dropna(), 5) * 100
    parametric_var1d = 1.645 * port_ret.std() * 100
    bench_ann_ret = ((1 + bench_ret).prod() ** (252 / len(bench_ret)) - 1) * 100 \
        if not bench_ret.empty else np.nan
    excess_ann = ann_ret - bench_ann_ret if pd.notna(bench_ann_ret) else np.nan
    best = port_ret.max() * 100
    worst = port_ret.min() * 100
    return {
        "DataPts": len(port_ret),
        "PortfolioBeta": round(port_beta, 2) if pd.notna(port_beta) else np.nan,
        "AnnReturn%": round(ann_ret, 2),
        "BenchAnnReturn%": round(bench_ann_ret, 2) if pd.notna(bench_ann_ret) else np.nan,
        "ExcessReturn%": round(excess_ann, 2) if pd.notna(excess_ann) else np.nan,
        "AnnVol%": round(ann_vol, 2),
        "Sharpe": round(sharpe, 2) if pd.notna(sharpe) else np.nan,
        "MaxDrawdown%": round(mdd, 2),
        "VaR_1d_95_hist%": round(var1d, 2),
        "VaR_5d_95_hist%": round(var5d, 2),
        "VaR_1d_95_param%": round(parametric_var1d, 2),
        "BestDay%": round(best, 2),
        "WorstDay%": round(worst, 2),
    }


def _notes_df() -> pd.DataFrame:
    rows = [
        ("Lookback",      f"{LOOKBACK_DAYS} calendar days (~252 trading days)"),
        ("Benchmark",     "^NSEI (Nifty 50 TRI proxy via Close)"),
        ("Beta",          "Cov(stock, bench) / Var(bench) on daily log-pct returns"),
        ("Portfolio Beta", "Sum(weight_i * beta_i) using current PresentValue weights"),
        ("VaR (historical)", "95th-percentile loss of empirical daily returns"),
        ("VaR (parametric)", "1.645 * sigma (assumes normality — usually understates)"),
        ("Sharpe",        "Risk-free rate assumed 0; use as relative measure"),
        ("MaxDrawdown",   "Peak-to-trough on synthetic portfolio NAV (rebased 100)"),
        ("Caveat",        "Weights assumed CONSTANT through lookback (no rebalance)."),
    ]
    return pd.DataFrame(rows, columns=["Field", "Value"])


def run(verbose: bool = True) -> dict:
    if verbose:
        print("  [risk] Loading holdings …")
    h = load_holdings(verbose=False)
    if h.empty:
        return {"sheets": {"Risk Summary": pd.DataFrame(
            [{"Metric": "Risk", "Value": "No holdings"}])}}

    h = h[h["PresentValue"].fillna(0) > 0].copy()
    h = (h.groupby("Symbol", as_index=False)
           .agg({"Company": "first", "Sector": "first", "Series": "first",
                 "PresentValue": "sum"}))
    total = h["PresentValue"].sum()
    h["Weight"] = h["PresentValue"] / total if total else 0

    if verbose:
        print(f"  [risk] Pulling {LOOKBACK_DAYS}d prices for "
              f"{len(h)} positions + ^NSEI …")
    panel = fetch_close_panel(zip(h["Symbol"], h.get("Series", "EQ")),
                              lookback_days=LOOKBACK_DAYS, verbose=verbose)
    bench = fetch_benchmark("^NSEI", lookback_days=LOOKBACK_DAYS)
    if panel.empty or bench is None or bench.empty:
        return {"sheets": {"Risk Summary": pd.DataFrame(
            [{"Metric": "Risk", "Value": "Insufficient price data"}])}}

    returns = panel.pct_change().dropna(how="all")
    bench_ret = bench.pct_change().dropna()

    per_pos = _per_position_stats(returns, bench_ret)

    weights = h.set_index("Symbol")["Weight"]
    port_ret = _portfolio_nav(returns, weights)
    bench_ret_aligned = bench_ret.reindex(port_ret.index).dropna()
    port_ret = port_ret.reindex(bench_ret_aligned.index).dropna()

    # Portfolio beta = weighted sum of available per-position betas
    if not per_pos.empty:
        beta_map = per_pos.set_index("Symbol")["Beta_vs_Nifty"].dropna()
        common = weights.index.intersection(beta_map.index)
        if len(common):
            w_common = weights.loc[common] / weights.loc[common].sum()
            port_beta = float((beta_map.loc[common] * w_common).sum())
        else:
            port_beta = np.nan
    else:
        port_beta = np.nan

    summary = _summary_metrics(port_ret, bench_ret_aligned, port_beta)

    # Decorate per-position with weight & contribution
    if not per_pos.empty:
        per_pos = per_pos.merge(
            h[["Symbol", "Company", "Sector", "PresentValue", "Weight"]],
            on="Symbol", how="left")
        per_pos["Weight%"] = (per_pos["Weight"] * 100).round(2)
        per_pos["BetaContribution"] = (
            per_pos["Beta_vs_Nifty"] * per_pos["Weight"]).round(3)
        per_pos = per_pos[[
            "Symbol", "Company", "Sector", "PresentValue", "Weight%",
            "DataPts", "Beta_vs_Nifty", "BetaContribution",
            "AnnVol%", "MaxDD%",
        ]].sort_values("Weight%", ascending=False)

    summary_df = pd.DataFrame(
        [{"Metric": k, "Value": v} for k, v in summary.items()])

    return {"sheets": {
        "Risk (per position)": per_pos if not per_pos.empty else
            pd.DataFrame([{"Symbol": "—", "Note": "no data"}]),
        "Risk Summary": summary_df,
        "Risk Notes": _notes_df(),
    }}


def main():
    ap = argparse.ArgumentParser(description="Portfolio Risk Metrics")
    ap.add_argument("--out", default=str(PORTFOLIO_DIR / "risk_metrics.xlsx"))
    args = ap.parse_args()
    result = run()
    with pd.ExcelWriter(args.out, engine="openpyxl") as w:
        for name, df in result["sheets"].items():
            df.to_excel(w, sheet_name=name[:31], index=False)
    print(f"\n  ✓ Wrote {args.out}")


if __name__ == "__main__":
    main()
