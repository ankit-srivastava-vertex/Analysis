"""
sl_target_tracker.py — stop-loss & target monitor for owned positions
======================================================================

SUMMARY
-------
Reads a user-maintained meta CSV (portfolio/holdings_meta.csv) with
per-symbol StopLoss / Target / Thesis levels, joins with current
prices from broker file (LastClose), and flags any position that has
hit its stop or target — or is within a small distance of either.

WORKFLOW
--------
1. Load holdings (gives Symbol, LastClose, AvgCost).
2. Load portfolio/holdings_meta.csv. If missing, write a starter
   template populated with current symbols (StopLoss/Target blank).
3. For each row compute Distance_to_SL%, Distance_to_Target%, Status.
4. Status: STOP_HIT | NEAR_STOP (<3%) | TARGET_HIT |
           NEAR_TARGET (<3%) | OK | NO_LEVELS

USAGE
-----
    python3 -m portfolio.sl_target_tracker
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio.holdings_loader import load_holdings

META_PATH = PORTFOLIO_DIR / "holdings_meta.csv"
META_COLS = ["Symbol", "StopLoss", "Target", "Thesis", "ReviewDate"]
NEAR_PCT = 3.0  # within 3% considered "near"


def _ensure_template(symbols: pd.Series) -> pd.DataFrame:
    """Create starter holdings_meta.csv if absent."""
    if META_PATH.exists():
        df = pd.read_csv(META_PATH)
        # Add new symbols if any
        existing = set(df["Symbol"].astype(str).str.upper())
        missing = [s for s in symbols if s and s.upper() not in existing]
        if missing:
            add = pd.DataFrame({"Symbol": missing,
                                "StopLoss": [None] * len(missing),
                                "Target": [None] * len(missing),
                                "Thesis": [""] * len(missing),
                                "ReviewDate": [""] * len(missing)})
            df = pd.concat([df, add], ignore_index=True)
            df.to_csv(META_PATH, index=False)
            print(f"  [sl] Added {len(missing)} new symbols to {META_PATH.name}")
        return df
    df = pd.DataFrame({"Symbol": symbols.unique(),
                       "StopLoss": [None] * symbols.nunique(),
                       "Target":   [None] * symbols.nunique(),
                       "Thesis":   [""]   * symbols.nunique(),
                       "ReviewDate": [""] * symbols.nunique()})
    df.to_csv(META_PATH, index=False)
    print(f"  [sl] Created template {META_PATH.name} — fill in StopLoss/Target")
    return df


def _status(row) -> str:
    px = row.get("LastClose")
    sl = row.get("StopLoss")
    tg = row.get("Target")
    if pd.isna(px):
        return "NO_PRICE"
    if pd.isna(sl) and pd.isna(tg):
        return "NO_LEVELS"
    if pd.notna(sl) and px <= sl:
        return "STOP_HIT"
    if pd.notna(tg) and px >= tg:
        return "TARGET_HIT"
    if pd.notna(sl):
        if (px - sl) / px * 100 <= NEAR_PCT:
            return "NEAR_STOP"
    if pd.notna(tg):
        if (tg - px) / px * 100 <= NEAR_PCT:
            return "NEAR_TARGET"
    return "OK"


def _notes_df() -> pd.DataFrame:
    rows = [
        ("Meta file",   str(META_PATH.name)),
        ("Format",      "Symbol,StopLoss,Target,Thesis,ReviewDate"),
        ("StopLoss",    "Absolute price (₹). Leave blank if no SL set."),
        ("Target",      "Absolute price (₹). Leave blank if no target set."),
        ("Auto-create", "First run creates a template populated with current "
                        "holdings; subsequent runs add new symbols."),
        ("NEAR window", f"Within {NEAR_PCT}% of SL/Target triggers NEAR_* alert."),
        ("Discipline",  "STOP_HIT and TARGET_HIT must be acted on the same day "
                        "to be useful — review at market open."),
    ]
    return pd.DataFrame(rows, columns=["Field", "Value"])


def run(verbose: bool = True) -> dict:
    if verbose:
        print("  [sl] Loading holdings …")
    h = load_holdings(verbose=False)
    if h.empty:
        return {"sheets": {"SL/Target": pd.DataFrame(
            [{"Note": "no holdings"}])}}
    h = h[h["PresentValue"].fillna(0) > 0].copy()

    meta = _ensure_template(h["Symbol"])
    meta["Symbol"] = meta["Symbol"].astype(str).str.upper().str.strip()
    h["Symbol"] = h["Symbol"].astype(str).str.upper().str.strip()

    merged = h.merge(meta, on="Symbol", how="left")
    merged["Status"] = merged.apply(_status, axis=1)

    def _dist(row, col):
        if pd.isna(row.get(col)) or pd.isna(row.get("LastClose")):
            return None
        return round((row["LastClose"] - row[col]) / row["LastClose"] * 100, 2)

    merged["DistToSL%"]     = merged.apply(lambda r: _dist(r, "StopLoss"), axis=1)
    merged["DistToTarget%"] = merged.apply(
        lambda r: round((r["Target"] - r["LastClose"]) / r["LastClose"] * 100, 2)
        if pd.notna(r.get("Target")) and pd.notna(r.get("LastClose")) else None,
        axis=1,
    )

    cols = ["Symbol", "Company", "Sector", "Quantity", "AvgCost",
            "LastClose", "StopLoss", "Target",
            "DistToSL%", "DistToTarget%", "PresentValue",
            "Status", "Thesis", "ReviewDate"]
    out = merged[[c for c in cols if c in merged.columns]].copy()

    status_order = {"STOP_HIT": 0, "TARGET_HIT": 1, "NEAR_STOP": 2,
                    "NEAR_TARGET": 3, "OK": 4, "NO_LEVELS": 5, "NO_PRICE": 6}
    out["_o"] = out["Status"].map(status_order).fillna(7)
    out = out.sort_values(["_o", "PresentValue"],
                          ascending=[True, False]).drop(columns=["_o"])

    actionable = out[out["Status"].isin(
        ["STOP_HIT", "TARGET_HIT", "NEAR_STOP", "NEAR_TARGET"])].copy()

    summary = pd.DataFrame([
        {"Metric": "Holdings",         "Value": len(out)},
        {"Metric": "STOP_HIT",         "Value": int((out["Status"] == "STOP_HIT").sum())},
        {"Metric": "TARGET_HIT",       "Value": int((out["Status"] == "TARGET_HIT").sum())},
        {"Metric": "NEAR_STOP (<3%)",  "Value": int((out["Status"] == "NEAR_STOP").sum())},
        {"Metric": "NEAR_TARGET (<3%)","Value": int((out["Status"] == "NEAR_TARGET").sum())},
        {"Metric": "OK",               "Value": int((out["Status"] == "OK").sum())},
        {"Metric": "NO_LEVELS",        "Value": int((out["Status"] == "NO_LEVELS").sum())},
    ])

    return {"sheets": {
        "SL-Target Status":   out,
        "SL-Target Action":   actionable if not actionable.empty else
            pd.DataFrame([{"Note": "no actionable triggers"}]),
        "SL-Target Summary":  summary,
        "SL-Target Notes":    _notes_df(),
    }}


def main():
    ap = argparse.ArgumentParser(description="Stop-loss & target monitor")
    ap.add_argument("--out", default=str(PORTFOLIO_DIR / "sl_target.xlsx"))
    args = ap.parse_args()
    result = run()
    with pd.ExcelWriter(args.out, engine="openpyxl") as w:
        for name, df in result["sheets"].items():
            df.to_excel(w, sheet_name=name[:31], index=False)
    print(f"\n  ✓ Wrote {args.out}")


if __name__ == "__main__":
    main()
