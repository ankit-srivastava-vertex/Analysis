"""
Percentage Down Screener
========================
Scans NSE-listed equities for stocks trading ≥15% below their
3-month, 6-month, 9-month, and 12-month highs.

Market-cap filter: ₹300 – ₹20,000 Cr (small & mid cap).

Output (for each run):
  - Excel workbook  : 4 period sheets + 1 "Common" sheet

Usage:
  python percentage_down.py                 # default 15% threshold
  python percentage_down.py -o report       # custom output prefix
  python percentage_down.py -t 20           # 20% threshold instead of 15%
"""

import os
import datetime
import time
import argparse
import warnings
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")          # suppress jugaad_data / numpy noise

from jugaad_data.nse import stock_df as jugaad_stock_df
from nsepython import nsefetch

try:
    import yfinance as yf
    _HAS_YFINANCE = True
except ImportError:
    _HAS_YFINANCE = False


# ─── Config ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TODAY = datetime.date.today()
DEFAULT_PCT = 15.0
MCAP_MIN = 300       # ₹ Crore
MCAP_MAX = 20000     # ₹ Crore
MAX_WORKERS = 5      # parallel history-fetch threads
PERIODS = [(3, "3M"), (6, "6M"), (9, "9M"), (12, "12M")]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _months_ago(n):
    """Date *n* calendar months before today."""
    y, m = TODAY.year, TODAY.month - n
    while m <= 0:
        m += 12
        y -= 1
    return datetime.date(y, m, min(TODAY.day, 28))


# ─── Data fetching ───────────────────────────────────────────────────────────

def fetch_universe():
    """Return list of stock dicts from NIFTY TOTAL MKT (~750 stocks).

    Primary: NSE API via nsepython.
    Fallback: niftyindices.com CSV + yfinance for prices & market cap.
    """
    # ── Primary: NSE API ──
    try:
        url = ("https://www.nseindia.com/api/equity-stockIndices"
               "?index=NIFTY%20TOTAL%20MKT")
        data = nsefetch(url)
        raw = data.get("data", [])
        out = []
        for s in raw:
            sym = s.get("symbol", "")
            if sym.startswith("NIFTY") or not sym:
                continue
            meta = s.get("meta") or {}
            s["_name"] = meta.get("companyName", sym)
            s["_industry"] = meta.get("industry", "")
            out.append(s)
        if out:
            return out
        print("  NSE API returned 0 stocks; trying fallback ...")
    except Exception as e:
        print("  NSE API failed (%s); trying fallback ..." % e)

    # ── Fallback: niftyindices.com CSV + yfinance ──
    return _fetch_universe_fallback()


