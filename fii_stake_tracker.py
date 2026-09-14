"""
FII Stake Tracker — New Entries & Increasing Stakes
=====================================================

SUMMARY
-------
Identifies stocks across ALL Indian bourses (NSE, BSE, NSE SME, BSE SME)
where Foreign Institutional Investors (FII/FPI) have:
  1. Newly bought (new entry) — zero or near-zero FII holding in prior quarter.
  2. Increased stake from last quarter (quarter-on-quarter increase).
  3. Been increasing stake over multiple consecutive quarters.

WORKFLOW
--------
1. **Data Fetch (Primary — Tickertape)**
   - POSTs to the Tickertape Screener API with filter `forInstHldng3M > 0`
     AND `mrktCapf > MIN_MARKET_CAP_CR` to get all stocks above the market-cap
     floor where FII holding increased in the last quarter.
   - Paginates in batches of 200, with 0.3s delay between requests.
   - Fetches 19 data fields per stock (price, PE, PB, EPS, ROE, ROCE,
     D/E, revenue growth, EPS growth 5Y, 1M return vs Nifty, 200D SMA,
     pledged %, face value, market cap, FII holding %, QoQ & 6M changes).
   - Returns ~900 stocks at the ₹500 Cr floor (~3,400 unfiltered).

2. **Data Fetch (Fallback — Screener.in)**
   - Activates automatically if Tickertape API fails (HTTP error, timeout,
     or returns empty data).
   - Loads credentials (SCREENER_USER / SCREENER_PASS) from `.env`.
   - Logs in via CSRF-protected POST to https://www.screener.in/login/.
   - Scrapes a pre-saved screen ("Change in FII holding > 0") by
     paginating through HTML table pages (~50 rows/page, ~960 stocks).
   - Fewer columns available: Name, Ticker, Price, Market Cap, PE, EPS,
     ROE (3Y), ROCE (3Y), Pledged %, FII Hold %, Change in FII Hold %.
   - Columns NOT available from Screener.in (set to None): Face Value, PB,
     D/E, Revenue Growth, EPS Growth 5Y, 1M Return vs Nifty, 200D SMA,
     Change 6M, Sector.

3. **Streak History (Tickertape holdings)**
   - The screener query only carries 3M and 6M deltas (9M/12M exist as
     fields but are always null), giving just Q0, Q-1, Q-2. That caps a
     derived streak at 2.
   - To resolve longer streaks, every candidate already at 2+ is looked up
     on `GET /stocks/holdings/<sid>`, which returns 6 quarters of the full
     shareholding pattern; `fiPctT` is the FII percentage. The `sid` comes
     free from the screener response (item-level `sid`, falling back to the
     `slug` tail: '/stocks/20-microns-MICR' -> 'MICR').
   - Cached 7 days in `.cache/tickertape_shp/`. Empty results are never
     cached, so a transient failure is retried rather than masked for a week.
   - 6 quarters means 5 is the deepest provable streak. Set
     DEEP_HISTORY_VIA_SCREENER = True to top up streak-capped stocks from
     Screener.in (~12 quarters), at the cost of ~1s/stock and ban risk.
   - All snapshots are upserted into `.cache/fii_stake_history.csv`
     (Ticker, AsOf, FII_Pct), which accumulates depth across runs.

4. **Classification**
   Each stock is categorized from its FII holding history:
   - "New Entry"                — prior-quarter stake < 0.05%.
   - "4-Quarter Increasing"     — 4+ consecutive QoQ increases.
   - "3-Quarter Increasing"     — exactly 3.
   - "Multi-Quarter Increasing" — exactly 2.
   - "Increased Stake"          — this quarter only.

5. **HNI / Superstar Holdings**
   Scrapes 33 Screener.in `/people/` pages (Kacholia, Kedia, Mukul Agrawal,
   Malabar, Steadview ...), comparing the latest two quarters per holding to
   flag "New Entry" / "Increased" / "Decreased" / "Exited"; holdings left
   unchanged are skipped. These pages only carry stakes above the 1% SEBI
   disclosure threshold, so "New Entry" means "crossed 1%" and "Exited" means
   "fell below 1%", not necessarily a full buy or sale. Requires login. Runs
   FIRST, before any bulk fetching, so throttling later in the run cannot cost
   this sheet. Exempt from the market-cap floor — the point of the sheet is
   what a named investor traded, at any size.

6. **Market-cap floor**
   The FII sheets are restricted to stocks above MIN_MARKET_CAP_CR (₹500 Cr).
   Tickertape applies it server-side; the Screener.in fallback is filtered
   client-side. A stock whose market cap is unknown is dropped rather than
   kept, because the floor cannot be proven for it. The HNIs sheet is not
   filtered.

7. **Sorting**
   By category priority (New Entry -> 4Q -> 3Q -> 2Q -> 1Q), then by QoQ
   change descending within each category.

8. **Excel Export**
   Multi-sheet workbook with auto-fitted column widths. The FII sheets are
   restricted to Market Cap > ₹500 Cr and are EXCLUSIVE — each stock appears
   in exactly one bucket:
   - "Summary"              — classification rules + per-sheet counts.
   - "New_Entry"            — Category = New Entry AND FII stake > 1%
                              (the >1% floor drops thousands of sub-0.05%
                              rounding-noise entries).
   - "1-Quarter_Increasing" — Category = Increased Stake.
   - "2-Quarter_Increasing" — Streak = 2.
   - "3-Quarter_Increasing" — Streak = 3.
   - "4-Quarter_Increasing" — Streak >= 4.
   - "HNIs"                 — superstar buys and sells, when the scrape
                              succeeded. No market-cap filter.

DATA SOURCES
------------
Primary (Tickertape, no authentication):
- Screener query — https://api.tickertape.in/screener/query
  Undocumented public JSON API. Covers all NSE/BSE listed equities including
  SME (~5,900 tickers; ~2,000 above ₹500 Cr). Note: `forInstHldng9M` /
  `forInstHldng12M` are accepted but return null for every stock; only 3M and
  6M are real. Premium fields (RSI, 200D EMA) return 403; 200D SMA is used
  instead. `count` is not capped, so the full universe fits in one request.
- Holdings history — https://api.tickertape.in/stocks/holdings/<sid>
  6 quarters of shareholding pattern per stock. ~0.07s/call, no rate limit
  observed.

Screener.in (requires login; SCREENER_USER / SCREENER_PASS in .env):
- HNI pages — https://www.screener.in/people/<id>/<slug>/
  33 hardcoded investors. No Tickertape equivalent exists.
- Deep shareholding history — https://www.screener.in/company/<ticker>/
  ~12 quarters, public. Opt-in only (DEEP_HISTORY_VIA_SCREENER); bans by IP
  at the TCP level on burst traffic, so requests are spaced >=1s.
- Fallback screen — https://www.screener.in/screens/3192887/fii-0/
  Used only if the Tickertape screener query fails outright. ~960 stocks,
  fewer columns (no PB, D/E, 6M change, sector).

OUTPUT COLUMNS
--------------
  Stock Name | Ticker | Price (₹) | Market Cap (₹ Cr) | Face Value |
  PE (TTM) | PB | EPS (₹) | ROE (%) | ROCE (%) | D/E |
  Revenue Growth (%) | EPS Growth 5Y (%) | 1M Return vs Nifty (%) |
  200D SMA | Pledged (%) | No. of Shareholders | FII Stake (%) |
  Change QoQ (pp) | Change 6M (pp) | Change 9M (pp) | Change 12M (pp) |
  Streak (Qtrs) | Category | Sector
  (9M/12M are derived from the accumulated history CSV, not from the API.)

USAGE
-----
Standalone run (not part of run_all.py):
    python3 fii_stake_tracker.py                  # default output
    python3 fii_stake_tracker.py -o my_report     # custom output prefix

CADENCE
-------
    FII holdings are disclosed quarterly (SEBI LODR filings within 21 days
    of the Mar/Jun/Sep/Dec quarter-ends), so run this once per quarter,
    ~3-4 weeks after a quarter-end. Streak history persists across runs.

ENVIRONMENT
-----------
.env file (required for the HNI sheet and the Screener.in fallback):
    SCREENER_USER='your_email@example.com'
    SCREENER_PASS='your_password'

DEPENDENCIES
------------
requests, pandas, openpyxl, beautifulsoup4, python-dotenv
"""

