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
2. Run 9 scenarios. They are LISTED below in output order (ALL_SCENARIOS,
   which fixes workbook sheet and chart tab order) but EXECUTED in
   EXECUTION_ORDER, widest download window first:

   a. bulk_block        → BulkBlock.BSEScraper          — NSE+BSE bulk & block deals,
                                                          filtered to a hardcoded
                                                          "superstar" client list and,
                                                          separately, to a hardcoded
                                                          stock watchlist. Each filter
                                                          yields ONE merged sheet with a
                                                          leading "Source" column:
                                                          "BulkBlock" and "Watchlist".
                                                          Standalone Excel emission is
                                                          SUPPRESSED via a _CapturingScraper
                                                          subclass.
   b. sector_index      → custom_sector_index.run()     — Custom equal-weighted sector
                                                          indices (Sector Idx Summary +
                                                          Sector Idx Values).
   c. fii_flows         → fii_flows.run()               — Daily FII equity cash flows
                                                          (FII Flow Summary + FII Daily Data).
   d. fii_sector_flows  → fii_sector_flows.run()        — Fortnightly FII sector-wise flows
                                                          (FII Sector Net Flows + Detail).
   e. sector_momentum   → sector_momentum.run()         — Comparative RS per custom sector
                                                          (RS History; its ranking is
                                                          folded into "Sector RS Ranking").
   f. nse_sector_rs     → nse_ready_sectors.run()       — Comparative RS on the official
                                                          NSE sector indices (30 indices)
                                                          (NSE Sector RS History; its
                                                          ranking is folded into
                                                          "Sector RS Ranking").
                                                          Both rankings carry RS vs Nifty
                                                          500 for the last 5 weekly
                                                          snapshots (RS Week 0 = current).
   g. rrg               → rrg_chart.run()               — Relative Rotation Graph for 6
                                                          timeframes (RRG 7 Day … Quarterly).
   h. sector_breadth    → sector_breadth.run()          — Sector-level market breadth from
                                                          the NIFTY 500 grouped by NSE
                                                          Industry. Chart-only ("Breadth"
                                                          tab); emits no Excel sheets.
   i. stage_analysis    → stage_analysis.run()          — Weinstein 30-week stage analysis
                                                          of the NIFTY 500 and its NSE
                                                          Industry composites (Stage
                                                          Sectors + Stage Stocks + Stage
                                                          Transitions) plus the "Stage
                                                          Analysis" chart tab.

   Each scenario is wrapped in try/except so a single failure does not
   abort the pipeline; failures are collected in `errors` and reported
   in the email body + summary.

   Scenarios are executed widest-download-window-first and the results are
   folded back in ALL_SCENARIOS order, so ordering is purely a cache
   optimisation and never moves a sheet or a tab. Four scenarios request
   heavily overlapping universes; this module also widens ohlcv_cache's
   in-memory TTL (ANGEL_CACHE_L1_TTL) for its own process so the second and
   later requests for a symbol are served from memory rather than re-fetched.
   That also pins every scenario to one price snapshot, so the sector index
   and the RS sheet can no longer be built from different bars.

   NOTE: the breakout scanner (breakout_scanner_angel.py) is not part of
   this pipeline. Run that script separately for the breakout output.

3. Merge every scenario's sheets into one Excel workbook
   (market_analysis_report.xlsx). Sub-module standalone Excel files are
   removed after their data is captured, so only the unified workbook
   remains on disk.

4. Collect all HTML chart files (8 charts: sector_index, fii_flows,
   fii_sector_flows, sector_momentum, nse_sector_rs, rrg, sector_breadth,
   stage_analysis).

5. Send consolidated email with the unified Excel + HTML charts
   attached (unless --no-email).

NOTE: india_macro is NOT part of this pipeline. Run india_macro.py
separately to refresh the macro dashboard.

