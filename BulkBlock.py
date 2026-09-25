"""
Bulk & Block Deals Scraper (NSE + BSE)
=======================================


SUMMARY
-------
Two modes over the same four feeds (NSE bulk, NSE block, BSE bulk, BSE block):

* DAILY (default) — latest trading session only. Applies two independent
  filters, writes eight Excel sheets, emails the report. This is what
  ``run_all.py`` drives and its behaviour is unchanged.
* RANGE (``--from``/``--to``) — arbitrary historical window. Dumps RAW,
  UNFILTERED deals into one Excel file, plus the same four scrip-filtered
  ``watchlist_*`` views the daily run produces. No email.

Each mode uses a different set of exchange endpoints, and both sets are kept
deliberately: if one is deprecated or blocked, the other still works.

WORKFLOW (daily)
----------------
1. Fetch NSE bulk + block deals from the single-day snapshot endpoint.
   Verified complete — a row-for-row compare against NSE's own CSV archive
   for the same session matched 123/123 with zero rows missing either way.
2. Fetch BSE bulk + block deals.
   Primary: the legacy per-deal-type JSON APIs (BulkDeal_Beta/BlockDeal_Beta).
   Fallback: the range API pinned to a single day. Both are live; the two
   were verified to return identical rows for the same session (87 = 87).
3. Normalise both feeds onto one column schema per exchange.
4. Filter the same four feeds two ways:
   a. by the superstar client names from investor_registry.py (who traded?)
   b. by the hardcoded STOCK_WATCHLIST of scrips (what was traded?)
   Either filter matching nothing yields a one-row "Status" sheet, so "no
   deals today" is never confused with "the fetch failed".
5. Save all deals to Excel with separate sheets:
   nse_bulk, nse_block, bse_bulk, bse_block and the four watchlist_* views.
6. Generate styled HTML email preview table.
7. Send email with Excel attachment via SMTP.

WORKFLOW (range)
----------------
1. BSE — one call per deal type against the range API. No row cap: a
   101-day window returned 5,009 bulk rows across all 73 trading days, and
   per-day counts reconciled exactly against single-day queries.
2. NSE — the CSV form of the historical endpoint. The JSON form of the same
   endpoint is NEVER used: it silently caps at 70 rows and returns only the
   first day of any range.
3. The NSE archive is published with a delay, so on a trading day the
   snapshot can hold deals the archive does not carry yet. When the window
   includes today the snapshot rows are merged in and de-duplicated. The two
   sources were verified to agree exactly wherever they overlap.
4. Every fetch is checked for short coverage — if the newest row returned is
   older than the date requested, a warning is printed rather than the gap
   passing unnoticed.

HOW TO RUN A MULTI-DAY PULL
---------------------------
Step 1. Pick the window. Dates are DD-MM-YYYY and both ends are inclusive.
Step 2. Run, from the project root:

            .venv/bin/python BulkBlock.py --from 01-06-2026 --to 11-09-2026

        Add ``--out <path.xlsx>`` to control the filename, otherwise it
        defaults to ``BULK_BLOCK_Range_<from>_<to>.xlsx`` in the cwd.
Step 3. Watch the console. Each feed prints its row count, its date span and
        its distinct-day count. A ``WARNING: coverage ends <date>`` line
        means the exchange has not published the tail of your window yet.
        Re-run later to fill it.
Step 4. Open the workbook. Four raw sheets — nse_bulk, nse_block, bse_bulk,
        bse_block — each holding every deal in the window, unfiltered; then
        four ``watchlist_*`` sheets carrying only STOCK_WATCHLIST scrips over
        that same window. Filter the raw sheets in Excel for anything else.

Notes:
  * No email is sent in range mode, and the daily workbook is never touched.
  * Long windows are one HTTP call per feed; be considerate re-running them.
  * Omitting both ``--from`` and ``--to`` runs the normal daily job.

DATA SOURCES
------------
Daily:
- NSE snapshot   — /api/snapshot-capital-market-largedeal
- BSE bulk       — https://api.bseindia.com/BseIndiaAPI/api/BulkDeal_Beta/w
- BSE block      — https://api.bseindia.com/BseIndiaAPI/api/BlockDeal_Beta/w
Range:
- NSE historical — /api/historicalOR/bulk-block-short-deals?...&csv=true
- BSE range      — https://api.bseindia.com/BseIndiaAPI/api/BulkblockDeal/w
                   (?fromdt=&todt=&type=1|2&scripcode=, dates DD/MM/YYYY)

OUTPUT
------
- Daily: BULK_BLOCK_Deals_<timestamp>.xlsx — 8 sheets: nse_bulk, nse_block,
  bse_bulk, bse_block (investor filter) + watchlist_nse_bulk,
  watchlist_nse_block, watchlist_bse_bulk, watchlist_bse_block (scrip filter)
  plus an HTML email with styled deal tables.
- Range: BULK_BLOCK_Range_<from>_<to>.xlsx — 8 sheets: nse_bulk, nse_block,
  bse_bulk, bse_block (raw, unfiltered) + the four watchlist_* scrip views.

USAGE
-----
Individual run:
    python3 BulkBlock.py                               # daily, Excel + email
    python3 BulkBlock.py --dry-run                     # daily, preview only
    python3 BulkBlock.py --from 01-06-2026 --to 11-09-2026   # range dump

Group run (via run_all.py):
    Scenario 1 (bulk_block) — deal scraping only; no email.
    Skip with: python3 run_all.py --skip bulk_block

DEPENDENCIES
------------
requests, pandas, openpyxl, smtplib
"""

import os
import io
import re
import sys
import time
import argparse
import traceback
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
import requests
import pandas as pd
from datetime import datetime


# ─── Endpoints ─────────────────────────────────────────────────────────────
# Two independent sets per exchange, kept on purpose: the single-day pair and
# the range pair are separate services, so a deprecation on one side leaves a
# working path on the other.
BSE_DAY_API = {
    "bulk": "https://api.bseindia.com/BseIndiaAPI/api/BulkDeal_Beta/w",
    "block": "https://api.bseindia.com/BseIndiaAPI/api/BlockDeal_Beta/w",
}
BSE_RANGE_API = "https://api.bseindia.com/BseIndiaAPI/api/BulkblockDeal/w"
BSE_RANGE_TYPE = {"bulk": "1", "block": "2"}

