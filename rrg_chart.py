"""
Relative Rotation Graph (RRG) — Indian Sector Indices
======================================================

Plots an interactive RRG chart showing sector rotation relative to
the Nifty 50 benchmark.  Sectors rotate clockwise through four
quadrants: Leading → Weakening → Lagging → Improving.

Features:
- 8 timeframes (3-day, 7-day, 2-week, 12-day, 3-week, weekly, monthly, quarterly)
- JdK RS-Ratio and RS-Momentum computation
- Constituent drill-down: click any custom sector dot to see per-stock
  mini-RRG with independent timeframe selector and stock checkboxes
- Only custom sectors (from index_constituents.json) have drill-down;
  standard Nifty sub-indices show "No constituent data available"
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
    """Download 1Y daily close for benchmark + all sector indices/ETFs."""
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

    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw[["Close"]]
        close.columns = [ticker_list[0]]

    rename_map = {}
    for col in close.columns:
        if col in name_by_ticker:
            rename_map[col] = name_by_ticker[col]
    close = close.rename(columns=rename_map)

    valid = close.dropna(axis=1, how="all")
    dropped = set(close.columns) - set(valid.columns)
    if dropped:
        print("  Dropped (no data): %s" % ", ".join(sorted(dropped)))

    print("  Got data for %d sectors + benchmark (%d trading days)" % (
        len(valid.columns) - 1, len(valid)))

    return valid


def _build_custom_indices(benchmark_series):
    """Build equal-weighted custom sector indices from index_constituents.json.

    Returns:
        (index_df, constituent_close_df)
        - index_df: DataFrame with Date index, one column per custom index (prefixed 'C:')
        - constituent_close_df: DataFrame with all constituent stock closes (ticker.NS columns)
    """
    if not os.path.exists(CONSTITUENTS_FILE):
        print("  No custom indices file found, skipping")
        return pd.DataFrame(), pd.DataFrame()

    with open(CONSTITUENTS_FILE, "r") as f:
        index_defs = json.load(f)

    all_tickers = set()
    for info in index_defs.values():
        for symbol in info["constituents"]:
            all_tickers.add(symbol + ".NS")

    if not all_tickers:
        return pd.DataFrame(), pd.DataFrame()

    print("  Downloading %d constituent stocks for %d custom indices ..." % (
        len(all_tickers), len(index_defs)))

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
        return pd.DataFrame(), pd.DataFrame()

    close = pd.concat(all_close, axis=1)
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

        returns = prices.pct_change().clip(-0.35, 0.35)
        portfolio_ret = returns.mean(axis=1)
        index_vals = (1 + portfolio_ret).cumprod()
        index_vals.iloc[0] = 1.0
        base_level = benchmark_series.iloc[0] if not benchmark_series.empty else 1000.0
        index_series = index_vals * base_level

        col_name = "C: " + index_name
        result[col_name] = index_series
        print("  [%s] Built: %d days, %d stocks" % (col_name, len(index_series.dropna()), len(available)))

    return result.dropna(how="all"), close


# ─── RS Computation ──────────────────────────────────────────────────────────

def resample_prices(daily_df, rule):
    """Resample daily close prices to a lower frequency."""
    return daily_df.resample(rule).last().dropna(how="all")


def compute_jdk_rs(sector_series, benchmark_series, sma_period):
    """Compute JdK RS-Ratio and RS-Momentum for one sector."""
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
    """Compute RS-Ratio & RS-Momentum for all sectors."""
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


# ─── Constituent RS Computation (NEW) ────────────────────────────────────────

def compute_constituent_quadrants(constituent_close, benchmark_series, index_defs):
    """Compute per-stock RS with full tail data for mini-RRG charts.

    Args:
        constituent_close: DataFrame with all stock closes (TICKER.NS columns)
        benchmark_series: Series of Nifty 50 daily close
        index_defs: dict from index_constituents.json

    Returns:
        dict: {timeframe_name: {sector_display_name: {symbol: {"x": [...], "y": [...]}}}}
        Each symbol has its RS-Ratio (x) and RS-Momentum (y) tail arrays for charting.
    """
    if constituent_close.empty or benchmark_series.empty:
        return {}

    print("\n[2b] Computing per-stock RS tails for constituent drill-down ...")

    # Map sector display name -> list of ticker.NS that have data
    sector_stocks = {}
    for index_name, info in index_defs.items():
        display_name = "C: " + index_name
        tickers = [s + ".NS" for s in info["constituents"]]
        available = [t for t in tickers if t in constituent_close.columns
                     and constituent_close[t].notna().sum() > 20]
        if available:
            sector_stocks[display_name] = available

    all_tf_constituents = {}

    for tf_name, (resample_rule, sma_period, tail_len) in TIMEFRAMES.items():
        # Resample constituent prices + benchmark
        if resample_rule is None:
            stock_prices = constituent_close.copy()
            bench = benchmark_series.copy()
        else:
            stock_prices = constituent_close.resample(resample_rule).last().dropna(how="all")
            bench = benchmark_series.resample(resample_rule).last().dropna()

        tf_result = {}
        for sector_name, tickers in sector_stocks.items():
            sector_data = {}

            for ticker in tickers:
                series = stock_prices[ticker].dropna() if ticker in stock_prices.columns else pd.Series()
                if len(series) < sma_period * 2:
                    continue
                common = series.index.intersection(bench.index)
                if len(common) < sma_period * 2:
                    continue

                try:
                    rs_data = compute_jdk_rs(series[common], bench[common], sma_period)
                    if rs_data.empty or len(rs_data) < 2:
                        continue
                    # Get tail data (same length as the sector tails)
                    tail = rs_data.iloc[-tail_len:] if len(rs_data) >= tail_len else rs_data
                    symbol = ticker.replace(".NS", "")
                    sector_data[symbol] = {
                        "x": [round(v, 2) for v in tail["RS_Ratio"].tolist()],
                        "y": [round(v, 2) for v in tail["RS_Momentum"].tolist()],
                    }
                except Exception:
                    continue

            if sector_data:
                tf_result[sector_name] = sector_data

        all_tf_constituents[tf_name] = tf_result
        n_sectors = len(tf_result)
        n_stocks = sum(len(v) for v in tf_result.values())
        print("  %s: %d stocks across %d sectors" % (tf_name, n_stocks, n_sectors))

    return all_tf_constituents


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
    """Compute axis limits from data, ensuring 100 is always visible."""
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

    x_lo = min(x_lo, 100)
    x_hi = max(x_hi, 100)
    y_lo = min(y_lo, 100)
    y_hi = max(y_hi, 100)

    x_pad = max((x_hi - x_lo) * pad_pct, 1.0)
    y_pad = max((y_hi - y_lo) * pad_pct, 1.0)

    return x_lo - x_pad, x_hi + x_pad, y_lo - y_pad, y_hi + y_pad


def create_rrg_chart(all_timeframe_data, title="Relative Rotation Graph \u2014 Indian Sectors"):
    """Create Plotly RRG figure with trace metadata for interactive controls."""
    fig = go.Figure()

    timeframe_names = list(all_timeframe_data.keys())
    trace_meta = {}
    trace_idx = 0

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

            tail = df.iloc[-tail_len:] if len(df) >= tail_len else df
            x_tail = tail["RS_Ratio"].tolist()
            y_tail = tail["RS_Momentum"].tolist()
            n_pts = len(x_tail)

            x_now = df["RS_Ratio"].iloc[-1]
            y_now = df["RS_Momentum"].iloc[-1]
            quadrant = _quadrant_label(x_now, y_now)

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

            dates_str = df.index[-1].strftime("%d-%b-%Y") if hasattr(df.index[-1], "strftime") else str(df.index[-1])
            hover = (
                "<b>%s</b><br>"
                "Date: %s<br>"
                "RS-Ratio: %%{x:.2f}<br>"
                "RS-Mom: %%{y:.2f}<br>"
                "Quadrant: %s<br>"
                "<i>Click for constituents</i>"
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

    x_lo, x_hi, y_lo, y_hi = _compute_axis_range(all_timeframe_data, TIMEFRAMES)

    fig.add_shape(type="rect", x0=100, y0=100, x1=x_hi, y1=y_hi,
                  fillcolor="rgba(76, 175, 80, 0.07)", line=dict(width=0), layer="below")
    fig.add_shape(type="rect", x0=100, y0=y_lo, x1=x_hi, y1=100,
                  fillcolor="rgba(255, 152, 0, 0.07)", line=dict(width=0), layer="below")
    fig.add_shape(type="rect", x0=x_lo, y0=y_lo, x1=100, y1=100,
                  fillcolor="rgba(244, 67, 54, 0.07)", line=dict(width=0), layer="below")
    fig.add_shape(type="rect", x0=x_lo, y0=100, x1=100, y1=y_hi,
                  fillcolor="rgba(33, 150, 243, 0.07)", line=dict(width=0), layer="below")

    fig.add_hline(y=100, line_dash="dash", line_color="gray", line_width=1)
    fig.add_vline(x=100, line_dash="dash", line_color="gray", line_width=1)

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
        xaxis=dict(title="JdK RS-Ratio \u2192", zeroline=False, range=[x_lo, x_hi]),
        yaxis=dict(title="JdK RS-Momentum \u2192", zeroline=False, range=[y_lo, y_hi]),
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

/* Constituent overlay panel — full-size like main chart */
#const-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;
  background:rgba(0,0,0,0.5);z-index:9999;justify-content:center;align-items:center}
#const-overlay.show{display:flex}
#const-panel{background:#fff;border-radius:10px;box-shadow:0 8px 32px rgba(0,0,0,0.3);
  width:96%;height:94vh;overflow:hidden;display:flex;flex-direction:column}
#const-header{display:flex;justify-content:space-between;align-items:center;
  padding:10px 16px;border-bottom:1px solid #e0e0e0;flex-shrink:0}
#const-header h3{margin:0;font-size:15px;color:#333}
#const-close{background:none;border:none;font-size:22px;cursor:pointer;color:#666;
  width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center}
#const-close:hover{background:#f0f0f0;color:#333}
#const-controls{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap;
  padding:8px 16px;background:#fafafa;border-bottom:1px solid #e0e0e0;flex-shrink:0}
#const-tf-btns{display:flex;gap:4px;flex-wrap:wrap}
#const-sboxes{display:flex;flex-wrap:wrap;gap:2px 10px;max-height:80px;overflow-y:auto}
#const-sboxes label{font-size:12px;cursor:pointer;white-space:nowrap;display:flex;align-items:center;gap:3px}
#const-chart{width:100%;flex:1;min-height:0;height:100%}
#const-nodata{padding:40px;text-align:center;color:#666;font-size:14px}
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

<!-- Constituent drill-down overlay -->
<div id="const-overlay" onclick="if(event.target===this)closeConstPanel()">
 <div id="const-panel">
  <div id="const-header">
   <h3 id="const-title">Sector Constituents</h3>
   <button id="const-close" onclick="closeConstPanel()">&times;</button>
  </div>
  <div id="const-controls">
   <div class="cg">
    <span class="cl">Timeframe</span>
    <div id="const-tf-btns"></div>
   </div>
   <div class="cg" style="flex:1">
    <span class="cl">Stocks
     <button class="sb" onclick="constSelAll()">All</button>
     <button class="sb" onclick="constSelNone()">None</button>
    </span>
    <div id="const-sboxes"></div>
   </div>
  </div>
  <div id="const-chart"></div>
  <div id="const-nodata" style="display:none">No constituent data available for this sector.<br><small>Only custom sectors (from index_constituents.json) have drill-down.</small></div>
 </div>
</div>

<script>
(function(){
var D=__FIG_DATA__,
    L=__FIG_LAYOUT__,
    M=__TRACE_META__,
    S=__SECTORS__,
    T=__TIMEFRAMES__,
    C=__COLORS__,
    CONST=__CONSTITUENTS__,
    N=D.length,aT=T[0],sel=new Set();

Plotly.newPlot('rrg-chart',D,L,{responsive:true,displayModeBar:true});

// Timeframe buttons
var tD=document.getElementById('tf-btns');
T.forEach(function(tf,i){
 var b=document.createElement('button');
 b.className='tb'+(i===0?' on':'');b.textContent=tf;
 b.onclick=function(){aT=tf;
  document.querySelectorAll('.tb').forEach(function(x){x.classList.remove('on')});
  b.classList.add('on');upd()};
 tD.appendChild(b)});

// Sector checkboxes
var sD=document.getElementById('sboxes');
S.forEach(function(s){
 var l=document.createElement('label'),c=document.createElement('input');
 c.type='checkbox';c.checked=false;
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
upd();

// ─── Click handler for constituent drill-down ────────────────────────────
var chart=document.getElementById('rrg-chart');
var STOCK_COLORS=[
 '#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd',
 '#8c564b','#e377c2','#7f7f7f','#bcbd22','#17becf',
 '#aec7e8','#ffbb78','#98df8a','#ff9896','#c5b0d5',
 '#c49c94','#f7b6d2','#c7c7c7','#dbdb8d','#9edae5',
 '#393b79','#637939','#8c6d31','#843c39','#7b4173'
];

// Constituent panel state
var cSector='',cTF=aT,cSel=new Set(),cSymbols=[],cColorMap={};

chart.on('plotly_click',function(data){
 if(!data||!data.points||!data.points.length) return;
 var pt=data.points[0];
 var trace=D[pt.curveNumber];
 if(!trace||!trace.text||!trace.text.length) return;
 var sectorName=trace.text[0];
 if(!sectorName) return;
 openConstPanel(sectorName);
});

function openConstPanel(sector){
 var overlay=document.getElementById('const-overlay');
 var title=document.getElementById('const-title');
 var chartDiv=document.getElementById('const-chart');
 var nodata=document.getElementById('const-nodata');
 var ctrlDiv=document.getElementById('const-controls');

 cSector=sector;
 cTF=aT;
 cSel=new Set();

 // Check if any timeframe has data for this sector
 var hasData=false;
 T.forEach(function(tf){if(CONST[tf]&&CONST[tf][sector])hasData=true;});

 if(!hasData){
  title.textContent=sector+' — Constituent RRG';
  chartDiv.style.display='none';
  ctrlDiv.style.display='none';
  nodata.style.display='block';
  overlay.classList.add('show');
  return;
 }

 nodata.style.display='none';
 ctrlDiv.style.display='flex';
 chartDiv.style.display='block';

 // Build timeframe buttons
 var tfBtns=document.getElementById('const-tf-btns');
 tfBtns.innerHTML='';
 T.forEach(function(tf){
  var b=document.createElement('button');
  b.className='tb'+(tf===cTF?' on':'');
  b.textContent=tf;
  b.onclick=function(){
   cTF=tf;cSel=new Set();
   document.querySelectorAll('#const-tf-btns .tb').forEach(function(x){x.classList.remove('on')});
   b.classList.add('on');
   buildConstCheckboxes();
   renderConstChart();
  };
  tfBtns.appendChild(b);
 });

 // Build stock checkboxes
 buildConstCheckboxes();

 // Render chart (empty initially — no stocks selected)
 renderConstChart();

 title.textContent=sector+' — Constituent RRG';
 overlay.classList.add('show');
}

function buildConstCheckboxes(){
 var sboxes=document.getElementById('const-sboxes');
 sboxes.innerHTML='';
 var tfData=CONST[cTF];
 var stocks=(tfData&&tfData[cSector])?tfData[cSector]:{};
 cSymbols=Object.keys(stocks).sort();
 cColorMap={};
 cSymbols.forEach(function(sym,i){cColorMap[sym]=STOCK_COLORS[i%STOCK_COLORS.length]});

 cSymbols.forEach(function(sym){
  var l=document.createElement('label');
  var c=document.createElement('input');
  c.type='checkbox';c.checked=cSel.has(sym);
  c.onchange=function(){if(c.checked)cSel.add(sym);else cSel.delete(sym);renderConstChart()};
  var d=document.createElement('span');d.className='cd';
  d.style.background=cColorMap[sym]||'#999';
  l.appendChild(c);l.appendChild(d);
  l.appendChild(document.createTextNode(' '+sym));
  sboxes.appendChild(l);
 });
}

window.constSelAll=function(){
 cSymbols.forEach(function(s){cSel.add(s)});
 document.querySelectorAll('#const-sboxes input').forEach(function(c){c.checked=true});
 renderConstChart();
};
window.constSelNone=function(){
 cSel.clear();
 document.querySelectorAll('#const-sboxes input').forEach(function(c){c.checked=false});
 renderConstChart();
};

function renderConstChart(){
 var chartDiv=document.getElementById('const-chart');
 var title=document.getElementById('const-title');
 title.textContent=cSector+' — Constituent RRG ('+cTF+')';

 var tfData=CONST[cTF];
 var stocks=(tfData&&tfData[cSector])?tfData[cSector]:{};
 var traces=[];
 var allX=[100],allY=[100]; // always include 100 for centering

 cSymbols.forEach(function(sym){
  if(!cSel.has(sym)) return;
  var sd=stocks[sym];
  if(!sd) return;
  var col=cColorMap[sym]||'#999';
  var n=sd.x.length;
  if(n<2) return;

  sd.x.forEach(function(v){allX.push(v)});
  sd.y.forEach(function(v){allY.push(v)});

  // Trail line with graduated markers
  var sizes=[],opacs=[];
  for(var k=0;k<n;k++){
   sizes.push(3+7*k/Math.max(n-1,1));
   opacs.push(0.3+0.6*k/Math.max(n-1,1));
  }
  traces.push({
   x:sd.x, y:sd.y,
   mode:'lines+markers',
   line:{color:col,width:2},
   marker:{size:sizes,color:col,opacity:opacs,line:{width:0.5,color:'white'}},
   showlegend:false,
   hoverinfo:'skip'
  });

  // Final dot + label
  var lastX=sd.x[n-1], lastY=sd.y[n-1];
  var q=(lastX>=100&&lastY>=100)?'Leading':(lastX>=100&&lastY<100)?'Weakening':(lastX<100&&lastY<100)?'Lagging':'Improving';
  traces.push({
   x:[lastX], y:[lastY],
   mode:'markers+text',
   marker:{size:12,color:col,line:{width:1.5,color:'white'},symbol:'circle'},
   text:[sym],
   textposition:'top center',
   textfont:{size:10,color:col},
   name:sym+' ('+q+')',
   showlegend:false,
   hovertemplate:'<b>'+sym+'</b><br>RS-Ratio: %{x:.2f}<br>RS-Mom: %{y:.2f}<br>Quadrant: '+q+'<extra></extra>'
  });
 });

 // Compute axis range
 var xLo=Math.min.apply(null,allX),xHi=Math.max.apply(null,allX);
 var yLo=Math.min.apply(null,allY),yHi=Math.max.apply(null,allY);
 xLo=Math.min(xLo,100);xHi=Math.max(xHi,100);
 yLo=Math.min(yLo,100);yHi=Math.max(yHi,100);
 var xPad=Math.max((xHi-xLo)*0.12,1.0);
 var yPad=Math.max((yHi-yLo)*0.12,1.0);
 xLo-=xPad;xHi+=xPad;yLo-=yPad;yHi+=yPad;

 var layout={
  xaxis:{title:'JdK RS-Ratio \u2192',range:[xLo,xHi],zeroline:false},
  yaxis:{title:'JdK RS-Momentum \u2192',range:[yLo,yHi],zeroline:false},
  hovermode:'closest',
  template:'plotly_white',
  margin:{l:55,r:30,t:20,b:50},
  showlegend:false,
  shapes:[
   {type:'rect',x0:100,y0:100,x1:xHi,y1:yHi,fillcolor:'rgba(76,175,80,0.07)',line:{width:0},layer:'below'},
   {type:'rect',x0:100,y0:yLo,x1:xHi,y1:100,fillcolor:'rgba(255,152,0,0.07)',line:{width:0},layer:'below'},
   {type:'rect',x0:xLo,y0:yLo,x1:100,y1:100,fillcolor:'rgba(244,67,54,0.07)',line:{width:0},layer:'below'},
   {type:'rect',x0:xLo,y0:100,x1:100,y1:yHi,fillcolor:'rgba(33,150,243,0.07)',line:{width:0},layer:'below'},
   {type:'line',x0:100,x1:100,y0:yLo,y1:yHi,line:{color:'gray',width:1,dash:'dash'}},
   {type:'line',x0:xLo,x1:xHi,y0:100,y1:100,line:{color:'gray',width:1,dash:'dash'}}
  ],
  annotations:[
   {x:0.99,y:0.99,xref:'paper',yref:'paper',text:'<b>Leading</b>',showarrow:false,font:{size:13,color:'rgba(76,175,80,0.5)'},xanchor:'right',yanchor:'top'},
   {x:0.99,y:0.01,xref:'paper',yref:'paper',text:'<b>Weakening</b>',showarrow:false,font:{size:13,color:'rgba(255,152,0,0.5)'},xanchor:'right',yanchor:'bottom'},
   {x:0.01,y:0.01,xref:'paper',yref:'paper',text:'<b>Lagging</b>',showarrow:false,font:{size:13,color:'rgba(244,67,54,0.5)'},xanchor:'left',yanchor:'bottom'},
   {x:0.01,y:0.99,xref:'paper',yref:'paper',text:'<b>Improving</b>',showarrow:false,font:{size:13,color:'rgba(33,150,243,0.5)'},xanchor:'left',yanchor:'top'}
  ]
 };

 Plotly.newPlot(chartDiv,traces,layout,{responsive:true,displayModeBar:true});
 setTimeout(function(){Plotly.Plots.resize(chartDiv)},50);
}

window.closeConstPanel=function(){
 document.getElementById('const-overlay').classList.remove('show');
 Plotly.purge(document.getElementById('const-chart'));
};

// Close on Escape key
document.addEventListener('keydown',function(e){
 if(e.key==='Escape') closeConstPanel();
});

})();
</script></body></html>"""


