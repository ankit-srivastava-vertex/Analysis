#!/usr/bin/env python3
"""
universe_mining.py
==================
Mine the RAW filtered universe (MPD Data / Screener Data) for an "elite" subset
whose realised tradeable-rate beats both the whole universe AND the breakout
scanner's default selection — i.e. can we pull a >=50% tradeable pool straight
from the raw universe, bypassing the breakout-timing penalty?

Companion to universe_review.py (which proved the default breakout flag
UNDERPERFORMS the raw universe on magnitude, lift ~0.83-0.87x). This asks the
follow-up: what CHEAP, scanner-independent features of a universe stock predict
a real rally?

Features per universe stock (all measured AT / BEFORE scan_date — no lookahead):
  MPD-native (from the sheet, MPD only):
    pct_from_high  1Y% mcap_cr hl_count
  OHLCV-derived (both pipelines, from data up to scan_date):
    dist_200dma  dist_50dma  above_200dma  pct_from_52wh
    mom_21  mom_63  vol_surge  atr_pct
Outcome (post scan_date, same yardstick as universe_review):
    tradeable = max_gain_pct >= 15%   big_win >= 25%   dud = <5% & red

The miner runs univariate threshold sweeps then AND-combinations to surface the
highest tradeable% subset with adequate coverage. It does NOT touch the scanner.

Usage:
  python3 universe_mining.py                 # all matured weeks
  python3 universe_mining.py --weeks 1 2 3
  python3 universe_mining.py --min-days 15 --min-cover 40
"""
from __future__ import annotations
import argparse, itertools
import numpy as np
import pandas as pd

from breakout_review import (
    _discover_weeks, _load_week, _fetch_ohlcv_bulk, TODAY,
    SPLIT_ARTIFACT_RATIO,
)
from universe_review import _scan_close_from_ohlcv, _outcome, UNIVERSES

TRADE_WIN_PCT = 15.0
BIG_WIN_PCT   = 25.0
DUD_GAIN      = 5.0
pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 60)

# scan-time OHLCV features common to both pipelines
OHLCV_FEATS = ["dist_200dma", "dist_50dma", "pct_from_52wh",
               "mom_21", "mom_63", "vol_surge", "atr_pct"]
# extra MPD-native sheet features
MPD_FEATS   = ["pct_from_high", "y1_pct", "mcap_cr", "hl_count"]


# ── FROZEN UNIVERSE RULES (locked 03-Jul-2026) ─────────────────────────────
# Mined from weeks 1-6 (in-sample). Only the ROBUST, high-coverage momentum/
# volatility filters are frozen — NOT the tiny-n overfit 3-way combos.
# From now on every week NOT in DERIVED_WEEKS is a true out-of-sample test.
# Do NOT re-tune these to fit new data.
FROZEN_UNI_DATE = "03-Jul-2026 (weeks 1-6)"
DERIVED_WEEKS = {1, 2, 3, 4, 5, 6}


def _frozen_uni_rules(s):
    return {
        "UNI_mom50  [dist_50dma>=17]":            s["dist_50dma"] >= 17,
        "UNI_atr    [atr_pct>=5.5]":              s["atr_pct"] >= 5.5,
        "UNI_combo  [dist_50dma>=17 & atr>=5.5]": (s["dist_50dma"] >= 17)
                                                  & (s["atr_pct"] >= 5.5),
    }


def _hdr(t):
    print("\n" + "=" * 80 + f"\n  {t}\n" + "=" * 80)


