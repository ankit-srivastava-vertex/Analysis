"""
Scorecard Walk-Forward Validator
================================
(module: breakout_scorecard_review.py)

The breakout side of this project already has a walk-forward harness
(``breakout_review.py`` + ``breakout_deep_analysis.py``) that measures whether
a breakout signal actually pays. The **scorecard** had no such feedback loop:
nobody had ever checked whether a high ``CompositeScore`` leads to better
forward returns than a low one. This module closes that gap.

Every scorecard run persists a dated snapshot of every scored name to
``data/scorecard_snapshots.csv`` (written by
``breakout_scanner_scorecard._append_snapshot``). This validator:

  1. loads those snapshots,
  2. keeps the ones old enough to have "matured" (>= ``--min-days``),
  3. re-fetches post-snapshot OHLCV via Angel One (same downloader the scanner
     and breakout_review use),
  4. computes forward returns (1w / 4w / 12w), max-gain and max-drawdown from
     each snapshot's own date, and
  5. reports whether ``CompositeScore`` / ``Verdict`` / each axis actually
     ranked the winners — the evidence needed before any composite re-weighting.

It writes ``Output/scorecard_review_YYYYMMDD_HHMMSS.xlsx`` and prints a
console summary. It changes NO scoring logic — it only measures.

USAGE
-----
    python3 breakout_scorecard_review.py                 # matured >= 7d
    python3 breakout_scorecard_review.py --min-days 30   # only >=30d matured
    python3 breakout_scorecard_review.py --tradeable 15  # win bar = +15% max gain

Classification mirrors the breakout deep-analysis "money" outcome:
    tradeable = max_gain_pct >= --tradeable  (a real sellable rally)
    dud       = max_gain_pct <  5%  AND  end_ret_pct < 0
"""

import os
import argparse
import datetime
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
SNAPSHOT_CSV = os.path.join(DATA_DIR, "scorecard_snapshots.csv")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "Output")

TODAY = datetime.date.today()

# forward-return horizons in TRADING days
HORIZONS = {"fwd_1w": 5, "fwd_4w": 20, "fwd_12w": 60}

AXIS_COLS = ["CompositeScore", "MomentumScore", "ValuationScore",
             "StageScore", "QualityScore", "ScanScore", "rr",
             "base_days", "base_range_pct", "distance_pct"]


def _load_snapshots():
    if not os.path.exists(SNAPSHOT_CSV):
        raise SystemExit(
            f"no snapshots found at {SNAPSHOT_CSV} — run the scorecard first "
            "(it now writes a snapshot every run)")
    df = pd.read_csv(SNAPSHOT_CSV)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df = df.dropna(subset=["date", "symbol"])
    return df


def _forward_metrics(candles, snap_date, entry_ref):
    """Compute forward-return metrics for one snapshot from its own date.

    ``candles`` is the symbol's daily OHLCV (DatetimeIndex). ``entry_ref`` is
    the snapshot Close/entry price. Returns a dict of forward metrics or None
    if there is no post-snapshot data.
    """
    if candles is None or candles.empty:
        return None
    idx = candles.index
    if not isinstance(idx, pd.DatetimeIndex):
        try:
            candles = candles.copy()
            candles.index = pd.to_datetime(candles.index)
        except Exception:
            return None
    snap_ts = pd.Timestamp(snap_date)
    fwd = candles[candles.index >= snap_ts]
    if fwd.empty or len(fwd) < 2:
        return None

    # entry reference: prefer the snapshot's stored price, else first close
    entry = entry_ref
    if entry is None or (isinstance(entry, float) and np.isnan(entry)) or entry <= 0:
        entry = float(fwd["Close"].iloc[0])
    if not entry or entry <= 0:
        return None

    closes = fwd["Close"].astype(float)
    highs = fwd["High"].astype(float) if "High" in fwd.columns else closes
    lows = fwd["Low"].astype(float) if "Low" in fwd.columns else closes

    out = {"maturity_days": int((TODAY - snap_date).days),
           "bars": int(len(fwd)), "entry_ref": round(entry, 2)}
    for name, k in HORIZONS.items():
        if len(closes) > k:
            out[name + "_pct"] = round((closes.iloc[k] / entry - 1) * 100, 2)
        else:
            out[name + "_pct"] = float("nan")
    out["end_ret_pct"] = round((closes.iloc[-1] / entry - 1) * 100, 2)
    out["max_gain_pct"] = round((highs.max() / entry - 1) * 100, 2)
    out["max_dd_pct"] = round((lows.min() / entry - 1) * 100, 2)
    return out


def _spearman(a, b):
    """Spearman rank correlation, NaN-safe."""
    s = pd.concat([pd.Series(a), pd.Series(b)], axis=1).dropna()
    if len(s) < 5:
        return float("nan")
    return round(s.iloc[:, 0].corr(s.iloc[:, 1], method="spearman"), 3)


def _bucket_table(df, by, tradeable_bar):
    g = df.groupby(by)
    tbl = pd.DataFrame({
        "n": g.size(),
        "tradeable_%": g["max_gain_pct"].apply(
            lambda s: round((s >= tradeable_bar).mean() * 100, 1)),
        "dud_%": g.apply(
            lambda d: round(((d["max_gain_pct"] < 5) &
                             (d["end_ret_pct"] < 0)).mean() * 100, 1)),
        "avg_max_gain": g["max_gain_pct"].mean().round(1),
        "avg_end_ret": g["end_ret_pct"].mean().round(1),
        "avg_fwd_4w": g["fwd_4w_pct"].mean().round(1),
    })
    return tbl


