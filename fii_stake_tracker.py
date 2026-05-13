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

    # Classify — we only have QoQ change, no 6M change from Screener
    for idx, row in df.iterrows():
        fii_pct = row["FII Stake (%)"]
        chg_3m = row["Change QoQ (pp)"]
        chg_6m = 0  # not available from Screener
        df.at[idx, "Category"] = _classify(fii_pct, chg_3m, chg_6m)

    cat_order = {"New Entry": 0, "Multi-Quarter Increasing": 1, "Increased Stake": 2}
    df["_sort"] = df["Category"].map(cat_order)
    df = df.sort_values(["_sort", "Change QoQ (pp)"], ascending=[True, False])
    df = df.drop(columns=["_sort"]).reset_index(drop=True)

    return df


# ─── Core logic ──────────────────────────────────────────────────────────────

def _classify(fii_pct, chg_3m, chg_6m):
    """Classify the FII stake change pattern.

    Returns one of:
      'New Entry'                — FII had zero holding before this quarter
      'Multi-Quarter Increasing' — FII has been increasing for > 1 quarter
      'Increased Stake'          — FII increased stake this quarter only
    """
    # New entry: current holding roughly equals the 3M change
    # (i.e. previous quarter holding was ~0)
    prev_qtr = fii_pct - chg_3m
    if prev_qtr < 0.05:  # practically zero before
        return "New Entry"

    # Multi-quarter: 6M change > 3M change AND both positive
    # means they also increased in the quarter before last
    if chg_6m > chg_3m and chg_3m > 0 and chg_6m > 0:
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

        category = _classify(fii_pct, chg_3m, chg_6m)

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
            "Category": category,
            "Sector": sector,
        })

    df = pd.DataFrame(rows)

    # Sort: New Entry first, then Multi-Quarter Increasing, then Increased
    cat_order = {"New Entry": 0, "Multi-Quarter Increasing": 1, "Increased Stake": 2}
    df["_sort"] = df["Category"].map(cat_order)
    df = df.sort_values(["_sort", "Change QoQ (pp)"], ascending=[True, False])
    df = df.drop(columns=["_sort"]).reset_index(drop=True)

    return df


# ─── Excel export ────────────────────────────────────────────────────────────

def save_to_excel(df, output_prefix):
    """Save FII stake tracker results to Excel."""
    excel_path = os.path.join(SCRIPT_DIR, f"{output_prefix}.xlsx")

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        # Summary counts
        summary_data = {
            "Category": ["New Entry", "Multi-Quarter Increasing",
                         "Increased Stake", "Total"],
            "Count": [
                len(df[df["Category"] == "New Entry"]),
                len(df[df["Category"] == "Multi-Quarter Increasing"]),
                len(df[df["Category"] == "Increased Stake"]),
                len(df),
            ],
        }
        pd.DataFrame(summary_data).to_excel(
            writer, sheet_name="Summary", index=False
        )

        # All stocks — full detail
        df.to_excel(writer, sheet_name="FII Stake Increase", index=False)

        # Separate sheets per category
        for cat in ["New Entry", "Multi-Quarter Increasing", "Increased Stake"]:
            cat_df = df[df["Category"] == cat]
            if not cat_df.empty:
                sheet_name = cat.replace(" ", "_")[:31]  # Excel 31-char limit
                cat_df.to_excel(writer, sheet_name=sheet_name, index=False)

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
    for cat in ["New Entry", "Multi-Quarter Increasing", "Increased Stake"]:
        count = len(df[df["Category"] == cat])
        print(f"  {cat:30s}: {count:>5}")
    print(f"  {'Total':30s}: {len(df):>5}")
    print(f"{'='*60}")

    excel_path = save_to_excel(df, output_prefix)
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
