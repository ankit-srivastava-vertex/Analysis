"""
pledge_promoter.py — pledge & promoter holding red-flag scanner
================================================================

SUMMARY
-------
Pulls pledge % and promoter shareholding for every owned name from
Tickertape's public screener API (same source the existing
fii_stake_tracker.py uses), then flags:

  RED   pledged > 25% OR promoter holding < 30%
  AMBER pledged > 10% OR promoter holding < 40%
  OK    pledged <= 10% AND promoter holding >= 40%

Also surfaces 3-month / 6-month change in FII holding for context.

WORKFLOW
--------
1. Load holdings.
2. Bulk-fetch Tickertape screener (all listed equities with mcap > 0).
3. Inner-join on ticker.
4. Score and sort by RED → AMBER → OK, then by Position Value desc.

USAGE
-----
    python3 -m portfolio.pledge_promoter
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
import requests

PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio.holdings_loader import load_holdings

API_URL = "https://api.tickertape.in/screener/query"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/json",
    "Content-Type": "application/json",
}
PROJECT_FIELDS = [
    "sid", "name", "ticker",
    "mrktCapf",
    "promShrPled",      # pledged %
    "promHld",          # promoter holding %
    "forInstHldng",     # FII %
    "forInstHldng3M",   # FII 3M change
    "forInstHldng6M",   # FII 6M change
    "domInstHldng",     # DII %
]
PAGE_SIZE = 200


def _fetch_all_screener(verbose: bool = True) -> pd.DataFrame:
    s = requests.Session()
    s.headers.update(HEADERS)
    match = {"mrktCapf": {"g": 0}}
    offset, all_rows, total = 0, [], None
    while True:
        payload = {"match": match, "sortBy": "mrktCapf", "sortOrder": -1,
                   "project": PROJECT_FIELDS, "offset": offset, "count": PAGE_SIZE}
        r = s.post(API_URL, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError("Tickertape API success=false")
        page = data["data"]
        results = page.get("results", [])
        if total is None:
            total = page.get("stats", {}).get("count", 0)
            if verbose:
                print(f"  [pledge] Tickertape universe: {total} stocks")
        all_rows.extend(results)
        if len(results) < PAGE_SIZE or len(all_rows) >= total:
            break
        offset += PAGE_SIZE
        time.sleep(0.3)

    flat = []
    for it in all_rows:
        stock = it.get("stock", {}) or {}
        info  = stock.get("info", {}) or {}
        adv   = stock.get("advancedRatios", {}) or {}
        flat.append({
            "Ticker": info.get("ticker"),
            "Name":   info.get("name"),
            "MarketCap_Cr":   adv.get("mrktCapf"),
            "Pledged%":       adv.get("promShrPled"),
            "Promoter%":      adv.get("promHld"),
            "FII%":           adv.get("forInstHldng"),
            "FII_3M_pp":      adv.get("forInstHldng3M"),
            "FII_6M_pp":      adv.get("forInstHldng6M"),
            "DII%":           adv.get("domInstHldng"),
        })
    return pd.DataFrame(flat)


def _flag(pledged, promoter):
    p  = pledged if pd.notna(pledged) else 0.0
    pr = promoter if pd.notna(promoter) else 100.0
    if p > 25 or pr < 30:
        return "RED"
    if p > 10 or pr < 40:
        return "AMBER"
    return "OK"


def _notes_df() -> pd.DataFrame:
    rows = [
        ("Source",       "Tickertape screener API (advancedRatios fields)"),
        ("Pledged%",     "Promoter shares pledged as % of total promoter holding"),
        ("Promoter%",    "Promoter & promoter-group holding"),
        ("RED rule",     "Pledged > 25%  OR  Promoter < 30%"),
        ("AMBER rule",   "Pledged > 10%  OR  Promoter < 40%"),
        ("Why pledge",   "High pledge => promoter under cash strain; price drop "
                         "can trigger margin calls and stock cascade-sell."),
        ("Why promoter", "Low/falling promoter holding signals reduced skin "
                         "in the game; large drops worth investigating."),
        ("FII delta",    "Negative 3M/6M change = institutional distribution; "
                         "look at WHY (governance? results? sector?)."),
    ]
    return pd.DataFrame(rows, columns=["Field", "Value"])


def run(verbose: bool = True) -> dict:
    if verbose:
        print("  [pledge] Loading holdings …")
    h = load_holdings(verbose=False)
    if h.empty:
        return {"sheets": {"Pledge & Promoter": pd.DataFrame(
            [{"Note": "no holdings"}])}}
    h = h[h["PresentValue"].fillna(0) > 0].copy()

    try:
        if verbose:
            print("  [pledge] Fetching Tickertape screener …")
        scr = _fetch_all_screener(verbose=verbose)
    except Exception as e:
        print(f"  [pledge] API failed: {e}")
        return {"sheets": {"Pledge & Promoter": pd.DataFrame(
            [{"Note": f"API failed: {e}"}])}}

    h["TickerKey"] = h["Symbol"].str.upper().str.strip()
    scr["TickerKey"] = scr["Ticker"].astype(str).str.upper().str.strip()

    merged = h.merge(scr, on="TickerKey", how="left")
    merged["Flag"] = merged.apply(
        lambda r: _flag(r["Pledged%"], r["Promoter%"]), axis=1)

    cols = ["Symbol", "Company", "Sector", "PresentValue", "Flag",
            "Pledged%", "Promoter%", "FII%", "FII_3M_pp", "FII_6M_pp",
            "DII%", "MarketCap_Cr"]
    out = merged[[c for c in cols if c in merged.columns]].copy()
    flag_order = {"RED": 0, "AMBER": 1, "OK": 2}
    out["_flag"] = out["Flag"].map(flag_order).fillna(3)
    out = out.sort_values(["_flag", "PresentValue"],
                          ascending=[True, False]).drop(columns=["_flag"])

    # Action list = RED + AMBER only
    action = out[out["Flag"].isin(["RED", "AMBER"])].copy()

    summary = pd.DataFrame([
        {"Metric": "Holdings scanned", "Value": len(out)},
        {"Metric": "RED",   "Value": int((out["Flag"] == "RED").sum())},
        {"Metric": "AMBER", "Value": int((out["Flag"] == "AMBER").sum())},
        {"Metric": "OK",    "Value": int((out["Flag"] == "OK").sum())},
        {"Metric": "Avg Pledged%",
         "Value": round(out["Pledged%"].mean(), 2) if "Pledged%" in out else 0},
        {"Metric": "Avg Promoter%",
         "Value": round(out["Promoter%"].mean(), 2) if "Promoter%" in out else 0},
    ])

    return {"sheets": {
        "Pledge & Promoter":   out,
        "Pledge Action List":  action if not action.empty else
            pd.DataFrame([{"Note": "no RED/AMBER flags"}]),
        "Pledge Summary":      summary,
        "Pledge Notes":        _notes_df(),
    }}


def main():
    ap = argparse.ArgumentParser(description="Pledge & promoter scanner")
    ap.add_argument("--out", default=str(PORTFOLIO_DIR / "pledge_promoter.xlsx"))
    args = ap.parse_args()
    result = run()
    with pd.ExcelWriter(args.out, engine="openpyxl") as w:
        for name, df in result["sheets"].items():
            df.to_excel(w, sheet_name=name[:31], index=False)
    print(f"\n  ✓ Wrote {args.out}")


if __name__ == "__main__":
    main()
