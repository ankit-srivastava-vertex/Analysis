"""
Breakout Scanner v1 — Pre-Breakout Setup Detector
==================================================
Scans Nifty 500 daily for stocks setting up to break out of horizontal
resistance, using the 7-part framework:

  Part 1: Universe + OHLCV ingestion (Nifty 500 via NSE archive)
  Part 2: Horizontal resistance detection (fractal pivots + clustering)
  Part 3: Composite "Coiled Spring" Score (6 components, 0-100)
  Part 4: Pocket-Pivot trigger inside the base
  Part 5: Confirmation layers (Wyckoff spring, OBV slope, TTM Squeeze)
  Part 6: Multi-timeframe + risk architecture (stop, target, R:R)
  Part 7: Output — Excel watchlist + per-stock annotated Plotly charts

Standalone. No email. No run_all integration.

Usage:
  python breakout_scanner.py                   # full scan, defaults
  python breakout_scanner.py --max 50          # limit universe (quick test)
  python breakout_scanner.py --min-score 70    # change watchlist cut-off
  python breakout_scanner.py --charts 15       # # of top charts to render
"""

import os
import math
import argparse
import datetime
import warnings
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TODAY = datetime.date.today()
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# Universe source — multi_pct_down_report.xlsx (all sheets)
PCT_DOWN_REPORT = os.path.join(SCRIPT_DIR, "multi_pct_down_report.xlsx")

NIFTY50_BENCH = "^NSEI"  # Nifty 50 index on yfinance

# ─── Defaults / thresholds ──────────────────────────────────────────────────
LOOKBACK_DAYS = 750        # ~ 3y of daily history (chart context)
RES_LOOKBACK_DAYS = 600    # window used for resistance pivot search
BASE_MIN_DAYS = 35         # min length of consolidation base
BASE_MAX_DAYS = 400        # cap base length
RES_BAND_PCT = 0.035       # touches counted within +/- 3.5% of resistance
PROXIMITY_MAX_PCT = 0.08   # consider stocks within 8% of resistance
MIN_TOUCHES = 2
MIN_AVG_VOL = 50_000       # liquidity filter (avg 50d volume)
WATCHLIST_MIN_SCORE = 50
TRIGGER_MIN_SCORE = 65


# ─── Universe ────────────────────────────────────────────────────────────────

def fetch_universe() -> list:
    """Build ticker universe from multi_pct_down_report.xlsx.

    Reads the 'Yahoo' column from every sheet, deduplicates, and returns
    a sorted list of yfinance-style tickers (e.g. 'RELIANCE.NS', '543745.BO').
    """
    if not os.path.exists(PCT_DOWN_REPORT):
        raise FileNotFoundError(f"Universe file not found: {PCT_DOWN_REPORT}")

    xls = pd.ExcelFile(PCT_DOWN_REPORT)
    universe = set()
    for sheet in xls.sheet_names:
        df = pd.read_excel(xls, sheet_name=sheet)
        if "Yahoo" not in df.columns:
            print(f"  WARNING: sheet '{sheet}' has no 'Yahoo' column — skipped")
            continue
        tickers = (
            df["Yahoo"]
            .dropna()
            .astype(str)
            .str.strip()
        )
        tickers = set(tickers[tickers != ""])
        new = tickers - universe
        universe |= tickers
        print(f"  {sheet:<28}: {len(tickers):>5} symbols  (+{len(new)} new)")
    xls.close()

    universe = sorted(universe)
    print(f"  Total universe: {len(universe)} unique tickers\n")
    return universe


# ─── Data ingestion (yfinance) ──────────────────────────────────────────────

