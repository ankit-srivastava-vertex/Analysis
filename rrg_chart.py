"""
Relative Rotation Graph (RRG) — Indian Sector Indices
======================================================

SUMMARY
-------
Plots an interactive RRG chart showing sector rotation relative to
the Nifty 50 benchmark.  Sectors rotate clockwise through four
quadrants: Leading → Weakening → Lagging → Improving.

WORKFLOW
--------
1. Fetch 1-year daily close prices for all sectors + Nifty 50 from yfinance.
2. Resample to 8 timeframes (3-day, 7-day, 2-week, 12-day, 3-week, weekly,
   monthly, quarterly).
3. For each timeframe compute:
   RS       = sector_close / benchmark_close × 100
   RS-Ratio = RS / SMA(RS, N) × 100
   RS-Mom   = RS-Ratio / SMA(RS-Ratio, N) × 100
4. Classify sectors into quadrants:
   Leading     (RS-Ratio > 100, RS-Mom > 100) — top-right
   Weakening   (RS-Ratio > 100, RS-Mom < 100) — bottom-right
   Lagging     (RS-Ratio < 100, RS-Mom < 100) — bottom-left
   Improving   (RS-Ratio < 100, RS-Mom > 100) — top-left
5. Create interactive scatter plot with timeframe selector buttons
   and sector multi-select dropdown.
6. Export to Excel + standalone HTML chart.

DATA SOURCES
------------
- yfinance — 1-year daily closes for:
  Sector indices: ^NSEBANK, ^CNXIT, ^CNXPHARMA, ^CNXAUTO, ^CNXMETAL, etc.
  Sector ETFs: HEALTHIETF.NS, COMMOIETF.NS, OILIETF.NS, CONSUMBEES.NS
  Benchmark: ^NSEI (Nifty 50)

OUTPUT
------
- rrg_chart.xlsx         — RRG data for all timeframes
- rrg_chart_chart.html   — Interactive scatter RRG with timeframe tabs

USAGE
-----
Individual run:
    python3 rrg_chart.py                  # default output
    python3 rrg_chart.py -o my_report     # custom output prefix

Group run (via run_all.py):
    Scenario name: rrg
    Called as: rrg_chart.run()  →  returns (all_data_dict, fig, excel_path, html_path)
    Skip with: python3 run_all.py --skip rrg

DEPENDENCIES
------------
pandas, plotly, yfinance
"""

import os
import json
import datetime
import argparse
import warnings

import pandas as pd
import plotly.graph_objects as go

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
    _HAS_YFINANCE = True
except ImportError:
    _HAS_YFINANCE = False

# ─── Config ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TODAY = datetime.date.today()
CONSTITUENTS_FILE = os.path.join(SCRIPT_DIR, "index_constituents.json")

BENCHMARK_NAME = "Nifty 50"
BENCHMARK_TICKER = "^NSEI"

# Sector indices available on yfinance (confirmed working)
SECTOR_INDICES = {
    "Bank":        "^NSEBANK",
    "IT":          "^CNXIT",
    "Pharma":      "^CNXPHARMA",
    "Auto":        "^CNXAUTO",
    "Metal":       "^CNXMETAL",
    "Realty":      "^CNXREALTY",
    "Energy":      "^CNXENERGY",
    "FMCG":        "^CNXFMCG",
    "Media":       "^CNXMEDIA",
    "PSU Bank":    "^CNXPSUBANK",
    "Infra":       "^CNXINFRA",
    "PSE":         "^CNXPSE",
    "MNC":         "^CNXMNC",
}

# Sector ETFs as proxy for indices not on yfinance
SECTOR_ETFS = {
    "Healthcare":  "HEALTHIETF.NS",
    "Commodities": "COMMOIETF.NS",
    "Oil & Gas":   "OILIETF.NS",
    "Consumption": "CONSUMBEES.NS",
}

# Merge all sectors
ALL_SECTORS = {}
ALL_SECTORS.update(SECTOR_INDICES)
ALL_SECTORS.update(SECTOR_ETFS)