DATA SOURCES
------------
All data is fetched by the individual sub-modules (see each file's header).
This script only orchestrates and consolidates — it does not call any
external APIs directly.

OUTPUT
------
- market_analysis_report.xlsx    — Unified workbook, typically ~22 sheets:
                                    4 BB (bulk/block) + 2 sector_index +
                                    2 fii_flows + 2 fii_sector_flows +
                                    2 sector_momentum + 1 nse_sector_rs +
                                    6 RRG timeframes + 3 stage_analysis.
- *_chart.html                   — 8 interactive Plotly charts.

USAGE
-----
    python3 run_all.py                                       # run all + send email
    python3 run_all.py --no-email                            # run all, skip email
    python3 run_all.py --skip bulk_block rrg                 # skip arbitrary scenarios

Available scenario names for --skip:
    bulk_block, sector_index, fii_flows, fii_sector_flows,
    sector_momentum, nse_sector_rs, rrg, sector_breadth, stage_analysis

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

# Scenarios re-request the same symbols minutes apart, which outlives
# ohlcv_cache's 300s in-memory TTL and forces a full re-fetch. Widen it for
# this process only — set before any sub-module (and hence ohlcv_cache) is
# imported, and left as a default so the caller can still override it.
os.environ.setdefault("ANGEL_CACHE_L1_TTL", "7200")

import pandas as pd  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TODAY = datetime.date.today()
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# Scenario names for --skip (order = sheet order in unified Excel)
ALL_SCENARIOS = ["bulk_block", "sector_index",
                 "fii_flows", "fii_sector_flows",
                 "sector_momentum", "nse_sector_rs", "rrg",
                 "sector_breadth", "stage_analysis"]


# ─── Scenario runners ──────────────────────────────────────

def _merge_deal_sheets(captured, source_map):
    """Fold per-exchange deal frames into one, tagged by a ``Source`` column.

    `source_map` maps a raw BulkBlock sheet key to the label written into
    ``Source``; it is iterated (rather than `captured`) so row order is
    deterministic regardless of scrape order. NSE and BSE carry different
    headers, so the concat is an outer join and unmatched columns come
    through blank. Returns None when no source frame was captured.
    """
    parts = []
    for raw_name, label in source_map.items():
        if raw_name not in captured:
            continue
        df = captured[raw_name]
        if df is None or (hasattr(df, "empty") and df.empty):
            df = pd.DataFrame({"Status": ["No matching deals"]})
        part = df.copy()
        part.insert(0, "Source", label)
        parts.append(part)

    if not parts:
        return None

    merged = pd.concat(parts, ignore_index=True, sort=False)
    # "Status" is the placeholder each source writes when it has no hits;
    # park it last so real deal columns stay on the left.
    cols = [c for c in merged.columns if c != "Status"]
    if "Status" in merged.columns:
        cols.append("Status")
    return merged[cols]


def run_bulk_block():
    """Scrape NSE+BSE bulk/block deals, filtered by superstar name and by the
    hardcoded stock watchlist. Returns sheets dict + None (no chart).
    Captures the scraped DataFrames in-memory; suppresses BulkBlock's
    own standalone Excel file so we only emit the unified workbook.

    Each filter collapses its four per-exchange views into ONE sheet with a
    leading ``Source`` column: ``BulkBlock`` for the superstar-name filter
    and ``Watchlist`` for the scrip filter.
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

    # Raw BulkBlock keys -> the label written into the merged "Source" column.
    # NOTE: BulkBlock emits the BSE frames under the lowercase keys
    # "bse_bulk"/"bse_block" (not "BSE Bulk Deals"), which is why those two
    # sheets previously leaked into the workbook under their raw names.
    deal_map = {
        "nse_bulk": "NSE Bulk",
        "nse_block": "NSE Block",
        "bse_bulk": "BSE Bulk",
        "bse_block": "BSE Block",
    }
    watchlist_map = {
        "watchlist_nse_bulk": "NSE Bulk",
        "watchlist_nse_block": "NSE Block",
        "watchlist_bse_bulk": "BSE Bulk",
        "watchlist_bse_block": "BSE Block",
    }

    sheets = {}
    for sheet_name, source_map in (("BulkBlock", deal_map),
                                   ("Watchlist", watchlist_map)):
        merged = _merge_deal_sheets(captured, source_map)
        if merged is not None:
            sheets[sheet_name] = merged

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
    # all_indices maps each sector to an OHLC frame; the workbook carries the
    # closing level per sector side by side, and the candles separately.
    idx_df = pd.DataFrame({name: frame["Close"] for name, frame in all_indices.items()})
    idx_df.index.name = "Date"
    sheets["Sector Idx Values"] = idx_df
    ohlc = pd.concat(all_indices, names=["Index"])
    ohlc.index.names = ["Index", "Date"]
    sheets["Sector Idx OHLC"] = ohlc.reset_index()

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


# Number of weekly RS snapshots shown on the "Sector RS Ranking" sheet.
# Week 0 is the current week; week N is N calendar weeks back.
WEEKLY_RS_WEEKS = 5


def _weekly_rs_labels(weeks=WEEKLY_RS_WEEKS):
    """Column headers for the weekly RS snapshots, newest first."""
    return ["RS Week %d" % w for w in range(weeks)]


def add_weekly_rs(ranking_df, all_rs, weeks=WEEKLY_RS_WEEKS, key="Sector"):
    """Replace the single RS column with `weeks` weekly RS-vs-Nifty-500 snapshots.

    `all_rs` is the producer's {name: comparative RS Series} dict, already
    computed against the PRIMARY benchmark (Nifty 500), so no re-fetch is
    needed. Week 0 anchors on the newest session present across all series;
    week N steps back N calendar weeks and takes the last session at or
    before that date, so holidays never shift a column onto a wrong week.
    Values are rebased to 0 = neutral, matching the producers'
    ``rs.iloc[-1] - 100`` convention.

    The MidSmall-400 column is dropped: only the Nifty 500 benchmark is
    wanted on the merged sheet.
    """
    labels = _weekly_rs_labels(weeks)
    series = {k: v.sort_index() for k, v in (all_rs or {}).items()
              if v is not None and not v.empty}
    if ranking_df is None or ranking_df.empty or not series:
        return ranking_df

    anchor = max(s.index[-1] for s in series.values())
    table = {}
    for name, s in series.items():
        row = {}
        for w, label in enumerate(labels):
            val = s.asof(anchor - pd.Timedelta(weeks=w))
            row[label] = None if pd.isna(val) else round(float(val) - 100, 1)
        table[name] = row

    df = ranking_df.copy()
    for label in labels:
        df[label] = df[key].map(
            lambda name, lbl=label: table.get(name, {}).get(lbl))

    df = df.drop(columns=[c for c in ("RS vs Nifty 500", "RS vs MidSmall 400")
                          if c in df.columns])

    # Sector / its descriptor first, then the weekly RS block, then the rest.
    front = [key] + [c for c in ("Description", "Index") if c in df.columns]
    rest = [c for c in df.columns if c not in front and c not in labels]
    df = df[front + labels + rest]
    return df.sort_values(labels[0], ascending=False, na_position="last")


# Ranking sheets folded into the single "Sector RS Ranking" sheet, in order,
# each tagged with its label in a leading "Source" column.
RS_RANKING_SOURCES = (
    ("RS Ranking", "Custom Sector"),
    ("NSE Sector RS Ranking", "NSE Official"),
)


def merge_rs_rankings(all_sheets):
    """Fold the custom-sector and official-index RS tables into one sheet.

    Pops the two source sheets and writes ``Sector RS Ranking`` in their
    place. The two frames carry different descriptor columns ("Description"
    vs "Index"), so the concat is an outer join and unmatched columns come
    through blank; both descriptors are pulled to the left so the weekly RS
    block still reads left-to-right. Mutates and returns `all_sheets`.
    """
    parts = []
    for name, label in RS_RANKING_SOURCES:
        df = all_sheets.pop(name, None)
        if df is None or (hasattr(df, "empty") and df.empty):
            continue
        part = df.copy()
        part.insert(0, "Source", label)
        parts.append(part)

    if parts:
        merged = pd.concat(parts, ignore_index=True, sort=False)
        front = [c for c in ("Source", "Sector", "Description", "Index")
                 if c in merged.columns]
        rest = [c for c in merged.columns if c not in front]
        all_sheets["Sector RS Ranking"] = merged[front + rest]
    return all_sheets


def run_sector_momentum():
    """Run Sector Momentum & RS Analyzer. Returns sheets dict + chart path.

    The ranking is emitted under the interim key "RS Ranking" and carries
    `WEEKLY_RS_WEEKS` weekly RS-vs-Nifty-500 columns; `merge_rs_rankings()`
    later folds it into the unified "Sector RS Ranking" sheet.
    """
    from sector_momentum import run as sm_run
    prefix = os.path.join(SCRIPT_DIR, "sector_momentum")
    result = sm_run(output_prefix=prefix)
    if result is None:
        return {}, None

    all_rs, all_indices, ranking_df, fig, excel_path, html_path = result

    sheets = {}
    sheets["RS Ranking"] = add_weekly_rs(ranking_df, all_rs)

    rs_df = pd.DataFrame(all_rs)
    rs_df.index.name = "Date"
    sheets["RS History"] = rs_df

    if os.path.exists(excel_path):
        os.remove(excel_path)

    return sheets, html_path


def run_nse_sector_rs():
    """Run NSE Sector RS Analyzer (official indices). Returns sheets dict
    + chart path. Uses distinct interim sheet names ('NSE Sector RS ...') to
    avoid colliding with sector_momentum's 'RS Ranking'/'RS History'.

    The ranking carries `WEEKLY_RS_WEEKS` weekly RS-vs-Nifty-500 columns and
    is folded into the unified "Sector RS Ranking" sheet by
    `merge_rs_rankings()`.
    """
    from nse_ready_sectors import run as nse_run
    prefix = os.path.join(SCRIPT_DIR, "nse_sector_rs")
    result = nse_run(output_prefix=prefix)
    if result is None:
        return {}, None

    all_rs, all_indices, ranking_df, fig, excel_path, html_path = result

    sheets = {}
    sheets["NSE Sector RS Ranking"] = add_weekly_rs(ranking_df, all_rs)

    rs_df = pd.DataFrame(all_rs)
    rs_df.index.name = "Date"
    sheets["NSE Sector RS History"] = rs_df

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

    all_timeframe_data, fig, html_path = result

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

    return sheets, html_path


def run_sector_breadth():
    """Run sector-level market breadth. Returns empty sheets + chart path.

    Chart-only by design (no Excel output requested), so the sheets dict is
    always empty and only the HTML feeds the "Breadth" tab.
    """
    from sector_breadth import run as breadth_run
    prefix = os.path.join(SCRIPT_DIR, "sector_breadth")
    _df, html_path = breadth_run(output_prefix=prefix)
    return {}, html_path


def run_stage_analysis():
    """Run Weinstein stage analysis. Returns sheets dict + chart path.

    The standalone workbook is suppressed (write_excel=False) because the
    three sheets go straight into the unified workbook.
    """
    from stage_analysis import run as stage_run
    prefix = os.path.join(SCRIPT_DIR, "stage_analysis")
    sheets, html_path = stage_run(output_prefix=prefix, write_excel=False)
    return sheets, html_path


# ─── Unified Excel builder ──────────────────────────────────────────────────

# Sheets explicitly excluded from the unified workbook (kept out of the
# Excel even if upstream scenarios produce them). Charts/HTML still render.
EXCLUDED_SHEETS = {
    "Sector Idx Summary",
    "Sector Idx Values",
    "FII Flow Summary",
    "FII Daily Data",
    "FII Sector Net Flows",
    "FII Sector Detail",
    "RS History",
    "NSE Sector RS History",
    "RRG 3 Day",
    "RRG 7 Day",
    "RRG 2 Week",
    "RRG 12 Day",
    "RRG 3 Week",
    "RRG Weekly",
    "RRG Monthly",
    "RRG Quarterly",
}

# Sheets lifted out of scenario order and placed immediately after a named
# anchor: (sheet, anchor). Workbook order otherwise follows ALL_SCENARIOS,
# which strands "Sector RS Ranking" at the end because merge_rs_rankings()
# appends it after popping its two source sheets.
SHEET_PLACEMENT = (
    ("Sector RS Ranking", "Watchlist"),
)


def _apply_sheet_placement(sheets):
    """Reorder `sheets` so each SHEET_PLACEMENT entry follows its anchor.

    Silently skipped when either sheet is missing, so a partial scenario run
    still produces a workbook in whatever order it managed to build.
    """
    ordered = sheets
    for name, anchor in SHEET_PLACEMENT:
        if name not in ordered or anchor not in ordered or name == anchor:
            continue
        moved = ordered.pop(name)
        rebuilt = {}
        for key, val in ordered.items():
            rebuilt[key] = val
            if key == anchor:
                rebuilt[name] = moved
        ordered = rebuilt
    return ordered


def build_unified_excel(all_sheets, output_path):
    """Write all scenario sheets into one Excel workbook.

    Sheet order follows insertion order, adjusted by SHEET_PLACEMENT.
    """
    if not all_sheets:
        print("  No data to write to unified Excel.")
        return None

    filtered = {k: v for k, v in all_sheets.items() if k not in EXCLUDED_SHEETS}
    filtered = _apply_sheet_placement(filtered)
    skipped = [k for k in all_sheets if k in EXCLUDED_SHEETS]

    if not filtered:
        print("  No data to write to unified Excel after filtering.")
        return None

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for sheet_name, df in filtered.items():
            # Excel sheet name limit is 31 chars
            safe_name = sheet_name[:31]
            if hasattr(df, "index") and df.index.name == "Date":
                df.to_excel(writer, sheet_name=safe_name)
            else:
                df.to_excel(writer, sheet_name=safe_name, index=False)

    print("  Unified Excel: %s (%d sheets)" % (output_path, len(filtered)))
    if skipped:
        print("  Excluded sheets (%d): %s" % (len(skipped), ", ".join(skipped)))
    return output_path


def build_combined_chart(chart_files):
    """Combine all chart HTML files into a single tabbed market_charts.html.

    Each chart's full HTML is embedded as an iframe srcdoc so the original
    standalone files remain untouched. Returns the combined file path or
    None if no charts are provided.
    """
    chart_files = [f for f in chart_files if f and os.path.exists(f)]
    if not chart_files:
        return None

    combined_path = os.path.join(SCRIPT_DIR, "market_charts.html")

    label_map = {
        "sector_momentum_chart.html": "Sector Momentum RS",
        "nse_sector_rs_chart.html": "NSE Sector RS",
        "custom_sector_index_chart.html": "Custom Sector Index",
        "rrg_chart.html": "RRG",
        "sector_breadth.html": "Breadth",
        "stage_analysis.html": "Stage Analysis",
        "fii_flows_chart.html": "FII Flows",
        "fii_sector_flows_chart.html": "FII Sector Flows",
    }

    tab_buttons = []
    tab_panels = []
    for i, path in enumerate(chart_files):
        fname = os.path.basename(path)
        display = label_map.get(fname, fname.replace("_", " ").replace(".html", ""))
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        safe = html.replace("&", "&amp;").replace('"', "&quot;")
        active = " active" if i == 0 else ""
        tab_buttons.append(
            '<button class="tab-btn%s" onclick="showTab(%d)">%s</button>'
            % (active, i, display))
        tab_panels.append(
            '<div class="tab-panel" id="panel-%d" style="display:%s;">'
            '<iframe srcdoc="%s"></iframe></div>'
            % (i, "block" if i == 0 else "none", safe))

    html_doc = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Market Charts</title>
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
.tab-panel { width:100%; height:calc(100vh - 60px); overflow:auto; }
.tab-panel iframe { width:100%; height:1400px; border:none; }
</style>
</head>
<body>
<div class="tab-bar">__BUTTONS__</div>
__PANELS__
<script>
function showTab(idx) {
  document.querySelectorAll('.tab-btn').forEach((b, i) => b.classList.toggle('active', i === idx));
  document.querySelectorAll('.tab-panel').forEach((p, i) => p.style.display = (i === idx ? 'block' : 'none'));
}
</script>
</body></html>
"""
    html_doc = html_doc.replace("__BUTTONS__", "\n".join(tab_buttons))
    html_doc = html_doc.replace("__PANELS__", "\n".join(tab_panels))

    with open(combined_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    print("  ✓ Combined chart: market_charts.html (%d tabs)" % len(chart_files))

    for path in chart_files:
        try:
            os.remove(path)
        except OSError:
            pass

    return combined_path


# ─── Main ────────────────────────────────────────────────────────────────────

# Scenario name -> (display label, runner callable).
SCENARIOS = {
    "bulk_block":       ("Bulk & Block Deals (NSE + BSE)", run_bulk_block),
    "sector_index":     ("Custom Sector Index", run_sector_index),
    "fii_flows":        ("FII Equity Cash Market Flows", run_fii_flows),
    "fii_sector_flows": ("FII Sector-wise Flows", run_fii_sector_flows),
    "sector_momentum":  ("Sector Momentum & Relative Strength",
                         run_sector_momentum),
    "nse_sector_rs":    ("NSE Sector Relative Strength (Official Indices)",
                         run_nse_sector_rs),
    "rrg":              ("Relative Rotation Graph", run_rrg),
    "sector_breadth":   ("Sector Market Breadth", run_sector_breadth),
    "stage_analysis":   ("Weinstein Stage Analysis", run_stage_analysis),
}

# Execution order, widest download window first. ohlcv_cache only serves a
# request whose span its cached frame already covers, so fetching the 1200-day
# stage universe first lets the narrower scenarios read those same bars from
# memory instead of re-fetching. Output order stays ALL_SCENARIOS.
EXECUTION_ORDER = [
    "stage_analysis",     # ~1200d over ~991 symbols — widest window, runs first
    "sector_index",       # since 2024-01-01, 741 symbols
    "sector_momentum",    # identical 741 symbols to sector_index
    "rrg",                # same constituents again
    "sector_breadth",     # 180d over the NIFTY 500
    "bulk_block",         # remaining scenarios fetch no constituent OHLCV
    "fii_flows",
    "fii_sector_flows",
    "nse_sector_rs",
]

# A scenario missing from either list would be silently dropped from the run.
assert set(EXECUTION_ORDER) == set(ALL_SCENARIOS), \
    "EXECUTION_ORDER and ALL_SCENARIOS must cover the same scenarios"


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

    # Run in cache-friendly order, keyed by scenario name so the results can be
    # folded back in the canonical order below.
    results = {}
    to_run = [name for name in EXECUTION_ORDER if name not in skip]
    for pos, name in enumerate(to_run, 1):
        label, runner = SCENARIOS[name]
        print("\n" + "=" * 70)
        print("  SCENARIO %d/%d: %s" % (pos, len(to_run), label))
        print("=" * 70)
        try:
            sheets, chart = runner()
            sheets = sheets or {}
            results[name] = (sheets, chart)
            print("  ✓ %s complete (%d sheets)" % (label, len(sheets)))
        except Exception as e:
            errors.append("%s: %s" % (name, e))
            print("  ✗ %s FAILED: %s" % (label, e))
            traceback.print_exc()

    # Assemble in ALL_SCENARIOS order so changing EXECUTION_ORDER can never
    # move a workbook sheet or a chart tab.
    for name in ALL_SCENARIOS:
        sheets, chart = results.get(name, ({}, None))
        unified_sheets.update(sheets)
        if chart:
            chart_files.append(chart)

    # ── Build Unified Excel ───────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  BUILDING OUTPUTS")
    print("=" * 70)

    unified_excel_path = os.path.join(
        SCRIPT_DIR, "market_analysis_report.xlsx")

    if unified_sheets:
        merge_rs_rankings(unified_sheets)
        build_unified_excel(unified_sheets, unified_excel_path)
    else:
        unified_excel_path = None
        print("  No unified Excel data to write.")

    # ── Build Combined Chart ─────────────────────────────────────────
    combined_chart_path = build_combined_chart(chart_files)

    # ── Send Email ───────────────────────────────────────────────────────
    if not args.no_email:
        print("\n" + "=" * 70)
        print("  SENDING EMAIL")
        print("=" * 70)

        from email_sender import send_report

        attachments = []
        if unified_excel_path and os.path.exists(unified_excel_path):
            attachments.append(unified_excel_path)
        if combined_chart_path and os.path.exists(combined_chart_path):
            attachments.append(combined_chart_path)

        subject = "Daily Market Analysis Report — %s" % TODAY.strftime("%d-%b-%Y")

        body_lines = [
            "Daily Market Analysis Report — %s" % TODAY.strftime("%d-%b-%Y"),
            "",
            "Attached reports:",
        ]
        if unified_excel_path:
            body_lines.append("  • Market Analysis Report (Excel) — %d sheets" %
                              len(unified_sheets))
        if combined_chart_path:
            body_lines.append("  • Combined Market Charts (HTML) — %d tabs" %
                              len([f for f in chart_files]))

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
    if combined_chart_path:
        print("  Combined Chart: %s" % os.path.basename(combined_chart_path))
    if errors:
        print("\n  ERRORS (%d):" % len(errors))
        for err in errors:
            print("    • %s" % err)
    else:
        print("\n  All scenarios completed successfully!")

    print("\nDONE!")


if __name__ == "__main__":
    main()
