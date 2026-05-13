"""
Breakout Review — Walk-Forward Validation
==========================================

Reviews weekly breakout scanner snapshots to evaluate prediction accuracy.
Compares breakout candidates against actual post-scan price action to
measure hit rate and identify what separates true breakouts from false signals.

WORKFLOW
-------
  1. User saves breakout_watchlist.xlsx into Output/WeekN/ each week
  2. User says "let's review" → this script runs
  3. For each WeekN, reads the 4 key sheets:
       - MPD Data          (raw multi_pct_down universe)
       - Screener Data     (raw screener.in universe)
       - MPD Breakouts     (breakout candidates from MPD)
       - Screener Breakouts(breakout candidates from screener)
  4. Fetches post-scan OHLCV via Angel One
  5. Classifies each breakout candidate:
       TRUE_BREAKOUT    : closed above R for ≥2 sessions with vol confirmation
       BREAKOUT_LOW_VOL : closed above R for ≥2 sessions, no vol spike
       ATTEMPTED        : touched/crossed R at least once
       HOLDING          : positive since scan but hasn't reached R
       FALSE_SIGNAL     : never reached R, negative since scan
       NO_DATA          : could not fetch price data
  6. Optionally checks for missed breakouts in the full universe (--full)
  7. Deep analysis: compares characteristics of TRUE vs FALSE signals
  8. Outputs review Excel + updates cumulative tracking CSV

FOLDER STRUCTURE
----------------
  Output/Week1/breakout_watchlist.xlsx
  Output/Week2/breakout_watchlist.xlsx
  ...
  Output/review_YYYYMMDD_HHMMSS.xlsx   (review output)
  Output/review_cumulative.csv          (running accuracy stats)

USAGE
-----
  python3 breakout_review.py                  # review all available weeks
  python3 breakout_review.py --weeks 1 2      # review specific weeks
  python3 breakout_review.py --full           # also check for missed breakouts
"""

import os
import sys
import re
import argparse
import datetime
import warnings

import numpy as np
import pandas as pd
from dotenv import load_dotenv

warnings.filterwarnings("ignore")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "Output")
CUMULATIVE_CSV = os.path.join(OUTPUT_DIR, "review_cumulative.csv")

TODAY = datetime.date.today()
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

# ── Breakout confirmation thresholds ──
BO_DAYS_ABOVE_R = 2        # must close above R for ≥N sessions
BO_VOL_MULT = 1.3           # breakout-day volume ≥ 1.3× 50DMA avg
MISSED_RALLY_PCT = 10.0     # universe stock rallied >10% = potential miss
FETCH_LOOKBACK_DAYS = 90    # fetch enough history to cover ~8 weeks


# ─── Discovery & loading ────────────────────────────────────────────────────

def _discover_weeks():
    """Find all WeekN-DDMon folders under Output/ that contain the Excel file.

    Matches patterns like: Week1-11May, Week2-18May, week3-25may, etc.
    Also matches plain WeekN for backward compatibility.
    Returns list of (week_num, folder_name) tuples sorted by week_num.
    """
    weeks = []
    if not os.path.isdir(OUTPUT_DIR):
        return weeks
    for name in sorted(os.listdir(OUTPUT_DIR)):
        m = re.match(r"^[Ww]eek(\d+)(?:-\d{1,2}[A-Za-z]+)?$", name)
        if m:
            folder = os.path.join(OUTPUT_DIR, name)
            xlsx = os.path.join(folder, "breakout_watchlist.xlsx")
            if os.path.exists(xlsx):
                weeks.append((int(m.group(1)), name))
    return sorted(weeks, key=lambda x: x[0])


