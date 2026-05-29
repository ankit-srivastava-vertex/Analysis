"""
fno_max_oi.py — F&O Max OI Strike Scanner
==========================================

Scans all NSE F&O stocks + NIFTY/BANKNIFTY index options to find the strike
price with the highest Open Interest for both Calls and Puts. Identifies
support (max put OI strike) and resistance (max call OI strike) levels.

PRIMARY SOURCE: Angel One SmartAPI Market Data (FULL mode) — live intraday OI
FALLBACK:       NSE BhavCopy zip (EOD data, no auth needed)

OUTPUT FILE: fno_<month>.xlsx  (e.g. fno_may.xlsx, fno_jun.xlsx)
  - The month in the filename is determined by the date of the first sheet.
  - Each daily run adds a new sheet named by the run date (e.g. "27-May-2026").
  - Re-running on the same day replaces the existing sheet for that date.
  - Use --new to start a fresh file (old file is not deleted).

SHEET STRUCTURE:
  Single sheet per day combining equity + index results with a "Type" column.
  Columns: Type, Symbol, Expiry, Underlying LTP, Call Max OI, Call Strike,
           Put Max OI, Put Strike

USAGE:
  python3 fno_max_oi.py                  # append sheet to existing fno_<month>.xlsx
  python3 fno_max_oi.py --new            # create a new fno_<month>.xlsx
  python3 fno_max_oi.py --expiry weekly  # nearest weekly expiry (default)
  python3 fno_max_oi.py --expiry monthly # nearest monthly expiry only
  python3 fno_max_oi.py --eod            # force EOD bhavcopy (skip Angel)

DEPENDENCIES:
  smartapi-python, pyotp, pandas, openpyxl, httpx
"""

import os
import sys
import json
import time
import zipfile
import io
import argparse
from datetime import date, datetime, timedelta
from typing import Optional

import glob

import pandas as pd
import httpx

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# Angel One scrip master (same cache as angel_client.py)
SCRIP_MASTER_CACHE = os.path.join(SCRIPT_DIR, ".angel_scrip_master.json")
SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)

# Index symbols for Sheet 2
INDEX_SYMBOLS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]

# NSE BhavCopy base URL (fallback)
BHAVCOPY_BASE = "https://nsearchives.nseindia.com/content/fo"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


# ===========================================================================
# SCRIP MASTER — load & filter NFO options
# ===========================================================================