import os
import sys
import time
import argparse
import datetime
import json
import re

import requests
import pandas as pd
from bs4 import BeautifulSoup

import screener_client

# ─── Config ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
API_URL = "https://api.tickertape.in/screener/query"
PAGE_SIZE = 200          # max results per API call
RATE_LIMIT_DELAY = 0.3   # seconds between paginated requests

# Market-cap floor (₹ Cr) applied to every sheet in the workbook. Stocks whose
# market cap is unknown are dropped too: the floor cannot be proven for them.
MIN_MARKET_CAP_CR = 500

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Fields to fetch from the screener
PROJECT_FIELDS = [
    "sid", "name", "ticker",
    "forInstHldng",      # current FII holding %
    "forInstHldng3M",    # change in FII holding over last 3 months (pp)
    "forInstHldng6M",    # change in FII holding over last 6 months (pp)
    "forInstHldng9M",    # change in FII holding over last 9 months (pp) — may be None
    "forInstHldng12M",   # change in FII holding over last 12 months (pp) — may be None
    "lastPrice",         # current close price
    "mrktCapf",          # market cap (₹ Cr)
    "ttmPe",             # TTM PE ratio
    "incEps",            # earnings per share (annual)
    "4wpctN",            # 1M return vs Nifty
    "faceValue",         # face value
    "promShrPled",       # pledged promoter holdings %
    "pbr",               # price-to-book ratio
    "roe",               # return on equity
    "roce",              # return on capital employed
    "rvng",              # 1Y historical revenue growth
    "epsGwth",           # 5Y historical EPS growth
    "dbtEqt",            # debt-to-equity ratio
    "sma200d",           # 200-day SMA
    "nShareholders",     # number of shareholders
]


# ─── API helpers ─────────────────────────────────────────────────────────────

