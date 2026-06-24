"""
Sector Momentum & Relative Strength Analyzer
==============================================

SUMMARY
-------
Computes Mansfield Relative Strength (RS) of each custom sector index
versus the Nifty 500 benchmark.  A secondary RS is also computed versus
the Nifty MidSmall 400 benchmark.  Ranks sectors by current RS and trend.

  RS > 0  = sector outperforming Nifty 500
  RS < 0  = sector underperforming Nifty 500
  Rising RS = sector gaining momentum relative to market

WORKFLOW
--------
1. Load custom sector definitions from index_constituents.json.
2. Fetch Nifty 500 (^CRSLDX) and Nifty MidSmall 400 (^NSEMS400) benchmarks (Angel One primary, yfinance fallback).
3. Build each custom sector index using custom_sector_index.py (equal-weighted).
4. Compute RS = (sector / benchmark) × 100 for each trading day.
5. Calculate RS stats — current level, 20-day trend (rising / falling).
6. Rank all sectors by current RS.
7. Create multi-line Plotly chart with RS history + range slider.
8. Export RS data + rankings to Excel.

DATA SOURCES
------------
- data_provider (Angel One) — ^CRSLDX / ^NSEMS400 index daily closes (primary)
- yfinance        — Fallback for benchmarks (^CRSLDX only)
- custom_sector_index.py — Sector index values (which uses jugaad-data + yfinance)
- index_constituents.json — User-defined sector → stock mappings

OUTPUT
------
- sector_momentum.xlsx                       — RS Ranking, RS History, Index Values sheets
- sector_momentum_chart.html                 — Single tabbed chart with 3 views:
                                                 1. RS vs Nifty 500
                                                 2. RS vs Nifty MidSmall 400
                                                 3. Per-sector RS vs both benchmarks

USAGE
-----
Individual run:
    python3 sector_momentum.py                # build & plot all sectors
    python3 sector_momentum.py -o my_report   # custom output prefix

Group run (via run_all.py):
    Scenario name: sector_momentum
    Called as: sector_momentum.run()  →  returns (rs_dict, indices_dict, ranking_df, fig, excel_path, html_path)
    Skip with: python3 run_all.py --skip sector_momentum

DEPENDENCIES
------------
pandas, plotly, jugaad-data, yfinance, custom_sector_index
"""

import os
import datetime
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

from custom_sector_index import (
    load_constituents, build_sector_index, BASE_VALUE, CONSTITUENTS_FILE,
)


START_DATE = datetime.date(2024, 1, 1)

# Broad-market benchmarks for Relative Strength (Angel One primary).
PRIMARY_BENCHMARK = ("Nifty 500", "^CRSLDX")
SECONDARY_BENCHMARK = ("Nifty MidSmall 400", "^NSEMS400")


# ─── Benchmark ───────────────────────────────────────────────────────────────

def fetch_benchmark(ticker, name, start_date, end_date):
    """Fetch a broad-market benchmark index close series.

    Primary: Angel One (via data_provider).  Fallback: yfinance.
    Works for index tickers like ^CRSLDX (Nifty 500) and ^NSEMS400
    (Nifty MidSmall 400).  ^NSEMS400 is Angel-only (not on yfinance).
    """
    print("\n  Fetching benchmark (%s / %s)..." % (name, ticker))

    # ── Primary: Angel One via data_provider ──
    try:
        from data_provider import _fetch_one, _resolve_period
        s, e = _resolve_period(str(start_date), str(end_date), None)
        dp_df = _fetch_one(ticker, s, e)
        if dp_df is not None and not dp_df.empty and "Close" in dp_df.columns:
            series = dp_df["Close"].copy()
            series.index = pd.to_datetime(series.index).normalize()
            series = series[~series.index.duplicated(keep="last")]
            series = pd.to_numeric(series, errors="coerce").dropna()
            if not series.empty:
                print("    %s: %d days" % (name, len(series)))
                return series
    except Exception as ex:
        print("    %s: data_provider failed (%s), trying yfinance ..." % (name, ex))

    # ── Fallback: yfinance (covers ^CRSLDX; ^NSEMS400 unsupported there) ──
    try:
        import yfinance as yf
        yf_df = yf.download(
            ticker, start=str(start_date), end=str(end_date),
            progress=False,
        )
        if yf_df is not None and not yf_df.empty:
            yf_df = yf_df.reset_index()
            if isinstance(yf_df.columns, pd.MultiIndex):
                yf_df.columns = yf_df.columns.droplevel(1)
            yf_df["Date"] = pd.to_datetime(yf_df["Date"]).dt.normalize()
            yf_df = (
                yf_df[["Date", "Close"]]
                .sort_values("Date")
                .drop_duplicates(subset="Date", keep="first")
            )
            series = yf_df.set_index("Date")["Close"]
            series = series[~series.index.duplicated(keep="last")]
            series = pd.to_numeric(series, errors="coerce").dropna()
            if not series.empty:
                print("    %s: %d days (yfinance)" % (name, len(series)))
                return series
    except Exception as ex:
        print("    %s: yfinance also FAILED (%s)" % (name, ex))

    return pd.Series(dtype=float)