def load_scrip_master():
    """Load the Angel One scrip master JSON (cached or fresh download)."""
    # Use existing cache if fresh (< 1 day for F&O since new expiries appear daily)
    if os.path.exists(SCRIP_MASTER_CACHE):
        age_hrs = (time.time() - os.path.getmtime(SCRIP_MASTER_CACHE)) / 3600
        if age_hrs < 24:
            with open(SCRIP_MASTER_CACHE) as f:
                return json.load(f)

    print("  Downloading scrip master (~25 MB)...")
    import urllib.request
    req = urllib.request.Request(SCRIP_MASTER_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    with open(SCRIP_MASTER_CACHE, "w") as f:
        json.dump(data, f)
    print(f"  Scrip master cached: {len(data)} instruments")
    return data


def get_fno_symbols_from_master(master):
    """Extract unique underlying symbols that have OPTSTK instruments on NFO."""
    symbols = set()
    for inst in master:
        if (inst.get("exch_seg") == "NFO" and
                inst.get("instrumenttype") in ("OPTSTK",)):
            name = inst.get("name", "").strip().upper()
            if name:
                symbols.add(name)
    return sorted(symbols)


def get_option_tokens(master, underlying, expiry_type="weekly"):
    """Get all option tokens for a given underlying (nearest expiry).

    Returns: list of dicts with keys: token, strike, option_type, expiry, symbol
    """
    today = date.today()
    options = []

    # Determine instrument type
    if underlying in INDEX_SYMBOLS:
        inst_types = ("OPTIDX",)
    else:
        inst_types = ("OPTSTK",)

    for inst in master:
        if inst.get("exch_seg") != "NFO":
            continue
        if inst.get("instrumenttype") not in inst_types:
            continue
        if inst.get("name", "").strip().upper() != underlying:
            continue

        # Parse expiry
        exp_str = inst.get("expiry", "")
        try:
            exp_date = datetime.strptime(exp_str, "%d%b%Y").date()
        except (ValueError, TypeError):
            continue

        if exp_date < today:
            continue

        strike = float(inst.get("strike", 0)) / 100.0  # Angel stores strike * 100
        opt_type = inst.get("symbol", "")
        # Determine CE/PE from the trading symbol suffix
        trading_sym = inst.get("symbol", "").upper()
        if trading_sym.endswith("CE"):
            ot = "CE"
        elif trading_sym.endswith("PE"):
            ot = "PE"
        else:
            continue

        options.append({
            "token": inst.get("token", ""),
            "strike": strike,
            "option_type": ot,
            "expiry": exp_date,
            "trading_symbol": trading_sym,
        })

    if not options:
        return [], None

    # Find nearest expiry
    expiries = sorted(set(o["expiry"] for o in options))

    if expiry_type == "monthly":
        # Monthly = last Thursday of month (pick the expiry closest to month-end)
        monthly_expiries = []
        for exp in expiries:
            # Check if it's the last expiry in its month
            next_month_start = (exp.replace(day=28) + timedelta(days=4)).replace(day=1)
            later_in_month = [e for e in expiries if e > exp and e < next_month_start]
            if not later_in_month:
                monthly_expiries.append(exp)
        if monthly_expiries:
            target_exp = monthly_expiries[0]
        else:
            target_exp = expiries[0]
    else:
        # Weekly = nearest available expiry
        target_exp = expiries[0]

    filtered = [o for o in options if o["expiry"] == target_exp]
    return filtered, target_exp


# ===========================================================================
# ANGEL ONE LIVE DATA — Market Data API (FULL mode with OI)
# ===========================================================================

def fetch_live_oi_angel(master, symbols, expiry_type="weekly", is_index=False):
    """Fetch live OI for all option tokens via Angel One Market Data API.

    Returns DataFrame with: Symbol, Expiry, LTP, Call_Max_OI, Call_Strike,
    Put_Max_OI, Put_Strike, PCR, Call_OI_Change, Put_OI_Change
    """
    from angel_client import _ensure_session, _load_env, get_angel_session

    _load_env()
    api_key, jwt = get_angel_session()
    obj = _ensure_session()

    results = []
    total = len(symbols)

    for idx, sym in enumerate(symbols, 1):
        options, expiry = get_option_tokens(master, sym, expiry_type)
        if not options or expiry is None:
            continue

        # Collect all tokens for this symbol
        tokens = [o["token"] for o in options if o["token"]]
        if not tokens:
            continue

        # Fetch in batches of 50
        all_quotes = {}
        for batch_start in range(0, len(tokens), 50):
            batch = tokens[batch_start:batch_start + 50]

            for attempt in range(3):
                try:
                    resp = obj.getMarketData("FULL", {"NFO": batch})
                    if resp and resp.get("status"):
                        fetched = resp.get("data", {}).get("fetched", [])
                        for q in fetched:
                            all_quotes[q["symbolToken"]] = q
                        break
                    else:
                        err = resp.get("message", "") if resp else "No response"
                        if "AG8001" in str(resp.get("errorcode", "")).upper():
                            # Token expired, refresh
                            from angel_client import refresh_token
                            refresh_token(force=False)
                            api_key, jwt = get_angel_session()
                            obj = _ensure_session()
                        time.sleep(0.5 * (attempt + 1))
                except Exception as e:
                    time.sleep(0.5 * (attempt + 1))

            # Rate limit: 1 req/sec
            time.sleep(1.0)

        # Process quotes — find max OI for calls and puts
        calls = []
        puts = []
        underlying_ltp = None

        for opt in options:
            tok = opt["token"]
            if tok not in all_quotes:
                continue
            q = all_quotes[tok]
            oi = q.get("opnInterest", 0) or 0
            ltp = q.get("ltp", 0) or 0
            # For underlying LTP, use the close field or fetch separately
            if underlying_ltp is None and q.get("ltp"):
                # We'll get underlying LTP from the equity token later
                pass

            entry = {
                "strike": opt["strike"],
                "oi": oi,
                "ltp": ltp,
                "change_oi": q.get("opnInterest", 0),  # Angel doesn't give change directly
            }
            if opt["option_type"] == "CE":
                calls.append(entry)
            else:
                puts.append(entry)

        if not calls or not puts:
            if idx % 20 == 0:
                print(f"  [{idx}/{total}] {sym}: no OI data")
            continue

        # Find max OI
        max_call = max(calls, key=lambda x: x["oi"])
        max_put = max(puts, key=lambda x: x["oi"])

        results.append({
            "Symbol": sym,
            "Expiry": expiry.strftime("%Y-%m-%d"),
            "Underlying LTP": None,  # will fill from equity quote
            "Call Max OI": max_call["oi"],
            "Call Strike": max_call["strike"],
            "Put Max OI": max_put["oi"],
            "Put Strike": max_put["strike"],
        })

        if idx % 10 == 0 or idx == total:
            print(f"  [{idx}/{total}] Processed {sym} — "
                  f"Call: {max_call['oi']:,} @ {max_call['strike']}, "
                  f"Put: {max_put['oi']:,} @ {max_put['strike']}")

    # Fetch underlying LTPs in bulk
    if results:
        _fill_underlying_ltp(obj, master, results, is_index)

    return pd.DataFrame(results)


def _fill_underlying_ltp(obj, master, results, is_index):
    """Fill underlying LTP by fetching equity/index quotes."""
    # Build symbol -> token map for underlying
    sym_tokens = {}
    for inst in master:
        if is_index:
            # For indices, look in NSE with specific tokens
            # NIFTY 50 = 99926000, BANKNIFTY = 99926009, etc.
            if inst.get("exch_seg") == "NSE" and inst.get("symbol", "").upper() in (
                "NIFTY 50", "NIFTY BANK", "NIFTY FIN SERVICE", "NIFTY MID SELECT"
            ):
                name_map = {
                    "NIFTY 50": "NIFTY",
                    "NIFTY BANK": "BANKNIFTY",
                    "NIFTY FIN SERVICE": "FINNIFTY",
                    "NIFTY MID SELECT": "MIDCPNIFTY",
                }
                mapped = name_map.get(inst["symbol"].upper())
                if mapped:
                    sym_tokens[mapped] = inst["token"]
        else:
            if (inst.get("exch_seg") == "NSE" and
                    inst.get("symbol", "").upper().endswith("-EQ")):
                base = inst["symbol"].upper().replace("-EQ", "")
                sym_tokens[base] = inst["token"]

    # Batch fetch
    needed_syms = [r["Symbol"] for r in results if r["Symbol"] in sym_tokens]
    tokens_to_fetch = [sym_tokens[s] for s in needed_syms if s in sym_tokens]
    token_to_sym = {sym_tokens[s]: s for s in needed_syms if s in sym_tokens}

    for batch_start in range(0, len(tokens_to_fetch), 50):
        batch = tokens_to_fetch[batch_start:batch_start + 50]
        try:
            resp = obj.getMarketData("LTP", {"NSE": batch})
            if resp and resp.get("status"):
                for q in resp.get("data", {}).get("fetched", []):
                    sym = token_to_sym.get(q["symbolToken"])
                    if sym:
                        for r in results:
                            if r["Symbol"] == sym:
                                r["Underlying LTP"] = q.get("ltp")
                                break
        except Exception:
            pass
        time.sleep(1.0)


# ===========================================================================
# FALLBACK — NSE BhavCopy (EOD, no auth)
# ===========================================================================

def fetch_eod_bhavcopy(same_day_only=False):
    """Download NSE F&O BhavCopy zip and return parsed DataFrame.

    Args:
        same_day_only: If True, only try today's date. If False, search last 7 days.
    """
    label = "same-day" if same_day_only else "recent"
    print(f"  Fetching NSE BhavCopy ({label} EOD data)...")
    client = httpx.Client(http2=False, follow_redirects=True, timeout=20,
                          headers={"User-Agent": UA})

    days_range = 1 if same_day_only else 7
    for days_ago in range(days_range):
        d = date.today() - timedelta(days=days_ago)
        if d.weekday() >= 5:
            continue
        yyyymmdd = d.strftime("%Y%m%d")
        url = f"{BHAVCOPY_BASE}/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
        try:
            r = client.get(url, timeout=15)
            if r.status_code == 200 and len(r.content) > 5000:
                z = zipfile.ZipFile(io.BytesIO(r.content))
                csv_name = z.namelist()[0]
                data = z.read(csv_name).decode("utf-8", errors="ignore")
                print(f"  ✓ Found BhavCopy for {d}")
                client.close()
                return d, pd.read_csv(io.StringIO(data))
        except Exception:
            pass

    client.close()
    if same_day_only:
        print("  ✗ Same-day BhavCopy not available yet")
    else:
        print("  ERROR: No BhavCopy found in last 7 days")
    return None, None


def parse_bhavcopy(df, expiry_type="weekly", filter_symbols=None, inst_type="STO"):
    """Parse BhavCopy DataFrame and extract max OI per symbol.

    Args:
        df: raw bhavcopy DataFrame (new NSE format)
        expiry_type: 'weekly' or 'monthly'
        filter_symbols: set of symbols to include (None = all)
        inst_type: 'STO' (stock options) or 'IDO' (index options)

    Returns: DataFrame with results
    """
    df.columns = [c.strip() for c in df.columns]
    for col in df.select_dtypes(include='object').columns:
        df[col] = df[col].str.strip()

    # Filter to options
    opts = df[df["FinInstrmTp"] == inst_type].copy()
    if opts.empty:
        return pd.DataFrame()

    opts["StrkPric"] = pd.to_numeric(opts["StrkPric"], errors="coerce")
    opts["OpnIntrst"] = pd.to_numeric(opts["OpnIntrst"], errors="coerce")
    opts["ChngInOpnIntrst"] = pd.to_numeric(opts.get("ChngInOpnIntrst", 0), errors="coerce")
    opts["LastPric"] = pd.to_numeric(opts.get("LastPric", 0), errors="coerce")
    opts["UndrlygPric"] = pd.to_numeric(opts.get("UndrlygPric", 0), errors="coerce")
    opts["NewBrdLotQty"] = pd.to_numeric(opts.get("NewBrdLotQty", 1), errors="coerce").replace(0, 1)
    opts["XpryDt"] = pd.to_datetime(opts["XpryDt"], format="mixed", dayfirst=True)

    # Convert OI from shares to lots (contracts)
    opts["OpnIntrst"] = (opts["OpnIntrst"] / opts["NewBrdLotQty"]).round().astype(int)
    opts["ChngInOpnIntrst"] = (opts["ChngInOpnIntrst"] / opts["NewBrdLotQty"]).round().astype(int)

    # TckrSymb contains the underlying symbol directly (e.g., RELIANCE, BANKNIFTY)
    opts["Underlying"] = opts["TckrSymb"]

    if filter_symbols:
        opts = opts[opts["Underlying"].isin(filter_symbols)]

    if opts.empty:
        return pd.DataFrame()

    # Find nearest expiry per underlying
    results = []
    for sym, grp in opts.groupby("Underlying"):
        expiries = sorted(grp["XpryDt"].unique())

        if expiry_type == "monthly":
            # Pick the last expiry of the nearest month
            monthly = []
            for exp in expiries:
                exp_date = pd.Timestamp(exp).date()
                next_month = (exp_date.replace(day=28) + timedelta(days=4)).replace(day=1)
                later = [e for e in expiries if pd.Timestamp(e).date() > exp_date
                         and pd.Timestamp(e).date() < next_month]
                if not later:
                    monthly.append(exp)
            target_exp = monthly[0] if monthly else expiries[0]
        else:
            target_exp = expiries[0]

        exp_grp = grp[grp["XpryDt"] == target_exp]
        calls = exp_grp[exp_grp["OptnTp"] == "CE"]
        puts = exp_grp[exp_grp["OptnTp"] == "PE"]

        if calls.empty or puts.empty:
            continue

        max_call = calls.loc[calls["OpnIntrst"].idxmax()]
        max_put = puts.loc[puts["OpnIntrst"].idxmax()]
        total_call_oi = calls["OpnIntrst"].sum()
        total_put_oi = puts["OpnIntrst"].sum()
        pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi > 0 else 0

        # Change in OI for max OI strikes
        call_oi_change = max_call.get("ChngInOpnIntrst", 0) or 0
        put_oi_change = max_put.get("ChngInOpnIntrst", 0) or 0

        # Underlying LTP from UndrlygPric
        underlying_ltp = None
        ltp_vals = exp_grp["UndrlygPric"].dropna()
        if not ltp_vals.empty and ltp_vals.iloc[0] > 0:
            underlying_ltp = ltp_vals.iloc[0]

        results.append({
            "Symbol": sym,
            "Expiry": pd.Timestamp(target_exp).strftime("%Y-%m-%d"),
            "Underlying LTP": underlying_ltp,
            "Call Max OI": int(max_call["OpnIntrst"]),
            "Call Strike": max_call["StrkPric"],
            "Put Max OI": int(max_put["OpnIntrst"]),
            "Put Strike": max_put["StrkPric"],
        })

    return pd.DataFrame(results)


# ===========================================================================
# MAIN ORCHESTRATOR
# ===========================================================================

def _get_output_path(create_new=False):
    """Determine output Excel path.

    - If create_new or no existing file: fno_<current_month>.xlsx
    - Otherwise: most recent existing fno_*.xlsx
    """
    existing = sorted(glob.glob(os.path.join(SCRIPT_DIR, "fno_*.xlsx")))
    if not create_new and existing:
        return existing[-1]
    # New file named after current month
    month_name = date.today().strftime("%b").lower()  # e.g. 'may'
    return os.path.join(SCRIPT_DIR, f"fno_{month_name}.xlsx")


def _is_market_open_today():
    """Check if NSE market has opened today (9:15 AM IST on a weekday).

    Returns True only if today is a weekday AND current time >= 9:15 AM IST.
    Angel One OI data is stale (previous session) until market opens.
    """
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    today_ist = now_ist.date()

    # Weekend — market closed
    if today_ist.weekday() >= 5:
        return False

    # Market opens at 9:15 AM IST
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    return now_ist >= market_open


def _drop_unwanted_cols(df):
    """Remove columns not needed in output."""
    drop = ["PCR", "Total Call OI", "Total Put OI", "Call OI Change", "Put OI Change"]
    return df.drop(columns=[c for c in drop if c in df.columns], errors="ignore")


def run(expiry_type="weekly", force_live=False, create_new=False):
    """Main entry point. Returns (combined_df, output_path)."""
    print("=" * 70)
    print("  F&O MAX OI SCANNER")
    print("=" * 70)
    print(f"  Date: {date.today()}")
    print(f"  Expiry: {expiry_type}")
    print(f"  Mode: {'Live (Angel One)' if force_live else 'BhavCopy (same-day) with Angel One fallback'}")
    print(f"  File mode: {'New file' if create_new else 'Append to existing'}")
    print()

    equity_df = pd.DataFrame()
    index_df = pd.DataFrame()

    bhavcopy_success = False

    data_date = None  # actual date of the OI data

    # Try same-day BhavCopy first (unless forced live)
    if not force_live:
        print("[1/2] Trying same-day NSE BhavCopy...")
        bhavcopy_date, bhavcopy_df = fetch_eod_bhavcopy(same_day_only=True)
        if bhavcopy_df is not None:
            print(f"  Parsing equity options (STO)...")
            equity_df = parse_bhavcopy(bhavcopy_df, expiry_type, inst_type="STO")
            print(f"  Parsing index options (IDO)...")
            index_df = parse_bhavcopy(
                bhavcopy_df, expiry_type,
                filter_symbols=set(INDEX_SYMBOLS), inst_type="IDO"
            )
            if not equity_df.empty:
                bhavcopy_success = True
                data_date = bhavcopy_date
                print(f"  ✓ BhavCopy ({bhavcopy_date}): "
                      f"{len(equity_df)} equity + {len(index_df)} index results")

    # Fallback to Angel One live — only if market is open today
    if not bhavcopy_success:
        if not _is_market_open_today():
            print("\n  ✗ Market has not opened today (pre-market or holiday).")
            print("    Angel One data would be stale (previous session). Aborting.")
            return pd.DataFrame(), None

        print("\n[2/2] Fetching live OI via Angel One...")
        try:
            print("  Loading scrip master...")
            master = load_scrip_master()

            print("  Fetching live OI — Equity F&O...")
            equity_symbols = get_fno_symbols_from_master(master)
            print(f"  Found {len(equity_symbols)} F&O equity symbols")
            equity_df = fetch_live_oi_angel(master, equity_symbols, expiry_type, is_index=False)

            print(f"\n  Fetching live OI — Index F&O...")
            index_df = fetch_live_oi_angel(master, INDEX_SYMBOLS, expiry_type, is_index=True)

            if not equity_df.empty:
                data_date = date.today()  # market is open, data is current
                print(f"\n  ✓ Angel One: {len(equity_df)} equity + {len(index_df)} index results")
            else:
                print("  ✗ No data from Angel One either")
                return pd.DataFrame(), None
        except Exception as e:
            print(f"\n  ✗ Angel One failed: {e}")
            print("  No data source available")
            return pd.DataFrame(), None

    # Drop unwanted columns
    equity_df = _drop_unwanted_cols(equity_df)
    index_df = _drop_unwanted_cols(index_df)

    # Sort results
    if not equity_df.empty:
        equity_df = equity_df.sort_values("Symbol").reset_index(drop=True)
    if not index_df.empty:
        index_df = index_df.sort_values("Symbol").reset_index(drop=True)

    # Combine equity + index into one sheet (add Type column)
    if not equity_df.empty:
        equity_df.insert(0, "Type", "Equity")
    if not index_df.empty:
        index_df.insert(0, "Type", "Index")
    combined_df = pd.concat([equity_df, index_df], ignore_index=True)

    if combined_df.empty:
        print("  ✗ No data to write")
        return combined_df, None

    # Determine output file and sheet name (use actual data date, not run date)
    output_path = _get_output_path(create_new=create_new)
    sheet_name = data_date.strftime("%d-%b-%Y")  # e.g. "27-May-2026"

    # Write: append sheet to existing file, or create new
    if os.path.exists(output_path) and not create_new:
        # Append new sheet to existing workbook
        with pd.ExcelWriter(output_path, engine="openpyxl", mode="a",
                            if_sheet_exists="replace") as w:
            combined_df.to_excel(w, sheet_name=sheet_name, index=False)
    else:
        # Create new file
        with pd.ExcelWriter(output_path, engine="openpyxl") as w:
            combined_df.to_excel(w, sheet_name=sheet_name, index=False)

    print(f"\n  ✓ Done: {output_path}")
    print(f"    Sheet: {sheet_name}")
    print(f"    Rows: {len(combined_df)} ({len(equity_df)} equity + {len(index_df)} index)")

    return combined_df, output_path


# ===========================================================================
# CLI
# ===========================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="F&O Max OI Strike Scanner")
    parser.add_argument("--expiry", choices=["weekly", "monthly"],
                        default="weekly", help="Expiry type (default: weekly)")
    parser.add_argument("--live", action="store_true",
                        help="Force Angel One live mode (skip BhavCopy)")
    parser.add_argument("--new", action="store_true",
                        help="Create a new Excel file (default: append to existing)")
    args = parser.parse_args()

    t0 = time.time()
    combined, out = run(expiry_type=args.expiry, force_live=args.live, create_new=args.new)
    elapsed = time.time() - t0
    print(f"\n  Total time: {elapsed:.1f}s")

    # Print summary
    if not combined.empty:
        eq_only = combined[combined["Type"] == "Equity"]
        if not eq_only.empty:
            print("\n  Top 10 by Call Max OI (Equity):")
            top = eq_only.nlargest(10, "Call Max OI")[["Symbol", "Call Strike", "Call Max OI", "Put Strike", "Put Max OI"]]
            print(top.to_string(index=False))