def fetch_ohlcv(tickers: list, lookback_days: int = LOOKBACK_DAYS,
                batch_size: int = 100) -> dict:
    """Bulk-download daily OHLCV via yfinance.

    `tickers` are full yfinance symbols, e.g. 'RELIANCE.NS' or '534109.BO'.
    Downloads in batches to avoid yfinance per-call ticker limits.
    Returns {ticker: DataFrame}.
    """
    import yfinance as yf
    end = TODAY + datetime.timedelta(days=1)
    start = TODAY - datetime.timedelta(days=int(lookback_days * 1.5))

    out = {}
    n = len(tickers)
    print(f"  Downloading OHLCV for {n} tickers in batches of {batch_size} ...")
    for i in range(0, n, batch_size):
        batch = tickers[i:i + batch_size]
        raw = yf.download(
            batch,
            start=start.isoformat(),
            end=end.isoformat(),
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            threads=True,
            progress=False,
        )
        if raw is None or raw.empty:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            top_level = set(raw.columns.get_level_values(0))
            for tk in batch:
                if tk not in top_level:
                    continue
                df = raw[tk].dropna(how="all").copy()
                if df.empty or len(df) < BASE_MIN_DAYS + 30:
                    continue
                df.columns = [c.title() for c in df.columns]
                out[tk] = df
        else:
            # single-ticker batch
            df = raw.dropna(how="all").copy()
            if not df.empty and len(df) >= BASE_MIN_DAYS + 30:
                df.columns = [c.title() for c in df.columns]
                out[batch[0]] = df
        print(f"    batch {i // batch_size + 1}: {min(i + batch_size, n)}/{n} done, "
              f"usable so far: {len(out)}")
    print(f"  Got usable history for {len(out)} tickers")
    return out


def fetch_benchmark(lookback_days: int = LOOKBACK_DAYS) -> pd.Series:
    import yfinance as yf
    end = TODAY + datetime.timedelta(days=1)
    start = TODAY - datetime.timedelta(days=int(lookback_days * 1.5))
    df = yf.download(
        NIFTY50_BENCH,
        start=start.isoformat(),
        end=end.isoformat(),
        interval="1d",
        auto_adjust=False,
        progress=False,
    )
    if df.empty:
        return pd.Series(dtype=float)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df["Close"].rename("Bench")


# ─── Part 2: Resistance detection ───────────────────────────────────────────

def fractal_pivots(highs: pd.Series, k: int = 3) -> pd.Series:
    """Boolean series: True where high is local max over [-k, +k] window."""
    h = highs.values
    n = len(h)
    out = np.zeros(n, dtype=bool)
    for i in range(k, n - k):
        if h[i] == h[i - k:i + k + 1].max() and h[i] >= h[i - 1] and h[i] >= h[i + 1]:
            out[i] = True
    return pd.Series(out, index=highs.index)


def detect_resistance(df: pd.DataFrame) -> Optional[dict]:
    """Find best horizontal resistance the stock is currently approaching.

    Uses long history (RES_LOOKBACK_DAYS) so the level is stable across days.
    Returns dict {R, base_start, touches, distance_pct, base_len_days}.
    """
    if len(df) < BASE_MIN_DAYS + 20:
        return None

    close = df["Close"]
    last_close = float(close.iloc[-1])

    # Use a long window so R doesn't drift day-to-day
    window = df.tail(RES_LOOKBACK_DAYS)
    # Two pivot scales: tight (k=3) and broad (k=8) -- broad gives stable
    # multi-month swing highs the eye picks out.
    piv_mask_tight = fractal_pivots(window["High"], k=3)
    piv_mask_broad = fractal_pivots(window["High"], k=8)
    pivots_tight = window["High"][piv_mask_tight]
    pivots_broad = window["High"][piv_mask_broad]
    pivots = pd.concat([pivots_tight, pivots_broad]).groupby(level=0).max()
    if len(pivots) < MIN_TOUCHES:
        return None

    # Cluster pivots into bands of width = RES_BAND_PCT * level (greedy)
    levels = sorted(pivots.tolist(), reverse=True)
    clusters = []
    for lvl in levels:
        placed = False
        for c in clusters:
            if abs(lvl - c["level"]) / c["level"] <= RES_BAND_PCT:
                c["sum"] += lvl
                c["count"] += 1
                c["level"] = c["sum"] / c["count"]
                placed = True
                break
        if not placed:
            clusters.append({"level": lvl, "sum": lvl, "count": 1})

    # Allow a wider distance window: from 3% above (just broken) to 8% below.
    candidates = []
    for c in clusters:
        R = c["level"]
        dist = (R - last_close) / last_close
        if c["count"] < MIN_TOUCHES:
            continue
        if dist < -0.03 or dist > PROXIMITY_MAX_PCT:
            continue
        cluster_pivots_idx = [
            ts for ts in pivots.index
            if abs(pivots.loc[ts] - R) / R <= RES_BAND_PCT
        ]
        if len(cluster_pivots_idx) < MIN_TOUCHES:
            continue
        base_start = min(cluster_pivots_idx)
        base_len = (df.index[-1] - base_start).days
        if base_len < BASE_MIN_DAYS:
            continue
        # Score: more touches better, longer base better, closer to 52w high better
        is_52w_high = R >= float(window["High"].max()) * 0.98
        candidates.append({
            "R": R,
            "touches": len(cluster_pivots_idx),
            "base_start": base_start,
            "base_len_days": base_len,
            "distance_pct": dist,
            "touch_dates": cluster_pivots_idx,
            "is_52w_high": is_52w_high,
        })

    if not candidates:
        return None
    # Best = is_52w_high first, then most touches, then closest distance
    candidates.sort(key=lambda c: (
        not c["is_52w_high"], -c["touches"], abs(c["distance_pct"])
    ))
    return candidates[0]