def run(min_days=7, tradeable_bar=15.0, lookback_buffer=15, verbose=True):
    snaps = _load_snapshots()
    snaps = snaps[snaps["date"].apply(lambda d: (TODAY - d).days >= min_days)]
    if snaps.empty:
        raise SystemExit(
            f"no snapshots matured >= {min_days} days yet — check back later")

    symbols = sorted(snaps["symbol"].astype(str).unique())
    earliest = min(snaps["date"])
    lookback = (TODAY - earliest).days + lookback_buffer

    if verbose:
        print("=" * 70)
        print(f"  SCORECARD WALK-FORWARD REVIEW — {TODAY:%d-%b-%Y}")
        print("=" * 70)
        print(f"  snapshots matured >= {min_days}d : {len(snaps)} rows "
              f"({len(symbols)} unique names, "
              f"{snaps['date'].nunique()} scan dates)")
        print(f"  fetching post-snapshot candles (lookback {lookback}d) …")

    import breakout_scanner_angel as bsa
    ohlcv = bsa.fetch_ohlcv(symbols, lookback)

    rows = []
    for _, s in snaps.iterrows():
        sym = str(s["symbol"])
        fm = _forward_metrics(ohlcv.get(sym), s["date"], _safe_float(s.get("entry")))
        if fm is None:
            # fall back to snapshot Close as entry reference
            fm = _forward_metrics(ohlcv.get(sym), s["date"],
                                  _safe_float(s.get("Close")))
        if fm is None:
            continue
        rec = {c: s.get(c) for c in (
            ["date", "symbol", "Sector", "Verdict", "Stage", "Substage",
             "QualityFlag"] + AXIS_COLS)}
        rec.update(fm)
        rows.append(rec)

    if not rows:
        raise SystemExit("no snapshots could be matured with candle data")

    res = pd.DataFrame(rows)
    res["VerdictBase"] = res["Verdict"].astype(str).str.replace(
        r"\s*[↑·↓]\s*$", "", regex=True).str.strip()
    res["tradeable"] = res["max_gain_pct"] >= tradeable_bar

    # composite decile (rank-based, robust to distribution)
    valid_comp = res["CompositeScore"].notna()
    res["CompScoreDecile"] = np.nan
    if valid_comp.sum() >= 10:
        res.loc[valid_comp, "CompScoreDecile"] = pd.qcut(
            res.loc[valid_comp, "CompositeScore"], 10,
            labels=False, duplicates="drop") + 1

    _write_excel(res)

    if verbose:
        _print_summary(res, tradeable_bar)
    return res


def _safe_float(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if np.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _write_excel(res):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"scorecard_review_{stamp}.xlsx")
    try:
        with pd.ExcelWriter(path, engine="openpyxl") as w:
            res.sort_values("date").to_excel(w, sheet_name="All Results",
                                             index=False)
            if res["VerdictBase"].notna().any():
                _bucket_table(res, "VerdictBase", 15.0).to_excel(
                    w, sheet_name="By Verdict")
            if res["CompScoreDecile"].notna().any():
                _bucket_table(res.dropna(subset=["CompScoreDecile"]),
                              "CompScoreDecile", 15.0).to_excel(
                    w, sheet_name="By CompScore Decile")
            if "Stage" in res.columns:
                _bucket_table(res, "Stage", 15.0).to_excel(
                    w, sheet_name="By Stage")
        print(f"  Excel written: {path}")
    except Exception as e:
        print(f"  [review] failed to write Excel: {e}")


def _print_summary(res, tradeable_bar):
    n = len(res)
    print(f"\n  matured rows scored: {n}")
    print(f"  overall tradeable (max_gain >= {tradeable_bar:g}%): "
          f"{res['tradeable'].mean() * 100:.1f}%   "
          f"avg max_gain {res['max_gain_pct'].mean():.1f}%   "
          f"avg end_ret {res['end_ret_pct'].mean():.1f}%")

    print("\n  ── by Verdict ──")
    print(_bucket_table(res, "VerdictBase", tradeable_bar)
          .sort_values("avg_max_gain", ascending=False).to_string())

    if res["CompScoreDecile"].notna().any():
        print("\n  ── by CompositeScore decile (10 = highest score) ──")
        print(_bucket_table(res.dropna(subset=["CompScoreDecile"]),
                            "CompScoreDecile", tradeable_bar).to_string())

    print("\n  ── does each axis RANK forward return? (Spearman vs outcome) ──")
    print(f"    {'axis':<16}{'ρ·max_gain':>12}{'ρ·end_ret':>12}"
          f"{'ρ·fwd_4w':>12}")
    for ax in AXIS_COLS:
        if ax in res.columns:
            print(f"    {ax:<16}"
                  f"{_spearman(res[ax], res['max_gain_pct']):>12}"
                  f"{_spearman(res[ax], res['end_ret_pct']):>12}"
                  f"{_spearman(res[ax], res['fwd_4w_pct']):>12}")
    print("\n  (ρ > 0 ⇒ higher axis value → bigger forward move ⇒ the axis "
          "ranks winners; ρ ≤ 0 ⇒ it does not. Use this to justify any\n"
          "   composite re-weighting — evidence first, no blind tuning.)")


def main():
    p = argparse.ArgumentParser(description="Scorecard walk-forward validator")
    p.add_argument("--min-days", type=int, default=7,
                   help="only evaluate snapshots at least this many days old")
    p.add_argument("--tradeable", type=float, default=15.0,
                   help="max-gain %% that counts as a tradeable win")
    args = p.parse_args()
    run(min_days=args.min_days, tradeable_bar=args.tradeable)


if __name__ == "__main__":
    main()