def _load_week(week_num, folder_name):
    """Load breakout_watchlist.xlsx from the week folder.

    Returns (sheets_dict, scan_date, xlsx_path) or (None, None, None).
    scan_date is inferred from the file's last-modification timestamp.
    """
    folder = os.path.join(OUTPUT_DIR, folder_name)
    xlsx = os.path.join(folder, "breakout_watchlist.xlsx")
    if not os.path.exists(xlsx):
        print(f"  WARNING: {xlsx} not found — skipping Week {week_num}")
        return None, None, None

    sheets = pd.read_excel(xlsx, sheet_name=None, engine="openpyxl")

    # Scan date = file modification time (rounded to date)
    mtime = os.path.getmtime(xlsx)
    scan_date = datetime.date.fromtimestamp(mtime)

    return sheets, scan_date, xlsx


# ─── Data fetching ──────────────────────────────────────────────────────────

def _fetch_ohlcv_bulk(tickers, lookback_days=FETCH_LOOKBACK_DAYS):
    """Fetch recent OHLCV for all tickers via Angel One."""
    from angel_client import angel_download_many

    end = TODAY + datetime.timedelta(days=1)
    start = TODAY - datetime.timedelta(days=int(lookback_days * 1.5))
    print(f"  Fetching OHLCV for {len(tickers)} tickers via Angel One ...")
    raw = angel_download_many(tickers, start, end)

    usable = {k: v for k, v in raw.items() if v is not None and not v.empty}
    print(f"  Got usable data for {len(usable)}/{len(tickers)} tickers")
    return usable


# ─── Classification ─────────────────────────────────────────────────────────

def _classify_candidate(row, ohlcv_data, scan_date):
    """Classify a single breakout candidate based on post-scan price action.

    Uses the resistance level and scan-day close from the Excel row,
    then checks actual OHLCV data after the scan date.
    """
    sym = str(row.get("symbol", ""))
    R = float(row.get("resistance", 0))
    scan_close = float(row.get("close", 0))

    # Carry forward key metrics for later deep analysis
    result = {
        "symbol": sym,
        "resistance": round(R, 2),
        "scan_close": round(scan_close, 2),
        "score": row.get("score", 0),
        "high_conviction": row.get("high_conviction", False),
        "hc_path": row.get("hc_path", ""),
        "base_days": row.get("base_days", 0),
        "base_range_pct": row.get("base_range_pct", 0),
        "touches": row.get("touches", 0),
        "distance_pct": row.get("distance_pct", 0),
        "pattern_multi_touch": row.get("pattern_multi_touch", False),
        "pattern_vcp": row.get("pattern_vcp", False),
        "pattern_w": row.get("pattern_w", False),
        "pattern_cup_handle": row.get("pattern_cup_handle", False),
        "vcr_raw": row.get("vcr_raw", 0),
        "vdu_raw": row.get("vdu_raw", 0),
        "rs_rising_50d": row.get("rs_rising_50d", False),
        "rr": row.get("rr", 0),
        "stop": row.get("stop", 0),
        "target": row.get("target", 0),
    }

    # No data available
    if sym not in ohlcv_data:
        result.update({
            "current_close": None, "pct_change": None,
            "status": "NO_DATA", "days_above_R": 0,
            "vol_confirmed": False, "max_high": None,
            "max_gain_pct": None, "days_since_scan": None,
        })
        return result

    df = ohlcv_data[sym]

    # Filter to post-scan data
    try:
        if hasattr(df.index, 'date'):
            post = df[df.index.date > scan_date]
        else:
            post = df[pd.to_datetime(df.index).date > scan_date]
    except Exception:
        post = df.tail(5)  # fallback

    if post.empty:
        post = df.tail(5)

    current_close = float(df["Close"].iloc[-1])
    pct_change = round(
        ((current_close - scan_close) / scan_close * 100), 2
    ) if scan_close > 0 else 0.0

    max_high = float(post["High"].max())
    max_gain = round(
        ((max_high - scan_close) / scan_close * 100), 2
    ) if scan_close > 0 else 0.0

    days_since = (TODAY - scan_date).days

    # Count sessions closing above R
    days_above = int((post["Close"] > R).sum()) if R > 0 else 0

    # Volume confirmation: any day closing above R had vol > 1.3× 50DMA
    v50_series = df["Volume"].rolling(min(50, len(df))).mean()
    v50 = float(v50_series.iloc[-1]) if not v50_series.empty else 0
    bo_days_df = post[post["Close"] > R] if R > 0 else pd.DataFrame()
    vol_confirmed = False
    if not bo_days_df.empty and v50 > 0:
        vol_confirmed = bool((bo_days_df["Volume"] > v50 * BO_VOL_MULT).any())

    # ── Classification logic ──
    if days_above >= BO_DAYS_ABOVE_R and vol_confirmed:
        status = "TRUE_BREAKOUT"
    elif days_above >= BO_DAYS_ABOVE_R:
        status = "BREAKOUT_LOW_VOL"
    elif days_above >= 1 or max_high > R:
        status = "ATTEMPTED"
    elif pct_change >= 0:
        status = "HOLDING"
    else:
        status = "FALSE_SIGNAL"

    result.update({
        "current_close": round(current_close, 2),
        "pct_change": pct_change,
        "status": status,
        "days_above_R": days_above,
        "vol_confirmed": vol_confirmed,
        "max_high": round(max_high, 2),
        "max_gain_pct": max_gain,
        "days_since_scan": days_since,
    })
    return result


