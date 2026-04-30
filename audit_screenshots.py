"""
Audit: would breakout_scanner.py have caught the user's screenshot picks?

For each ticker, replay the scanner at several "as-of" dates leading up to
the visible April-2026 breakout. Reports best score reached, distance to R,
and which flags fired -- so we can honestly say whether the scanner would
have surfaced the setup BEFORE the breakout.
"""
import os
import sys
import warnings
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import breakout_scanner as bs

TICKERS = [
    "SKYGOLD.NS",
    "AZAD.NS",
    "SCI.NS",
    "SYRMA.NS",
    "QPOWER.NS",
    "MCX.NS",
    "WELCORP.NS",
    "JAYNECOIND.NS",
    "KRN.NS",
    "KECL.NS",
]

# As-of dates: scan a window before/around the supposed April-2026 breakouts
ASOF_DATES = [
    "2026-03-15",
    "2026-03-25",
    "2026-04-01",
    "2026-04-08",
    "2026-04-15",
    "2026-04-22",
]

START = "2024-01-01"
END   = "2026-04-30"


def fetch(ticker):
    df = yf.download(ticker, start=START, end=END, progress=False,
                     auto_adjust=False, threads=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    return df


def fetch_bench():
    df = yf.download("^NSEI", start=START, end=END, progress=False,
                     auto_adjust=False, threads=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df["Close"].dropna()


def audit_one(ticker, df_full, bench_full):
    rows = []
    for asof in ASOF_DATES:
        asof_ts = pd.Timestamp(asof)
        df = df_full.loc[:asof_ts]
        bench = bench_full.loc[:asof_ts]
        if len(df) < 150:
            rows.append({"asof": asof, "status": "insufficient history"})
            continue
        try:
            res = bs.detect_resistance(df)
            if res is None:
                rows.append({"asof": asof, "status": "no resistance found"})
                continue
            score = bs.compute_score(df, res, bench)
            close = float(df["Close"].iloc[-1])
            dist = (res["R"] - close) / close * 100.0
            pp = bs.pocket_pivot(df, res["R"])
            sq = bs.ttm_squeeze_on(df)
            lvs = bs.liquidity_vacuum_score(df, res["R"], res["base_start"])
            rs_pos = score["rs"] > 0
            hc = bool(pp and sq and rs_pos and dist <= 2.0
                      and lvs["lvs"] >= 0.5)
            rows.append({
                "asof": asof,
                "score": round(score["score"], 1),
                "close": round(close, 2),
                "R": round(res["R"], 2),
                "dist_pct": round(dist, 2),
                "touches": res["touches"],
                "base_days": res["base_len_days"],
                "pp": pp, "sq": sq, "rs+": rs_pos,
                "lvs": round(lvs["lvs"], 2),
                "HC": hc,
                "WL": score["score"] >= bs.WATCHLIST_MIN_SCORE,
                "TR": score["score"] >= bs.TRIGGER_MIN_SCORE,
            })
        except Exception as e:
            rows.append({"asof": asof, "status": f"err: {e}"})
    return rows


def main():
    print("Fetching benchmark ^NSEI ...")
    bench = fetch_bench()
    print(f"  benchmark rows: {len(bench)}")
    summary = []
    for t in TICKERS:
        print(f"\n=== {t} ===")
        df = fetch(t)
        if df is None or len(df) < 200:
            print("  ✗ no/insufficient data on yfinance")
            summary.append({"ticker": t, "best_score": None,
                            "ever_watchlist": False, "ever_HC": False,
                            "note": "no data"})
            continue
        rows = audit_one(t, df, bench)
        df_out = pd.DataFrame(rows)
        print(df_out.to_string(index=False))
        scored = df_out[df_out.get("score").notna()] if "score" in df_out else df_out
        best = scored["score"].max() if not scored.empty and "score" in scored else None
        ever_wl = bool((scored.get("score", pd.Series(dtype=float)) >= bs.WATCHLIST_MIN_SCORE).any()) \
                  if "score" in scored else False
        ever_tr = bool((scored.get("score", pd.Series(dtype=float)) >= bs.TRIGGER_MIN_SCORE).any()) \
                  if "score" in scored else False
        ever_hc = bool(scored.get("HC", pd.Series(dtype=bool)).any()) \
                  if "HC" in scored else False
        summary.append({"ticker": t, "best_score": best,
                        f"WL>={bs.WATCHLIST_MIN_SCORE}": ever_wl,
                        f"TR>={bs.TRIGGER_MIN_SCORE}": ever_tr,
                        "HC": ever_hc})

    print("\n\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(pd.DataFrame(summary).to_string(index=False))


if __name__ == "__main__":
    main()