# Timeframe settings: (resample_rule, sma_period, tail_length)
# resample_rule=None means use daily data as-is
TIMEFRAMES = {
    "3 Day":     (None,     3,  8),
    "7 Day":     (None,     7, 12),
    "2 Week":    (None,    10, 15),
    "12 Day":    (None,    12, 18),
    "3 Week":    (None,    15, 20),
    "Weekly":    ("W-FRI", 10, 12),
    "Monthly":   ("ME",     4,  6),
    "Quarterly": ("QE",     2,  4),
}

COLORS = [
    "#2196F3", "#FF5722", "#4CAF50", "#9C27B0", "#FF9800",
    "#00BCD4", "#E91E63", "#8BC34A", "#673AB7", "#CDDC39",
    "#795548", "#607D8B", "#F44336", "#3F51B5", "#009688",
    "#FFC107", "#03A9F4",
]


# ─── Data Fetching ───────────────────────────────────────────────────────────

def fetch_all_prices():
    """Download 1Y daily close for benchmark + all sector indices/ETFs.

    Primary: Angel One (via data_provider).  Fallback: jugaad-data, yfinance.

    Returns:
        DataFrame with Date index and one column per sector + benchmark.
    """
    tickers = {BENCHMARK_NAME: BENCHMARK_TICKER}
    tickers.update(ALL_SECTORS)

    ticker_list = list(tickers.values())
    name_by_ticker = {v: k for k, v in tickers.items()}

    print("  Downloading 1Y daily data for %d tickers ..." % len(ticker_list))
    try:
        import data_provider as dp
        raw = dp.download(ticker_list, period="1y", progress=False)
    except Exception as e:
        print("  data_provider failed (%s), falling back to yfinance ..." % e)
        if not _HAS_YFINANCE:
            raise RuntimeError("yfinance is required for RRG chart")
        raw = yf.download(ticker_list, period="1y", progress=False)

    if raw is None or raw.empty:
        print("  ERROR: no data returned")
        return pd.DataFrame()

    # Extract Close prices
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw[["Close"]]
        close.columns = [ticker_list[0]]

    # Rename columns from tickers to sector names
    rename_map = {}
    for col in close.columns:
        if col in name_by_ticker:
            rename_map[col] = name_by_ticker[col]
    close = close.rename(columns=rename_map)

    # Drop sectors with no data
    valid = close.dropna(axis=1, how="all")
    dropped = set(close.columns) - set(valid.columns)
    if dropped:
        print("  Dropped (no data): %s" % ", ".join(sorted(dropped)))

    print("  Got data for %d sectors + benchmark (%d trading days)" % (
        len(valid.columns) - 1, len(valid)))

    return valid


