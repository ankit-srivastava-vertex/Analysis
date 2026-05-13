"""
ipo_anchor_tracker.py
=====================

PURPOSE
-------
Build a one-shot Excel report of every IPO listed on NSE / NSE-SME in the
last N months (default 15), with the listing-day move and a flag for any
anchor investor that matches a hard-coded WATCHLIST (the operator's tracked
funds / promoters / HNIs).  The report is meant to spot which recent IPOs
were backed by "smart money" and how they performed on day one.

OUTPUT (single Excel `ipo_anchor_report.xlsx`, two sheets)
----------------------------------------------------------
Sheet `IPOs`  -- one row per IPO, newest first:
    1. Stock Name           : Company legal name as given by NSE
    2. Listing Date         : YYYY-MM-DD (date of first trade)
    3. Bourse(s)            : "NSE" or "NSE SME"
    4. IPO Price (Rs)       : Issue price (upper band if a range was given)
    5. Listing Day +/-      : "Positive (+x.xx%)" / "Negative (-x.xx%)" /
                              "Flat (+/-x.xx%)" / "NA"  (vs issue price,
                              measured at listing-day Close)
    6. Watchlist Anchors    : ";" separated WATCHLIST names that appear in
                              the IPO's anchor allocation table (empty if
                              none matched or anchor table not retrievable)
    7. _NSE Symbol          : NSE ticker (diagnostic)
    8. _Chittorgarh URL     : Source URL used for anchors (diagnostic)
    9. _Notes               : Free-text remark, e.g. "chittorgarh URL not
                              found" when the lookup failed
Sheet `Notes` -- prose methodology notes for downstream readers.

WATCHLIST
---------
Hard-coded list near the top of this file (~85 entries).  Includes Abakkus
funds, the Kacholia / Kedia families and their holding co's, Madhusudan
Kela vehicles (Cohesion / Singularity / Founders Collective / India Equity
Fund 1 / Sanshi / Chartered Finance & Leasing), Goldman Sachs name
variants, Malabar India, MIT, Nalanda, Mukul Agrawal / Param Capital,
Smallcap World Fund variants, SBI Life, Steadview, Valuequest / VQ
Fastercap, Oxbow, etc.  Edit the WATCHLIST list literal to add / remove.

DATA SOURCES (selected for multi-year stability)
------------------------------------------------
* IPO list   : NSE public past-issues API
               https://www.nseindia.com/api/public-past-issues?index=equities
               Returns ~1300 records covering NSE Main board (securityType
               "EQ") and NSE SME (securityType "SME").  Filtered to keep
               only equity offerings whose symbol starts with a letter
               (drops NCDs / bonds that NSE labels as SME but use
               digit-leading symbols like "808CIFCL29").
               *Limitation*: BSE-only IPOs (those that did NOT also list
               on NSE) are NOT in the report.  Acknowledged in `Notes`.
* Listing $$ : Repo's own `data_provider.download()` (Angel One -> jugaad
               -> yfinance chain) with a final yfinance `<sym>.NS` /
               `<sym>.BO` fallback.  Returns the listing-day Close, which
               is then compared to the IPO issue price.
* Anchors    : chittorgarh.com.  Two-step lookup:
               (a) Fetch the IPO sitemap once
                   (https://www.chittorgarh.com/google_sitemap_urlredirect.asp?a=27)
                   and build a {slug -> URL} index of ~1958 IPOs.
               (b) For each IPO convert the company name to a slug, look
                   it up (with progressive trailing-word trimming and a
                   substring fallback), then GET the corresponding
                   /ipo_subscription/{slug}/{id}/ page and parse the first
                   `<table>` whose header contains "Anchor".
               *Limitation*: chittorgarh publicly displays only the FIRST
               2 anchor allottees per IPO -- the rest are paywalled.
               Hence Watchlist Anchors is best-effort: a hit means the
               name is definitely there; a miss can mean either "not
               present" OR "present but past row 2".

WORKFLOW (orchestrated by `main()`)
-----------------------------------
[1/4] Fetch NSE past-issues -> filter to last N months & equity offerings
      -> build a list of `IPO` dataclasses sorted newest first.
[2/4] For each IPO: download listing-day OHLC, compute pct vs issue price,
      assign "Positive" / "Negative" / "Flat" / "NA" label.
[3/4] For each IPO: resolve chittorgarh URL -> scrape anchor table ->
      normalize each anchor name and intersect with the normalized
      WATCHLIST -> attach matches to the row.  (`--no-anchors` skips this
      whole step, useful for a quick listing-only refresh.)
[4/4] Write the Excel with the IPOs sheet + a methodology Notes sheet.

NORMALIZATION & MATCHING
------------------------
`_norm()` strips unicode marks, upper-cases, replaces every non-alnum run
with a single space, and collapses whitespace.  `match_watchlist()` then
checks each scraped anchor name `n` against every normalized watchlist
key `w` and accepts a hit if `w in n` OR `n in w`.  This tolerates the
common cases ("Goldman Sachs Funds - Goldman Sachs India Equity Portfolio"
vs the variants we list, "SMALLCAP WORLD FUND INC" vs "SMALLCAP WORLD
FUND INC.", trailing "LIMITED" vs "LTD", etc.).

CACHING (.cache/ipo_tracker/)
-----------------------------
Every network call is memoized to disk as JSON keyed by URL/symbol:
    nse_past_issues               6h  TTL    (NSE list refresh)
    listret_<SYM>_<YYYYMMDD>      30d TTL    (listing-day Close per IPO)
    chittorgarh_sitemap_v1        7d  TTL    (slug -> URL index)
    chit_<slug>_<id>              30d TTL    (anchor table per IPO)
Delete the folder -- or just selected files -- to force a re-pull.

NETWORK MANNERS
---------------
NSE blocks bare requests; `nse_session()` warms cookies by hitting
nseindia.com first.  REQUEST_SLEEP=1.2s between chittorgarh calls.

CLI USAGE
---------
    python3 ipo_anchor_tracker.py                 # default 15 months
    python3 ipo_anchor_tracker.py --months 24
    python3 ipo_anchor_tracker.py --no-anchors    # skip anchor scrape (fast)
    python3 ipo_anchor_tracker.py --limit 10      # only first 10 IPOs (debug)
    python3 ipo_anchor_tracker.py --out path.xlsx # custom output path

OUTPUT
------
    ipo_anchor_report.xlsx (written next to this script by default).

DEPENDENCIES
------------
    requests, beautifulsoup4, pandas, openpyxl, yfinance (for fallback).
    All standard pip; no system binaries required.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Watchlist (provided by user; case-insensitive substring match after normalization)
# ---------------------------------------------------------------------------
WATCHLIST: list[str] = [
    'ABAKKUS ASSET MANAGER LLP',
    'ABAKKUS ASSET MANAGER LLP(HDFC CUSTODY)',
    'ABAKKUS ASSET MANAGER PRIVATE LIMITED',
    'ABAKKUS DIVERSIFIED ALPHA FUND',
    'ABAKKUS DIVERSIFIED ALPHA FUND-2',
    'ABAKKUS EMERGING OPPORTUNITIES FUND - 1',
    'ABAKKUS GROWTH FUND - 1',
    'ABAKKUS GROWTH FUND-2',
    'AJAY KUMAR AGGARWAL',
    'AJAY UPADHYAYA',
    'UPADHYAYA AJAY',
    'UPADHYAYA AJAY SHIV NARAYAN',
    'AKASH BHANSHALI',
    'Ankit Vijay Kedia',
    'Vijay Krishanlal Kedia',
    'Kedia Secuirities Private Limited',
    'ANKUSH  KEDIA',
    'ANKUSH KEDIA',
    'ASHISH KACHOLIA',
    'ASHISH RAMESH KACHOLIA',
    'ASHISH RAMESHCHANDRA KACHOLIA',
    'BENGAL FIN. & INV. PVT. LTD',
    'SURYAVANSHI COMMOTRADE PVT LTD',
    'Suryavanshi Commotrade Private Limited',
    'HIMALAYA FINANCE & INV. CO',
    'HIMALAYA FINANCE & INVESTMENT COMPANY',
    'HIMALAYA FINANCE AND INVESTMENT CO',
    'KACHOLIA ASHISH',
    'LUCKY INVESTMENT MANAGERS PRIVATE LIMITED',
    'R.B.A. FINANCE ## INVESTMENT CO.',
    'R.B.A.FINANCE & INVT. CO',
    'Suresh Kumar Agarwal',
    'GOLDMAN SACHS (SINGAPORE) PTE',
    'GOLDMAN SACHS (SINGAPORE) PTE.- ODI',
    'GOLDMAN SACHS COLLECTIVE TRUST - EMERGING MARKETS EQUITY EX CHINA FUND',
    'GOLDMAN SACHS COLLECTIVE TRUST - EMERGING MARKETS EQUITY EX. CHINA FUND',
    'GOLDMAN SACHS FDS GOLDMAN SACHS INDIA EQ PORTFOLIO',
    'GOLDMAN SACHS FUNDS  GOLDMAN SACHS INDIA EQUITY PORTFOLIO',
    'GOLDMAN SACHS FUNDS - GOLDMAN SACHS INDIA EQUITY PORTFOLIO',
    'GOLDMAN SACHS FUNDS GOLDMAN SACHS INDIA EQUITY PORTFOLIO',
    'GOLDMAN SACHS FUNDS-GOLDMAN SACHS ASIA EQUITY PORTFOLIO',
    'GOLDMAN SACHS INDIA LIMITED',
    'GOLDMAN SACHS INVESTMENT (MAURITIUS) I LTD',
    'GOLDMAN SACHS INVESTMENTS (MAURITIUS) I LIMITED',
    'GOLDMAN SACHS INVESTMENTS HOLDINGS ASIA LIMITED',
    'GOLDMAN SACHS INVESTMENTS MAURITIUS  I LIMITED',
    'GOLDMAN SACHS INVESTMENTS MAURITIUS  I LTD',
    'GOLDMAN SACHS INVESTMENTS MAURITIUS I LIMITED',
    'GOLDMAN SACHS TRUST II - GOLDMAN SACHS GQG PARTNERS INTERNATIONAL OPPORTUNITIES FUND',
    'GOLDMANSACHS FUNDS GOLDMANSACHS INDIA EQUITY PORTFOLIO',
    'INDIA EQUITY FUND 1',
    'MADHURI MADHUSUDAN KELA',
    'COHESION MK BEST IDEAS SUB-TRUST',
    'FOUNDERS COLLECTIVE FUND',
    'SINGULARITY EQUITY FUND I',
    'SINGULARITY LARGE VALUE FUND II',
    'SINGULARITY LARGE VALUE FUND III',
    'Chartered Finance & Leasing Limited',
    'Madhusudan Murlidhar Kela',
    'LAROIA MONA',
    'MONA LAROIA',
    'BIJAL PRITESH VORA',
    'MALABAR INDIA FUND LIMITED',
    'MASSACHUSETTS INSTITUTE OF TECHNOLOGY',
    'MANISH GROVER',                    # Jeena Sikho promoter
    'ROHAN GUPTA',                      # SG Finserve promoter
    'NALANDA INDIA EQUITY FUND LIMITED',
    'NALANDA INDIA FUND LIMITED',
    'OXBOW MASTER FUND LIMITED',
    'QRG INVESTMENTS AND HOLDINGS LIMITED',
    'RITU BAPNA',
    'SANDEEP SINGH',
    'Sandeep Kapadia',
    'KAPADIA SANDEEP',
    'Mukul Mahavir Agrawal',
    'SANSHI FUND-I',
    'PARAM CAPITAL',
    'Asha Mukul Agrawal',
    'SANDEEP KAPADIA',
    'SBI LIFE INSURANCE COMPANY LIMITED',
    'SBI LIFE INSURANCE COMPANY LTD',
    'SHALU  AGGARWAL',
    'SIXTEENTH STREET ASIAN GEMS FUND',
    'SMALL CAP WORLD FUND INC',
    'SMALLCAP WORLD FUND INC',
    'SMALLCAP WORLD FUND INC.',
    'SMALLCAPWORLD FUND INC',
    'SMALLER CAP WORLD FUND INC',
    'STEADVIEW CAPITAL MASTER FUND LTD.',
    'STEADVIEW CAPITAL MAURITIUS LIMITED',
    'STEADVIEW CAPITAL OPPORTUNITIES PCC',
    'VANAJA SUNDAR IYER',
    'VENKATA NAGARAJU PADALA',
    'VINOD  KUMAR',
    'Valuequest S C A L E Fund',
    'VQ FASTERCAP FUND',
    # Variants discovered from 1Y historical analysis (May 2026)
    'ABAKKUS EMERGING OPPORTUNITIES FUND-1',
    'ABAKKUS DIVERSIFIED ALPHA FUND - 2',
    'SINGULARITY LARGE VALUE FUND I',
    'SURYA VANSHI COMMOTRADE PVT. LTD.',
    'CHARTERED FINANCE & LEASI NG LIMITED',
    'BENGAL FINANCE & INVESTMENT PRIVATE LIMITED',
    'VALUEQUEST INVESTMENT ADVISORS PVT LTD',
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
CACHE_DIR = SCRIPT_DIR / ".cache" / "ipo_tracker"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
NSE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

REQUEST_SLEEP = 1.2  # polite delay between network calls

# ---------------------------------------------------------------------------
# Tiny disk cache
# ---------------------------------------------------------------------------
def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:200]
    return CACHE_DIR / safe

def cache_get(key: str, ttl_seconds: int) -> Any | None:
    p = _cache_path(key)
    if not p.exists():
        return None
    if time.time() - p.stat().st_mtime > ttl_seconds:
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None

def cache_set(key: str, value: Any) -> None:
    _cache_path(key).write_text(json.dumps(value, default=str))


# ---------------------------------------------------------------------------
# NSE session (warm cookies)
# ---------------------------------------------------------------------------
_session: requests.Session | None = None

def nse_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(NSE_HEADERS)
        # Warm cookies (NSE rejects requests without them)
        for url in ("https://www.nseindia.com",
                    "https://www.nseindia.com/market-data/all-upcoming-issues-ipo"):
            try:
                s.get(url, timeout=20)
                time.sleep(0.5)
            except Exception:
                pass
        _session = s
    return _session


# ---------------------------------------------------------------------------
# Step 1 — fetch IPO list from NSE
# ---------------------------------------------------------------------------
@dataclass
class IPO:
    company: str
    symbol: str
    listing_date: datetime | None
    issue_price: float | None
    price_range: str
    bourses: list[str] = field(default_factory=list)
    listing_pct: float | None = None
    listing_label: str = "NA"
    anchor_matches: list[str] = field(default_factory=list)
    chittorgarh_url: str = ""
    notes: str = ""


def fetch_nse_past_issues() -> list[dict]:
    cached = cache_get("nse_past_issues", ttl_seconds=6 * 3600)
    if cached is not None:
        return cached
    s = nse_session()
    r = s.get("https://www.nseindia.com/api/public-past-issues?index=equities",
              timeout=30)
    r.raise_for_status()
    data = r.json()
    cache_set("nse_past_issues", data)
    return data


def parse_listing_date(raw: str) -> datetime | None:
    if not raw or raw == "-":
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_issue_price(raw_price: str, price_range: str) -> float | None:
    """Issue price comes either as a number ('Rs.123') or '-' if cancelled.
    Fallback to upper end of price range."""
    def _num(s: str) -> float | None:
        m = re.search(r"(\d+(?:\.\d+)?)", s.replace(",", ""))
        return float(m.group(1)) if m else None

    if raw_price and raw_price not in ("-", ""):
        v = _num(raw_price)
        if v:
            return v
    if price_range:
        # "Rs.95 to Rs.100"
        nums = re.findall(r"(\d+(?:\.\d+)?)", price_range.replace(",", ""))
        if nums:
            return float(nums[-1])  # take upper band
    return None


def collect_ipos(months_back: int) -> list[IPO]:
    cutoff = datetime.now() - timedelta(days=months_back * 30)
    raw = fetch_nse_past_issues()
    out: list[IPO] = []
    for r in raw:
        ld = parse_listing_date(r.get("listingDate", ""))
        if ld is None or ld < cutoff:
            continue
        sec_type = (r.get("securityType") or "").upper()
        # Keep only equity offerings (mainboard EQ + SME). Skip NCDs, InvITs etc.
        if sec_type not in ("EQ", "SME"):
            continue
        symbol = (r.get("symbol") or "").upper()
        # NSE often labels NCDs/bonds with SME securityType but their symbols
        # start with a digit (e.g. "10MWL29" = 10% coupon Mangalam 2029).
        # Real equity tickers are alphabetic-leading.
        if not symbol or not symbol[0].isalpha():
            continue
        ip = parse_issue_price(r.get("issuePrice", ""), r.get("priceRange", ""))
        bourses = ["NSE SME"] if sec_type == "SME" else ["NSE"]
        out.append(IPO(
            company=r.get("companyName") or r.get("company") or "",
            symbol=symbol,
            listing_date=ld,
            issue_price=ip,
            price_range=r.get("priceRange", ""),
            bourses=bourses,
        ))
    # Sort newest first
    out.sort(key=lambda x: x.listing_date or datetime.min, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Step 2 — listing-day return (positive / negative)
# ---------------------------------------------------------------------------
def listing_return(ipo: IPO) -> tuple[float | None, str]:
    """Return (pct, label). Tries the project's data_provider first; if that
    fails (or symbol not on Angel/jugaad), falls back to yfinance ``.NS``."""
    if ipo.issue_price is None or ipo.listing_date is None:
        return None, "NA"
    cached = cache_get(f"listret_{ipo.symbol}_{ipo.listing_date:%Y%m%d}",
                       ttl_seconds=30 * 24 * 3600)
    if cached is not None:
        return cached["pct"], cached["label"]

    close = _try_close(ipo)
    if close is None:
        return None, "NA"
    pct = (close - ipo.issue_price) / ipo.issue_price * 100.0
    label = "Positive" if pct > 0.5 else ("Negative" if pct < -0.5 else "Flat")
    cache_set(f"listret_{ipo.symbol}_{ipo.listing_date:%Y%m%d}",
              {"pct": pct, "label": label})
    return pct, label


def _try_close(ipo: IPO) -> float | None:
    # Try project's data_provider (Angel -> jugaad -> yfinance fallback chain).
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        import data_provider  # type: ignore
        start = ipo.listing_date.strftime("%Y-%m-%d")
        end = (ipo.listing_date + timedelta(days=4)).strftime("%Y-%m-%d")
        df = data_provider.download(ipo.symbol, start, end)
        if df is not None and not df.empty:
            row = df.iloc[0]
            for col in ("Close", "close", "CLOSE"):
                if col in df.columns:
                    return float(row[col])
    except Exception:
        pass
    # Plain yfinance fallback.
    try:
        import yfinance as yf  # type: ignore
        for suffix in (".NS", ".BO"):
            t = yf.Ticker(ipo.symbol + suffix)
            hist = t.history(start=ipo.listing_date.strftime("%Y-%m-%d"),
                             end=(ipo.listing_date + timedelta(days=5))
                                  .strftime("%Y-%m-%d"))
            if hist is not None and not hist.empty:
                return float(hist["Close"].iloc[0])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Step 3 — anchor investors via chittorgarh
# ---------------------------------------------------------------------------
_SITEMAP_INDEX: dict[str, str] | None = None  # normalized-slug -> full URL


def _load_chittorgarh_sitemap() -> dict[str, str]:
    """One-time load of chittorgarh's IPO sitemap. Returns a map
    {normalized-slug-without-suffix: full https URL}. Cached for 7 days."""
    global _SITEMAP_INDEX
    if _SITEMAP_INDEX is not None:
        return _SITEMAP_INDEX
    cached = cache_get("chittorgarh_sitemap_v1", ttl_seconds=7 * 24 * 3600)
    if cached:
        _SITEMAP_INDEX = cached
        return _SITEMAP_INDEX
    print("      (loading chittorgarh IPO sitemap, ~one-time)...")
    r = requests.get(
        "https://www.chittorgarh.com/google_sitemap_urlredirect.asp?a=27",
        headers={"User-Agent": UA}, timeout=40)
    urls = re.findall(
        r"https?://www\.chittorgarh\.com/ipo/[a-z0-9\-]+/\d+/?", r.text)
    idx: dict[str, str] = {}
    for u in urls:
        m = re.search(r"/ipo/([a-z0-9\-]+)/(\d+)", u)
        if not m:
            continue
        slug = m.group(1)
        # Drop the conventional "-ipo" suffix to match company names better
        norm_slug = re.sub(r"-ipo$", "", slug)
        # Store latest occurrence (last wins; most recent IPO ID)
        idx[norm_slug] = u.rstrip("/") + "/"
    cache_set("chittorgarh_sitemap_v1", idx)
    _SITEMAP_INDEX = idx
    print(f"      sitemap indexed {len(idx)} IPO URLs.")
    return idx


def _company_to_slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"&", " and ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    # Strip common trailing words that chittorgarh slugs omit
    for tail in ("-limited", "-ltd", "-private-limited", "-pvt-ltd",
                 "-india-limited", "-india-ltd", "-corporation",
                 "-company", "-co", "-india"):
        if s.endswith(tail):
            s = s[: -len(tail)]
    return s


def find_chittorgarh_url(company: str) -> str | None:
    """Look the company up in chittorgarh's own sitemap. Tries an exact slug
    match first, then progressively trims trailing words."""
    idx = _load_chittorgarh_sitemap()
    slug = _company_to_slug(company)
    if not slug:
        return None
    # 1. exact
    if slug in idx:
        return idx[slug]
    # 2. progressive prefix shrink (drop trailing word at a time)
    parts = slug.split("-")
    for i in range(len(parts), 1, -1):
        cand = "-".join(parts[:i])
        if cand in idx:
            return idx[cand]
    # 3. substring fallback: any slug containing all leading words of company
    head = "-".join(parts[:3]) if len(parts) >= 3 else slug
    matches = [u for k, u in idx.items() if head and head in k]
    return matches[0] if matches else None


def fetch_anchor_table(detail_url: str) -> list[str]:
    """Convert /ipo/{slug}/{id}/ -> /ipo_subscription/{slug}/{id}/ and parse
    the anchor allocation table. Returns a flat list of anchor-name strings.
    """
    m = re.match(r"https?://www\.chittorgarh\.com/ipo/([a-z0-9\-]+)/(\d+)/?",
                 detail_url)
    if not m:
        return []
    slug, _id = m.group(1), m.group(2)
    sub_url = f"https://www.chittorgarh.com/ipo_subscription/{slug}/{_id}/"
    key = f"chit_{slug}_{_id}"
    cached = cache_get(key, ttl_seconds=30 * 24 * 3600)
    if cached is not None:
        return cached
    names: list[str] = []
    try:
        r = requests.get(sub_url, headers={"User-Agent": UA}, timeout=25)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            for tbl in soup.find_all("table"):
                head_row = tbl.find("tr")
                if not head_row:
                    continue
                cols = [c.get_text(strip=True).lower()
                        for c in head_row.find_all(["th", "td"])]
                # The anchor allocation table has an "Anchor" column (and
                # often "Group Entity"). Skip summary / category tables.
                if "anchor" not in cols:
                    continue
                idx = cols.index("anchor")
                for tr in tbl.find_all("tr")[1:]:
                    cells = [td.get_text(strip=True)
                             for td in tr.find_all(["td", "th"])]
                    if len(cells) > idx and cells[idx]:
                        names.append(cells[idx])
                break  # first matching table is the right one
    except Exception:
        pass
    time.sleep(REQUEST_SLEEP)
    cache_set(key, names)
    return names


# ---------------------------------------------------------------------------
# Step 4 — match against watchlist
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_NORM_WATCH: dict[str, str] = {_norm(w): w for w in WATCHLIST}
_NORM_WATCH_KEYS: list[str] = list(_NORM_WATCH.keys())


def match_watchlist(anchor_names: list[str]) -> list[str]:
    out: set[str] = set()
    for raw in anchor_names:
        n = _norm(raw)
        if not n:
            continue
        for w in _NORM_WATCH_KEYS:
            if w in n or n in w:
                out.add(_NORM_WATCH[w])
    return sorted(out)


# ---------------------------------------------------------------------------
# Step 5 — Excel writer
# ---------------------------------------------------------------------------
def write_excel(rows: list[IPO], path: Path) -> None:
    df = pd.DataFrame([{
        "Stock Name":         r.company,
        "Listing Date":       r.listing_date.strftime("%Y-%m-%d") if r.listing_date else "",
        "Bourse(s)":          ", ".join(r.bourses),
        "IPO Price (Rs)":     r.issue_price if r.issue_price is not None else "",
        "Listing Day +/-":    r.listing_label + (
            f" ({r.listing_pct:+.2f}%)" if r.listing_pct is not None else ""),
        "Watchlist Anchors":  "; ".join(r.anchor_matches),
        # diagnostics (hidden if you delete)
        "_NSE Symbol":        r.symbol,
        "_Chittorgarh URL":   r.chittorgarh_url,
        "_Notes":             r.notes,
    } for r in rows])
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="IPOs", index=False)
        ws = xw.sheets["IPOs"]
        for i, col in enumerate(df.columns, start=1):
            width = max(12, min(60, df[col].astype(str).map(len).max() + 2))
            ws.column_dimensions[chr(64 + i) if i <= 26 else "A" + chr(64 + i - 26)].width = width
        # Notes / methodology sheet
        notes = pd.DataFrame({"Notes": [
            "Source of IPO list: NSE public-past-issues API "
            "(https://www.nseindia.com/api/public-past-issues?index=equities). "
            "Covers NSE main board (securityType=EQ) and NSE SME (SME). "
            "BSE-only IPOs that did not also list on NSE are NOT in this report.",
            "Listing-day +/-: close on listing day vs IPO issue price, via the "
            "project's data_provider (Angel One -> jugaad-data -> yfinance "
            "fallback chain).",
            "Anchor list: chittorgarh.com /ipo_subscription page (public free "
            "table). Note: chittorgarh displays only the first 2 anchor "
            "allottees publicly, so the Watchlist Anchors column is best-effort.",
            "Re-runs are cached in .cache/ipo_tracker/ for 7-30 days. Delete that "
            "folder to force a refresh.",
        ]})
        notes.to_excel(xw, sheet_name="Notes", index=False)
        xw.sheets["Notes"].column_dimensions["A"].width = 110


# ---------------------------------------------------------------------------
# Programmatic API (used by run_all.py orchestrator)
# ---------------------------------------------------------------------------
def _ipos_to_dataframe(ipos: list[IPO]) -> pd.DataFrame:
    """Build the IPOs sheet dataframe from the in-memory list."""
    return pd.DataFrame([{
        "Stock Name":         r.company,
        "Listing Date":       r.listing_date.strftime("%Y-%m-%d") if r.listing_date else "",
        "Bourse(s)":          ", ".join(r.bourses),
        "IPO Price (Rs)":     r.issue_price if r.issue_price is not None else "",
        "Listing Day +/-":    r.listing_label + (
            f" ({r.listing_pct:+.2f}%)" if r.listing_pct is not None else ""),
        "Watchlist Anchors":  "; ".join(r.anchor_matches),
        "_NSE Symbol":        r.symbol,
        "_Chittorgarh URL":   r.chittorgarh_url,
        "_Notes":             r.notes,
    } for r in ipos])


def _notes_dataframe() -> pd.DataFrame:
    return pd.DataFrame({"Notes": [
        "Source of IPO list: NSE public-past-issues API "
        "(https://www.nseindia.com/api/public-past-issues?index=equities). "
        "Covers NSE main board (securityType=EQ) and NSE SME (SME). "
        "BSE-only IPOs that did not also list on NSE are NOT in this report.",
        "Listing-day +/-: close on listing day vs IPO issue price, via the "
        "project's data_provider (Angel One -> jugaad-data -> yfinance "
        "fallback chain).",
        "Anchor list: chittorgarh.com /ipo_subscription page (public free "
        "table). Note: chittorgarh displays only the first 2 anchor "
        "allottees publicly, so the Watchlist Anchors column is best-effort.",
        "Re-runs are cached in .cache/ipo_tracker/ for 7-30 days. Delete that "
        "folder to force a refresh.",
    ]})


def run(months: int = 14, limit: int = 0, fetch_anchors: bool = True,
        tv_txt_path: str | Path | None = None) -> dict:
    """Programmatic entry-point for run_all.py.

    Returns a dict:
        {
          "sheets":   {"IPOs": <DataFrame>, "Notes": <DataFrame>},
          "tv_path":  <Path to TradingView .txt watchlist that was written>,
          "ipos":     <list of IPO dataclass instances>,
        }

    The TradingView .txt file is always written (default: next to this
    script as ipo_anchor_report.txt). The Excel is NOT written here -- the
    caller decides whether to merge `sheets` into a unified workbook.
    """
    print(f"\n[1/3] Fetching NSE past-issues (last {months} months)...")
    ipos = collect_ipos(months)
    if limit:
        ipos = ipos[: limit]
    print(f"      Found {len(ipos)} IPOs in window.\n")

    print("[2/3] Computing listing-day returns...")
    for i, ipo in enumerate(ipos, 1):
        pct, label = listing_return(ipo)
        ipo.listing_pct, ipo.listing_label = pct, label
        if i % 10 == 0 or i == len(ipos):
            print(f"      {i:>4}/{len(ipos)}  {ipo.symbol:>10}  {label}")

    if not fetch_anchors:
        print("\n[3/3] fetch_anchors=False: skipping anchor scrape.")
    else:
        print("\n[3/3] Fetching anchor lists from chittorgarh...")
        for i, ipo in enumerate(ipos, 1):
            url = find_chittorgarh_url(ipo.company)
            if not url:
                ipo.notes = "chittorgarh URL not found"
            else:
                ipo.chittorgarh_url = url
                anchors = fetch_anchor_table(url)
                ipo.anchor_matches = match_watchlist(anchors)
            if i % 10 == 0 or i == len(ipos):
                print(f"      {i:>4}/{len(ipos)}  {ipo.symbol:<10}  "
                      f"matches={len(ipo.anchor_matches)}")

    # Always write the TradingView watchlist (kept as a separate file).
    tv_path = Path(tv_txt_path) if tv_txt_path else (
        SCRIPT_DIR / "ipo_anchor_report.txt")
    tv_lines = [f"NSE:{ipo.symbol}" for ipo in ipos if ipo.symbol]
    tv_path.write_text(",\n".join(tv_lines))
    print(f"\n      TradingView watchlist -> {tv_path}  ({len(tv_lines)} symbols)")

    matched = sum(1 for x in ipos if x.anchor_matches)
    print(f"      Total IPOs: {len(ipos)}   With watchlist anchor: {matched}")

    return {
        "sheets": {"IPOs": _ipos_to_dataframe(ipos), "Notes": _notes_dataframe()},
        "tv_path": tv_path,
        "ipos": ipos,
    }


# ---------------------------------------------------------------------------
# CLI orchestrator
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--months", type=int, default=14,
                    help="Look-back window in months (default: 14)")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only first N IPOs (debug; 0 = all)")
    ap.add_argument("--no-anchors", action="store_true",
                    help="Skip anchor scrape (fast; column will be blank)")
    ap.add_argument("--out",
                    default=str(SCRIPT_DIR / "ipo_anchor_report.xlsx"),
                    help="Output Excel path (default: alongside this script)")
    args = ap.parse_args()

    out_path = Path(args.out).resolve()
    tv_path = out_path.with_suffix(".txt")

    result = run(months=args.months, limit=args.limit,
                 fetch_anchors=not args.no_anchors, tv_txt_path=tv_path)

    print(f"\n[4/4] Writing Excel -> {out_path}")
    write_excel(result["ipos"], out_path)

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