def _fetch_universe_fallback():
    """Fetch NIFTY TOTAL MKT constituents via niftyindices.com CSV,
    then enrich with price & market-cap data from Yahoo Finance."""
    if not _HAS_YFINANCE:
        raise RuntimeError("yfinance not installed — cannot use fallback")

    import requests as _req
    from io import StringIO

    csv_url = ("https://www.niftyindices.com/IndexConstituent/"
               "ind_niftytotalmarket_list.csv")
    r = _req.get(csv_url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    }, timeout=15)
    r.raise_for_status()

    csv_df = pd.read_csv(StringIO(r.text))
    sym_col = next(c for c in csv_df.columns if "symbol" in c.lower())
    name_col = next((c for c in csv_df.columns if "company" in c.lower()), None)
    ind_col = next((c for c in csv_df.columns if "industry" in c.lower()), None)

    symbols = csv_df[sym_col].dropna().str.strip().unique().tolist()
    print("  niftyindices.com: %d constituents" % len(symbols))

    # Name / industry lookup from CSV
    csv_info = {}
    for _, row in csv_df.iterrows():
        sym = str(row[sym_col]).strip()
        csv_info[sym] = {
            "name": str(row[name_col]).strip() if name_col and pd.notna(row.get(name_col)) else sym,
            "industry": str(row[ind_col]).strip() if ind_col and pd.notna(row.get(ind_col)) else "",
        }

    # Batch-download 1Y price data via yfinance
    nse_tickers = [s + ".NS" for s in symbols]
    print("  Downloading 1Y prices via yfinance (%d tickers) ..." % len(nse_tickers))
    price_data = yf.download(nse_tickers, period="1y", progress=False, threads=True)
    multi = isinstance(price_data.columns, pd.MultiIndex)

    # Fetch market caps in parallel
    print("  Fetching market-cap data via yfinance ...")
    mcap_map = {}

    def _get_mcap(sym):
        try:
            mc = getattr(yf.Ticker(sym + ".NS").fast_info, "market_cap", 0)
            return sym, mc or 0
        except Exception:
            return sym, 0

    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_get_mcap, s): s for s in symbols}
        done = 0
        for fut in as_completed(futs):
            sym, mc = fut.result()
            mcap_map[sym] = mc
            done += 1
            if done % 100 == 0:
                print("    mcap: %d / %d ..." % (done, len(symbols)))

    # Build stock dicts (same shape as NSE API output)
    out = []
    for sym in symbols:
        nse_sym = sym + ".NS"
        try:
            if multi:
                close = price_data["Close"][nse_sym]
                high = price_data["High"][nse_sym]
            else:
                close = price_data["Close"]
                high = price_data["High"]
            close = close.dropna()
            if close.empty:
                continue
            last_price = float(close.iloc[-1])
            year_high = float(high.max())
            pct_365 = ((last_price / float(close.iloc[0])) - 1) * 100
        except Exception:
            continue

        info = csv_info.get(sym, {"name": sym, "industry": ""})
        out.append({
            "symbol": sym,
            "_name": info["name"],
            "_industry": info["industry"],
            "lastPrice": last_price,
            "yearHigh": year_high,
            "ffmc": mcap_map.get(sym, 0),  # raw INR; apply_mcap_filter auto-detects
            "perChange365d": pct_365,
            "meta": {},
        })

    print("  Fallback: %d stocks with valid data" % len(out))
    return out