def _build_custom_indices(benchmark_series):
    """Build equal-weighted custom sector indices from index_constituents.json.

    Downloads 1Y daily close for all constituent stocks via yfinance,
    then computes equal-weighted index values for each custom sector.

    Returns:
        DataFrame with Date index and one column per custom index (prefixed 'C:').
    """
    if not os.path.exists(CONSTITUENTS_FILE):
        print("  No custom indices file found, skipping")
        return pd.DataFrame()

    with open(CONSTITUENTS_FILE, "r") as f:
        index_defs = json.load(f)

    # Collect all unique tickers
    all_tickers = set()
    for info in index_defs.values():
        for symbol in info["constituents"]:
            all_tickers.add(symbol + ".NS")

    if not all_tickers:
        return pd.DataFrame()

    print("  Downloading %d constituent stocks for %d custom indices ..." % (
        len(all_tickers), len(index_defs)))

    # Download in batches via data_provider (Angel One primary, yfinance fallback)
    ticker_list = sorted(all_tickers)
    batch_size = 40
    all_close = []
    for start in range(0, len(ticker_list), batch_size):
        batch = ticker_list[start:start + batch_size]
        try:
            import data_provider as dp
            raw = dp.download(batch, period="1y", progress=False)
        except Exception:
            try:
                raw = yf.download(batch, period="1y", progress=False) if _HAS_YFINANCE else None
            except Exception:
                raw = None
        try:
            if raw is not None and not raw.empty:
                if isinstance(raw.columns, pd.MultiIndex):
                    batch_close = raw["Close"]
                else:
                    batch_close = raw[["Close"]]
                    batch_close.columns = [batch[0]]
                all_close.append(batch_close)
        except Exception as e:
            print("  WARNING: Batch download failed: %s" % e)

    if not all_close:
        print("  WARNING: Could not fetch custom index constituent prices")
        return pd.DataFrame()

    close = pd.concat(all_close, axis=1)
    # Remove duplicate columns
    close = close.loc[:, ~close.columns.duplicated()]

    # Build each index
    result = pd.DataFrame(index=close.index)
    for index_name, info in index_defs.items():
        tickers = [s + ".NS" for s in info["constituents"]]
        available = [t for t in tickers if t in close.columns and close[t].notna().sum() > 5]
        if len(available) < 2:
            print("  [C: %s] Skipped — only %d stocks with data" % (index_name, len(available)))
            continue
        prices = close[available].ffill().bfill().dropna(how="all")
        if prices.empty or len(prices) < 10:
            print("  [C: %s] Skipped — only %d trading days" % (index_name, len(prices)))
            continue

        # Equal-weighted: daily returns → average → cumulative
        returns = prices.pct_change().clip(-0.35, 0.35)
        portfolio_ret = returns.mean(axis=1)
        index_vals = (1 + portfolio_ret).cumprod()
        index_vals.iloc[0] = 1.0
        # Scale to benchmark-like level so RS computation works cleanly
        base_level = benchmark_series.iloc[0] if not benchmark_series.empty else 1000.0
        index_series = index_vals * base_level

        col_name = "C: " + index_name
        result[col_name] = index_series
        print("  [%s] Built: %d days, %d stocks" % (col_name, len(index_series.dropna()), len(available)))

    return result.dropna(how="all")


# ─── RS Computation ──────────────────────────────────────────────────────────

def resample_prices(daily_df, rule):
    """Resample daily close prices to a lower frequency."""
    return daily_df.resample(rule).last().dropna(how="all")


def compute_jdk_rs(sector_series, benchmark_series, sma_period):
    """Compute JdK RS-Ratio and RS-Momentum for one sector.

    Returns:
        DataFrame with columns: RS, RS_Ratio, RS_Momentum
    """
    rs = sector_series / benchmark_series * 100

    rs_sma = rs.rolling(window=sma_period, min_periods=sma_period).mean()
    rs_ratio = rs / rs_sma * 100

    rs_ratio_sma = rs_ratio.rolling(window=sma_period, min_periods=sma_period).mean()
    rs_momentum = rs_ratio / rs_ratio_sma * 100

    result = pd.DataFrame({
        "RS": rs,
        "RS_Ratio": rs_ratio,
        "RS_Momentum": rs_momentum,
    })
    return result.dropna()


def compute_all_rs(prices_df, sma_period):
    """Compute RS-Ratio & RS-Momentum for all sectors.

    Returns:
        dict of {sector_name: DataFrame(RS, RS_Ratio, RS_Momentum)}
    """
    if BENCHMARK_NAME not in prices_df.columns:
        print("  ERROR: Benchmark '%s' not in data" % BENCHMARK_NAME)
        return {}

    benchmark = prices_df[BENCHMARK_NAME]
    sectors = [c for c in prices_df.columns if c != BENCHMARK_NAME]

    results = {}
    for sector in sectors:
        series = prices_df[sector].dropna()
        if len(series) < sma_period * 2:
            continue
        common = series.index.intersection(benchmark.index)
        if len(common) < sma_period * 2:
            continue
        rs_data = compute_jdk_rs(series[common], benchmark[common], sma_period)
        if not rs_data.empty:
            results[sector] = rs_data

    return results


# ─── Chart ───────────────────────────────────────────────────────────────────