def save_excel(all_timeframe_data, output_path):
    """Save RS-Ratio & RS-Momentum data for all timeframes to Excel."""
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for tf_name, sector_data in all_timeframe_data.items():
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


def save_chart_html(fig, trace_meta, sorted_sectors, constituents_data, output_path):
    """Save chart as standalone HTML with interactive controls + constituent drill-down."""
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
    html = html.replace("__CONSTITUENTS__", json.dumps(constituents_data))

    with open(output_path, "w") as f:
        f.write(html)
    print("  HTML chart saved: %s" % output_path)


# ─── Main ────────────────────────────────────────────────────────────────────

def run(output_prefix=None):
    """Main entry point."""
    print("=" * 60)
    print("Relative Rotation Graph — Indian Sectors (v2 — Constituent Drill-Down)")
    print("=" * 60)

    # 1. Fetch data
    print("\n[1] Fetching 1Y daily price data ...")
    daily = fetch_all_prices()
    if daily.empty or BENCHMARK_NAME not in daily.columns:
        print("  ERROR: Could not fetch price data.")
        return None

    # 1b. Build custom sector indices and get constituent prices
    print("\n[1b] Building custom sector indices ...")
    custom, constituent_close = _build_custom_indices(daily[BENCHMARK_NAME])
    if not custom.empty:
        daily = daily.join(custom, how="left")
        print("  Merged %d custom indices into price data" % len(custom.columns))

    # 2. Compute RS for each timeframe (sector-level)
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

    # 2b. Compute per-stock constituent RS (for drill-down)
    constituents_data = {}
    if not constituent_close.empty:
        with open(CONSTITUENTS_FILE, "r") as f:
            index_defs = json.load(f)
        constituents_data = compute_constituent_quadrants(
            constituent_close, daily[BENCHMARK_NAME], index_defs
        )

    # 3. Build chart
    print("\n[3] Building RRG chart ...")
    fig, trace_meta, sorted_sectors = create_rrg_chart(all_timeframe_data)

    # 4. Save outputs
    if output_prefix is None:
        output_prefix = os.path.join(SCRIPT_DIR, "rrg_chart")

    html_path = output_prefix + ".html"

    print("\n[4] Saving outputs ...")
    save_chart_html(fig, trace_meta, sorted_sectors, constituents_data, html_path)

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

    return all_timeframe_data, fig, html_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RRG Chart — Indian Sectors")
    parser.add_argument("-o", "--output", help="Output filename prefix")
    args = parser.parse_args()
    run(output_prefix=args.output)
