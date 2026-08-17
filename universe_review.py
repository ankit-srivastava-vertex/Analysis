#!/usr/bin/env python3
"""
universe_review.py
==================
Head-to-head validation: does the breakout scanner actually ADD VALUE over the
raw filtered universe it selects from?

For every matured week it takes the two RAW UNIVERSE sheets (MPD Data,
Screener Data) and the two BREAKOUT sheets (MPD Breakouts, Screener Breakouts)
from breakout_watchlist.xlsx, then measures the *realised outcome* of every
stock — independent of resistance levels — so universe and breakout candidates
are judged on the exact same yardstick:

  tradeable = max_gain_pct >= 15%   (a real, sellable rally)
  big_win   = max_gain_pct >= 25%
  positive  = pct_change  >= 0
  dud       = max_gain < 5% AND ended red
  DATA_ERROR= post-scan high < 50% of ref (split/bonus artifact) -> excluded

For each universe it compares three cohorts:
  ALL universe   |  BREAKOUT (scanner-flagged)  |  REJECTED (universe minus BO)
The scanner "adds value" if BREAKOUT tradeable% >> ALL/REJECTED tradeable%.

This is the companion to breakout_review.py + breakout_deep_analysis.py and is
run alongside them on every "let's review".

Usage:
  python3 universe_review.py                 # all matured weeks
  python3 universe_review.py --weeks 1 2 3    # specific weeks
  python3 universe_review.py --min-days 15    # maturity gate (default 15)
"""
from __future__ import annotations
import argparse
import numpy as np
import pandas as pd

from breakout_review import (
    _discover_weeks, _load_week, _fetch_ohlcv_bulk, TODAY,
    SPLIT_ARTIFACT_RATIO,
)

TRADE_WIN_PCT = 15.0
BIG_WIN_PCT   = 25.0
DUD_GAIN      = 5.0
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 40)

# (data sheet, ticker col, ref-price col or None to derive, breakout sheet)
UNIVERSES = [
    ("MPD Data",      "Yahoo",  "Last Close", "MPD Breakouts",      "MPD"),
    ("Screener Data", "Ticker", None,          "Screener Breakouts", "Screener"),
]


def _scan_close_from_ohlcv(df, scan_date):
    """Reference close = last close on/before scan_date."""
    try:
        idx = df.index.date if hasattr(df.index, "date") else pd.to_datetime(df.index).date
        pre = df[idx <= scan_date]
        if not pre.empty:
            return float(pre["Close"].iloc[-1])
    except Exception:
        pass
    return None


def _outcome(ticker, ref_close, ohlcv, scan_date):
    """Outcome-only classification (no resistance)."""
    if ticker not in ohlcv or ref_close is None or ref_close <= 0:
        return None
    df = ohlcv[ticker]
    try:
        idx = df.index.date if hasattr(df.index, "date") else pd.to_datetime(df.index).date
        post = df[idx > scan_date]
    except Exception:
        post = df.tail(5)
    if post.empty:
        return None
    max_high = float(post["High"].max())
    current = float(df["Close"].iloc[-1])
    max_gain = (max_high - ref_close) / ref_close * 100
    pct_change = (current - ref_close) / ref_close * 100
    if max_high < ref_close * SPLIT_ARTIFACT_RATIO:
        status = "DATA_ERROR"
    else:
        status = "OK"
    return {
        "ref_close": round(ref_close, 2),
        "max_high": round(max_high, 2),
        "max_gain_pct": round(max_gain, 2),
        "pct_change": round(pct_change, 2),
        "status": status,
        "tradeable": int(max_gain >= TRADE_WIN_PCT),
        "big_win": int(max_gain >= BIG_WIN_PCT),
        "positive": int(pct_change >= 0),
        "dud": int(max_gain < DUD_GAIN and pct_change < 0),
    }


def _collect(weeks, min_days):
    """Build the full per-stock record set across matured weeks."""
    records = []
    ticker_ref = {}   # (ticker, week) -> ref_close for MPD (has Last Close)
    all_tickers = set()
    week_meta = []    # (week, folder, scan_date, sheets)

    for wk, folder in weeks:
        sheets, scan_date, _ = _load_week(wk, folder)
        if sheets is None:
            continue
        days = (TODAY - scan_date).days
        if days < min_days:
            print(f"  Week {wk}: only {days}d old (< {min_days}) — skipped")
            continue
        week_meta.append((wk, folder, scan_date, sheets, days))
        for data_sheet, tcol, refcol, bo_sheet, src in UNIVERSES:
            if data_sheet not in sheets:
                continue
            udf = sheets[data_sheet]
            if tcol not in udf.columns:
                continue
            bo_syms = set()
            if bo_sheet in sheets and "symbol" in sheets[bo_sheet].columns:
                bo_syms = set(sheets[bo_sheet]["symbol"].dropna().astype(str))
            for _, row in udf.iterrows():
                t = str(row[tcol]).strip()
                if not t or t == "nan":
                    continue
                all_tickers.add(t)
                ref = None
                if refcol and refcol in udf.columns and pd.notna(row[refcol]):
                    try:
                        ref = float(row[refcol])
                    except (ValueError, TypeError):
                        ref = None
                ticker_ref[(t, wk, src)] = ref
                records.append({
                    "ticker": t, "week": wk, "source": src, "days": days,
                    "is_breakout": t in bo_syms, "ref_pre": ref,
                })
    return records, sorted(all_tickers), week_meta