def _quadrant_label(x, y):
    """Return quadrant name given RS-Ratio (x) and RS-Momentum (y)."""
    if x >= 100 and y >= 100:
        return "Leading"
    elif x >= 100 and y < 100:
        return "Weakening"
    elif x < 100 and y < 100:
        return "Lagging"
    else:
        return "Improving"


def _compute_axis_range(all_timeframe_data, timeframes_cfg, pad_pct=0.15):
    """Compute axis limits from data, ensuring 100 is always visible.

    Returns (x_min, x_max, y_min, y_max) with padding.
    """
    all_x, all_y = [], []
    for tf_name, sector_data in all_timeframe_data.items():
        _, _, tail_len = timeframes_cfg[tf_name]
        for df in sector_data.values():
            tail = df.iloc[-tail_len:] if len(df) >= tail_len else df
            all_x.extend(tail["RS_Ratio"].tolist())
            all_y.extend(tail["RS_Momentum"].tolist())

    if not all_x:
        return 90, 110, 90, 110

    x_lo, x_hi = min(all_x), max(all_x)
    y_lo, y_hi = min(all_y), max(all_y)

    # Ensure 100 is included
    x_lo = min(x_lo, 100)
    x_hi = max(x_hi, 100)
    y_lo = min(y_lo, 100)
    y_hi = max(y_hi, 100)

    # Symmetric padding
    x_pad = max((x_hi - x_lo) * pad_pct, 1.0)
    y_pad = max((y_hi - y_lo) * pad_pct, 1.0)

    return x_lo - x_pad, x_hi + x_pad, y_lo - y_pad, y_hi + y_pad