def _create_session():
    """Create requests session with appropriate headers."""
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _fetch_page(session, match, offset, sort_by="forInstHldng3M",
                sort_order=-1):
    """Fetch one page of screener results."""
    payload = {
        "match": match,
        "sortBy": sort_by,
        "sortOrder": sort_order,
        "project": PROJECT_FIELDS,
        "offset": offset,
        "count": PAGE_SIZE,
    }
    resp = session.post(API_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError("Tickertape API returned success=false")
    return data["data"]


def _fetch_all(session, match, sort_by="forInstHldng3M", sort_order=-1):
    """Paginate through all screener results matching the filter."""
    offset = 0
    all_results = []
    total = None

    while True:
        page = _fetch_page(session, match, offset, sort_by, sort_order)
        results = page.get("results", [])
        if total is None:
            total = page.get("stats", {}).get("count", 0)
            print(f"  Total stocks matching filter: {total}")
        all_results.extend(results)
        if len(results) < PAGE_SIZE or len(all_results) >= total:
            break
        offset += PAGE_SIZE
        time.sleep(RATE_LIMIT_DELAY)

    return all_results


def _apply_mcap_filter(df, label="rows"):
    """Drop rows below MIN_MARKET_CAP_CR. Unknown market cap is treated as fail."""
    col = "Market Cap (₹ Cr)"
    if df.empty or col not in df.columns:
        return df
    before = len(df)
    keep = pd.to_numeric(df[col], errors="coerce") > MIN_MARKET_CAP_CR
    out = df[keep].reset_index(drop=True)
    dropped = before - len(out)
    if dropped:
        print(f"  Market cap filter (> ₹{MIN_MARKET_CAP_CR} Cr): "
              f"dropped {dropped} of {before} {label}")
    return out


# ─── Screener.in fallback ────────────────────────────────────────────────────

SCREENER_LOGIN_URL = "https://www.screener.in/login/"
SCREENER_SCREEN_URL = "https://www.screener.in/screens/3192887/fii-0/"


def _load_screener_creds():
    """Load Screener.in credentials. Kept as a thin wrapper so callers can still
    report "credentials missing" before any network work is attempted; the
    actual login is owned by screener_client."""
    if not screener_client.have_credentials():
        return None, None
    return "configured", "configured"


def _parse_screener_number(text):
    """Parse a number from Screener cell text like '1,234.56' or '12.34%'."""
    text = text.strip().replace(",", "").replace("%", "").replace("\xa0", "")
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _scrape_screener_page(page_num):
    """Scrape one page of the saved Screener.in screen. Returns list of row dicts.

    Never cached: the screen has to reflect the latest filings.
    """
    params = {"page": page_num} if page_num > 1 else None
    text = screener_client.get(SCREENER_SCREEN_URL, ttl_hours=0, params=params)
    if not text:
        return []
    soup = BeautifulSoup(text, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    # Parse header row — the screen has columns:
    # S.No. | Name | CMP | Mar Cap | 1day return | P/E | Down | RSI |
    # EPS 12M | Pledged | DII Hold | FII Hold | Sales Var 3Yrs |
    # Profit Var 3Yrs | ROE 3Yr | ROCE 3Yr | Chg in FII Hold
    rows = []
    data_rows = table.find_all("tr")[1:]  # skip header
    for tr in data_rows:
        cells = tr.find_all("td")
        if len(cells) < 17:
            continue
        # Extract company link for ticker
        name_cell = cells[1]
        link = name_cell.find("a")
        name = link.text.strip() if link else name_cell.text.strip()
        href = link.get("href", "") if link else ""
        # Ticker from URL like /company/RELIANCE/consolidated/
        ticker_match = re.search(r"/company/([^/]+)/", href)
        ticker = ticker_match.group(1) if ticker_match else ""

        cmp_val = _parse_screener_number(cells[2].text)
        mcap = _parse_screener_number(cells[3].text)
        pe = _parse_screener_number(cells[5].text)
        eps_val = _parse_screener_number(cells[8].text)
        pledged = _parse_screener_number(cells[9].text)
        fii_hold = _parse_screener_number(cells[11].text)
        roe_3y = _parse_screener_number(cells[14].text)
        roce_3y = _parse_screener_number(cells[15].text)
        chg_fii = _parse_screener_number(cells[16].text)

        if fii_hold is None or chg_fii is None:
            continue

        rows.append({
            "Stock Name": name,
            "Ticker": ticker,
            "Price (₹)": round(cmp_val, 2) if cmp_val is not None else None,
            "Market Cap (₹ Cr)": round(mcap, 2) if mcap is not None else None,
            "Face Value": None,
            "PE (TTM)": round(pe, 2) if pe is not None else None,
            "PB": None,
            "EPS (₹)": round(eps_val, 2) if eps_val is not None else None,
            "ROE (%)": round(roe_3y, 2) if roe_3y is not None else None,
            "ROCE (%)": round(roce_3y, 2) if roce_3y is not None else None,
            "D/E": None,
            "Revenue Growth (%)": None,
            "EPS Growth 5Y (%)": None,
            "1M Return vs Nifty (%)": None,
            "200D SMA": None,
            "Pledged (%)": round(pledged, 2) if pledged is not None else None,
            "No. of Shareholders": None,
            "FII Stake (%)": round(fii_hold, 2),
            "Change QoQ (pp)": round(chg_fii, 2),
            "Change 6M (pp)": None,
            "Category": "",       # will be classified later
            "Sector": "",
        })
    return rows


def fetch_fii_stake_data_screener():
    """Fallback: fetch FII stake data from Screener.in saved screen."""
    user, pwd = _load_screener_creds()
    if not user:
        print("  Screener.in credentials not found in .env — skipping fallback.")
        return pd.DataFrame()

    print("  Logging in to Screener.in ...")
    if not screener_client.login_ok():
        print("  Screener.in login failed.")
        return pd.DataFrame()
    print("  Login successful.")

    all_rows = []
    page = 1
    first_page_count = None
    while True:
        print(f"  Fetching page {page} ...", end="", flush=True)
        rows = _scrape_screener_page(page)
        print(f" {len(rows)} rows")
        if not rows:
            break
        all_rows.extend(rows)
        # Detect page size from first page; stop after a short page (last page)
        if first_page_count is None:
            first_page_count = len(rows)
        elif len(rows) < first_page_count:
            break
        page += 1

    if not all_rows:
        print("  No data from Screener.in.")
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)

    # Build _raw column so the shared enrichment can update history & classify.
    # Screener.in only provides QoQ change \u2014 no 6M / 9M / 12M deltas.
    df["_raw"] = df.apply(
        lambda r: {
            "ticker": r.get("Ticker"),
            "fii_pct": r.get("FII Stake (%)"),
            "chg_3m": r.get("Change QoQ (pp)"),
            "chg_6m": None,
            "chg_9m": None,
            "chg_12m": None,
        },
        axis=1,
    )
    df["Change 6M (pp)"] = None
    df["Change 9M (pp)"] = None
    df["Change 12M (pp)"] = None
    df = _enrich_with_streaks(df)

    cat_order = {
        "New Entry": 0,
        "4-Quarter Increasing": 1,
        "3-Quarter Increasing": 2,
        "Multi-Quarter Increasing": 3,
        "Increased Stake": 4,
    }
    df["_sort"] = df["Category"].map(cat_order)
    df = df.sort_values(["_sort", "Change QoQ (pp)"], ascending=[True, False])
    df = df.drop(columns=["_sort"]).reset_index(drop=True)

    return df


# ─── Core logic ──────────────────────────────────────────────────────────────

HISTORY_CSV = os.path.join(SCRIPT_DIR, ".cache", "fii_stake_history.csv")
SHP_CACHE_DIR = os.path.join(SCRIPT_DIR, ".cache", "screener_shp")
SHP_CACHE_TTL_DAYS = 7
# Request pacing for screener.in now lives in screener_client, which holds every
# caller in this repo to one shared, process-wide gap.

HOLDINGS_URL = "https://api.tickertape.in/stocks/holdings/{sid}"
HOLDINGS_CACHE_DIR = os.path.join(SCRIPT_DIR, ".cache", "tickertape_shp")
HOLDINGS_REQUEST_DELAY = 0.1
# Tickertape returns 6 quarters, so 5 is the deepest streak it can prove.
HOLDINGS_MAX_STREAK = 5
# screener.in carries ~12 quarters but bans on burst traffic; opt-in only.
DEEP_HISTORY_VIA_SCREENER = False

_QTR_MONTH = {"Mar": (3, 31), "Jun": (6, 30), "Sep": (9, 30), "Dec": (12, 31)}


def _parse_qtr_label(label):
    """Convert 'Mar 2024' → datetime.date(2024, 3, 31). Returns None on failure."""
    try:
        parts = label.strip().split()
        if len(parts) != 2:
            return None
        m, y = parts
        mm, dd = _QTR_MONTH[m[:3]]
        return datetime.date(int(y), mm, dd)
    except Exception:
        return None


def _sid_from_slug(slug):
    """Extract the Tickertape security id from a slug ('/stocks/20-microns-MICR' → 'MICR')."""
    if not slug:
        return None
    tail = str(slug).rstrip("/").rsplit("/", 1)[-1]
    return tail.rsplit("-", 1)[-1] if "-" in tail else tail or None


def _snap_to_quarter_end(d):
    """Map a filing date onto the calendar quarter end it belongs to.

    Tickertape occasionally reports an off-cycle date (e.g. 2025-10-31 for an
    interim filing); the streak logic keys strictly on quarter ends.
    """
    mm, dd = _QTR_MONTH[("Mar", "Jun", "Sep", "Dec")[(d.month - 1) // 3]]
    return datetime.date(d.year, mm, dd)


def _fetch_tickertape_holdings(sid, session):
    """Quarterly FII shareholding history for one stock, from Tickertape.

    Returns dict {quarter_end_date: fii_pct} (6 quarters). Cached on disk for
    7 days. Empty results are NOT cached — a transient failure and a genuine
    "no FII" both look like {}, and caching the former hides the stock for a
    week.
    """
    if not sid:
        return {}
    cache_path = os.path.join(HOLDINGS_CACHE_DIR, f"{sid}.json")
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < SHP_CACHE_TTL_DAYS * 86400:
            try:
                with open(cache_path) as f:
                    raw = json.load(f)
                return {datetime.date.fromisoformat(k): float(v)
                        for k, v in raw.items()}
            except Exception:
                pass

    out = {}
    try:
        r = session.get(HOLDINGS_URL.format(sid=sid), timeout=15)
        if r.status_code == 200:
            exact = set()
            for entry in (r.json().get("data") or []):
                raw_date = (entry.get("date") or "")[:10]
                val = (entry.get("data") or {}).get("fiPctT")
                if not raw_date or val is None:
                    continue
                try:
                    d = datetime.date.fromisoformat(raw_date)
                except ValueError:
                    continue
                qe = _snap_to_quarter_end(d)
                # A true quarter-end filing always beats a snapped interim one.
                if d == qe:
                    exact.add(qe)
                elif qe in exact:
                    continue
                out[qe] = float(val)
    except Exception:
        return {}

    if out:
        try:
            os.makedirs(HOLDINGS_CACHE_DIR, exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump({k.isoformat(): v for k, v in out.items()}, f)
        except Exception:
            pass
    return out


def _fetch_screener_shp(ticker):
    """Fetch full quarterly FII shareholding history for a ticker from Screener.in.

    Returns dict: {quarter_end_date: fii_pct}. The parsed result is cached on
    disk for 7 days; the raw HTML is not, because caching a 200KB company page
    per ticker would dwarf the data extracted from it. Deeper than Tickertape
    (~12 quarters vs 6) but heavy, so this is opt-in via
    DEEP_HISTORY_VIA_SCREENER. Pacing is handled by screener_client.
    """
    if not ticker:
        return {}
    cache_path = os.path.join(SHP_CACHE_DIR, f"{ticker}.json")
    if os.path.exists(cache_path):
        age = time.time() - os.path.getmtime(cache_path)
        if age < SHP_CACHE_TTL_DAYS * 86400:
            try:
                with open(cache_path) as f:
                    raw = json.load(f)
                return {datetime.date.fromisoformat(k): float(v) for k, v in raw.items()}
            except Exception:
                pass

    out = {}
    for path in (f"/company/{ticker}/consolidated/", f"/company/{ticker}/"):
        try:
            text = screener_client.get(f"https://www.screener.in{path}", ttl_hours=0)
            if not text:
                continue
            soup = BeautifulSoup(text, "html.parser")
            sec = soup.find(id="quarterly-shp")
            if not sec:
                continue
            table = sec.find("table")
            if not table:
                continue
            thead = table.find("thead")
            tbody = table.find("tbody")
            if not thead or not tbody:
                continue
            headers = [th.get_text(strip=True) for th in thead.find_all("th")][1:]
            for tr in tbody.find_all("tr"):
                cells = tr.find_all("td")
                if not cells:
                    continue
                label = cells[0].get_text(strip=True).rstrip("+").strip()
                if not label.upper().startswith("FII"):
                    continue
                for h, c in zip(headers, cells[1:]):
                    qe = _parse_qtr_label(h)
                    if qe is None:
                        continue
                    v = c.get_text(strip=True).rstrip("%").replace(",", "")
                    if v and v != "-":
                        try:
                            out[qe] = float(v)
                        except ValueError:
                            pass
                break
            if out:
                break
        except Exception:
            continue

    if out:
        try:
            os.makedirs(SHP_CACHE_DIR, exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump({k.isoformat(): v for k, v in out.items()}, f)
        except Exception:
            pass
    return out


def _current_quarter_end(today=None):
    """Most recently completed calendar quarter end."""
    today = today or datetime.date.today()
    y, m = today.year, today.month
    if m <= 3:
        return datetime.date(y - 1, 12, 31)
    if m <= 6:
        return datetime.date(y, 3, 31)
    if m <= 9:
        return datetime.date(y, 6, 30)
    return datetime.date(y, 9, 30)


def _shift_quarter(qe, n):
    """Shift a quarter-end date by n quarters (negative=backward)."""
    return (pd.Timestamp(qe) + pd.tseries.offsets.QuarterEnd(n)).date()


def _load_history():
    if not os.path.exists(HISTORY_CSV):
        return pd.DataFrame(columns=["Ticker", "AsOf", "FII_Pct"])
    try:
        df = pd.read_csv(HISTORY_CSV)
        df["AsOf"] = pd.to_datetime(df["AsOf"]).dt.date
        df["FII_Pct"] = pd.to_numeric(df["FII_Pct"], errors="coerce")
        return df.dropna(subset=["FII_Pct"])
    except Exception as e:
        print(f"  Warning: history file unreadable ({e}); starting fresh.")
        return pd.DataFrame(columns=["Ticker", "AsOf", "FII_Pct"])


def _save_history(df):
    os.makedirs(os.path.dirname(HISTORY_CSV), exist_ok=True)
    df = df.sort_values(["Ticker", "AsOf"]).reset_index(drop=True)
    df.to_csv(HISTORY_CSV, index=False)


def _backfill_snapshots(raw_rows, asof_q0):
    """Derive quarter-end FII% snapshots from current + 3M/6M/9M/12M deltas.

    Each fetched row contributes up to 5 snapshots: Q0, Q-1, Q-2, Q-3, Q-4.
    Q-3 and Q-4 only if 9M/12M deltas are present.
    """
    qe = {n: _shift_quarter(asof_q0, n) for n in (0, -1, -2, -3, -4)}
    snaps = []
    for r in raw_rows:
        t = r.get("ticker")
        if not t:
            continue
        fii = r.get("fii_pct")
        if fii is None:
            continue
        snaps.append((t, qe[0], float(fii)))
        c3 = r.get("chg_3m")
        if c3 is not None:
            snaps.append((t, qe[-1], max(0.0, float(fii) - float(c3))))
        c6 = r.get("chg_6m")
        if c6 is not None:
            snaps.append((t, qe[-2], max(0.0, float(fii) - float(c6))))
        c9 = r.get("chg_9m")
        if c9 is not None:
            snaps.append((t, qe[-3], max(0.0, float(fii) - float(c9))))
        c12 = r.get("chg_12m")
        if c12 is not None:
            snaps.append((t, qe[-4], max(0.0, float(fii) - float(c12))))
    return pd.DataFrame(snaps, columns=["Ticker", "AsOf", "FII_Pct"])


def _merge_history(existing, new_snaps):
    """Upsert new snapshots into existing history (newer rows win on dup keys)."""
    if new_snaps.empty:
        return existing
    combined = pd.concat([existing, new_snaps], ignore_index=True)
    combined = combined.sort_values(["Ticker", "AsOf"])
    combined = combined.drop_duplicates(subset=["Ticker", "AsOf"], keep="last")
    return combined.reset_index(drop=True)


def _build_streak_lookup(history_df, asof_q0):
    """Return dict: ticker -> streak length (consecutive QoQ increases ending at Q0).

    Streak=1 means FII at Q0 > FII at Q-1.
    Streak=N means N consecutive quarter-over-quarter increases.
    """
    out = {}
    if history_df.empty:
        return out
    for ticker, grp in history_df.groupby("Ticker"):
        sd = dict(zip(grp["AsOf"], grp["FII_Pct"]))
        cur = asof_q0
        streak = 0
        while cur in sd:
            prev = _shift_quarter(cur, -1)
            if prev not in sd:
                break
            if sd[cur] > sd[prev]:
                streak += 1
                cur = prev
            else:
                break
        out[ticker] = streak
    return out


def _classify(fii_pct, chg_3m, streak):
    """Classify the FII stake change pattern using streak length.

    Returns one of:
      'New Entry'                — FII had near-zero holding before this quarter
      '4-Quarter Increasing'     — increased for 4+ consecutive quarters
      '3-Quarter Increasing'     — increased for exactly 3 consecutive quarters
      'Multi-Quarter Increasing' — increased for exactly 2 consecutive quarters
      'Increased Stake'          — increased this quarter only
    """
    prev_qtr = (fii_pct or 0) - (chg_3m or 0)
    if prev_qtr < 0.05:
        return "New Entry"
    if streak >= 4:
        return "4-Quarter Increasing"
    if streak == 3:
        return "3-Quarter Increasing"
    if streak == 2:
        return "Multi-Quarter Increasing"
    return "Increased Stake"


def fetch_fii_stake_data():
    """Fetch and process FII stake tracker data.

    Primary: Tickertape Screener API.
    Fallback: Screener.in saved screen (if Tickertape fails).

    Only stocks above MIN_MARKET_CAP_CR are returned. Tickertape applies that
    floor server-side; the Screener.in fallback is filtered here instead.

    Returns a pandas DataFrame with classified FII stake changes.
    """
    # ── Primary: Tickertape ──
    try:
        df = _fetch_fii_tickertape()
        if not df.empty:
            return _apply_mcap_filter(df, "stocks")
    except Exception as e:
        print(f"  Tickertape failed: {e}")

    # ── Fallback: Screener.in ──
    print("\nFalling back to Screener.in ...")
    try:
        df = fetch_fii_stake_data_screener()
        if not df.empty:
            print(f"  Screener.in returned {len(df)} records.")
            return _apply_mcap_filter(df, "stocks")
    except Exception as e:
        print(f"  Screener.in fallback also failed: {e}")

    return pd.DataFrame()


def _fetch_fii_tickertape():
    """Fetch FII stake data from Tickertape (primary source)."""
    session = _create_session()

    print("Fetching stocks where FII increased stake (last quarter) ...")
    print("  Source: Tickertape Screener API")
    # Filter: FII holding rose over the last 3 months, above the market-cap floor.
    # Applying the floor server-side keeps the streak-enrichment loop small.
    match = {
        "forInstHldng3M": {"g": 0},
        "mrktCapf": {"g": MIN_MARKET_CAP_CR},
    }
    print(f"  Market cap floor: > ₹{MIN_MARKET_CAP_CR} Cr")
    results = _fetch_all(session, match)
    print(f"  Fetched {len(results)} stock records.")

    if not results:
        print("  No stocks found with FII stake increase.")
        return pd.DataFrame()

    # Parse into rows
    rows = []
    for item in results:
        stock = item.get("stock", {})
        info = stock.get("info", {})
        ratios = stock.get("advancedRatios", {})

        name = info.get("name", "")
        ticker = info.get("ticker", "")
        sector = info.get("sector", "")
        sid = item.get("sid") or _sid_from_slug(stock.get("slug"))

        fii_pct = ratios.get("forInstHldng", 0) or 0
        chg_3m = ratios.get("forInstHldng3M", 0) or 0
        chg_6m = ratios.get("forInstHldng6M", 0) or 0
        chg_9m = ratios.get("forInstHldng9M")
        chg_12m = ratios.get("forInstHldng12M")
        price = ratios.get("lastPrice", None)
        mcap = ratios.get("mrktCapf", None)
        pe = ratios.get("ttmPe", None)
        eps = ratios.get("incEps", None)
        ret_vs_nifty = ratios.get("4wpctN", None)
        face_val = ratios.get("faceValue", None)
        pledged = ratios.get("promShrPled", None)
        pb = ratios.get("pbr", None)
        roe_val = ratios.get("roe", None)
        roce_val = ratios.get("roce", None)
        rev_growth = ratios.get("rvng", None)
        eps_growth = ratios.get("epsGwth", None)
        de_ratio = ratios.get("dbtEqt", None)
        sma200 = ratios.get("sma200d", None)
        n_shareholders = ratios.get("nShareholders", None)

        def _r(v, d=2):
            return round(v, d) if v is not None else None

        rows.append({
            "Stock Name": name,
            "Ticker": ticker,
            "Price (₹)": _r(price),
            "Market Cap (₹ Cr)": _r(mcap),
            "Face Value": _r(face_val),
            "PE (TTM)": _r(pe),
            "PB": _r(pb),
            "EPS (₹)": _r(eps),
            "ROE (%)": _r(roe_val),
            "ROCE (%)": _r(roce_val),
            "D/E": _r(de_ratio),
            "Revenue Growth (%)": _r(rev_growth),
            "EPS Growth 5Y (%)": _r(eps_growth),
            "1M Return vs Nifty (%)": _r(ret_vs_nifty),
            "200D SMA": _r(sma200),
            "Pledged (%)": _r(pledged),
            "No. of Shareholders": int(n_shareholders) if n_shareholders is not None else None,
            "FII Stake (%)": round(fii_pct, 2),
            "Change QoQ (pp)": round(chg_3m, 2),
            "Change 6M (pp)": round(chg_6m, 2),
            "Change 9M (pp)": _r(chg_9m),
            "Change 12M (pp)": _r(chg_12m),
            "Sector": sector,
            "_raw": {
                "ticker": ticker,
                "sid": sid,
                "fii_pct": fii_pct,
                "chg_3m": chg_3m,
                "chg_6m": chg_6m,
                "chg_9m": chg_9m,
                "chg_12m": chg_12m,
            },
        })

    df = pd.DataFrame(rows)
    df = _enrich_with_streaks(df)

    # Sort: New Entry first, then longest streak, then by QoQ change
    cat_order = {
        "New Entry": 0,
        "4-Quarter Increasing": 1,
        "3-Quarter Increasing": 2,
        "Multi-Quarter Increasing": 3,
        "Increased Stake": 4,
    }
    df["_sort"] = df["Category"].map(cat_order)
    df = df.sort_values(["_sort", "Change QoQ (pp)"], ascending=[True, False])
    df = df.drop(columns=["_sort"]).reset_index(drop=True)

    return df


def _enrich_with_streaks(df):
    """Update history with new snapshots, fetch full FII history for candidates,
    compute streak per ticker, classify, and populate 9M/12M deltas from history."""
    if df.empty:
        return df
    asof_q0 = _current_quarter_end()
    q3 = _shift_quarter(asof_q0, -3)
    q4 = _shift_quarter(asof_q0, -4)

    raw_rows = df["_raw"].tolist()
    new_snaps = _backfill_snapshots(raw_rows, asof_q0)
    history = _load_history()
    merged = _merge_history(history, new_snaps)

    df["_sid"] = df["_raw"].apply(lambda r: r.get("sid"))

    # First-pass streak (using only Tickertape-derived snapshots + prior history)
    streaks = _build_streak_lookup(merged, asof_q0)
    df["Streak (Qtrs)"] = df["Ticker"].map(streaks).fillna(0).astype(int)

    # For candidates with streak >= 2, fetch full quarterly FII history to
    # determine whether the streak actually extends further.
    extend_targets = (
        df.loc[df["Streak (Qtrs)"] >= 2, ["Ticker", "_sid"]]
        .dropna(subset=["Ticker"]).drop_duplicates("Ticker")
        .itertuples(index=False, name=None)
    )
    extend_targets = [(t, s) for t, s in extend_targets if s]
    if extend_targets:
        print(
            f"  Fetching Tickertape shareholding history for "
            f"{len(extend_targets)} multi-quarter candidates ..."
        )
        shp_session = requests.Session()
        shp_session.headers.update({"User-Agent": HEADERS["User-Agent"],
                                    "Accept": "application/json"})
        extra_snaps = []
        cached_hits = 0
        for i, (ticker, sid) in enumerate(extend_targets, 1):
            cache_path = os.path.join(HOLDINGS_CACHE_DIR, f"{sid}.json")
            was_cached = (
                os.path.exists(cache_path)
                and (time.time() - os.path.getmtime(cache_path)) < SHP_CACHE_TTL_DAYS * 86400
            )
            shp = _fetch_tickertape_holdings(sid, shp_session)
            if was_cached:
                cached_hits += 1
            else:
                time.sleep(HOLDINGS_REQUEST_DELAY)
            for qe, v in shp.items():
                extra_snaps.append((ticker, qe, v))
            if i % 100 == 0 or i == len(extend_targets):
                print(f"    {i}/{len(extend_targets)}  (cache hits: {cached_hits})")
        if extra_snaps:
            merged = _merge_history(
                merged,
                pd.DataFrame(extra_snaps, columns=["Ticker", "AsOf", "FII_Pct"]),
            )
            streaks = _build_streak_lookup(merged, asof_q0)
            df["Streak (Qtrs)"] = df["Ticker"].map(streaks).fillna(0).astype(int)

        # Stocks pinned at Tickertape's 6-quarter ceiling may run deeper;
        # only screener.in can prove it, and only if explicitly enabled.
        if DEEP_HISTORY_VIA_SCREENER:
            capped = df.loc[df["Streak (Qtrs)"] >= HOLDINGS_MAX_STREAK,
                            "Ticker"].dropna().unique().tolist()
            if capped:
                print(f"  Deep history via Screener.in for {len(capped)} "
                      f"streak-capped stocks ...")
                deep_snaps = []
                for ticker in capped:
                    for qe, v in _fetch_screener_shp(ticker).items():
                        deep_snaps.append((ticker, qe, v))
                if deep_snaps:
                    merged = _merge_history(
                        merged,
                        pd.DataFrame(deep_snaps,
                                     columns=["Ticker", "AsOf", "FII_Pct"]),
                    )
                    streaks = _build_streak_lookup(merged, asof_q0)
                    df["Streak (Qtrs)"] = (
                        df["Ticker"].map(streaks).fillna(0).astype(int))

    _save_history(merged)

    # Populate 9M / 12M deltas from merged history when possible
    hist_lookup = {(r.Ticker, r.AsOf): r.FII_Pct for r in merged.itertuples(index=False)}

    def _delta(ticker, qn):
        cur = hist_lookup.get((ticker, asof_q0))
        prev = hist_lookup.get((ticker, qn))
        if cur is None or prev is None:
            return None
        return round(cur - prev, 2)

    df["Change 9M (pp)"] = df["Ticker"].apply(lambda t: _delta(t, q3))
    df["Change 12M (pp)"] = df["Ticker"].apply(lambda t: _delta(t, q4))

    df["Category"] = df.apply(
        lambda r: _classify(
            r["_raw"].get("fii_pct"),
            r["_raw"].get("chg_3m"),
            int(r["Streak (Qtrs)"]),
        ),
        axis=1,
    )
    df = df.drop(columns=["_raw", "_sid"])
    print(
        f"  History snapshots stored: {len(merged)} "
        f"({merged['Ticker'].nunique()} tickers, "
        f"{merged['AsOf'].nunique()} quarter-ends)"
    )
    sd = df["Streak (Qtrs)"].value_counts().sort_index()
    print("  Streak distribution: " + ", ".join(f"{int(k)}Q={int(v)}" for k, v in sd.items()))
    return df


# ─── HNI / superstar holdings (Screener.in /people/ pages) ──────────────

# Logged-in Screener.in "People" pages. Each page lists a single investor's
# quarter-by-quarter stake in every company they hold >1%. We compare the
# latest two quarters per row to flag "New Entry" / "Increased" / "Decreased" /
# "Exited".  Only stakes above the 1% disclosure threshold appear at all.
HNI_PAGE_TTL_HOURS = 12  # these pages only change when a filing lands
HNI_PEOPLE_URLS = [
    "https://www.screener.in/people/127736/ashish-kacholia/",
    "https://www.screener.in/people/148535/bengal-finance-and-investment-pvt-ltd/",
    "https://www.screener.in/people/64/bengal-finance-and-ninvestment-private-limited/",
    "https://www.screener.in/people/19205/suryavanshi-commotrade-private-limited/",
    "https://www.screener.in/people/133451/bengal-finance-investment-p-ltd/",
    "https://www.screener.in/people/153475/rba-finance-investment-co-partnership-firm/",
    "https://www.screener.in/people/2350/suresh-kumar-agarwal/",
    "https://www.screener.in/people/163158/vijay-kishanlal-kedia/",
    "https://www.screener.in/people/134160/vijay-kedia/",
    "https://www.screener.in/people/7379/kedia-secuirities-private-limited/",
    "https://www.screener.in/people/123054/venkata-nagaraju-padala/",
    "https://www.screener.in/people/33390/rohan-gupta/",
    "https://www.screener.in/people/21712/ajay-kumar-aggarwal/",
    "https://www.screener.in/people/71485/nibe-ganesh-ramesh/",
    "https://www.screener.in/people/108142/laroia-mona/",
    "https://www.screener.in/people/174015/india-equity-fund-1/",
    "https://www.screener.in/people/131338/shalu-aggarwal/",
    "https://www.screener.in/people/170071/akash-bhanshali/",
    "https://www.screener.in/people/30960/madhuri-madhusudan-kela/",
    "https://www.screener.in/people/86419/madhusudhan-murlidhar-kela/",
    "https://www.screener.in/people/32876/madhusudan-murlidhar-kela/",
    "https://www.screener.in/people/154329/mahi-madhusudan-kela/",
    "https://www.screener.in/people/35415/cohesion-mk-best-ideas-sub-trust/",
    "https://www.screener.in/people/150091/singularity-equity-fund-i/",
    "https://www.screener.in/people/126373/chartered-finance-leasing-limited/",
    "https://www.screener.in/people/162189/vq-fastercap-fund/",
    "https://www.screener.in/people/21426/steadview-capital-mauritius-limited/",
    "https://www.screener.in/people/141932/valuequest-s-c-a-l-e-fund/",
    "https://www.screener.in/people/6066/asha-mukul-agrawal/",
    "https://www.screener.in/people/168570/sanshi-fund-i/",
    "https://www.screener.in/people/98486/ms-param-capital/",
    "https://www.screener.in/people/127829/mukul-mahavir-agrawal/",
    "https://www.screener.in/people/116773/bijal-pritesh-vora/",
    "https://www.screener.in/people/180470/ritu-bapna/",
    "https://www.screener.in/people/119660/manish-grover/",
    "https://www.screener.in/people/78663/nalanda-india-fund-limited/",
    "https://www.screener.in/people/73618/nalanda-india-equity-fund-limited/",
    "https://www.screener.in/people/23593/sandeep-singh/",
    "https://www.screener.in/people/161937/reina-ra-jaisinghani/",
    "https://www.screener.in/people/78665/kunjal-lalitkumar-patel/",
    "https://www.screener.in/people/679/ajay-upadhyaya/",
    "https://www.screener.in/people/392/vanjana-sundar-iyer/",
    "https://www.screener.in/people/126875/malabar-india-fund-limited/",
    "https://www.screener.in/people/131169/goldman-sachs-funds-goldman-sachs-asia-equity-portfolio/",
    "https://www.screener.in/people/129685/goldman-sachc-funds-goldman-sachs-india-equity-portfolio/",
    "https://www.screener.in/people/19335/goldman-sachs-funds-goldman-sachsindia-equity-p/",
    "https://www.screener.in/people/98375/goldman-sachs-investments-mauritius-i-limited/",
    "https://www.screener.in/people/181599/goldman-sachs-bank-europe-se/",
    "https://www.screener.in/people/149987/massachusetts-institute-of-techno/",
]


def _parse_pct(text):
    """Parse a percent cell like '2.13' or '' into float or None."""
    t = (text or "").strip().rstrip("%").replace(",", "")
    if not t or t == "-":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _extract_ticker_from_href(href):
    """Pull a usable ticker symbol out of a Screener.in /company/<x>/ link."""
    if not href:
        return ""
    m = re.search(r"/company/([^/]+)/", href)
    return m.group(1) if m else ""


def _fetch_hni_page(url):
    """Fetch one Screener.in /people/<id>/ page. Returns a list of dicts, one per
    holding the investor moved between the last two quarters on the page, flagged
    "New Entry", "Increased", "Decreased" or "Exited". Unchanged holdings are
    skipped.

    Caveat on "Exited": these pages only list stakes above the 1% disclosure
    threshold, so an exit here means "fell below 1%", not necessarily a full sale
    — and a company that has not yet filed its latest shareholding will look the
    same as one that was sold.

    Cached for HNI_PAGE_TTL_HOURS. The page only moves when a shareholding
    filing lands, so a same-day repeat run costs no screener.in requests.
    """
    rows_out = []
    try:
        text = screener_client.get(url, ttl_hours=HNI_PAGE_TTL_HOURS)
        if not text:
            print(f"    {url} -> no response")
            return rows_out
        soup = BeautifulSoup(text, "html.parser")

        h1 = soup.find("h1")
        hni_name = h1.get_text(strip=True) if h1 else url.rstrip("/").rsplit("/", 1)[-1]

        # Find the holdings table (one whose header row has quarter labels)
        holdings_table = None
        for t in soup.find_all("table"):
            header_tr = t.find("tr")
            if not header_tr:
                continue
            labels = [c.get_text(strip=True) for c in header_tr.find_all(["th", "td"])]
            if any(_parse_qtr_label(l) for l in labels):
                holdings_table = t
                break
        if holdings_table is None:
            return rows_out

        header_cells = holdings_table.find("tr").find_all(["th", "td"])
        quarters = [c.get_text(strip=True) for c in header_cells][1:]
        if len(quarters) < 2:
            return rows_out
        latest_qtr, prev_qtr = quarters[-1], quarters[-2]

        for tr in holdings_table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            name_cell = cells[0]
            link = name_cell.find("a")
            stock_name = name_cell.get_text(strip=True)
            ticker = _extract_ticker_from_href(link.get("href", "") if link else "")
            vals = [c.get_text(strip=True) for c in cells[1:]]
            if len(vals) < 2:
                continue
            # A blank cell means "not disclosed above 1%", which is how both an
            # entry and an exit show up on these pages.
            latest = _parse_pct(vals[-1]) or 0.0
            prev = _parse_pct(vals[-2]) or 0.0
            if latest == 0 and prev == 0:
                continue  # not held in either quarter
            if prev == 0:
                flag = "New Entry"
            elif latest == 0:
                flag = "Exited"
            elif latest > prev:
                flag = "Increased"
            elif latest < prev:
                flag = "Decreased"
            else:
                continue  # held, unchanged
            rows_out.append({
                "HNI": hni_name,
                "Stock Name": stock_name,
                "Ticker": ticker,
                "Latest %": latest,
                "Previous %": prev,
                "Change (pp)": round(latest - prev, 2),
                "Flag": flag,
                "Latest Quarter": latest_qtr,
                "Previous Quarter": prev_qtr,
            })
    except Exception as e:
        print(f"    {url} -> error: {e}")
    return rows_out


def fetch_hni_holdings():
    """Login to Screener.in, scrape each HNI /people/ page, return a DataFrame of
    every holding that moved in the latest quarter — bought (New Entry,
    Increased) and sold (Decreased, Exited).

    Deliberately exempt from MIN_MARKET_CAP_CR: the point of this sheet is what
    a named investor traded, at any size.
    """
    user, pwd = _load_screener_creds()
    if not user:
        print("HNI scrape skipped: Screener.in credentials missing in .env")
        return pd.DataFrame()

    print(f"\nFetching HNI / superstar holdings ({len(HNI_PEOPLE_URLS)} investors)...")
    if not screener_client.login_ok():
        print("  Screener.in login failed; HNI sheet skipped.")
        return pd.DataFrame()

    all_rows = []
    for i, url in enumerate(HNI_PEOPLE_URLS, 1):
        rows = _fetch_hni_page(url)
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        print(f"  [{i}/{len(HNI_PEOPLE_URLS)}] {slug[:40]:40s} {len(rows)} moves")
        all_rows.extend(rows)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    flag_order = {"New Entry": 0, "Increased": 1, "Decreased": 2, "Exited": 3}
    df["_o"] = df["Flag"].map(flag_order).fillna(99).astype(int)
    # Rank by size of the move, so the sell blocks lead with the biggest cuts.
    df["_mag"] = df["Change (pp)"].abs()
    df = df.sort_values(["_o", "_mag"], ascending=[True, False])
    df = df.drop(columns=["_o", "_mag"]).reset_index(drop=True)
    counts = df["Flag"].value_counts()
    print(f"  HNI moves total: {len(df)} ("
          + ", ".join(f"{counts.get(f, 0)} {f.lower()}"
                      for f in ("New Entry", "Increased", "Decreased", "Exited"))
          + ")")
    return df


# ─── Excel export ─────────────────────────────────────────────────────────────────────

def save_to_excel(df, output_prefix, hni_df=None):
    """Save FII stake tracker results to Excel."""
    excel_path = os.path.join(SCRIPT_DIR, f"{output_prefix}.xlsx")

    cat_list = [
        "New Entry",
        "4-Quarter Increasing",
        "3-Quarter Increasing",
        "Multi-Quarter Increasing",
        "Increased Stake",
    ]
    # Per-sheet filters. Streak sheets are now EXCLUSIVE (each stock appears
    # in exactly one streak bucket). Sequence: New Entry -> 1Q -> 2Q -> 3Q -> 4Q.
    # New Entry also requires FII stake > 1% to filter out negligible entries.
    SHEET_SPECS = [
        ("New_Entry",
            lambda d: d[(d["Category"] == "New Entry") & (d["FII Stake (%)"] > 1.0)],
            ["Change 9M (pp)", "Change 12M (pp)"]),
        ("1-Quarter_Increasing",
            lambda d: d[d["Category"] == "Increased Stake"],
            ["Change 9M (pp)", "Change 12M (pp)"]),
        ("2-Quarter_Increasing",
            lambda d: d[(d["Streak (Qtrs)"] == 2) & (d["Category"] != "New Entry")],
            ["Change 9M (pp)", "Change 12M (pp)"]),
        ("3-Quarter_Increasing",
            lambda d: d[(d["Streak (Qtrs)"] == 3) & (d["Category"] != "New Entry")],
            ["Change 12M (pp)"]),
        ("4-Quarter_Increasing",
            lambda d: d[(d["Streak (Qtrs)"] >= 4) & (d["Category"] != "New Entry")],
            []),
    ]

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        # Summary: classification rules + per-sheet counts
        summary_rows = [
            ("Universe filter:", f"Market Cap > ₹{MIN_MARKET_CAP_CR} Cr "
                                 f"(FII sheets; HNIs exempt)"),
            ("", ""),
            ("Classification rules (applied in order):", ""),
            ("  if prev_qtr < 0.05", '-> "New Entry"'),
            ("  elif streak >= 4", '-> "4-Quarter Increasing"'),
            ("  elif streak == 3", '-> "3-Quarter Increasing"'),
            ("  elif streak == 2", '-> "Multi-Quarter Increasing" (2-Quarter)'),
            ("  elif streak == 1", '-> "Increased Stake" (1-Quarter)'),
            ("", ""),
            ("Sheet filters (exclusive):", ""),
            ("  New_Entry", "Category = New Entry AND FII Stake > 1%"),
            ("  1-Quarter_Increasing", "Category = Increased Stake"),
            ("  2-Quarter_Increasing", "Streak = 2 AND Category != New Entry"),
            ("  3-Quarter_Increasing", "Streak = 3 AND Category != New Entry"),
            ("  4-Quarter_Increasing", "Streak >= 4 AND Category != New Entry"),
            ("", ""),
            ("HNIs sheet flags (latest quarter vs previous):", ""),
            ("  New Entry", "not disclosed before, held now (crossed 1%)"),
            ("  Increased", "stake up"),
            ("  Decreased", "stake down, still above 1%"),
            ("  Exited", "held before, no longer disclosed (fell below 1%)"),
            ("", ""),
            ("Sheet counts:", ""),
        ]
        for sheet_name, selector, _ in SHEET_SPECS:
            summary_rows.append((sheet_name, len(selector(df))))
        summary_rows.append(("Total (all categories)", len(df)))
        pd.DataFrame(summary_rows, columns=["Category", "Count"]).to_excel(
            writer, sheet_name="Summary", index=False
        )

        # Per-sheet slices (exclusive) with column trimming
        for sheet_name, selector, drop_cols in SHEET_SPECS:
            sub = selector(df)
            if sub.empty:
                continue
            if "Streak (Qtrs)" in sub.columns:
                sub = sub.sort_values(
                    ["Streak (Qtrs)", "Change QoQ (pp)"], ascending=[False, False]
                )
            drop = [c for c in drop_cols if c in sub.columns]
            sub = sub.drop(columns=drop)
            sub.to_excel(writer, sheet_name=sheet_name[:31], index=False)

        # HNI / superstar activity in the latest quarter, buys and sells
        if hni_df is not None and not hni_df.empty:
            hni_df.to_excel(writer, sheet_name="HNIs", index=False)

        # Auto-fit column widths
        for ws in writer.book.worksheets:
            for col in ws.columns:
                max_len = max(
                    len(str(cell.value or "")) for cell in col
                )
                col_letter = col[0].column_letter
                ws.column_dimensions[col_letter].width = min(max_len + 3, 45)

    print(f"\nExcel saved: {excel_path}")
    return excel_path


# ─── Entry points ────────────────────────────────────────────────────────────


def get_sheets():
    """Return FII stake + HNI sheets as a dict of DataFrames (for BulkBlock integration).
    Does NOT write its own Excel file."""
    SHEET_SPECS = [
        ("FII_New_Entry",
            lambda d: d[(d["Category"] == "New Entry") & (d["FII Stake (%)"] > 1.0)],
            ["Change 9M (pp)", "Change 12M (pp)"]),
        ("FII_1Q_Increasing",
            lambda d: d[d["Category"] == "Increased Stake"],
            ["Change 9M (pp)", "Change 12M (pp)"]),
        ("FII_2Q_Increasing",
            lambda d: d[(d["Streak (Qtrs)"] == 2) & (d["Category"] != "New Entry")],
            ["Change 9M (pp)", "Change 12M (pp)"]),
        ("FII_3Q_Increasing",
            lambda d: d[(d["Streak (Qtrs)"] == 3) & (d["Category"] != "New Entry")],
            ["Change 12M (pp)"]),
        ("FII_4Q_Increasing",
            lambda d: d[(d["Streak (Qtrs)"] >= 4) & (d["Category"] != "New Entry")],
            []),
    ]

    # HNI pages first — see run() for why.
    hni_df = pd.DataFrame()
    try:
        hni_df = fetch_hni_holdings()
    except Exception as e:
        print(f"  HNI fetch failed: {e}")

    df = fetch_fii_stake_data()
    if df.empty:
        return {}

    sheets = {}
    for sheet_name, selector, drop_cols in SHEET_SPECS:
        sub = selector(df)
        if sub.empty:
            continue
        # Sort by Stock Name ascending
        if "Stock Name" in sub.columns:
            sub = sub.sort_values("Stock Name", ascending=True)
        elif "Streak (Qtrs)" in sub.columns:
            sub = sub.sort_values(
                ["Streak (Qtrs)", "Change QoQ (pp)"], ascending=[False, False]
            )
        drop = [c for c in drop_cols if c in sub.columns]
        sub = sub.drop(columns=drop)
        sheets[sheet_name] = sub.reset_index(drop=True)

    if hni_df is not None and not hni_df.empty:
        sheets["HNIs"] = hni_df

    return sheets


def run(output_prefix="fii_stake_tracker"):
    """Main entry point (for run_all.py integration).

    Returns (df, excel_path).
    """
    # HNI pages first: they need a logged-in screener.in session, and any
    # later bulk fetching is the thing most likely to get the IP throttled.
    try:
        hni_df = fetch_hni_holdings()
    except Exception as e:
        print(f"HNI fetch failed: {e}")
        hni_df = pd.DataFrame()

    df = fetch_fii_stake_data()

    if df.empty:
        print("No data to export.")
        return df, None

    # Print summary
    print(f"\n{'='*60}")
    print("FII Stake Tracker — Summary")
    print(f"{'='*60}")
    for cat in [
        "New Entry",
        "4-Quarter Increasing",
        "3-Quarter Increasing",
        "Multi-Quarter Increasing",
        "Increased Stake",
    ]:
        count = len(df[df["Category"] == cat])
        print(f"  {cat:30s}: {count:>5}")
    print(f"  {'Total':30s}: {len(df):>5}")
    print(f"{'='*60}")

    excel_path = save_to_excel(df, output_prefix, hni_df=hni_df)
    return df, excel_path


def main():
    parser = argparse.ArgumentParser(
        description="FII Stake Tracker — identify FII new entries & increasing stakes"
    )
    parser.add_argument(
        "-o", "--output", default="fii_stake_tracker",
        help="Output file prefix (default: fii_stake_tracker)"
    )
    args = parser.parse_args()
    run(output_prefix=args.output)


if __name__ == "__main__":
    main()