NSE_SNAPSHOT_API = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
NSE_RANGE_API = "https://www.nseindia.com/api/historicalOR/bulk-block-short-deals"
# The root of nseindia.com answers 403 to a cold client; this report page does
# not, and hands back the cookies the API calls need.
NSE_WARMUP_URL = "https://www.nseindia.com/report-detail/display-bulk-and-block-deals"


# ─── Column schemas ────────────────────────────────────────────────────────
# Both BSE endpoints are folded onto one schema. They differ in exactly one
# header — the day API spells it "ScripName", the range API "scripname" — and
# that difference alone is enough to break the downstream filters.
BSE_COL_MAP = {
    "DEAL_DATE": "Deal Date",
    "SCRIP_CODE": "Scrip Code",
    "ScripName": "Scrip Name",
    "scripname": "Scrip Name",
    "CLIENT_NAME": "Client Name",
    "TRANSACTION_TYPE": "Buy/Sell",
    "QUANTITY": "Quantity",
    "PRICE": "Price",
}

# NSE's CSV export is folded onto the snapshot's field names so the two can be
# concatenated and filtered by the same code.
NSE_CSV_COL_MAP = {
    "Date": "date",
    "Symbol": "symbol",
    "Security Name": "name",
    "Client Name": "clientName",
    "Buy / Sell": "buySell",
    "Quantity Traded": "qty",
    "Trade Price / Wght. Avg. Price": "watp",
    "Remarks": "remarks",
}


def _bse_normalise(df):
    """Put either BSE endpoint's frame onto the shared column schema."""
    df = df.rename(columns=BSE_COL_MAP)
    return df.drop(columns=["SENDTOWEBSITE"], errors="ignore")


def _parse_deal_dates(series):
    """Parse a deal-date column without knowing which endpoint produced it.

    The four feeds use four different formats: '04 Sep 2026' (BSE range),
    '10/09/2026' (BSE day), '10-Sep-2026' (NSE snapshot) and '03-AUG-2026'
    (NSE CSV).
    """
    s = series.astype(str).str.strip()
    for fmt in ("%d %b %Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%m-%Y"):
        out = pd.to_datetime(s, format=fmt, errors="coerce")
        if out.notna().any():
            return out
    return pd.to_datetime(s, errors="coerce", dayfirst=True)


def _report_coverage(df, date_col, label, to_date=None):
    """Print what a fetch actually covered, and shout if it falls short.

    Silent short coverage is the specific failure mode these feeds exhibit —
    NSE's block archive trails live by several sessions — so the gap is
    surfaced rather than left for the reader to notice.
    """
    if df is None or df.empty:
        print(f"  {label}: no rows")
        return
    dates = _parse_deal_dates(df[date_col]).dropna()
    if dates.empty:
        print(f"  {label}: {len(df)} rows (dates unparseable)")
        return
    print(f"  {label}: {len(df)} rows, {dates.dt.date.nunique()} day(s), "
          f"{dates.min().date()} .. {dates.max().date()}")
    if to_date is not None and dates.max().date() < to_date.date():
        print(f"  WARNING: coverage ends {dates.max().date()}, requested "
              f"through {to_date.date()} — the exchange has not published "
              f"the tail of this window yet.")


def _dedupe_deals(df):
    """Drop rows duplicated across the NSE archive and snapshot feeds."""
    key = pd.DataFrame({
        "d": _parse_deal_dates(df["date"]).dt.date.astype(str),
        "s": df["symbol"].map(_normalise_name),
        "c": df["clientName"].map(_normalise_name),
        "b": df["buySell"].map(_normalise_name),
        "q": pd.to_numeric(df["qty"], errors="coerce"),
        "p": pd.to_numeric(df["watp"], errors="coerce").round(2),
    })
    return df[~key.duplicated()].reset_index(drop=True)


def _date_range(df):
    """Span of deal dates in `df`, rendered for a status message."""
    if df is None or df.empty:
        return None
    for c in df.columns:
        if 'date' in str(c).lower():
            try:
                vals = {str(v).strip() for v in df[c].dropna() if str(v).strip()}
            except Exception:
                return None
            if not vals:
                return None
            ordered = sorted(vals, key=lambda v: (
                _parse_deal_dates(pd.Series([v])).iloc[0], v))
            return ordered[0] if len(ordered) == 1 else f"{ordered[0]} to {ordered[-1]}"
    return None


# ─── Name matching ─────────────────────────────────────────────────────────
def _normalise_name(value):
    """Collapse a client name to a comparable form: upper case, single spaces.

    Both exchanges emit names with inconsistent internal whitespace — BSE
    returned 'SANDEEP  SINGH' where the watchlist carries 'SANDEEP SINGH', and
    62 distinct NSE names have doubled spaces. An exact compare drops those
    deals silently, which is the failure this function exists to prevent.
    """
    return re.sub(r"\s+", " ", str(value)).strip().upper()


def _match_clients(df, column, names):
    """Rows of `df` whose `column` matches `names`, whitespace/case-insensitive.

    Shared by both exchanges and both run modes so the matching rule can never
    drift between them.
    """
    if df is None or df.empty or column not in df.columns:
        return pd.DataFrame()
    wanted = {_normalise_name(n) for n in names}
    return df[df[column].map(_normalise_name).isin(wanted)]