def create_rrg_chart(all_timeframe_data, title="Relative Rotation Graph \u2014 Indian Sectors"):
    """Create Plotly RRG figure with trace metadata for interactive controls.

    Returns:
        (fig, trace_meta, sorted_sectors)
        trace_meta: {timeframe: {sector: [trace_indices]}}
    """
    fig = go.Figure()

    timeframe_names = list(all_timeframe_data.keys())
    trace_meta = {}
    trace_idx = 0

    # Consistent sector ordering and colors across all timeframes
    all_sector_names = set()
    for sector_data in all_timeframe_data.values():
        all_sector_names.update(sector_data.keys())
    sorted_sectors = sorted(all_sector_names)
    color_map = {s: COLORS[i % len(COLORS)] for i, s in enumerate(sorted_sectors)}

    for tf_name, sector_data in all_timeframe_data.items():
        _, _, tail_len = TIMEFRAMES[tf_name]
        trace_meta[tf_name] = {}

        for sector in sorted_sectors:
            if sector not in sector_data:
                continue
            df = sector_data[sector]
            color = color_map[sector]

            if len(df) < 2:
                continue

            indices = []

            # Tail (trailing path)
            tail = df.iloc[-tail_len:] if len(df) >= tail_len else df
            x_tail = tail["RS_Ratio"].tolist()
            y_tail = tail["RS_Momentum"].tolist()
            n_pts = len(x_tail)

            # Current position (last point)
            x_now = df["RS_Ratio"].iloc[-1]
            y_now = df["RS_Momentum"].iloc[-1]
            quadrant = _quadrant_label(x_now, y_now)

            # Trail line with graduated markers (small→large, faded→opaque)
            marker_sizes = [3 + 6 * k / max(n_pts - 1, 1) for k in range(n_pts)]
            marker_opacities = [0.3 + 0.6 * k / max(n_pts - 1, 1) for k in range(n_pts)]

            fig.add_trace(go.Scatter(
                x=x_tail, y=y_tail,
                mode="lines+markers",
                line=dict(color=color, width=2),
                marker=dict(
                    size=marker_sizes,
                    color=color,
                    opacity=marker_opacities,
                    line=dict(width=0.5, color="white"),
                ),
                showlegend=False,
                hoverinfo="skip",
                visible=(tf_name == timeframe_names[0]),
            ))
            indices.append(trace_idx)
            trace_idx += 1

            # Current dot + label
            dates_str = df.index[-1].strftime("%d-%b-%Y") if hasattr(df.index[-1], "strftime") else str(df.index[-1])
            hover = (
                "<b>%s</b><br>"
                "Date: %s<br>"
                "RS-Ratio: %%{x:.2f}<br>"
                "RS-Mom: %%{y:.2f}<br>"
                "Quadrant: %s"
                "<extra></extra>"
            ) % (sector, dates_str, quadrant)

            fig.add_trace(go.Scatter(
                x=[x_now], y=[y_now],
                mode="markers+text",
                marker=dict(size=12, color=color,
                            line=dict(width=1.5, color="white"),
                            symbol="circle"),
                text=[sector],
                textposition="top center",
                textfont=dict(size=9, color=color),
                name="%s (%s)" % (sector, quadrant),
                hovertemplate=hover,
                visible=(tf_name == timeframe_names[0]),
            ))
            indices.append(trace_idx)
            trace_idx += 1

            trace_meta[tf_name][sector] = indices

    # Compute axis range from data
    x_lo, x_hi, y_lo, y_hi = _compute_axis_range(all_timeframe_data, TIMEFRAMES)

    # Quadrant background shapes — sized to axis range
    fig.add_shape(type="rect", x0=100, y0=100, x1=x_hi, y1=y_hi,
                  fillcolor="rgba(76, 175, 80, 0.07)", line=dict(width=0),
                  layer="below")
    fig.add_shape(type="rect", x0=100, y0=y_lo, x1=x_hi, y1=100,
                  fillcolor="rgba(255, 152, 0, 0.07)", line=dict(width=0),
                  layer="below")
    fig.add_shape(type="rect", x0=x_lo, y0=y_lo, x1=100, y1=100,
                  fillcolor="rgba(244, 67, 54, 0.07)", line=dict(width=0),
                  layer="below")
    fig.add_shape(type="rect", x0=x_lo, y0=100, x1=100, y1=y_hi,
                  fillcolor="rgba(33, 150, 243, 0.07)", line=dict(width=0),
                  layer="below")

    # Crosshairs at 100, 100
    fig.add_hline(y=100, line_dash="dash", line_color="gray", line_width=1)
    fig.add_vline(x=100, line_dash="dash", line_color="gray", line_width=1)

    # Quadrant labels
    annotations = [
        dict(x=0.99, y=0.99, xref="paper", yref="paper", text="<b>Leading</b>",
             showarrow=False, font=dict(size=14, color="rgba(76,175,80,0.5)"),
             xanchor="right", yanchor="top"),
        dict(x=0.99, y=0.01, xref="paper", yref="paper", text="<b>Weakening</b>",
             showarrow=False, font=dict(size=14, color="rgba(255,152,0,0.5)"),
             xanchor="right", yanchor="bottom"),
        dict(x=0.01, y=0.01, xref="paper", yref="paper", text="<b>Lagging</b>",
             showarrow=False, font=dict(size=14, color="rgba(244,67,54,0.5)"),
             xanchor="left", yanchor="bottom"),
        dict(x=0.01, y=0.99, xref="paper", yref="paper", text="<b>Improving</b>",
             showarrow=False, font=dict(size=14, color="rgba(33,150,243,0.5)"),
             xanchor="left", yanchor="top"),
    ]

    fig.update_layout(
        title=dict(text=title, font=dict(size=20)),
        xaxis=dict(title="JdK RS-Ratio \u2192", zeroline=False,
                    range=[x_lo, x_hi]),
        yaxis=dict(title="JdK RS-Momentum \u2192", zeroline=False,
                    range=[y_lo, y_hi]),
        hovermode="closest",
        template="plotly_white",
        height=900,
        width=None,
        annotations=annotations,
        showlegend=False,
        margin=dict(l=60, r=60, t=80, b=60),
    )

    return fig, trace_meta, sorted_sectors


# ─── Output ──────────────────────────────────────────────────────────────────

_RRG_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>RRG — Indian Sectors</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
#rrg-wrap{margin:0 auto;padding:10px}
#controls{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap;
  padding:8px 12px;background:#fafafa;border:1px solid #e0e0e0;
  border-radius:6px;margin-bottom:6px}
