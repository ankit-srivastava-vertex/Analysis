"""Tickertape public-API client — exact ticker resolution and financial statements.

Tickertape publishes annual and interim statements for ~5,900 NSE and BSE
listings (BSE SME included) with no authentication, which makes it a useful
middle tier between yfinance and a Screener.in scrape.

Two properties of the upstream API drive the design here:

* **Security ids are resolved from the bulk ``screener/query`` universe, keyed on
  the exact exchange ticker.** The public ``/search`` endpoint is deliberately
  never called: it fuzzy-matches and returns a *different* company without
  signalling an error (searching "sunita tools" yields Sterling Tools, "yash
  highvoltage" yields Jash Engineering), so a wrong-company answer would be
  indistinguishable from a correct one. The bulk universe carries an exact
  ``ticker`` field, so resolution is a dictionary hit or an honest miss.
* **Statement values are denominated in Rs crore**, the same scale Screener.in
  uses, so the same 1e7 multiplier converts them to the absolute rupee figures
  yfinance reports. Verified against Reliance FY2025 (``incTrev`` 982671 =
  Rs 9,82,671 cr revenue, ``incNinc`` 69648 = Rs 69,648 cr PAT).

History is shallow — roughly 5-6 annual periods and 5 quarters, against
Screener's ~12 years — so this supplements a deeper source rather than
replacing one. Balance sheet and cash flow are annual only; the interim
endpoint carries the income statement alone.

Public API
----------
``resolve_sid(ticker)``      exact ticker -> Tickertape security id, or None
``fetch_statements(sid)``    normalized annual + interim statement records

Each record carries a ``reporting`` field ("consolidated" or "standalone").
Callers that merge these frames with another source must check it: splicing a
consolidated series onto a standalone one produces a step change that looks
like a real jump in the business.
"""

from __future__ import annotations

import json
import os
import time

import requests

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
UNIVERSE_CACHE = os.path.join(CACHE_DIR, "tickertape_universe.json")
UNIVERSE_TTL_SECONDS = 7 * 86400

SCREENER_QUERY_URL = "https://api.tickertape.in/screener/query"
FINANCIALS_URL = "https://api.tickertape.in/stocks/financials/{kind}/{sid}/{period}/normal"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
}

# Tickertape field -> (yfinance-style row name, multiplier to absolute INR).
# Names match the targets in forensic_accounting._SCREENER_*_MAP so merged
# frames stay on one vocabulary.
INCOME_MAP = {
    "incTrev": ("Total Revenue", 1e7),
    "incEbi":  ("EBITDA", 1e7),
    "incDep":  ("Reconciled Depreciation", 1e7),
    "incPbi":  ("EBIT", 1e7),
    "incPfc":  ("Interest Expense", 1e7),
    "incIoi":  ("Interest Income Non Operating", 1e7),
    "incPbt":  ("Pretax Income", 1e7),
    "incToi":  ("Tax Provision", 1e7),
    "incNinc": ("Net Income", 1e7),
    "incRaw":  ("Cost Of Revenue", 1e7),
    "incSga":  ("Selling General And Administration", 1e7),
    "incEps":  ("Basic EPS", 1.0),
}

BALANCE_MAP = {
    "balComs": ("Common Stock", 1e7),
    "balRtne": ("Retained Earnings", 1e7),
    "balTeq":  ("Stockholders Equity", 1e7),
    "balTdeb": ("Total Debt", 1e7),
    "balTltd": ("Long Term Debt", 1e7),
    "balOcl":  ("Other Current Liabilities", 1e7),
    "balTcl":  ("Current Liabilities", 1e7),
    "balTotl": ("Total Liabilities Net Minority Interest", 1e7),
    "balNppe": ("Net PPE", 1e7),
    "balGint": ("Goodwill And Other Intangible Assets", 1e7),
    "balLti":  ("Investments And Advances", 1e7),
    "balTinv": ("Inventory", 1e7),
    "balTrec": ("Accounts Receivable", 1e7),
    "balAccp": ("Accounts Payable", 1e7),
    "balCsti": ("Cash Cash Equivalents And Short Term Investments", 1e7),
    "balOca":  ("Other Current Assets", 1e7),
    "balTca":  ("Current Assets", 1e7),
    "balTota": ("Total Assets", 1e7),
    # Share count is reported in crore, so it scales to a plain share count.
    "balTcso": ("Ordinary Shares Number", 1e7),
}

CASHFLOW_MAP = {
    "cafCfoa": ("Operating Cash Flow", 1e7),
    "cafCfia": ("Investing Cash Flow", 1e7),
    "cafCffa": ("Financing Cash Flow", 1e7),
    "cafFcf":  ("Free Cash Flow", 1e7),
    "cafCexp": ("Capital Expenditure", 1e7),
    "cafNcic": ("Changes In Cash", 1e7),
    "cafTcdp": ("Cash Dividends Paid", 1e7),
}

_session = None
_universe = None


def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(_HEADERS)
    return _session