# ─── Indicators ─────────────────────────────────────────────────────────────

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    sign = np.sign(df["Close"].diff().fillna(0))
    return (sign * df["Volume"]).cumsum()


def linreg_slope(y: pd.Series) -> float:
    if y.dropna().size < 5:
        return 0.0
    yy = y.dropna().values
    xx = np.arange(len(yy))
    return float(np.polyfit(xx, yy, 1)[0])


def ttm_squeeze_on(df: pd.DataFrame, n: int = 20, mult_bb: float = 2.0,
                   mult_kc: float = 1.5) -> bool:
    """True if Bollinger Bands inside Keltner Channels on latest bar."""
    if len(df) < n + 5:
        return False
    c = df["Close"]
    ma = c.rolling(n).mean()
    sd = c.rolling(n).std()
    bb_up = ma + mult_bb * sd
    bb_dn = ma - mult_bb * sd
    a = atr(df, n)
    kc_up = ma + mult_kc * a
    kc_dn = ma - mult_kc * a
    return bool(bb_up.iloc[-1] < kc_up.iloc[-1] and bb_dn.iloc[-1] > kc_dn.iloc[-1])


# ─── Part 3: Composite "Coiled Spring" Score ────────────────────────────────

def compute_score(df: pd.DataFrame, res: dict, bench: pd.Series) -> dict:
    """Compute 0-100 composite score. Returns dict of components + total.

    Re-weighted (v2 calibration) after 10-ticker screenshot audit:
      A Base quality      25
      B Volatility contr  10
      C Volume dry-up      5
      D Proximity to R    20
      E Trend             15
      F Relative strength 10
      G 52w high          15
      Total              100
    """
    base_start = res["base_start"]
    base = df.loc[base_start:]
    if len(base) < 20:
        return {"score": 0.0}

    R = res["R"]
    last = df.iloc[-1]
    last_close = float(last["Close"])

    # ── A: Base quality (25) ──
    T = res["base_len_days"]
    Tmax = 120
    base_score = 25.0 * min(T / Tmax, 1.0)
    # touches multiplier: 2=0.75, 3=0.9, 4=1.0, 5+=1.0 (no penalty above 4)
    touches_mult = min(0.75 + 0.075 * (res["touches"] - 2), 1.0)
    if res["touches"] >= 5:
        touches_mult = 1.0
    base_score *= touches_mult
    lows_idx = base["Low"].rolling(11, center=True).min() == base["Low"]
    swing_lows = base["Low"][lows_idx].dropna()
    higher_lows = (linreg_slope(swing_lows) > 0) if len(swing_lows) >= 3 else False
    if higher_lows:
        base_score = min(base_score * 1.15, 25.0)

    # ── B: Volatility Contraction (10) ──
    a_series = atr(df, 14)
    atr_now = float(a_series.iloc[-10:].mean())
    atr_then = float(a_series.loc[base_start:].iloc[:20].mean()) if len(base) >= 20 else atr_now
    vcr = 1.0 - (atr_now / atr_then) if atr_then > 0 else 0.0
    vcr_score = 10.0 * max(min(vcr / 0.30, 1.0), 0.0)

    # ── C: Volume Dry-Up (5) ──
    v50 = float(df["Volume"].rolling(50).mean().iloc[-1])
    v10 = float(df["Volume"].iloc[-10:].mean())
    vdu = 1.0 - (v10 / v50) if v50 > 0 else 0.0
    vdu_score = 5.0 * max(min(vdu / 0.20, 1.0), 0.0)

    # ── D: Proximity (20) — reward being close to or just above R ──
    dist = (R - last_close) / last_close
    if -0.03 <= dist <= 0.08:
        prox_score = 20.0 * max(0.0, 1.0 - abs(dist) / 0.08)
    else:
        prox_score = 0.0

    # ── E: Trend (15) ──
    ma50 = df["Close"].rolling(50).mean()
    ma200 = df["Close"].rolling(200).mean()
    trend_score = 0.0
    if last_close > ma50.iloc[-1]:
        trend_score += 5
    if last_close > ma200.iloc[-1]:
        trend_score += 5
    if (linreg_slope(ma50.tail(20)) > 0
            and linreg_slope(ma200.tail(20)) > 0):
        trend_score += 5

    # ── F: Mansfield RS (10) ──
    rs_score = 0.0
    rs_value = 0.0
    if not bench.empty:
        b = bench.reindex(df.index).ffill()
        ratio = (df["Close"] / b).dropna()
        if len(ratio) >= 60:
            sma52w = ratio.rolling(min(252, len(ratio))).mean()
            mans = (ratio / sma52w - 1.0) * 100.0
            rs_value = float(mans.iloc[-1]) if not pd.isna(mans.iloc[-1]) else 0.0
            if rs_value > 0:
                rs_score = 5.0
            if linreg_slope(mans.tail(20)) > 0:
                rs_score += 5.0

    # ── G: 52-week high proximity (15) ──
    hi_52w = float(df["High"].tail(252).max())
    pct_off_high = (hi_52w - last_close) / hi_52w
    if pct_off_high <= 0.15:
        hi_score = 15.0 * (1.0 - pct_off_high / 0.15)
    else:
        hi_score = 0.0

    total = (base_score + vcr_score + vdu_score
             + prox_score + trend_score + rs_score + hi_score)

    return {
        "score": round(total, 2),
        "base_quality": round(base_score, 2),
        "vcr": round(vcr_score, 2),
        "vdu": round(vdu_score, 2),
        "proximity": round(prox_score, 2),
        "trend": round(trend_score, 2),
        "rs": round(rs_score, 2),
        "hi_52w": round(hi_score, 2),
        "vcr_raw": round(vcr, 3),
        "vdu_raw": round(vdu, 3),
        "atr_now": round(atr_now, 3),
        "atr_then": round(atr_then, 3),
        "higher_lows": higher_lows,
        "rs_value": round(rs_value, 3),
        "pct_off_52w_high": round(pct_off_high * 100, 2),
    }


