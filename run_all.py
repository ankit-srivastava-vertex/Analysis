"""
Master Report Runner (Orchestrator)
====================================

SUMMARY
-------
Command-centre script that runs all market analysis scenarios in sequence,
consolidates their outputs into a single unified Excel workbook, and sends
one email with the workbook + interactive HTML charts attached.

WORKFLOW
--------
1. Parse CLI args (--no-email, --skip <scenarios>).
2. Run 8 scenarios in order:

   a. bulk_block        → BulkBlock.BSEScraper          — NSE+BSE bulk & block deals,
                                                          filtered to a hardcoded
                                                          "superstar" client list.
                                                          Standalone Excel emission is
                                                          SUPPRESSED via a _CapturingScraper
                                                          subclass. Sheets prefixed "BB ".
   b. sector_index      → custom_sector_index.run()     — Custom equal-weighted sector
                                                          indices (Sector Idx Summary +
                                                          Sector Idx Values).
   c. fii_flows         → fii_flows.run()               — Daily FII equity cash flows
                                                          (FII Flow Summary + FII Daily Data).
   d. fii_sector_flows  → fii_sector_flows.run()        — Fortnightly FII sector-wise flows
                                                          (FII Sector Net Flows + Detail).
   e. sector_momentum   → sector_momentum.run()         — Mansfield RS per sector
                                                          (RS Ranking + RS History).
   f. rrg               → rrg_chart.run()               — Relative Rotation Graph for 8
                                                          timeframes (RRG 3 Day … Quarterly).
   g. ipo_anchor        → ipo_anchor_tracker.run()       — Last-15-month IPOs (NSE + NSE SME)
                                                          with listing-day +/- and watchlist
                                                          anchor matches (sheets prefixed
                                                          "IPO Anchor"). Also writes a
                                                          standalone TradingView watchlist
                                                          file ipo_anchor_report.txt.
   h. india_macro       → india_macro.main()            — India macro dashboard (33
                                                          indicators). Produces its own
                                                          standalone Excel + HTML chart
                                                          (not merged into unified workbook).

   Each scenario is wrapped in try/except so a single failure does not
   abort the pipeline; failures are collected in `errors` and reported
   in the email body + summary.

   NOTE: multi_pct_down is now integrated directly into
   breakout_scanner_angel.py (runs inline as Universe 1). Run that
   script separately for the combined breakout output.

3. Merge every scenario's sheets into one Excel workbook
   (market_analysis_report.xlsx). Sub-module standalone Excel files are
   removed after their data is captured, so only the unified workbook
   remains on disk.

4. Collect all HTML chart files (6 charts: sector_index, fii_flows,
   fii_sector_flows, sector_momentum, rrg, india_macro_dashboard).

5. Send consolidated email with the unified Excel + india_macro Excel +
   HTML charts attached (unless --no-email).

DATA SOURCES
------------
All data is fetched by the individual sub-modules (see each file's header).
This script only orchestrates and consolidates — it does not call any
external APIs directly.

OUTPUT
------
- market_analysis_report.xlsx    — Unified workbook, typically ~18 sheets:
                                    4 BB (bulk/block) + 2 sector_index +
                                    2 fii_flows + 2 fii_sector_flows +
                                    2 sector_momentum + 8 RRG timeframes.
- india_macro_data.xlsx          — Standalone India Macro workbook (34 sheets).
- *_chart.html                   — 6 interactive Plotly charts.

USAGE
-----
    python3 run_all.py                                       # run all + send email
    python3 run_all.py --no-email                            # run all, skip email
    python3 run_all.py --skip bulk_block rrg                 # skip arbitrary scenarios

Available scenario names for --skip:
    bulk_block, sector_index, fii_flows, fii_sector_flows,
    sector_momentum, rrg, ipo_anchor, india_macro

DEPENDENCIES
------------
pandas, openpyxl, email_sender, and all sub-module dependencies
(BulkBlock requires requests + bs4).
"""

