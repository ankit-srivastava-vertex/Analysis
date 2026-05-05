"""
Custom Sector Index Builder
===========================

SUMMARY
-------
Builds custom equal-weighted sector indices from user-defined stock
constituents.  Fetches 1-year prices, calculates index values
(base = 1000), and produces interactive charts with summary statistics.

WORKFLOW
--------
1. Load sector definitions from index_constituents.json.
   Each sector maps to a list of NSE stock symbols.
2. For each sector, fetch 1-year daily close prices for all constituents
   (jugaad-data primary, yfinance fallback).
3. Calculate daily returns per stock (clipped at ±35% to handle splits/demergers).
4. Compute equal-weighted portfolio return (simple average of stock returns).
5. Build cumulative index values with base value 1000.
6. Create multi-line Plotly chart with all sector indices + individual sub-charts.
7. Export summary stats + index values + daily prices to Excel + standalone HTML.

DATA SOURCES
------------
- jugaad-data              — NSE stock daily close prices (primary)
- yfinance                 — Fallback if jugaad-data fails for a symbol
- index_constituents.json  — User-defined sector → stock symbol mappings
                             (must exist in script directory)

OUTPUT
------
- custom_sector_index.xlsx         — Summary stats, Index Values, Daily Prices sheets
- custom_sector_index_chart.html   — Multi-line chart + individual sector sub-charts

USAGE
-----
Individual run:
    python3 custom_sector_index.py                         # default
    python3 custom_sector_index.py -c my_constituents.json  # custom file
    python3 custom_sector_index.py -o my_report             # custom output prefix

Group run (via run_all.py):
    Scenario name: sector_index
    Called as: custom_sector_index.run()  →  returns (indices_dict, prices_dict, summary_df, fig, excel_path, html_path)
    Skip with: python3 run_all.py --skip sector_index

DEPENDENCIES
------------
pandas, plotly, jugaad-data, yfinance
"""

import json
import os
import datetime
import pandas as pd
import plotly.graph_objects as go
from jugaad_data.nse import stock_df


# ─── Config ──────────────────────────────────────────────────────────────────
CONSTITUENTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index_constituents.json")
BASE_VALUE = 1000  # Starting value for each custom index


def _build_constituents_table_html():
    """Build an HTML table showing constituents of each custom sector."""
    if not os.path.exists(CONSTITUENTS_FILE):
        return ""
    with open(CONSTITUENTS_FILE, "r") as f:
        raw = json.load(f)
    if not raw:
        return ""

    sectors = {}
    for name, val in raw.items():
        if isinstance(val, dict) and "constituents" in val:
            sectors[name] = val["constituents"]
        elif isinstance(val, list):
            sectors[name] = val
    if not sectors:
        return ""

    max_len = max(len(v) for v in sectors.values())
    header = "".join(
        '<th style="padding:6px 10px;text-align:left;border:1px solid #ccc;'
        'background:#e3f2fd;font-size:12px;white-space:nowrap">%s (%d)</th>' % (name, len(tickers))
        for name, tickers in sectors.items()
    )
    rows = []
    for i in range(max_len):
        cells = []
        for name in sectors:
            constituents = sectors[name]
            val = constituents[i] if i < len(constituents) else ""
            cells.append(
                '<td style="padding:4px 8px;border:1px solid #ddd;font-size:11px'
                '%s">%s</td>' % (";background:#f9f9f9" if i % 2 else "", val)
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        '<div style="margin-top:24px;padding:12px;background:#fafafa;'
        'border:1px solid #e0e0e0;border-radius:6px;overflow-x:auto">'
        '<h3 style="margin:0 0 10px 0;font-size:15px;color:#333">'
        'Sector Constituents</h3>'
        '<table style="border-collapse:collapse;width:100%">'
        '<tr>' + header + '</tr>' + "".join(rows) +
        '</table></div>'
    )


# ─── Data fetching ───────────────────────────────────────────────────────────

