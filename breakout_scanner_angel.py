"""
Breakout Scanner v4.4 (Angel One edition) — Pre-Breakout Setup Detector
========================================================================

Breakout scanner that identifies stocks forming horizontal resistance bases
and approaching breakout levels. Runs the full pipeline end-to-end:
universe generation → OHLCV download → pattern detection → scoring →
Excel + chart output.

ARCHITECTURE (v4.4)
-------------------
A single universe is scanned:

  Universe — Screener.in:
    Logs into screener.in and fetches a saved screen URL
    (default: https://www.screener.in/screens/2877406/52w-15/).
    Resolves screener slugs to .NS/.BO tickers for Angel One.
    Raw screener data is preserved as Sheet 1.
    If screener.in is unavailable, the universe is rebuilt from Angel
    batched quotes instead (see UNIVERSE below) and Sheet 1 records that.

The breakout scan detects:
  - Horizontal resistance (fractal pivots clustered into bands)
  - Base quality (duration, range, higher lows)
  - Volume contraction (VCR, VDU)
  - Patterns: multi-touch, VCP, W-bottom, cup & handle
  - Relative strength vs Nifty 500 (rising RS line over 50 sessions)
  - Risk/reward plan (stop, target, R:R ratio)

SCORING (v4.3)
--------------
  Composite score 0-100 from: base_quality, vcr, vdu, proximity,
  trend, rs. Hard gates (stage2 uptrend, not extended, recent R-test,
  base range, RS rising) eliminate weak setups before scoring.

  HIGH-CONVICTION requires ALL of:
    - One structural pattern (multi_touch ≥2 OR vcp OR w_pattern OR cup_handle)
    - Close > 50 DMA (stage2)
    - Not extended (no vertical chase)
    - Distance to R: -5% to +4%
    - Recent R-test in last 50 sessions
    - Base range ≤ 40%
    - RS rising over 50 sessions

OUTPUT (4-sheet Excel)
----------------------
  breakout_watchlist.xlsx:
    Sheet 1: "Screener Data"      — Raw screener.in stock list, or the Angel
                                    fallback universe when screener.in failed
    Sheet 2: "Screener Breakouts" — Breakout candidates from the screener universe
    Sheet 3: "Energy Expansion"   — Observational volume/range tag
    Sheet 4: "MinerviniTrend"     — Names passing all 8 Trend Template criteria.
                                    Scored over a wide NSE+BSE universe built
                                    from the ohlcv_cache + index_constituents,
                                    independent of the screener universe.

  Logs:   Output/logs/logs_breakout_scanner_angel_v35_<timestamp>.txt

DATA SOURCE (OHLCV)
-------------------
  Angel One SmartAPI (via angel_client.py):
    - Auth: .env with ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET
    - Daily candles via getCandleData()
    - Full coverage: NSE main + NSE Emerge (SME) + BSE main + BSE SME
    - Scrip master (~25 MB) cached weekly

UNIVERSE
--------
  Primary:  a screener.in screen (default "52w-15"), which needs a login.
  Fallback: if screener.in fails for any reason, the universe is rebuilt from
            Angel batched quotes (getMarketData, 50 symbols per request),
            keeping names trading within --off-high-pct of their 52-week
            high. Angel's 52-week levels are split/bonus adjusted. ETF and
            fund units are removed using the NSE bhavcopy ISIN prefix, since
            they share the EQ series with ordinary shares.
            Coverage is all of NSE cash plus the BSE Emerge (SME) board,
            identified by the BSE bhavcopy scrip group; pass --no-bse-sme to
            drop it. The rest of BSE is skipped because it is overwhelmingly
            dual listings of NSE names plus illiquid scrips that the scanner
            has no turnover floor to reject.
            SME names must also clear --sme-min-turnover (median 20d traded
            value, default ₹0.5cr); NSE names are never turnover-filtered, so
            that part of the universe is unaffected.

PREREQS
-------
  - Angel One demat account + SmartAPI app (free)
  - TOTP enabled → ANGEL_TOTP_SECRET (base32)
  - screener.in account → SCREENER_USER / SCREENER_PASS in .env
    (optional: without it the scanner falls back to the Angel universe)
  - pip: pyotp python-dotenv pandas numpy openpyxl plotly

USAGE
-----
  python3 breakout_scanner_angel.py                     # full scan
  python3 breakout_scanner_angel.py --max 50            # cap universe to 50
  python3 breakout_scanner_angel.py --high-conviction   # only HC picks in output
  python3 breakout_scanner_angel.py --symbols-csv f.csv # custom universe mode
  python3 breakout_scanner_angel.py --min-score 70      # raise score threshold

"""

import os
import sys
import re
import io
import csv
import glob
import json
import math
import zipfile
import hashlib
import argparse
import datetime
import warnings
import urllib.request
import urllib.parse
import http.cookiejar
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv

import screener_client

warnings.filterwarnings("ignore")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, os.pardir, "Output")
TODAY = datetime.date.today()
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


class _Tee:
    """Duplicate writes to both a file and the original stream."""
    def __init__(self, stream, filepath):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        self._file = open(filepath, "w")
        self._stream = stream

    def write(self, data):
        self._stream.write(data)
        self._file.write(data)

    def flush(self):
        self._stream.flush()
        self._file.flush()

    def close(self):
        self._file.close()

# Screener.in screen URL — universe source
SCREENER_URL_DEFAULT = "https://www.screener.in/screens/2877406/52w-15/"

NIFTY500_BENCH = "^CRSLDX"  # Nifty 500 index (handled via Angel INDEX_OVERRIDES)


def _yahoo_to_tv(ticker: str) -> str:
    """Convert Yahoo-style ticker to TradingView format.
    RELIANCE.NS -> NSE:RELIANCE, 543745.BO -> BSE:543745"""
    if ticker.endswith(".NS"):
        return f"NSE:{ticker[:-3]}"
    elif ticker.endswith(".BO"):
        return f"BSE:{ticker[:-3]}"
    return ticker

# ─── Defaults / thresholds (v4.1) ───────────────────────────────────────────
LOOKBACK_DAYS = 252        # v4.1: only consider last ~1 year of daily history
RES_LOOKBACK_DAYS = 252    # v4.1: pivot/resistance search restricted to 1y
MIN_HISTORY_DAYS = 45      # v4.3: min trading days required to evaluate a name
BASE_MIN_DAYS = 20         # pattern matters more than duration
BASE_MAX_DAYS = 180        # v4.1: cap base length at ~180 calendar days
RES_BAND_PCT = 0.050       # touches counted within +/- 5% of resistance
PROXIMITY_MAX_PCT = 0.04   # distance to resistance upper bound = +4%
PROXIMITY_MIN_PCT = -0.05  # distance to resistance lower bound = -5%
MIN_TOUCHES = 2            # detect_resistance qualification floor
HC_MULTITOUCH_MIN = 2      # v4.1: pattern A requires >= 2 touches
MAX_BASE_RANGE_PCT = 0.40  # reject any base wider than 40%
RECENT_R_TEST_LOOKBACK = 50  # 50 sessions for recent resistance test
RS_RISING_LOOKBACK = 50    # v4.4: rising RS-line over last 50 sessions
MIN_AVG_VOL = 0            # v4.3: liquidity filter disabled
WATCHLIST_MIN_SCORE = 50
TRIGGER_MIN_SCORE = 65
# Observational tag only — never ANDed with the score gates (near-orthogonal).
ENERGY_VCR_MAX = -0.20        # vcr_raw <= this => ATR expanded >=20% into pivot
ENERGY_BASE_RANGE_MIN = 28.0  # base_range_pct floor, in percent

# ─── Minervini Trend Template ────────────────────────────────────────────────
MINERVINI_MIN_BARS       = 252   # a full year of sessions (52-week window)
MINERVINI_MA200_TREND    = 21    # criterion 4: 200-DMA up over ~1 month
MINERVINI_LOW_MIN_PCT    = 25.0  # criterion 6: >= 25% above the 52-week low
MINERVINI_HIGH_MAX_PCT   = 25.0  # criterion 7: within 25% of the 52-week high
MINERVINI_RS_MIN         = 70    # criterion 8: RS rating floor (1-99)
MINERVINI_MAX_STALE_DAYS = 7     # drop names whose newest bar lags the universe
MINERVINI_MIN_PRICE      = 10.0  # rupees; below this a scrip is untradable
MINERVINI_MIN_TURNOVER   = 1e7   # median 20d traded value floor (₹1 crore)
MINERVINI_TURNOVER_DAYS  = 20
# "ideally" preferences — reported via the `ideal` column, never enforced
MINERVINI_RS_IDEAL       = 80
MINERVINI_LOW_IDEAL_PCT  = 100.0
MINERVINI_HIGH_IDEAL_PCT = 15.0


# ─── Screener.in universe fetch ──────────────────────────────────────────────

def _screener_fetch_names(url: str) -> list:
    """Fetch all pages of a screener.in screen, return list of (slug, name).

    Screen results are never served from cache: the universe has to reflect
    today's market, not last week's.
    """
    names = []
    page = 1
    while True:
        page_url = f"{url.rstrip('/')}/?page={page}" if page > 1 else url
        html = screener_client.get(page_url, ttl_hours=0)
        if not html:
            break
        pattern = r'href="/company/([^/]+)/[^"]*"[^>]*>\s*([^<]+?)\s*</a>'
        found = re.findall(pattern, html)
        if not found:
            break
        for sym_slug, name in found:
            names.append((sym_slug.strip().upper(), name.strip()))
        page += 1
        if f"page={page}" not in html and "Next" not in html:
            break
    return names