import os
import sys
import datetime
import argparse
import traceback
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TODAY = datetime.date.today()
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# Scenario names for --skip (order = sheet order in unified Excel)
ALL_SCENARIOS = ["bulk_block", "sector_index",
                 "fii_flows", "fii_sector_flows",
                 "sector_momentum", "rrg", "ipo_anchor", "india_macro"]


# ─── Scenario runners ──────────────────────────────────────

def run_bulk_block():
    """Scrape NSE+BSE bulk/block deals filtered for superstar names.
    Returns sheets dict + None (no chart).
    Captures the scraped DataFrames in-memory; suppresses BulkBlock's
    own standalone Excel file so we only emit the unified workbook.
    """
    from BulkBlock import BSEScraper
    captured = {}

    class _CapturingScraper(BSEScraper):
        def save_to_excel(self, dataframes_dict, filename):
            # Capture only — do not write a standalone file.
            captured.update(dataframes_dict)
            print("  (run_all) captured %d BulkBlock sheet(s); standalone Excel suppressed."
                  % len(dataframes_dict))

    scraper = _CapturingScraper()
    scraper.run()

    # Normalise sheet names: prefix with "BB " so they group together
    # at the top of the unified workbook.
    sheets = {}
    name_map = {
        "nse_bulk": "BB NSE Bulk",
        "nse_block": "BB NSE Block",
        "BSE Bulk Deals": "BB BSE Bulk",
        "BSE Block Deals": "BB BSE Block",
    }
    for raw_name, df in captured.items():
        clean = name_map.get(raw_name, "BB " + str(raw_name))
        if df is None or (hasattr(df, "empty") and df.empty):
            df = pd.DataFrame({"Note": ["No matching deals"]})
        sheets[clean[:31]] = df
    return sheets, None


def run_sector_index():
    """Run Custom Sector Index Builder. Returns sheets dict + chart path."""
    from custom_sector_index import run as csi_run
    prefix = os.path.join(SCRIPT_DIR, "custom_sector_index")
    result = csi_run(output_prefix=prefix)
    if result is None or result[0] is None:
        return {}, None

    all_indices, all_prices, summary_df, fig, excel_path, html_path = result

    sheets = {}
    sheets["Sector Idx Summary"] = summary_df
    idx_df = pd.DataFrame(all_indices)
    idx_df.index.name = "Date"
    sheets["Sector Idx Values"] = idx_df

    # Clean up individual Excel (data goes into unified Excel)
    if os.path.exists(excel_path):
        os.remove(excel_path)

    return sheets, html_path


def run_fii_flows():
    """Run FII Equity Cash Market Tracker. Returns sheets dict + chart path."""
    from fii_flows import run as fii_run
    prefix = os.path.join(SCRIPT_DIR, "fii_flows")
    result = fii_run(output_prefix=prefix)
    if result is None:
        return {}, None

    equity_df, oi_df, fig, excel_path, html_path = result

    sheets = {}
    edf = equity_df.copy()
    edf["FII_Cumulative_Cr"] = edf["FII_Net_Cr"].cumsum()

    # Summary sheet
    latest = edf.iloc[-1]
    summary_data = {
        "Metric": [
            "Date Range",
            "Trading Days",
            "Latest Net (₹ Cr)",
            "Cumulative Net (₹ Cr)",
            "Avg Daily Net (₹ Cr)",
        ],
        "Value": [
            "%s to %s" % (
                edf["Date"].min().strftime("%d-%b-%Y")
                if hasattr(edf["Date"].min(), "strftime")
                else str(edf["Date"].min()),
                edf["Date"].max().strftime("%d-%b-%Y")
                if hasattr(edf["Date"].max(), "strftime")
                else str(edf["Date"].max()),
            ),
            len(edf),
            latest["FII_Net_Cr"],
            latest["FII_Cumulative_Cr"],
            round(edf["FII_Net_Cr"].mean(), 2),
        ],
    }
    sheets["FII Flow Summary"] = pd.DataFrame(summary_data)
    sheets["FII Daily Data"] = edf

    if os.path.exists(excel_path):
        os.remove(excel_path)

    return sheets, html_path