def fetch_close_prices(symbol, start_date, end_date):
    """Fetch historical close prices.

    Primary: jugaad-data.  Fallback: yfinance.
    """
    # ── Primary: jugaad-data ──
    try:
        df = stock_df(symbol=symbol, from_date=start_date, to_date=end_date, series="EQ")
        if df is not None and not df.empty:
            df = df.rename(columns={"DATE": "Date", "CLOSE": "Close"})
            df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
            df = df[["Date", "Close"]].sort_values("Date").drop_duplicates(subset="Date", keep="first").reset_index(drop=True)
            df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
            print("    %s: %d days" % (symbol, len(df)))
            return df
    except Exception as e:
        print("    %s: jugaad-data failed (%s), trying yfinance ..." % (symbol, e))

    # ── Fallback: yfinance ──
    try:
        import yfinance as yf
        yf_df = yf.download(
            symbol + ".NS", start=str(start_date), end=str(end_date),
            progress=False,
        )
        if yf_df is not None and not yf_df.empty:
            yf_df = yf_df.reset_index()
            if isinstance(yf_df.columns, pd.MultiIndex):
                yf_df.columns = yf_df.columns.droplevel(1)
            yf_df["Date"] = pd.to_datetime(yf_df["Date"]).dt.normalize()
            yf_df = yf_df[["Date", "Close"]].sort_values("Date").drop_duplicates(subset="Date", keep="first").reset_index(drop=True)
            yf_df["Close"] = pd.to_numeric(yf_df["Close"], errors="coerce")
            print("    %s: %d days (yfinance)" % (symbol, len(yf_df)))
            return yf_df
    except Exception as e:
        print("    %s: yfinance also FAILED (%s)" % (symbol, e))

    print("    %s: NO DATA" % symbol)
    return pd.DataFrame(columns=["Date", "Close"])


# ─── Index calculation ───────────────────────────────────────────────────────

def calculate_equal_weight_index(price_df, base_value=BASE_VALUE):
    """Calculate an equal-weighted price index from a pivoted close price DataFrame.

    Args:
        price_df: DataFrame with Date index and one column per stock (close prices).
        base_value: Starting index value.

    Returns:
        Series with Date index and index values.
    """
    # Daily returns for each stock
    returns = price_df.pct_change()

    # Clip extreme daily returns (e.g., from stock splits / demergers)
    returns = returns.clip(lower=-0.35, upper=0.35)

    # Equal-weighted portfolio return = simple average of individual returns
    n_stocks = returns.shape[1]
    portfolio_returns = returns.mean(axis=1)

    # Build index from cumulative returns
    index_values = (1 + portfolio_returns).cumprod() * base_value
    # Set the first day to base_value
    index_values.iloc[0] = base_value

    return index_values


def build_sector_index(index_name, constituents, start_date, end_date):
    """Fetch data and calculate a single sector index.

    Args:
        index_name: Name of the custom index
        constituents: List of NSE stock symbols
        start_date: datetime.date
        end_date: datetime.date

    Returns:
        Tuple of (index_series, prices_df, failed_symbols)
        - index_series: Series with Date index and index values
        - prices_df: DataFrame of close prices per stock
        - failed_symbols: list of symbols that could not be fetched
    """
    print("\n  [%s] Fetching %d stocks..." % (index_name, len(constituents)))

    all_prices = {}
    failed = []

    for symbol in constituents:
        df = fetch_close_prices(symbol, start_date, end_date)
        if df.empty:
            failed.append(symbol)
            continue
        # Use Date as index, Close as value; deduplicate index
        series = df.set_index("Date")["Close"]
        series = series[~series.index.duplicated(keep="last")]
        series.name = symbol
        all_prices[symbol] = series

    if not all_prices:
        print("  [%s] No data fetched for any constituent!" % index_name)
        return pd.Series(dtype=float), pd.DataFrame(), failed

    # Combine into a single DataFrame, aligning on dates
    prices_df = pd.DataFrame(all_prices)
    prices_df = prices_df.sort_index()

    # Forward-fill small gaps (holidays may differ), then drop leading NaNs
    prices_df = prices_df.ffill().dropna()

    if prices_df.empty:
        print("  [%s] No overlapping dates after alignment!" % index_name)
        return pd.Series(dtype=float), prices_df, failed

    # Calculate index
    index_series = calculate_equal_weight_index(prices_df, BASE_VALUE)
    index_series.name = index_name

    current = index_series.iloc[-1]
    change_pct = ((current / BASE_VALUE) - 1) * 100
    print("  [%s] Built: %d days, %d stocks, current=%.2f (%+.2f%%)" % (
        index_name, len(index_series), len(all_prices), current, change_pct))

    if failed:
        print("  [%s] Failed symbols: %s" % (index_name, ", ".join(failed)))

    return index_series, prices_df, failed


# ─── Plotting ────────────────────────────────────────────────────────────────