# ── scan-time feature engineering (no lookahead) ───────────────────────────
def _scan_features(df, scan_date):
    """Compute technical features from OHLCV using only bars up to scan_date."""
    try:
        idx = df.index.date if hasattr(df.index, "date") else pd.to_datetime(df.index).date
        pre = df[idx <= scan_date]
    except Exception:
        return None
    if len(pre) < 30:
        return None
    close = pre["Close"].astype(float)
    high = pre["High"].astype(float)
    low = pre["Low"].astype(float)
    vol = pre["Volume"].astype(float) if "Volume" in pre.columns else None
    sc = float(close.iloc[-1])
    if sc <= 0:
        return None

    sma200 = float(close.tail(200).mean())
    sma50 = float(close.tail(50).mean())
    high_252 = float(high.tail(252).max())
    c21 = float(close.iloc[-22]) if len(close) >= 22 else np.nan
    c63 = float(close.iloc[-64]) if len(close) >= 64 else np.nan

    # ATR14 (Wilder-ish simple mean of true range)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low),
                    (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean())

    vsurge = np.nan
    if vol is not None and len(vol) >= 25:
        recent = float(vol.tail(5).mean())
        base = float(vol.iloc[-25:-5].mean())
        if base > 0:
            vsurge = recent / base

    return {
        "scan_close": round(sc, 2),
        "dist_200dma": round((sc / sma200 - 1) * 100, 2) if sma200 > 0 else np.nan,
        "dist_50dma": round((sc / sma50 - 1) * 100, 2) if sma50 > 0 else np.nan,
        "pct_from_52wh": round((sc / high_252 - 1) * 100, 2) if high_252 > 0 else np.nan,
        "mom_21": round((sc / c21 - 1) * 100, 2) if c21 and c21 > 0 else np.nan,
        "mom_63": round((sc / c63 - 1) * 100, 2) if c63 and c63 > 0 else np.nan,
        "vol_surge": round(vsurge, 2) if vsurge == vsurge else np.nan,
        "atr_pct": round(atr / sc * 100, 2) if atr == atr else np.nan,
    }


def _collect(weeks, min_days):
    """Build universe records + MPD-native features + breakout membership."""
    records, all_tickers, week_meta = [], set(), []
    for wk, folder in weeks:
        sheets, scan_date, _ = _load_week(wk, folder)
        if sheets is None:
            continue
        days = (TODAY - scan_date).days
        if days < min_days:
            print(f"  Week {wk}: only {days}d old (< {min_days}) — skipped")
            continue
        week_meta.append((wk, folder, scan_date, days))
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
                if refcol and refcol in udf.columns and pd.notna(row.get(refcol)):
                    try:
                        ref = float(row[refcol])
                    except (ValueError, TypeError):
                        ref = None

                def _num(col):
                    if col in udf.columns and pd.notna(row.get(col)):
                        v = pd.to_numeric(row[col], errors="coerce")
                        return float(v) if v == v else np.nan
                    return np.nan

                records.append({
                    "ticker": t, "week": wk, "source": src, "days": days,
                    "scan_date": scan_date, "is_breakout": t in bo_syms,
                    "ref_pre": ref,
                    "pct_from_high": _num("Pct From High"),
                    "y1_pct": _num("1Y %"),
                    "mcap_cr": _num("Mcap (Cr)"),
                    "hl_count": _num("HL Count"),
                })
    return records, sorted(all_tickers), week_meta


# ── rule evaluation ────────────────────────────────────────────────────────
def _eval(df, mask, base):
    sub = df[mask]
    n = len(sub)
    if n == 0:
        return None
    tr = sub["tradeable"].mean() * 100
    return {
        "n": n, "cover%": round(n / len(df) * 100, 1),
        "tradeable%": round(tr, 1),
        "big_win%": round(sub["big_win"].mean() * 100, 1),
        "dud%": round(sub["dud"].mean() * 100, 1),
        "avg_gain%": round(sub["max_gain_pct"].mean(), 1),
        "lift": round(tr / base, 2) if base else np.nan,
    }


def _sweep_feature(df, feat, base, min_cover):
    """Try >= and <= cutoffs at deciles; return the best-improving conditions."""
    vals = df[feat].dropna()
    if len(vals) < min_cover:
        return []
    qs = np.unique(np.round(vals.quantile(np.linspace(0.1, 0.9, 9)).values, 2))
    out = []
    for thr in qs:
        for op in (">=", "<="):
            mask = (df[feat] >= thr) if op == ">=" else (df[feat] <= thr)
            r = _eval(df, mask, base)
            if r and r["n"] >= min_cover and r["tradeable%"] > base:
                out.append({"cond": f"{feat} {op} {thr}", "feat": feat,
                            "op": op, "thr": thr, **r})
    return out