.cg{display:flex;flex-direction:column;gap:4px}
.cl{font-size:11px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.5px}
#tf-btns{display:flex;gap:4px;flex-wrap:wrap}
.tb{padding:5px 12px;border:1px solid #ccc;background:#fff;cursor:pointer;
    border-radius:4px;font-size:12px;transition:all .15s}
.tb:hover{background:#e3f2fd}
.tb.on{background:#1976D2;color:#fff;border-color:#1976D2}
.sb{padding:2px 8px;border:1px solid #ccc;background:#f5f5f5;cursor:pointer;
    border-radius:3px;font-size:11px;margin-left:6px}
.sb:hover{background:#e0e0e0}
#sboxes{display:flex;flex-wrap:wrap;gap:2px 10px}
#sboxes label{font-size:12px;cursor:pointer;white-space:nowrap;display:flex;align-items:center;gap:3px}
.cd{display:inline-block;width:10px;height:10px;border-radius:50%}
</style></head><body>
<div id="rrg-wrap">
 <div id="controls">
  <div class="cg">
   <span class="cl">Timeframe</span>
   <div id="tf-btns"></div>
  </div>
  <div class="cg" style="flex:1">
   <span class="cl">Sectors
    <button class="sb" onclick="selAll()">All</button>
    <button class="sb" onclick="selNone()">None</button>
   </span>
   <div id="sboxes"></div>
  </div>
 </div>
 <div id="rrg-chart" style="min-height:85vh"></div>
 <details style="margin-top:12px;padding:8px 12px;background:#fafafa;border:1px solid #e0e0e0;border-radius:6px">
  <summary style="cursor:pointer;font-size:13px;font-weight:600;color:#555">Timeframe Reference</summary>
  <table style="width:100%;border-collapse:collapse;margin-top:8px;font-size:12px">
   <tr style="background:#e3f2fd"><th style="padding:5px 8px;text-align:left;border:1px solid #ccc">Timeframe</th><th style="padding:5px 8px;text-align:center;border:1px solid #ccc">SMA</th><th style="padding:5px 8px;text-align:center;border:1px solid #ccc">Tail</th><th style="padding:5px 8px;text-align:left;border:1px solid #ccc">Data Used</th><th style="padding:5px 8px;text-align:left;border:1px solid #ccc">Meaning</th></tr>
   <tr><td style="padding:4px 8px;border:1px solid #ddd">3 Day</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">3</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">8</td><td style="padding:4px 8px;border:1px solid #ddd">Daily (raw)</td><td style="padding:4px 8px;border:1px solid #ddd">Last 8 daily points</td></tr>
   <tr style="background:#f9f9f9"><td style="padding:4px 8px;border:1px solid #ddd">7 Day</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">7</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">12</td><td style="padding:4px 8px;border:1px solid #ddd">Daily (raw)</td><td style="padding:4px 8px;border:1px solid #ddd">Last 12 daily points</td></tr>
   <tr><td style="padding:4px 8px;border:1px solid #ddd">2 Week</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">10</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">15</td><td style="padding:4px 8px;border:1px solid #ddd">Daily (raw)</td><td style="padding:4px 8px;border:1px solid #ddd">Last 15 daily points</td></tr>
   <tr style="background:#f9f9f9"><td style="padding:4px 8px;border:1px solid #ddd">12 Day</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">12</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">18</td><td style="padding:4px 8px;border:1px solid #ddd">Daily (raw)</td><td style="padding:4px 8px;border:1px solid #ddd">Last 18 daily points</td></tr>
   <tr><td style="padding:4px 8px;border:1px solid #ddd">3 Week</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">15</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">20</td><td style="padding:4px 8px;border:1px solid #ddd">Daily (raw)</td><td style="padding:4px 8px;border:1px solid #ddd">Last 20 daily points</td></tr>
   <tr style="background:#f9f9f9"><td style="padding:4px 8px;border:1px solid #ddd">Weekly</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">10</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">12</td><td style="padding:4px 8px;border:1px solid #ddd">Resampled W-FRI</td><td style="padding:4px 8px;border:1px solid #ddd">Last 12 weeks (~3 months)</td></tr>
   <tr><td style="padding:4px 8px;border:1px solid #ddd">Monthly</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">4</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">6</td><td style="padding:4px 8px;border:1px solid #ddd">Resampled month-end</td><td style="padding:4px 8px;border:1px solid #ddd">Last 6 months</td></tr>
   <tr style="background:#f9f9f9"><td style="padding:4px 8px;border:1px solid #ddd">Quarterly</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">2</td><td style="padding:4px 8px;text-align:center;border:1px solid #ddd">4</td><td style="padding:4px 8px;border:1px solid #ddd">Resampled quarter-end</td><td style="padding:4px 8px;border:1px solid #ddd">Last 4 quarters (~1 year)</td></tr>
  </table>
 </details>
</div>
<script>
(function(){
var D=__FIG_DATA__,
    L=__FIG_LAYOUT__,
    M=__TRACE_META__,
    S=__SECTORS__,
    T=__TIMEFRAMES__,
    C=__COLORS__,
    N=D.length,aT=T[0],sel=new Set(S.filter(function(s){return s.startsWith('C: ')}));
Plotly.newPlot('rrg-chart',D,L,{responsive:true,displayModeBar:true});
var tD=document.getElementById('tf-btns');
T.forEach(function(tf,i){
 var b=document.createElement('button');
 b.className='tb'+(i===0?' on':'');b.textContent=tf;
 b.onclick=function(){aT=tf;
  document.querySelectorAll('.tb').forEach(function(x){x.classList.remove('on')});
  b.classList.add('on');upd()};
 tD.appendChild(b)});
var sD=document.getElementById('sboxes');
S.forEach(function(s){
 var l=document.createElement('label'),c=document.createElement('input');
 c.type='checkbox';c.checked=s.startsWith('C: ');
 c.onchange=function(){if(c.checked)sel.add(s);else sel.delete(s);upd()};
 var d=document.createElement('span');d.className='cd';
 d.style.background=C[s]||'#999';
 l.appendChild(c);l.appendChild(d);
 l.appendChild(document.createTextNode(' '+s));sD.appendChild(l)});
window.selAll=function(){S.forEach(function(s){sel.add(s)});
 document.querySelectorAll('#sboxes input').forEach(function(c){c.checked=true});upd()};
window.selNone=function(){sel.clear();
 document.querySelectorAll('#sboxes input').forEach(function(c){c.checked=false});upd()};
function upd(){
 var v=[];for(var i=0;i<N;i++)v.push(false);
 var m=M[aT]||{};
 sel.forEach(function(s){(m[s]||[]).forEach(function(j){v[j]=true})});
 Plotly.restyle('rrg-chart','visible',v)}
})();
</script></body></html>"""


def save_excel(all_timeframe_data, output_path):
    """Save RS-Ratio & RS-Momentum data for all timeframes to Excel."""
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for tf_name, sector_data in all_timeframe_data.items():
            # Build a summary table for this timeframe
            rows = []
            for sector in sorted(sector_data.keys()):
                df = sector_data[sector]
                if df.empty:
                    continue
                x = df["RS_Ratio"].iloc[-1]
                y = df["RS_Momentum"].iloc[-1]
                rows.append({
                    "Sector": sector,
                    "RS-Ratio": round(x, 2),
                    "RS-Momentum": round(y, 2),
                    "Quadrant": _quadrant_label(x, y),
                    "Date": df.index[-1].strftime("%d-%b-%Y") if hasattr(df.index[-1], "strftime") else str(df.index[-1]),
                })

            summary = pd.DataFrame(rows)
            if not summary.empty:
                summary = summary.sort_values("RS-Ratio", ascending=False)
            sheet = "RRG %s" % tf_name
            summary.to_excel(writer, sheet_name=sheet[:31], index=False)

    print("  Excel saved: %s" % output_path)


def save_chart_html(fig, trace_meta, sorted_sectors, output_path):
    """Save chart as standalone HTML with interactive controls."""
    fig_dict = fig.to_dict()
    fig_data = json.dumps(fig_dict["data"], default=str)
    fig_layout = json.dumps(fig_dict["layout"], default=str)

    color_map = {s: COLORS[i % len(COLORS)] for i, s in enumerate(sorted_sectors)}
    timeframe_names = list(trace_meta.keys())

    html = _RRG_HTML_TEMPLATE
    html = html.replace("__FIG_DATA__", fig_data)
    html = html.replace("__FIG_LAYOUT__", fig_layout)
    html = html.replace("__TRACE_META__", json.dumps(trace_meta))
    html = html.replace("__SECTORS__", json.dumps(sorted_sectors))
    html = html.replace("__TIMEFRAMES__", json.dumps(timeframe_names))
    html = html.replace("__COLORS__", json.dumps(color_map))

    with open(output_path, "w") as f:
        f.write(html)
    print("  HTML chart saved: %s" % output_path)


# ─── Main ────────────────────────────────────────────────────────────────────

def run(output_prefix=None):
    """Main entry point.

    Returns:
        Tuple of (all_timeframe_data, fig, excel_path, html_path)
        or None on failure.
    """
    print("=" * 60)
    print("Relative Rotation Graph — Indian Sectors")
    print("=" * 60)

    # 1. Fetch data
    print("\n[1] Fetching 1Y daily price data ...")
    daily = fetch_all_prices()
    if daily.empty or BENCHMARK_NAME not in daily.columns:
        print("  ERROR: Could not fetch price data.")
        return None

    # 1b. Build custom sector indices and merge
    print("\n[1b] Building custom sector indices ...")
    custom = _build_custom_indices(daily[BENCHMARK_NAME])
    if not custom.empty:
        # Align indices and merge
        daily = daily.join(custom, how="left")
        print("  Merged %d custom indices into price data" % len(custom.columns))

    # 2. Compute RS for each timeframe
    all_timeframe_data = {}
    for tf_name, (resample_rule, sma_period, _) in TIMEFRAMES.items():
        print("\n[2] Computing RS — %s (SMA=%d) ..." % (tf_name, sma_period))
        if resample_rule is None:
            resampled = daily.copy()
        else:
            resampled = resample_prices(daily, resample_rule)
        rs_dict = compute_all_rs(resampled, sma_period)
        all_timeframe_data[tf_name] = rs_dict
        print("  %s: %d sectors computed" % (tf_name, len(rs_dict)))

    if not any(all_timeframe_data.values()):
        print("  ERROR: No RS data computed for any timeframe.")
        return None

    # 3. Build chart
    print("\n[3] Building RRG chart ...")
    fig, trace_meta, sorted_sectors = create_rrg_chart(all_timeframe_data)

    # 4. Save outputs
    if output_prefix is None:
        output_prefix = os.path.join(SCRIPT_DIR, "rrg_chart")

    excel_path = output_prefix + ".xlsx"
    html_path = output_prefix + "_chart.html"

    print("\n[4] Saving outputs ...")
    save_excel(all_timeframe_data, excel_path)
    save_chart_html(fig, trace_meta, sorted_sectors, html_path)

    # Summary
    print("\n" + "=" * 60)
    print("DONE — RRG Chart")
    print("=" * 60)
    for tf_name, rs_dict in all_timeframe_data.items():
        if not rs_dict:
            continue
        print("\n  %s:" % tf_name)
        for sector in sorted(rs_dict.keys()):
            df = rs_dict[sector]
            x = df["RS_Ratio"].iloc[-1]
            y = df["RS_Momentum"].iloc[-1]
            q = _quadrant_label(x, y)
            print("    %-15s  Ratio=%6.2f  Mom=%6.2f  [%s]" % (sector, x, y, q))

    return all_timeframe_data, fig, excel_path, html_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RRG Chart — Indian Sectors")
    parser.add_argument("-o", "--output", help="Output filename prefix")
    args = parser.parse_args()
    run(output_prefix=args.output)