# ─── RS Computation ──────────────────────────────────────────────────────────

def compute_rs(sector_series, benchmark_series):
    """Compute Mansfield Relative Strength.

    Both series are normalised to 100 at start.
    RS = (sector_norm / bench_norm) * 100
    """
    common = sector_series.index.intersection(benchmark_series.index)
    if len(common) < 2:
        return pd.Series(dtype=float)
    sector = sector_series.loc[common]
    bench = benchmark_series.loc[common]
    sector_norm = sector / sector.iloc[0] * 100
    bench_norm = bench / bench.iloc[0] * 100
    rs = sector_norm / bench_norm * 100
    return rs


# ─── Charts ──────────────────────────────────────────────────────────────────

def create_rs_chart(all_rs, all_indices, benchmark_name="Nifty 500"):
    """Create interactive Plotly chart with RS lines + sector index lines."""
    title = "Sector Relative Strength vs %s" % benchmark_name
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.30,
        subplot_titles=(
            "Relative Strength (> 0 = Outperforming %s)<br><sup>Sector vs %s — rising line means sector gaining strength relative to benchmark, even if both are falling</sup>" % (benchmark_name, benchmark_name),
            "Sector Index — %% Change from Base<br><sup>Absolute gain/loss of each sector index from starting value — independent of %s performance</sup>" % benchmark_name,
        ),
        row_heights=[0.50, 0.50],
    )

    colors = [
        "#2196F3", "#FF5722", "#4CAF50", "#9C27B0", "#FF9800",
        "#00BCD4", "#E91E63", "#8BC34A", "#673AB7", "#CDDC39",
    ]

    for i, (name, rs) in enumerate(all_rs.items()):
        color = colors[i % len(colors)]
        current_rs = rs.iloc[-1]
        rs_zeroed = rs - 100  # rebase so 0 = neutral

        # 20-day trend
        lookback = min(20, len(rs))
        rs_change = rs.iloc[-1] - rs.iloc[-lookback]
        trend = "\u2191" if rs_change > 0 else "\u2193"

        # ── RS line (top panel) ──────────────────────────────
        hover_rs = (
            "<b>" + name + "</b><br>"
            "Date: %{x|%d-%b-%Y}<br>"
            "RS: %{y:+.1f}<br>"
            "<extra></extra>"
        )
        fig.add_trace(go.Scatter(
            x=rs_zeroed.index, y=rs_zeroed.values,
            mode="lines",
            name="%s (RS=%+.1f %s)" % (name, current_rs - 100, trend),
            line=dict(width=2.5, color=color),
            hovertemplate=hover_rs,
        ), row=1, col=1)

        # ── Sector index (bottom panel) ─────────────────────
        if name in all_indices:
            series = all_indices[name]
            pct_change = ((series / BASE_VALUE) - 1) * 100  # % change series
            current_pct = pct_change.iloc[-1]

            hover_idx = (
                "<b>" + name + "</b><br>"
                "Date: %{x|%d-%b-%Y}<br>"
                "Change: %{y:+.1f}%<br>"
                "<extra></extra>"
            )
            fig.add_trace(go.Scatter(
                x=pct_change.index, y=pct_change.values,
                mode="lines",
                name="%s (%+.1f%%)" % (name, current_pct),
                line=dict(width=2, color=color),
                hovertemplate=hover_idx,
                showlegend=False,
            ), row=2, col=1)

    # Reference lines at 0
    fig.add_hline(
        y=0, line_dash="dash", line_color="gray",
        annotation_text="RS = 0 (Neutral)", row=1, col=1,
    )
    fig.add_hline(
        y=0, line_dash="dash", line_color="gray",
        annotation_text="Base (0%)", row=2, col=1,
    )

    fig.update_layout(
        title=dict(text=title, font=dict(size=20), y=0.98, yanchor="top"),
        hovermode="closest",
        legend=dict(
            orientation="h",
            yanchor="top",
            y=0.6,
            xanchor="center",
            x=0.5,
            font=dict(size=11),
            traceorder="normal",
            entrywidth=200,
            entrywidthmode="pixels",
        ),
        template="plotly_white",
        height=1100,
        margin=dict(t=100, r=50, b=80),
    )

    # Y-axis tick scaling for both panels
    fig.update_yaxes(dtick=25, row=1, col=1)
    fig.update_yaxes(dtick=25, row=2, col=1)

    # Range selector on bottom panel
    fig.update_xaxes(
        rangeslider=dict(visible=True),
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=3, label="3M", step="month", stepmode="backward"),
                dict(count=6, label="6M", step="month", stepmode="backward"),
                dict(count=1, label="1Y", step="year", stepmode="backward"),
                dict(step="all", label="All"),
            ],
        ),
        row=2, col=1,
    )

    return fig