def _download_universe():
    """Page through screener/query and return {TICKER: sid} for every listing."""
    session = _get_session()
    out = {}
    offset = 0
    while offset <= 8000:
        body = {"match": {}, "sortBy": "mrktCapf", "sortOrder": -1,
                "project": ["mrktCapf"], "offset": offset, "count": 500}
        resp = session.post(SCREENER_QUERY_URL, json=body, timeout=60)
        resp.raise_for_status()
        results = (resp.json().get("data") or {}).get("results") or []
        if not results:
            break
        for row in results:
            info = (row.get("stock") or {}).get("info") or {}
            ticker = str(info.get("ticker") or "").strip().upper()
            if ticker and row.get("sid"):
                out[ticker] = row["sid"]
        offset += len(results)
        if len(results) < 500:
            break
    return out


def _load_universe():
    """Return the cached {TICKER: sid} map, refreshing it once a week."""
    global _universe
    if _universe is not None:
        return _universe

    if os.path.exists(UNIVERSE_CACHE):
        age = time.time() - os.path.getmtime(UNIVERSE_CACHE)
        if age < UNIVERSE_TTL_SECONDS:
            try:
                with open(UNIVERSE_CACHE) as fh:
                    _universe = json.load(fh)
                return _universe
            except Exception:
                pass

    try:
        fresh = _download_universe()
    except Exception:
        fresh = {}

    if fresh:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(UNIVERSE_CACHE, "w") as fh:
                json.dump(fresh, fh)
        except Exception:
            pass
        _universe = fresh
    elif os.path.exists(UNIVERSE_CACHE):
        # Upstream is down; a stale map beats no map.
        try:
            with open(UNIVERSE_CACHE) as fh:
                _universe = json.load(fh)
        except Exception:
            _universe = {}
    else:
        _universe = {}
    return _universe


def resolve_sid(ticker):
    """Return the Tickertape security id for ``ticker``, or None if not listed.

    Matching is exact on the exchange ticker after stripping any yfinance
    suffix (``.NS`` / ``.BO``) and the NSE ``-EQ`` series tag. Fuzzy matching is
    intentionally absent — see the module docstring.
    """
    if not ticker:
        return None
    key = str(ticker).strip().upper()
    for suffix in (".NS", ".BO", "-EQ", "-BE"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return _load_universe().get(key)


def _normalize_quarterly_key(key):
    """Map an interim field name onto its annual equivalent ('qIncTrev' -> 'incTrev')."""
    if len(key) > 1 and key[0] == "q" and key[1].isupper():
        return key[1].lower() + key[2:]
    return key


def _normalize_records(rows, mapping):
    """Convert raw Tickertape rows into ``[{period, date, values}, ...]``.

    ``TTM`` rows and rows whose fields are all null are dropped: Tickertape
    emits a trailing TTM placeholder with every value set to None for companies
    that have not reported, which would otherwise look like a real period of
    zeroes.
    """
    out = []
    for row in rows or []:
        period = str(row.get("displayPeriod") or "").strip()
        if not period or period.upper() in ("TTM", "TRAILING"):
            continue
        values = {}
        for raw_key, raw_val in row.items():
            if not isinstance(raw_val, (int, float)) or isinstance(raw_val, bool):
                continue
            name_mult = mapping.get(_normalize_quarterly_key(raw_key))
            if not name_mult:
                continue
            name, mult = name_mult
            values[name] = float(raw_val) * mult
        if not values:
            continue

        # Screener publishes these as first-class rows; Tickertape implies them.
        # Screener's convention is Sales - Expenses = Operating Profit, so
        # expenses are everything above EBITDA and gross profit *is* EBITDA.
        revenue = values.get("Total Revenue")
        ebitda = values.get("EBITDA")
        if revenue is not None and ebitda is not None:
            values.setdefault("Total Expenses", revenue - ebitda)
            values.setdefault("Gross Profit", ebitda)
        if "EBIT" in values:
            values.setdefault("Operating Income", values["EBIT"])
        if "Stockholders Equity" in values:
            values.setdefault("Common Stock Equity", values["Stockholders Equity"])

        out.append({"period": period,
                    "date": str(row.get("endDate") or "")[:10],
                    "reporting": str(row.get("reporting") or ""),
                    "values": values})
    out.sort(key=lambda item: item["date"])
    return out


def _fetch_section(sid, kind, period, mapping):
    url = FINANCIALS_URL.format(kind=kind, sid=sid, period=period)
    try:
        resp = _get_session().get(url, timeout=30)
        if resp.status_code != 200:
            return []
        return _normalize_records(resp.json().get("data"), mapping)
    except Exception:
        return []


def fetch_statements(sid):
    """Return normalized statements for ``sid``.

    Keys ``income_annual``, ``balance_annual``, ``cashflow_annual`` and
    ``income_quarterly`` each hold ``[{period, date, values}, ...]`` sorted
    oldest-first, with values already scaled to absolute rupees. Sections the
    upstream has no data for come back as empty lists rather than raising, so a
    partially covered small-cap still yields whatever it does publish.
    """
    if not sid:
        return {}
    return {
        "income_annual": _fetch_section(sid, "income", "annual", INCOME_MAP),
        "balance_annual": _fetch_section(sid, "balancesheet", "annual", BALANCE_MAP),
        "cashflow_annual": _fetch_section(sid, "cashflow", "annual", CASHFLOW_MAP),
        "income_quarterly": _fetch_section(sid, "income", "interim", INCOME_MAP),
    }