def create_chart(all_indices, title="Custom Sector Indices"):
    """Create an interactive Plotly line chart.

    Args:
        all_indices: dict of {index_name: Series(Date -> value)}
        title: Chart title

    Returns:
        plotly Figure object
    """
    fig = go.Figure()

    colors = [
        "#2196F3", "#FF5722", "#4CAF50", "#9C27B0", "#FF9800",
        "#00BCD4", "#E91E63", "#8BC34A", "#673AB7", "#CDDC39",
    ]

    for i, (name, series) in enumerate(all_indices.items()):
        color = colors[i % len(colors)]
        current = series.iloc[-1]
        change_pct = ((current / BASE_VALUE) - 1) * 100
        pct_series = ((series / BASE_VALUE) - 1) * 100

        hover_tpl = (
            "<b>" + name + "</b><br>"
            "Date: %{x|%d-%b-%Y}<br>"
            "Change: %{y:+.1f}%<br>"
            "<extra></extra>"
        )

        fig.add_trace(go.Scatter(
            x=pct_series.index,
            y=pct_series.values,
            mode="lines",
            name="%s (%+.1f%%)" % (name, change_pct),
            line=dict(width=2, color=color),
            hovertemplate=hover_tpl,
        ))

    fig.update_layout(
        title=dict(text=title, font=dict(size=20)),
        xaxis=dict(
            title="Date",
            rangeslider=dict(visible=True),
            rangeselector=dict(
                buttons=[
                    dict(count=1, label="1M", step="month", stepmode="backward"),
                    dict(count=3, label="3M", step="month", stepmode="backward"),
                    dict(count=6, label="6M", step="month", stepmode="backward"),
                    dict(step="all", label="1Y"),
                ],
            ),
        ),
        yaxis=dict(title="% Change from Base", rangemode="tozero", dtick=25),
        hovermode="closest",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.06,
            xanchor="right",
            x=1,
            font=dict(size=11),
        ),
        template="plotly_white",
        height=700,
        margin=dict(t=120),
    )

    return fig


def create_individual_charts(all_indices):
    """Create individual Plotly charts for each sector index with duration sliders.

    Args:
        all_indices: dict of {index_name: Series(Date -> value)}

    Returns:
        list of plotly Figure objects (one per sector)
    """
    colors = [
        "#2196F3", "#FF5722", "#4CAF50", "#9C27B0", "#FF9800",
        "#00BCD4", "#E91E63", "#8BC34A", "#673AB7", "#CDDC39",
    ]

    figures = []
    for i, (name, series) in enumerate(all_indices.items()):
        fig = go.Figure()
        color = colors[i % len(colors)]
        current = series.iloc[-1]
        change_pct = ((current / BASE_VALUE) - 1) * 100

        pct_series = ((series / BASE_VALUE) - 1) * 100

        hover_tpl = (
            "<b>" + name + "</b><br>"
            "Date: %{x|%d-%b-%Y}<br>"
            "Change: %{y:+.1f}%<br>"
            "<extra></extra>"
        )

        fig.add_trace(go.Scatter(
            x=pct_series.index,
            y=pct_series.values,
            mode="lines",
            name="%s (%+.1f%%)" % (name, change_pct),
            line=dict(width=2, color=color),
            hovertemplate=hover_tpl,
        ))

        fig.update_layout(
            title=dict(text="%s (%+.1f%%)" % (name, change_pct), font=dict(size=16)),
            xaxis=dict(
                title="Date",
                rangeslider=dict(visible=True),
                rangeselector=dict(
                    buttons=[
                        dict(count=1, label="1M", step="month", stepmode="backward"),
                        dict(count=3, label="3M", step="month", stepmode="backward"),
                        dict(count=6, label="6M", step="month", stepmode="backward"),
                        dict(count=9, label="9M", step="month", stepmode="backward"),
                        dict(step="all", label="ALL"),
                    ],
                ),
            ),
            yaxis=dict(title="% Change from Base", rangemode="tozero", dtick=25),
            hovermode="x",
            template="plotly_white",
            height=400,
        )
        figures.append(fig)

    return figures


# ─── Output ──────────────────────────────────────────────────────────────────

def save_to_excel(all_indices, all_prices, summary, output_file):
    """Save index data and constituent prices to Excel.

    Sheets:
        - Summary: one row per index with current value and % change
        - Index Values: all index time series
        - <IndexName> Prices: constituent close prices for each index
    """
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        # Summary sheet
        summary.to_excel(writer, sheet_name="Summary", index=False)

        # Combined index values
        index_df = pd.DataFrame(all_indices)
        index_df.index.name = "Date"
        index_df.to_excel(writer, sheet_name="Index Values")

        # Per-index constituent prices
        for name, prices_df in all_prices.items():
            sheet_name = name[:28]  # Excel sheet name limit is 31 chars
            prices_df.index.name = "Date"
            prices_df.to_excel(writer, sheet_name=sheet_name)

    print("\nExcel saved: %s" % output_file)