def _check_missed_universe(universe_df, candidate_syms, ohlcv_data,
                           scan_date, ticker_col):
    """Check for stocks in universe but NOT in candidates that rallied big.

    These are potential missed breakouts — stocks the scanner's hard gates
    filtered out but that subsequently moved >10%.
    """
    if universe_df is None or universe_df.empty:
        return []
    if ticker_col not in universe_df.columns:
        return []

    uni_syms = set(
        universe_df[ticker_col].dropna().astype(str).str.strip()
    ) - {"", "nan"}
    non_candidates = uni_syms - candidate_syms

    missed = []
    for sym in sorted(non_candidates):
        if sym not in ohlcv_data:
            continue
        df = ohlcv_data[sym]

        try:
            if hasattr(df.index, 'date'):
                post = df[df.index.date > scan_date]
            else:
                post = df[pd.to_datetime(df.index).date > scan_date]
        except Exception:
            continue

        if post.empty or len(post) < 2:
            continue

        first_close = float(post["Close"].iloc[0])
        current_close = float(post["Close"].iloc[-1])
        max_high = float(post["High"].max())
        pct_change = round(
            ((current_close - first_close) / first_close * 100), 2
        ) if first_close > 0 else 0.0
        max_gain = round(
            ((max_high - first_close) / first_close * 100), 2
        ) if first_close > 0 else 0.0

        if max_gain >= MISSED_RALLY_PCT:
            missed.append({
                "symbol": sym,
                "scan_close": round(first_close, 2),
                "current_close": round(current_close, 2),
                "pct_change": pct_change,
                "max_high": round(max_high, 2),
                "max_gain_pct": max_gain,
                "status": "MISSED",
            })

    return missed


# ─── Deep Analysis ──────────────────────────────────────────────────────────