def apply_mcap_filter(stocks):
    """Keep stocks with free-float market cap in [MCAP_MIN, MCAP_MAX] Cr.

    Auto-detects the unit of the ``ffmc`` field returned by NSE.
    """
    for s in stocks:
        v = s.get("ffmc") or 0
        if isinstance(v, str):
            v = float(v.replace(",", ""))
        s["_ffmc_raw"] = float(v)

    valid = [s["_ffmc_raw"] for s in stocks if s["_ffmc_raw"] > 0]
    if not valid:
        print("  No ffmc data; skipping market-cap filter")
        for s in stocks:
            s["_ffmc_cr"] = 0
        return stocks

    median = sorted(valid)[len(valid) // 2]
    print("  FFMC stats: min=%.0f  median=%.0f  max=%.0f" % (
        min(valid), median, max(valid)))

    # Auto-detect unit — median of ~750 stocks should be a midcap (~5k–30k Cr)
    # 1 Crore = 10^7 rupees.
    if median > 1_000_000_000:
        divisor = 1_00_00_000  # 10^7  — raw is in ₹ Rupees
        print("  Unit detected: ₹ Rupees → dividing by 1e7 for Crores")
    elif median > 500_000:
        divisor = 100          # raw is in ₹ Lakhs
        print("  Unit detected: ₹ Lakhs → dividing by 100 for Crores")
    else:
        divisor = 1            # raw already in Crores
        print("  Unit detected: ₹ Crores")

    for s in stocks:
        s["_ffmc_cr"] = s["_ffmc_raw"] / divisor

    filtered = [s for s in stocks
                if MCAP_MIN <= s["_ffmc_cr"] <= MCAP_MAX]
    return filtered


def pre_filter_by_yearhigh(stocks, pct):
    """Quick pre-filter: if CMP ≥ (1-pct/100) × yearHigh the stock
    cannot be *pct*% below any shorter-period high → skip it.
    """
    threshold_factor = 1 - pct / 100
    keep = []
    for s in stocks:
        cmp = s.get("lastPrice") or 0
        yh  = s.get("yearHigh") or 0
        if cmp <= 0 or yh <= 0:
            continue
        if cmp < yh * threshold_factor:
            keep.append(s)
    return keep


def _fetch_one(symbol):
    """Fetch 12-month daily High for *symbol*. Returns (symbol, df).

    Primary: jugaad-data.  Fallback: yfinance.
    """
    start = _months_ago(12)

    # ── Primary: jugaad-data ──
    try:
        df = jugaad_stock_df(symbol=symbol, from_date=start,
                             to_date=TODAY, series="EQ")
        if df is not None and not df.empty:
            df = df.rename(columns={"DATE": "Date", "HIGH": "High",
                                    "CLOSE": "Close"})
            df["Date"] = pd.to_datetime(df["Date"]).dt.date
            df = df[["Date", "High", "Close"]].sort_values("Date").reset_index(drop=True)

            # Remove duplicate-date outliers (mixed series like BE/EQ)
            if df["Date"].duplicated().any():
                med = df["Close"].median()
                df = df[df["Close"] < med * 2.5]
                df = df.drop_duplicates(subset="Date", keep="last").reset_index(drop=True)

            # Detect splits/bonus: if Close drops >45% day-over-day, trim
            if len(df) > 1:
                pct_chg = df["Close"].pct_change()
                split_mask = pct_chg < -0.45
                if split_mask.any():
                    last_split_idx = split_mask[split_mask].index[-1]
                    df = df.iloc[last_split_idx:].reset_index(drop=True)
            return symbol, df[["Date", "High"]]
    except Exception:
        pass

    # ── Fallback: yfinance (auto-adjusts for splits) ──
    if _HAS_YFINANCE:
        try:
            yf_df = yf.download(
                symbol + ".NS", start=str(start), end=str(TODAY),
                progress=False,
            )
            if yf_df is not None and not yf_df.empty:
                yf_df = yf_df.reset_index()
                if isinstance(yf_df.columns, pd.MultiIndex):
                    yf_df.columns = yf_df.columns.droplevel(1)
                yf_df["Date"] = pd.to_datetime(yf_df["Date"]).dt.date
                yf_df = yf_df[["Date", "High"]].sort_values("Date").reset_index(drop=True)
                return symbol, yf_df
        except Exception:
            pass

    return symbol, pd.DataFrame()


def fetch_histories(symbols):
    """Parallel-fetch 12-month history for every symbol in *symbols*."""
    results = {}
    n = len(symbols)
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(_fetch_one, s): s for s in symbols}
        for fut in as_completed(futs):
            sym, df = fut.result()
            results[sym] = df
            done += 1
            if done % 25 == 0 or done == n:
                print("    %d / %d ..." % (done, n))
    return results


# ─── Analysis ────────────────────────────────────────────────────────────────

def compute_period_data(stocks_info, history_map):
    """Build a master DataFrame with per-period highs and % down.

    Columns: Symbol, Name, Exchange, Current Price, FFMC (Cr),
             High_3M, PctDown_3M, High_6M, PctDown_6M, …
    """
    cutoffs = {label: _months_ago(m) for m, label in PERIODS}
    rows = []
    for s in stocks_info:
        sym = s["symbol"]
        df = history_map.get(sym)
        if df is None or df.empty:
            continue
        cmp = s.get("lastPrice") or 0
        if cmp <= 0:
            continue

        row = {
            "Symbol": sym,
            "Name": s.get("_name", sym),
            "Industry": s.get("_industry", ""),
            "Exchange": "NSE",
            "Current Price": round(cmp, 2),
            "FFMC (Cr)": round(s.get("_ffmc_cr", 0)),
        }
        for _, label in PERIODS:
            period_df = df[df["Date"] >= cutoffs[label]]
            if period_df.empty:
                row["High_%s" % label] = None
                row["PctDown_%s" % label] = None
            else:
                hi = period_df["High"].max()
                pct = (hi - cmp) / hi * 100 if hi > 0 else 0
                row["High_%s" % label] = round(hi, 2)
                row["PctDown_%s" % label] = round(pct, 2)
        rows.append(row)
    return pd.DataFrame(rows)


PCT_MIN = 1.0   # lower bound of the "down" range