def fetch_screener_universe(url: str) -> list:
    """Fetch a screener.in screen and resolve names to Angel-compatible tickers.

    Returns a list of yfinance-style tickers (e.g. 'RELIANCE.NS', '543745.BO').
    Resolution strategy:
      1. screener.in URL slugs are usually NSE symbols → try SYM.NS directly
      2. Fall back to Angel scrip master name-match for any unresolved

    Raises SystemExit when screener.in cannot be used at all; main() catches
    that and falls back to the Angel-quote universe.
    """
    if not screener_client.have_credentials():
        print("  ERROR: SCREENER_USER / SCREENER_PASS not set in .env")
        raise SystemExit("Cannot proceed without screener.in login")
    if not screener_client.login_ok():
        print("  ERROR: screener.in login failed")
        raise SystemExit("Cannot proceed without screener.in login")
    print("  screener.in login OK")

    print(f"  Fetching screen: {url}")
    raw = _screener_fetch_names(url)
    if not raw:
        raise SystemExit("No stocks found on screener.in (check URL or visibility)")
    print(f"  Found {len(raw)} stocks on screener.in")

    # screener.in slugs are typically NSE symbols (e.g. RELIANCE, HDFCBANK)
    # Try .NS first; for numeric slugs (BSE scrip codes) try .BO
    tickers = []
    for slug, name in raw:
        if slug.isdigit():
            tickers.append(f"{slug}.BO")
        else:
            tickers.append(f"{slug}.NS")

    tickers = sorted(set(tickers))
    print(f"  Resolved to {len(tickers)} unique tickers")

    # Save to Output/screener_data.xlsx for reference
    out_dir = os.path.join(SCRIPT_DIR, os.pardir, "Output")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "screener_data.xlsx")
    df = pd.DataFrame({"Name": [n for _, n in raw], "Ticker": [
        f"{s}.BO" if s.isdigit() else f"{s}.NS" for s, _ in raw]})
    df.to_excel(out_path, index=False, engine="openpyxl")
    print(f"  Reference saved: {out_path}")

    return tickers


# ─── Fallback universe (Angel batched quotes) ──────────────────────────────

# Mirrors the band of the default screener.in screen ("52w-15").
ANGEL_UNIVERSE_OFF_HIGH_PCT = 0.15
NSE_BHAVCOPY_URL = ("https://nsearchives.nseindia.com/content/cm/"
                    "BhavCopy_NSE_CM_0_0_0_{date}_F_0000.csv.zip")
BSE_BHAVCOPY_URL = ("https://www.bseindia.com/download/BhavCopy/Equity/"
                    "BhavCopy_BSE_CM_0_0_0_{date}_F_0000.CSV")
# BSE scrip groups that make up the SME (Emerge) board.
BSE_SME_SERIES = {"M", "MT", "MS"}
# SME turnover is ~54x thinner than NSE at the median (Rs 0.34cr vs Rs 18.4cr),
# with a tail that trades a few thousand rupees a day. Rs 0.5cr admits the
# genuinely tradeable Emerge names; the Minervini floor of Rs 1cr was measured
# to be too tight here (it cuts SUNITATOOL at Rs 0.78cr).
SME_MIN_TURNOVER = 5e6
SME_TURNOVER_DAYS = 20