def _mine_source(df, src, min_cover):
    s = df[df["source"] == src].copy()
    if s.empty:
        print(f"  {src}: no data")
        return None
    feats = OHLCV_FEATS + (MPD_FEATS if src == "MPD" else [])
    base = s["tradeable"].mean() * 100
    _hdr(f"{src} UNIVERSE — mining a >=50% tradeable subset  (n={len(s)}, "
         f"baseline tradeable {base:.1f}%)")

    # 1) univariate sweeps
    singles = []
    for f in feats:
        singles += _sweep_feature(s, f, base, min_cover)
    if not singles:
        print("  No single feature beats baseline at the coverage floor.")
        return {"source": src, "baseline": round(base, 1), "best": None}
    sdf = pd.DataFrame(singles).sort_values("tradeable%", ascending=False)
    print("\n  Top single-feature conditions (by tradeable%):")
    print(sdf[["cond", "n", "cover%", "tradeable%", "big_win%", "dud%",
               "avg_gain%", "lift"]].head(10).to_string(index=False))

    # keep the strongest, non-redundant single conditions (best per feature/dir)
    best_conds = (sdf.sort_values("tradeable%", ascending=False)
                     .drop_duplicates(subset=["feat", "op"]).head(8)
                     .to_dict("records"))

    # 2) AND-combinations (2 and 3 way)
    combos = []
    for k in (2, 3):
        for combo in itertools.combinations(best_conds, k):
            feats_used = {c["feat"] for c in combo}
            if len(feats_used) < k:   # don't AND two cuts on the same feature
                continue
            mask = pd.Series(True, index=s.index)
            for c in combo:
                mask &= (s[c["feat"]] >= c["thr"]) if c["op"] == ">=" \
                        else (s[c["feat"]] <= c["thr"])
            r = _eval(s, mask, base)
            if r and r["n"] >= min_cover:
                combos.append({"rule": "  &  ".join(c["cond"] for c in combo),
                               "k": k, **r})
    best = None
    if combos:
        cdf = pd.DataFrame(combos).sort_values(
            ["tradeable%", "cover%"], ascending=[False, False])
        print("\n  Top AND-combinations (coverage-gated):")
        print(cdf[["rule", "n", "cover%", "tradeable%", "big_win%", "dud%",
                   "avg_gain%", "lift"]].head(10).to_string(index=False))
        best = cdf.iloc[0].to_dict()
    else:
        print("\n  No AND-combination cleared the coverage floor.")

    return {"source": src, "baseline": round(base, 1),
            "best_single": sdf.iloc[0].to_dict(),
            "best_combo": best}