# ─── Part 4: Pocket Pivot ───────────────────────────────────────────────────

def pocket_pivot(df: pd.DataFrame, R: float) -> bool:
    if len(df) < 12:
        return False
    last = df.iloc[-1]
    prior10 = df.iloc[-11:-1]
    down_days = prior10[prior10["Close"] < prior10["Close"].shift(1)]
    if down_days.empty:
        return False
    max_down_vol = float(down_days["Volume"].max())
    if last["Volume"] <= max_down_vol:
        return False
    if last["Close"] <= last["Open"]:
        return False
    rng = last["High"] - last["Low"]
    if rng <= 0:
        return False
    pos_in_range = (last["Close"] - last["Low"]) / rng
    if pos_in_range < 0.5:
        return False
    if abs(R - last["Close"]) / last["Close"] > 0.05:
        return False
    return True


# ─── Part 5: Confirmation layers ────────────────────────────────────────────

def wyckoff_spring(df: pd.DataFrame, base_start: pd.Timestamp) -> bool:
    base = df.loc[base_start:]
    if len(base) < 20:
        return False
    base_low = base["Low"].iloc[:-5].min()
    recent = base.iloc[-15:]
    wicks = recent[(recent["Low"] < base_low) & (recent["Close"] > base_low)]
    return not wicks.empty


def obv_divergence(df: pd.DataFrame, base_start: pd.Timestamp) -> bool:
    base = df.loc[base_start:]
    if len(base) < 30:
        return False
    o = obv(base)
    slope_ok = linreg_slope(o.tail(50)) > 0
    obv_at_high = o.iloc[-1] >= o.tail(50).max() * 0.98
    price_at_high = base["Close"].iloc[-1] >= base["Close"].tail(50).max() * 0.99
    return bool(slope_ok and obv_at_high and not price_at_high)


# ─── Part 6: Risk architecture ──────────────────────────────────────────────