def build_period_table(master, label, threshold):
    """Return stocks with % down in [PCT_MIN, threshold] for one period."""
    pct_col  = "PctDown_%s" % label
    high_col = "High_%s" % label
    mask = (master[pct_col].notna()
            & (master[pct_col] >= PCT_MIN)
            & (master[pct_col] <= threshold))
    df = (master[mask]
          [["Symbol", "Name", "Industry", "Exchange", "Current Price",
            high_col, pct_col, "FFMC (Cr)"]]
          .copy())
    df = df.rename(columns={high_col: "%s High" % label,
                            pct_col: "%% Down from %s High" % label})
    df = df.sort_values("%% Down from %s High" % label,
                        ascending=False).reset_index(drop=True)
    return df


def build_common_table(tables, min_count=3):
    """Stocks appearing in *min_count* or more period tables."""
    from collections import Counter
    counter = Counter()
    info = {}
    for label, df in tables.items():
        for _, row in df.iterrows():
            sym = row["Symbol"]
            counter[sym] += 1
            if sym not in info:
                info[sym] = {"Symbol": sym, "Name": row["Name"],
                             "Industry": row.get("Industry", ""),
                             "Exchange": row["Exchange"],
                             "Current Price": row["Current Price"],
                             "FFMC (Cr)": row.get("FFMC (Cr)", "")}

    common = [sym for sym, c in counter.items() if c >= min_count]
    if not common:
        return pd.DataFrame()

    rows = []
    for sym in common:
        r = info[sym].copy()
        r["Tables"] = counter[sym]
        present = []
        for label, df in tables.items():
            match = df[df["Symbol"] == sym]
            if not match.empty:
                present.append(label)
                pct_col = [c for c in match.columns if c.startswith("% Down")][0]
                high_col = [c for c in match.columns if "High" in c][0]
                r[high_col] = match.iloc[0][high_col]
                r["Down_%s" % label] = match.iloc[0][pct_col]
        r["In"] = ", ".join(present)
        rows.append(r)

    return (pd.DataFrame(rows)
            .sort_values("Tables", ascending=False)
            .reset_index(drop=True))


def build_all_periods_table(master, threshold):
    """Stocks that are 1%–threshold% down in ALL 4 periods simultaneously."""
    mask = pd.Series(True, index=master.index)
    for _, label in PERIODS:
        pct_col = "PctDown_%s" % label
        mask = mask & master[pct_col].notna() \
                    & (master[pct_col] >= PCT_MIN) \
                    & (master[pct_col] <= threshold)
    df = master[mask].copy()
    if df.empty:
        return pd.DataFrame()

    # Build a clean output with all period columns
    cols = ["Symbol", "Name", "Industry", "Exchange", "Current Price",
            "FFMC (Cr)"]
    for _, label in PERIODS:
        cols += ["High_%s" % label, "PctDown_%s" % label]
    df = df[cols].copy()
    rename = {}
    for _, label in PERIODS:
        rename["High_%s" % label] = "%s High" % label
        rename["PctDown_%s" % label] = "%% Down %s" % label
    df = df.rename(columns=rename)
    # Sort by avg % down descending
    pct_cols = ["%% Down %s" % label for _, label in PERIODS]
    df["Avg %% Down"] = df[pct_cols].mean(axis=1).round(2)
    df = df.sort_values("Avg %% Down", ascending=False).reset_index(drop=True)
    return df


# ─── Output ──────────────────────────────────────────────────────────────────

def save_excel(tables, common_df, all_periods_df, path, threshold):
    """Write all tables into one Excel workbook."""
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        for label, df in tables.items():
            sheet = "%s Down %g%%-%g%%" % (label, PCT_MIN, threshold)
            df.to_excel(w, sheet_name=sheet[:31], index=False)
        if not common_df.empty:
            common_df.to_excel(w, sheet_name="Common (3+ tables)", index=False)
        if not all_periods_df.empty:
            all_periods_df.to_excel(w, sheet_name="All 4 Periods", index=False)
    print("  Excel saved: %s" % path)


# ─── Main ────────────────────────────────────────────────────────────────────

