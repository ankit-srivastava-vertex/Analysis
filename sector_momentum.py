"""
Sector Momentum & Relative Strength Analyzer
==============================================

SUMMARY
-------
Computes Mansfield Relative Strength (RS) of each custom sector index
versus the Nifty 50 benchmark (NIFTYBEES ETF proxy).  Ranks sectors by
current RS and trend direction.

  RS > 0  = sector outperforming Nifty 50
  RS < 0  = sector underperforming Nifty 50
  Rising RS = sector gaining momentum relative to market

WORKFLOW
--------
1. Load custom sector definitions from index_constituents.json.
2. Fetch Nifty 50 benchmark via NIFTYBEES ETF (jugaad-data primary, yfinance fallback).
3. Build each custom sector index using custom_sector_index.py (equal-weighted).
4. Compute RS = (sector / benchmark) × 100 for each trading day.
5. Calculate RS stats — current level, 20-day trend (rising / falling).
6. Rank all sectors by current RS.
7. Create multi-line Plotly chart with RS history + range slider.
8. Export RS data + rankings to Excel.

DATA SOURCES
------------
- jugaad-data     — NIFTYBEES.NS benchmark daily closes (primary)
- yfinance        — Fallback if jugaad-data fails
- custom_sector_index.py — Sector index values (which uses jugaad-data + yfinance)
- index_constituents.json — User-defined sector → stock mappings

OUTPUT
------
- sector_momentum.xlsx         — RS Ranking, RS History, Index Values sheets
- sector_momentum_chart.html   — Multi-line RS chart with range slider

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
from jugaad_data.nse import stock_df

from custom_sector_index import (
    load_constituents, build_sector_index, BASE_VALUE, CONSTITUENTS_FILE,
)


START_DATE = datetime.date(2024, 1, 1)


# ─── Benchmark ───────────────────────────────────────────────────────────────

def fetch_benchmark(start_date, end_date):
    """Fetch Nifty 50 proxy via NIFTYBEES ETF.

    Primary: Angel One (via data_provider).  Fallback: jugaad-data, yfinance.
    """
    print("\n  Fetching benchmark (NIFTYBEES)...")

    # ── Primary: Angel One via data_provider ──
    try:
        from data_provider import _fetch_one, _resolve_period
        s, e = _resolve_period(str(start_date), str(end_date), None)
        dp_df = _fetch_one("NIFTYBEES.NS", s, e)
        if dp_df is not None and not dp_df.empty and "Close" in dp_df.columns:
            series = dp_df["Close"].copy()
            series.index = pd.to_datetime(series.index).normalize()
            series = series[~series.index.duplicated(keep="last")]
            series = pd.to_numeric(series, errors="coerce")
            print("    NIFTYBEES: %d days" % len(series))
            return series
    except Exception as e:
        print("    NIFTYBEES: data_provider failed (%s), trying jugaad-data ..." % e)

    # ── Fallback 1: jugaad-data ──
    try:
        df = stock_df(
            symbol="NIFTYBEES",
            from_date=start_date,
            to_date=end_date,
            series="EQ",
        )
        if df is not None and not df.empty:
            df = df.rename(columns={"DATE": "Date", "CLOSE": "Close"})
            df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
            df = (
                df[["Date", "Close"]]
                .sort_values("Date")
                .drop_duplicates(subset="Date", keep="first")
            )
            series = df.set_index("Date")["Close"]
            series = series[~series.index.duplicated(keep="last")]
            series = pd.to_numeric(series, errors="coerce")
            print("    NIFTYBEES: %d days (jugaad)" % len(series))
            return series
    except Exception as e:
        print("    NIFTYBEES: jugaad-data failed (%s), trying yfinance ..." % e)

    # ── Fallback 2: yfinance ──
    try:
        import yfinance as yf
        yf_df = yf.download(
            "NIFTYBEES.NS", start=str(start_date), end=str(end_date),
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
            series = pd.to_numeric(series, errors="coerce")
            print("    NIFTYBEES: %d days (yfinance)" % len(series))
            return series
    except Exception as e:
        print("    NIFTYBEES: yfinance also FAILED (%s)" % e)

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

def create_rs_chart(all_rs, all_indices, title="Sector Relative Strength vs Nifty 50"):
    """Create interactive Plotly chart with RS lines + sector index lines."""
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.30,
        subplot_titles=(
            "Relative Strength (> 0 = Outperforming Nifty 50)<br><sup>Sector vs Nifty 50 — rising line means sector gaining strength relative to benchmark, even if both are falling</sup>",
            "Sector Index — % Change from Base<br><sup>Absolute gain/loss of each sector index from starting value — independent of Nifty performance</sup>",
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
    fig.update_yaxes(dtick=10, row=1, col=1)
    fig.update_yaxes(dtick=10, row=2, col=1)

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


# ─── Output ──────────────────────────────────────────────────────────────────

def save_to_excel(all_rs, all_indices, ranking_df, output_file):
    """Save RS data and rankings to multi-sheet Excel."""
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        ranking_df.to_excel(writer, sheet_name="RS Ranking", index=False)

        rs_df = pd.DataFrame(all_rs)
        rs_df.index.name = "Date"
        rs_df.to_excel(writer, sheet_name="RS History")

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

    # Fetch benchmark
    benchmark = fetch_benchmark(start_dt, end_dt)
    if benchmark.empty:
        print("ERROR: Could not fetch benchmark data!")
        return

    # Build sector indices and compute RS
    all_indices = {}
    all_rs = {}
    ranking_rows = []

    for index_name, info in index_defs.items():
        constituents = info["constituents"]
        index_series, prices_df, failed = build_sector_index(
            index_name, constituents, start_dt, end_dt,
        )
        if index_series.empty:
            continue

        all_indices[index_name] = index_series

        # Relative Strength
        rs = compute_rs(index_series, benchmark)
        if rs.empty:
            continue
        all_rs[index_name] = rs

        # Stats
        current_rs = rs.iloc[-1] - 100  # rebased to 0
        lookback = min(20, len(rs))
        rs_trend = rs.iloc[-1] - rs.iloc[-lookback]
        trend_str = "\u2191 %.1f" % rs_trend if rs_trend > 0 else "\u2193 %.1f" % abs(rs_trend)

        current_val = index_series.iloc[-1]
        change_pct = ((current_val / BASE_VALUE) - 1) * 100

        ranking_rows.append({
            "Sector": index_name,
            "Description": info.get("description", ""),
            "Current RS": round(current_rs, 1),
            "20D Trend": trend_str,
            "RS Status": "Outperforming" if current_rs >= 0 else "Underperforming",
            "Index Value": round(current_val, 2),
            "Change %": round(change_pct, 2),
        })

    if not all_rs:
        print("No sectors could be analysed!")
        return

    # Sort by RS descending
    ranking_df = pd.DataFrame(ranking_rows).sort_values(
        "Current RS", ascending=False,
    )

    # Print ranking
    print("\n" + "=" * 60)
    print("SECTOR RS RANKING (vs Nifty 50)")
    print("=" * 60)
    for _, row in ranking_df.iterrows():
        star = "\u2605" if row["Current RS"] >= 0 else " "
        print("  %s %-15s RS=%+-6.1f %-8s [%s]" % (
            star, row["Sector"], row["Current RS"],
            row["20D Trend"], row["RS Status"]))

    # Output files
    if output_prefix is None:
        output_prefix = os.path.join(SCRIPT_DIR, "sector_momentum")

    excel_path = output_prefix + ".xlsx"
    html_path = output_prefix + "_chart.html"

    fig = create_rs_chart(all_rs, all_indices)

    save_to_excel(all_rs, all_indices, ranking_df, excel_path)
    save_chart_html(fig, html_path)

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