def risk_plan(df: pd.DataFrame, res: dict) -> dict:
    R = res["R"]
    base = df.loc[res["base_start"]:]
    base_low = float(base["Low"].min())
    last_close = float(df["Close"].iloc[-1])
    swing_lows_recent = base["Low"].tail(20).min()
    stop = float(swing_lows_recent) * 0.99
    height = R - base_low
    target = R + height  # measured move
    risk = last_close - stop
    reward = target - last_close
    rr = round(reward / risk, 2) if risk > 0 else None
    return {
        "entry": round(last_close, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk_pct": round(risk / last_close * 100, 2) if last_close else None,
        "reward_pct": round(reward / last_close * 100, 2) if last_close else None,
        "rr": rr,
        "base_low": round(base_low, 2),
        "base_height": round(height, 2),
    }


# ─── Part 7: Output ─────────────────────────────────────────────────────────

def render_chart(symbol: str, df: pd.DataFrame, res: dict, score: dict,
                 risk: dict, flags: dict, out_path: str):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, row_heights=[0.75, 0.25],
        vertical_spacing=0.03,
    )
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"],
        low=df["Low"], close=df["Close"], name="Price",
        increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
    ), row=1, col=1)

    # Resistance line
    fig.add_hline(y=res["R"], line_color="#1e88e5", line_width=2,
                  annotation_text=f"R = {res['R']:.2f}", row=1, col=1)
    # Stop / target
    fig.add_hline(y=risk["stop"], line_color="#ef5350", line_dash="dash",
                  annotation_text=f"Stop {risk['stop']:.2f}", row=1, col=1)
    fig.add_hline(y=risk["target"], line_color="#26a69a", line_dash="dash",
                  annotation_text=f"Tgt {risk['target']:.2f}", row=1, col=1)

    # Mark base region
    fig.add_vrect(x0=res["base_start"], x1=df.index[-1],
                  fillcolor="#1e88e5", opacity=0.05, line_width=0, row=1, col=1)

    # Volume + 50d MA
    colors = np.where(df["Close"] >= df["Open"], "#26a69a", "#ef5350")
    fig.add_trace(go.Bar(x=df.index, y=df["Volume"], marker_color=colors,
                         name="Volume", showlegend=False), row=2, col=1)
    fig.add_trace(go.Scatter(x=df.index,
                             y=df["Volume"].rolling(50).mean(),
                             line=dict(color="white", width=1.2),
                             name="Vol 50DMA"), row=2, col=1)

    flags_str = ", ".join([k for k, v in flags.items() if v]) or "—"
    title = (
        f"{symbol} — Score {score['score']:.1f}/100 | "
        f"R={res['R']:.2f} ({res['distance_pct']*100:+.2f}%) | "
        f"Touches={res['touches']} | RR={risk['rr']} | "
        f"Flags: {flags_str}"
    )
    fig.update_layout(
        title=title, template="plotly_dark", height=720,
        xaxis_rangeslider_visible=False, showlegend=False,
    )
    fig.write_html(out_path, include_plotlyjs="cdn")


def write_excel(rows: list, out_path: str):
    df = pd.DataFrame(rows).sort_values("score", ascending=False)
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Watchlist", index=False)
        triggers = df[df["score"] >= TRIGGER_MIN_SCORE]
        if not triggers.empty:
            triggers.to_excel(w, sheet_name="Triggers", index=False)
        if "high_conviction" in df.columns:
            hc = df[df["high_conviction"] == True]  # noqa: E712
            if not hc.empty:
                # bring high_conviction columns to front
                cols_front = [
                    "symbol", "close", "resistance", "distance_pct",
                    "score", "pocket_pivot", "ttm_squeeze", "rs_positive",
                    "lvs", "rr", "stop", "target",
                ]
                others = [c for c in hc.columns if c not in cols_front]
                hc[cols_front + others].to_excel(
                    w, sheet_name="High Conviction", index=False)
    print(f"  Excel written: {out_path}")


# ─── Liquidity Vacuum Score (used by high-conviction rule) ───────────────

