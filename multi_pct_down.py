"""
Multi-Universe Pct-Down Screener (NSE / NSE-SME / BSE-SME)
==========================================================
Initial universes (fetched live, no cached CSV files):
  - NSE main board   <- nsearchives.nseindia.com EQUITY_L.csv
  - NSE SME (Emerge) <- nsearchives.nseindia.com SME_EQUITY_L.csv
  - BSE SME platform <- api.bseindia.com ListofScripData (groups M/MT/MS)

Pipeline (applied independently per universe; see FILTER_MATRIX below):
  1. Load full universe
  2. Drop NSE F&O underlyings           [NSE only]
  3. Keep market cap in [MCAP_MIN_CR, MCAP_MAX_CR] (300-45000 Cr) [NSE only]
  4. Drop stocks with 1Y price change > MAX_1Y_RUNUP_PCT (50%)   [all]
  5. Keep stocks down between MIN_PCT% and MAX_PCT% (2%-30%) from
     their 3M / 6M / 9M highs                                    [all]

Filter matrix:
  +-----------+-----------+-----------------+----------+----------------+
  | Universe  | F&O drop  | Mcap 300-45k Cr | 1Y runup | Pct down 2-30% |
  +-----------+-----------+-----------------+----------+----------------+
  | NSE       |    Yes    |      Yes        |   Yes    |      Yes       |
  | NSE_SME   |    No     |      No         |   Yes    |      Yes       |
  | BSE_SME   |    No     |      No         |   Yes    |      Yes       |
  +-----------+-----------+-----------------+----------+----------------+

Output: one Excel workbook with sheets per universe:
   <UNI> 3M, <UNI> 6M, <UNI> 9M
   <UNI> Common 3M+6M
   <UNI> Common 3M+6M+9M

Usage:
  python multi_pct_down.py
  python multi_pct_down.py --min 5 --max 25
  python multi_pct_down.py --skip bse_sme --workers 4
  python multi_pct_down.py --max-symbols 100   # quick test
  python multi_pct_down.py --workers 2         # safest vs Yahoo rate limit
  python multi_pct_down.py -o my_report

Coverage notes:
  Yahoo Finance does NOT carry NSE Emerge (NSE_SME) listings, so most
  of that universe will be missing data even with retries. NSE main and
  BSE_SME are well covered; transient failures are recovered via:
    - up to 3 retries with exponential backoff (1s/2s/4s)
    - automatic .NS -> .BO fallback for NSE tickers that have a BSE
      listing too (uses BSE's full active-equity list as the lookup map)
"""

import os
import sys
import csv
import io
import json
import time
import argparse
import datetime
import warnings
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

warnings.filterwarnings("ignore")


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TODAY = datetime.date.today()
PERIODS = [(3, "3M"), (6, "6M"), (9, "9M")]

# Filters
MCAP_MIN_CR = 300
MCAP_MAX_CR = 45000
MAX_1Y_RUNUP_PCT = 50.0
DEFAULT_WORKERS = 4   # lower than before: Yahoo rate-limits aggressive
                      # parallelism and starts returning empty data
RETRY_BACKOFF_S = 1.0 # base backoff seconds between retries

# ---------------------------------------------------------------------------
# Per-universe filter matrix (single source of truth - used by run() and
# echoed at the top of every run for explainability).
#
#   +-----------+-----------+-----------------+----------+----------------+
#   | Universe  | F&O drop  | Mcap 300-45k Cr | 1Y runup | Pct down 2-30% |
#   +-----------+-----------+-----------------+----------+----------------+
#   | NSE       |    Yes    |      Yes        |   Yes    |      Yes       |
#   | NSE_SME   |    No     |      No         |   Yes    |      Yes       |
#   | BSE_SME   |    No     |      No         |   Yes    |      Yes       |
#   +-----------+-----------+-----------------+----------+----------------+
#
# 1Y runup and Pct-down filters are always applied (hard-coded in the
# per-ticker analyzer); the booleans below toggle only F&O removal and the
# market-cap band.
# ---------------------------------------------------------------------------
FILTER_MATRIX = {
    # apply_fno  : drop F&O underlyings
    # apply_mcap : enforce MCAP_MIN_CR..MCAP_MAX_CR band
    # max_retries: Yahoo download retry budget (NSE_SME=1 because Yahoo
    #              does not carry NSE Emerge listings, so retries waste time)
    "NSE":     {"apply_fno": True,  "apply_mcap": True,  "max_retries": 3},
    "NSE_SME": {"apply_fno": False, "apply_mcap": False, "max_retries": 1},
    "BSE_SME": {"apply_fno": False, "apply_mcap": False, "max_retries": 3},
}