def _archive_opener(referer: str = "") -> urllib.request.OpenerDirector:
    """Cookie-aware urllib opener that the exchange archives will answer."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        ("Accept", "*/*"),
    ]
    if referer:
        opener.addheaders.append(("Referer", referer))
    return opener


def _nse_share_symbols() -> Optional[set]:
    """NSE symbols whose ISIN marks them as ordinary shares.

    ETF and fund units trade in the same EQ series as shares and are
    indistinguishable in Angel's scrip master (both carry a blank
    instrumenttype), but their ISIN starts with INF rather than INE/IN9.
    Returns None when the bhavcopy cannot be read, letting the caller build an
    unfiltered universe rather than fail outright.
    """
    opener = _archive_opener()
    try:
        # Best-effort: the archive host serves without a session cookie, and
        # the www host answers 403 to urllib, so a failure here is not fatal.
        opener.open("https://www.nseindia.com/", timeout=20).read()
    except Exception:
        pass

    # The most recent session may be a holiday or not yet published.
    for back in range(1, 8):
        day = TODAY - datetime.timedelta(days=back)
        url = NSE_BHAVCOPY_URL.format(date=day.strftime("%Y%m%d"))
        try:
            blob = opener.open(url, timeout=45).read()
        except Exception:
            continue
        if not blob.startswith(b"PK"):
            continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
            reader = csv.DictReader(
                io.TextIOWrapper(zf.open(zf.namelist()[0])))
            out = {
                row["TckrSymb"].strip().upper()
                for row in reader
                if not str(row.get("ISIN", "")).strip().upper().startswith("INF")
            }
        except Exception:
            continue
        if out:
            print(f"  Share filter: {len(out)} NSE symbols from {day} bhavcopy")
            return out
    return None


def _bse_sme_symbols() -> Optional[set]:
    """BSE SME (Emerge) symbols, taken from the scrip group in the bhavcopy.

    These names list only on BSE, so they are invisible to the NSE sweep that
    builds the rest of the fallback universe. The bhavcopy's SctySrs column is
    the authoritative marker (M / MT / MS); Angel's scrip master carries no
    equivalent field. Fund units are dropped by ISIN as on the NSE side.
    Returns None when the bhavcopy cannot be read.
    """
    opener = _archive_opener("https://www.bseindia.com/")
    for back in range(1, 8):
        day = TODAY - datetime.timedelta(days=back)
        url = BSE_BHAVCOPY_URL.format(date=day.strftime("%Y%m%d"))
        try:
            blob = opener.open(url, timeout=45).read()
        except Exception:
            continue
        try:
            reader = csv.DictReader(
                io.StringIO(blob.decode("utf-8", "replace")))
            out = {
                row["TckrSymb"].strip().upper()
                for row in reader
                if str(row.get("SctySrs", "")).strip().upper() in BSE_SME_SERIES
                and not str(row.get("ISIN", "")).strip().upper().startswith("INF")
            }
        except Exception:
            continue
        if out:
            print(f"  SME filter  : {len(out)} BSE SME symbols from {day} bhavcopy")
            return out
    return None


def fetch_angel_universe(
    off_high_pct: float = ANGEL_UNIVERSE_OFF_HIGH_PCT,
    include_bse_sme: bool = True,
    sme_min_turnover: float = SME_MIN_TURNOVER,
) -> list:
    """Build the universe from Angel batched quotes instead of screener.in.

    Quotes every NSE cash symbol 50 at a time and keeps those trading within
    `off_high_pct` of their 52-week high — the same criterion as the default
    screener.in screen, but sourced from the broker feed so no login is
    involved. Angel's 52-week levels are split/bonus adjusted, so unlike raw
    bhavcopy prices they need no corporate-action repair.

    With `include_bse_sme` the BSE Emerge board is swept as well. Those names
    list only on BSE and would otherwise be missing entirely; the rest of BSE
    is skipped because it is almost all dual listings of NSE names plus
    illiquid scrips that the scanner has no turnover floor to reject. SME
    names are additionally held to `sme_min_turnover` (median traded value
    over SME_TURNOVER_DAYS sessions) because the board is thin enough that a
    breakout signal there is often unfillable. NSE names are not filtered, so
    the non-SME universe is unchanged.

    Returns yfinance-style tickers ('RELIANCE.NS', 'SUNITATOOL.BO'), matching
    the shape fetch_screener_universe returns.
    """
    from angel_client import angel_quotes, _load_scrip_master

    master = _load_scrip_master()
    nse = master[(master["exch_seg"] == "NSE")
                 & (master["symbol"].astype(str).str.endswith("-EQ"))]
    symbols = sorted({str(s).split("-", 1)[0].upper() for s in nse["symbol"]})
    # NSE keeps dummy instruments live in the scrip master.
    symbols = [s for s in symbols if "NSETEST" not in s]

    shares = _nse_share_symbols()
    if shares is None:
        print("  WARNING: bhavcopy unavailable — ETF/fund units cannot be "
              "excluded and may appear in results.")
    else:
        symbols = [s for s in symbols if s in shares]

    requests_list = [f"{s}.NS" for s in symbols]

    if include_bse_sme:
        sme = _bse_sme_symbols()
        if sme is None:
            print("  WARNING: BSE bhavcopy unavailable — SME names skipped.")
        else:
            # Only keep the ones Angel can actually quote.
            tradable = set(
                master.loc[master["exch_seg"] == "BSE", "symbol"]
                .astype(str).str.upper())
            requests_list += [f"{s}.BO" for s in sorted(sme & tradable)]

    print(f"  Quoting {len(requests_list)} symbols via Angel "
          f"({-(-len(requests_list) // 50)} requests) ...")
    quotes = angel_quotes(requests_list)
    print(f"  Received {len(quotes)} quotes")

    tickers = []
    for ticker, rec in quotes.items():
        try:
            ltp = float(rec.get("ltp") or 0)
            high52 = float(rec.get("52WeekHigh") or 0)
        except (TypeError, ValueError):
            continue
        if ltp <= 0 or high52 <= 0:
            continue
        if (high52 - ltp) / high52 <= off_high_pct:
            tickers.append(ticker)

    tickers = sorted(set(tickers))

    if include_bse_sme and sme_min_turnover > 0:
        sme_hits = [t for t in tickers if t.endswith(".BO")]
        if sme_hits:
            print(f"  Applying SME turnover floor "
                  f"(median {SME_TURNOVER_DAYS}d >= "
                  f"₹{sme_min_turnover / 1e7:.2f}cr) to {len(sme_hits)} names ...")
            sme_ohlcv = fetch_ohlcv(sme_hits)
            keep = set()
            for sym, df in sme_ohlcv.items():
                if df is None or len(df) < SME_TURNOVER_DAYS:
                    continue
                tail = df.tail(SME_TURNOVER_DAYS)
                turnover = (tail["Close"] * tail["Volume"]).median()
                if pd.notna(turnover) and turnover >= sme_min_turnover:
                    keep.add(sym)
            dropped = len(sme_hits) - len(keep)
            tickers = [t for t in tickers if not t.endswith(".BO") or t in keep]
            print(f"  Dropped {dropped} illiquid SME names, kept {len(keep)}")

    print(f"  {len(tickers)} names within {off_high_pct:.0%} of the 52-week high")
    return tickers


# ─── Data ingestion (Angel One SmartAPI) ───────────────────────────────────

def fetch_ohlcv(tickers: list, lookback_days: int = LOOKBACK_DAYS,
                batch_size: int = 100) -> dict:
    """Bulk-download daily OHLCV via Angel One SmartAPI.

    `tickers` are yfinance-style symbols (e.g. 'RELIANCE.NS', '534109.BO').
    The angel_client adapter resolves them to Angel symboltokens and
    returns DataFrames with the same Open/High/Low/Close/Volume columns,
    so downstream scoring code is unchanged.

    `batch_size` is kept for API parity but not used — SmartAPI is
    single-symbol per call, internally rate-limited by angel_client.
    Returns {ticker: DataFrame}.
    """
    from angel_client import angel_download_many
    end = TODAY + datetime.timedelta(days=1)
    start = TODAY - datetime.timedelta(days=int(lookback_days * 1.5))
    print(f"  Downloading OHLCV for {len(tickers)} tickers via Angel One ...")
    raw = angel_download_many(tickers, start, end)
    out = {}
    for tk, df in raw.items():
        if df is None or df.empty or len(df) < BASE_MIN_DAYS + 30:
            continue
        out[tk] = df
    print(f"  Got usable history for {len(out)} tickers "
          f"(of {len(tickers)} requested)")
    return out


def fetch_benchmark(lookback_days: int = LOOKBACK_DAYS) -> pd.Series:
    """Fetch Nifty 500 index close history from Angel One."""
    from angel_client import angel_download
    end = TODAY + datetime.timedelta(days=1)
    start = TODAY - datetime.timedelta(days=int(lookback_days * 1.5))
    df = angel_download("^CRSLDX", start, end)
    if df.empty:
        return pd.Series(dtype=float)
    return df["Close"].rename("Bench")


# ─── Part 2: Resistance detection ───────────────────────────────────────────

def fractal_pivots(highs: pd.Series, k: int = 3) -> pd.Series:
    """Boolean series: True where high is local max over [-k, +k] window."""
    h = highs.values
    n = len(h)
    out = np.zeros(n, dtype=bool)
    for i in range(k, n - k):
        if h[i] == h[i - k:i + k + 1].max() and h[i] >= h[i - 1] and h[i] >= h[i + 1]:
            out[i] = True
    return pd.Series(out, index=highs.index)


def detect_resistance(df: pd.DataFrame) -> Optional[dict]:
    """Find best horizontal resistance the stock is currently approaching.

    Uses long history (RES_LOOKBACK_DAYS) so the level is stable across days.
    Returns dict {R, base_start, touches, distance_pct, base_len_days}.
    """
    if len(df) < BASE_MIN_DAYS + 20:
        return None

    close = df["Close"]
    last_close = float(close.iloc[-1])

    # Use a long window so R doesn't drift day-to-day
    window = df.tail(RES_LOOKBACK_DAYS)
    # Two pivot scales: tight (k=3) and broad (k=8) -- broad gives stable
    # multi-month swing highs the eye picks out.
    piv_mask_tight = fractal_pivots(window["High"], k=3)
    piv_mask_broad = fractal_pivots(window["High"], k=8)
    pivots_tight = window["High"][piv_mask_tight]
    pivots_broad = window["High"][piv_mask_broad]
    pivots = pd.concat([pivots_tight, pivots_broad]).groupby(level=0).max()
    if len(pivots) < MIN_TOUCHES:
        return None

    # Cluster pivots into bands of width = RES_BAND_PCT * level (greedy)
    levels = sorted(pivots.tolist(), reverse=True)
    clusters = []
    for lvl in levels:
        placed = False
        for c in clusters:
            if abs(lvl - c["level"]) / c["level"] <= RES_BAND_PCT:
                c["sum"] += lvl
                c["count"] += 1
                c["level"] = c["sum"] / c["count"]
                placed = True
                break
        if not placed:
            clusters.append({"level": lvl, "sum": lvl, "count": 1})

    # Allow a wider distance window: from 3% above (just broken) to 8% below.
    candidates = []
    for c in clusters:
        R = c["level"]
        dist = (R - last_close) / last_close
        if c["count"] < MIN_TOUCHES:
            continue
        if dist < PROXIMITY_MIN_PCT or dist > PROXIMITY_MAX_PCT:
            continue
        cluster_pivots_idx = [
            ts for ts in pivots.index
            if abs(pivots.loc[ts] - R) / R <= RES_BAND_PCT
        ]
        if len(cluster_pivots_idx) < MIN_TOUCHES:
            continue
        base_start = min(cluster_pivots_idx)
        base_len = (df.index[-1] - base_start).days
        if base_len < BASE_MIN_DAYS:
            continue
        # v4.1: cap base length at BASE_MAX_DAYS (180 calendar days). Older
        # pivots are treated as historical context, not the active base.
        if base_len > BASE_MAX_DAYS:
            recent_pivots = [ts for ts in cluster_pivots_idx
                             if (df.index[-1] - ts).days <= BASE_MAX_DAYS]
            if len(recent_pivots) < MIN_TOUCHES:
                continue
            base_start = min(recent_pivots)
            base_len = (df.index[-1] - base_start).days
        # Score: more touches better, longer base better, closer to 52w high better
        is_52w_high = R >= float(window["High"].max()) * 0.98
        candidates.append({
            "R": R,
            "touches": len(cluster_pivots_idx),
            "base_start": base_start,
            "base_len_days": base_len,
            "distance_pct": dist,
            "touch_dates": cluster_pivots_idx,
            "is_52w_high": is_52w_high,
        })

    if not candidates:
        return None
    # Best = most-tested + longest base first; break ties by proximity.
    # (Dropped is_52w_high primary key — it forced selection of fresh swing
    # highs over the structurally significant horizontal level.)
    candidates.sort(key=lambda c: (
        -c["touches"], -c["base_len_days"], abs(c["distance_pct"])
    ))
    best = candidates[0]
    # Fix B: if there exists ANOTHER candidate ABOVE the chosen one with
    # similar or stronger structural strength (>=80% of best's touches AND
    # base length), prefer the higher one — that is the real ceiling, not a
    # mid-range support. Stops the scanner picking a 55 line when the real
    # box top is 60 with 12+ touches.
    higher = [c for c in candidates
              if c["R"] > best["R"] * 1.02
              and c["touches"] >= max(2, int(best["touches"] * 0.8))
              and c["base_len_days"] >= int(best["base_len_days"] * 0.8)]
    if higher:
        # Pick the highest such ceiling
        higher.sort(key=lambda c: -c["R"])
        return higher[0]
    return best


# ─── Indicators ─────────────────────────────────────────────────────────────

def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    sign = np.sign(df["Close"].diff().fillna(0))
    return (sign * df["Volume"]).cumsum()


def linreg_slope(y: pd.Series) -> float:
    if y.dropna().size < 5:
        return 0.0
    yy = y.dropna().values
    xx = np.arange(len(yy))
    return float(np.polyfit(xx, yy, 1)[0])


def rs_rising(df: pd.DataFrame, bench: pd.Series,
              lookback: int = RS_RISING_LOOKBACK) -> dict:
    """v4.0: True if the relative-strength line (stock_close / benchmark_close)
    has a positive slope over the last `lookback` sessions. This is the raw RS
    line both Mansfield and IBD draw — independent of absolute RS magnitude;
    it captures whether the stock is OUT-PERFORMING the index right now,
    regardless of the longer-term gap. Mansfield's own indicator (the same
    ratio measured against its 52-week average) is scored separately in
    `score_stock` block F. Returns dict {pass, slope, lookback}.
    """
    if bench is None or len(bench) < lookback or len(df) < lookback:
        return {"pass": False, "slope": 0.0, "lookback": lookback}
    common = df.index.intersection(bench.index)
    if len(common) < lookback:
        return {"pass": False, "slope": 0.0, "lookback": lookback}
    s = df["Close"].reindex(common).tail(lookback).astype(float)
    b = bench.reindex(common).tail(lookback).astype(float)
    if (b <= 0).any() or s.isna().any() or b.isna().any():
        return {"pass": False, "slope": 0.0, "lookback": lookback}
    rs_line = (s / b).values
    # Normalise so slope magnitude is comparable across stocks
    rs_norm = rs_line / rs_line[0] if rs_line[0] > 0 else rs_line
    x = np.arange(len(rs_norm), dtype=float)
    slope = float(np.polyfit(x, rs_norm, 1)[0])
    return {"pass": bool(slope > 0), "slope": slope, "lookback": lookback}




# ─── Part 3: Composite "Coiled Spring" Score ────────────────────────────────

def compute_score(df: pd.DataFrame, res: dict, bench: pd.Series) -> dict:
    """Compute 0-100 composite score. Returns dict of components + total.

    Re-weighted (v2 calibration) after 10-ticker screenshot audit:
      A Base quality      25
      B Volatility contr  10
      C Volume dry-up      5
      D Proximity to R    20
      E Trend             15
      F Relative strength 10
      G 52w high          15
      Total              100
    """
    base_start = res["base_start"]
    base = df.loc[base_start:]
    if len(base) < 20:
        return {"score": 0.0}

    R = res["R"]
    last = df.iloc[-1]
    last_close = float(last["Close"])

    # ── A: Base quality (25) ──
    T = res["base_len_days"]
    Tmax = 120
    base_score = 25.0 * min(T / Tmax, 1.0)
    # touches multiplier: 2=0.75, 3=0.9, 4=1.0, 5+=1.0 (no penalty above 4)
    touches_mult = min(0.75 + 0.075 * (res["touches"] - 2), 1.0)
    if res["touches"] >= 5:
        touches_mult = 1.0
    base_score *= touches_mult
    lows_idx = base["Low"].rolling(11, center=True).min() == base["Low"]
    swing_lows = base["Low"][lows_idx].dropna()
    higher_lows = (linreg_slope(swing_lows) > 0) if len(swing_lows) >= 3 else False
    if higher_lows:
        base_score = min(base_score * 1.15, 25.0)
    # Young-leader / trend-continuation boost: post-IPO or short-base names
    # in a strong uptrend (price > 50dma > 200dma, both rising) get a floor
    # so that a 60-day VCP against a 200dma slope doesn't get penalised
    # purely on length. Lifts base_score to at least 16/25.
    ma50_now = df["Close"].rolling(50).mean().iloc[-1]
    ma200_series = df["Close"].rolling(200).mean()
    ma200_now = ma200_series.iloc[-1] if not pd.isna(ma200_series.iloc[-1]) else 0
    in_uptrend = (last_close > ma50_now > ma200_now > 0
                  and linreg_slope(df["Close"].rolling(50).mean().tail(20)) > 0)
    young_leader = bool(in_uptrend and 30 <= T < 100)
    if young_leader:
        base_score = max(base_score, 16.0)

    # ── B: Volatility Contraction (10) ──
    a_series = atr(df, 14)
    atr_now = float(a_series.iloc[-10:].mean())
    atr_then = float(a_series.loc[base_start:].iloc[:20].mean()) if len(base) >= 20 else atr_now
    vcr = 1.0 - (atr_now / atr_then) if atr_then > 0 else 0.0
    vcr_score = 10.0 * max(min(vcr / 0.30, 1.0), 0.0)

    # ── C: Volume Dry-Up (5) ──
    v50 = float(df["Volume"].rolling(50).mean().iloc[-1])
    v10 = float(df["Volume"].iloc[-10:].mean())
    vdu = 1.0 - (v10 / v50) if v50 > 0 else 0.0
    vdu_score = 5.0 * max(min(vdu / 0.20, 1.0), 0.0)

    # ── D: Proximity (20) — reward being close to or just above R ──
    dist = (R - last_close) / last_close
    if PROXIMITY_MIN_PCT <= dist <= PROXIMITY_MAX_PCT:
        prox_score = 20.0 * max(0.0, 1.0 - abs(dist) / PROXIMITY_MAX_PCT)
    else:
        prox_score = 0.0

    # ── E: Trend (15) ──
    ma50 = df["Close"].rolling(50).mean()
    ma200 = df["Close"].rolling(200).mean()
    trend_score = 0.0
    if last_close > ma50.iloc[-1]:
        trend_score += 5
    if last_close > ma200.iloc[-1]:
        trend_score += 5
    if (linreg_slope(ma50.tail(20)) > 0
            and linreg_slope(ma200.tail(20)) > 0):
        trend_score += 5

    # ── F: Mansfield RS (10) ──
    rs_score = 0.0
    rs_value = 0.0
    if not bench.empty:
        b = bench.reindex(df.index).ffill()
        ratio = (df["Close"] / b).dropna()
        if len(ratio) >= 60:
            sma52w = ratio.rolling(min(252, len(ratio))).mean()
            mans = (ratio / sma52w - 1.0) * 100.0
            rs_value = float(mans.iloc[-1]) if not pd.isna(mans.iloc[-1]) else 0.0
            if rs_value > 0:
                rs_score = 5.0
            if linreg_slope(mans.tail(20)) > 0:
                rs_score += 5.0

    # ── G: 52-week high proximity (15) ──
    hi_52w = float(df["High"].tail(252).max())
    pct_off_high = (hi_52w - last_close) / hi_52w
    if pct_off_high <= 0.15:
        hi_score = 15.0 * (1.0 - pct_off_high / 0.15)
    else:
        hi_score = 0.0

    total = (base_score + vcr_score + vdu_score
             + prox_score + trend_score + rs_score + hi_score)

    return {
        "score": round(total, 2),
        "base_quality": round(base_score, 2),
        "vcr": round(vcr_score, 2),
        "vdu": round(vdu_score, 2),
        "proximity": round(prox_score, 2),
        "trend": round(trend_score, 2),
        "rs": round(rs_score, 2),
        "hi_52w": round(hi_score, 2),
        "vcr_raw": round(vcr, 3),
        "vdu_raw": round(vdu, 3),
        "atr_now": round(atr_now, 3),
        "atr_then": round(atr_then, 3),
        "higher_lows": higher_lows,
        "rs_value": round(rs_value, 3),
        "pct_off_52w_high": round(pct_off_high * 100, 2),
    }


# ─── Part 4: Pocket Pivot ───────────────────────────────────────────────────


# ─── Part 6: Risk architecture ──────────────────────────────────────────────

def risk_plan(df: pd.DataFrame, res: dict) -> dict:
    R = res["R"]
    base = df.loc[res["base_start"]:]
    base_low = float(base["Low"].min())
    last_close = float(df["Close"].iloc[-1])
    swing_lows_recent = base["Low"].tail(20).min()
    stop = float(swing_lows_recent) * 0.99
    height = R - base_low
    target = R + height  # measured move
    risk = last_close - stop
    reward = target - last_close
    rr = round(reward / risk, 2) if risk > 0 else None
    return {
        "entry": round(last_close, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk_pct": round(risk / last_close * 100, 2) if last_close else None,
        "reward_pct": round(reward / last_close * 100, 2) if last_close else None,
        "rr": rr,
        "base_low": round(base_low, 2),
        "base_height": round(height, 2),
    }


# ─── Part 7: Output ─────────────────────────────────────────────────────────

def _build_summary_sheet():
    """Static reference content written as the first sheet of the Excel.

    Includes:
      1. Run summary placeholder (filled by write_excel from `rows`)
      2. Final pre-breakout audit table (from the 25-ticker calibration set)
      3. Metric legend explaining what each row means
    """
    # 25-ticker calibration audit (pre-breakout, T-1 evaluation)
    audit_rows = [
        ("KRN.NS",         89.80, "YES", "YES",   990.0,  999.54,   0.96, "OK",   3.11,  7,  428, "od",   0.96),
        ("SYRMA.NS",       84.89, "YES", "YES",   915.0,  871.67,  -4.73, "OK",  -2.61, 10,  219, "pp",   0.82),
        ("WELCORP.NS",     75.60, "YES", "YES",   990.0,  950.82,  -3.96, "OK",  -1.14, 10,  304, "-",    0.88),
        ("SAAKSHI.NS",     75.24, "YES", "YES",   196.0,  188.67,  -3.74, "OK",  -3.25, 16,  504, "-",    0.38),
        ("MCX.NS",         74.28, "YES", "YES",  2780.0, 2647.15,  -4.78, "OK",  -4.28,  4,   74, "-",    0.91),
        ("SKYGOLD.NS",     72.25, "YES", "YES",   372.0,  353.10,  -5.08, "near", -4.39, 12,  529, "ppsqod", 0.54),
        ("PRUDENT.NS",     72.09, "YES", "YES",  2760.0, 2752.73,  -0.26, "OK",   0.07, 21,  616, "-",    0.62),
        ("ROLEXRINGS.NS",  70.85, "YES", "YES",   145.0,  138.74,  -4.32, "OK",  -0.53, 12,  392, "ppod", 0.74),
        ("SCI.NS",         70.82, "YES", "YES",   272.0,  280.89,   3.27, "OK",  10.87, 14,  800, "sqod", 0.80),
        ("QPOWER.NS",      67.67, "YES", "YES",  1080.0, 1054.72,  -2.34, "OK",   6.18,  4,  201, "-",    0.95),
        ("AZAD.NS",        66.24, "YES", "YES",  1780.0, 1670.00,  -6.18, "near",-4.09, 27,  623, "pp",   0.52),
        ("PARAS.NS",       65.70, "YES", "YES",   760.0,  724.64,  -4.65, "OK",  -4.00, 11,  631, "pp",   0.33),
        ("ASTRAMICRO.NS",  64.74, "YES", "no",   1050.0, 1045.11,  -0.47, "OK",   1.22, 12,  666, "-",    0.35),
        ("SUDEEPPHRM.NS",  64.48, "YES", "no",    690.0,  683.57,  -0.93, "OK",  -1.33,  8,  138, "-",    0.92),
        ("JAYNECOIND.NS",  64.26, "YES", "no",     83.0,   82.35,  -0.78, "OK",   0.81,  3,  163, "-",    0.67),
        ("ADVAIT.BO",      62.57, "YES", "no",   1880.0, 1941.27,   3.26, "OK",   3.96, 12,  666, "-",    0.46),
        ("RKFORGE.NS",     59.31, "YES", "no",    580.0,  587.24,   1.25, "OK",   4.27, 12,  257, "ppod", 0.96),
        ("KECL.NS",        58.55, "YES", "no",    108.0,  108.97,   0.90, "OK",   4.56,  5,  753, "-",   -0.49),
        ("WEBELSOLAR.NS",  55.58, "YES", "no",     98.5,   98.64,   0.14, "OK",   1.60,  4,  608, "pp",   0.29),
        ("TRITURBINE.NS",  54.10, "YES", "no",    550.0,  545.96,  -0.74, "OK",   5.92, 17,  359, "od",   0.25),
        ("EMMVEE.NS",      44.86, "no",  "no",    237.0,  227.62,  -3.96, "OK",   4.95,  3,   68, "pp",   0.99),
        ("FINBUD.NS",      41.07, "no",  "no",    113.0,  121.23,   7.29, "near",-3.55,  3,   92, "-",    0.27),
        ("PATILAUTOM.NS",  26.08, "no",  "no",    157.0,  159.70,   1.72, "OK",   7.91,  4,   62, "-",    0.97),
        ("SYSTEMATIC.BO",  13.42, "no",  "no",    168.0,  169.67,   0.99, "OK",  13.26,  3,   58, "-",    0.97),
    ]
    audit_df = pd.DataFrame(audit_rows, columns=[
        "symbol", "score", "WL>=50", "TR>=65",
        "expR", "scnR", "R_err%", "R_acc",
        "dist%", "touches", "base_d", "flags", "lvs",
    ])

    legend_rows = [
        ("R found",                "Pivot detector saw a level",         "Sanity check; if low, the geometry engine is broken"),
        ("R err <= 5%",            "R-line accuracy",                    "Is the level we picked the same one you'd draw?"),
        ("WL >= 50  (Watchlist)",  "Setup is forming",                   "Stocks worth monitoring daily"),
        ("TR >= 65  (Trigger) *",  "Setup is ripe",                      "* Stocks worth acting on -- this is what fills your buy list"),
        ("High-conviction",        "TR + all 4 confirm flags",           "Strict swing-trade entries"),
    ]
    legend_df = pd.DataFrame(legend_rows, columns=["Metric", "What it means", "When to look at it"])
    return audit_df, legend_df


def write_excel(rows: list, out_path: str):
    df = pd.DataFrame(rows).sort_values("score", ascending=False)

    # Build run-summary numbers from `rows`
    n_total   = len(df)
    n_wl      = int((df["score"] >= WATCHLIST_MIN_SCORE).sum())
    n_tr      = int((df["score"] >= TRIGGER_MIN_SCORE).sum())
    n_hc      = int(df.get("high_conviction", pd.Series([], dtype=bool)).sum()) if "high_conviction" in df.columns else 0
    run_summary = pd.DataFrame([
        ("Scan date",                   TODAY.strftime("%d-%b-%Y")),
        ("Universe candidates scored",  n_total),
        ("Watchlist  (score >= 50)",    n_wl),
        ("Trigger    (score >= 65)  *", n_tr),
        ("High-conviction (TR + all 5 conditions)", n_hc),
        ("", ""),
        ("Focus on TR >= 65 -- that's your actionable signal rate.", ""),
        ("Everything else is diagnostic.", ""),
    ], columns=["Metric", "Value"])

    audit_df, legend_df = _build_summary_sheet()

    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        # ── Sheet 1: Summary ──
        startrow = 0
        run_summary.to_excel(w, sheet_name="Summary", index=False, startrow=startrow)
        startrow += len(run_summary) + 3

        # Section header for legend
        pd.DataFrame([["METRIC LEGEND -- what each row means and when to look at it"]]).to_excel(
            w, sheet_name="Summary", index=False, header=False, startrow=startrow)
        startrow += 2
        legend_df.to_excel(w, sheet_name="Summary", index=False, startrow=startrow)
        startrow += len(legend_df) + 3

        # Section header for audit
        pd.DataFrame([["FINAL PRE-BREAKOUT AUDIT (25-ticker calibration set, evaluated at T-1)"]]).to_excel(
            w, sheet_name="Summary", index=False, header=False, startrow=startrow)
        startrow += 2
        audit_df.to_excel(w, sheet_name="Summary", index=False, startrow=startrow)

        # ── Sheet 2: Watchlist (full sorted by score) ──
        df.to_excel(w, sheet_name="Watchlist", index=False)

        # ── Sheet 3: Triggers (HC first, then by score) ──
        # Include any high-conviction pick even if its score < 65, since HC
        # is the strictest signal and should always appear on the action list.
        if "high_conviction" in df.columns:
            mask = (df["score"] >= TRIGGER_MIN_SCORE) | (df["high_conviction"] == True)  # noqa: E712
        else:
            mask = df["score"] >= TRIGGER_MIN_SCORE
        triggers = df[mask].copy()
        if not triggers.empty:
            if "high_conviction" in triggers.columns:
                triggers = triggers.sort_values(
                    ["high_conviction", "score"],
                    ascending=[False, False],
                )
            else:
                triggers = triggers.sort_values("score", ascending=False)
            triggers.to_excel(w, sheet_name="Triggers", index=False)

        # ── Sheet 4: High Conviction ──
        if "high_conviction" in df.columns:
            hc = df[df["high_conviction"] == True].copy()  # noqa: E712
            if not hc.empty:
                hc = hc.sort_values("score", ascending=False)
                cols_front = [
                    "symbol", "close", "resistance", "distance_pct",
                    "score", "hc_path", "pattern_multi_touch", "pattern_vcp",
                    "pattern_cup_handle", "rs_rising_50d",
                    "rr", "stop", "target",
                ]
                others = [c for c in hc.columns if c not in cols_front]
                hc[cols_front + others].to_excel(
                    w, sheet_name="High Conviction", index=False)

        # ── Sheet 5: Energy Expansion (observational, not gated) ──
        if "energy_expansion" in df.columns:
            ee = df[df["energy_expansion"] == True].copy()  # noqa: E712
            if not ee.empty:
                ee = ee.sort_values("vcr_raw", ascending=True)
                cols_front = [
                    "symbol", "close", "resistance", "distance_pct",
                    "vcr_raw", "base_range_pct", "score", "hc_path",
                    "rr", "stop", "target",
                ]
                cols_front = [c for c in cols_front if c in ee.columns]
                others = [c for c in ee.columns if c not in cols_front]
                ee[cols_front + others].to_excel(
                    w, sheet_name="Energy Expansion", index=False)
    print(f"  Excel written: {out_path}")



# ─── Hard gates (v4.3, eliminative) ────────────────────────────────────────
# Built from the 11-chart audit (SUBAHOTELS, ZAPPFRESH, ADCOUNTY, FINBUD,
# MSAFE, INDIAMART, KMEW, ALIVUS, PRIMECAB, JTLIND, ROLEXRINGS) + chart
# follow-up (FIEMIND, INDOBORAX, SJS). Each gate eliminates a specific
# chart pathology and is logged in the drop funnel for transparency.

def stage2_uptrend(df: pd.DataFrame) -> dict:
    """Stage-2 transition gate (v4.4, simplified).

    Required: close > 50DMA. The 200-DMA and 52w-low checks were removed
    in v4.4 — they over-filtered recovering names in the universe.
    """
    if len(df) < 60:
        return {"pass": False, "reason": "insufficient_history"}
    c = df["Close"]
    last = float(c.iloc[-1])
    ma50 = c.rolling(50).mean()
    if pd.isna(ma50.iloc[-1]) or last <= ma50.iloc[-1]:
        return {"pass": False, "reason": "below_ma50"}
    return {"pass": True, "reason": "ok"}



def not_extended(df: pd.DataFrame, max_bar_gain: float = 0.05,
                 max_bar_atr_mult: float = 2.5) -> bool:
    """True if entry isn't a vertical chase.
       No |close-to-close| > 5% in last 5 bars AND last TR <= 2.5*ATR(20)."""
    c = df["Close"]
    if len(c) < 25:
        return False
    if (c.pct_change().tail(5).abs() > max_bar_gain).any():
        return False
    a = atr(df, 20)
    if pd.isna(a.iloc[-1]) or a.iloc[-1] <= 0:
        return True
    last = df.iloc[-1]
    return float(last["High"] - last["Low"]) <= float(a.iloc[-1]) * max_bar_atr_mult


def recent_failed_breakout(df: pd.DataFrame, R: float,
                           lookback: int = 15) -> bool:
    """True if any high in last `lookback` bars pierced R*1.03 but stock
    is now < R*0.98 — R just rejected price (ROLEXRINGS / PRIMECAB)."""
    seg = df.tail(lookback)
    if seg.empty:
        return False
    pierced = bool((seg["High"] > R * 1.03).any())
    return pierced and float(df["Close"].iloc[-1]) < R * 0.98


def recent_r_test(df: pd.DataFrame, R: float, band_pct: float = 0.04,
                  lookback: int = RECENT_R_TEST_LOOKBACK) -> dict:
    """At least one bar in last `lookback` whose High is within band_pct
    of R. Kills KMEW-style picks where R was drawn from old pivots and
    the current rally has not yet physically tested the level.
    (Note: an absorption-volume sub-clause was tried in v3.3-strict but
    conflicted with the base dry-up requirement — kept as touch-only.)"""
    if len(df) < 60 or R <= 0:
        return {"pass": False, "reason": "insufficient_history",
                "n_touches_recent": 0}
    seg = df.tail(lookback)
    near = seg[seg["High"] >= R * (1 - band_pct)]
    n = int(len(near))
    if n == 0:
        return {"pass": False, "reason": "no_recent_test",
                "n_touches_recent": 0}
    return {"pass": True, "reason": "ok", "n_touches_recent": n}


def base_metrics(df: pd.DataFrame, base_start, R: float) -> dict:
    """Geometry of the base region.
       range_pct = (base_high-base_low)/R; trailing on last 25 bars."""
    base = df.loc[base_start:]
    if base.empty or R <= 0:
        return {"range_pct": 1.0, "trailing_pct": 1.0, "is_flat": False}
    base_low = float(base["Low"].min())
    base_high = float(base["High"].max())
    range_pct = (base_high - base_low) / R
    trail = base.tail(25)
    if trail.empty:
        trailing_pct = range_pct
    else:
        trailing_pct = (float(trail["High"].max())
                        - float(trail["Low"].min())) / R
    return {"range_pct": float(range_pct),
            "trailing_pct": float(trailing_pct),
            "is_flat": bool(trailing_pct <= 0.10)}


# ─── Pattern detectors (boost score / inform HC rule) ────────────────────

def cup_and_handle(df: pd.DataFrame, base_start, R: float) -> bool:
    """Crude cup & handle (kept for diagnostics, NOT used in HC rule —
       backtest showed 0 wins / 16 trades; detector misfires)."""
    base = df.loc[base_start:]
    if len(base) < 50 or R <= 0:
        return False
    cup = base.iloc[:-5]
    handle = base.tail(15)
    if len(cup) < 40 or len(handle) < 5:
        return False
    cup_high = float(cup["High"].max())
    cup_low = float(cup["Low"].min())
    if cup_high <= 0:
        return False
    cup_depth = (cup_high - cup_low) / cup_high
    if cup_depth < 0.12 or cup_depth > 0.40:
        return False
    third = max(1, len(cup) // 3)
    mid_low = float(cup.iloc[third:2 * third]["Low"].min())
    if mid_low > cup_low * 1.05:
        return False
    handle_depth = (cup_high - float(handle["Low"].min())) / cup_high
    if handle_depth > cup_depth * 0.5:
        return False
    last_close = float(df["Close"].iloc[-1])
    if abs(R - last_close) / last_close > 0.08:
        return False
    return True


def vcp_contractions(df: pd.DataFrame, base_start) -> int:
    """Count VCP-style successive contractions in the base.
       Each pullback < 85% of prior AND < 15% absolute. >=2 is valid VCP."""
    base = df.loc[base_start:]
    if len(base) < 30:
        return 0
    h = base["High"].values
    l = base["Low"].values
    n = len(h)
    k = 5
    pivots = []
    for i in range(k, n - k):
        if h[i] == h[i - k:i + k + 1].max():
            pivots.append((i, float(h[i])))
    if len(pivots) < 2:
        return 0
    pullbacks = []
    for j in range(1, len(pivots)):
        i_prev, p_prev = pivots[j - 1]
        i_cur, _ = pivots[j]
        seg_low = float(l[i_prev:i_cur + 1].min())
        if p_prev > 0:
            pullbacks.append((p_prev - seg_low) / p_prev)
    n_contr = 0
    for j in range(1, len(pullbacks)):
        if pullbacks[j] < pullbacks[j - 1] * 0.85 and pullbacks[j] < 0.15:
            n_contr += 1
    return n_contr




def w_pattern(df: pd.DataFrame, base_start, R: float) -> bool:
    """v4.1: Double-bottom (W) pattern inside the base.

    Geometry:
      - Two swing-lows (L1, L2) within 4% of each other
      - L2 occurs at least 5 bars after L1
      - A middle peak between them >= 5% above the lower low
      - Right side recovering: current close above 0.97 * middle-peak
        OR current close already inside resistance proximity band
    """
    base = df.loc[base_start:]
    if len(base) < 25 or R <= 0:
        return False
    lows = base["Low"].values
    highs = base["High"].values
    n = len(lows)
    k = 4
    pivot_lows = []
    for i in range(k, n - k):
        if lows[i] == lows[i - k:i + k + 1].min():
            pivot_lows.append((i, float(lows[i])))
    if len(pivot_lows) < 2:
        return False
    last_close = float(df["Close"].iloc[-1])
    for a in range(len(pivot_lows) - 1):
        i1, l1 = pivot_lows[a]
        for b in range(a + 1, len(pivot_lows)):
            i2, l2 = pivot_lows[b]
            if i2 - i1 < 5:
                continue
            lo, hi = (l1, l2) if l1 <= l2 else (l2, l1)
            if (hi - lo) / lo > 0.04:
                continue
            mid_high = float(highs[i1 + 1:i2].max()) if i2 > i1 + 1 else 0.0
            if mid_high <= 0:
                continue
            if (mid_high - lo) / lo < 0.05:
                continue
            near_neckline = last_close >= mid_high * 0.97
            dist = (R - last_close) / last_close
            near_R = (PROXIMITY_MIN_PCT <= dist <= PROXIMITY_MAX_PCT)
            if near_neckline or near_R:
                return True
    return False


# ─── Minervini Trend Template ────────────────────────────────────

def build_minervini_universe() -> list:
    """Wide NSE+BSE universe for the Trend Template.

    Union of two on-disk sources:
      * every ticker in the ohlcv_cache, recovered from the cache filename
        (``<safename>_<sha1[:8]>__1d.csv.gz``) and verified against the hash
      * every constituent listed in index_constituents.json

    Bare symbols are normalised to '.NS' and index symbols ('^...') dropped.
    nse_ready_sectors.py is deliberately NOT a source: it carries sector *index*
    closes only and has no stock-level constituents to contribute.
    """
    try:
        import ohlcv_cache
        cache_dir = ohlcv_cache.CACHE_DIR
    except Exception:
        cache_dir = os.path.join(SCRIPT_DIR, ".ohlcv_cache")

    syms, n_cache = set(), 0
    suffix = "__1d.csv.gz"
    for path in glob.glob(os.path.join(cache_dir, "*" + suffix)):
        base = os.path.basename(path)[:-len(suffix)]
        if "_" not in base:
            continue
        name, digest = base.rsplit("_", 1)
        if hashlib.sha1(name.encode("utf-8")).hexdigest()[:8] != digest:
            continue                      # sanitised name collided — untrustworthy
        if name.startswith("^"):
            continue                      # benchmark/index series, not a stock
        syms.add(name if "." in name else name + ".NS")
        n_cache += 1

    n_idx = 0
    try:
        with open(os.path.join(SCRIPT_DIR, "index_constituents.json")) as fh:
            groups = json.load(fh)
        for grp in groups.values():
            for sym in grp.get("constituents", []):
                sym = str(sym).strip()
                if sym:
                    syms.add(sym if "." in sym else sym + ".NS")
                    n_idx += 1
    except Exception as e:
        print(f"  index_constituents.json unusable: {e}")

    print(f"  Universe: {len(syms)} unique tickers "
          f"({n_cache} from ohlcv_cache, {n_idx} from index_constituents)")
    return sorted(syms)


def drop_stale(ohlcv: dict, max_stale_days: int = MINERVINI_MAX_STALE_DAYS) -> tuple:
    """Drop names whose newest bar lags the universe's newest session.

    The cache holds delisted/suspended scrips whose last bar can be months old;
    scoring those would report a 52-week range and MAs frozen at a stale date.
    Returns (fresh_ohlcv, n_dropped).
    """
    if not ohlcv:
        return {}, 0
    newest = max(df.index.max() for df in ohlcv.values())
    cutoff = newest - pd.Timedelta(days=max_stale_days)
    fresh = {k: v for k, v in ohlcv.items() if v.index.max() >= cutoff}
    return fresh, len(ohlcv) - len(fresh)


def rs_ratings(ohlcv: dict) -> dict:
    """IBD-style 1-99 relative-strength rating for every name in `ohlcv`.

    Raw strength uses the classic IBD weighting that double-counts the most
    recent quarter::

        2*(P/P_63) + (P/P_126) + (P/P_189) + (P/P_252)

    The raw values are then percentile-ranked onto a 1-99 scale. NOTE: the
    ranking is relative to THIS scan's universe (the screener list), not the
    whole market, so a 70 here means "top 30% of the names scanned today".
    Names with less than a year of history are omitted (no rating).

    Returns {ticker: rating}.
    """
    raw = {}
    for sym, df in ohlcv.items():
        c = df["Close"].dropna()
        if len(c) < MINERVINI_MIN_BARS:
            continue
        p0 = float(c.iloc[-1])
        legs = [float(c.iloc[-n]) for n in (63, 126, 189, 252)]
        if p0 <= 0 or min(legs) <= 0:
            continue
        q1, q2, q3, q4 = legs
        raw[sym] = 2.0 * (p0 / q1) + (p0 / q2) + (p0 / q3) + (p0 / q4)
    if not raw:
        return {}
    ranked = pd.Series(raw).rank(pct=True) * 98.0 + 1.0
    return {k: int(round(v)) for k, v in ranked.items()}


def liquid_enough(df: pd.DataFrame) -> bool:
    """Price and traded-value floor for the Trend Template.

    The Template says nothing about liquidity, so without this a ₹0.91 scrip
    with a rising 200-DMA qualifies as a "buy candidate" you could never fill.
    """
    c = df["Close"].dropna()
    if c.empty or float(c.iloc[-1]) < MINERVINI_MIN_PRICE:
        return False
    tail = df.tail(MINERVINI_TURNOVER_DAYS)
    turnover = (tail["Close"] * tail["Volume"]).median()
    return bool(pd.notna(turnover) and turnover >= MINERVINI_MIN_TURNOVER)


def _px(v: float) -> float:
    """Round a price for display; sub-rupee scrips need more than 2 decimals or
    their 50/150/200-DMAs collapse onto the same printed value."""
    return round(float(v), 2 if abs(v) >= 10 else 4)


def minervini_trend(df: pd.DataFrame, rs: Optional[float]) -> Optional[dict]:
    """Evaluate Minervini's 8-point Trend Template for a single symbol.

    All eight criteria are hard gates — failing even one disqualifies the
    stock. The returned record carries the measured values, a `passed` flag
    and the list of failed criteria (for the funnel report).

    Returns None when there is less than a year of history, which would make
    the 52-week range and the 200-DMA tests meaningless.
    """
    c = df["Close"].dropna()
    if len(c) < MINERVINI_MIN_BARS:
        return None

    close = float(c.iloc[-1])
    m50 = c.rolling(50).mean().iloc[-1]
    m150 = c.rolling(150).mean().iloc[-1]
    ma200 = c.rolling(200).mean().dropna()
    if pd.isna(m50) or pd.isna(m150) or len(ma200) <= MINERVINI_MA200_TREND:
        return None
    m50, m150 = float(m50), float(m150)
    m200 = float(ma200.iloc[-1])
    m200_then = float(ma200.iloc[-1 - MINERVINI_MA200_TREND])

    # Consecutive rising sessions in the 200-DMA. Bounded by how much of the
    # 200-DMA we can actually see: with the default ~252-day lookback only
    # ~55 sessions of it exist, so the "ideally 4-5 months" preference cannot
    # be verified and is reported as a streak rather than gated on.
    rising = 0
    for delta in reversed(ma200.diff().dropna().tolist()):
        if delta > 0:
            rising += 1
        else:
            break

    win = df.tail(MINERVINI_MIN_BARS)
    lo52 = float(win["Low"].min())
    hi52 = float(win["High"].max())
    if lo52 <= 0 or hi52 <= 0:
        return None
    above_low = (close / lo52 - 1.0) * 100.0
    below_high = (1.0 - close / hi52) * 100.0
    rs_val = float("nan") if rs is None or pd.isna(rs) else float(rs)

    checks = {
        "1_price_above_ma150": close > m150,
        "2_price_above_ma200": close > m200,
        "3_ma150_above_ma200": m150 > m200,
        "4_ma200_rising_1m":   m200 > m200_then,
        "5_price_above_ma50":  close > m50,
        "6_above_52w_low":     above_low >= MINERVINI_LOW_MIN_PCT,
        "7_near_52w_high":     below_high <= MINERVINI_HIGH_MAX_PCT,
        "8_rs_rating":         (not pd.isna(rs_val)) and rs_val >= MINERVINI_RS_MIN,
    }
    fails = [name for name, ok in checks.items() if not ok]
    ideal = (not fails
             and rs_val >= MINERVINI_RS_IDEAL
             and above_low >= MINERVINI_LOW_IDEAL_PCT
             and below_high <= MINERVINI_HIGH_IDEAL_PCT)

    return {
        "close": _px(close),
        "rs_rating": None if pd.isna(rs_val) else int(rs_val),
        "pct_above_52w_low": round(above_low, 1),
        "pct_below_52w_high": round(below_high, 1),
        "ma50": _px(m50),
        "ma150": _px(m150),
        "ma200": _px(m200),
        "ma200_rising_days": rising,
        "low_52w": _px(lo52),
        "high_52w": _px(hi52),
        "ideal": ideal,
        "passed": not fails,
        "fails": fails,
    }


def scan_minervini(ohlcv: dict) -> tuple:
    """Run the Trend Template across an entire fetched universe.

    Evaluates every ticker in `ohlcv` (not just the breakout candidates) — the
    candles are already downloaded, so this costs no extra API calls.

    The price/turnover floor is applied BEFORE the RS percentile is computed, so
    illiquid scrips (whose percentage moves are erratic) cannot distort the
    ranking of the tradable names.

    Returns (rows, fail_counts, skipped, illiquid) where `rows` holds only the
    names that passed all eight criteria, `fail_counts` maps each criterion to
    how many names it eliminated, `skipped` counts names with less than a year
    of history and `illiquid` counts names cut by the liquidity floor.
    """
    tradable = {s: df for s, df in ohlcv.items() if liquid_enough(df)}
    illiquid = len(ohlcv) - len(tradable)
    ratings = rs_ratings(tradable)
    rows, fail_counts, skipped = [], {}, 0
    for sym in sorted(tradable):
        rec = minervini_trend(tradable[sym], ratings.get(sym))
        if rec is None:
            skipped += 1
            continue
        if rec["passed"]:
            row = {"symbol": sym}
            row.update({k: v for k, v in rec.items()
                        if k not in ("passed", "fails")})
            rows.append(row)
        else:
            for name in rec["fails"]:
                fail_counts[name] = fail_counts.get(name, 0) + 1
    rows.sort(key=lambda r: (-(r["rs_rating"] or 0), r["symbol"]))
    return rows, fail_counts, skipped, illiquid


# ─── Scan driver ─────────────────────────────────────────────────

def scan(symbols: list, ohlcv: dict, bench: pd.Series,
         min_score: float, strict: bool = True) -> tuple:
    """Run per-ticker scan. Returns (rows, drop_counts).

    When strict=True, the v3.3 hard gates are enforced and every drop
    is logged into drop_counts for the funnel report. strict=False
    disables gates (diagnostic v1 funnel)."""
    rows = []
    drops: dict = {}

    def _drop(reason: str):
        drops[reason] = drops.get(reason, 0) + 1

    n = len(symbols)
    for i, sym in enumerate(symbols, 1):
        if sym not in ohlcv:
            _drop("no_data"); continue
        df = ohlcv[sym]
        # v4.0 GATE 0: minimum history (100 trading days)
        if len(df) < MIN_HISTORY_DAYS:
            _drop("insufficient_history"); continue
        if df["Volume"].rolling(50).mean().iloc[-1] < MIN_AVG_VOL:
            _drop("liquidity"); continue

        # ── HARD GATE 1: Stage-2 uptrend (Minervini, MA200-based) ──
        # NOTE v4.0: 50DMA-falling gate REMOVED per user request.
        if strict:
            s2 = stage2_uptrend(df)
            if not s2["pass"]:
                _drop(f"stage2:{s2['reason']}"); continue

        # ── HARD GATE 1d: entry must not be a vertical chase ──
        if strict and not not_extended(df):
            _drop("extended_entry"); continue

        try:
            res = detect_resistance(df)
            if res is None:
                _drop("no_resistance"); continue
            R = res["R"]

            # ── HARD GATE 2: distance to resistance in [-5%, +4%] ──
            if strict and not (PROXIMITY_MIN_PCT <= res["distance_pct"]
                               <= PROXIMITY_MAX_PCT):
                _drop("dist_out_of_band"); continue

            # ── HARD GATE 2a: recent failed breakout ──
            if strict and recent_failed_breakout(df, R):
                _drop("recent_failed_bo"); continue

            # ── HARD GATE 2b: recent R touch (50 sessions) ──
            rrt = recent_r_test(df, R, lookback=RECENT_R_TEST_LOOKBACK)
            if strict and not rrt["pass"]:
                _drop(f"r_test:{rrt['reason']}"); continue

            # ── HARD GATE 3: base width <= 40% (no wider bases) ──
            base_geo = base_metrics(df, res["base_start"], R)
            if strict and base_geo["range_pct"] > MAX_BASE_RANGE_PCT:
                _drop("base_too_wide"); continue

            # ── HARD GATE 4: rising relative strength over last 50 sessions ──
            rs = rs_rising(df, bench, lookback=RS_RISING_LOOKBACK)
            if strict and not rs["pass"]:
                _drop("rs_not_rising_50d"); continue

            # Volume ratio kept as informational column only (no longer a gate)
            v50 = float(df["Volume"].rolling(50).mean().iloc[-1])
            base_window = df["Volume"].iloc[-28:-3]
            v_base = float(base_window.mean()) if len(base_window) else v50
            v_ratio = (v_base / v50) if v50 > 0 else 1.0

            score = compute_score(df, res, bench)
            if score["score"] < min_score:
                _drop("low_score"); continue

            # ── Pattern detection (priority: multi_touch > vcp > W > C&H) ──
            n_vcp = vcp_contractions(df, res["base_start"])
            pattern_multitouch = bool(res["touches"] >= HC_MULTITOUCH_MIN)
            pattern_vcp = bool(n_vcp >= 2)
            pattern_w = bool(w_pattern(df, res["base_start"], R))
            pattern_ch = bool(cup_and_handle(df, res["base_start"], R))
            if pattern_multitouch:
                pattern_label = "multi_touch"
            elif pattern_vcp:
                pattern_label = "vcp"
            elif pattern_w:
                pattern_label = "w_pattern"
            elif pattern_ch:
                pattern_label = "cup_handle"
            else:
                pattern_label = ""
            pattern_ok = bool(pattern_multitouch or pattern_vcp
                              or pattern_w or pattern_ch)

            risk = risk_plan(df, res)
            distance_pct_value = round(res["distance_pct"] * 100, 2)

            # === HIGH-CONVICTION (v4.0) ===
            # All hard gates already enforced above. HC requires that the
            # setup also matches at least one of the three approved patterns.
            high_conviction = pattern_ok
            hc_path = pattern_label  # "multi_touch" | "vcp" | "cup_handle" | ""

            rows.append({
                "symbol": sym,
                "high_conviction": high_conviction,
                "hc_path": hc_path,
                "score": score["score"],
                "close": round(float(df["Close"].iloc[-1]), 2),
                "resistance": round(R, 2),
                "distance_pct": distance_pct_value,
                "touches": res["touches"],
                "base_days": res["base_len_days"],
                "base_range_pct": round(base_geo["range_pct"] * 100, 2),
                "trail25_range_pct": round(base_geo["trailing_pct"] * 100, 2),
                "vol_ratio_base": round(v_ratio, 3),
                "n_touches_recent": rrt.get("n_touches_recent", 0),
                **{k: score[k] for k in
                    ["base_quality", "vcr", "vdu", "proximity", "trend", "rs"]},
                "vcr_raw": score["vcr_raw"],
                "vdu_raw": score["vdu_raw"],
                # Observational tag only (v4.4): ATR expanded >=20% into the
                # pivot on a wide base. Scores 0/10 on vcr yet had the best
                # risk-adjusted outcomes in the 15-week review. Not gated.
                "energy_expansion": bool(
                    score["vcr_raw"] <= ENERGY_VCR_MAX
                    and base_geo["range_pct"] * 100 >= ENERGY_BASE_RANGE_MIN),
                "higher_lows": score["higher_lows"],
                "rs_rising_50d": rs["pass"],
                "rs_slope_50d": round(rs["slope"], 6),
                "pattern_multi_touch": pattern_multitouch,
                "pattern_vcp": pattern_vcp,
                "pattern_w": pattern_w,
                "pattern_cup_handle": pattern_ch,
                "n_vcp": n_vcp,
                **risk,
            })
        except Exception as e:
            print(f"  [{sym}] error: {e}")
        if i % 100 == 0:
            print(f"  scanned {i}/{n} ...")
    return rows, drops


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Breakout Scanner v4.3")
    p.add_argument("--max", type=int, default=0,
                   help="cap universe size (0 = all)")
    p.add_argument("--min-score", type=float, default=WATCHLIST_MIN_SCORE)

    p.add_argument("--lookback", type=int, default=LOOKBACK_DAYS)
    p.add_argument("--no-strict", action="store_true",
                   help="disable v3.3 hard gates (diagnostic v1 funnel)")
    p.add_argument("--high-conviction", action="store_true",
                   help="only output HC picks (v3.3 calibrated rule)")
    p.add_argument("--symbols-csv", type=str, default="",
                   help="path to CSV with a 'ticker' column to use as universe "
                        "(overrides the default screener universe)")
    p.add_argument("--screener-url", type=str, default=SCREENER_URL_DEFAULT,
                   help="screener.in screen URL for the universe "
                        f"(default: {SCREENER_URL_DEFAULT})")
    p.add_argument("--off-high-pct", type=float,
                   default=ANGEL_UNIVERSE_OFF_HIGH_PCT,
                   help="distance below the 52-week high used by the Angel "
                        "fallback universe when screener.in is unavailable "
                        f"(default: {ANGEL_UNIVERSE_OFF_HIGH_PCT})")
    p.add_argument("--no-bse-sme", action="store_true",
                   help="exclude the BSE Emerge board from the Angel fallback "
                        "universe entirely, regardless of turnover")
    p.add_argument("--sme-min-turnover", type=float,
                   default=SME_MIN_TURNOVER / 1e7,
                   help="minimum median 20d traded value, in ₹ crore, for BSE "
                        "SME names in the Angel fallback universe; 0 disables "
                        f"(default: {SME_MIN_TURNOVER / 1e7:.2f})")
    p.add_argument("--out-tag", type=str, default="",
                   help="suffix appended to output Excel filenames")
    p.add_argument("--minervini-universe", choices=("cache", "screener"),
                   default="cache",
                   help="universe for the MinerviniTrend sheet: 'cache' = full "
                        "NSE+BSE ohlcv_cache + index_constituents (slow, "
                        "correct), 'screener' = reuse the screener universe")
    p.add_argument("--skip-minervini", action="store_true",
                   help="skip the MinerviniTrend sheet (keeps runs fast)")
    args = p.parse_args()
    strict = not args.no_strict

    # Tee stdout+stderr to Output/logs/ so logs land in the right place.
    log_path = os.path.join(OUTPUT_DIR, "logs",
                            f"logs_breakout_scanner_angel_v35_{TIMESTAMP}.txt")
    _tee_out = _Tee(sys.stdout, log_path)
    _tee_err = _Tee(sys.stderr, log_path)
    sys.stdout = _tee_out
    sys.stderr = _tee_err

    print("=" * 70)
    print(f"  BREAKOUT SCANNER v4.4 — {TODAY.strftime('%d-%b-%Y')}")
    print(f"  Mode  : {'STRICT (v3.3 hard gates ON)' if strict else 'DIAGNOSTIC (gates OFF)'}")
    if args.high_conviction:
        print("  Filter: HIGH-CONVICTION only (v3.3 rule)")
    print("  Universe : Screener.in")
    print("=" * 70)

    effective_min_score = 0.0 if args.high_conviction else args.min_score

    # ── Custom CSV mode (single universe, legacy behavior) ──
    if args.symbols_csv:
        scsv = pd.read_csv(args.symbols_csv)
        if "ticker" not in scsv.columns:
            raise SystemExit(f"--symbols-csv {args.symbols_csv} must have a 'ticker' column")
        tickers = sorted({str(t).strip() for t in scsv["ticker"].dropna() if str(t).strip()})
        print(f"  Custom universe: {len(tickers)} tickers from {args.symbols_csv}")
        if args.max > 0:
            tickers = tickers[:args.max]
        ohlcv = fetch_ohlcv(tickers, args.lookback)
        bench = fetch_benchmark(args.lookback)
        rows, drops = scan(list(ohlcv.keys()), ohlcv, bench,
                           effective_min_score, strict=strict)
        _print_scan_stats(rows, drops, effective_min_score)
        if rows:
            excel_path = os.path.join(SCRIPT_DIR, "breakout_watchlist.xlsx")
            write_excel(rows, excel_path)
        print("\nDONE.")
        return

    # ── Screener universe mode (default) ──
    # Fetch benchmark once
    bench = fetch_benchmark(args.lookback)

    scr_raw_df = None # raw screener data
    scr_rows = []     # breakout results from screener universe
    ohlcv_scr = {}    # candles fetched for the Screener universe

    # ── Universe: Screener.in, falling back to Angel quotes ──
    print("\n" + "=" * 70)
    print("  UNIVERSE: Screener.in")
    print("=" * 70)
    scr_tickers = []
    try:
        scr_tickers = fetch_screener_universe(args.screener_url)
        # Save the raw screener reference DataFrame
        out_ref = os.path.join(OUTPUT_DIR, "screener_data.xlsx")
        if os.path.exists(out_ref):
            scr_raw_df = pd.read_excel(out_ref)
    except (Exception, SystemExit) as e:
        print(f"  Screener universe FAILED: {e}")
        import traceback
        traceback.print_exc()
        print("\n" + "=" * 70)
        print("  UNIVERSE FALLBACK: Angel 52-week-high quotes")
        print("=" * 70)
        try:
            scr_tickers = fetch_angel_universe(
                args.off_high_pct, include_bse_sme=not args.no_bse_sme,
                sme_min_turnover=args.sme_min_turnover * 1e7)
            _scope = "NSE" if args.no_bse_sme else "NSE + BSE SME"
            scr_raw_df = pd.DataFrame({
                "Ticker": scr_tickers,
                "Source": f"Angel 52w-high fallback, {_scope} "
                          f"(within {args.off_high_pct:.0%})",
            })
        except Exception as e2:
            print(f"  Angel universe FAILED: {e2}")
            traceback.print_exc()

    try:
        if args.max > 0:
            scr_tickers = scr_tickers[:args.max]
            print(f"  Universe capped to {len(scr_tickers)}")
        if scr_tickers:
            ohlcv_scr = fetch_ohlcv(scr_tickers, args.lookback)
            print("\n  Scanning universe ...")
            scr_rows, scr_drops = scan(list(ohlcv_scr.keys()), ohlcv_scr,
                                       bench, effective_min_score, strict=strict)
            _print_scan_stats(scr_rows, scr_drops, effective_min_score)
        else:
            print("  No tickers resolved — skipping scan.")
    except Exception as e:
        print(f"  Universe scan FAILED: {e}")
        import traceback
        traceback.print_exc()

    # ── Minervini Trend Template over the whole fetched universe ──
    mt_rows, mt_fails, mt_skipped, mt_illiquid = [], {}, 0, 0
    if not args.skip_minervini:
        print("\n" + "=" * 70)
        print("  MINERVINI TREND TEMPLATE")
        print("=" * 70)
        if args.minervini_universe == "screener":
            mt_ohlcv = dict(ohlcv_scr)
            print(f"  Universe: screener list ({len(mt_ohlcv)} tickers)")
        else:
            mt_universe = build_minervini_universe()
            if args.max > 0:
                mt_universe = mt_universe[:args.max]
                print(f"  Universe capped to {len(mt_universe)}")
            mt_ohlcv = fetch_ohlcv(mt_universe, args.lookback)
        mt_ohlcv, mt_stale = drop_stale(mt_ohlcv)
        if mt_stale:
            print(f"  Dropped {mt_stale} stale names "
                  f"(newest bar > {MINERVINI_MAX_STALE_DAYS}d behind the universe)")
        if mt_ohlcv:
            mt_rows, mt_fails, mt_skipped, mt_illiquid = scan_minervini(mt_ohlcv)
            print(f"  Dropped {mt_illiquid} illiquid names "
                  f"(< ₹{MINERVINI_MIN_PRICE:.0f} or median {MINERVINI_TURNOVER_DAYS}d "
                  f"turnover < ₹{MINERVINI_MIN_TURNOVER / 1e7:.0f}cr)")
            print(f"  Evaluated {len(mt_ohlcv) - mt_illiquid - mt_skipped} names "
                  f"({mt_skipped} skipped: < 1 year of history)")
            for name in sorted(mt_fails):
                print(f"    failed {name}: {mt_fails[name]}")
            print(f"  Passed all 8 criteria: {len(mt_rows)}")

    # ── Build unified 4-sheet Excel ──
    print("\n" + "=" * 70)
    print("  BUILDING COMBINED OUTPUT")
    print("=" * 70)

    excel_out = os.path.join(SCRIPT_DIR, "breakout_watchlist.xlsx")
    if args.out_tag:
        excel_out = os.path.join(SCRIPT_DIR,
                                 f"breakout_watchlist_{args.out_tag}.xlsx")

    with pd.ExcelWriter(excel_out, engine="openpyxl") as w:
        # Sheet 1: Screener raw data
        if scr_raw_df is not None and not scr_raw_df.empty:
            scr_raw_df.to_excel(w, sheet_name="Screener Data", index=False)
        else:
            pd.DataFrame({"Note": ["No Screener data"]}).to_excel(
                w, sheet_name="Screener Data", index=False)

        # Sheet 2: Breakout results from Screener universe
        if scr_rows:
            scr_df = pd.DataFrame(scr_rows).sort_values(
                ["high_conviction", "score"], ascending=[False, False])
            scr_df.to_excel(w, sheet_name="Screener Breakouts", index=False)
        else:
            pd.DataFrame({"Note": ["No Screener breakout candidates"]}).to_excel(
                w, sheet_name="Screener Breakouts", index=False)

        # Sheet 3: Energy Expansion — observational tag, not gated on anything.
        all_bo_df = pd.DataFrame(scr_rows) if scr_rows else pd.DataFrame()
        if not all_bo_df.empty and "symbol" in all_bo_df.columns:
            all_bo_df["symbol"] = all_bo_df["symbol"].astype(str).str.strip()
            sort_cols = [c for c in ("high_conviction", "score")
                         if c in all_bo_df.columns]
            if sort_cols:
                all_bo_df = all_bo_df.sort_values(
                    sort_cols, ascending=[False] * len(sort_cols))

        n_energy = 0
        ee_df = (all_bo_df[all_bo_df.get("energy_expansion") == True]  # noqa: E712
                 .drop_duplicates(subset=["symbol"])
                 if (not all_bo_df.empty
                     and "energy_expansion" in all_bo_df.columns)
                 else pd.DataFrame())
        if not ee_df.empty:
            ee_df = ee_df.sort_values("vcr_raw", ascending=True)
            front = [c for c in ("symbol", "close", "resistance", "distance_pct",
                                 "vcr_raw", "base_range_pct", "score", "hc_path",
                                 "rr", "stop", "target") if c in ee_df.columns]
            rest = [c for c in ee_df.columns if c not in front]
            ee_df[front + rest].to_excel(
                w, sheet_name="Energy Expansion", index=False)
            n_energy = len(ee_df)
        else:
            pd.DataFrame({"Note": [
                f"No candidates with vcr_raw <= {ENERGY_VCR_MAX} and "
                f"base_range_pct >= {ENERGY_BASE_RANGE_MIN}"]}).to_excel(
                w, sheet_name="Energy Expansion", index=False)

        # Sheet 4: Minervini Trend Template — all 8 criteria, eliminative.
        if mt_rows:
            pd.DataFrame(mt_rows).to_excel(
                w, sheet_name="MinerviniTrend", index=False)
        else:
            pd.DataFrame({"Note": [
                "No stocks passed all 8 Trend Template criteria"]}).to_excel(
                w, sheet_name="MinerviniTrend", index=False)

    print(f"  Excel written: {excel_out}")
    print(f"    Sheet 1: Screener Data")
    print(f"    Sheet 2: Screener Breakouts ({len(scr_rows)} candidates)")
    print(f"    Sheet 3: Energy Expansion ({n_energy} tagged, observational)")
    print(f"    Sheet 4: MinerviniTrend ({len(mt_rows)} passed all 8 criteria)")

    # ── TradingView TXT files ──
    tv_dir = os.path.dirname(excel_out)
    tag = f"_{args.out_tag}" if args.out_tag else ""

    # 1. All breakout candidates (deduplicated)
    bo_syms = sorted({r["symbol"] for r in scr_rows})
    _write_tv_file(os.path.join(tv_dir, f"tv_breakouts_combined{tag}.txt"), bo_syms)

    print(f"  TradingView files written:")
    print(f"    tv_breakouts_combined{tag}.txt  ({len(bo_syms)} symbols)")

    # Print top 10
    all_rows = list(scr_rows)
    if all_rows:
        all_sorted = sorted(all_rows, key=lambda r: (
            not r.get("high_conviction"), -r["score"]))
        print(f"\n  Top 10 overall (HC first, then by score):")
        cols = ["symbol", "high_conviction", "hc_path", "score", "close",
                "resistance", "distance_pct", "touches", "base_days",
                "base_range_pct", "rs_rising_50d", "rr"]
        top = pd.DataFrame(all_sorted[:10])[cols]
        print(top.to_string(index=False))
    else:
        print("\n  No breakout candidates found.")

    print("\nDONE.")


def _write_tv_file(path: str, tickers: list):
    """Write a TradingView-format watchlist file from Yahoo-style tickers."""
    converted = [_yahoo_to_tv(t) for t in tickers if t]
    with open(path, "w") as f:
        f.write(",\n".join(converted))


def _print_scan_stats(rows, drops, effective_min_score):
    """Print scan statistics and drop funnel."""
    if drops:
        print("\n  Drop funnel (reason -> count):")
        for reason, cnt in sorted(drops.items(), key=lambda x: -x[1]):
            print(f"    {reason:32s} {cnt:>5d}")

    print(f"\n  Candidates surviving all gates (score >= {effective_min_score}): {len(rows)}")

    if rows:
        n_mt   = sum(1 for r in rows if r["pattern_multi_touch"])
        n_vcp  = sum(1 for r in rows if r["pattern_vcp"])
        n_w    = sum(1 for r in rows if r["pattern_w"])
        n_ch   = sum(1 for r in rows if r["pattern_cup_handle"])
        n_rsr  = sum(1 for r in rows if r["rs_rising_50d"])
        n_d    = sum(1 for r in rows if PROXIMITY_MIN_PCT * 100
                                       <= r["distance_pct"]
                                       <= PROXIMITY_MAX_PCT * 100)
        n_b40  = sum(1 for r in rows if r["base_range_pct"] <= 40.0)
        n_hc   = sum(1 for r in rows if r["high_conviction"])
        n_hc_mt = sum(1 for r in rows if r.get("hc_path") == "multi_touch")
        n_hc_vcp = sum(1 for r in rows if r.get("hc_path") == "vcp")
        n_hc_w = sum(1 for r in rows if r.get("hc_path") == "w_pattern")
        n_hc_ch = sum(1 for r in rows if r.get("hc_path") == "cup_handle")
        print("  HC v4.3 condition pass rates:")
        print(f"    patterns: multi_touch={n_mt}, vcp={n_vcp},"
              f" w_pattern={n_w}, cup_handle={n_ch}")
        print(f"    rs_rising_50d={n_rsr}, dist[-5,+4]={n_d},"
              f" base<=40%={n_b40}")
        print(f"    HIGH-CONVICTION total: {n_hc}  "
              f"(multi_touch={n_hc_mt}, vcp={n_hc_vcp},"
              f" w={n_hc_w}, cup_handle={n_hc_ch})")





if __name__ == "__main__":
    main()