def create_individual_dual_chart(all_rs_primary, all_rs_secondary,
                                 primary_name="Nifty 500",
                                 secondary_name="Nifty MidSmall 400"):
    """Per-sector small-multiples: each sector's RS vs BOTH benchmarks.

    One subplot per sector with two RS lines (rebased so 0 = neutral):
    blue = vs primary benchmark, orange = vs secondary benchmark.
    """
    import math

    sectors = list(all_rs_primary.keys())
    n = len(sectors)
    cols = 3
    rows = max(1, math.ceil(n / cols))

    fig = make_subplots(
        rows=rows, cols=cols,
        subplot_titles=sectors,
        vertical_spacing=0.07,
        horizontal_spacing=0.06,
    )

    c_primary, c_secondary = "#1565C0", "#EF6C00"

    for i, name in enumerate(sectors):
        r = i // cols + 1
        c = i % cols + 1
        show_legend = (i == 0)

        rs_p = all_rs_primary.get(name)
        if rs_p is not None and not rs_p.empty:
            fig.add_trace(go.Scatter(
                x=rs_p.index, y=(rs_p - 100).values, mode="lines",
                name="RS vs %s" % primary_name,
                legendgroup="primary", showlegend=show_legend,
                line=dict(width=2, color=c_primary),
                hovertemplate=("<b>" + name + " vs " + primary_name +
                               "</b><br>%{x|%d-%b-%Y}<br>RS: %{y:+.1f}<extra></extra>"),
            ), row=r, col=c)

        rs_s = all_rs_secondary.get(name)
        if rs_s is not None and not rs_s.empty:
            fig.add_trace(go.Scatter(
                x=rs_s.index, y=(rs_s - 100).values, mode="lines",
                name="RS vs %s" % secondary_name,
                legendgroup="secondary", showlegend=show_legend,
                line=dict(width=2, color=c_secondary),
                hovertemplate=("<b>" + name + " vs " + secondary_name +
                               "</b><br>%{x|%d-%b-%Y}<br>RS: %{y:+.1f}<extra></extra>"),
            ), row=r, col=c)

        fig.add_hline(y=0, line_dash="dash", line_color="gray",
                      line_width=1, row=r, col=c)

    fig.update_layout(
        title=dict(
            text="Sector Relative Strength — %s vs %s (per sector)" % (
                primary_name, secondary_name),
            font=dict(size=20), y=0.99, yanchor="top",
        ),
        template="plotly_white",
        height=max(450, rows * 260),
        hovermode="closest",
        legend=dict(orientation="h", yanchor="bottom", y=1.015,
                    xanchor="center", x=0.5, font=dict(size=12)),
        margin=dict(t=120, r=40, b=60),
    )
    return fig


# ─── Output ──────────────────────────────────────────────────────────────────