def run_fii_sector_flows():
    """Run FII Sector-wise Flows. Returns sheets dict + chart path."""
    from fii_sector_flows import run as fsf_run
    prefix = os.path.join(SCRIPT_DIR, "fii_sector_flows")
    result = fsf_run(output_prefix=prefix)
    if result is None:
        return {}, None

    sector_totals, detail_df, fig, chart_path, excel_path = result

    sheets = {}
    sheets["FII Sector Net Flows"] = sector_totals.sort_values(
        "Net_Cr", ascending=False).copy()
    if not detail_df.empty:
        sheets["FII Sector Detail"] = detail_df

    if os.path.exists(excel_path):
        os.remove(excel_path)

    return sheets, chart_path


def run_sector_momentum():
    """Run Sector Momentum & RS Analyzer. Returns sheets dict + chart path."""
    from sector_momentum import run as sm_run
    prefix = os.path.join(SCRIPT_DIR, "sector_momentum")
    result = sm_run(output_prefix=prefix)
    if result is None:
        return {}, None

    all_rs, all_indices, ranking_df, fig, excel_path, html_path = result

    sheets = {}
    sheets["RS Ranking"] = ranking_df

    rs_df = pd.DataFrame(all_rs)
    rs_df.index.name = "Date"
    sheets["RS History"] = rs_df

    if os.path.exists(excel_path):
        os.remove(excel_path)

    return sheets, html_path


def run_rrg():
    """Run RRG Chart. Returns sheets dict + chart path."""
    from rrg_chart import run as rrg_run
    prefix = os.path.join(SCRIPT_DIR, "rrg_chart")
    result = rrg_run(output_prefix=prefix)
    if result is None:
        return {}, None

    all_timeframe_data, fig, excel_path, html_path = result

    sheets = {}
    for tf_name, sector_data in all_timeframe_data.items():
        rows = []
        for sector in sorted(sector_data.keys()):
            df = sector_data[sector]
            if df.empty:
                continue
            x = df["RS_Ratio"].iloc[-1]
            y = df["RS_Momentum"].iloc[-1]
            q = "Leading" if x >= 100 and y >= 100 else \
                "Weakening" if x >= 100 else \
                "Lagging" if y < 100 else "Improving"
            rows.append({
                "Sector": sector,
                "RS-Ratio": round(x, 2),
                "RS-Momentum": round(y, 2),
                "Quadrant": q,
            })
        if rows:
            sheet_name = "RRG %s" % tf_name
            sheets[sheet_name[:31]] = pd.DataFrame(rows).sort_values(
                "RS-Ratio", ascending=False)

    if os.path.exists(excel_path):
        os.remove(excel_path)

    return sheets, html_path


def run_ipo_anchor():
    """Run IPO Anchor Tracker. Returns sheets dict + None (no chart).
    Sheets: 'IPOs' (last-15-month listings + watchlist anchor matches) and
    'Notes' (methodology). The TradingView .txt watchlist is written by
    the underlying module to ipo_anchor_report.txt and is NOT merged into
    the unified workbook (kept as a standalone file for upload).
    """
    from ipo_anchor_tracker import run as ipo_run
    result = ipo_run()
    sheets_in = result.get("sheets", {})
    # Prefix sheets so they group together in the unified workbook.
    sheets = {}
    if "IPOs" in sheets_in:
        sheets["IPO Anchor List"] = sheets_in["IPOs"]
    if "Notes" in sheets_in:
        sheets["IPO Anchor Notes"] = sheets_in["Notes"]
    return sheets, None


def run_india_macro():
    """Run India Macro Dashboard. Returns (excel_path, chart_path).
    This scenario produces its own standalone Excel + HTML chart and does NOT
    merge into the unified workbook.
    """
    import india_macro
    # Run all direct fetchers then build the dashboard
    saved_argv = sys.argv
    try:
        sys.argv = ["india_macro.py", "--fetch-direct"]
        india_macro.main()
    finally:
        sys.argv = saved_argv

    excel_path = os.path.join(SCRIPT_DIR, "india_macro_data.xlsx")
    chart_path = os.path.join(SCRIPT_DIR, "india_macro_dashboard.html")

    excel_out = excel_path if os.path.exists(excel_path) else None
    chart_out = chart_path if os.path.exists(chart_path) else None
    return excel_out, chart_out