# ─── Stock watchlist ───────────────────────────────────────────────────────
# Mirror image of the superstar-investor filter in BSEScraper.run(): that one
# asks "did these people trade anything?", this one asks "did anyone trade
# these scrips?". Every NSE/BSE bulk and block deal is checked against the
# list below and reported regardless of who the counterparty was.
#
# Tuple layout: (NSE symbol, BSE scrip code, BSE scrip id, company name)
#   - NSE symbol  is None for scrips listed only on BSE.
#   - BSE code/id are None for scrips listed only on NSE (mostly SME) and for
#     BSE Ltd itself, which is not traded on its own exchange.
#   - BSE deals are matched on the numeric scrip CODE, because the BSE feed's
#     "Scrip Name" column carries a short scrip id ("GLAND", "TATATECH"), not
#     the company name. The scrip id is kept as a secondary key for the HTML
#     fallback path, whose columns differ from the JSON API's.
#
# Resolved against the NSE EQUITY_L + SME_EQUITY_L and the BSE ListofScripData
# masters via ISIN, so the symbols/codes are exchange-authoritative rather than
# hand-typed. Re-verify against those masters if you edit this list: a wrong
# symbol fails silently as "no deals".
STOCK_WATCHLIST = [
    ('ADANIPOWER', 533096,  'ADANIPOWER', 'Adani Power Limited'),
    ('ABSLAMC',    543374,  'ABSLAMC',    'Aditya Birla Sun Life AMC Limited'),
    ('AEROFLEX',   543972,  'AEROFLEX',   'Aeroflex Industries Limited'),
    ('AERON',      None,    None,         'Aeron Composite Limited'),
    ('AIMTRON',    None,    None,         'Aimtron Electronics Limited'),
    ('ALLETEC',    None,    None,         'All E Technologies Limited'),
    ('ANAWIL',     None,    None,         'Anawil Wire and Engineering Limited'),
    ('ARDEE',      544860,  'ARDEE',      'Ardee Industries Limited'),
    ('AUGMONT',    544888,  'AUGMONT',    'Augmont Enterprises Limited'),
    ('AURIONPRO',  532668,  'AURIONPRO',  'Aurionpro Solutions Limited'),
    ('AWFIS',      544181,  'AWFIS',      'Awfis Space Solutions Limited'),
    ('BAJEL',      544042,  'BAJEL',      'Bajel Projects Limited'),
    ('BLEL',       544870,  'BLEL',       'Behari Lal Engineering Limited'),
    ('BHADORA',    None,    None,         'Bhadora Industries Limited'),
    ('BDL',        541143,  'BDL',        'Bharat Dynamics Limited'),
    ('BLS',        540073,  'BLS',        'BLS International Services Limited'),
    ('BSE',        None,    None,         'BSE Limited'),
    (None,         544343,  'CNINFOTECH', 'Capitalnumbers Infotech Ltd'),
    ('CHANDAN',    None,    None,         'Chandan Healthcare Limited'),
    ('CMRGREEN',   544777,  'CMRGREEN',   'CMR Green Technologies Limited'),
    ('CREDITACC',  541770,  'CREDITACC',  'CreditAccess Grameen Limited'),
    ('DANISH',     None,    None,         'Danish Power Limited'),
    ('DEEPINDS',   543288,  'DEEPINDS',   'Deep Industries Limited'),
    ('EIMCOELECO', 523708,  'EIMCOELECO', 'Eimco Elecon (India) Limited'),
    ('EMMIL',      None,    None,         'Energy Mission Machineries (India) Limited'),
    ('EXCELSOFT',  544617,  'EXCELSOFT',  'Excelsoft Technologies Limited'),
    ('EXICOM',     544133,  'EXICOM',     'Exicom Tele-Systems Limited'),
    ('FINBUD',     None,    None,         'Finbud Financial Services Limited'),
    ('FUSION',     543652,  'FUSION',     'Fusion Finance Limited'),
    ('GAUDIUMIVF', 544709,  'GAUDIUMIVF', 'Gaudium IVF and Women Health Limited'),
    ('GLASSWLSYS', None,    None,         'Glass Wall System (I) Limited'),
    ('GSFC',       500690,  'GSFC',       'Gujarat State Fertilizers & Chemicals Limited'),
    ('HARIOMPIPE', 543517,  'HARIOMPIPE', 'Hariom Pipe Industries Limited'),
    ('HAVELLS',    517354,  'HAVELLS',    'Havells India Limited'),
    ('HITECH',     543411,  'HITECH',     'Hi-Tech Pipes Limited'),
    ('HINDCOPPER', 513599,  'HINDCOPPER', 'Hindustan Copper Limited'),
    ('HPL',        540136,  'HPL',        'HPL Electric & Power Limited'),
    ('IOC',        530965,  'IOC',        'Indian Oil Corporation Limited'),
    ('INA',        543620,  'INA',        'Insolation Energy Limited'),
    ('ITCHOTELS',  544325,  'ITCHOTELS',  'ITC Hotels Limited'),
    ('ITC',        500875,  'ITC',        'ITC Limited'),
    ('JSFB',       544118,  'JSFB',       'Jana Small Finance Bank Limited'),
    ('JASH',       544402,  'JASH',       'Jash Engineering Limited'),
    ('JAYBEE',     None,    None,         'Jay Bee Laminations Limited'),
    ('JLHL',       543980,  'JLHL',       'Jupiter Life Line Hospitals Limited'),
    ('KARURVYSYA', 590003,  'KARURVYSYA', 'Karur Vysya Bank Limited'),
    ('KEI',        517569,  'KEI',        'KEI Industries Limited'),
    ('KPIGREEN',   542323,  'KPIGREEN',   'KPI Green Energy Limited'),
    (None,         544554,  'KVSCASTING', 'KVS Castings Ltd'),
    ('LALITHAA',   544879,  'LALITHAA',   'Lalithaa Jewellery Mart Limited'),
    ('LAXMIDENTL', 544339,  'LAXMIDENTL', 'Laxmi Dental Limited'),
    ('LAXMIINDIA', 544465,  'LAXMIINDIA', 'Laxmi India Finance Limited'),
    ('LCCPROJECT', None,    None,         'LCC Projects Limited'),
    ('M&M',        500520,  'M&M',        'Mahindra & Mahindra Limited'),
    ('MOLDTKPAC',  533080,  'MOLDTKPAC',  'Mold-Tek Packaging Limited'),
    ('MPIMANIPAL', None,    None,         'Manipal Technologies Limited'),
    ('MSPL',       532650,  'MSPL',       'MSP Steel & Power Limited'),
    ('NEWJAISA',   None,    None,         'Newjaisa Technologies Limited'),
    ('NORTHARC',   544260,  'NORTHARC',   'Northern Arc Capital Limited'),
    ('OMAXE',      532880,  'OMAXE',      'Omaxe Limited'),
    ('PSFL',       None,    None,         'Paramount Speciality Forgings Limited'),
    ('PGEL',       533581,  'PGEL',       'PG Electroplast Limited'),
    ('PHYCHEM',    None,    None,         'Phychem Technologies Limited'),
    ('PRAJIND',    522205,  'PRAJIND',    'Praj Industries Limited'),
    ('PPL',        542684,  'PPL',        'Prakash Pipes Limited'),
    ('PRAMODINI',  None,    None,         'Pramodini Medicare Limited'),
    ('QLINE',      None,    None,         'Q-Line Biotech Limited'),
    (None,         544091,  'QLL',        'Qualitek Labs Ltd'),
    ('RACE',       537785,  'RACE',       'Race Eco Chain Limited'),
    ('RAKSAN',     None,    None,         'Raksan Transformers Limited'),
    ('RPOWER',     532939,  'RPOWER',     'Reliance Power Limited'),
    ('RMC',        540358,  'RMC',        'RMC Switchgears Limited'),
    ('SAHASRA',    None,    None,         'Sahasra Electronic Solutions Limited'),
    ('SANSERA',    543358,  'SANSERA',    'Sansera Engineering Limited'),
    ('SENORES',    544319,  'SENORES',    'Senores Pharmaceuticals Limited'),
    ('SGFIN',      539199,  'SGFIN',      'SG Finserve Limited'),
    ('SGMART',     512329,  'SGMART',     'SG Mart Limited'),
    ('SHALBY',     540797,  'SHALBY',     'Shalby Limited'),
    ('SBCL',       513097,  'SBCL',       'Shivalik Bimetal Controls Limited'),
    ('SKYWAYS',    544890,  'SKYWAYS',    'Skyways Air Services Limited'),
    ('SPIC',       590030,  'SPIC',       'Southern Petrochemicals Industries Corporation Limited'),
    ('STEAMHOUSE', None,    None,         'Steamhouse India Limited'),
    ('SUDEEPPHRM', 544619,  'SUDEEPPHRM', 'Sudeep Pharma Limited'),
    ('SULA',       543711,  'SULA',       'Sula Vineyards Limited'),
    ('SUNPHARMA',  524715,  'SUNPHARMA',  'Sun Pharmaceutical Industries Limited'),
    ('TAC',        None,    None,         'TAC Infosec Limited'),
    ('TATAELXSI',  500408,  'TATAELXSI',  'Tata Elxsi Limited'),
    ('TDPOWERSYS', 533553,  'TDPOWERSYS', 'TD Power Systems Limited'),
    ('TECHERA',    None,    None,         'TechEra Engineering (India) Limited'),
    ('TECHNOCRAF', 544864,  'TECHNOCRAF', 'Technocraft Ventures Limited'),
    ('TGL',        None,    None,         'Teerth Gopicon Limited'),
    ('FEDERALBNK', 500469,  'FEDERALBNK', 'The Federal Bank Limited'),
    ('TITAGARH',   532966,  'TITAGARH',   'Titagarh Rail Systems Limited'),
    ('TRANSRAILL', 544317,  'TRANSRAILL', 'Transrail Lighting Limited'),
    (None,         544531,  'TRUECOLORS', 'True Colors Ltd'),
    ('UFO',        539141,  'UFO',        'UFO Moviez India Limited'),
    ('UNIECOM',    544227,  'UNIECOM',    'Unicommerce Esolutions Limited'),
    ('VIKRAN',     544496,  'VIKRAN',     'Vikran Engineering Limited'),
    ('VPRPL',      543974,  'VPRPL',      'Vishnu Prakash R Punglia Limited'),
    (None,         544219,  'VVIPIL',     'VVIP Infratech Ltd'),
    (None,         539337,  'WAAREE',     'Waaree Technologies Ltd'),
    ('YESBANK',    532648,  'YESBANK',    'Yes Bank Limited'),
]

