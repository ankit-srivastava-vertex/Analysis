"""
Master Report Runner
====================
Runs all analysis scenarios, creates consolidated outputs, and
sends a single email with all reports + charts attached.

Outputs:
  - market_analysis_report.xlsx   (sheets from: sector index, FII flows,
                                   FII sector flows, sector momentum)
  - pct_down_report.xlsx          (percentage down screener — separate)
  - *_chart.html                  (one interactive chart per scenario)

Usage:
  python run_all.py               # Run all, save files + send email
  python run_all.py --no-email    # Run all, save files only
  python run_all.py --skip fii_flows pct_down   # Skip specific scenarios
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

# Scenario names for --skip
ALL_SCENARIOS = ["sector_index", "fii_flows", "fii_sector_flows",
                 "sector_momentum", "pct_down", "rrg"]


# ─── Scenario runners ───────────────────────────────────────────────────────

def run_sector_index():
    """Run Custom Sector Index Builder. Returns sheets dict + chart path."""
    from custom_sector_index import run as csi_run
    prefix = os.path.join(SCRIPT_DIR, "_tmp_csi_%s" % TIMESTAMP)
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
    prefix = os.path.join(SCRIPT_DIR, "_tmp_fii_%s" % TIMESTAMP)
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
    prefix = os.path.join(SCRIPT_DIR, "_tmp_fsf_%s" % TIMESTAMP)
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
    prefix = os.path.join(SCRIPT_DIR, "_tmp_sm_%s" % TIMESTAMP)
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


def run_pct_down():
    """Run Percentage Down Screener. Returns Excel path (separate workbook)."""
    from percentage_down import run as pd_run
    prefix = os.path.join(SCRIPT_DIR, "pct_down_report")
    result = pd_run(output_prefix=prefix)
    if result is None:
        return None

    tables, common, all4, excel_path = result
    return excel_path


def run_rrg():
    """Run RRG Chart. Returns sheets dict + chart path."""
    from rrg_chart import run as rrg_run
    prefix = os.path.join(SCRIPT_DIR, "_tmp_rrg_%s" % TIMESTAMP)
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
    pct_down_excel = None
    errors = []

    # ── 1. Custom Sector Index ───────────────────────────────────────────
    if "sector_index" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 1/6: Custom Sector Index")
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

    # ── 2. FII Equity Flows ──────────────────────────────────────────────
    if "fii_flows" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 2/6: FII Equity Cash Market Flows")
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

    # ── 3. FII Sector-wise Flows ─────────────────────────────────────────
    if "fii_sector_flows" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 3/6: FII Sector-wise Flows")
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

    # ── 4. Sector Momentum ───────────────────────────────────────────────
    if "sector_momentum" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 4/6: Sector Momentum & Relative Strength")
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

    # ── 5. Percentage Down Screener ──────────────────────────────────────
    if "pct_down" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 5/6: Percentage Down Screener")
        print("=" * 70)
        try:
            pct_down_excel = run_pct_down()
            if pct_down_excel:
                print("  ✓ Percentage Down complete")
            else:
                errors.append("pct_down: returned no data")
                print("  ✗ Percentage Down returned no data")
        except Exception as e:
            errors.append("pct_down: %s" % e)
            print("  ✗ Percentage Down FAILED: %s" % e)
            traceback.print_exc()

    # ── 6. RRG Chart ─────────────────────────────────────────────────────
    if "rrg" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 6/6: Relative Rotation Graph")
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

    # ── 7. Build Unified Excel ───────────────────────────────────────────
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
        if pct_down_excel and os.path.exists(pct_down_excel):
            attachments.append(pct_down_excel)
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
        if pct_down_excel:
            body_lines.append("  • Percentage Down Screener (Excel)")
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
    if pct_down_excel:
        print("  Pct Down Excel: %s" % os.path.basename(pct_down_excel))
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