# ─── Unified Excel builder ──────────────────────────────────────────────────

def build_unified_excel(all_sheets, output_path):
    """Write all scenario sheets into one Excel workbook."""
    if not all_sheets:
        print("  No data to write to unified Excel.")
        return None

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for sheet_name, df in all_sheets.items():
            # Excel sheet name limit is 31 chars
            safe_name = sheet_name[:31]
            if hasattr(df, "index") and df.index.name == "Date":
                df.to_excel(writer, sheet_name=safe_name)
            else:
                df.to_excel(writer, sheet_name=safe_name, index=False)

    print("  Unified Excel: %s (%d sheets)" % (output_path, len(all_sheets)))
    return output_path


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Master Report Runner")
    parser.add_argument("--no-email", action="store_true",
                        help="Skip sending email")
    parser.add_argument("--skip", nargs="*", default=[],
                        choices=ALL_SCENARIOS,
                        help="Scenarios to skip")
    args = parser.parse_args()

    skip = set(args.skip)

    print("=" * 70)
    print("  MASTER REPORT RUNNER — %s" % TODAY.strftime("%d-%b-%Y"))
    print("=" * 70)

    unified_sheets = {}
    chart_files = []
    errors = []

    # ── 1. Bulk & Block Deals (NSE + BSE) ─────────────────────────
    if "bulk_block" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 1/8: Bulk & Block Deals (NSE + BSE)")
        print("=" * 70)
        try:
            sheets, chart = run_bulk_block()
            unified_sheets.update(sheets)
            if chart:
                chart_files.append(chart)
            print("  ✓ Bulk & Block Deals complete (%d sheets)" % len(sheets))
        except Exception as e:
            errors.append("bulk_block: %s" % e)
            print("  ✗ Bulk & Block Deals FAILED: %s" % e)
            traceback.print_exc()

    # ── 2. Custom Sector Index ─────────────────────────────────
    if "sector_index" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 2/8: Custom Sector Index")
        print("=" * 70)
        try:
            sheets, chart = run_sector_index()
            unified_sheets.update(sheets)
            if chart:
                chart_files.append(chart)
            print("  ✓ Sector Index complete")
        except Exception as e:
            errors.append("sector_index: %s" % e)
            print("  ✗ Sector Index FAILED: %s" % e)
            traceback.print_exc()

    # ── 3. FII Equity Flows ────────────────────────────────────
    if "fii_flows" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 3/8: FII Equity Cash Market Flows")
        print("=" * 70)
        try:
            sheets, chart = run_fii_flows()
            unified_sheets.update(sheets)
            if chart:
                chart_files.append(chart)
            print("  ✓ FII Flows complete")
        except Exception as e:
            errors.append("fii_flows: %s" % e)
            print("  ✗ FII Flows FAILED: %s" % e)
            traceback.print_exc()

    # ── 4. FII Sector-wise Flows ─────────────────────────────────
    if "fii_sector_flows" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 4/8: FII Sector-wise Flows")
        print("=" * 70)
        try:
            sheets, chart = run_fii_sector_flows()
            unified_sheets.update(sheets)
            if chart:
                chart_files.append(chart)
            print("  ✓ FII Sector Flows complete")
        except Exception as e:
            errors.append("fii_sector_flows: %s" % e)
            print("  ✗ FII Sector Flows FAILED: %s" % e)
            traceback.print_exc()

    # ── 5. Sector Momentum ─────────────────────────────────────
    if "sector_momentum" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 5/8: Sector Momentum & Relative Strength")
        print("=" * 70)
        try:
            sheets, chart = run_sector_momentum()
            unified_sheets.update(sheets)
            if chart:
                chart_files.append(chart)
            print("  ✓ Sector Momentum complete")
        except Exception as e:
            errors.append("sector_momentum: %s" % e)
            print("  ✗ Sector Momentum FAILED: %s" % e)
            traceback.print_exc()

    # ── 6. RRG Chart ────────────────────────────────────────────
    if "rrg" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 6/8: Relative Rotation Graph")
        print("=" * 70)
        try:
            sheets, chart = run_rrg()
            unified_sheets.update(sheets)
            if chart:
                chart_files.append(chart)
            print("  ✓ RRG Chart complete")
        except Exception as e:
            errors.append("rrg: %s" % e)
            print("  ✗ RRG Chart FAILED: %s" % e)
            traceback.print_exc()

    # ── 7. IPO Anchor Tracker ──────────────────────────────────
    if "ipo_anchor" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 7/8: IPO Anchor Tracker")
        print("=" * 70)
        try:
            sheets, chart = run_ipo_anchor()
            unified_sheets.update(sheets)
            if chart:
                chart_files.append(chart)
            print("  ✓ IPO Anchor Tracker complete")
        except Exception as e:
            errors.append("ipo_anchor: %s" % e)
            print("  ✗ IPO Anchor Tracker FAILED: %s" % e)
            traceback.print_exc()

    # ── 8. India Macro Dashboard ─────────────────────────────────────────
    india_macro_excel = None
    india_macro_chart = None
    if "india_macro" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 8/8: India Macro Dashboard")
        print("=" * 70)
        try:
            india_macro_excel, india_macro_chart = run_india_macro()
            if india_macro_chart:
                chart_files.append(india_macro_chart)
            print("  ✓ India Macro Dashboard complete")
        except Exception as e:
            errors.append("india_macro: %s" % e)
            print("  ✗ India Macro FAILED: %s" % e)
            traceback.print_exc()

    # ── Build Unified Excel ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  BUILDING OUTPUTS")
    print("=" * 70)

    unified_excel_path = os.path.join(
        SCRIPT_DIR, "market_analysis_report.xlsx")

    if unified_sheets:
        build_unified_excel(unified_sheets, unified_excel_path)
    else:
        unified_excel_path = None
        print("  No unified Excel data to write.")

    # ── 8. Send Email ────────────────────────────────────────────────────
    if not args.no_email:
        print("\n" + "=" * 70)
        print("  SENDING EMAIL")
        print("=" * 70)

        from email_sender import send_report

        attachments = []
        if unified_excel_path and os.path.exists(unified_excel_path):
            attachments.append(unified_excel_path)
        if india_macro_excel and os.path.exists(india_macro_excel):
            attachments.append(india_macro_excel)
        attachments.extend([f for f in chart_files if os.path.exists(f)])

        subject = "Daily Market Analysis Report — %s" % TODAY.strftime("%d-%b-%Y")

        body_lines = [
            "Daily Market Analysis Report — %s" % TODAY.strftime("%d-%b-%Y"),
            "",
            "Attached reports:",
        ]
        if unified_excel_path:
            body_lines.append("  • Market Analysis Report (Excel) — %d sheets" %
                              len(unified_sheets))
        for cf in chart_files:
            body_lines.append("  • %s (Interactive Chart)" % os.path.basename(cf))

        if errors:
            body_lines.append("")
            body_lines.append("Scenarios with errors:")
            for err in errors:
                body_lines.append("  ✗ %s" % err)

        body_text = "\n".join(body_lines)

        sent = send_report(
            subject=subject,
            body_text=body_text,
            attachments=attachments,
        )
        if not sent:
            print("  Email not sent (check EMAIL_* env vars).")
    else:
        print("\n  --no-email: Skipping email send.")

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  SUMMARY — %s" % TODAY.strftime("%d-%b-%Y"))
    print("=" * 70)

    if unified_excel_path:
        print("  Unified Excel : %s" % os.path.basename(unified_excel_path))
    for cf in chart_files:
        print("  Chart         : %s" % os.path.basename(cf))
    if errors:
        print("\n  ERRORS (%d):" % len(errors))
        for err in errors:
            print("    • %s" % err)
    else:
        print("\n  All scenarios completed successfully!")

    print("\nDONE!")


if __name__ == "__main__":
    main()