def _deep_analysis(all_results):
    """Compare numeric & pattern characteristics of TRUE vs FALSE signals.

    This is the core insight engine: which scanner metrics actually
    predict real breakouts? After several weeks of data, patterns emerge.
    """
    df = pd.DataFrame(all_results)
    if df.empty:
        return pd.DataFrame()

    true_bo = df[df["status"].isin(["TRUE_BREAKOUT", "BREAKOUT_LOW_VOL"])]
    false_sig = df[df["status"] == "FALSE_SIGNAL"]

    if true_bo.empty and false_sig.empty:
        return pd.DataFrame()

    rows = []

    # ── Numeric metrics ──
    numeric_metrics = [
        "score", "base_days", "base_range_pct", "touches", "distance_pct",
        "vcr_raw", "vdu_raw", "rr",
    ]
    for m in numeric_metrics:
        if m not in df.columns:
            continue
        t_vals = pd.to_numeric(true_bo[m], errors="coerce").dropna()
        f_vals = pd.to_numeric(false_sig[m], errors="coerce").dropna()
        rows.append({
            "metric": m,
            "true_bo_mean": round(float(t_vals.mean()), 2) if len(t_vals) else None,
            "true_bo_median": round(float(t_vals.median()), 2) if len(t_vals) else None,
            "false_sig_mean": round(float(f_vals.mean()), 2) if len(f_vals) else None,
            "false_sig_median": round(float(f_vals.median()), 2) if len(f_vals) else None,
            "true_bo_n": len(t_vals),
            "false_sig_n": len(f_vals),
            "predictive_edge": "",
        })

    # ── Pattern presence rates ──
    pattern_cols = [
        "pattern_multi_touch", "pattern_vcp", "pattern_w",
        "pattern_cup_handle", "high_conviction", "rs_rising_50d",
    ]
    for p in pattern_cols:
        if p not in df.columns:
            continue
        t_count = int(true_bo[p].sum()) if len(true_bo) else 0
        f_count = int(false_sig[p].sum()) if len(false_sig) else 0
        t_rate = round(t_count / len(true_bo) * 100, 1) if len(true_bo) else 0
        f_rate = round(f_count / len(false_sig) * 100, 1) if len(false_sig) else 0
        edge = ""
        if t_rate > f_rate + 10:
            edge = "PREDICTIVE ↑"
        elif f_rate > t_rate + 10:
            edge = "ANTI-PREDICTIVE ↓"
        rows.append({
            "metric": f"{p} (%)",
            "true_bo_mean": t_rate,
            "true_bo_median": None,
            "false_sig_mean": f_rate,
            "false_sig_median": None,
            "true_bo_n": t_count,
            "false_sig_n": f_count,
            "predictive_edge": edge,
        })

    return pd.DataFrame(rows)


# ─── Cumulative tracking ────────────────────────────────────────────────────

