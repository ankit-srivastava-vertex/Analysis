"""
Master Report Runner (Orchestrator)
====================================

SUMMARY
-------
Command-centre script that runs all market analysis scenarios in sequence.
BulkBlock produces the single output Excel workbook (with RS Ranking and
IPO Anchor List sheets appended). The other scenarios produce interactive
HTML charts only.

WORKFLOW
--------
1. Parse CLI args (--no-email, --skip <scenarios>).
2. Run 7 scenarios in order:

   a. bulk_block        → BulkBlock.BSEScraperWithEmail  — NSE+BSE bulk & block deals,
                                                           FII Stake Tracker sheets,
                                                           HNI holdings. Produces
                                                           BULK_BLOCK_Deals_<ts>.xlsx.
   b. sector_index      → custom_sector_index.run()      — Custom equal-weighted sector
                                                           indices. Chart only.
   c. fii_flows         → fii_flows.run()                — Daily FII equity cash flows.
                                                           Chart only.
   d. fii_sector_flows  → fii_sector_flows.run()         — Fortnightly FII sector-wise
                                                           flows. Chart only.
   e. sector_momentum   → sector_momentum.run()          — Mansfield RS per sector.
                                                           Chart + "RS Ranking" sheet
                                                           appended to BulkBlock Excel.
   f. rrg               → rrg_chart.run()                — Relative Rotation Graph.
                                                           Chart only.
   g. ipo_anchor        → ipo_anchor_tracker.run()       — Last-15-month IPOs with
                                                           anchor investor matching.
                                                           "IPO Anchor List" sheet
                                                           appended to BulkBlock Excel.

   Each scenario is wrapped in try/except so a single failure does not
   abort the pipeline.

   NOTE: multi_pct_down runs inline via breakout_scanner_angel.py.
   NOTE: fii_stake_tracker runs via BulkBlock.py.
   NOTE: india_macro.py runs independently (not part of this pipeline).

3. Append "RS Ranking" and "IPO Anchor List" sheets to the BulkBlock
   output Excel.

4. Collect 5 HTML chart files (sector_index, fii_flows, fii_sector_flows,
   sector_momentum, rrg).

5. Send email with BulkBlock Excel + 5 charts attached (unless --no-email).

DATA SOURCES
------------
All data is fetched by the individual sub-modules (see each file's header).
This script only orchestrates — it does not call any external APIs directly.

OUTPUT
------
- BULK_BLOCK_Deals_<timestamp>.xlsx  — Single output workbook containing:
                                        bulk/block deals, FII_Summary,
                                        FII_New_Entry, FII_1-4Q_Increasing,
                                        HNIs, RS Ranking, IPO Anchor List.
- *_chart.html                       — 5 interactive Plotly charts.

USAGE
-----
    python3 run_all.py                                       # run all + send email
    python3 run_all.py --no-email                            # run all, skip email
    python3 run_all.py --skip bulk_block rrg                 # skip arbitrary scenarios

Available scenario names for --skip:
    bulk_block, sector_index, fii_flows, fii_sector_flows,
    sector_momentum, rrg, ipo_anchor

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

# Scenario names for --skip
ALL_SCENARIOS = ["bulk_block", "sector_index",
                 "fii_flows", "fii_sector_flows",
                 "sector_momentum", "rrg", "ipo_anchor"]


# ─── Scenario runners ──────────────────────────────────────

def run_bulk_block():
    """Run BulkBlock scraper. Returns the output Excel path."""
    from BulkBlock import BSEScraper
    captured_filename = {}

    class _CapturingScraper(BSEScraper):
        def save_to_excel(self, dataframes_dict, filename):
            super().save_to_excel(dataframes_dict, filename)
            captured_filename["path"] = os.path.join(SCRIPT_DIR, filename)

    scraper = _CapturingScraper()
    scraper.run()
    return captured_filename.get("path")


def run_sector_index():
    """Run Custom Sector Index Builder. Returns (chart_path, None)."""
    from custom_sector_index import run as csi_run
    prefix = os.path.join(SCRIPT_DIR, "custom_sector_index")
    result = csi_run(output_prefix=prefix)
    if result is None or result[0] is None:
        return None
    _all_indices, _all_prices, _summary_df, _fig, excel_path, html_path = result
    # Remove standalone Excel — chart is the only output
    if excel_path and os.path.exists(excel_path):
        os.remove(excel_path)
    return html_path


def run_fii_flows():
    """Run FII Equity Cash Market Tracker. Returns chart_path."""
    from fii_flows import run as fii_run
    prefix = os.path.join(SCRIPT_DIR, "fii_flows")
    result = fii_run(output_prefix=prefix)
    if result is None:
        return None
    _equity_df, _oi_df, _fig, excel_path, html_path = result
    if excel_path and os.path.exists(excel_path):
        os.remove(excel_path)
    return html_path


def run_fii_sector_flows():
    """Run FII Sector-wise Flows. Returns chart_path."""
    from fii_sector_flows import run as fsf_run
    prefix = os.path.join(SCRIPT_DIR, "fii_sector_flows")
    result = fsf_run(output_prefix=prefix)
    if result is None:
        return None
    _sector_totals, _detail_df, _fig, chart_path, excel_path = result
    if excel_path and os.path.exists(excel_path):
        os.remove(excel_path)
    return chart_path


def run_sector_momentum():
    """Run Sector Momentum. Returns (rs_ranking_df, chart_path)."""
    from sector_momentum import run as sm_run
    prefix = os.path.join(SCRIPT_DIR, "sector_momentum")
    result = sm_run(output_prefix=prefix)
    if result is None:
        return None, None
    _all_rs, _all_indices, ranking_df, _fig, excel_path, html_path = result
    if excel_path and os.path.exists(excel_path):
        os.remove(excel_path)
    return ranking_df, html_path


def run_rrg():
    """Run RRG Chart. Returns chart_path."""
    from rrg_chart import run as rrg_run
    prefix = os.path.join(SCRIPT_DIR, "rrg_chart")
    result = rrg_run(output_prefix=prefix)
    if result is None:
        return None
    _all_timeframe_data, _fig, excel_path, html_path = result
    if excel_path and os.path.exists(excel_path):
        os.remove(excel_path)
    return html_path


def run_ipo_anchor():
    """Run IPO Anchor Tracker. Returns ipo_anchor_df."""
    from ipo_anchor_tracker import run as ipo_run
    result = ipo_run()
    sheets_in = result.get("sheets", {})
    return sheets_in.get("IPOs")


# ─── Combined chart builder ─────────────────────────────────────────────────

CHART_LABELS = {
    "custom_sector_index": "Sector Index",
    "fii_flows": "FII Flows",
    "fii_sector_flows": "FII Sector Flows",
    "sector_momentum": "Sector Momentum",
    "rrg_chart": "RRG Chart",
}


def build_combined_chart(chart_files):
    """Merge individual chart HTMLs into a single tabbed HTML file.

    Embeds each chart's full HTML as an iframe srcdoc panel with a
    tab-switching UI. Deletes the individual files afterwards.
    Returns the combined HTML path.
    """
    if not chart_files:
        return None

    combined_path = os.path.join(SCRIPT_DIR, "market_charts.html")

    # Determine label for each chart from its filename
    panels = []
    for path in chart_files:
        basename = os.path.basename(path)
        label = basename  # fallback
        for key, lbl in CHART_LABELS.items():
            if key in basename:
                label = lbl
                break
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        panels.append((label, content))

    # Build tabbed HTML
    tab_buttons = []
    tab_panels = []
    for i, (label, content) in enumerate(panels):
        active = " active" if i == 0 else ""
        tab_buttons.append(
            '  <button class="tab-btn%s" onclick="showTab(%d)">%s</button>'
            % (active, i, label)
        )
        display = "block" if i == 0 else "none"
        # Escape for srcdoc: replace " with &quot; and </script with escaped
        safe = (content
                .replace('&', '&amp;')
                .replace('"', '&quot;')
                .replace('<', '&lt;')
                .replace('>', '&gt;'))
        tab_panels.append(
            '<div class="tab-panel" id="panel-%d" style="display:%s">'
            '<iframe srcdoc="%s"></iframe></div>' % (i, display, safe)
        )

    html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Daily Market Analysis Charts — %s</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#f5f5f5; }
.tab-bar {
  display:flex; gap:4px; padding:12px 16px;
  background: linear-gradient(135deg, #1F4E79, #2E75B6);
  position:sticky; top:0; z-index:1000;
  box-shadow: 0 2px 8px rgba(0,0,0,0.3);
}
.tab-btn {
  padding:10px 22px; border:none; border-radius:6px 6px 0 0; cursor:pointer;
  font-size:14px; font-weight:600; color:#b0c4de; background:rgba(255,255,255,0.1);
  transition:all 0.2s;
}
.tab-btn:hover { color:#fff; background:rgba(255,255,255,0.2); }
.tab-btn.active { color:#fff; background:#e94560; box-shadow: 0 -2px 6px rgba(233,69,96,0.4); }
.tab-panel { width:100%%; height:calc(100vh - 60px); }
.tab-panel iframe { width:100%%; height:100%%; border:none; }
</style>
</head>
<body>
<div class="tab-bar">
%s
</div>
%s
<script>
function showTab(idx) {
  document.querySelectorAll('.tab-panel').forEach((p,i) => p.style.display = i===idx ? 'block' : 'none');
  document.querySelectorAll('.tab-btn').forEach((b,i) => b.classList.toggle('active', i===idx));
}
</script>
</body>
</html>""" % (TODAY.strftime("%d-%b-%Y"), "\n".join(tab_buttons), "\n".join(tab_panels))

    with open(combined_path, "w", encoding="utf-8") as f:
        f.write(html)

    # Remove individual chart files
    for path in chart_files:
        try:
            os.remove(path)
        except OSError:
            pass

    print("  ✓ Combined chart: %s (%d tabs)" % (os.path.basename(combined_path), len(panels)))
    return combined_path