# Sheet key -> (source DataFrame exchange, human label) for the watchlist views.
WATCHLIST_SHEETS = {
    "watchlist_nse_bulk": ("nse", "NSE bulk"),
    "watchlist_nse_block": ("nse", "NSE block"),
    "watchlist_bse_bulk": ("bse", "BSE bulk"),
    "watchlist_bse_block": ("bse", "BSE block"),
}


def _watchlist_lookups():
    """Return (nse_symbols, bse_codes, bse_scrip_ids) as upper-cased sets.

    Derived from STOCK_WATCHLIST on every call so edits to the list take
    effect without any cache invalidation; the list is ~100 rows, so the cost
    is irrelevant next to the network fetches.
    """
    symbols = {s.strip().upper() for s, _c, _i, _n in STOCK_WATCHLIST if s}
    codes = {int(c) for _s, c, _i, _n in STOCK_WATCHLIST if c}
    scrip_ids = {i.strip().upper() for _s, _c, i, _n in STOCK_WATCHLIST if i}
    return symbols, codes, scrip_ids


def _match_watchlist(df, exchange):
    """Rows of `df` whose scrip is on STOCK_WATCHLIST.

    Parameters
    ----------
    df : pandas.DataFrame
        A raw (unfiltered) deals frame from either exchange.
    exchange : {'nse', 'bse'}
        Selects the matching keys. NSE deals carry a clean `symbol` column.
        BSE deals are matched on the numeric scrip code first — the only key
        the feed exposes that is stable — with the short scrip id as a
        fallback for the HTML-scrape path, whose headers vary.

    Returns
    -------
    pandas.DataFrame
        The matching rows, empty if none. Column names are left untouched.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    symbols, codes, scrip_ids = _watchlist_lookups()
    cols = {str(c).strip().lower(): c for c in df.columns}

    if exchange == "nse":
        col = cols.get("symbol")
        if col is None:
            return df.iloc[0:0]
        return df[df[col].astype(str).str.strip().str.upper().isin(symbols)]

    code_col = next((cols[k] for k in cols if "code" in k), None)
    name_col = next((cols[k] for k in cols
                     if "name" in k and "client" not in k), None)
    mask = pd.Series(False, index=df.index)
    if code_col is not None:
        mask |= pd.to_numeric(df[code_col], errors="coerce").isin(codes)
    if name_col is not None:
        mask |= df[name_col].astype(str).str.strip().str.upper().isin(scrip_ids)
    return df[mask]


def _watchlist_sheet(df, exchange, label, pulled_str, date_range):
    """Build one watchlist sheet, mirroring the investor sheets' conventions.

    Returns either the matching deals or a single-row "Status" frame so the
    sheet is never silently absent: the user must be able to tell "no deals in
    my stocks today" apart from "the feed failed".
    """
    if df is None or df.empty:
        return pd.DataFrame({"Status": [
            f"ERROR: {label} deals fetch failed or returned empty. {pulled_str}."]})

    hits = _match_watchlist(df, exchange)
    if not hits.empty:
        return hits

    dr = date_range(df)
    msg = (f"No deals in watchlist stocks. Total {label} deals fetched: "
           f"{len(df)}. Watchlist size: {len(STOCK_WATCHLIST)} stocks.")
    if dr:
        msg += f" Data date: {dr}."
    msg += f" {pulled_str}."
    return pd.DataFrame({"Status": [msg]})


class BSEScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        })

    def _nse_session(self):
        """Create/refresh a requests session with NSE cookies."""
        if not hasattr(self, '_nse_sess') or self._nse_sess is None:
            s = requests.Session()
            s.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                              'AppleWebKit/537.36 (KHTML, like Gecko) '
                              'Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/csv, */*; q=0.01',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': NSE_WARMUP_URL,
            })
            for attempt in range(3):
                try:
                    r = s.get(NSE_WARMUP_URL, timeout=20)
                    if r.status_code == 200:
                        self._nse_sess = s
                        return s
                except Exception:
                    time.sleep(1 + attempt)
            self._nse_sess = s  # return even without cookies
        return self._nse_sess

    def nse_largedeals(self, mode="bulk_deals"):
        """Fetch the latest session's bulk/block deals from the NSE snapshot.

        Verified complete against NSE's own CSV archive for the same session:
        123 rows on both sides, matching row for row, nothing dropped.
        """
        key = 'BULK_DEALS_DATA' if mode == 'bulk_deals' else 'BLOCK_DEALS_DATA'
        for attempt in range(3):
            try:
                sess = self._nse_session()
                r = sess.get(NSE_SNAPSHOT_API, timeout=15)
                if r.status_code == 401:
                    # Cookie expired — refresh
                    self._nse_sess = None
                    time.sleep(1)
                    continue
                if r.status_code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                r.raise_for_status()
                payload = r.json()
                data = payload.get(key, [])
                if data:
                    print(f"  ✓ NSE {mode}: {len(data)} deals fetched")
                    return pd.DataFrame(data)
                return None
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                else:
                    print(f"  ⚠️ NSE {mode} fetch failed after 3 attempts: {e}")
        return None

    def fetch_bse_deals_api(self, deal_type="bulk"):
        """Latest BSE session's bulk/block deals.

        Primary: the legacy per-deal-type JSON API, which always returns the
        most recent session and ignores any date parameters.
        Fallback: the range API over the trailing week, reduced to its newest
        date. The week window rather than today's date is deliberate — it has
        to behave like "latest session" on holidays and before the day's file
        is published.
        """
        label = f"BSE {deal_type.title()} Deals"
        url = BSE_DAY_API.get(deal_type)
        try:
            print(f"\n{'='*100}")
            print(f"Fetching: {label} (day API)")
            print(f"URL: {url}")
            print(f"{'='*100}\n")

            rows = self._bse_get(url)
            if not rows:
                raise ValueError("day API returned no rows")
            df = _bse_normalise(pd.DataFrame(rows))
            print(f"\u2713 Fetched {len(df)} {deal_type} deals from BSE day API")
            print(f"\u2713 Columns: {list(df.columns)}")
            return df
        except Exception as e:
            print(f"\u274c BSE day API failed for {label}: {e}")

        try:
            print(f"  Falling back to the BSE range API for {label} ...")
            today = datetime.now()
            df = self.fetch_bse_deals_range(
                deal_type, today - pd.Timedelta(days=7), today, quiet=True)
            if df is not None and not df.empty:
                latest = _parse_deal_dates(df["Deal Date"]).max()
                df = df[_parse_deal_dates(df["Deal Date"]) == latest]
                print(f"  \u2713 BSE {deal_type} deals: {len(df)} fetched "
                      f"(range API, {latest.date()})")
                return df
        except Exception as e2:
            print(f"  \u26a0\ufe0f BSE range fallback also failed: {e2}")

        print(f"  \u26a0\ufe0f BSE {deal_type} deals: no data available")
        return None

    def _bse_get(self, url, params=None, timeout=60):
        """GET a BSE API and return its ``Table`` rows.

        BSE answers an unknown endpoint with a 200-OK HTML error page, so a
        body that will not parse as JSON has to be treated as a hard failure
        rather than an empty result.
        """
        r = self.session.get(url, params=params, timeout=timeout, headers={
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'https://www.bseindia.com/markets/equity/EQReports/BulknBlockDeals',
            'Origin': 'https://www.bseindia.com',
        })
        r.raise_for_status()
        return r.json().get("Table", []) or []

    def fetch_bse_deals_range(self, deal_type, from_date, to_date, quiet=False):
        """BSE bulk/block deals across an inclusive date window.

        One call covers the whole window: a 101-day request returned 5,009
        bulk rows spanning every trading day in it, and the per-day counts
        reconciled exactly against single-day queries, so there is no cap to
        page around.
        """
        label = f"BSE {deal_type} {from_date:%d-%m-%Y}..{to_date:%d-%m-%Y}"
        params = {
            "fromdt": from_date.strftime("%d/%m/%Y"),
            "todt": to_date.strftime("%d/%m/%Y"),
            "type": BSE_RANGE_TYPE[deal_type],
            "scripcode": "",
        }
        try:
            rows = self._bse_get(BSE_RANGE_API, params=params)
        except Exception as e:
            print(f"  \u274c {label}: {e}")
            return None
        if not rows:
            if not quiet:
                print(f"  {label}: no rows")
            return None

        df = _bse_normalise(pd.DataFrame(rows))
        if not quiet:
            _report_coverage(df, "Deal Date", label, to_date)
        return df

    def nse_deals_range(self, mode, from_date, to_date):
        """NSE bulk/block deals across an inclusive date window.

        Always requests the CSV form. The JSON form of this same endpoint
        silently caps at 70 rows and returns only the first day of whatever
        window it is given, which would look like success.
        """
        opt = "bulk_deals" if mode == "bulk_deals" else "block_deals"
        label = f"NSE {opt} {from_date:%d-%m-%Y}..{to_date:%d-%m-%Y}"
        params = {
            "optionType": opt,
            "from": from_date.strftime("%d-%m-%Y"),
            "to": to_date.strftime("%d-%m-%Y"),
            "csv": "true",
        }
        for attempt in range(3):
            try:
                sess = self._nse_session()
                r = sess.get(NSE_RANGE_API, params=params, timeout=90)
                if r.status_code in (401, 403):
                    self._nse_sess = None
                    time.sleep(1 + attempt)
                    continue
                if r.status_code == 429:
                    time.sleep(3 * (attempt + 1))
                    continue
                r.raise_for_status()
                # NSE ships these exports with a BOM and padded headers.
                r.encoding = "utf-8-sig"
                df = pd.read_csv(io.StringIO(r.text))
                df.columns = [str(c).replace("\ufeff", "").strip()
                              for c in df.columns]
                df = df.rename(columns=NSE_CSV_COL_MAP)
                if "date" not in df.columns or df.empty:
                    print(f"  {label}: no rows")
                    return None
                # Warning is deferred to nse_deals_window, which can still
                # close a tail gap from the snapshot.
                _report_coverage(df, "date", label)
                return df
            except pd.errors.EmptyDataError:
                print(f"  {label}: no rows")
                return None
            except Exception as e:
                if attempt == 2:
                    print(f"  \u274c {label}: {e}")
        return None

    def nse_deals_window(self, mode, from_date, to_date):
        """Archive rows for the window, topped up from the live snapshot.

        The archive is published with a delay, so a window ending today can
        be missing deals the snapshot already shows. The two feeds were
        verified to agree exactly where they overlap, so concatenating and
        de-duplicating is safe — identical deals collapse to one row.
        """
        frames = []
        arch = self.nse_deals_range(mode, from_date, to_date)
        if arch is not None and not arch.empty:
            frames.append(arch)

        if to_date.date() >= datetime.now().date():
            snap = self.nse_largedeals(mode=mode)
            if snap is not None and not snap.empty:
                snap = snap.copy()
                snap.columns = snap.columns.str.strip()
                frames.append(snap)

        if not frames:
            return None
        merged = pd.concat(frames, ignore_index=True, sort=False)
        # Both feeds ship Indian-format quantity strings ("3,81,000").
        for col in ("qty", "watp"):
            if col in merged.columns:
                merged[col] = pd.to_numeric(
                    merged[col].astype(str).str.replace(",", "", regex=False),
                    errors="coerce")
        before = len(merged)
        merged = _dedupe_deals(merged)
        if len(frames) > 1:
            print(f"  merged archive + snapshot: {before} -> {len(merged)} "
                  f"rows after de-duplication")
        _report_coverage(merged, "date", f"NSE {mode} window", to_date)
        return merged

    def save_to_excel(self, dataframes_dict, filename):
        """Save all dataframes to Excel with multiple sheets"""
        try:
            print(f"\n{'='*100}")
            print(f"Saving data to Excel file: {filename}")
            print(f"{'='*100}\n")

            with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                for sheet_name, df in dataframes_dict.items():
                    clean_sheet_name = sheet_name[:31]
                    df.to_excel(writer, sheet_name=clean_sheet_name, index=False)
                    print(f"✓ Sheet '{clean_sheet_name}': {len(df)} rows saved")

            print(f"\n{'='*100}")
            print(f"✓ Excel file saved successfully: {filename}")
            print(f"{'='*100}\n")

        except Exception as e:
            print(f"❌ Error saving to Excel: {e}")
            traceback.print_exc()

    def run(self):
        """Fetch every NSE/BSE bulk and block deal and emit eight Excel sheets.

        Two independent filters are applied to the same four raw feeds:
          * client name in `client_names_to_filter` -> nse_bulk / nse_block /
            bse_bulk / bse_block
          * scrip in the module-level STOCK_WATCHLIST -> the four
            `watchlist_*` sheets
        A filter that matches nothing still produces a one-row "Status" sheet
        so an empty result is distinguishable from a failed fetch.
        """
        # Download NSE bulk deals data for the latest day
        nse_bulk_deals_df = self.nse_largedeals(mode="bulk_deals")

        # Download NSE block deals data
        nse_block_deals_df = self.nse_largedeals(mode="block_deals")

        from investor_registry import all_bulk_deal_names
        client_names_to_filter = all_bulk_deal_names()

        # Guard against empty NSE DataFrames (e.g. the fetch failed)
        pulled_str = f"Data pulled on {datetime.now().strftime('%d-%b-%Y %H:%M')}"

        if nse_bulk_deals_df is not None and not nse_bulk_deals_df.empty:
            nse_bulk_deals_df.columns = nse_bulk_deals_df.columns.str.strip()
            filtered_nse_bulk_df = _match_clients(
                nse_bulk_deals_df, 'clientName', client_names_to_filter)
            if filtered_nse_bulk_df.empty:
                dr = _date_range(nse_bulk_deals_df)
                msg = f"No deals matched filter. Total deals fetched: {len(nse_bulk_deals_df)}."
                if dr:
                    msg += f" Data date: {dr}."
                msg += f" {pulled_str}."
                filtered_nse_bulk_df = pd.DataFrame({"Status": [msg]})
        else:
            filtered_nse_bulk_df = pd.DataFrame({"Status": [f"ERROR: NSE Bulk deals fetch failed or returned empty. {pulled_str}."]})

        if nse_block_deals_df is not None and not nse_block_deals_df.empty:
            nse_block_deals_df.columns = nse_block_deals_df.columns.str.strip()
            filtered_nse_block_df = _match_clients(
                nse_block_deals_df, 'clientName', client_names_to_filter)
            if filtered_nse_block_df.empty:
                dr = _date_range(nse_block_deals_df)
                msg = f"No deals matched filter. Total deals fetched: {len(nse_block_deals_df)}."
                if dr:
                    msg += f" Data date: {dr}."
                msg += f" {pulled_str}."
                filtered_nse_block_df = pd.DataFrame({"Status": [msg]})
        else:
            filtered_nse_block_df = pd.DataFrame({"Status": [f"ERROR: NSE Block deals fetch failed or returned empty. {pulled_str}."]})

        dataframes = {"nse_bulk": filtered_nse_bulk_df,
                      "nse_block": filtered_nse_block_df}

        # Same feeds, filtered by scrip instead of by client name.
        watchlist = {
            "watchlist_nse_bulk": _watchlist_sheet(
                nse_bulk_deals_df, "nse", "NSE bulk", pulled_str, _date_range),
            "watchlist_nse_block": _watchlist_sheet(
                nse_block_deals_df, "nse", "NSE block", pulled_str, _date_range),
        }

        # Fetch BSE BULK DEALS via API
        bulk_name = 'bse_bulk'
        try:
            bulk_df = self.fetch_bse_deals_api("bulk")
        except Exception as e:
            bulk_df = None
            print(f"⚠️  BSE Bulk fetch exception: {e}")

        if bulk_df is not None and not bulk_df.empty:
            bulk_df.columns = bulk_df.columns.str.strip()
            filtered_bulk_df = _match_clients(
                bulk_df, 'Client Name', client_names_to_filter)
            if filtered_bulk_df.empty:
                dr = _date_range(bulk_df)
                msg = f"No deals matched filter. Total BSE bulk deals fetched: {len(bulk_df)}."
                if dr:
                    msg += f" Data date: {dr}."
                msg += f" {pulled_str}."
                dataframes[bulk_name] = pd.DataFrame({"Status": [msg]})
            else:
                dataframes[bulk_name] = filtered_bulk_df
        else:
            dataframes[bulk_name] = pd.DataFrame({"Status": [f"ERROR: BSE Bulk deals API failed or returned no data. {pulled_str}."]})
            print(f"⚠️  No data fetched for {bulk_name}")

        watchlist["watchlist_bse_bulk"] = _watchlist_sheet(
            bulk_df, "bse", "BSE bulk", pulled_str, _date_range)

        time.sleep(1)

        # Fetch BSE BLOCK DEALS via API
        block_name = 'bse_block'
        try:
            block_df = self.fetch_bse_deals_api("block")
        except Exception as e:
            block_df = None
            print(f"⚠️  BSE Block fetch exception: {e}")

        if block_df is not None and not block_df.empty:
            block_df.columns = block_df.columns.str.strip()
            filtered_block_df = _match_clients(
                block_df, 'Client Name', client_names_to_filter)
            if filtered_block_df.empty:
                dr = _date_range(block_df)
                msg = f"No deals matched filter. Total BSE block deals fetched: {len(block_df)}."
                if dr:
                    msg += f" Data date: {dr}."
                msg += f" {pulled_str}."
                dataframes[block_name] = pd.DataFrame({"Status": [msg]})
            else:
                dataframes[block_name] = filtered_block_df
        else:
            dataframes[block_name] = pd.DataFrame({"Status": [f"ERROR: BSE Block deals API failed or returned no data. {pulled_str}."]})
            print(f"⚠️  No data fetched for {block_name}")

        watchlist["watchlist_bse_block"] = _watchlist_sheet(
            block_df, "bse", "BSE block", pulled_str, _date_range)

        # Watchlist sheets go last so the investor sheets stay where they are.
        dataframes.update(watchlist)
        hits = sum(len(v) for k, v in watchlist.items()
                   if "Status" not in v.columns)
        print(f"\n✓ Watchlist ({len(STOCK_WATCHLIST)} stocks): {hits} deal(s) matched")

        # Save to Excel
        if dataframes:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"BULK_BLOCK_Deals_{timestamp}.xlsx"
            self.save_to_excel(dataframes, filename)
        else:
            print("\n❌ No data was scraped from any endpoint.")
            print("="*100 + "\n")

    def run_range(self, from_date, to_date, out_path=None):
        """Dump every deal in an inclusive window, plus the watchlist views.

        Unlike `run()` the four exchange sheets are RAW — no superstar-name
        filter — because a historical pull is normally the input to ad-hoc
        analysis rather than a daily alert. The four ``watchlist_*`` sheets
        are built exactly as the daily job builds them.

        Returns the sheet dict that was written, or None if nothing came back.
        """
        print(f"\n{'='*100}")
        print(f"RANGE MODE  {from_date:%d-%b-%Y} .. {to_date:%d-%b-%Y}")
        print(f"{'='*100}\n")

        pulled_str = f"Data pulled on {datetime.now().strftime('%d-%b-%Y %H:%M')}"
        feeds = {}

        print("NSE (historical CSV archive + live snapshot):")
        feeds["nse_bulk"] = ("nse", "NSE bulk",
                             self.nse_deals_window("bulk_deals", from_date, to_date))
        feeds["nse_block"] = ("nse", "NSE block",
                              self.nse_deals_window("block_deals", from_date, to_date))

        print("\nBSE (range API):")
        feeds["bse_bulk"] = ("bse", "BSE bulk",
                             self.fetch_bse_deals_range("bulk", from_date, to_date))
        time.sleep(1)
        feeds["bse_block"] = ("bse", "BSE block",
                              self.fetch_bse_deals_range("block", from_date, to_date))

        sheets = {}
        for key, (_exch, label, df) in feeds.items():
            if df is not None and not df.empty:
                sheets[key] = df
            else:
                sheets[key] = pd.DataFrame({"Status": [
                    f"No {label} deals returned for "
                    f"{from_date:%d-%b-%Y} to {to_date:%d-%b-%Y}. {pulled_str}."]})

        for key, (exch, label, df) in feeds.items():
            sheets[f"watchlist_{key}"] = _watchlist_sheet(
                df, exch, label, pulled_str, _date_range)

        hits = sum(len(v) for k, v in sheets.items()
                   if k.startswith("watchlist_") and "Status" not in v.columns)
        print(f"\n✓ Watchlist ({len(STOCK_WATCHLIST)} stocks): {hits} deal(s) matched")

        if not any(k in sheets and "Status" not in sheets[k].columns
                   for k in feeds):
            print("\n❌ No deals returned for this window from either exchange.")
            return None

        filename = out_path or (f"BULK_BLOCK_Range_{from_date:%d%m%Y}_"
                                f"{to_date:%d%m%Y}.xlsx")
        self.save_to_excel(sheets, filename)
        return sheets


class BSEScraperWithEmail(BSEScraper):
    """Extends BSEScraper to add email reporting after scraping."""

    def __init__(self, email_config=None):
        super().__init__()
        self._email_config = email_config or self._load_config_from_env()
        self._saved_dataframes = {}
        self._saved_filename = None
        self._dry_run = '--dry-run' in sys.argv

    @staticmethod
    def _load_config_from_env():
        to_addrs = [a.strip() for a in os.environ.get('EMAIL_TO', '').split(',') if a.strip()]
        # Prefer a daily-specific secret name if provided, otherwise fall back
        subject_prefix = os.environ.get('EMAIL_SUBJECT_PREFIX_DAILY') or os.environ.get('EMAIL_SUBJECT_PREFIX', 'Bulk & Block Deals Report')
        return {
            'smtp_server': os.environ.get('EMAIL_SMTP_SERVER', 'smtp.gmail.com'),
            'smtp_port': int(os.environ.get('EMAIL_SMTP_PORT', '587')),
            'from_addr': os.environ.get('EMAIL_FROM', ''),
            'to_addrs': to_addrs,
            'username': os.environ.get('EMAIL_USERNAME', ''),
            'password': os.environ.get('EMAIL_PASSWORD', ''),
            'use_tls': os.environ.get('EMAIL_USE_TLS', 'true').lower() != 'false',
            'subject_prefix': subject_prefix,
        }

    def save_to_excel(self, dataframes_dict, filename):
        self._saved_dataframes = dict(dataframes_dict)
        self._saved_filename = filename
        super().save_to_excel(dataframes_dict, filename)

    def run(self):
        super().run()
        if not self._saved_dataframes:
            print("\nNo data available for email report.")
            return
        if self._dry_run:
            self._generate_preview()
            return
        self.send_email()

    def _build_html_body(self):
        date_str = datetime.now().strftime('%d-%b-%Y %H:%M')
        total_deals = sum(len(df) for df in self._saved_dataframes.values() if df is not None and not df.empty)

        parts = [f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: Calibri, Arial, sans-serif; margin: 20px; color: #333; }}
h1 {{ color: #1F4E79; font-size: 22px; border-bottom: 2px solid #1F4E79; padding-bottom: 8px; }}
h2 {{ color: #2E75B6; font-size: 16px; margin-top: 25px; }}
.summary {{ background: #F2F7FB; padding: 12px 16px; border-left: 4px solid #2E75B6; margin-bottom: 20px; font-size: 13px; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; font-size: 12px; }}
th {{ background-color: #2E75B6; color: #FFFFFF; padding: 8px 10px; text-align: left; font-weight: 600; border: 1px solid #2068A0; }}
td {{ padding: 6px 10px; border: 1px solid #D6D6D6; }}
tr:nth-child(even) {{ background-color: #F2F2F2; }}
tr:hover {{ background-color: #E8F0FE; }}
.no-data {{ color: #999; font-style: italic; padding: 10px 0; }}
.badge {{ display: inline-block; background: #2E75B6; color: white; padding: 2px 8px; border-radius: 3px; font-size: 11px; margin-left: 8px; }}
.footer {{ margin-top: 30px; font-size: 11px; color: #888; border-top: 1px solid #ddd; padding-top: 10px; }}
</style>
</head>
<body>
<h1>Bulk &amp; Block Deals Report</h1>
<div class="summary">
<strong>Report Date:</strong> {date_str}<br>
<strong>Total Filtered Deals:</strong> {total_deals}
</div>
"""]

        for sheet_name, df in self._saved_dataframes.items():
            row_count = len(df) if df is not None and not df.empty else 0
            parts.append(f"<h2>{sheet_name} <span class=\"badge\">{row_count} deal(s)</span></h2>")
            if df is not None and not df.empty:
                parts.append(df.to_html(index=False, border=0, na_rep='-'))
            else:
                parts.append('<p class="no-data">No matching deals found for this category.</p>')

        parts.append(f"""
<div class="footer">
<p>This is an automated report. The Excel file is attached for reference.</p>
<p>Attachment: {os.path.basename(self._saved_filename) if self._saved_filename else 'N/A'}</p>
</div>
</body>
</html>
""")

        return '\n'.join(parts)

    def send_email(self):
        config = self._email_config
        required_keys = ['from_addr', 'to_addrs', 'username', 'password']
        missing = [k for k in required_keys if not config.get(k)]
        if missing:
            print(f"\nX Email not sent. Missing configuration: {', '.join(missing)}")
            print("Set environment variables: EMAIL_FROM, EMAIL_TO, EMAIL_USERNAME, EMAIL_PASSWORD")
            return False

        try:
            print(f"\n{'='*100}")
            print("Sending email report ...")
            print(f"{'='*100}\n")

            msg = MIMEMultipart('mixed')
            msg['From'] = config['from_addr']
            to_list = config['to_addrs']
            msg['To'] = ', '.join(to_list)
            msg['Subject'] = f"{config.get('subject_prefix', 'Bulk & Block Deals Report')} - {datetime.now().strftime('%d-%b-%Y')}"

            html_body = self._build_html_body()
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))

            if self._saved_filename and os.path.exists(self._saved_filename):
                with open(self._saved_filename, 'rb') as fh:
                    part = MIMEBase('application', 'vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                    part.set_payload(fh.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(self._saved_filename)}"')
                    msg.attach(part)
                    print(f"i Attached: {self._saved_filename}")

            with smtplib.SMTP(config['smtp_server'], config['smtp_port']) as server:
                server.ehlo()
                if config.get('use_tls', True):
                    server.starttls()
                    server.ehlo()
                server.login(config['username'], config['password'])
                server.send_message(msg)

            print(f"i Email sent to: {', '.join(to_list)}")
            print('='*100)
            return True

        except smtplib.SMTPAuthenticationError:
            print("Email authentication failed. Check username/password.")
            print("For Gmail: use an App Password (not your regular password).")
            return False
        except smtplib.SMTPException as exc:
            print(f"SMTP error: {exc}")
            return False
        except Exception as exc:
            print(f"Error sending email: {exc}")
            traceback.print_exc()
            return False

    def _generate_preview(self):
        html = self._build_html_body()
        preview_file = 'email_preview.html'
        with open(preview_file, 'w', encoding='utf-8') as fh:
            fh.write(html)

        print('\n' + '='*80)
        print('DRY RUN - Email preview generated (not sent)')
        print('='*80)
        print(f" Subject    : {self._email_config.get('subject_prefix', 'Report')} - {datetime.now().strftime('%d-%b-%Y')}")
        print(f" Attachment : {self._saved_filename}")
        print(f" HTML preview : {os.path.abspath(preview_file)}")
        print(f" Body length : {len(html)} chars")
        for name, df in self._saved_dataframes.items():
            rows = len(df) if df is not None and not df.empty else 0
            print(f" • {name}: {rows} row(s)")
        print('='*80 + '\n')


