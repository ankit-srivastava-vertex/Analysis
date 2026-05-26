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
     to get all stocks where FII holding increased in the last quarter.
   - Paginates in batches of 200, with 0.3s delay between requests.
   - Fetches 19 data fields per stock (price, PE, PB, EPS, ROE, ROCE,
     D/E, revenue growth, EPS growth 5Y, 1M return vs Nifty, 200D SMA,
     pledged %, face value, market cap, FII holding %, QoQ & 6M changes).
   - Typically returns ~3,400 stocks covering all listed equities.

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

3. **Classification**
   Each stock is categorized by examining its FII holding history:
   - "New Entry" — previous quarter FII stake was near zero (< 0.05%).
     Detected when current FII % minus QoQ change ≈ 0.
   - "Multi-Quarter Increasing" — FII has been increasing for 2+ quarters.
     Detected when 6M change > 3M change and both are positive.
     (Not available from Screener.in fallback since 6M data is missing.)
   - "Increased Stake" — FII increased this quarter but not necessarily
     in prior quarters. Default category for all other positive changes.

4. **Sorting**
   Results are sorted by category priority (New Entry → Multi-Quarter
   Increasing → Increased Stake), then by QoQ change descending within
   each category.

5. **Excel Export**
   Produces a multi-sheet Excel workbook with auto-fitted column widths:
   - Sheet 1: "Summary" — count of stocks per category + total.
   - Sheet 2: "FII Stake Increase" — all stocks, full detail.
   - Sheet 3: "New_Entry" — only new FII entries.
   - Sheet 4: "Multi-Quarter_Increasing" — only multi-quarter risers.
   - Sheet 5: "Increased_Stake" — only single-quarter increases.

DATA SOURCES
------------
Primary:
- Tickertape Screener API — https://api.tickertape.in/screener/query
  Undocumented public JSON API (stable 3+ years). Covers all NSE/BSE
  listed equities including SME. Provides latest quarterly shareholding
  pattern data as filed with exchanges. No authentication required.
  Note: Premium fields (RSI, 200D EMA) return 403; 200D SMA is used instead.

Fallback (if Tickertape fails):
- Screener.in saved screen — https://www.screener.in/screens/3192887/fii-0/
  Query: "Change in FII holding > 0". Requires login (credentials from
  .env: SCREENER_USER / SCREENER_PASS). Returns ~960 stocks across ~20
  HTML pages. Fewer columns; no sector info or 6M holding change.

OUTPUT COLUMNS (21)
-------------------
  Stock Name | Ticker | Price (₹) | Market Cap (₹ Cr) | Face Value |
  PE (TTM) | PB | EPS (₹) | ROE (%) | ROCE (%) | D/E |
  Revenue Growth (%) | EPS Growth 5Y (%) | 1M Return vs Nifty (%) |
  200D SMA | Pledged (%) | FII Stake (%) | Change QoQ (pp) |
  Change 6M (pp) | Category | Sector

USAGE
-----
Individual run:
    python3 fii_stake_tracker.py                  # default output
    python3 fii_stake_tracker.py -o my_report     # custom output prefix

Group run (via run_all.py):
    Scenario name: fii_stake_tracker
    Called as: fii_stake_tracker.run()
    Skip with: python3 run_all.py --skip fii_stake_tracker

ENVIRONMENT
-----------
.env file (required only for Screener.in fallback):
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
import re

import requests
import pandas as pd
from bs4 import BeautifulSoup

# ─── Config ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
API_URL = "https://api.tickertape.in/screener/query"
PAGE_SIZE = 200          # max results per API call
RATE_LIMIT_DELAY = 0.3   # seconds between paginated requests

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


# ─── Screener.in fallback ────────────────────────────────────────────────────

SCREENER_LOGIN_URL = "https://www.screener.in/login/"
SCREENER_SCREEN_URL = "https://www.screener.in/screens/3192887/fii-0/"
SCREENER_RATE_DELAY = 0.5  # seconds between page requests