def print_filter_matrix():
    print("  Filter matrix:")
    print("  +-----------+----------+----------+----------+----------+")
    print("  | Universe  | F&O drop | Mcap band| 1Y runup | Pct down |")
    print("  +-----------+----------+----------+----------+----------+")
    for uni, cfg in FILTER_MATRIX.items():
        print("  | %-9s |   %-3s    |   %-3s    |   Yes    |   Yes    |" % (
            uni,
            "Yes" if cfg["apply_fno"] else "No",
            "Yes" if cfg["apply_mcap"] else "No",
        ))
    print("  +-----------+----------+----------+----------+----------+")

# --- live data sources ------------------------------------------------------
NSE_EQUITY_URL = (
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
)
NSE_SME_URL = (
    "https://nsearchives.nseindia.com/emerge/corporates/content/"
    "SME_EQUITY_L.csv"
)
BSE_LIST_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
BSE_SME_GROUPS = {"M", "MT", "MS"}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _http_get(url, referer=None, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        **({"Referer": referer} if referer else {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_nse_equity_universe():
    """Live-fetch NSE main board list -> [(yahoo, symbol, name), ...]."""
    print("-> Fetching NSE main board list ...")
    raw = _http_get(NSE_EQUITY_URL, referer="https://www.nseindia.com/")
    text = raw.decode("utf-8", errors="ignore")
    out = []
    r = csv.reader(io.StringIO(text))
    next(r, None)
    for row in r:
        if not row:
            continue
        sym = row[0].strip()
        name = row[1].strip() if len(row) > 1 else sym
        if sym:
            out.append(("%s.NS" % sym, sym, name))
    print("   NSE symbols: %d" % len(out))
    return out


def fetch_nse_sme_universe():
    """Live-fetch NSE SME (Emerge) list -> [(yahoo, symbol, name), ...]."""
    print("-> Fetching NSE SME (Emerge) list ...")
    raw = _http_get(NSE_SME_URL, referer="https://www.nseindia.com/emerge/")
    text = raw.decode("utf-8", errors="ignore")
    out = []
    r = csv.reader(io.StringIO(text))
    next(r, None)
    for row in r:
        if not row:
            continue
        sym = row[0].strip()
        name = row[1].strip() if len(row) > 1 else sym
        if sym:
            out.append(("%s.NS" % sym, sym, name))
    print("   NSE_SME symbols: %d" % len(out))
    return out


def fetch_bse_sme_universe():
    """Live-fetch BSE SME platform list -> [(yahoo, code, name), ...]."""
    print("-> Fetching BSE SME platform list ...")
    raw = _http_get(BSE_LIST_URL, referer="https://www.bseindia.com/")
    data = json.loads(raw)
    out = []
    for r in data:
        if r.get("GROUP") not in BSE_SME_GROUPS:
            continue
        if r.get("Status") != "Active":
            continue
        code = (r.get("SCRIP_CD") or "").strip()
        name = (r.get("Scrip_Name") or r.get("Issuer_Name") or code).strip()
        if code:
            out.append(("%s.BO" % code, code, name))
    print("   BSE_SME symbols: %d" % len(out))
    return out


def fetch_bse_full_symbol_map():
    """Return {scrip_id_upper: '<scripcode>.BO'} for ALL active BSE
    equities (used as a .NS -> .BO fallback when Yahoo doesn't carry the
    NSE listing)."""
    print("-> Fetching BSE full equity list (for NSE->BSE fallback) ...")
    try:
        raw = _http_get(BSE_LIST_URL, referer="https://www.bseindia.com/")
        data = json.loads(raw)
    except Exception as e:
        print("   WARN  Could not fetch BSE list (%s); fallback disabled."
              % e)
        return {}
    mp = {}
    for r in data:
        if r.get("Status") != "Active":
            continue
        sid = (r.get("scrip_id") or "").strip().upper()
        code = (r.get("SCRIP_CD") or "").strip()
        if sid and code:
            mp[sid] = "%s.BO" % code
    print("   BSE active equities indexed: %d" % len(mp))
    return mp


# F&O underlyings list (NSE) -> used to drop F&O names from the universe.
FNO_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"


def load_fno_symbols():
    """Return set of NSE symbols that have F&O contracts."""
    try:
        raw = _http_get(FNO_URL, referer="https://www.nseindia.com/")
    except Exception as e:
        print("   WARN  Could not fetch F&O list (%s); skipping F&O filter."
              % e)
        return set()
    syms = set()
    for ln in raw.decode("utf-8", errors="ignore").splitlines():
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 2:
            continue
        sym = parts[1].strip().upper()
        if not sym or sym == "SYMBOL" or sym.startswith("NIFTY") or \
                sym.startswith("BANKNIFTY") or sym.startswith("FINNIFTY") or \
                sym.startswith("MIDCPNIFTY"):
            continue
        syms.add(sym)
    return syms


# --- helpers ----------------------------------------------------------------

def _months_ago(n):
    y, m = TODAY.year, TODAY.month - n
    while m <= 0:
        m += 12
        y -= 1
    return datetime.date(y, m, min(TODAY.day, 28))


def _get_market_cap_cr(yf, ticker, last_close=None):
    """Yahoo `fast_info.market_cap` is None for most NSE/BSE tickers,
    so we compute mcap = shares * last_close."""
    try:
        fi = yf.Ticker(ticker).fast_info
        try:
            mc = fi.get("market_cap") if hasattr(fi, "get") else \
                getattr(fi, "market_cap", None)
        except Exception:
            mc = None
        if mc:
            return float(mc) / 1e7
        try:
            shares = fi.get("shares") if hasattr(fi, "get") else \
                getattr(fi, "shares", None)
        except Exception:
            shares = None
        if shares and last_close:
            return float(shares) * float(last_close) / 1e7
    except Exception:
        return None
    return None


# --- per-ticker work --------------------------------------------------------

def _yf_download_with_retry(yf, ticker, start_date, max_retries):
    """Download price history with retries + backoff. Returns DataFrame
    or None. Treats empty df as failure (Yahoo's typical rate-limit
    response is 200 OK with an empty body)."""
    for attempt in range(max(1, max_retries)):
        try:
            df = yf.download(
                ticker, start=start_date.isoformat(),
                progress=False, auto_adjust=False, threads=False,
            )
            if df is not None and not df.empty and "Close" in df.columns:
                return df
        except Exception:
            pass
        # backoff only between retries (not after the last attempt)
        if attempt < max_retries - 1:
            time.sleep(RETRY_BACKOFF_S * (2 ** attempt))
    return None


def _fetch_history(yf, primary_ticker, fallback_ticker, start_date,
                   max_retries):
    """Try primary (.NS) first; if empty, try fallback (.BO).
    Returns (df, ticker_used) or (None, primary)."""
    df = _yf_download_with_retry(yf, primary_ticker, start_date, max_retries)
    if df is not None:
        return df, primary_ticker
    if fallback_ticker and fallback_ticker != primary_ticker:
        df = _yf_download_with_retry(yf, fallback_ticker, start_date,
                                     max_retries)
        if df is not None:
            return df, fallback_ticker
    return None, primary_ticker


def _analyze_one(yf, ticker, symbol, name, start_date,
                 mcap_min, mcap_max, max_1y_runup, min_pct, max_pct,
                 apply_mcap, fallback_ticker=None, max_retries=3):
    """Return dict for one ticker:
        {'_drop': str|None, 'periods': {label: row|None}}
    """
    df, used = _fetch_history(yf, ticker, fallback_ticker, start_date,
                              max_retries)
    if df is None:
        return {"_drop": "no_data"}
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    closes = df["Close"].dropna()
    highs = df["High"].dropna() if "High" in df.columns else closes
    if closes.empty:
        return {"_drop": "no_close"}
    # Track which ticker actually delivered data (for diagnostics).
    ticker = used

    last_close = float(closes.iloc[-1])
    last_date = closes.index[-1].date()

    # 1Y change
    cutoff_1y = pd.Timestamp(_months_ago(12))
    closes_1y = closes[closes.index >= cutoff_1y]
    first_close = float(closes_1y.iloc[0]) if not closes_1y.empty \
        else float(closes.iloc[0])
    pct_1y = ((last_close - first_close) / first_close * 100.0
              if first_close > 0 else None)
    if pct_1y is not None and pct_1y > max_1y_runup:
        return {"_drop": "runup_%.0f" % pct_1y}

    # Market cap (skipped entirely when apply_mcap=False)
    if apply_mcap:
        mcap_cr = _get_market_cap_cr(yf, ticker, last_close=last_close)
        if mcap_cr is None:
            return {"_drop": "no_mcap"}
        if not (mcap_min <= mcap_cr <= mcap_max):
            return {"_drop": "mcap_%.0f" % mcap_cr}
    else:
        mcap_cr = _get_market_cap_cr(yf, ticker, last_close=last_close)

    # Period highs / pct down (only keep rows within band)
    periods = {}
    for months, label in PERIODS:
        cutoff = pd.Timestamp(_months_ago(months))
        window_high = highs[highs.index >= cutoff]
        if window_high.empty:
            periods[label] = None
            continue
        hi = float(window_high.max())
        hi_date = window_high.idxmax().date()
        if hi <= 0:
            periods[label] = None
            continue
        pct = (last_close - hi) / hi * 100.0
        if not (-max_pct <= pct <= -min_pct):
            periods[label] = None
            continue
        periods[label] = {
            "Symbol": symbol,
            "Name": name,
            "Yahoo": ticker,
            "Mcap (Cr)": round(mcap_cr, 1) if mcap_cr is not None else None,
            "1Y %": round(pct_1y, 2) if pct_1y is not None else None,
            "Last Close": round(last_close, 2),
            "Last Date": last_date,
            "%s High" % label: round(hi, 2),
            "%s High Date" % label: hi_date,
            "Pct From High": round(pct, 2),
        }
    return {"_drop": None, "periods": periods}


# --- per-universe screen ----------------------------------------------------

def screen_universe(yf, name, tickers, fno_set, mcap_min, mcap_max,
                    max_1y_runup, min_pct, max_pct, workers,
                    apply_fno=True, apply_mcap=True,
                    bse_symbol_map=None, max_retries=3):
    n_initial = len(tickers)
    print("\n--- %s -------------------------------" % name)
    print("  Initial universe       : %d" % n_initial)

    bse_symbol_map = bse_symbol_map or {}

    def _fallback_for(yahoo_ticker, sym):
        # NSE symbol -> .BO via BSE scrip_id lookup. Only meaningful for
        # .NS tickers; .BO tickers have no useful fallback.
        if not yahoo_ticker.endswith(".NS"):
            return None
        return bse_symbol_map.get(sym.upper())

    # F&O removal (optional)
    if apply_fno and fno_set:
        kept = [t for t in tickers if t[1].upper() not in fno_set]
        print("  After F&O removal      : %d  (-%d)" %
              (len(kept), n_initial - len(kept)))
        tickers = kept
    elif not apply_fno:
        print("  F&O filter             : skipped")

    start_date = _months_ago(max(p[0] for p in PERIODS) + 1)
    period_rows = {label: [] for _, label in PERIODS}
    counts = {"pass": 0, "runup": 0, "mcap_drop": 0, "no_mcap": 0,
              "errors": 0}
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(_analyze_one, yf, t, s, nm, start_date,
                      mcap_min, mcap_max, max_1y_runup,
                      min_pct, max_pct, apply_mcap,
                      _fallback_for(t, s), max_retries): (t, s, nm)
            for (t, s, nm) in tickers
        }
        for fut in as_completed(futs):
            done += 1
            if done % 200 == 0 or done == len(futs):
                print("    %d/%d (%.1fs)"
                      % (done, len(futs), time.time() - t0))
            try:
                res = fut.result()
            except Exception:
                counts["errors"] += 1
                continue
            if not res:
                counts["errors"] += 1
                continue
            drop = res.get("_drop")
            if drop is None:
                counts["pass"] += 1
                for label, row in (res.get("periods") or {}).items():
                    if row:
                        period_rows[label].append(row)
            elif drop.startswith("runup_"):
                counts["runup"] += 1
            elif drop == "no_mcap":
                counts["no_mcap"] += 1
            elif drop.startswith("mcap_"):
                counts["mcap_drop"] += 1
            else:
                counts["errors"] += 1

    print("  After 1Y runup <=%g%%   : -%d dropped"
          % (max_1y_runup, counts["runup"]))
    if apply_mcap:
        print("  After mcap %d-%d Cr  : %d kept  (-%d out of band, "
              "-%d no-mcap)" % (mcap_min, mcap_max, counts["pass"],
                                counts["mcap_drop"], counts["no_mcap"]))
    else:
        print("  Mcap filter            : skipped  (%d passed runup)"
              % counts["pass"])
    print("  Errors / no-data       : %d" % counts["errors"])

    # Build per-period DataFrames
    period_dfs = {}
    period_syms = {}
    for _, label in PERIODS:
        rows = period_rows[label]
        if not rows:
            period_dfs[label] = pd.DataFrame()
            period_syms[label] = set()
        else:
            df = pd.DataFrame(rows).sort_values("Pct From High")
            period_dfs[label] = df
            period_syms[label] = set(df["Symbol"].tolist())
        print("  %s hits (down %g-%g%%)  : %d"
              % (label, min_pct, max_pct, len(period_dfs[label])))

    return period_dfs, period_syms


# --- common-set sheet builders ----------------------------------------------

def _build_common(period_dfs, period_syms, labels):
    """Return DataFrame of stocks present in ALL given period sets,
    with Pct-from-High columns for each requested period."""
    if not all(lbl in period_syms for lbl in labels):
        return pd.DataFrame()
    common = set.intersection(*[period_syms[lbl] for lbl in labels])
    if not common:
        return pd.DataFrame()

    # Use first label's DF as base; merge pct cols from others
    base_label = labels[0]
    base = period_dfs[base_label]
    base = base[base["Symbol"].isin(common)].copy()
    base = base.rename(columns={
        "Pct From High": "Pct %s" % base_label,
        "%s High" % base_label: "%s High" % base_label,
        "%s High Date" % base_label: "%s High Date" % base_label,
    })
    keep_base = ["Symbol", "Name", "Yahoo", "Mcap (Cr)", "1Y %",
                 "Last Close", "Last Date",
                 "%s High" % base_label, "%s High Date" % base_label,
                 "Pct %s" % base_label]
    base = base[[c for c in keep_base if c in base.columns]]

    for lbl in labels[1:]:
        df = period_dfs[lbl]
        df = df[df["Symbol"].isin(common)][
            ["Symbol", "%s High" % lbl, "%s High Date" % lbl,
             "Pct From High"]
        ].rename(columns={"Pct From High": "Pct %s" % lbl})
        base = base.merge(df, on="Symbol", how="left")

    pct_cols = ["Pct %s" % lbl for lbl in labels]
    base["Worst Pct"] = base[pct_cols].min(axis=1)
    base = base.sort_values("Worst Pct").drop(columns=["Worst Pct"])
    return base


# --- main runner ------------------------------------------------------------

def run(out_dir, skip, min_pct, max_pct, max_symbols, workers,
        output_prefix):
    if min_pct < 0 or max_pct <= min_pct:
        sys.exit("Invalid --min/--max range.")

    try:
        import yfinance as yf
    except ImportError:
        sys.exit("Requires: pip install yfinance pandas openpyxl")

    _ = out_dir  # not used for input anymore; kept for output path

    universes = []
    if "nse" not in skip:
        try:
            universes.append(("NSE", fetch_nse_equity_universe()))
        except Exception as e:
            print("  FAIL fetch NSE: %s" % e)
    if "nse_sme" not in skip:
        try:
            universes.append(("NSE_SME", fetch_nse_sme_universe()))
        except Exception as e:
            print("  FAIL fetch NSE_SME: %s" % e)
    if "bse_sme" not in skip:
        try:
            universes.append(("BSE_SME", fetch_bse_sme_universe()))
        except Exception as e:
            print("  FAIL fetch BSE_SME: %s" % e)
    if max_symbols > 0:
        universes = [(n, t[:max_symbols]) for n, t in universes]

    print("=" * 72)
    print("  MULTI-UNIVERSE PCT-DOWN SCREENER")
    print("  Band: %.1f%% - %.1f%% from high  |  Drop 1Y runup > %.0f%%"
          % (min_pct, max_pct, MAX_1Y_RUNUP_PCT))
    print("  Mcap band (when applied): %d - %d Cr"
          % (MCAP_MIN_CR, MCAP_MAX_CR))
    print("=" * 72)
    print_filter_matrix()
    print("=" * 72)

    print("-> Loading F&O underlyings list ...")
    fno_set = load_fno_symbols()
    print("   F&O symbols: %d" % len(fno_set))

    # Build NSE-symbol -> .BO fallback map once (covers NSE + NSE_SME).
    bse_symbol_map = fetch_bse_full_symbol_map()

    all_sheets = {}  # ordered
    for uni_name, tickers in universes:
        cfg = FILTER_MATRIX.get(
            uni_name,
            {"apply_fno": True, "apply_mcap": True, "max_retries": 3})
        try:
            period_dfs, period_syms = screen_universe(
                yf, uni_name, tickers, fno_set,
                MCAP_MIN_CR, MCAP_MAX_CR, MAX_1Y_RUNUP_PCT,
                min_pct, max_pct, workers,
                apply_fno=cfg["apply_fno"],
                apply_mcap=cfg["apply_mcap"],
                bse_symbol_map=bse_symbol_map,
                max_retries=cfg.get("max_retries", 3),
            )
        except Exception as e:
            print("  FAIL %s: %s" % (uni_name, e))
            continue

        for _, label in PERIODS:
            sheet = ("%s %s" % (uni_name, label))[:31]
            all_sheets[sheet] = period_dfs.get(label, pd.DataFrame())

        common36 = _build_common(period_dfs, period_syms, ["3M", "6M"])
        common369 = _build_common(period_dfs, period_syms,
                                  ["3M", "6M", "9M"])
        all_sheets[("%s Common 3M+6M" % uni_name)[:31]] = common36
        all_sheets[("%s Common 3M+6M+9M" % uni_name)[:31]] = common369
        print("  Common 3M+6M           : %d" % len(common36))
        print("  Common 3M+6M+9M        : %d" % len(common369))

    prefix = output_prefix or os.path.join(out_dir, "multi_pct_down")
    out_xlsx = "%s.xlsx" % prefix

    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as w:
        wrote = 0
        for sheet, df in all_sheets.items():
            if df is None or df.empty:
                pd.DataFrame({"Note": ["No matches"]}).to_excel(
                    w, sheet_name=sheet, index=False)
            else:
                df.to_excel(w, sheet_name=sheet, index=False)
                wrote += 1

    print("\n" + "=" * 72)
    print("  Written: %s  (%d sheets, %d with hits)"
          % (out_xlsx, len(all_sheets), wrote))
    print("=" * 72)
    return out_xlsx


def main():
    ap = argparse.ArgumentParser(
        description="Multi-universe pct-down screener (NSE / NSE-SME / "
                    "BSE-SME).")
    ap.add_argument("--out", default=SCRIPT_DIR,
                    help="Output directory (default: script dir)")
    ap.add_argument("--skip", nargs="*", default=[],
                    choices=["nse", "nse_sme", "bse_sme"],
                    help="Universes to skip")
    ap.add_argument("--min", dest="min_pct", type=float, default=2.0,
                    help="Min %% down from high (default: 2)")
    ap.add_argument("--max", dest="max_pct", type=float, default=30.0,
                    help="Max %% down from high (default: 30)")
    ap.add_argument("--max-symbols", type=int, default=0,
                    help="Cap symbols per universe (0 = no cap)")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="Parallel download threads (default: %d)" %
                    DEFAULT_WORKERS)
    ap.add_argument("-o", "--output-prefix", default=None,
                    help="Output Excel prefix "
                         "(default: multi_pct_down_<date>)")
    args = ap.parse_args()

    run(
        out_dir=args.out,
        skip=set(args.skip),
        min_pct=args.min_pct,
        max_pct=args.max_pct,
        max_symbols=args.max_symbols,
        workers=args.workers,
        output_prefix=args.output_prefix,
    )


if __name__ == "__main__":
    main()