# ─── Append sheets to existing Excel ────────────────────────────────────────

def append_sheets_to_excel(excel_path, extra_sheets):
    """Append additional DataFrames as new sheets to an existing Excel file."""
    if not excel_path or not os.path.exists(excel_path):
        print("  ⚠️  Cannot append sheets — Excel not found: %s" % excel_path)
        return
    if not extra_sheets:
        return

    from openpyxl import load_workbook
    wb = load_workbook(excel_path)
    with pd.ExcelWriter(excel_path, engine="openpyxl", mode="a",
                        if_sheet_exists="replace") as writer:
        writer.workbook = wb
        for name, df in extra_sheets.items():
            if df is not None and not df.empty:
                safe_name = name[:31]
                df.to_excel(writer, sheet_name=safe_name, index=False)
                print("  ✓ Appended sheet '%s': %d rows" % (safe_name, len(df)))


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

    bulkblock_excel = None
    chart_files = []
    extra_sheets = {}  # sheets to append to BulkBlock Excel
    errors = []

    # ── 1. Bulk & Block Deals (NSE + BSE + FII + HNI) ─────────────────────
    if "bulk_block" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 1/7: Bulk & Block Deals (NSE + BSE + FII + HNI)")
        print("=" * 70)
        try:
            bulkblock_excel = run_bulk_block()
            print("  ✓ Bulk & Block Deals complete → %s" %
                  (os.path.basename(bulkblock_excel) if bulkblock_excel else "N/A"))
        except Exception as e:
            errors.append("bulk_block: %s" % e)
            print("  ✗ Bulk & Block Deals FAILED: %s" % e)
            traceback.print_exc()

    # ── 2. Custom Sector Index ─────────────────────────────────
    if "sector_index" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 2/7: Custom Sector Index")
        print("=" * 70)
        try:
            chart = run_sector_index()
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
        print("  SCENARIO 3/7: FII Equity Cash Market Flows")
        print("=" * 70)
        try:
            chart = run_fii_flows()
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
        print("  SCENARIO 4/7: FII Sector-wise Flows")
        print("=" * 70)
        try:
            chart = run_fii_sector_flows()
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
        print("  SCENARIO 5/7: Sector Momentum & Relative Strength")
        print("=" * 70)
        try:
            ranking_df, chart = run_sector_momentum()
            if chart:
                chart_files.append(chart)
            if ranking_df is not None and not ranking_df.empty:
                extra_sheets["RS Ranking"] = ranking_df
            print("  ✓ Sector Momentum complete")
        except Exception as e:
            errors.append("sector_momentum: %s" % e)
            print("  ✗ Sector Momentum FAILED: %s" % e)
            traceback.print_exc()

    # ── 6. RRG Chart ────────────────────────────────────────────
    if "rrg" not in skip:
        print("\n" + "=" * 70)
        print("  SCENARIO 6/7: Relative Rotation Graph")
        print("=" * 70)
        try:
            chart = run_rrg()
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
        print("  SCENARIO 7/7: IPO Anchor Tracker")
        print("=" * 70)
        try:
            ipo_df = run_ipo_anchor()
            if ipo_df is not None and not ipo_df.empty:
                extra_sheets["IPO Anchor List"] = ipo_df
            print("  ✓ IPO Anchor Tracker complete")
        except Exception as e:
            errors.append("ipo_anchor: %s" % e)
            print("  ✗ IPO Anchor Tracker FAILED: %s" % e)
            traceback.print_exc()

    # ── Append extra sheets to BulkBlock Excel ────────────────────────────
    print("\n" + "=" * 70)
    print("  FINALIZING OUTPUT")
    print("=" * 70)

    if bulkblock_excel and extra_sheets:
        append_sheets_to_excel(bulkblock_excel, extra_sheets)
    elif extra_sheets and not bulkblock_excel:
        print("  ⚠️  BulkBlock Excel missing — cannot append RS Ranking / IPO sheets")

    # Build combined chart from individual chart files
    combined_chart = None
    if chart_files:
        combined_chart = build_combined_chart(chart_files)

    # ── Send Email ────────────────────────────────────────────────────────
    if not args.no_email:
        print("\n" + "=" * 70)
        print("  SENDING EMAIL")
        print("=" * 70)

        from email_sender import send_report

        attachments = []
        if bulkblock_excel and os.path.exists(bulkblock_excel):
            attachments.append(bulkblock_excel)
        if combined_chart and os.path.exists(combined_chart):
            attachments.append(combined_chart)

        subject = "Daily Market Analysis Report — %s" % TODAY.strftime("%d-%b-%Y")

        body_lines = [
            "Daily Market Analysis Report — %s" % TODAY.strftime("%d-%b-%Y"),
            "",
            "Attached reports:",
        ]
        if bulkblock_excel:
            body_lines.append("  • %s (Deals + FII + HNI + RS Ranking + IPO Anchors)"
                              % os.path.basename(bulkblock_excel))
        if combined_chart:
            body_lines.append("  • %s (5 Interactive Charts — tabbed)"
                              % os.path.basename(combined_chart))
        if errors:
            body_lines.append("")
            body_lines.append("Scenarios with errors:")
            for err in errors:
                body_lines.append("  ✗ %s" % err)

        body_text = "\n".join(body_lines)
        sent = send_report(subject=subject, body_text=body_text,
                           attachments=attachments)
        if not sent:
            print("  Email not sent (check EMAIL_* env vars).")
    else:
        print("\n  --no-email: Skipping email send.")

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  SUMMARY — %s" % TODAY.strftime("%d-%b-%Y"))
    print("=" * 70)

    if bulkblock_excel:
        print("  Output Excel  : %s" % os.path.basename(bulkblock_excel))
    if combined_chart:
        print("  Combined Chart: %s" % os.path.basename(combined_chart))
    if errors:
        print("\n  ERRORS (%d):" % len(errors))
        for err in errors:
            print("    • %s" % err)
    else:
        print("\n  All scenarios completed successfully!")

    print("\nDONE!")


if __name__ == "__main__":
    main()