def run(output_prefix=None, threshold=DEFAULT_PCT):
    print("=" * 60)
    print("Percentage Down Screener  (%.0f%%–%.0f%% below period highs)" % (PCT_MIN, threshold))
    print("=" * 60)

    # 1. Universe
    print("\n[1] Fetching NIFTY TOTAL MKT universe from NSE ...")
    all_stocks = fetch_universe()
    print("  Stocks in universe: %d" % len(all_stocks))

    # 2. Market-cap filter
    print("\n[2] Applying market-cap filter (₹%d – ₹%d Cr) ..." % (MCAP_MIN, MCAP_MAX))
    mcap_stocks = apply_mcap_filter(all_stocks)
    print("  Stocks after mcap filter: %d" % len(mcap_stocks))
    if not mcap_stocks:
        print("  WARNING: mcap filter returned 0 stocks — using full universe")
        mcap_stocks = all_stocks
        for s in mcap_stocks:
            s["_ffmc_cr"] = s.get("_ffmc_raw", 0)

    # 2b. Remove F&O stocks and 1Y gainers >70%
    before = len(mcap_stocks)
    mcap_stocks = [s for s in mcap_stocks
                   if not (s.get("meta") or {}).get("isFNOSec", False)]
    print("  Removed F&O stocks: %d → %d" % (before, len(mcap_stocks)))

    before = len(mcap_stocks)
    mcap_stocks = [s for s in mcap_stocks
                   if (s.get("perChange365d") or 0) <= 70]
    print("  Removed 1Y change >70%%: %d → %d" % (before, len(mcap_stocks)))

    # 3. Quick pre-filter via yearHigh
    print("\n[3] Pre-filtering by 52-week high ...")
    candidates = pre_filter_by_yearhigh(mcap_stocks, PCT_MIN)
    print("  Candidates (CMP < %.0f%% of yearHigh): %d" % (
        100 - PCT_MIN, len(candidates)))

    if not candidates:
        print("\nNo stocks meet the criteria. Done.")
        return

    # 4. Fetch 12-month history
    symbols = [s["symbol"] for s in candidates]
    print("\n[4] Fetching 12-month price history for %d stocks ..." % len(symbols))
    t0 = time.time()
    history = fetch_histories(symbols)
    ok = sum(1 for d in history.values() if not d.empty)
    print("  Fetched: %d / %d  (%.0f s)" % (ok, len(symbols), time.time() - t0))

    # 5. Compute period highs
    print("\n[5] Computing period highs & %% down ...")
    master = compute_period_data(candidates, history)
    print("  Stocks with valid data: %d" % len(master))

    # 6. Build tables
    tables = {}
    for _, label in PERIODS:
        tbl = build_period_table(master, label, threshold)
        tables[label] = tbl
        print("  %s: %d stocks" % (label, len(tbl)))

    common = build_common_table(tables)
    print("  Common (3+ tables): %d stocks" % len(common))

    # Build All-4-Periods sheet
    all4 = build_all_periods_table(master, threshold)
    print("  All 4 periods (1%%–%.0f%% in 3M+6M+9M+12M): %d stocks" % (
        threshold, len(all4)))

    # 7. Save output
    if output_prefix is None:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        output_prefix = os.path.join(SCRIPT_DIR, "PctDown_%s" % ts)

    excel_path = output_prefix + ".xlsx"

    print("\n[6] Saving output ...")
    save_excel(tables, common, all4, excel_path, threshold)

    # Summary
    print("\n" + "=" * 60)
    print("DONE — %s" % TODAY.strftime("%d-%b-%Y"))
    print("=" * 60)
    for label, tbl in tables.items():
        print("  %-4s : %3d stocks %.0f%%–%.0f%% down" % (
            label, len(tbl), PCT_MIN, threshold))
    print("  Common: %d stocks (in 3+ tables)" % len(common))
    print("  All 4 : %d stocks" % len(all4))
    if not common.empty:
        print("\n  Top common stocks:")
        for _, r in common.head(10).iterrows():
            print("    %-20s  CMP ₹%8.2f   in %s" % (
                r["Symbol"], r["Current Price"], r["In"]))
    return tables, common, all4, excel_path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Percentage Down Screener")
    parser.add_argument("-o", "--output", help="Output filename prefix")
    parser.add_argument("-t", "--threshold", type=float, default=DEFAULT_PCT,
                        help="Min %% down (default: %.0f)" % DEFAULT_PCT)
    args = parser.parse_args()
    run(output_prefix=args.output, threshold=args.threshold)
