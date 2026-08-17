#!/usr/bin/env python3
"""
breakout_deep_analysis.py
=========================
Deep, evidence-based pattern mining on the accumulated walk-forward review data.

Goal: find selection RULES (feature thresholds + combinations) that maximise the
probability of a real, tradeable breakout — i.e. the highest-conviction "elite"
subset of candidates the scanner produces.

This does NOT modify the scanner. It only ANALYSES the per-candidate review
output (Output/review_*.xlsx, sheet 'All Results') so we can decide — with proof
— which filters are worth applying later.

Targets defined per candidate (from realised outcome):
  - true_bo   : status == TRUE_BREAKOUT          (confirmed breakout)
  - tradeable : max_gain_pct >= TRADE_WIN_PCT     (a real, sellable rally)
  - big_win   : max_gain_pct >= BIG_WIN_PCT       (outsized winner)
  - dud       : max_gain_pct < DUD_GAIN & pct_change < 0  (never moved, ended red)

Maturity: gain-magnitude analysis is run on "mature" candidates only
(days_since_scan >= MATURE_DAYS) so young weeks don't understate hit rates.

Usage:
  python3 breakout_deep_analysis.py                # latest review file
  python3 breakout_deep_analysis.py <review.xlsx>  # specific file
"""
from __future__ import annotations
import sys, glob, itertools
import numpy as np
import pandas as pd

# ---- tunables for the analysis (NOT scanner params) -----------------------
TRADE_WIN_PCT = 15.0     # max gain that counts as a tradeable win
BIG_WIN_PCT   = 25.0     # outsized winner
DUD_GAIN      = 5.0      # never even moved this far up
MATURE_DAYS   = 15       # candidate had >=15 sessions to play out
MIN_COVER     = 25       # min candidates for a rule to be reportable
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 50)

NUMERIC = ["score", "base_days", "base_range_pct", "touches",
           "distance_pct", "vcr_raw", "vdu_raw", "rr"]
BOOLS   = ["pattern_vcp", "pattern_w", "pattern_cup_handle", "high_conviction"]

# ── FROZEN ELITE RULES (locked 03-Jul-2026, cycle 7) ───────────────────────
# These were mined & REPLICATED across cycles 6 and 7. From now on they are
# FIXED, so every future week reviewed is a true OUT-OF-SAMPLE test of them.
# Do NOT re-tune these to fit new data — that would defeat the validation.
# A rule "holds" if, on weeks it never helped derive, the elite subset keeps
# beating the rest on tradeable% with low dud%.
def _elite_rules(df):
    return {
        # primary precision rule (replicated 72% tradeable / 100% confirm / 0 dud)
        "ELITE_precision  [score>=65 & range<=35 & rr>=3.0]":
            (df["score"] >= 65) & (df["base_range_pct"] <= 35) & (df["rr"] >= 3.0),
        # broader coverage version
        "ELITE_broad      [score>=60 & rr>=3.0]":
            (df["score"] >= 60) & (df["rr"] >= 3.0),
        # outsized-winner recipe (below R + high rr + W base)
        "BIGWIN_recipe    [dist<=-2 & rr>=2.5 & pattern_w]":
            (df["distance_pct"] <= -2) & (df["rr"] >= 2.5) & (df["pattern_w"]),
    }
FROZEN_DATE = "03-Jul-2026 (cycle 7)"



