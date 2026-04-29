"""
FII Sector-wise Flows — Equity Cash Market (Last 1 Year)
=========================================================
Fetches fortnightly sector-wise FII/FPI net-investment data from
NSDL FPI Monitor, aggregates the last 12 months, and produces a
single horizontal bar chart showing which sectors FII are buying
versus selling (equity cash market only — no F&O).

Data Source
-----------
NSDL FPI Monitor — Fortnightly Sector-wise FII Investment Data
https://www.fpi.nsdl.co.in/web/Reports/FPI_Fortnightly_Selection.aspx

Output
------
  - Interactive HTML chart  (horizontal bar: green = buying, red = selling)
  - Excel workbook          (total flows + fortnightly detail)

Usage
-----
  python fii_sector_flows.py
  python fii_sector_flows.py -o my_report
"""

import os
import datetime
import re
import time
import warnings
import argparse

import requests
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from io import StringIO

warnings.filterwarnings("ignore")

# ─── Config ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_URL = "https://www.fpi.nsdl.co.in/web"
SELECTION_URL = BASE_URL + "/Reports/FPI_Fortnightly_Selection.aspx"
TODAY = datetime.date.today()


# ─── Session ─────────────────────────────────────────────────────────────────

def create_session():
    """Create a requests session and obtain NSDL cookies."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    })
    s.get(BASE_URL + "/", timeout=15)          # seed cookies
    return s


# ─── Report Discovery ───────────────────────────────────────────────────────

def get_available_reports(session):
    """Return a list of dicts {date, url, label} for every NSDL
    fortnightly sector report, oldest-first."""
    r = session.get(SELECTION_URL, timeout=15)
    if r.status_code != 200:
        print("  ERROR: NSDL selection page returned %d" % r.status_code)
        return []

    opts = re.findall(
        r'<option[^>]*value="([^"]*FIIInvestSector[^"]*)"[^>]*>([^<]*)</option>',
        r.text,
    )

    reports = []
    for url_path, date_text in opts:
        date_text = date_text.strip()
        dt = None
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                dt = datetime.datetime.strptime(date_text, fmt).date()
                break
            except ValueError:
                continue
        if dt is None:
            continue
        full_url = url_path.replace("~/", BASE_URL + "/")
        reports.append({"date": dt, "url": full_url, "label": date_text})

    return sorted(reports, key=lambda x: x["date"])


def filter_last_year(reports):
    """Keep only reports whose date falls within the last ~12 months."""
    cutoff = TODAY - datetime.timedelta(days=370)
    return [r for r in reports if r["date"] >= cutoff]


# ─── Report Parsing ─────────────────────────────────────────────────────────

def _find_equity_net_col(df):
    """Dynamically locate the column index for the **current fortnight's**
    equity net investment in INR Cr.

    The table's first four rows are multi-level headers:
      row 0 — period      (e.g. "Net Investment April 01-15, 2026")
      row 1 — currency    ("IN INR Cr." or "IN USD Mn")
      row 2 — category    ("Equity", "Debt", …)
      row 3 — sub-cat     ("Equity", "Debt General Limit", …)

    We want:
      • "Net Investment …" in row 0
      • "INR" in row 1
      • "Equity" in row 2

    Among all matches, the one with the highest column index is the
    *current* (most-recent) fortnight.

    Returns (col_index, period_label) or None.
    """
    row0 = df.iloc[0]
    row1 = df.iloc[1]
    row2 = df.iloc[2]

    candidates = []
    for ci in range(2, len(df.columns)):
        h0 = str(row0[ci])
        h1 = str(row1[ci])
        h2 = str(row2[ci])
        if "Net Investment" in h0 and "INR" in h1 and h2 == "Equity":
            candidates.append((ci, h0))

    if not candidates:
        return None
    return max(candidates, key=lambda x: x[0])


def fetch_and_parse(session, report):
    """Fetch one fortnightly report; return a list of row-dicts
    [{Sector, Net_Cr, Period, Report_Date}, …]."""
    url = report["url"]
    try:
        r = session.get(url, timeout=25)
        if r.status_code != 200:
            return []

        tables = pd.read_html(StringIO(r.text))
        if not tables:
            return []

        df = tables[0]
        result = _find_equity_net_col(df)
        if result is None:
            return []
        col_idx, period_label = result

        rows = []
        for ri in range(4, len(df)):
            sector = str(df.iloc[ri, 1]).strip()
            if not sector or sector.lower() == "nan":
                continue
            if "total" in sector.lower():
                continue

            val = df.iloc[ri, col_idx]
            try:
                net_cr = float(str(val).replace(",", ""))
            except (ValueError, TypeError):
                continue

            rows.append({
                "Sector": sector,
                "Net_Cr": net_cr,
                "Period": period_label.replace("Net Investment ", ""),
                "Report_Date": report["date"],
            })
        return rows

    except Exception:
        return []


def fetch_all_data(session, reports):
    """Sequentially fetch all reports and collect sector-wise data.
    Only the current fortnight from each report is used (no double-counting).
    """
    all_rows = []
    for i, report in enumerate(reports):
        pct = (i + 1) * 100 // len(reports)
        print("\r  Fetching %d/%d (%d%%) — %s ...        " % (
            i + 1, len(reports), pct, report["label"]), end="", flush=True)

        rows = fetch_and_parse(session, report)
        all_rows.extend(rows)

        if i < len(reports) - 1:
            time.sleep(0.3)

    print()
    return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()


# ─── Chart ───────────────────────────────────────────────────────────────────

# 24 distinct colours for sector lines
_LINE_COLORS = [
    "#2196F3", "#FF5722", "#4CAF50", "#9C27B0", "#FF9800",
    "#00BCD4", "#E91E63", "#8BC34A", "#3F51B5", "#CDDC39",
    "#009688", "#F44336", "#03A9F4", "#FFC107", "#673AB7",
    "#795548", "#607D8B", "#FFEB3B", "#00E676", "#FF1744",
    "#651FFF", "#00B0FF", "#76FF03", "#D50000",
]


def create_chart(sector_totals, detail_df, date_range_str):
    """Two-panel chart: horizontal bar (top) + sector line chart (bottom)."""
    bar_df = sector_totals.sort_values("Net_Cr")
    bar_height = max(900, len(bar_df) * 45)
    line_height = 900
    total_height = bar_height + line_height + 120

    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[bar_height / total_height, line_height / total_height],
        vertical_spacing=0.06,
        subplot_titles=(
            "Net FII Flows by Sector (Total)",
            "Sector-wise FII Flows Over Time (Cumulative)",
        ),
        specs=[[{"type": "bar"}], [{"type": "scatter"}]],
    )

    # ── Row 1: horizontal bar chart ──
    colors = ["#26a69a" if v >= 0 else "#ef5350" for v in bar_df["Net_Cr"]]
    fig.add_trace(go.Bar(
        y=bar_df["Sector"],
        x=bar_df["Net_Cr"],
        orientation="h",
        marker_color=colors,
        text=[u"\u20B9{:,.0f} Cr".format(v) for v in bar_df["Net_Cr"]],
        textposition="outside",
        textfont=dict(size=10),
        showlegend=False,
    ), row=1, col=1)

    # ── Row 2: line chart — cumulative net per sector over fortnights ──
    if not detail_df.empty:
        detail_df["Report_Date"] = pd.to_datetime(detail_df["Report_Date"])
        pivot = (
            detail_df
            .pivot_table(index="Report_Date", columns="Sector",
                         values="Net_Cr", aggfunc="sum")
            .sort_index()
            .fillna(0)
        )
        cum = pivot.cumsum()

        # Sort sectors by final cumulative value for legend ordering
        final_vals = cum.iloc[-1].sort_values(ascending=False)
        for i, sector in enumerate(final_vals.index):
            color = _LINE_COLORS[i % len(_LINE_COLORS)]
            fig.add_trace(go.Scatter(
                x=cum.index,
                y=cum[sector],
                mode="lines+markers",
                name=sector,
                line=dict(color=color, width=2.5),
                marker=dict(size=5),
                hovertemplate=(
                    "%{fullData.name}<br>"
                    "Date: %{x|%d-%b-%Y}<br>"
                    "Cumulative: \u20B9%{y:,.0f} Cr"
                    "<extra></extra>"
                ),
            ), row=2, col=1)

    # ── Layout ──
    total_net = bar_df["Net_Cr"].sum()
    fig.update_layout(
        title=dict(
            text=(
                "FII Sector-wise Net Flows \u2014 Equity Cash Market<br>"
                "<sup>%s  |  Total Net: \u20B9%s Cr  |  Source: NSDL FPI Monitor</sup>"
                % (date_range_str, "{:,.0f}".format(total_net))
            ),
            x=0.5,
        ),
        height=total_height,
        width=None,                              # fill browser width
        margin=dict(l=320, r=60, t=90, b=120),
        plot_bgcolor="#fafafa",
        hovermode="closest",                      # show only hovered sector
        dragmode="pan",                                # drag to pan, not select
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.03,
            xanchor="center",
            x=0.5,
            font=dict(size=10),
            itemwidth=40,
        ),
    )
    # Bar chart axes
    fig.update_xaxes(zeroline=True, zerolinewidth=2, zerolinecolor="#333",
                     gridcolor="#e0e0e0",
                     title_text="Net FII Investment (\u20B9 Cr)", row=1, col=1)
    fig.update_yaxes(tickfont=dict(size=10), row=1, col=1)
    # Line chart axes — with rangeslider for drag/scroll
    fig.update_xaxes(
        title_text="", gridcolor="#e0e0e0",
        rangeslider=dict(visible=True, thickness=0.08),
        row=2, col=1,
    )
    fig.update_yaxes(title_text="Cumulative Net (\u20B9 Cr)",
                     gridcolor="#e0e0e0", zeroline=True,
                     zerolinewidth=1, zerolinecolor="#999",
                     fixedrange=False, row=2, col=1)
    return fig


# ─── Output ──────────────────────────────────────────────────────────────────

def save_outputs(sector_totals, detail_df, chart_fig, prefix, chart_path=None):
    """Save HTML chart + Excel workbook."""
    if chart_path is None:
        chart_path = prefix + "_chart.html"
    excel_path = prefix + ".xlsx"

    chart_fig.write_html(
        chart_path,
        config={
            "scrollZoom": True,
            "displayModeBar": True,
            "modeBarButtonsToRemove": ["select2d", "lasso2d"],
        },
        full_html=True,
        default_width="100%",
        default_height="100%",
    )
    print("  Chart : %s" % chart_path)

    with pd.ExcelWriter(excel_path, engine="openpyxl") as w:
        out = sector_totals.sort_values("Net_Cr", ascending=False).copy()
        out.to_excel(w, sheet_name="Net Flows by Sector", index=False)
        if not detail_df.empty:
            detail_df.to_excel(w, sheet_name="Fortnightly Detail", index=False)
    print("  Excel : %s" % excel_path)


# ─── Run ─────────────────────────────────────────────────────────────────────

def run(output_prefix=None):
    print("=" * 60)
    print("FII Sector-wise Flows \u2014 Equity Cash Market")
    print("=" * 60)

    # 1. Session
    print("\n[1] Connecting to NSDL FPI Monitor ...")
    session = create_session()

    # 2. Discover reports
    print("\n[2] Discovering fortnightly reports ...")
    all_reports = get_available_reports(session)
    print("  Total available: %d" % len(all_reports))

    reports = filter_last_year(all_reports)
    print("  Last 1 year   : %d reports" % len(reports))
    if reports:
        print("  Range: %s \u2192 %s" % (
            reports[0]["date"].strftime("%d-%b-%Y"),
            reports[-1]["date"].strftime("%d-%b-%Y"),
        ))

    if not reports:
        print("  ERROR: No reports found for the last year!")
        return

    # 3. Fetch & parse
    print("\n[3] Fetching sector-wise data (one per fortnight) ...")
    data = fetch_all_data(session, reports)
    if data.empty:
        print("  ERROR: Could not parse any sector data!")
        return

    print("  Data points : %d" % len(data))
    print("  Sectors     : %d" % data["Sector"].nunique())
    print("  Fortnights  : %d" % data["Period"].nunique())

    # 4. Aggregate
    sector_totals = (
        data.groupby("Sector", as_index=False)["Net_Cr"]
        .sum()
        .sort_values("Net_Cr")
    )
    sector_totals["Net_Cr"] = sector_totals["Net_Cr"].round(2)

    # 5. Chart
    print("\n[4] Building chart ...")
    date_range = "%s \u2013 %s" % (
        reports[0]["date"].strftime("%b %Y"),
        reports[-1]["date"].strftime("%b %Y"),
    )
    fig = create_chart(sector_totals, data, date_range)

    # 6. Save
    if output_prefix is None:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_prefix = os.path.join(SCRIPT_DIR, "fii_sector_flows_%s" % ts)

    chart_path = os.path.join(SCRIPT_DIR, "fii_sector_flows_chart.html")

    print("\n[5] Saving output ...")
    save_outputs(sector_totals, data, fig, output_prefix, chart_path=chart_path)

    # Summary
    print("\n" + "=" * 60)
    total_net = sector_totals["Net_Cr"].sum()
    print("Total Net FII Flow (Equity Cash): \u20B9{:,.0f} Cr".format(total_net))

    print("\nTop 5 BUYING sectors:")
    for _, r in sector_totals.nlargest(5, "Net_Cr").iterrows():
        print("  %-45s \u20B9%10s Cr" % (r["Sector"], "{:,.0f}".format(r["Net_Cr"])))

    print("\nTop 5 SELLING sectors:")
    for _, r in sector_totals.nsmallest(5, "Net_Cr").iterrows():
        print("  %-45s \u20B9%10s Cr" % (r["Sector"], "{:,.0f}".format(r["Net_Cr"])))

    print("\nDONE \u2014 %s" % TODAY.strftime("%d-%b-%Y"))
    return sector_totals, data, fig, output_prefix + "_chart.html", output_prefix + ".xlsx"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FII Sector-wise Flows (Equity Cash Market)")
    parser.add_argument("-o", "--output", help="Output filename prefix")
    args = parser.parse_args()
    run(output_prefix=args.output)