def _update_cumulative(all_results, review_date):
    """Append this review session's stats to the cumulative CSV."""
    df = pd.DataFrame(all_results)
    if df.empty:
        return

    rows = []
    for (week, source), grp in df.groupby(["week", "source"]):
        total = len(grp)
        no_data = int((grp["status"] == "NO_DATA").sum())
        valid = total - no_data
        true_bo = int(grp["status"].isin(["TRUE_BREAKOUT"]).sum())
        bo_low_vol = int(grp["status"].isin(["BREAKOUT_LOW_VOL"]).sum())
        attempted = int((grp["status"] == "ATTEMPTED").sum())
        holding = int((grp["status"] == "HOLDING").sum())
        false_sig = int((grp["status"] == "FALSE_SIGNAL").sum())

        hit_strict = round(true_bo / valid * 100, 1) if valid else 0
        hit_loose = round((true_bo + bo_low_vol) / valid * 100, 1) if valid else 0

        rows.append({
            "review_date": review_date,
            "week": int(week),
            "source": source,
            "total_candidates": total,
            "valid": valid,
            "true_breakout": true_bo,
            "breakout_low_vol": bo_low_vol,
            "attempted": attempted,
            "holding": holding,
            "false_signal": false_sig,
            "no_data": no_data,
            "hit_rate_strict_%": hit_strict,
            "hit_rate_loose_%": hit_loose,
        })

    new_df = pd.DataFrame(rows)

    if os.path.exists(CUMULATIVE_CSV):
        existing = pd.read_csv(CUMULATIVE_CSV)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df

    combined.to_csv(CUMULATIVE_CSV, index=False)
    print(f"  Cumulative stats updated: {CUMULATIVE_CSV}")


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Breakout Review — Walk-Forward Validation")
    p.add_argument("--weeks", type=int, nargs="*", default=None,
                   help="Week numbers to review (default: all available)")
    p.add_argument("--full", action="store_true",
                   help="Also check for missed breakouts from full universe "
                        "(slower — fetches all universe tickers)")
    args = p.parse_args()

    print("=" * 70)
    print(f"  BREAKOUT REVIEW — {TODAY.strftime('%d-%b-%Y')}")
    print("=" * 70)

    # ── Discover available weeks ──
    available = _discover_weeks()
    if not available:
        print("  No WeekN-DDMon folders found in Output/")
        print(f"  Expected: {OUTPUT_DIR}/Week1-11May/breakout_watchlist.xlsx")
        print(f"            {OUTPUT_DIR}/Week2-18May/breakout_watchlist.xlsx")
        print("            ...")
        return

    # Build lookup: week_num -> folder_name
    avail_nums = [w for w, _ in available]
    avail_map = {w: name for w, name in available}

    if args.weeks:
        review_nums = sorted([w for w in args.weeks if w in avail_map])
    else:
        review_nums = avail_nums

    if not review_nums:
        print(f"  Requested weeks not found. Available: {[f'{w} ({n})' for w, n in available]}")
        return

    print(f"  Weeks available : {[f'{w} ({n})' for w, n in available]}")
    print(f"  Reviewing       : {[f'{w} ({avail_map[w]})' for w in review_nums]}")
    if args.full:
        print(f"  Mode            : FULL (incl. missed breakout check)")
    else:
        print(f"  Mode            : CANDIDATES ONLY (use --full for missed check)")
    print()

    # ── Load all weeks ──
    week_data = {}
    all_tickers = set()

    for w in review_nums:
        sheets, scan_date, xlsx_path = _load_week(w, avail_map[w])
        if sheets is None:
            continue
        week_data[w] = {
            "sheets": sheets, "scan_date": scan_date, "path": xlsx_path
        }

        # Collect tickers from breakout candidate sheets
        for sheet_name in ["MPD Breakouts", "Screener Breakouts"]:
            if sheet_name in sheets:
                sdf = sheets[sheet_name]
                if "symbol" in sdf.columns:
                    syms = sdf["symbol"].dropna().astype(str).str.strip()
                    all_tickers |= set(syms[syms != ""])

        # If --full, also collect universe tickers
        if args.full:
            if "MPD Data" in sheets:
                mpd = sheets["MPD Data"]
                if "Yahoo" in mpd.columns:
                    syms = mpd["Yahoo"].dropna().astype(str).str.strip()
                    all_tickers |= set(syms[syms != ""])
            if "Screener Data" in sheets:
                scr = sheets["Screener Data"]
                if "Ticker" in scr.columns:
                    syms = scr["Ticker"].dropna().astype(str).str.strip()
                    all_tickers |= set(syms[syms != ""])

    if not week_data:
        print("  No valid week data loaded.")
        return

    # Clean tickers
    all_tickers = {t for t in all_tickers if t and t != "nan"}
    print(f"  Total unique tickers to fetch: {len(all_tickers)}")

    # ── Fetch OHLCV (one bulk request for all tickers) ──
    ohlcv = _fetch_ohlcv_bulk(sorted(all_tickers))

    # ── Review each week ──
    all_results = []
    all_missed = []

    for w in sorted(week_data.keys()):
        wd = week_data[w]
        sheets = wd["sheets"]
        scan_date = wd["scan_date"]
        days_ago = (TODAY - scan_date).days

        print(f"\n{'─' * 60}")
        print(f"  WEEK {w}  (scanned: {scan_date.strftime('%d-%b-%Y')},"
              f"  {days_ago} days ago)")
        print(f"{'─' * 60}")

        candidate_syms = set()

        for sheet_name, source in [("MPD Breakouts", "MPD"),
                                    ("Screener Breakouts", "Screener")]:
            if sheet_name not in sheets:
                print(f"  {source}: sheet not found")
                continue

            sdf = sheets[sheet_name]

            # Check for placeholder sheet (no real candidates)
            if "Note" in sdf.columns or "symbol" not in sdf.columns:
                print(f"  {source}: no candidates (placeholder sheet)")
                continue

            print(f"\n  {source} Breakouts ({len(sdf)} candidates):")

            for _, row in sdf.iterrows():
                result = _classify_candidate(row, ohlcv, scan_date)
                result["week"] = w
                result["source"] = source
                all_results.append(result)
                candidate_syms.add(str(row.get("symbol", "")))

            # Per-source status summary
            week_src = [r for r in all_results
                        if r["week"] == w and r["source"] == source]
            status_counts = {}
            for r in week_src:
                s = r["status"]
                status_counts[s] = status_counts.get(s, 0) + 1
            for status in ["TRUE_BREAKOUT", "BREAKOUT_LOW_VOL", "ATTEMPTED",
                           "HOLDING", "FALSE_SIGNAL", "NO_DATA"]:
                if status in status_counts:
                    print(f"    {status:20s}: {status_counts[status]}")

        # ── Missed analysis (--full only) ──
        if args.full:
            print(f"\n  Checking for missed breakouts in full universe ...")
            for sheet_name, source, tcol in [
                ("MPD Data", "MPD", "Yahoo"),
                ("Screener Data", "Screener", "Ticker"),
            ]:
                if sheet_name not in sheets:
                    continue
                missed = _check_missed_universe(
                    sheets[sheet_name], candidate_syms, ohlcv,
                    scan_date, tcol)
                for m in missed:
                    m["week"] = w
                    m["source"] = source
                all_missed.extend(missed)
                if missed:
                    print(f"    {source}: {len(missed)} potential missed"
                          f" breakouts (>{MISSED_RALLY_PCT}% rally)")
                else:
                    print(f"    {source}: no missed breakouts detected")

    # ── Deep Analysis ──
    print(f"\n{'=' * 70}")
    print(f"  DEEP ANALYSIS — TRUE BREAKOUT vs FALSE SIGNAL")
    print(f"{'=' * 70}")

    analysis_df = _deep_analysis(all_results)
    if not analysis_df.empty:
        print(analysis_df.to_string(index=False))
    else:
        print("  Insufficient data for deep analysis")
        print("  (need both TRUE_BREAKOUT and FALSE_SIGNAL classifications)")

    # ── Overall Summary ──
    print(f"\n{'=' * 70}")
    print(f"  OVERALL SUMMARY")
    print(f"{'=' * 70}")

    res_df = pd.DataFrame(all_results)
    if not res_df.empty:
        total = len(res_df)
        no_data = int((res_df["status"] == "NO_DATA").sum())
        valid = total - no_data
        true_bo = int(res_df["status"].isin(["TRUE_BREAKOUT"]).sum())
        bo_low = int(res_df["status"].isin(["BREAKOUT_LOW_VOL"]).sum())
        attempted = int((res_df["status"] == "ATTEMPTED").sum())
        holding = int((res_df["status"] == "HOLDING").sum())
        false_sig = int((res_df["status"] == "FALSE_SIGNAL").sum())

        print(f"  Total candidates reviewed : {total}")
        print(f"  Valid (excl. no_data)      : {valid}")
        if valid:
            print(f"  TRUE_BREAKOUT             : {true_bo:>4d}"
                  f"  ({true_bo/valid*100:.1f}%)")
            print(f"  BREAKOUT_LOW_VOL          : {bo_low:>4d}"
                  f"  ({bo_low/valid*100:.1f}%)")
            print(f"  ATTEMPTED                 : {attempted:>4d}"
                  f"  ({attempted/valid*100:.1f}%)")
            print(f"  HOLDING                   : {holding:>4d}"
                  f"  ({holding/valid*100:.1f}%)")
            print(f"  FALSE_SIGNAL              : {false_sig:>4d}"
                  f"  ({false_sig/valid*100:.1f}%)")
        print(f"  NO_DATA                   : {no_data:>4d}")

        if valid:
            print(f"\n  Hit rate (strict — TRUE_BREAKOUT only)        :"
                  f" {true_bo/valid*100:.1f}%")
            print(f"  Hit rate (loose  — incl. BREAKOUT_LOW_VOL)    :"
                  f" {(true_bo+bo_low)/valid*100:.1f}%")
            print(f"  Hit rate (action — incl. ATTEMPTED)           :"
                  f" {(true_bo+bo_low+attempted)/valid*100:.1f}%")

        # Top performers
        res_with_gain = res_df.dropna(subset=["max_gain_pct"])
        if len(res_with_gain) >= 3:
            top = res_with_gain.sort_values(
                "max_gain_pct", ascending=False
            ).head(10)
            print(f"\n  Top 10 performers (by max gain from scan):")
            show_cols = ["symbol", "week", "source", "score",
                         "high_conviction", "status", "scan_close",
                         "max_high", "max_gain_pct", "pct_change"]
            show_cols = [c for c in show_cols if c in top.columns]
            print(top[show_cols].to_string(index=False))

        # Worst performers
        if len(res_with_gain) >= 3:
            bottom = res_with_gain.sort_values(
                "pct_change", ascending=True
            ).head(5)
            print(f"\n  Bottom 5 (worst declines from scan):")
            print(bottom[show_cols].to_string(index=False))
    else:
        print("  No results to summarise.")

    # ── Write review Excel ──
    review_xlsx = os.path.join(OUTPUT_DIR, f"review_{TIMESTAMP}.xlsx")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with pd.ExcelWriter(review_xlsx, engine="openpyxl") as writer:
        if not res_df.empty:
            # Per-week sheets
            for w in sorted(week_data.keys()):
                wdf = res_df[res_df["week"] == w].copy()
                if not wdf.empty:
                    wdf.sort_values(
                        ["status", "max_gain_pct"],
                        ascending=[True, False],
                        na_position="last",
                    ).to_excel(writer, sheet_name=f"Week {w}", index=False)

            # All results combined
            res_df.sort_values(
                ["week", "source", "status", "max_gain_pct"],
                ascending=[True, True, True, False],
                na_position="last",
            ).to_excel(writer, sheet_name="All Results", index=False)

        # Deep analysis sheet
        if not analysis_df.empty:
            analysis_df.to_excel(
                writer, sheet_name="Deep Analysis", index=False)

        # Missed breakouts sheet
        if all_missed:
            missed_df = pd.DataFrame(all_missed)
            missed_df.sort_values(
                "max_gain_pct", ascending=False
            ).to_excel(writer, sheet_name="Missed", index=False)

        # Summary statistics sheet
        if not res_df.empty:
            summary_rows = []
            for (w, src), grp in res_df.groupby(["week", "source"]):
                valid_g = grp[grp["status"] != "NO_DATA"]
                n_valid = len(valid_g)
                n_true = int(valid_g["status"].isin(
                    ["TRUE_BREAKOUT"]).sum())
                n_low = int(valid_g["status"].isin(
                    ["BREAKOUT_LOW_VOL"]).sum())
                summary_rows.append({
                    "week": int(w),
                    "source": src,
                    "total": len(grp),
                    "valid": n_valid,
                    "true_breakout": n_true,
                    "breakout_low_vol": n_low,
                    "attempted": int(
                        (valid_g["status"] == "ATTEMPTED").sum()),
                    "holding": int(
                        (valid_g["status"] == "HOLDING").sum()),
                    "false_signal": int(
                        (valid_g["status"] == "FALSE_SIGNAL").sum()),
                    "hit_strict_%": round(
                        n_true / n_valid * 100, 1) if n_valid else 0,
                    "hit_loose_%": round(
                        (n_true + n_low) / n_valid * 100, 1
                    ) if n_valid else 0,
                })
            pd.DataFrame(summary_rows).to_excel(
                writer, sheet_name="Summary", index=False)

    print(f"\n  Review written: {review_xlsx}")

    # ── Update cumulative CSV ──
    if all_results:
        _update_cumulative(all_results, TODAY.strftime("%Y-%m-%d"))

    print("\nDONE.")


if __name__ == "__main__":
    main()