def _load_screener_creds():
    """Load Screener.in credentials from .env file."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
    except ImportError:
        pass
    user = (os.getenv("SCREENER_USER") or "").strip("'\" ")
    pwd = (os.getenv("SCREENER_PASS") or "").strip("'\" ")
    if not user or not pwd:
        return None, None
    return user, pwd


def _screener_login(session, user, pwd):
    """Login to Screener.in, return True on success."""
    r = session.get(SCREENER_LOGIN_URL, timeout=15)
    soup = BeautifulSoup(r.text, "html.parser")
    csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    if not csrf_input:
        return False
    csrf = csrf_input["value"]
    r2 = session.post(
        SCREENER_LOGIN_URL,
        data={"csrfmiddlewaretoken": csrf, "username": user, "password": pwd},
        headers={"Referer": SCREENER_LOGIN_URL},
        timeout=15,
    )
    return "/login/" not in r2.url


def _parse_screener_number(text):
    """Parse a number from Screener cell text like '1,234.56' or '12.34%'."""
    text = text.strip().replace(",", "").replace("%", "").replace("\xa0", "")
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _scrape_screener_page(session, page_num):
    """Scrape one page of the saved Screener.in screen. Returns list of row dicts."""
    url = SCREENER_SCREEN_URL
    params = {"page": page_num} if page_num > 1 else {}
    r = session.get(url, params=params, timeout=20)
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
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

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })

    print("  Logging in to Screener.in ...")
    if not _screener_login(session, user, pwd):
        print("  Screener.in login failed.")
        return pd.DataFrame()
    print("  Login successful.")

    all_rows = []
    page = 1
    first_page_count = None
    while True:
        print(f"  Fetching page {page} ...", end="", flush=True)
        rows = _scrape_screener_page(session, page)
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
        time.sleep(SCREENER_RATE_DELAY)

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
SHP_REQUEST_DELAY = 0.4

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


def _fetch_screener_shp(ticker, session):
    """Fetch full quarterly FII shareholding history for a ticker from Screener.in.

    Returns dict: {quarter_end_date: fii_pct}. Cached on disk for 7 days.
    No login required — company pages are public.
    """
    import json
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
            r = session.get(f"https://www.screener.in{path}", timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
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

    Returns a pandas DataFrame with classified FII stake changes.
    """
    # ── Primary: Tickertape ──
    try:
        df = _fetch_fii_tickertape()
        if not df.empty:
            return df
    except Exception as e:
        print(f"  Tickertape failed: {e}")

    # ── Fallback: Screener.in ──
    print("\nFalling back to Screener.in ...")
    try:
        df = fetch_fii_stake_data_screener()
        if not df.empty:
            print(f"  Screener.in returned {len(df)} records.")
            return df
    except Exception as e:
        print(f"  Screener.in fallback also failed: {e}")

    return pd.DataFrame()