def oos_validation(df):
    """Track the FROZEN universe rules per week — in-sample vs out-of-sample.
    Each week not in DERIVED_WEEKS is a genuine OOS test of the frozen rules.
    A rule 'holds' if its tradeable% stays >> the universe baseline with low dud%.
    """
    _hdr(f"FROZEN UNIVERSE RULES — OOS tracking  (frozen {FROZEN_UNI_DATE})")
    for src in ["MPD", "Screener"]:
        s = df[df["source"] == src]
        if s.empty:
            continue
        base = s["tradeable"].mean() * 100
        print(f"\n  {src}  (universe baseline tradeable {base:.1f}%)")
        rules = _frozen_uni_rules(s)
        for name, mask in rules.items():
            rows = []
            for wk in sorted(s["week"].unique()):
                w = s[s["week"] == wk]
                sub = w[mask.reindex(w.index).fillna(False)]
                if len(sub) == 0:
                    continue
                wbase = w["tradeable"].mean() * 100
                rows.append({
                    "week": wk, "OOS": "" if wk in DERIVED_WEEKS else "OOS",
                    "n": len(sub),
                    "rule_trade%": round(sub["tradeable"].mean() * 100, 1),
                    "uni_trade%": round(wbase, 1),
                    "dud%": round(sub["dud"].mean() * 100, 1),
                    "lift": round((sub["tradeable"].mean() * 100) / wbase, 2)
                            if wbase else np.nan,
                })
            if not rows:
                continue
            rdf = pd.DataFrame(rows)
            allm = s[mask.fillna(False)]
            overall_tr = allm["tradeable"].mean() * 100 if len(allm) else float("nan")
            overall_dud = allm["dud"].mean() * 100 if len(allm) else float("nan")
            oos = rdf[rdf["OOS"] == "OOS"]
            verdict = "in-sample only"
            if not oos.empty:
                held = (oos["rule_trade%"] >= oos["uni_trade%"] * 1.15).mean()
                verdict = ("PASS (OOS held)" if held >= 0.6
                           else "WATCH (OOS weak)")
            print(f"\n    {name}   overall {overall_tr:.1f}% tradeable / "
                  f"{overall_dud:.1f}% dud / n={len(allm)}   -> {verdict}")
            print(rdf.to_string(index=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", nargs="*", type=int)
    ap.add_argument("--min-days", type=int, default=15)
    ap.add_argument("--min-cover", type=int, default=30)
    args = ap.parse_args()

    weeks = _discover_weeks()
    if args.weeks:
        weeks = [(w, f) for (w, f) in weeks if w in args.weeks]

    _hdr(f"UNIVERSE RULE MINING — pull a >=50% tradeable subset ({TODAY:%d-%b-%Y})")
    print(f"  Weeks discovered: {[f'{w}({f})' for w, f in weeks]}")
    print(f"  Maturity gate   : >= {args.min_days}d   min coverage: {args.min_cover}")

    records, tickers, week_meta = _collect(weeks, args.min_days)
    if not records:
        print("  No matured universe records found.")
        return
    print(f"  Weeks used      : {[w for w, *_ in week_meta]}")
    print(f"  Universe records: {len(records)} ({len(tickers)} unique tickers)")

    ohlcv = _fetch_ohlcv_bulk(tickers)

    rows = []
    for r in records:
        sd = r["scan_date"]
        df_t = ohlcv.get(r["ticker"])
        if df_t is None:
            continue
        ref = r["ref_pre"]
        if ref is None:
            ref = _scan_close_from_ohlcv(df_t, sd)
        out = _outcome(r["ticker"], ref, ohlcv, sd)
        if out is None or out["status"] == "DATA_ERROR":
            continue
        feats = _scan_features(df_t, sd)
        if feats is None:
            continue
        rows.append({**r, **feats, **out})

    df = pd.DataFrame(rows)
    if df.empty:
        print("  No usable classified records.")
        return
    df.drop(columns=["scan_date"], inplace=True)
    print(f"  Classified w/ features: {len(df)}  "
          f"(MPD {len(df[df.source=='MPD'])}, Screener {len(df[df.source=='Screener'])})")

    results = {}
    for src in ["MPD", "Screener"]:
        results[src] = _mine_source(df, src, args.min_cover)

    # ── frozen-rule out-of-sample tracking ──
    oos_validation(df)

    # ── verdict: does a mined universe rule beat the breakout cohort? ──
    _hdr("VERDICT — mined universe elite vs default breakout flag")
    for src in ["MPD", "Screener"]:
        s = df[df["source"] == src]
        bo = s[s["is_breakout"]]
        bo_tr = bo["tradeable"].mean() * 100 if len(bo) else float("nan")
        uni_tr = s["tradeable"].mean() * 100
        res = results.get(src) or {}
        combo = res.get("best_combo")
        best_tr = combo["tradeable%"] if combo else float("nan")
        best_n = combo["n"] if combo else 0
        rule = combo["rule"] if combo else "(none cleared coverage)"
        print(f"\n  {src}:")
        print(f"    whole universe tradeable%      : {uni_tr:5.1f}  (n={len(s)})")
        print(f"    default breakout tradeable%    : {bo_tr:5.1f}  (n={len(bo)})")
        print(f"    MINED universe rule tradeable% : {best_tr:5.1f}  (n={best_n})")
        print(f"    rule: {rule}")
        if combo and best_tr >= 50 and best_tr > bo_tr:
            print(f"    >>> PASS: mined rule pulls a {best_tr:.0f}% subset, "
                  f"beats breakout ({bo_tr:.0f}%).")
        elif combo and best_tr > bo_tr:
            print(f"    >>> PARTIAL: beats breakout but < 50% tradeable.")
        else:
            print(f"    >>> WEAK: no mined rule beats the breakout flag.")

    out_path = f"Output/universe_mining_{TODAY:%Y%m%d}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Universe+Features", index=False)
    print(f"\n  Detail written: {out_path}")
    print("\n" + "=" * 80)
    print("  These rules are IN-SAMPLE. Freeze the winners and re-test on fresh")
    print("  weeks before trusting them. No scanner change until proven OOS.")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