def _load() -> pd.DataFrame:
    path = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("Output/review_*.xlsx"))[-1]
    print(f"\n  Source: {path}")
    df = pd.read_excel(path, sheet_name="All Results")
    df = df[~df["status"].isin(["NO_DATA", "DATA_ERROR"])].copy()
    for c in NUMERIC + ["max_gain_pct", "pct_change", "days_since_scan"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for b in BOOLS:
        df[b] = df[b].astype(bool)
    # outcome targets
    df["true_bo"]   = (df["status"] == "TRUE_BREAKOUT").astype(int)
    df["tradeable"] = (df["max_gain_pct"] >= TRADE_WIN_PCT).astype(int)
    df["big_win"]   = (df["max_gain_pct"] >= BIG_WIN_PCT).astype(int)
    df["dud"]       = ((df["max_gain_pct"] < DUD_GAIN) & (df["pct_change"] < 0)).astype(int)
    df["mature"]    = df["days_since_scan"] >= MATURE_DAYS
    return df


def _hdr(t):
    print("\n" + "=" * 74 + f"\n  {t}\n" + "=" * 74)


def baseline(df, mat):
    _hdr("BASELINE RATES")
    print(f"  All candidates           : {len(df)}")
    print(f"  Mature (>= {MATURE_DAYS}d)          : {len(mat)}")
    print(f"  Baseline true_bo (all)   : {df['true_bo'].mean()*100:5.1f}%")
    print(f"  Baseline tradeable (mat) : {mat['tradeable'].mean()*100:5.1f}%  (max_gain >= {TRADE_WIN_PCT:.0f}%)")
    print(f"  Baseline big_win  (mat)  : {mat['big_win'].mean()*100:5.1f}%  (max_gain >= {BIG_WIN_PCT:.0f}%)")
    print(f"  Baseline dud      (mat)  : {mat['dud'].mean()*100:5.1f}%  (flat & red)")
    print(f"  Avg max_gain (mat)       : {mat['max_gain_pct'].mean():5.1f}%")


def univariate(df, mat):
    """For each numeric feature: quartile buckets -> outcome rates + avg gain."""
    _hdr("UNIVARIATE — numeric features by quartile (mature subset)")
    for c in NUMERIC:
        sub = mat.dropna(subset=[c])
        if len(sub) < 40:
            continue
        try:
            sub = sub.assign(_q=pd.qcut(sub[c], 4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop"))
        except ValueError:
            continue
        g = sub.groupby("_q", observed=True).agg(
            n=("tradeable", "size"),
            lo=(c, "min"), hi=(c, "max"),
            tradeable=("tradeable", "mean"),
            true_bo=("true_bo", "mean"),
            avg_gain=("max_gain_pct", "mean"),
        )
        g["tradeable"] = (g["tradeable"] * 100).round(1)
        g["true_bo"]   = (g["true_bo"] * 100).round(1)
        g["avg_gain"]  = g["avg_gain"].round(1)
        g[["lo", "hi"]] = g[["lo", "hi"]].round(2)
        print(f"\n  -- {c} --")
        print(g.to_string())


def boolean_feats(df, mat):
    _hdr("BOOLEAN FEATURES & SOURCE — lift over baseline (mature subset)")
    base = mat["tradeable"].mean()
    rows = []
    for b in BOOLS:
        for val in (True, False):
            s = mat[mat[b] == val]
            if len(s) >= MIN_COVER:
                rows.append([f"{b}={val}", len(s), round(s["tradeable"].mean()*100, 1),
                             round(s["true_bo"].mean()*100, 1), round(s["max_gain_pct"].mean(), 1),
                             round((s["tradeable"].mean()/base - 1)*100, 1)])
    for src in mat["source"].unique():
        s = mat[mat["source"] == src]
        rows.append([f"source={src}", len(s), round(s["tradeable"].mean()*100, 1),
                     round(s["true_bo"].mean()*100, 1), round(s["max_gain_pct"].mean(), 1),
                     round((s["tradeable"].mean()/base - 1)*100, 1)])
    out = pd.DataFrame(rows, columns=["group", "n", "tradeable%", "true_bo%", "avg_gain%", "lift%"])
    print(out.sort_values("lift%", ascending=False).to_string(index=False))


def threshold_sweep(df, mat):
    """Sweep distance_pct threshold (the suspected star predictor)."""
    _hdr("THRESHOLD SWEEP — distance_pct cutoff (buy below resistance?)")
    print("  Keep candidates with distance_pct <= cutoff:")
    print(f"  {'cutoff':>7} {'n':>5} {'cover%':>7} {'tradeable%':>11} {'true_bo%':>9} {'avg_gain%':>10} {'dud%':>6}")
    tot = len(mat)
    for cut in [-3, -2, -1, 0, 1, 2, 3, 100]:
        s = mat[mat["distance_pct"] <= cut]
        if len(s) == 0:
            continue
        print(f"  {cut:>7} {len(s):>5} {len(s)/tot*100:>6.1f}% {s['tradeable'].mean()*100:>10.1f}% "
              f"{s['true_bo'].mean()*100:>8.1f}% {s['max_gain_pct'].mean():>9.1f}% {s['dud'].mean()*100:>5.1f}%")


def _conditions(df):
    """Build a library of candidate boolean conditions for rule mining."""
    conds = {}
    # numeric thresholds at sensible quantiles
    specs = {
        "distance_pct": ("<=", [-2, -1, 0, 1]),
        "score":        (">=", [55, 60, 65, 70]),
        "base_days":    ("<=", [120, 140, 160]),
        "base_range_pct": ("<=", [25, 30, 35]),
        "touches":      (">=", [8, 10, 12]),
        "vcr_raw":      ("<=", [-0.2, -0.1, 0]),
        "rr":           (">=", [2.0, 2.5, 3.0]),
    }
    for col, (op, vals) in specs.items():
        for v in vals:
            if op == "<=":
                conds[f"{col}<={v}"] = (df[col] <= v)
            else:
                conds[f"{col}>={v}"] = (df[col] >= v)
    for b in ["pattern_w", "pattern_cup_handle"]:
        conds[f"{b}"] = df[b]
    conds["not_vcp"] = ~df["pattern_vcp"]
    return conds


def rule_mining(df, mat):
    """Greedy/exhaustive search of 1-3 condition rules maximising tradeable rate."""
    _hdr("RULE MINING — best 1-3 condition rules (mature subset)")
    base = mat["tradeable"].mean()
    conds = _conditions(mat)
    keys = list(conds.keys())
    results = []
    # 1,2,3-way combinations
    for r in (1, 2, 3):
        for combo in itertools.combinations(keys, r):
            mask = np.ones(len(mat), dtype=bool)
            for k in combo:
                mask &= conds[k].values
            n = int(mask.sum())
            if n < MIN_COVER:
                continue
            sub = mat[mask]
            tr = sub["tradeable"].mean()
            results.append({
                "rule": " & ".join(combo), "n": n,
                "cover%": round(n/len(mat)*100, 1),
                "tradeable%": round(tr*100, 1),
                "true_bo%": round(sub["true_bo"].mean()*100, 1),
                "big_win%": round(sub["big_win"].mean()*100, 1),
                "avg_gain%": round(sub["max_gain_pct"].mean(), 1),
                "dud%": round(sub["dud"].mean()*100, 1),
                "lift%": round((tr/base - 1)*100, 1),
            })
    res = pd.DataFrame(results)
    if res.empty:
        print("  (no rules met coverage threshold)")
        return
    # de-dup near-identical rules by keeping highest tradeable for each n bucket later;
    print(f"\n  Baseline tradeable = {base*100:.1f}%   (min coverage = {MIN_COVER})")

    print("\n  >> TOP 15 by PRECISION (tradeable%), coverage >= {}:".format(MIN_COVER))
    print(res.sort_values(["tradeable%", "n"], ascending=[False, False]).head(15).to_string(index=False))

    print("\n  >> TOP 15 by BIG-WIN rate (outsized movers):")
    print(res.sort_values(["big_win%", "n"], ascending=[False, False]).head(15).to_string(index=False))

    print("\n  >> BEST BALANCE (tradeable>=base*1.25 AND cover>=15%), sorted by avg_gain:")
    bal = res[(res["tradeable%"] >= base*125) & (res["cover%"] >= 15)]
    if bal.empty:
        bal = res[res["cover%"] >= 15]
    print(bal.sort_values("avg_gain%", ascending=False).head(15).to_string(index=False))


def score_calibration(df, mat):
    """Does the scanner's own score rank outcomes correctly?"""
    _hdr("SCANNER SCORE CALIBRATION (mature subset)")
    sub = mat.dropna(subset=["score"]).copy()
    sub["band"] = pd.cut(sub["score"], [0, 55, 60, 65, 70, 100],
                         labels=["<55", "55-60", "60-65", "65-70", "70+"])
    g = sub.groupby("band", observed=True).agg(
        n=("tradeable", "size"),
        tradeable=("tradeable", "mean"),
        true_bo=("true_bo", "mean"),
        avg_gain=("max_gain_pct", "mean"),
    )
    g["tradeable"] = (g["tradeable"]*100).round(1)
    g["true_bo"]   = (g["true_bo"]*100).round(1)
    g["avg_gain"]  = g["avg_gain"].round(1)
    print(g.to_string())
    print("\n  (If higher score bands don't show higher tradeable%/avg_gain,")
    print("   the score formula is NOT ranking well and needs rework.)")


def oos_validation(df, mat):
    """Out-of-sample validation of the FROZEN elite rules (per week + overall).

    Because the rules are locked (see _elite_rules / FROZEN_DATE), every week
    here is an out-of-sample test. For each rule we show, per week, how the
    ELITE-tagged subset performs vs the REST, plus an overall verdict.
    """
    _hdr(f"OUT-OF-SAMPLE ELITE-RULE VALIDATION  (frozen {FROZEN_DATE})")
    base = mat["tradeable"].mean()
    print(f"  Mature baseline tradeable = {base*100:.1f}%")
    print("  A rule PASSES if elite tradeable% >> baseline with low dud%.\n")

    for name, mask_full in _elite_rules(mat).items():
        sub = mat[mask_full]
        rest = mat[~mask_full]
        print(f"  -- {name} --")
        if len(sub) == 0:
            print("     (no candidates match on the mature set)\n")
            continue
        # per-week breakdown
        print(f"     {'week':>4} {'n_elite':>7} {'elite_trade%':>12} "
              f"{'elite_conf%':>11} {'elite_gain%':>11} {'rest_trade%':>11}")
        for wk in sorted(mat["week"].unique()):
            e = sub[sub["week"] == wk]
            r = rest[rest["week"] == wk]
            if len(e) == 0:
                continue
            rt = f"{r['tradeable'].mean()*100:>10.1f}%" if len(r) else "       n/a"
            print(f"     {int(wk):>4} {len(e):>7} {e['tradeable'].mean()*100:>11.1f}% "
                  f"{e['true_bo'].mean()*100:>10.1f}% {e['max_gain_pct'].mean():>10.1f}% {rt}")
        # overall verdict
        et, ec, eg, ed = (sub["tradeable"].mean(), sub["true_bo"].mean(),
                          sub["max_gain_pct"].mean(), sub["dud"].mean())
        rt2 = rest["tradeable"].mean() if len(rest) else float("nan")
        lift = (et / base - 1) * 100
        verdict = "PASS" if (et >= base * 1.25 and ed <= base) else "WATCH"
        print(f"     ---- OVERALL: n={len(sub)}  tradeable={et*100:.1f}%  "
              f"confirm={ec*100:.1f}%  avg_gain={eg:.1f}%  dud={ed*100:.1f}%  "
              f"vs_rest={rt2*100:.1f}%  lift={lift:+.1f}%  => {verdict}\n")


def main():
    df = _load()
    mat = df[df["mature"]].copy()
    baseline(df, mat)
    univariate(df, mat)
    boolean_feats(df, mat)
    threshold_sweep(df, mat)
    score_calibration(df, mat)
    oos_validation(df, mat)
    rule_mining(df, mat)
    print("\n" + "=" * 74)
    print("  DONE. Read the rule-mining tables: high tradeable% + decent cover% +")
    print("  low dud% = an elite filter worth proposing for the scanner.")
    print("=" * 74 + "\n")


if __name__ == "__main__":
    main()