def liquidity_vacuum_score(df: pd.DataFrame, R: float,
                           base_start: pd.Timestamp,
                           bins: int = 80) -> dict:
    """Approximate Volume-at-Price using daily OHLC: each day distributes
    its volume uniformly across (Low, High). Compare volume traded in
    [R, R*1.15] vs [R*0.85, R].

    LVS = 1 - (above_vol / below_vol). Higher = thinner air above.
    """
    base = df.loc[base_start:].copy()
    if len(base) < 30:
        return {"lvs": 0.0}

    lo = R * 0.85
    hi = R * 1.15
    edges = np.linspace(lo, hi, bins + 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    bin_vol = np.zeros(bins)

    for _, row in base.iterrows():
        b_lo, b_hi, vol = float(row["Low"]), float(row["High"]), float(row["Volume"])
        if b_hi <= b_lo or vol <= 0:
            continue
        oh = min(b_hi, hi)
        ol = max(b_lo, lo)
        if oh <= ol:
            continue
        per_unit = vol / (b_hi - b_lo)
        for j in range(bins):
            seg_lo = max(edges[j], ol)
            seg_hi = min(edges[j + 1], oh)
            if seg_hi > seg_lo:
                bin_vol[j] += per_unit * (seg_hi - seg_lo)

    above_mask = centers > R
    above_vol = float(bin_vol[above_mask].sum())
    below_vol = float(bin_vol[~above_mask].sum())
    if below_vol <= 0:
        return {"lvs": 0.0}
    lvs = 1.0 - (above_vol / below_vol)
    lvs = max(min(lvs, 1.0), -1.0)
    return {"lvs": float(lvs)}


# ─── Scan driver ─────────────────────────────────────────────────

def scan(symbols: list, ohlcv: dict, bench: pd.Series,
         min_score: float) -> list:
    rows = []
    n = len(symbols)
    for i, sym in enumerate(symbols, 1):
        if sym not in ohlcv:
            continue
        df = ohlcv[sym]
        if df["Volume"].rolling(50).mean().iloc[-1] < MIN_AVG_VOL:
            continue
        try:
            res = detect_resistance(df)
            if res is None:
                continue
            score = compute_score(df, res, bench)
            if score["score"] < min_score:
                continue

            flags = {
                "pocket_pivot": pocket_pivot(df, res["R"]),
                "ttm_squeeze": ttm_squeeze_on(df),
                "wyckoff_spring": wyckoff_spring(df, res["base_start"]),
                "obv_divergence": obv_divergence(df, res["base_start"]),
            }
            # extras needed for the high-conviction trigger rule
            lvs = liquidity_vacuum_score(df, res["R"], res["base_start"])
            risk = risk_plan(df, res)
            distance_pct_value = round(res["distance_pct"] * 100, 2)
            rs_positive = score["rs"] > 0

            # === High-conviction trigger (calibrated on NSE main, 2018-2025) ===
            # pocket_pivot & ttm_squeeze & rs_positive & dist<=2% & lvs>=0.5
            high_conviction = bool(
                flags["pocket_pivot"]
                and flags["ttm_squeeze"]
                and rs_positive
                and distance_pct_value <= 2.0
                and lvs["lvs"] >= 0.5
            )

            rows.append({
                "symbol": sym,
                "high_conviction": high_conviction,
                "score": score["score"],
                "close": round(float(df["Close"].iloc[-1]), 2),
                "resistance": round(res["R"], 2),
                "distance_pct": distance_pct_value,
                "touches": res["touches"],
                "base_days": res["base_len_days"],
                **{k: score[k] for k in
                    ["base_quality", "vcr", "vdu", "proximity", "trend", "rs"]},
                "vcr_raw": score["vcr_raw"],
                "vdu_raw": score["vdu_raw"],
                "higher_lows": score["higher_lows"],
                "rs_positive": rs_positive,
                "lvs": round(lvs["lvs"], 3),
                **flags,
                **risk,
            })
        except Exception as e:
            print(f"  [{sym}] error: {e}")
        if i % 50 == 0:
            print(f"  scanned {i}/{n} ...")
    return rows


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Breakout Scanner v1")
    p.add_argument("--max", type=int, default=0,
                   help="cap universe size (0 = all)")
    p.add_argument("--min-score", type=float, default=WATCHLIST_MIN_SCORE)
    p.add_argument("--charts", type=int, default=20,
                   help="render top-N charts")
    p.add_argument("--lookback", type=int, default=LOOKBACK_DAYS)
    p.add_argument("--high-conviction", action="store_true",
                   help="only output setups matching the calibrated rule:"
                        " pocket_pivot & ttm_squeeze & rs_positive"
                        " & dist<=2%% & lvs>=0.5")
    args = p.parse_args()

    print("=" * 70)
    print(f"  BREAKOUT SCANNER v1 — {TODAY.strftime('%d-%b-%Y')}")
    print(f"  Source: {os.path.basename(PCT_DOWN_REPORT)}")
    if args.high_conviction:
        print("  Filter: HIGH-CONVICTION only"
              " (pocket_pivot & ttm_squeeze & rs_positive & dist<=2% & lvs>=0.5)")
    print("=" * 70)

    tickers = fetch_universe()
    if args.max > 0:
        tickers = tickers[:args.max]
        print(f"  Universe capped to {len(tickers)}")

    ohlcv = fetch_ohlcv(tickers, args.lookback)
    bench = fetch_benchmark(args.lookback)

    print("\n  Scanning ...")
    # When high-conviction is requested, the calibrated rule does NOT use
    # `score`, so bypass the score pre-filter to avoid losing valid triggers.
    effective_min_score = 0.0 if args.high_conviction else args.min_score
    rows = scan(list(ohlcv.keys()), ohlcv, bench, effective_min_score)
    print(f"\n  Candidates >= {effective_min_score}: {len(rows)}")

    if rows:
        # Diagnostics: per-condition pass counts for the high-conviction rule
        n_pp  = sum(1 for r in rows if r["pocket_pivot"])
        n_sq  = sum(1 for r in rows if r["ttm_squeeze"])
        n_rs  = sum(1 for r in rows if r["rs_positive"])
        n_d   = sum(1 for r in rows if r["distance_pct"] <= 2.0)
        n_lvs = sum(1 for r in rows if r["lvs"] >= 0.5)
        n_hc_pre = sum(1 for r in rows if r["high_conviction"])
        print("  HC condition pass rates:"
              f" pocket_pivot={n_pp}, ttm_squeeze={n_sq},"
              f" rs_positive={n_rs}, dist<=2%={n_d}, lvs>=0.5={n_lvs}"
              f" | ALL5={n_hc_pre}")

    # Always write the full candidate dump (diagnostic) before HC filtering
    if rows:
        excel_full = os.path.join(SCRIPT_DIR, "breakout_watchlist.xlsx")
        write_excel(rows, excel_full)

    if args.high_conviction:
        rows = [r for r in rows if r.get("high_conviction")]
        print(f"  High-conviction picks: {len(rows)}")

    if not rows:
        print("  No candidates found.")
        return

    n_hc = sum(1 for r in rows if r.get("high_conviction"))
    print(f"  Rows in output: {len(rows)} | High-conviction triggers: {n_hc}")

    if args.high_conviction:
        # Write a separate file just for the HC picks (full dump already saved)
        excel_path = os.path.join(SCRIPT_DIR, "breakout_high_conviction.xlsx")
        write_excel(rows, excel_path)

    # Charts: prefer high-conviction picks first, then by score
    rows_sorted = sorted(rows, key=lambda r: (not r.get("high_conviction"),
                                              -r["score"]))
    charts_dir = os.path.join(SCRIPT_DIR, "breakout_charts")
    os.makedirs(charts_dir, exist_ok=True)
    print(f"\n  Rendering top {min(args.charts, len(rows_sorted))} charts ...")
    for r in rows_sorted[:args.charts]:
        sym = r["symbol"]
        df = ohlcv[sym]
        res = detect_resistance(df)
        if res is None:
            continue
        score = compute_score(df, res, bench)
        risk = risk_plan(df, res)
        flags = {
            "pocket_pivot": r["pocket_pivot"],
            "ttm_squeeze": r["ttm_squeeze"],
            "wyckoff_spring": r["wyckoff_spring"],
            "obv_divergence": r["obv_divergence"],
        }
        prefix = "HC_" if r.get("high_conviction") else ""
        out = os.path.join(charts_dir, f"{prefix}{sym}_breakout.html")
        render_chart(sym, df.tail(args.lookback), res, score, risk, flags, out)
    print(f"  Charts saved to: {charts_dir}")

    print("\n  Top 10 (high-conviction first):")
    cols = ["symbol", "high_conviction", "score", "close", "resistance",
            "distance_pct", "touches", "base_days",
            "pocket_pivot", "ttm_squeeze", "rs_positive", "lvs", "rr"]
    top = pd.DataFrame(rows_sorted[:10])[cols]
    print(top.to_string(index=False))
    print("\nDONE.")


if __name__ == "__main__":
    main()