def save_chart_html(fig, output_file, individual_figs=None):
    """Save Plotly chart as standalone HTML with optional individual sector charts."""
    combined_html = fig.to_html(full_html=False, include_plotlyjs="cdn")

    individual_html = ""
    if individual_figs:
        individual_html = '<hr style="margin:40px 0;"><h2 style="text-align:center;font-family:sans-serif;">Individual Sector Charts</h2>'
        for ifig in individual_figs:
            individual_html += ifig.to_html(full_html=False, include_plotlyjs=False)

    # Build constituents table
    constituents_table = _build_constituents_table_html()

    full_html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Custom Sector Indices</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
</head><body style="margin:20px;">
%s
%s
%s
</body></html>""" % (combined_html, individual_html, constituents_table)

    with open(output_file, "w") as f:
        f.write(full_html)
    print("HTML chart saved: %s" % output_file)


# ─── Main ────────────────────────────────────────────────────────────────────

def load_constituents(filepath=CONSTITUENTS_FILE):
    """Load index definitions from JSON file."""
    with open(filepath, "r") as f:
        data = json.load(f)
    print("Loaded %d custom indices from %s" % (len(data), os.path.basename(filepath)))
    for name, info in data.items():
        print("  %s: %d stocks — %s" % (name, len(info["constituents"]), info.get("description", "")))
    return data


def run(constituents_file=None, output_prefix=None):
    """Main entry point: load constituents, fetch data, build indices, plot, export.

    Args:
        constituents_file: Path to JSON file (default: index_constituents.json)
        output_prefix: Prefix for output files (default: auto with timestamp)

    Returns:
        Tuple of (all_indices dict, fig, excel_path, html_path)
    """
    if constituents_file is None:
        constituents_file = CONSTITUENTS_FILE

    print("=" * 60)
    print("Custom Sector Index Builder")
    print("=" * 60)

    # Load index definitions
    index_defs = load_constituents(constituents_file)

    # Date range: from 1st January 2024 to today
    end_dt = datetime.date.today()
    start_dt = datetime.date(2024, 1, 1)
    print("\nDate range: %s to %s" % (start_dt.strftime("%d-%m-%Y"), end_dt.strftime("%d-%m-%Y")))

    # Build each index
    all_indices = {}
    all_prices = {}
    summary_rows = []

    for index_name, info in index_defs.items():
        constituents = info["constituents"]
        index_series, prices_df, failed = build_sector_index(
            index_name, constituents, start_dt, end_dt
        )
        if index_series.empty:
            continue

        all_indices[index_name] = index_series
        all_prices[index_name] = prices_df

        current = index_series.iloc[-1]
        change_pct = ((current / BASE_VALUE) - 1) * 100
        summary_rows.append({
            "Index": index_name,
            "Description": info.get("description", ""),
            "Constituents": len(constituents),
            "Failed": len(failed),
            "Start Date": index_series.index.min().strftime("%d-%b-%Y"),
            "End Date": index_series.index.max().strftime("%d-%b-%Y"),
            "Trading Days": len(index_series),
            "Current Value": round(current, 2),
            "1Y Change %": round(change_pct, 2),
            "52W High": round(index_series.max(), 2),
            "52W Low": round(index_series.min(), 2),
        })

    if not all_indices:
        print("\nNo indices could be built. Check network connectivity and stock symbols.")
        return {}, None, None, None

    summary_df = pd.DataFrame(summary_rows)
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(summary_df.to_string(index=False))

    # Generate output filenames
    if output_prefix is None:
        output_prefix = os.path.join(SCRIPT_DIR, "custom_sector_index")

    excel_path = output_prefix + ".xlsx"
    html_path = output_prefix + "_chart.html"

    # Plot
    fig = create_chart(all_indices)
    individual_figs = create_individual_charts(all_indices)

    # Save outputs
    save_to_excel(all_indices, all_prices, summary_df, excel_path)
    save_chart_html(fig, html_path, individual_figs=individual_figs)

    print("\nDone! %d indices built." % len(all_indices))
    return all_indices, all_prices, summary_df, fig, excel_path, html_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Custom Sector Index Builder")
    parser.add_argument("--constituents", "-c", help="Path to constituents JSON file")
    parser.add_argument("--output", "-o", help="Output filename prefix")
    args = parser.parse_args()

    run(constituents_file=args.constituents, output_prefix=args.output)