def _fetch_fii_tickertape():
    """Fetch FII stake data from Tickertape (primary source)."""
    session = _create_session()

    print("Fetching stocks where FII increased stake (last quarter) ...")
    print("  Source: Tickertape Screener API")
    # Filter: FII holding change in last 3 months > 0
    match = {"forInstHldng3M": {"g": 0}}
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

    # First-pass streak (using only Tickertape-derived snapshots + prior history)
    streaks = _build_streak_lookup(merged, asof_q0)
    df["Streak (Qtrs)"] = df["Ticker"].map(streaks).fillna(0).astype(int)

    # For candidates with streak >= 2, fetch full quarterly FII history from
    # Screener.in to determine whether the streak actually extends to 3 or 4+.
    extend_targets = (
        df.loc[df["Streak (Qtrs)"] >= 2, "Ticker"].dropna().unique().tolist()
    )
    if extend_targets:
        print(
            f"  Fetching Screener.in shareholding history for "
            f"{len(extend_targets)} multi-quarter candidates ..."
        )
        shp_session = requests.Session()
        shp_session.headers.update({"User-Agent": HEADERS["User-Agent"]})
        extra_snaps = []
        cached_hits = 0
        for i, ticker in enumerate(extend_targets, 1):
            cache_path = os.path.join(SHP_CACHE_DIR, f"{ticker}.json")
            was_cached = (
                os.path.exists(cache_path)
                and (time.time() - os.path.getmtime(cache_path)) < SHP_CACHE_TTL_DAYS * 86400
            )
            shp = _fetch_screener_shp(ticker, shp_session)
            if was_cached:
                cached_hits += 1
            else:
                time.sleep(SHP_REQUEST_DELAY)
            for qe, v in shp.items():
                extra_snaps.append((ticker, qe, v))
            if i % 50 == 0 or i == len(extend_targets):
                print(f"    {i}/{len(extend_targets)}  (cache hits: {cached_hits})")
        if extra_snaps:
            merged = _merge_history(
                merged,
                pd.DataFrame(extra_snaps, columns=["Ticker", "AsOf", "FII_Pct"]),
            )
            streaks = _build_streak_lookup(merged, asof_q0)
            df["Streak (Qtrs)"] = df["Ticker"].map(streaks).fillna(0).astype(int)

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
    df = df.drop(columns=["_raw"])
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
# latest two quarters per row to flag "New Entry" / "Increased".
HNI_PEOPLE_URLS = [
    "https://www.screener.in/people/127736/ashish-kacholia/",
    "https://www.screener.in/people/148535/bengal-finance-and-investment-pvt-ltd/",
    "https://www.screener.in/people/163158/vijay-kishanlal-kedia/",
    "https://www.screener.in/people/123054/venkata-nagaraju-padala/",
    "https://www.screener.in/people/33390/rohan-gupta/",
    "https://www.screener.in/people/21712/ajay-kumar-aggarwal/",
    "https://www.screener.in/people/71485/nibe-ganesh-ramesh/",
    "https://www.screener.in/people/108142/laroia-mona/",
    "https://www.screener.in/people/174015/india-equity-fund-1/",
    "https://www.screener.in/people/168570/sanshi-fund-i/",
    "https://www.screener.in/people/131338/shalu-aggarwal/",
    "https://www.screener.in/people/170071/akash-bhanshali/",
    "https://www.screener.in/people/150091/singularity-equity-fund-i/",
    "https://www.screener.in/people/126373/chartered-finance-leasing-limited/",
    "https://www.screener.in/people/162189/vq-fastercap-fund/",
    "https://www.screener.in/people/44707/ankush-kedia/",
    "https://www.screener.in/people/56652/sandeep-kapadia/",
    "https://www.screener.in/people/21426/steadview-capital-mauritius-limited/",
    "https://www.screener.in/people/141932/valuequest-s-c-a-l-e-fund/",
    "https://www.screener.in/people/98486/ms-param-capital/",
    "https://www.screener.in/people/127829/mukul-mahavir-agrawal/",
    "https://www.screener.in/people/116773/bijal-pritesh-vora/",
    "https://www.screener.in/people/180470/ritu-bapna/",
    "https://www.screener.in/people/161937/reina-ra-jaisinghani/",
    "https://www.screener.in/people/78665/kunjal-lalitkumar-patel/",
    "https://www.screener.in/people/64/bengal-finance-and-ninvestment-private-limited/",
    "https://www.screener.in/people/679/ajay-upadhyaya/",
    "https://www.screener.in/people/2350/suresh-kumar-agarwal/",
    "https://www.screener.in/people/134160/vijay-kedia/",
    "https://www.screener.in/people/392/vanjana-sundar-iyer/",
    "https://www.screener.in/people/126875/malabar-india-fund-limited/",
    "https://www.screener.in/people/150899/nav-capital-vcc-nav-capital-emerging-star-fund/",
    "https://www.screener.in/people/169381/mansi-share-and-stock-broking-private-limited/",
    "https://www.screener.in/people/74548/amansa-holdings-private-limited/",
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


def _fetch_hni_page(session, url):
    """Fetch one Screener.in /people/<id>/ page. Returns list of dicts where
    the investor newly entered OR increased stake in the latest quarter."""
    rows_out = []
    try:
        r = None
        for attempt in range(3):
            r = session.get(url, timeout=20)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            break
        if r is None or r.status_code != 200:
            print(f"    {url} -> HTTP {r.status_code if r else 'no-response'}")
            return rows_out
        soup = BeautifulSoup(r.text, "html.parser")

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
            latest = _parse_pct(vals[-1])
            prev = _parse_pct(vals[-2])
            if latest is None or latest == 0:
                continue  # not held in latest quarter
            if prev is None or prev == 0:
                flag = "New Entry"
            elif latest > prev:
                flag = "Increased"
            else:
                continue  # held but flat / decreased
            rows_out.append({
                "HNI": hni_name,
                "Stock Name": stock_name,
                "Ticker": ticker,
                "Latest %": latest,
                "Previous %": prev if prev is not None else 0.0,
                "Change (pp)": round(latest - (prev or 0.0), 2),
                "Flag": flag,
                "Latest Quarter": latest_qtr,
                "Previous Quarter": prev_qtr,
            })
    except Exception as e:
        print(f"    {url} -> error: {e}")
    return rows_out


def fetch_hni_holdings():
    """Login to Screener.in, scrape each HNI /people/ page, return a DataFrame
    of new-entry / increased holdings in the latest quarter."""
    user, pwd = _load_screener_creds()
    if not user:
        print("HNI scrape skipped: Screener.in credentials missing in .env")
        return pd.DataFrame()

    session = requests.Session()
    session.headers.update({"User-Agent": HEADERS["User-Agent"]})
    print(f"\nFetching HNI / superstar holdings ({len(HNI_PEOPLE_URLS)} investors)...")
    if not _screener_login(session, user, pwd):
        print("  Screener.in login failed; HNI sheet skipped.")
        return pd.DataFrame()

    all_rows = []
    for i, url in enumerate(HNI_PEOPLE_URLS, 1):
        rows = _fetch_hni_page(session, url)
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        print(f"  [{i}/{len(HNI_PEOPLE_URLS)}] {slug[:40]:40s} +{len(rows)} buys")
        all_rows.extend(rows)
        time.sleep(SCREENER_RATE_DELAY)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    flag_order = {"New Entry": 0, "Increased": 1}
    df["_o"] = df["Flag"].map(flag_order).fillna(99).astype(int)
    df = df.sort_values(["_o", "Change (pp)"], ascending=[True, False])
    df = df.drop(columns=["_o"]).reset_index(drop=True)
    print(f"  HNI buys total: {len(df)} "
          f"({(df['Flag'] == 'New Entry').sum()} new, "
          f"{(df['Flag'] == 'Increased').sum()} increased)")
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

        # HNI / superstar buys (new entry + increased in latest quarter)
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

    df = fetch_fii_stake_data()
    if df.empty:
        return {}

    sheets = {}
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
        sheets[sheet_name] = sub.reset_index(drop=True)

    # HNI holdings
    try:
        hni_df = fetch_hni_holdings()
        if hni_df is not None and not hni_df.empty:
            sheets["HNIs"] = hni_df
    except Exception as e:
        print(f"  HNI fetch failed: {e}")

    return sheets


def run(output_prefix="fii_stake_tracker"):
    """Main entry point (for run_all.py integration).

    Returns (df, excel_path).
    """
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

    # Fetch HNI / superstar holdings (Screener.in /people/ pages)
    try:
        hni_df = fetch_hni_holdings()
    except Exception as e:
        print(f"HNI fetch failed: {e}")
        hni_df = pd.DataFrame()

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