def save_to_excel(all_rs, all_indices, ranking_df, output_file,
                  all_rs_secondary=None, secondary_name="Nifty MidSmall 400"):
    """Save RS data and rankings to multi-sheet Excel."""
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        ranking_df.to_excel(writer, sheet_name="RS Ranking", index=False)

        rs_df = pd.DataFrame(all_rs)
        rs_df.index.name = "Date"
        rs_df.to_excel(writer, sheet_name="RS History")

        if all_rs_secondary:
            rs2_df = pd.DataFrame(all_rs_secondary)
            rs2_df.index.name = "Date"
            sheet2 = ("RS History %s" % secondary_name)[:31]
            rs2_df.to_excel(writer, sheet_name=sheet2)

        idx_df = pd.DataFrame(all_indices)
        idx_df.index.name = "Date"
        idx_df.to_excel(writer, sheet_name="Index Values")

    print("\nExcel saved: %s" % output_file)


def save_chart_html(fig, output_file):
    """Save chart as standalone HTML."""
    html = fig.to_html(
        full_html=True,
        include_plotlyjs="cdn",
        config={"responsive": True},
    )
    with open(output_file, "w") as f:
        f.write(html)
    print("HTML chart saved: %s" % output_file)


def save_combined_chart_html(sections, output_file):
    """Write multiple figures into ONE tabbed standalone HTML file.

    sections: list of (tab_label, fig).  Plotly.js is loaded once; each
    figure lives in its own tab panel.  Switching tabs fires a resize so
    charts that were rendered hidden lay out correctly.
    """
    panels = []
    buttons = []
    for i, (label, fig) in enumerate(sections):
        div = fig.to_html(
            full_html=False,
            include_plotlyjs=("cdn" if i == 0 else False),
            config={"responsive": True},
        )
        active = " active" if i == 0 else ""
        panels.append(
            '<div id="panel-%d" class="tab-panel%s">%s</div>' % (i, active, div)
        )
        buttons.append(
            '<button class="tab-btn%s" onclick="showTab(%d)">%s</button>'
            % (active, i, label)
        )

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sector Momentum &amp; Relative Strength</title>
<style>
  body {{ font-family: Arial, Helvetica, sans-serif; margin: 0; background: #fff; }}
  .tabs {{ display: flex; flex-wrap: wrap; gap: 6px; padding: 10px 14px;
          background: #f5f5f5; border-bottom: 1px solid #ddd;
          position: sticky; top: 0; z-index: 100; }}
  .tab-btn {{ padding: 8px 18px; border: 1px solid #ccc; background: #fff;
             cursor: pointer; border-radius: 6px; font-size: 14px; color: #333; }}
  .tab-btn:hover {{ background: #eef4fb; }}
  .tab-btn.active {{ background: #1565C0; color: #fff; border-color: #1565C0; }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
</style>
</head>
<body>
<div class="tabs">{buttons}</div>
{panels}
<script>
function showTab(i) {{
  var panels = document.querySelectorAll('.tab-panel');
  var btns = document.querySelectorAll('.tab-btn');
  for (var j = 0; j < panels.length; j++) {{
    panels[j].classList.toggle('active', j === i);
    btns[j].classList.toggle('active', j === i);
  }}
  window.dispatchEvent(new Event('resize'));
}}
</script>
</body>
</html>""".format(buttons="\n".join(buttons), panels="\n".join(panels))

    with open(output_file, "w") as f:
        f.write(html)
    print("HTML chart saved: %s" % output_file)


# ─── Main ────────────────────────────────────────────────────────────────────

def run(constituents_file=None, output_prefix=None):
    """Main entry point."""
    if constituents_file is None:
        constituents_file = CONSTITUENTS_FILE

    print("=" * 60)
    print("Sector Momentum & Relative Strength Analyzer")
    print("=" * 60)

    index_defs = load_constituents(constituents_file)

    end_dt = datetime.date.today()
    start_dt = START_DATE
    print("\nDate range: %s to %s" % (
        start_dt.strftime("%d-%m-%Y"), end_dt.strftime("%d-%m-%Y")))

    # Fetch benchmarks (primary = Nifty 500, secondary = Nifty MidSmall 400)
    p_name, p_ticker = PRIMARY_BENCHMARK
    s_name, s_ticker = SECONDARY_BENCHMARK
    benchmark = fetch_benchmark(p_ticker, p_name, start_dt, end_dt)
    if benchmark.empty:
        print("ERROR: Could not fetch primary benchmark (%s)!" % p_name)
        return
    benchmark2 = fetch_benchmark(s_ticker, s_name, start_dt, end_dt)

    # Build sector indices and compute RS
    all_indices = {}
    all_rs = {}           # RS vs primary benchmark (Nifty 500)
    all_rs2 = {}          # RS vs secondary benchmark (Nifty MidSmall 400)
    ranking_rows = []

    for index_name, info in index_defs.items():
        constituents = info["constituents"]
        index_series, prices_df, failed = build_sector_index(
            index_name, constituents, start_dt, end_dt,
        )
        if index_series.empty:
            continue

        all_indices[index_name] = index_series

        # Relative Strength vs primary benchmark (Nifty 500)
        rs = compute_rs(index_series, benchmark)
        if rs.empty:
            continue
        all_rs[index_name] = rs

        # Relative Strength vs secondary benchmark (Nifty MidSmall 400)
        rs2 = compute_rs(index_series, benchmark2) if not benchmark2.empty else pd.Series(dtype=float)
        if not rs2.empty:
            all_rs2[index_name] = rs2

        # Stats (vs primary)
        current_rs = rs.iloc[-1] - 100  # rebased to 0
        lookback = min(20, len(rs))
        rs_trend = rs.iloc[-1] - rs.iloc[-lookback]
        trend_str = "\u2191 %.1f" % rs_trend if rs_trend > 0 else "\u2193 %.1f" % abs(rs_trend)

        current_rs2 = (rs2.iloc[-1] - 100) if not rs2.empty else None

        current_val = index_series.iloc[-1]
        change_pct = ((current_val / BASE_VALUE) - 1) * 100

        ranking_rows.append({
            "Sector": index_name,
            "Description": info.get("description", ""),
            "RS vs Nifty 500": round(current_rs, 1),
            "RS vs MidSmall 400": round(current_rs2, 1) if current_rs2 is not None else None,
            "20D Trend": trend_str,
            "RS Status": "Outperforming" if current_rs >= 0 else "Underperforming",
            "Index Value": round(current_val, 2),
            "Change %": round(change_pct, 2),
        })

    if not all_rs:
        print("No sectors could be analysed!")
        return

    # Sort by RS descending (vs Nifty 500)
    ranking_df = pd.DataFrame(ranking_rows).sort_values(
        "RS vs Nifty 500", ascending=False,
    )

    # Print ranking
    print("\n" + "=" * 60)
    print("SECTOR RS RANKING (vs Nifty 500)")
    print("=" * 60)
    for _, row in ranking_df.iterrows():
        star = "\u2605" if row["RS vs Nifty 500"] >= 0 else " "
        print("  %s %-15s RS=%+-6.1f %-8s [%s]" % (
            star, row["Sector"], row["RS vs Nifty 500"],
            row["20D Trend"], row["RS Status"]))

    # Output files
    if output_prefix is None:
        output_prefix = os.path.join(SCRIPT_DIR, "sector_momentum")

    excel_path = output_prefix + ".xlsx"
    html_path = output_prefix + "_chart.html"   # combined: all views in one file

    fig = create_rs_chart(all_rs, all_indices, benchmark_name=p_name)

    save_to_excel(all_rs, all_indices, ranking_df, excel_path,
                  all_rs_secondary=all_rs2, secondary_name=s_name)

    # Combine all views into a single tabbed HTML (sector_momentum_chart.html):
    #   1. RS vs Nifty 500   2. RS vs Nifty MidSmall 400   3. Per-sector (both)
    sections = [("RS vs %s" % p_name, fig)]
    if all_rs2:
        fig2 = create_rs_chart(all_rs2, all_indices, benchmark_name=s_name)
        fig3 = create_individual_dual_chart(all_rs, all_rs2, p_name, s_name)
        sections.append(("RS vs %s" % s_name, fig2))
        sections.append(("Per-CustomSector (%s vs %s)" % (p_name, s_name), fig3))
    else:
        print("  WARN: %s data unavailable; combined chart shows %s only"
              % (s_name, p_name))

    save_combined_chart_html(sections, html_path)

    print("\nDone! %d sectors analysed." % len(all_rs))
    return all_rs, all_indices, ranking_df, fig, excel_path, html_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Sector Momentum & Relative Strength Analyzer",
    )
    parser.add_argument("--constituents", "-c",
                        help="Path to constituents JSON file")
    parser.add_argument("--output", "-o", help="Output filename prefix")
    args = parser.parse_args()

    run(constituents_file=args.constituents, output_prefix=args.output)