def _parse_cli_date(value):
    """Parse a DD-MM-YYYY command-line date."""
    try:
        return datetime.strptime(value.strip(), "%d-%m-%Y")
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected DD-MM-YYYY, got {value!r}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="NSE + BSE bulk and block deals. Daily by default; pass "
                    "--from/--to for a historical range dump.")
    parser.add_argument("--from", dest="from_date", type=_parse_cli_date,
                        help="range start, DD-MM-YYYY (inclusive)")
    parser.add_argument("--to", dest="to_date", type=_parse_cli_date,
                        help="range end, DD-MM-YYYY (inclusive)")
    parser.add_argument("--out", dest="out_path",
                        help="output .xlsx path for range mode")
    parser.add_argument("--dry-run", action="store_true",
                        help="daily mode: write an email preview, send nothing")
    args = parser.parse_args(argv)

    if bool(args.from_date) != bool(args.to_date):
        parser.error("--from and --to must be given together")

    if args.from_date:
        if args.from_date > args.to_date:
            parser.error("--from is later than --to")
        if args.out_path and not args.out_path.lower().endswith(".xlsx"):
            parser.error("--out must end in .xlsx")
        # Range mode deliberately skips the email subclass: a backfill is an
        # ad-hoc pull, not a daily alert.
        BSEScraper().run_range(args.from_date, args.to_date, args.out_path)
        return

    BSEScraperWithEmail().run()


if __name__ == '__main__':
    main()