def _summ(df, label):
    n = len(df)
    if n == 0:
        return {"cohort": label, "n": 0}
    return {
        "cohort": label, "n": n,
        "tradeable%": round(df["tradeable"].mean() * 100, 1),
        "big_win%": round(df["big_win"].mean() * 100, 1),
        "positive%": round(df["positive"].mean() * 100, 1),
        "avg_gain%": round(df["max_gain_pct"].mean(), 1),
        "dud%": round(df["dud"].mean() * 100, 1),
    }


def _hdr(t):
    print("\n" + "=" * 78 + f"\n  {t}\n" + "=" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", nargs="*", type=int)
    ap.add_argument("--min-days", type=int, default=15)
    args = ap.parse_args()

    weeks = _discover_weeks()
    if args.weeks:
        weeks = [(w, f) for (w, f) in weeks if w in args.weeks]

    _hdr(f"UNIVERSE vs BREAKOUT — outcome validation ({TODAY:%d-%b-%Y})")
    print(f"  Weeks discovered: {[f'{w}({f})' for w, f in weeks]}")
    print(f"  Maturity gate   : >= {args.min_days} days post-scan")

    records, tickers, week_meta = _collect(weeks, args.min_days)
    if not records:
        print("  No matured universe records found.")
        return
    print(f"  Weeks used      : {[w for w, *_ in week_meta]}")
    print(f"  Universe records: {len(records)} ({len(tickers)} unique tickers)")

    ohlcv = _fetch_ohlcv_bulk(tickers)

    # classify outcomes
    rows = []
    for r in records:
        scan_date = next(sd for w, f, sd, s, d in week_meta if w == r["week"])
        ref = r["ref_pre"]
        if ref is None:
            ref = _scan_close_from_ohlcv(ohlcv.get(r["ticker"]), scan_date) \
                  if r["ticker"] in ohlcv else None
        out = _outcome(r["ticker"], ref, ohlcv, scan_date)
        if out is None:
            continue
        rows.append({**r, **out})

    df = pd.DataFrame(rows)
    df = df[df["status"] != "DATA_ERROR"].copy()
    if df.empty:
        print("  No usable classified records.")
        return

    # ── Per-universe cohort comparison ──
    for src in ["MPD", "Screener"]:
        s = df[df["source"] == src]
        if s.empty:
            continue
        _hdr(f"{src} UNIVERSE  —  does the breakout scanner add value?")
        allu = _summ(s, f"{src} ALL universe")
        bo   = _summ(s[s["is_breakout"]], f"{src} BREAKOUT (flagged)")
        rej  = _summ(s[~s["is_breakout"]], f"{src} REJECTED (not flagged)")
        comp = pd.DataFrame([allu, bo, rej])
        print(comp.to_string(index=False))
        if bo.get("n") and allu.get("tradeable%"):
            lift = bo["tradeable%"] / allu["tradeable%"]
            lift_r = (bo["tradeable%"] / rej["tradeable%"]) if rej.get("tradeable%") else float("nan")
            print(f"\n  Scanner lift (BREAKOUT tradeable% / ALL)      : {lift:.2f}x")
            print(f"  Scanner lift (BREAKOUT tradeable% / REJECTED) : {lift_r:.2f}x")
            print(f"  Big-win lift  (BREAKOUT / ALL)                : "
                  f"{(bo['big_win%']/allu['big_win%']) if allu['big_win%'] else float('nan'):.2f}x")

    # ── MPD vs Screener head-to-head (breakout cohort) ──
    _hdr("MPD vs SCREENER — which universe & scanner is more accurate?")
    comp2 = []
    for src in ["MPD", "Screener"]:
        s = df[df["source"] == src]
        comp2.append({**_summ(s[s["is_breakout"]], f"{src} breakout"),
                      "universe_tradeable%": _summ(s, src).get("tradeable%")})
    print(pd.DataFrame(comp2).to_string(index=False))

    # ── Per-week trend (breakout tradeable% vs universe) ──
    _hdr("PER-WEEK TREND — breakout vs universe tradeable%")
    trend = []
    for wk in sorted(df["week"].unique()):
        w = df[df["week"] == wk]
        for src in ["MPD", "Screener"]:
            ws = w[w["source"] == src]
            if ws.empty:
                continue
            b = ws[ws["is_breakout"]]
            trend.append({
                "week": wk, "source": src,
                "uni_n": len(ws), "bo_n": len(b),
                "uni_trade%": round(ws["tradeable"].mean() * 100, 1),
                "bo_trade%": round(b["tradeable"].mean() * 100, 1) if len(b) else None,
                "uni_gain%": round(ws["max_gain_pct"].mean(), 1),
                "bo_gain%": round(b["max_gain_pct"].mean(), 1) if len(b) else None,
            })
    print(pd.DataFrame(trend).to_string(index=False))

    # ── Save ──
    out_path = f"Output/universe_review_{TODAY:%Y%m%d}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="All Records", index=False)
    print(f"\n  Detail written: {out_path}")
    print("\n" + "=" * 78)
    print("  READ: if BREAKOUT tradeable% > ALL/REJECTED, the scanner adds value.")
    print("  Compare MPD vs Screener lift to see which pipeline is more accurate.")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    main()
