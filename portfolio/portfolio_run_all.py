"""
portfolio_run_all.py — Master Portfolio Runner (Orchestrator)
==============================================================

SUMMARY
-------
The portfolio twin of run_all.py. Runs every portfolio/ scenario in
sequence, consolidates their sheets into one unified Excel workbook
(portfolio_report.xlsx), and (optionally) emails it.

WORKFLOW
--------
1. Parse CLI args (--no-email).
2. Run 9 scenarios in order:

   1. portfolio_tracker    → P&L, sector exposure, concentration
   2. position_health      → DMA/RSI/drawdown technical scan
   3. sl_target_tracker    → user-defined SL/Target hit alerts
                             (reads/maintains portfolio/holdings_meta.csv)
   4. risk_metrics         → portfolio beta, VaR (1d/5d 95%), Sharpe,
                             max drawdown, per-position vol & beta
   5. correlation_clusters → return-correlation pairs & greedy clusters
                             (hidden concentration vs. sector view)
   6. pledge_promoter      → pledge % + promoter holding red flags
                             (Tickertape screener API)
   7. mf_overlap           → crowding from MF holdings overlap
                             (reads portfolio/mf_holdings.csv)
   8. events_calendar      → owned-name corp events (NSE)
   9. premarket_dashboard  → global cues, FX/commodities, NIFTY 500
                             breadth + 6-month chart

   Each scenario is wrapped in try/except — a single failure does not
   abort the pipeline; failures are reported in the email body + summary.

3. Merge all sheets into portfolio_report.xlsx (inside portfolio/).
4. Send a consolidated email (unless --no-email) using the existing
   email_sender utility (same SMTP config as run_all.py).

DATA SOURCES (full pipeline)
----------------------------
- Broker holdings xlsx  : Angel One holdings.xlsx  (auto-discover; portfolio/
                          folder, project root, or ~/Downloads)
                         : Groww  Stocks_Holdings_Statement.xlsx
- ISIN -> NSE Symbol    : NSE EQUITY_L.csv + SME_EQUITY_L.csv (cached 7d)
                          https://archives.nseindia.com/content/equities/
- OHLCV / Last close    : data_provider (Angel SmartAPI -> jugaad-data ->
                          yfinance) — same as run_all.py
- Corporate events      : NSE public APIs
                          /api/corporate-board-meetings
                          /api/corporates-corporateActions
                          /api/corporate-announcements
- Pre-market quotes     : Yahoo Finance via data_provider
                          (^GSPC, ^IXIC, ^DJI, ^N225, ^HSI, ^FTSE,
                           ^NSEI, ^NSEBANK, ^INDIAVIX,
                           INR=X, DX-Y.NYB, BZ=F, GC=F, HG=F, ^TNX)
- Breadth universe      : Official NIFTY 500 from NSE
                          ind_nifty500list.csv (cached 7d in
                          portfolio/.cache/)

OUTPUT
------
- portfolio/portfolio_report.xlsx — Unified workbook (~16 sheets), written
                                    inside the portfolio/ folder
                                    (alongside the broker xlsx files).
- portfolio/premarket_dashboard_chart.html — 4-panel breadth chart.
- Optional email with both attached.

USAGE
-----
    python3 portfolio/portfolio_run_all.py                    # all + email
    python3 portfolio/portfolio_run_all.py --no-email         # dry run

All four scenarios always run; the breadth pass (NIFTY-500 sample in
pre-market) always runs too.

DEPENDENCIES
------------
pandas, openpyxl, requests, plus all sub-module deps (data_provider,
holdings_loader). Email: smtplib (stdlib).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import traceback
from pathlib import Path

import pandas as pd

PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TODAY = dt.date.today()


# ─── Scenario runners ──────────────────────────────────────────

def run_portfolio_tracker():
    from portfolio.portfolio_tracker import run as t_run
    return t_run().get("sheets", {})


def run_position_health():
    from portfolio.position_health import run as h_run
    return h_run().get("sheets", {})


def run_events_calendar():
    from portfolio.events_calendar import run as e_run
    return e_run().get("sheets", {})


def run_premarket():
    from portfolio.premarket_dashboard import run as p_run
    res = p_run()
    return res.get("sheets", {}), res.get("chart")


def run_risk_metrics():
    from portfolio.risk_metrics import run as r_run
    return r_run().get("sheets", {})


def run_correlation_clusters():
    from portfolio.correlation_clusters import run as c_run
    return c_run().get("sheets", {})


def run_pledge_promoter():
    from portfolio.pledge_promoter import run as pp_run
    return pp_run().get("sheets", {})


def run_sl_target():
    from portfolio.sl_target_tracker import run as sl_run
    return sl_run().get("sheets", {})


def run_mf_overlap():
    from portfolio.mf_overlap import run as mf_run
    return mf_run().get("sheets", {})


# ─── Unified Excel builder ─────────────────────────────────────

def build_unified_excel(all_sheets: dict, output_path: Path):
    if not all_sheets:
        print("  No data to write to unified Excel.")
        return None
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for name, df in all_sheets.items():
            safe = name[:31]
            if hasattr(df, "index") and df.index.name == "Date":
                df.to_excel(writer, sheet_name=safe)
            else:
                df.to_excel(writer, sheet_name=safe, index=False)
    print(f"  Unified Excel: {output_path} ({len(all_sheets)} sheets)")
    return output_path


# ─── Main ──────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Master Portfolio Runner")
    ap.add_argument("--no-email", action="store_true", help="Skip email send")
    args = ap.parse_args()

    print("=" * 70)
    print(f"  MASTER PORTFOLIO RUNNER — {TODAY.strftime('%d-%b-%Y')}")
    print("=" * 70)

    unified: dict = {}
    chart_files: list = []
    errors: list = []

    runners = [
        ("portfolio_tracker",   "1/9: Portfolio Tracker (P&L, exposure, concentration)",
         run_portfolio_tracker),
        ("position_health",     "2/9: Position Health (technical scan)",
         run_position_health),
        ("sl_target",           "3/9: Stop-Loss / Target Monitor",
         run_sl_target),
        ("risk_metrics",        "4/9: Risk Metrics (beta, VaR, Sharpe, MDD)",
         run_risk_metrics),
        ("correlation_clusters","5/9: Correlation & Cluster Concentration",
         run_correlation_clusters),
        ("pledge_promoter",     "6/9: Pledge & Promoter Holding Scan",
         run_pledge_promoter),
        ("mf_overlap",          "7/9: Mutual-Fund Overlap (crowding)",
         run_mf_overlap),
        ("events_calendar",     "8/9: Events Calendar (owned-name corp events)",
         run_events_calendar),
        ("premarket",           "9/9: Pre-Market Dashboard (global cues + breadth)",
         run_premarket),
    ]

    for name, label, fn in runners:
        print("\n" + "=" * 70)
        print(f"  SCENARIO {label}")
        print("=" * 70)
        try:
            res = fn()
            if isinstance(res, tuple):
                sheets, chart = res
            else:
                sheets, chart = res, None
            unified.update(sheets)
            if chart:
                chart_files.append(chart)
            print(f"  ✓ {name} complete ({len(sheets)} sheets)")
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"  ✗ {name} FAILED: {e}")
            traceback.print_exc()

    print("\n" + "=" * 70)
    print("  BUILDING OUTPUT")
    print("=" * 70)
    out = PORTFOLIO_DIR / "portfolio_report.xlsx"
    excel_path = build_unified_excel(unified, out) if unified else None

    if not args.no_email:
        print("\n" + "=" * 70)
        print("  SENDING EMAIL")
        print("=" * 70)
        try:
            from email_sender import send_report
            attachments = [str(excel_path)] if excel_path and excel_path.exists() else []
            attachments.extend(c for c in chart_files if c and Path(c).exists())
            subject = f"Portfolio Report — {TODAY.strftime('%d-%b-%Y')}"
            body_lines = [
                f"Portfolio Report — {TODAY.strftime('%d-%b-%Y')}",
                "",
                "Attached:",
            ]
            if excel_path:
                body_lines.append(f"  • portfolio_report.xlsx — {len(unified)} sheets")
            for cf in chart_files:
                body_lines.append(f"  • {Path(cf).name} (Interactive Chart)")
            if errors:
                body_lines.append("")
                body_lines.append("Scenarios with errors:")
                for err in errors:
                    body_lines.append(f"  ✗ {err}")
            sent = send_report(subject=subject,
                               body_text="\n".join(body_lines),
                               attachments=attachments)
            if not sent:
                print("  Email not sent (check EMAIL_* env vars).")
        except Exception as e:
            print(f"  Email send FAILED: {e}")
    else:
        print("\n  --no-email: Skipping email send.")

    print("\n" + "=" * 70)
    print(f"  SUMMARY — {TODAY.strftime('%d-%b-%Y')}")
    print("=" * 70)
    if excel_path:
        print(f"  Unified Excel : {excel_path.name}")
    for cf in chart_files:
        print(f"  Chart         : {Path(cf).name}")
    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for err in errors:
            print(f"    • {err}")
    else:
        print("\n  All scenarios completed successfully!")
    print("\nDONE!")


if __name__ == "__main__":
    main()
