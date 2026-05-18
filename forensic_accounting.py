"""
Forensic Accounting & Deep Fundamental Analysis Tool
=====================================================

SUMMARY
-------
Comprehensive single-stock forensic + deep fundamental analysis for any
listed Indian company.  Generates a professional PDF report (40+ pages)
with investment recommendation (BUY / HOLD / SELL / AVOID).

WORKFLOW
--------
1. Resolve the user-supplied symbol to the best yfinance ticker by trying
   .NS, .BO, a manual SME-alias map, prefix-truncation heuristics, and
   yf.Search. The candidate with the most years of financials wins.
2. Fetch financials (Balance Sheet, P&L, Cash Flow) via yfinance, then
   universally backfill from Screener.in (HTML scrape) so even BSE-only /
   SME / newly-listed stocks get 6-12 years of statements. Screener values
   are in Rs. crore; converted to absolute INR (×1e7) for yfinance shape.
3. Compute forensic scores:
   - Beneish M-Score (earnings manipulation detection)
   - Altman Z-Score  (bankruptcy risk)
   - Piotroski F-Score (financial strength, 0-9)
   - DuPont decomposition (ROE breakdown)
   - Springate S-Score, Ohlson O-Score, Montier C-Score
   - Benford's Law digit distribution analysis
4. Fetch extended data from NSE APIs (with retry + 18-hour cache; index=sme
   retry baked into _nse_get_json for SME-listed issuers):
   - Credit ratings, promoter holding, ESM status
   - Concall transcripts, investor presentations (auto-downloaded PDFs)
   - Shareholding history (quarterly), SAST / insider disclosures
   - Delivery / volume data, sector peers
   - Corporate actions, related-party filings, MF / institutional holders
   - Financial-result PDFs and annual-report PDFs (filings library, exchange-
     direct, plus PDF-regex fallback to synthesize income_annual when
     yfinance has nothing).
5. Run deep fundamental analysis:
   - Shareholding trend, insider trading signals
   - Peer comparison, relative strength vs Nifty 50
   - Technical structure, Graham / Magic Formula valuation
   - Capex cycle, tax sustainability, institutional holding trends
   - Credit rating intelligence
6. Score: Forensic Score (0-100) + Deep Fundamental Score (0-100).
7. Generate PDF report with all sections and save to script directory.

DATA SOURCES
------------
- yfinance          — Financial statements, historical prices, MF holders,
                      corporate actions (.NS = NSE, .BO = BSE; resolver auto-
                      picks the best variant including SME aliases).
- Screener.in       — Universal financials backfill (P&L, BS, CF, quarterly)
                      via HTML scrape of /company/<symbol-or-scripcode>/.
                      Works for NSE main-board, BSE main-board, and BSE-SME
                      stocks where yfinance has 0-4 years of data. Values
                      converted from Rs. crore to absolute INR.
- NSE APIs          — Credit ratings, shareholding, SAST, delivery data,
                      sector peers, concalls, investor presentations,
                      related-party filings, ESM status, promoter holding,
                      financial-result and annual-report filing libraries.
                      index=sme retry built in for SME issuers.
- Local PDF parsing  — Concall transcripts, investor presentations,
                      annual reports, financial-result PDFs (auto-
                      downloaded from NSE, parsed via PyPDF2; revenue/PAT/
                      EBITDA regex-extracted as last-resort financial source).

RESILIENCE
----------
- Retry:  All NSE API calls retry 3× with exponential backoff.
          Auto-refreshes cookies on HTTP 401; backs off on 429.
- Cache:  JSON responses cached in .cache/ directory (18-hour TTL); large
          Screener HTML pages cached separately as .html files.
          Same-day re-runs are dramatically faster.
- Ticker resolution: .NS → .BO → SME alias map → prefix-truncation →
          yf.Search. Best candidate (most years of financials) wins.
- SME support: NSE corporate APIs auto-retry with index=sme when the
          equities call returns nothing.
- Universal financials: Screener.in scrape backfills missing years/rows
          when yfinance is sparse; PDF-regex extraction synthesizes a
          minimal income statement when even Screener has no data.
- No hard refusal: the script will always produce a report, even for
          newly-listed / SME / data-poor stocks (with appropriate caveats).

OUTPUT
------
- forensic_report_<SYMBOL>_<timestamp>.pdf

USAGE
-----
Individual run:
    python3 forensic_accounting.py TCS                 # any NSE symbol as argument
    python3 forensic_accounting.py RELIANCE
    python3 forensic_accounting.py                     # uses COMPANY_SYMBOL set below
    python3 -c "from forensic_accounting import run; run('RELIANCE')"  # programmatic

Group run (via run_all.py):
    Not part of run_all.py — run independently.

DEPENDENCIES
------------
yfinance, pandas, fpdf2 (FPDF), PyPDF2, requests
(fpdf2 and PyPDF2 are auto-installed if missing)
"""

import os
import sys
import math
import datetime
import warnings

warnings.filterwarnings("ignore")

# ── Auto-install required third-party libraries if missing ───────────────────
def _ensure(import_name, pip_name=None):
    """Import a module, pip-installing it on the fly if it isn't available."""
    import importlib
    try:
        return importlib.import_module(import_name)
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name or import_name])
        return importlib.import_module(import_name)


_fpdf_mod = _ensure("fpdf", "fpdf2")
FPDF = _fpdf_mod.FPDF

import re
import io
import json
import time
import hashlib

pd = _ensure("pandas")
yf = _ensure("yfinance")
requests = _ensure("requests")
PyPDF2 = _ensure("PyPDF2")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ══════════════════════════════════════════════════════════════════════════════
# RESILIENCE: Same-day cache + Retry logic with backoff
# ══════════════════════════════════════════════════════════════════════════════

_CACHE_DIR = os.path.join(SCRIPT_DIR, ".cache")
_CACHE_TTL_HOURS = 18  # cache valid for 18 hours (re-run same day = instant)


def _cache_path(key):
    """Get cache file path for a given key."""
    safe_key = hashlib.md5(key.encode()).hexdigest()
    return os.path.join(_CACHE_DIR, safe_key + ".json")


def _cache_get(key):
    """Retrieve cached data if fresh (within TTL)."""
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    try:
        mtime = os.path.getmtime(path)
        age_hours = (time.time() - mtime) / 3600
        if age_hours > _CACHE_TTL_HOURS:
            return None
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


def _cache_set(key, data):
    """Store data in cache."""
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = _cache_path(key)
        with open(path, "w") as f:
            json.dump(data, f, default=str)
    except Exception:
        pass  # caching is best-effort


def _nse_get(session, url, params=None, max_retries=3, timeout=15):
    """
    NSE API GET with retry + exponential backoff.
    Returns response object or None on failure.
    """
    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code == 401:
                # Cookie expired — refresh session
                try:
                    session.get("https://www.nseindia.com/", timeout=10)
                except Exception:
                    pass
            if resp.status_code == 429:
                # Rate limited — wait longer
                time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code >= 500:
                # Server error — retry
                time.sleep(1.5 ** attempt)
                continue
            # 4xx other than 401/429 — don't retry
            return resp
        except requests.exceptions.Timeout:
            time.sleep(1.5 ** attempt)
        except requests.exceptions.ConnectionError:
            time.sleep(2 ** attempt)
        except Exception:
            time.sleep(1)
    return None


def _nse_get_json(session, url, params=None, cache_key=None, max_retries=3):
    """
    NSE API GET returning parsed JSON, with cache support.
    """
    # Check cache first
    if cache_key:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    resp = _nse_get(session, url, params=params, max_retries=max_retries)
    if resp is None or resp.status_code != 200:
        data = None
    else:
        try:
            data = resp.json()
        except Exception:
            data = None

    # ── SME RETRY ──
    # Many NSE corporate APIs require index=sme for SME-listed issuers.
    # If the equities call returned nothing useful, transparently retry with sme.
    def _is_empty(d):
        if d is None:
            return True
        if isinstance(d, list) and len(d) == 0:
            return True
        if isinstance(d, dict):
            # Common 'no data' shapes
            if not d:
                return True
            for k in ("data", "records", "results", "shareholding"):
                v = d.get(k)
                if isinstance(v, list) and len(v) == 0:
                    return True
        return False

    if _is_empty(data) and params and params.get("index") == "equities":
        sme_params = dict(params)
        sme_params["index"] = "sme"
        sme_cache_key = (cache_key + "_sme") if cache_key else None
        if sme_cache_key:
            cached_sme = _cache_get(sme_cache_key)
            if cached_sme is not None:
                return cached_sme
        resp2 = _nse_get(session, url, params=sme_params, max_retries=max_retries)
        if resp2 is not None and resp2.status_code == 200:
            try:
                data2 = resp2.json()
                if not _is_empty(data2):
                    if sme_cache_key:
                        _cache_set(sme_cache_key, data2)
                    return data2
            except Exception:
                pass

    # Store in cache
    if cache_key and data:
        _cache_set(cache_key, data)

    return data


def _nse_download_pdf(session, pdf_url, max_retries=2):
    """Download a PDF from NSE with retry. Returns bytes or None."""
    for attempt in range(max_retries):
        try:
            resp = session.get(pdf_url, timeout=25)
            if resp.status_code == 200 and resp.content[:5] == b'%PDF-':
                return resp.content
            if resp.status_code == 403 or resp.status_code == 401:
                # Refresh cookies and retry
                try:
                    session.get("https://www.nseindia.com/", timeout=10)
                except Exception:
                    pass
                time.sleep(1)
                continue
        except Exception:
            time.sleep(1.5 ** attempt)
    return None

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — Change this symbol each time you run the analysis
# ══════════════════════════════════════════════════════════════════════════════
COMPANY_SYMBOL = "SUDEEPPHRM"       # NSE symbol (e.g. RELIANCE, TCS, INFY, HDFCBANK)
# ══════════════════════════════════════════════════════════════════════════════


# ── Aliases for financial line items (yfinance names vary) ────────────────────
ALIASES = {
    # Income Statement
    "revenue":      ["Total Revenue", "Operating Revenue", "Revenue"],
    "cogs":         ["Cost Of Revenue", "Reconciled Cost Of Revenue"],
    "gross_profit": ["Gross Profit"],
    "operating_inc": ["Operating Income", "EBIT"],
    "operating_exp": ["Operating Expense", "Total Expenses"],
    "net_income":   ["Net Income", "Net Income Common Stockholders",
                     "Net Income From Continuing And Discontinued Operation"],
    "ebitda":       ["EBITDA", "Normalized EBITDA"],
    "ebit":         ["EBIT", "Operating Income"],
    "interest_exp": ["Interest Expense", "Interest Expense Non Operating"],
    "interest_inc": ["Interest Income", "Interest Income Non Operating"],
    "depreciation": ["Reconciled Depreciation", "Depreciation And Amortization"],
    "sga":          ["Selling General And Administration"],
    "tax":          ["Tax Provision"],
    "pretax_income": ["Pretax Income"],
    "diluted_shares": ["Diluted Average Shares", "Basic Average Shares"],
    "basic_eps":    ["Basic EPS"],
    "diluted_eps":  ["Diluted EPS"],
    # Balance Sheet
    "total_assets":        ["Total Assets"],
    "current_assets":      ["Current Assets"],
    "current_liabilities": ["Current Liabilities"],
    "cash":                ["Cash And Cash Equivalents", "Cash Financial",
                            "Cash Cash Equivalents And Short Term Investments"],
    "receivables":         ["Accounts Receivable", "Other Receivables"],
    "inventory":           ["Inventory"],
    "ppe_net":             ["Net PPE"],
    "ppe_gross":           ["Gross PPE"],
    "accum_dep":           ["Accumulated Depreciation"],
    "total_debt":          ["Total Debt"],
    "long_term_debt":      ["Long Term Debt",
                            "Long Term Debt And Capital Lease Obligation"],
    "current_debt":        ["Current Debt",
                            "Current Debt And Capital Lease Obligation"],
    "equity":              ["Stockholders Equity", "Common Stock Equity"],
    "total_equity":        ["Total Equity Gross Minority Interest"],
    "retained_earnings":   ["Retained Earnings"],
    "total_liabilities":   ["Total Liabilities Net Minority Interest"],
    "goodwill":            ["Goodwill"],
    "intangibles":         ["Other Intangible Assets",
                            "Goodwill And Other Intangible Assets"],
    "minority_interest":   ["Minority Interest"],
    "working_capital":     ["Working Capital"],
    "net_debt":            ["Net Debt"],
    "shares_outstanding":  ["Ordinary Shares Number", "Share Issued"],
    "payables":            ["Payables", "Accounts Payable"],
    "invested_capital":    ["Invested Capital"],
    # Cash Flow
    "operating_cf":  ["Operating Cash Flow"],
    "investing_cf":  ["Investing Cash Flow"],
    "financing_cf":  ["Financing Cash Flow"],
    "capex":         ["Capital Expenditure"],
    "fcf":           ["Free Cash Flow"],
    "dep_cf":        ["Depreciation And Amortization", "Depreciation"],
    "dividends_paid": ["Cash Dividends Paid"],
    "debt_repayment": ["Repayment Of Debt"],
    "debt_issuance":  ["Issuance Of Debt"],
    "change_wc":     ["Change In Working Capital"],
    "change_recv":   ["Change In Receivables"],
    "change_inv":    ["Change In Inventory"],
    "change_pay":    ["Change In Payable"],
    "stock_issuance": ["Issuance Of Capital Stock", "Common Stock Issuance"],
}


# ── Helper functions ─────────────────────────────────────────────────────────

def safe_get(df, item_key, col_idx=0, default=float("nan")):
    """Get a value from a financial-statement DataFrame using alias lookup."""
    if df is None or df.empty:
        return default
    names = ALIASES.get(item_key, [item_key] if isinstance(item_key, str) else item_key)
    ncols = df.shape[1]
    if col_idx >= ncols:
        return default
    for name in names:
        if name in df.index:
            try:
                val = df.iloc[df.index.get_loc(name), col_idx]
                if pd.notna(val):
                    return float(val)
            except (IndexError, KeyError, TypeError):
                continue
    return default


def safe_div(a, b, default=float("nan")):
    """Division with NaN / zero protection."""
    try:
        if math.isnan(a) or math.isnan(b) or b == 0:
            return default
        return a / b
    except (TypeError, ValueError):
        return default


def pct_change_val(curr, prev):
    """Percentage change from prev to curr."""
    return safe_div(curr - prev, abs(prev)) * 100


def to_cr(val):
    """Convert INR value to crores (÷ 1e7)."""
    try:
        if math.isnan(val):
            return float("nan")
        return val / 1e7
    except (TypeError, ValueError):
        return float("nan")


def fmt_cr(val, decimals=1):
    """Format as 'Rs. X Cr' string."""
    cr = to_cr(val)
    if math.isnan(cr):
        return "N/A"
    return "Rs. {:,.{d}f} Cr".format(cr, d=decimals)


def fmt_pct(val, decimals=1):
    """Format as percentage string."""
    try:
        if math.isnan(val):
            return "N/A"
        return "{:+.{d}f}%".format(val, d=decimals)
    except (TypeError, ValueError):
        return "N/A"


def fmt_num(val, decimals=2):
    """Format a plain number."""
    try:
        if math.isnan(val):
            return "N/A"
        return "{:,.{d}f}".format(val, d=decimals)
    except (TypeError, ValueError):
        return "N/A"


def _nan(val):
    """Check if value is NaN."""
    try:
        return math.isnan(val)
    except (TypeError, ValueError):
        return True


def _fy_label(ts):
    """Convert timestamp to FY label like 'FY2025'."""
    if hasattr(ts, "month"):
        yr = ts.year if ts.month > 6 else ts.year
        return "FY%d" % yr
    return str(ts)


def _parse_float(val):
    """Parse a value to float, handling string percentages."""
    if val is None:
        return float("nan")
    try:
        if isinstance(val, (int, float)):
            return float(val)
        s = str(val).strip().replace("%", "").replace(",", "")
        if s == "" or s == "-":
            return float("nan")
        return float(s)
    except (ValueError, TypeError):
        return float("nan")


# ══════════════════════════════════════════════════════════════════════════════
# DATA FETCHING
# ══════════════════════════════════════════════════════════════════════════════

class FinancialData:
    """Container for all fetched financial data."""
    def __init__(self):
        self.symbol = ""
        self.info = {}
        self.income_annual = None       # DataFrame
        self.income_quarterly = None
        self.balance_annual = None
        self.balance_quarterly = None
        self.cashflow_annual = None
        self.cashflow_quarterly = None
        self.years = 0
        self.quarters = 0
        self.fy_labels = []
        self.q_labels = []
        self.credit_ratings = []     # list of dicts from NSE filings
        self.esm_status = None           # dict with ESM stage info
        self.promoter_holding = None     # dict with promoter holding info
        # ── Deep Fundamental Analysis Data ──
        self.historical_prices = None    # DataFrame of daily OHLCV (5+ years)
        self.peer_symbols = []           # list of peer NSE symbols
        self.concall_texts = []          # list of dicts: {quarter, text}
        self.annual_report_texts = []    # list of dicts: {year, text}
        self.shareholding_history = []   # list of dicts per quarter
        self.deep_results = {}           # results from DeepFundamentalAnalyzer
        # ── Extended Data (Auto-Fetched) ──
        self.shareholding_quarterly = [] # quarterly promoter/public % history
        self.sast_disclosures = []       # insider buy/sell disclosures
        self.delivery_data = {}          # current delivery %
        self.sector_peers = {}           # sector info + peer list
        self.corporate_actions = {}      # dividends, splits, bonus
        self.related_party_filings = []  # RPT disclosures
        self.mf_institutional_data = {}  # MF + institutional holder data
        # ── Filings library (independent of yfinance) ──
        self.financial_results_filings = []  # quarterly P&L PDFs from NSE
        self.annual_report_filings = []      # annual report PDFs from NSE
        self.filings_summary = {}            # counts per source
        self.order_book_history = []         # [{date, value_crore, type, context}]


# ── Resolved-ticker cache so we only resolve once per symbol per process ───
_RESOLVED_TICKERS = {}

# ── Manual alias map for SME / unusual tickers where Yahoo abbreviates the symbol
#    differently from the exchange listing. Add new entries here as needed.
#    Key = user-friendly symbol (uppercased); value = list of yfinance suffixed
#    candidates to try in order.
_TICKER_ALIASES = {
    "YASHHIGHVOLTAGE": ["YASHHV.BO"],
    "JEENASIKHO":      ["JSLL.NS", "JSLL.BO"],
    "JEENASIKHOLIFECARE": ["JSLL.NS", "JSLL.BO"],
}


def _score_yf_candidate(t):
    """Score a yfinance Ticker: prefer most years of financials, then info presence."""
    years = 0
    has_info = 0
    has_price = 0
    try:
        fin = t.financials
        if fin is not None and not fin.empty:
            years = fin.shape[1]
    except Exception:
        pass
    try:
        info = t.info or {}
        if info.get("longName") or info.get("shortName"):
            has_info = 1
    except Exception:
        info = {}
    try:
        h = t.history(period="6mo")
        if h is not None and not h.empty:
            has_price = len(h)
    except Exception:
        pass
    return (years, has_info, has_price, info)


def _resolve_yf_ticker(symbol):
    """
    Resolve user symbol to the best yfinance Ticker.
    Tries SYMBOL.NS, SYMBOL.BO, then yf.Search lookup (handles SME tickers
    like YASHHIGHVOLTAGE -> YASHHV.BO, JEENASIKHO -> JSLL.NS, etc.).
    Picks the candidate with the most financial-year coverage.
    """
    if symbol in _RESOLVED_TICKERS:
        return _RESOLVED_TICKERS[symbol]

    candidates = []  # list of (yf_sym, ticker, years, has_info, has_price, info)
    tried = set()

    def _try(yf_sym):
        if not yf_sym or yf_sym in tried:
            return
        tried.add(yf_sym)
        try:
            t = yf.Ticker(yf_sym)
            years, has_info, has_price, info = _score_yf_candidate(t)
            if years > 0 or has_info or has_price > 30:
                candidates.append((yf_sym, t, years, has_info, has_price, info))
                print("    candidate %-22s  years=%d  info=%s  price_days=%d" % (
                    yf_sym, years, "Y" if has_info else "-", has_price))
        except Exception:
            pass

    # 1) Direct suffix attempts (main board NSE/BSE)
    _try(symbol + ".NS")
    _try(symbol + ".BO")

    # 2) Manual alias map (handles SME tickers Yahoo abbreviates)
    for alias in _TICKER_ALIASES.get(symbol, []):
        _try(alias)

    # 3) Prefix-truncation heuristic for BSE SME (e.g. YASHHIGHVOLTAGE -> YASHHV.BO).
    #    Try the first 4..8 chars of the symbol with .BO and .NS.
    if not candidates or max(c[2] for c in candidates) < 1:
        for n in (8, 7, 6, 5, 4):
            if n >= len(symbol):
                continue
            _try(symbol[:n] + ".BO")
            _try(symbol[:n] + ".NS")
            if candidates and max(c[2] for c in candidates) >= 2:
                break

    # 4) yfinance Search — handles natural-language remapping
    if not candidates or max(c[2] for c in candidates) < 2:
        try:
            sr = yf.Search(symbol, max_results=8)
            for q in (sr.quotes or [])[:8]:
                qs = q.get("symbol", "")
                if qs.endswith(".NS") or qs.endswith(".BO"):
                    _try(qs)
        except Exception as e:
            print("    yf.Search failed: %s" % e)

    if not candidates:
        # Return an empty .NS ticker as last resort
        t = yf.Ticker(symbol + ".NS")
        result = (t, symbol + ".NS", {}, False)
        _RESOLVED_TICKERS[symbol] = result
        return result

    # Pick best: most years > most info > most price
    candidates.sort(key=lambda c: (c[2], c[3], c[4]), reverse=True)
    best = candidates[0]
    yf_sym, ticker, years, has_info, has_price, info = best
    is_sme = False
    # Heuristic: BSE-only listings (no .NS candidate with data) often = SME
    has_ns = any(c[0].endswith(".NS") and c[2] > 0 for c in candidates)
    if not has_ns:
        is_sme = True
    print("    -> resolved '%s' to %s (%d yrs%s)" % (
        symbol, yf_sym, years, ", SME-likely" if is_sme else ""))
    result = (ticker, yf_sym, info, is_sme)
    _RESOLVED_TICKERS[symbol] = result
    return result


def fetch_financial_data(symbol):
    """Fetch all financial data from yfinance with multi-variant resolution."""
    data = FinancialData()
    data.symbol = symbol

    print("\n[1/5] Fetching financial data for %s ..." % symbol)
    print("  Resolving ticker variants (.NS / .BO / SME)...")
    ticker, yf_symbol, resolved_info, _is_sme = _resolve_yf_ticker(symbol)
    print("  Using yfinance ticker: %s" % yf_symbol)

    # ── Company info ──
    try:
        data.info = ticker.info or {}
        name = data.info.get("longName", data.info.get("shortName", symbol))
        print("  Company : %s" % name)
        print("  Sector  : %s" % data.info.get("sectorDisp", "N/A"))
        print("  Industry: %s" % data.info.get("industryDisp", "N/A"))
        mkt = data.info.get("marketCap", 0)
        if mkt:
            print("  Mkt Cap : Rs. {:,.0f} Cr".format(mkt / 1e7))
    except Exception as e:
        print("  WARNING: Could not fetch company info (%s)" % e)

    # ── Annual financial statements ──
    try:
        data.income_annual = ticker.financials
        if data.income_annual is not None and not data.income_annual.empty:
            data.years = data.income_annual.shape[1]
            data.fy_labels = [_fy_label(c) for c in data.income_annual.columns]
            print("  Income Stmt (Annual) : %d years  %s" % (
                data.years, ", ".join(data.fy_labels)))
        else:
            print("  Income Stmt (Annual) : NO DATA")
    except Exception as e:
        print("  Income Stmt (Annual) : FAILED (%s)" % e)

    try:
        data.balance_annual = ticker.balance_sheet
        if data.balance_annual is not None and not data.balance_annual.empty:
            print("  Balance Sheet (Annual): %d years" % data.balance_annual.shape[1])
        else:
            print("  Balance Sheet (Annual): NO DATA")
    except Exception as e:
        print("  Balance Sheet (Annual): FAILED (%s)" % e)

    try:
        data.cashflow_annual = ticker.cashflow
        if data.cashflow_annual is not None and not data.cashflow_annual.empty:
            print("  Cash Flow (Annual)   : %d years" % data.cashflow_annual.shape[1])
        else:
            print("  Cash Flow (Annual)   : NO DATA")
    except Exception as e:
        print("  Cash Flow (Annual)   : FAILED (%s)" % e)

    # ── Quarterly financial statements ──
    try:
        data.income_quarterly = ticker.quarterly_financials
        if data.income_quarterly is not None and not data.income_quarterly.empty:
            data.quarters = data.income_quarterly.shape[1]
            data.q_labels = [c.strftime("%b-%Y") for c in data.income_quarterly.columns]
            print("  Income Stmt (Qtr)    : %d quarters" % data.quarters)
        else:
            print("  Income Stmt (Qtr)    : NO DATA")
    except Exception as e:
        print("  Income Stmt (Qtr)    : FAILED (%s)" % e)

    try:
        data.balance_quarterly = ticker.quarterly_balance_sheet
        if data.balance_quarterly is not None and not data.balance_quarterly.empty:
            print("  Balance Sheet (Qtr)  : %d quarters" % data.balance_quarterly.shape[1])
        else:
            print("  Balance Sheet (Qtr)  : NO DATA")
    except Exception as e:
        print("  Balance Sheet (Qtr)  : FAILED (%s)" % e)

    try:
        data.cashflow_quarterly = ticker.quarterly_cashflow
        if data.cashflow_quarterly is not None and not data.cashflow_quarterly.empty:
            print("  Cash Flow (Qtr)      : %d quarters" % data.cashflow_quarterly.shape[1])
        else:
            print("  Cash Flow (Qtr)      : NO DATA")
    except Exception as e:
        print("  Cash Flow (Qtr)      : FAILED (%s)" % e)

    if False and data.years < 2:
        # ── (legacy) BSE fallback path — disabled, resolver above already picked best ticker
        print("\n  NSE data insufficient — trying BSE fallback (.BO)...")
        try:
            bse_ticker = yf.Ticker(symbol + ".BO")
            bse_info = bse_ticker.info or {}
            if bse_info.get("longName") or bse_info.get("shortName"):
                # BSE ticker valid — fetch financials
                if not data.info or not data.info.get("longName"):
                    data.info = bse_info
                    print("  Company (BSE): %s" % bse_info.get("longName", symbol))

                bse_inc = bse_ticker.financials
                if bse_inc is not None and not bse_inc.empty and bse_inc.shape[1] > data.years:
                    data.income_annual = bse_inc
                    data.years = bse_inc.shape[1]
                    data.fy_labels = [_fy_label(c) for c in bse_inc.columns]
                    print("  Income Stmt (BSE)    : %d years" % data.years)

                bse_bal = bse_ticker.balance_sheet
                if bse_bal is not None and not bse_bal.empty:
                    if data.balance_annual is None or data.balance_annual.empty:
                        data.balance_annual = bse_bal
                        print("  Balance Sheet (BSE)  : %d years" % bse_bal.shape[1])

                bse_cf = bse_ticker.cashflow
                if bse_cf is not None and not bse_cf.empty:
                    if data.cashflow_annual is None or data.cashflow_annual.empty:
                        data.cashflow_annual = bse_cf
                        print("  Cash Flow (BSE)      : %d years" % bse_cf.shape[1])

                # Quarterly from BSE
                bse_qinc = bse_ticker.quarterly_financials
                if bse_qinc is not None and not bse_qinc.empty:
                    if data.income_quarterly is None or data.income_quarterly.empty:
                        data.income_quarterly = bse_qinc
                        data.quarters = bse_qinc.shape[1]
                        data.q_labels = [c.strftime("%b-%Y") for c in bse_qinc.columns]
                        print("  Income Stmt Qtr (BSE): %d quarters" % data.quarters)

                bse_qbal = bse_ticker.quarterly_balance_sheet
                if bse_qbal is not None and not bse_qbal.empty:
                    if data.balance_quarterly is None or data.balance_quarterly.empty:
                        data.balance_quarterly = bse_qbal

                bse_qcf = bse_ticker.quarterly_cashflow
                if bse_qcf is not None and not bse_qcf.empty:
                    if data.cashflow_quarterly is None or data.cashflow_quarterly.empty:
                        data.cashflow_quarterly = bse_qcf

                # Historical prices from BSE as fallback
                if data.historical_prices is None or (data.historical_prices is not None and len(data.historical_prices) < 100):
                    bse_hist = bse_ticker.history(period="10y")
                    if bse_hist is not None and not bse_hist.empty:
                        data.historical_prices = bse_hist
                        print("  Prices (BSE)         : %d days" % len(bse_hist))

                ticker = bse_ticker  # Use BSE ticker for remaining fetches
                print("  BSE fallback: OK (%d years of data)" % data.years)
        except Exception as e:
            print("  BSE fallback failed: %s" % e)

    if data.years < 2:
        print("\n  NOTE: Only %d year(s) of annual data available — report will be generated" % data.years)
        print("  with whatever data could be sourced (newly-listed / SME / low-history stocks).")

    # ── Screener.in backfill (universal source for NSE + BSE incl. SME) ──
    # Always attempt — when yfinance is rich, Screener just adds older years.
    # When yfinance is sparse (BSE-only / SME / new listing), Screener provides
    # the bulk of the financial statements.
    try:
        sc = fetch_screener_financials(symbol)
        if sc:
            yf_years_before = data.years
            _merge_screener_into_data(data, sc)
            if data.years > yf_years_before:
                print("  Screener backfill: years %d -> %d (added %d)" % (
                    yf_years_before, data.years, data.years - yf_years_before))
            elif data.years > 0:
                print("  Screener backfill: rows augmented (years unchanged at %d)" % data.years)
    except Exception as e:
        print("  Screener backfill failed: %s" % e)

    # ── Credit ratings from NSE filings ──
    data.credit_ratings = fetch_credit_ratings(symbol)

    # ── ESM status check ──
    data.esm_status = fetch_esm_status(symbol)

    # ── Promoter holding ──
    data.promoter_holding = fetch_promoter_holding(symbol)
    if data.promoter_holding is None:
        # Fallback: extract from yfinance info (already fetched)
        data.promoter_holding = _promoter_from_yfinance(data.info)
        if data.promoter_holding:
            pp = data.promoter_holding["data"][0]["promoter_pct"]
            print("  Promoter holding from yfinance: %.1f%%" % pp)

    # ── Historical price data (10 years for valuation band) ──
    try:
        hist = ticker.history(period="10y")
        if hist is not None and not hist.empty:
            data.historical_prices = hist
            print("  Historical Prices    : %d days (%.1f years)" % (
                len(hist), len(hist) / 252))
    except Exception as e:
        print("  Historical Prices    : FAILED (%s)" % e)

    # ── Shareholding pattern history ──
    try:
        holders = ticker.major_holders
        inst = ticker.institutional_holders
        if inst is not None and not inst.empty:
            data.shareholding_history = [{"institutional_holders": inst}]
            print("  Institutional Holders: %d entries" % len(inst))
    except Exception as e:
        print("  Shareholding data    : FAILED (%s)" % e)

    return data


# ── Credit Rating Fetcher (NSE corporate filings) ───────────────────────────

_AGENCY_PATTERNS = [
    ("crisil", "CRISIL"),
    ("icra", "ICRA"),
    ("care", "CARE"),
    ("indiarating", "India Ratings (Fitch)"),
    ("india_rating", "India Ratings (Fitch)"),
    ("fitch", "Fitch"),
    ("acuite", "Acuite"),
    ("brickwork", "Brickwork"),
    ("s&p", "S&P Global"),
    ("moody", "Moody's"),
]

_RATING_REGEXES = [
    # CRISIL
    re.compile(r'CRISIL\s+(AAA|AA\+|AA|A1\+|A1|A\+|A|BBB\+|BBB)', re.IGNORECASE),
    # ICRA (often [ICRA]AA+ format)
    re.compile(r'\[?ICRA\]?\s*(AAA|AA\+|AA|A1\+|A1|A\+|A|BBB\+|BBB)', re.IGNORECASE),
    # India Ratings (IND AAA)
    re.compile(r'IND\s+(AAA|AA\+|AA|A1\+|A1|A\+|A|BBB\+|BBB)', re.IGNORECASE),
    # CARE
    re.compile(r'CARE\s+(AAA|AA\+|AA|A1\+|A1|A\+|A|BBB\+|BBB)', re.IGNORECASE),
    # Generic "Rating: AAA" or "rated AAA"
    re.compile(r'(?:rating|rated)[:\s]+(AAA|AA\+|AA|A1\+|A1|A\+|A|BBB\+|BBB|BB\+|BB|B\+|B)', re.IGNORECASE),
    # Outlook
    re.compile(r'(Stable|Positive|Negative|Watch)\s*(?:Outlook|outlook)', re.IGNORECASE),
    # S&P style
    re.compile(r'(?:S&P|upgraded|downgraded|affirmed).*?(?:to|at)\s+[\'\"]*([A-Z]{1,3}[\+\-]?)[\'\"]*', re.IGNORECASE),
]


def _detect_agency(filename):
    """Detect rating agency from PDF filename."""
    fname_lower = filename.lower()
    for key, name in _AGENCY_PATTERNS:
        if key in fname_lower:
            return name
    return "Unknown"


def _extract_ratings_from_pdf(pdf_bytes):
    """Extract rating strings from first 3 pages of a PDF."""
    ratings = []
    outlook = None
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages[:3]:
            text += page.extract_text() or ""
        for regex in _RATING_REGEXES:
            for m in regex.finditer(text):
                val = m.group(1).strip()
                if val.lower() in ("stable", "positive", "negative", "watch"):
                    outlook = val.capitalize()
                else:
                    ratings.append(val.upper())
    except Exception:
        pass
    return list(dict.fromkeys(ratings)), outlook  # deduplicated, preserving order


def fetch_credit_ratings(symbol):
    """Fetch credit rating filings from NSE corporate announcements."""
    print("\n[2/5] Fetching credit ratings from NSE filings...")
    results = []
    try:
        session = _nse_session()
        url = "https://www.nseindia.com/api/corporate-announcements"
        cache_key = "credit_ratings_%s" % symbol
        filings = _nse_get_json(session, url, params={
            "index": "equities", "symbol": symbol, "subject": "Credit Rating",
        }, cache_key=cache_key)

        if not filings or not isinstance(filings, list):
            print("  No credit rating filings found on NSE")
            return results

        print("  Found %d credit rating filings" % len(filings))

        # Process up to 10 most recent filings
        for item in filings[:10]:
            pdf_url = item.get("attchmntFile", "")
            date_str = item.get("an_dt", "")
            if not pdf_url:
                continue

            fname = pdf_url.split("/")[-1]
            agency = _detect_agency(fname)

            # Download and parse PDF for actual ratings
            extracted_ratings = []
            outlook = None
            pdf_content = None
            try:
                pdf_content = _nse_download_pdf(session, pdf_url)
                if pdf_content:
                    extracted_ratings, outlook = _extract_ratings_from_pdf(pdf_content)
            except Exception:
                pass

            # If agency unknown, try to detect from PDF text
            if agency == "Unknown" and extracted_ratings and pdf_content:
                try:
                    reader = PyPDF2.PdfReader(io.BytesIO(pdf_content))
                    page_text = (reader.pages[0].extract_text() or "").lower()
                    for key, name in _AGENCY_PATTERNS:
                        if key in page_text:
                            agency = name
                            break
                    if agency == "Unknown" and "s&p" in page_text:
                        agency = "S&P Global"
                except Exception:
                    pass

            entry = {
                "date": date_str,
                "agency": agency,
                "ratings": extracted_ratings,
                "outlook": outlook,
                "pdf_url": pdf_url,
            }
            results.append(entry)

            rating_str = ", ".join(extracted_ratings) if extracted_ratings else "(see PDF)"
            outlook_str = " / %s" % outlook if outlook else ""
            print("  %s | %-25s | %s%s" % (date_str[:11], agency, rating_str, outlook_str))

    except Exception as e:
        print("  Credit rating fetch failed: %s" % e)

    return results


# ── ESM (Enhanced Surveillance Measure) Checker ──────────────────────────────

def fetch_esm_status(symbol):
    """Check if stock is in ESM (Enhanced Surveillance Measure) stage on NSE."""
    print("\n  Checking ESM (Enhanced Surveillance Measure) status...")
    result = {"in_esm": False, "stage": None, "details": ""}
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://www.nseindia.com/",
        })
        session.get("https://www.nseindia.com/", timeout=10)

        # Approach 1: Check quote-equity for surveillance info
        try:
            resp = session.get("https://www.nseindia.com/api/quote-equity",
                               params={"symbol": symbol}, timeout=15)
            if resp.status_code == 200:
                qdata = resp.json()
                sec_info = qdata.get("securityInfo", {})
                surv = sec_info.get("surveillance", {})
                surv_str = str(surv).lower() if surv else ""
                if "esm" in surv_str:
                    stage = "Stage II" if any(s in surv_str for s in ["stage ii", "stage 2", "stg2"]) else "Stage I"
                    result = {"in_esm": True, "stage": stage, "details": surv}
        except Exception:
            pass

        # Approach 2: Check merged-daily-reports for ESM lists
        if not result["in_esm"]:
            for stage_label, key in [("Stage I", "favESMStg1"), ("Stage II", "favESMStg2")]:
                try:
                    resp = session.get("https://www.nseindia.com/api/merged-daily-reports",
                                       params={"key": key}, timeout=15)
                    if resp.status_code == 200:
                        rdata = resp.json()
                        stocks = rdata if isinstance(rdata, list) else rdata.get("data", [])
                        for stock in stocks:
                            sym = stock.get("symbol", stock.get("SYMBOL", ""))
                            if sym.upper() == symbol.upper():
                                result = {"in_esm": True, "stage": stage_label, "details": stock}
                                break
                    if result["in_esm"]:
                        break
                except Exception:
                    pass

        # Approach 3: Check live-analysis-variation-ban (ESM stocks appear here too)
        if not result["in_esm"]:
            try:
                resp = session.get("https://www.nseindia.com/api/live-analysis-esm",
                                   timeout=15)
                if resp.status_code == 200:
                    rdata = resp.json()
                    stocks = rdata if isinstance(rdata, list) else rdata.get("data", [])
                    for stock in stocks:
                        sym = stock.get("symbol", stock.get("SYMBOL", ""))
                        if sym.upper() == symbol.upper():
                            stage = stock.get("stage", stock.get("esmStage", "Unknown"))
                            result = {"in_esm": True, "stage": str(stage), "details": stock}
                            break
            except Exception:
                pass

        if result["in_esm"]:
            print("  WARNING: %s IS IN ESM %s!" % (symbol, result["stage"]))
        else:
            print("  %s is NOT in any ESM stage (normal trading)" % symbol)

    except Exception as e:
        print("  ESM check failed: %s" % e)

    return result


# ── Promoter Holding Fetcher ─────────────────────────────────────────────────

def fetch_promoter_holding(symbol):
    """Fetch promoter shareholding data from NSE."""
    print("\n  Fetching promoter shareholding data...")
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://www.nseindia.com/",
        })
        session.get("https://www.nseindia.com/", timeout=10)

        # Try NSE corporate-shareholding API
        data = []
        try:
            resp = session.get(
                "https://www.nseindia.com/api/corporate-shareholding",
                params={"symbol": symbol, "index": "equities"}, timeout=15)
            if resp.status_code == 200:
                raw = resp.json()
                if isinstance(raw, list):
                    for item in raw:
                        entry = {
                            "date": item.get("date", item.get("an_dt", "")),
                            "promoter_pct": _parse_float(item.get("promoterAndPromoterGroup",
                                                item.get("promoter", item.get("val1", "")))),
                            "pledge_pct": _parse_float(item.get("pledgePercent",
                                            item.get("promoterPledge", item.get("val4", "")))),
                            "public_pct": _parse_float(item.get("public",
                                            item.get("val2", ""))),
                        }
                        data.append(entry)
                elif isinstance(raw, dict):
                    for key in ["data", "shareholding", "results"]:
                        if key in raw and isinstance(raw[key], list):
                            for item in raw[key]:
                                entry = {
                                    "date": item.get("date", ""),
                                    "promoter_pct": _parse_float(item.get("promoterAndPromoterGroup",
                                                     item.get("promoter", ""))),
                                    "pledge_pct": _parse_float(item.get("pledgePercent",
                                                    item.get("promoterPledge", ""))),
                                }
                                data.append(entry)
                            break
        except Exception:
            pass

        # Fallback: Try quote-equity for basic shareholding
        if not data:
            try:
                resp = session.get("https://www.nseindia.com/api/quote-equity",
                                   params={"symbol": symbol}, timeout=15)
                if resp.status_code == 200:
                    qdata = resp.json()
                    sec_info = qdata.get("securityInfo", {})
                    # Some quote responses include shareholding percentages
                    prom = _parse_float(sec_info.get("promoterHolding",
                                sec_info.get("promoterPercentage", "")))
                    pledge = _parse_float(sec_info.get("promoterPledge",
                                 sec_info.get("pledgedPercentage", "")))
                    if not _nan(prom):
                        data.append({
                            "date": "Latest",
                            "promoter_pct": prom,
                            "pledge_pct": pledge if not _nan(pledge) else 0.0,
                        })
            except Exception:
                pass

        if data:
            print("  Found %d shareholding records" % len(data))
            for d in data[:3]:
                pp = d.get("promoter_pct", float("nan"))
                pl = d.get("pledge_pct", float("nan"))
                print("    %s | Promoter: %s | Pledge: %s" % (
                    d.get("date", "?"),
                    "%.1f%%" % pp if not _nan(pp) else "N/A",
                    "%.1f%%" % pl if not _nan(pl) else "N/A"))
            return {"data": data}
        else:
            print("  No shareholding data from NSE API")
            return None

    except Exception as e:
        print("  Promoter holding fetch failed: %s" % e)
        return None


def _promoter_from_yfinance(info):
    """Extract promoter holding from yfinance info as fallback."""
    pct = info.get("heldPercentInsiders")
    if pct is not None:
        try:
            val = float(pct) * 100  # yfinance returns as fraction
            if 0 < val <= 100:
                return {"data": [{"date": "Latest (yfinance)", "promoter_pct": val, "pledge_pct": float("nan")}]}
        except (TypeError, ValueError):
            pass
    return None


# ── Auto-Fetch: Concall Transcripts & Annual Report/Investor Presentations ──

def _nse_session():
    """Create an NSE session with cookies set. Retries cookie fetch."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, application/pdf, */*",
        "Referer": "https://www.nseindia.com/",
    })
    for attempt in range(3):
        try:
            resp = session.get("https://www.nseindia.com/", timeout=10)
            if resp.status_code == 200:
                break
        except Exception:
            time.sleep(1.5 ** attempt)
    return session


def fetch_concall_transcripts(symbol, max_transcripts=6):
    """
    Auto-fetch concall/earnings-call transcripts from NSE corporate filings.
    Downloads PDFs and extracts text via PyPDF2.
    Returns list of dicts: [{quarter, text}, ...]
    """
    print("\n  Fetching concall transcripts from NSE filings...")
    results = []

    try:
        session = _nse_session()
        url = "https://www.nseindia.com/api/corporate-announcements"
        cache_key = "concall_%s" % symbol
        filings = _nse_get_json(session, url, params={
            "index": "equities",
            "symbol": symbol,
            "subject": "Transcript of Analysts/Institutional Investor Meet/Con. Call",
        }, cache_key=cache_key)

        if not filings or not isinstance(filings, list):
            print("    No concall transcript filings found on NSE")
            return results

        print("    Found %d transcript filings on NSE" % len(filings))

        for item in filings[:max_transcripts]:
            pdf_url = item.get("attchmntFile", "")
            date_str = item.get("an_dt", "")
            if not pdf_url:
                continue

            quarter_label = _quarter_from_date(date_str)

            try:
                pdf_bytes = _nse_download_pdf(session, pdf_url)
                if not pdf_bytes:
                    print("    Failed to download: %s" % pdf_url.split("/")[-1])
                    continue

                reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
                pages_text = []
                for page in reader.pages:
                    pt = page.extract_text()
                    if pt:
                        pages_text.append(pt)
                text = "\n".join(pages_text)

                if len(text) < 200:
                    print("    Skipped (too little text): %s" % pdf_url.split("/")[-1])
                    continue

                results.append({"quarter": quarter_label, "text": text})
                print("    Loaded: %s (%d pages, %d chars)" % (
                    quarter_label, len(reader.pages), len(text)))

            except Exception as e:
                print("    Failed to parse PDF: %s" % e)
                continue

    except Exception as e:
        print("    Concall transcript fetch failed: %s" % e)

    print("    Total concall transcripts loaded: %d" % len(results))
    return results


def fetch_investor_presentations(symbol, max_docs=4):
    """
    Auto-fetch investor presentations from NSE corporate filings.
    Returns list of dicts: [{year, text}, ...]
    """
    print("  Fetching investor presentations / annual report filings...")
    results = []

    try:
        session = _nse_session()
        url = "https://www.nseindia.com/api/corporate-announcements"

        subjects = [
            "Investor Presentation",
            "Annual General Meeting",
            "Annual Report",
            "Notice of Annual General Meeting",
        ]

        all_filings = []
        for subject in subjects:
            cache_key = "inv_pres_%s_%s" % (symbol, subject.replace(" ", "_"))
            filings = _nse_get_json(session, url, params={
                "index": "equities",
                "symbol": symbol,
                "subject": subject,
            }, cache_key=cache_key)
            if filings and isinstance(filings, list):
                for f in filings:
                    f["_source_subject"] = subject
                all_filings.extend(filings)

        if not all_filings:
            print("    No investor presentation / AGM filings found on NSE")
            return results

        seen_urls = set()
        unique_filings = []
        for f in all_filings:
            pdf_url = f.get("attchmntFile", "")
            if pdf_url and pdf_url not in seen_urls:
                seen_urls.add(pdf_url)
                unique_filings.append(f)

        print("    Found %d relevant filings" % len(unique_filings))

        for item in unique_filings[:max_docs]:
            pdf_url = item.get("attchmntFile", "")
            date_str = item.get("an_dt", "")
            source = item.get("_source_subject", "")
            if not pdf_url:
                continue

            year_label = _year_from_date(date_str)

            try:
                pdf_bytes = _nse_download_pdf(session, pdf_url)
                if not pdf_bytes:
                    continue

                reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
                pages_text = []
                for page in reader.pages:
                    pt = page.extract_text()
                    if pt:
                        pages_text.append(pt)
                text = "\n".join(pages_text)

                if len(text) < 300:
                    continue

                results.append({"year": year_label, "text": text})
                print("    Loaded [%s]: %s (%d pages, %d chars)" % (
                    source, year_label, len(reader.pages), len(text)))

            except Exception as e:
                print("    Failed to parse: %s" % e)
                continue

    except Exception as e:
        print("    Investor presentation fetch failed: %s" % e)

    print("    Total documents loaded for NLP: %d" % len(results))
    return results


def _quarter_from_date(date_str):
    """Convert NSE date string to quarter label (e.g., 'Q2 FY2023')."""
    try:
        # Format: "21-Nov-2022 15:58:57" or "21-Nov-2022"
        dt = datetime.datetime.strptime(date_str[:11].strip(), "%d-%b-%Y")
        month = dt.month
        year = dt.year
        # Indian FY: Apr-Jun=Q1, Jul-Sep=Q2, Oct-Dec=Q3, Jan-Mar=Q4
        if month in (4, 5, 6):
            return "Q1 FY%d" % (year + 1)
        elif month in (7, 8, 9):
            return "Q2 FY%d" % (year + 1)
        elif month in (10, 11, 12):
            return "Q3 FY%d" % (year + 1)
        else:
            return "Q4 FY%d" % year
    except Exception:
        return date_str[:11] if date_str else "Unknown"


def _year_from_date(date_str):
    """Convert NSE date string to FY label."""
    try:
        dt = datetime.datetime.strptime(date_str[:11].strip(), "%d-%b-%Y")
        month = dt.month
        year = dt.year
        fy = year + 1 if month >= 4 else year
        return "FY%d" % fy
    except Exception:
        return date_str[:11] if date_str else "Unknown"


# ── Auto-Fetch: Shareholding Pattern History (Quarterly FII/DII/Promoter) ────

def fetch_shareholding_history(symbol, max_quarters=12):
    """
    Fetch quarterly shareholding pattern from NSE.
    Returns list of dicts with promoter_pct, public_pct, date for each quarter.
    """
    print("  Fetching shareholding pattern history...")
    results = []
    try:
        session = _nse_session()
        cache_key = "shp_%s" % symbol
        data = _nse_get_json(session,
            "https://www.nseindia.com/api/corporate-share-holdings-master",
            params={"symbol": symbol, "index": "equities"},
            cache_key=cache_key,
        )

        if not data or not isinstance(data, list):
            print("    No shareholding data available")
            return results

        for item in data[:max_quarters]:
            entry = {
                "date": item.get("date", ""),
                "promoter_pct": _parse_float(item.get("pr_and_prgrp", "0")),
                "public_pct": _parse_float(item.get("public_val", "0")),
                "employee_trusts_pct": _parse_float(item.get("employeeTrusts", "0")),
            }
            results.append(entry)

        if results:
            print("    Loaded %d quarters of shareholding data (%s to %s)" % (
                len(results), results[-1]["date"], results[0]["date"]))
            print("    Latest: Promoter %.1f%% | Public %.1f%%" % (
                results[0]["promoter_pct"], results[0]["public_pct"]))
    except Exception as e:
        print("    Shareholding history fetch failed: %s" % e)

    return results


# ── Auto-Fetch: Insider/Promoter Buy-Sell (SAST Disclosures) ─────────────────

def fetch_sast_disclosures(symbol, max_filings=10):
    """
    Fetch SAST (Substantial Acquisition of Shares) disclosures from NSE.
    Returns list of dicts with date, action, shares.
    """
    print("  Fetching SAST / insider trading disclosures...")
    results = []
    try:
        session = _nse_session()
        cache_key = "sast_%s" % symbol
        filings = _nse_get_json(session,
            "https://www.nseindia.com/api/corporate-announcements",
            params={
                "index": "equities",
                "symbol": symbol,
                "subject": "Disc. under Reg.30 of SEBI (SAST) Reg.2011",
            },
            cache_key=cache_key,
        )
        if not filings or not isinstance(filings, list):
            filings = []

        # Also check for Takeover disclosures
        cache_key2 = "sast_takeover_%s" % symbol
        more = _nse_get_json(session,
            "https://www.nseindia.com/api/corporate-announcements",
            params={
                "index": "equities",
                "symbol": symbol,
                "subject": "Disclosure under SEBI Takeover Regulations",
            },
            cache_key=cache_key2,
        )
        if more and isinstance(more, list):
            filings.extend(more)

        if not filings:
            print("    No SAST/insider trading disclosures found")
            return results

        print("    Found %d SAST/insider disclosures" % len(filings))

        for item in filings[:max_filings]:
            pdf_url = item.get("attchmntFile", "")
            date_str = item.get("an_dt", "")
            desc = item.get("desc", "")

            entry = {
                "date": date_str[:11] if date_str else "",
                "description": desc,
                "pdf_url": pdf_url,
                "action": "Unknown",
                "shares": 0,
                "pct_change": 0,
            }

            if pdf_url:
                try:
                    pdf_bytes = _nse_download_pdf(session, pdf_url)
                    if pdf_bytes:
                        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
                        text = ""
                        for page in reader.pages[:3]:
                            text += (page.extract_text() or "")
                        text_lower = text.lower()

                        if "acquisition" in text_lower or "purchase" in text_lower or "bought" in text_lower:
                            entry["action"] = "BUY"
                        elif "disposal" in text_lower or "sold" in text_lower or "sale" in text_lower:
                            entry["action"] = "SELL"

                        share_match = re.search(r'(\d[\d,]+)\s*(?:equity\s+)?shares?', text, re.IGNORECASE)
                        if share_match:
                            entry["shares"] = int(share_match.group(1).replace(",", ""))

                        pct_match = re.search(r'(\d+\.?\d*)\s*%\s*(?:of|after)', text)
                        if pct_match:
                            entry["pct_change"] = float(pct_match.group(1))
                except Exception:
                    pass

            results.append(entry)

        buys = sum(1 for r in results if r["action"] == "BUY")
        sells = sum(1 for r in results if r["action"] == "SELL")
        print("    Parsed: %d buys, %d sells, %d unknown" % (buys, sells, len(results) - buys - sells))

    except Exception as e:
        print("    SAST fetch failed: %s" % e)

    return results


# ── Auto-Fetch: Delivery & Volume Data ───────────────────────────────────────

def fetch_delivery_data(symbol):
    """
    Fetch current delivery percentage and volume info from NSE trade_info.
    Returns dict with delivery_pct, volume_traded, etc.
    """
    print("  Fetching delivery / volume data...")
    result = {}
    try:
        session = _nse_session()
        # Delivery data changes daily — short cache key with date
        today = datetime.datetime.now().strftime("%Y%m%d")
        cache_key = "delivery_%s_%s" % (symbol, today)
        data = _nse_get_json(session,
            "https://www.nseindia.com/api/quote-equity",
            params={"symbol": symbol, "section": "trade_info"},
            cache_key=cache_key,
        )
        if not data:
            return result

        dp = data.get("securityWiseDP", {})
        if dp:
            result = {
                "quantity_traded": dp.get("quantityTraded", 0),
                "delivery_quantity": dp.get("deliveryQuantity", 0),
                "delivery_pct": dp.get("deliveryToTradedQuantity", 0),
                "date": dp.get("secWiseDelPosDate", ""),
            }
            print("    Delivery: %.1f%% | Volume: %d | Date: %s" % (
                result["delivery_pct"], result["quantity_traded"], result["date"]))

    except Exception as e:
        print("    Delivery data fetch failed: %s" % e)

    return result


# ── Auto-Fetch: Sector/Industry Classification & Peer List ───────────────────

def fetch_sector_peers(symbol):
    """
    Get sector classification and find peer stocks from the same NSE sector index.
    Returns dict with sector_info and list of peer symbols.
    """
    print("  Fetching sector peers...")
    result = {"sector_info": {}, "peers": [], "sector_index": ""}
    try:
        session = _nse_session()

        # Step 1: Get company's sector info from quote
        cache_key = "quote_%s" % symbol
        data = _nse_get_json(session,
            "https://www.nseindia.com/api/quote-equity",
            params={"symbol": symbol},
            cache_key=cache_key,
        )
        if not data:
            return result

        industry_info = data.get("industryInfo", {})
        metadata = data.get("metadata", {})

        result["sector_info"] = {
            "macro": industry_info.get("macro", ""),
            "sector": industry_info.get("sector", ""),
            "industry": industry_info.get("industry", ""),
            "basic_industry": industry_info.get("basicIndustry", ""),
            "sector_pe": metadata.get("pdSectorPe", 0),
            "sector_index": metadata.get("pdSectorInd", ""),
        }

        sector_idx = metadata.get("pdSectorInd", "")
        result["sector_index"] = sector_idx

        if not sector_idx:
            print("    No sector index found for %s" % symbol)
            return result

        # Step 2: Fetch all stocks in that sector index
        time.sleep(0.5)
        cache_key2 = "sector_idx_%s" % sector_idx.replace(" ", "_")
        idx_data = _nse_get_json(session,
            "https://www.nseindia.com/api/equity-stockIndices",
            params={"index": sector_idx},
            cache_key=cache_key2,
        )
        if idx_data:
            stocks = idx_data.get("data", [])
            for stock in stocks:
                sym = stock.get("symbol", "")
                if sym and sym != symbol and sym != sector_idx:
                    result["peers"].append({
                        "symbol": sym,
                        "last_price": stock.get("lastPrice", 0),
                        "change_1y": stock.get("perChange365d", 0),
                        "change_30d": stock.get("perChange30d", 0),
                    })

        print("    Sector: %s | Index: %s | Peers found: %d" % (
            industry_info.get("sector", "N/A"), sector_idx, len(result["peers"])))

    except Exception as e:
        print("    Sector peer fetch failed: %s" % e)

    return result


# ── Auto-Fetch: Corporate Actions (Dividends, Splits, Bonus) ─────────────────

def fetch_corporate_actions(symbol):
    """
    Fetch corporate action history from yfinance (dividends, splits).
    Returns dict with dividend history, splits, and bonus history.
    """
    print("  Fetching corporate actions history...")
    result = {"dividends": [], "splits": [], "actions_summary": ""}
    try:
        ticker, yf_symbol, _, _ = _resolve_yf_ticker(symbol)

        # Dividends
        divs = ticker.dividends
        if divs is not None and not divs.empty:
            for date, amount in divs.tail(20).items():
                result["dividends"].append({
                    "date": date.strftime("%Y-%m-%d"),
                    "amount": float(amount),
                })
            print("    Dividends: %d records (last 5Y)" % len(result["dividends"]))

        # Splits
        splits = ticker.splits
        if splits is not None and not splits.empty:
            for date, ratio in splits.items():
                if ratio != 1.0:
                    result["splits"].append({
                        "date": date.strftime("%Y-%m-%d"),
                        "ratio": str(ratio),
                    })
            if result["splits"]:
                print("    Splits: %d events" % len(result["splits"]))

        # Summary
        total_div = sum(d["amount"] for d in result["dividends"])
        result["actions_summary"] = "%d dividends (total Rs. %.1f), %d splits" % (
            len(result["dividends"]), total_div, len(result["splits"]))

    except Exception as e:
        print("    Corporate actions fetch failed: %s" % e)

    return result


# ── Auto-Fetch: Related Party Transactions (NSE Filings) ─────────────────────

def fetch_related_party_filings(symbol, max_filings=5):
    """
    Fetch related party transaction disclosures from NSE.
    Returns list of RPT filing details.
    """
    print("  Fetching related party transaction filings...")
    results = []
    try:
        session = _nse_session()
        cache_key = "rpt_%s" % symbol
        filings = _nse_get_json(session,
            "https://www.nseindia.com/api/corporate-announcements",
            params={
                "index": "equities",
                "symbol": symbol,
                "subject": "Related Party Transaction",
            },
            cache_key=cache_key,
        )

        if not filings or not isinstance(filings, list):
            cache_key2 = "rpt2_%s" % symbol
            filings = _nse_get_json(session,
                "https://www.nseindia.com/api/corporate-announcements",
                params={
                    "index": "equities",
                    "symbol": symbol,
                    "subject": "Related Party Transactions",
                },
                cache_key=cache_key2,
            )

        if not filings or not isinstance(filings, list):
            print("    No RPT filings found")
            return results

        print("    Found %d RPT filings" % len(filings))

        for item in filings[:max_filings]:
            pdf_url = item.get("attchmntFile", "")
            date_str = item.get("an_dt", "")

            entry = {
                "date": date_str[:11] if date_str else "",
                "pdf_url": pdf_url,
                "amount_cr": 0,
                "parties": [],
                "nature": "",
            }

            if pdf_url:
                try:
                    pdf_bytes = _nse_download_pdf(session, pdf_url)
                    if pdf_bytes:
                        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
                        text = ""
                        for page in reader.pages[:5]:
                            text += (page.extract_text() or "")

                        amounts = re.findall(
                            r'(?:Rs\.?|INR)\s*([\d,]+(?:\.\d+)?)\s*(?:Cr|crore|Lacs|lakh)',
                            text, re.IGNORECASE,
                        )
                        if amounts:
                            entry["amount_cr"] = max(float(a.replace(",", "")) for a in amounts)

                        text_lower = text.lower()
                        if "loan" in text_lower:
                            entry["nature"] = "Loan"
                        elif "sale" in text_lower or "purchase" in text_lower:
                            entry["nature"] = "Sale/Purchase"
                        elif "service" in text_lower or "contract" in text_lower:
                            entry["nature"] = "Services/Contract"
                        elif "guarantee" in text_lower:
                            entry["nature"] = "Guarantee"
                except Exception:
                    pass

            results.append(entry)

    except Exception as e:
        print("    RPT fetch failed: %s" % e)

    return results


# ── Auto-Fetch: Mutual Fund Holdings (from yfinance) ─────────────────────────

def fetch_mutual_fund_data(symbol):
    """
    Fetch mutual fund and institutional holder data from yfinance.
    Returns dict with major holders, institutional holders, MF holders.
    """
    print("  Fetching mutual fund / institutional holder data...")
    result = {"major_holders": {}, "institutional_holders": [], "mf_holders": []}
    try:
        ticker, yf_symbol, _, _ = _resolve_yf_ticker(symbol)

        # Major holders (% breakdown)
        try:
            mh = ticker.major_holders
            if mh is not None and not mh.empty:
                for _, row in mh.iterrows():
                    result["major_holders"][row.iloc[1]] = row.iloc[0]
        except Exception:
            pass

        # Institutional holders (top institutions)
        try:
            ih = ticker.institutional_holders
            if ih is not None and not ih.empty:
                for _, row in ih.iterrows():
                    result["institutional_holders"].append({
                        "holder": str(row.get("Holder", "")),
                        "shares": int(row.get("Shares", 0)),
                        "pct_out": float(row.get("% Out", 0)) if row.get("% Out") else 0,
                        "value": float(row.get("Value", 0)) if row.get("Value") else 0,
                    })
                print("    Institutional holders: %d entries" % len(result["institutional_holders"]))
        except Exception:
            pass

        # Mutual fund holders
        try:
            mf = ticker.mutualfund_holders
            if mf is not None and not mf.empty:
                for _, row in mf.iterrows():
                    result["mf_holders"].append({
                        "holder": str(row.get("Holder", "")),
                        "shares": int(row.get("Shares", 0)),
                        "pct_out": float(row.get("% Out", 0)) if row.get("% Out") else 0,
                    })
                print("    Mutual fund holders: %d entries" % len(result["mf_holders"]))
        except Exception:
            pass

    except Exception as e:
        print("    MF/institutional data fetch failed: %s" % e)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# SCREENER.IN FALLBACK — universal financials source (NSE + BSE incl. SME)
# Used to backfill yfinance gaps. Screener publishes 6-10 years of P&L, BS, CF
# in HTML form for every listed Indian stock (consolidated AND standalone).
# Values are in Rs. crore; we convert to absolute INR (×1e7) so downstream
# code that expects yfinance-shape numbers works unchanged.
# ══════════════════════════════════════════════════════════════════════════════

def _screener_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0",
        "Accept": "text/html,application/json,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _screener_resolve_url(symbol):
    """Resolve user symbol -> canonical screener.in /company/<slug>/ URL.
    Tries /company/<SYMBOL>/ directly, then any cached yfinance-resolved
    ticker (without exchange suffix), then the search API, then the
    company-info longName from the resolved yfinance Ticker.
    """
    cache_key = "screener_url_%s" % symbol
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached or None
    s = _screener_session()

    # Build a list of candidate query strings
    candidates = [symbol]
    # If yfinance already resolved this symbol, also try the ticker
    # (e.g. YASHHIGHVOLTAGE -> YASHHV.BO -> 'YASHHV') and the company name
    if symbol in _RESOLVED_TICKERS:
        _t, yf_sym, info, _sme = _RESOLVED_TICKERS[symbol]
        base = yf_sym.split(".")[0]
        if base and base not in candidates:
            candidates.append(base)
        nm = (info or {}).get("longName") or (info or {}).get("shortName") or ""
        if nm:
            # Strip common suffixes
            nm2 = re.sub(r"\s+(Ltd|Limited|Ltd\.|Limited\.|Inc|Corporation|Corp)\.?$", "", nm, flags=re.I)
            candidates.append(nm2)

    # 1) Direct /company/<X>/ URL
    for q in candidates:
        try:
            r = s.get("https://www.screener.in/company/%s/" % q.replace(" ", "%20"),
                      timeout=15, allow_redirects=True)
            if r.status_code == 200 and "Profit & Loss" in r.text:
                url = r.url.rstrip("/") + "/"
                _cache_set(cache_key, url)
                return url
        except Exception:
            pass

    # 2) Search API (try each candidate)
    for q in candidates:
        try:
            r = s.get("https://www.screener.in/api/company/search/",
                      params={"q": q, "v": 3}, timeout=15)
            if r.status_code == 200:
                hits = r.json() or []
                if hits and isinstance(hits, list):
                    rel = hits[0].get("url", "")
                    if rel:
                        url = "https://www.screener.in" + rel
                        _cache_set(cache_key, url)
                        return url
        except Exception:
            pass

    _cache_set(cache_key, "")
    return None


_SCREENER_NUM_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _screener_parse_num(s):
    """Parse a Screener cell: '38', '-29', '21%', '1,234.5', '' -> float (NaN if blank)."""
    if not s or s in ("-", ""):
        return float("nan")
    s = s.replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _screener_parse_table(html, section_id):
    """Extract a Screener section's data table as (headers, {label: [values]}).
    headers = list of period strings (e.g. ['Mar 2020','Mar 2021',...,'TTM'])
    """
    sec_re = re.compile(
        r'<section[^>]*id="' + re.escape(section_id) + r'"[^>]*>([\s\S]*?)</section>')
    sec = sec_re.search(html)
    if not sec:
        return [], {}
    body = sec.group(1)
    tab_re = re.compile(r'(<table[\s\S]*?</table>)')
    tab = tab_re.search(body)
    if not tab:
        return [], {}
    t = tab.group(1)
    raw_heads = re.findall(r'<th[^>]*>([\s\S]*?)</th>', t)
    headers = [re.sub(r'<[^>]+>', '', h).strip() for h in raw_heads]
    headers = [h for h in headers if h and h.lower() != "&nbsp;"]
    rows_html = re.findall(r'<tr[^>]*>([\s\S]*?)</tr>', t)
    data = {}
    for row in rows_html:
        lm = re.search(r'<td class="text"[^>]*>([\s\S]*?)</td>', row)
        if not lm:
            continue
        label = re.sub(r'<[^>]+>', '', lm.group(1))
        label = label.replace("\xa0", " ").replace("&nbsp;", " ")
        label = re.sub(r"\s+", " ", label).replace(" +", "").strip()
        if not label:
            continue
        # Numeric cells
        cells = re.findall(r'<td[^>]*class="[^"]*"[^>]*>\s*([\s\S]*?)\s*</td>', row)
        # The first td was the label; skip it
        vals_raw = []
        for c in cells:
            txt = re.sub(r'<[^>]+>', '', c)
            txt = txt.replace("\xa0", " ").replace("&nbsp;", " ")
            txt = re.sub(r"\s+", " ", txt).replace(" +", "").strip()
            vals_raw.append(txt)
        # Drop the label cell (first occurrence)
        if vals_raw and vals_raw[0] == label:
            vals_raw = vals_raw[1:]
        # Normalize length to headers
        vals = [_screener_parse_num(v) for v in vals_raw]
        data[label] = vals
    return headers, data


# Mapping: Screener row label -> yfinance-style row name(s) used by ALIASES.
# Values from Screener are in Rs. Crore -> multiply by 1e7 unless tagged "ratio".
_SCREENER_PNL_MAP = {
    "Sales":             ("Total Revenue", 1e7),
    "Revenue":           ("Total Revenue", 1e7),
    "Expenses":          ("Total Expenses", 1e7),
    "Operating Profit":  ("EBITDA", 1e7),
    "Other Income":      ("Interest Income Non Operating", 1e7),
    "Interest":          ("Interest Expense", 1e7),
    "Depreciation":      ("Reconciled Depreciation", 1e7),
    "Profit before tax": ("Pretax Income", 1e7),
    "Net Profit":        ("Net Income", 1e7),
    "EPS in Rs":         ("Basic EPS", 1.0),
}

_SCREENER_BS_MAP = {
    "Equity Capital":      ("Common Stock", 1e7),
    "Reserves":            ("Retained Earnings", 1e7),
    "Borrowings":          ("Total Debt", 1e7),
    "Other Liabilities":   ("Other Current Liabilities", 1e7),
    "Total Liabilities":   ("Total Liabilities Net Minority Interest", 1e7),
    "Fixed Assets":        ("Net PPE", 1e7),
    "CWIP":                ("Construction In Progress", 1e7),
    "Investments":         ("Investments And Advances", 1e7),
    "Other Assets":        ("Other Current Assets", 1e7),
    "Total Assets":        ("Total Assets", 1e7),
}

_SCREENER_CF_MAP = {
    "Cash from Operating Activity":  ("Operating Cash Flow", 1e7),
    "Cash from Investing Activity":  ("Investing Cash Flow", 1e7),
    "Cash from Financing Activity":  ("Financing Cash Flow", 1e7),
    "Free Cash Flow":                ("Free Cash Flow", 1e7),
}


def _screener_section_to_df(headers, raw_data, mapping):
    """Convert (headers, raw rows) + label-mapping -> pandas DataFrame in
    yfinance shape (rows = financial line items, cols = period labels).
    Excludes 'TTM'/'Trailing' columns from annual frames; caller can re-include.
    """
    if not headers or not raw_data:
        return None
    # Filter to annual columns (drop TTM)
    keep_cols = [(i, h) for i, h in enumerate(headers)
                 if h.upper() not in ("TTM", "TRAILING")]
    if not keep_cols:
        return None
    col_labels = [h for _, h in keep_cols]
    # Build dict-of-dicts: {col_label: {yf_row_name: value}}
    out = {h: {} for h in col_labels}
    for screener_label, vals in raw_data.items():
        if screener_label not in mapping:
            continue
        yf_name, mult = mapping[screener_label]
        for (idx, h) in keep_cols:
            if idx >= len(vals):
                continue
            v = vals[idx]
            if not _nan(v):
                out[h][yf_name] = v * mult
    df = pd.DataFrame(out)
    if df.empty:
        return None
    # Reverse columns so newest is first (matches yfinance convention)
    df = df[df.columns[::-1]]
    # Add derived rows: Stockholders Equity = Equity Capital + Reserves; EBIT = EBITDA - Dep; Tax Provision = PBT - NI; Gross Profit = Sales - Expenses
    if mapping is _SCREENER_BS_MAP and "Common Stock" in df.index and "Retained Earnings" in df.index:
        df.loc["Stockholders Equity"] = df.loc["Common Stock"].fillna(0) + df.loc["Retained Earnings"].fillna(0)
        df.loc["Common Stock Equity"] = df.loc["Stockholders Equity"]
    if mapping is _SCREENER_PNL_MAP:
        if "EBITDA" in df.index and "Reconciled Depreciation" in df.index:
            df.loc["EBIT"] = df.loc["EBITDA"].fillna(0) - df.loc["Reconciled Depreciation"].fillna(0)
            df.loc["Operating Income"] = df.loc["EBIT"]
        if "Pretax Income" in df.index and "Net Income" in df.index:
            df.loc["Tax Provision"] = df.loc["Pretax Income"].fillna(0) - df.loc["Net Income"].fillna(0)
        if "Total Revenue" in df.index and "Total Expenses" in df.index:
            df.loc["Gross Profit"] = df.loc["Total Revenue"].fillna(0) - df.loc["Total Expenses"].fillna(0)
    return df


def _screener_period_to_ts(period_str):
    """Convert 'Mar 2025' -> pd.Timestamp at month-end."""
    try:
        return pd.Timestamp(datetime.datetime.strptime(period_str, "%b %Y")) + pd.offsets.MonthEnd(0)
    except Exception:
        try:
            # Try '202503' or other formats
            return pd.Timestamp(period_str)
        except Exception:
            return period_str


def fetch_screener_financials(symbol):
    """Fetch full P&L, BS, CF, Quarterly tables from Screener.in.
    Returns dict with keys: income_annual, balance_annual, cashflow_annual,
    income_quarterly, fy_labels, q_labels, source_url, periods.
    All numeric values converted to absolute INR (yfinance scale).
    """
    print("  [Screener.in] fetching universal financials backup...")
    url = _screener_resolve_url(symbol)
    if not url:
        print("    Could not resolve symbol on Screener")
        return {}

    s = _screener_session()
    cache_key = "screener_html_%s" % symbol
    html = _cache_get(cache_key)
    if html is None:
        # Try consolidated first
        cons_url = url.rstrip("/") + "/consolidated/"
        try:
            r = s.get(cons_url, timeout=20)
            html_cons = r.text if r.status_code == 200 else ""
        except Exception:
            html_cons = ""
        # Standalone
        try:
            r2 = s.get(url, timeout=20)
            html_std = r2.text if r2.status_code == 200 else ""
        except Exception:
            html_std = ""
        # Choose whichever has more populated data cells
        cons_cells = html_cons.count('<td class="">') if html_cons else 0
        std_cells = html_std.count('<td class="">') if html_std else 0
        if cons_cells >= max(std_cells, 30):
            html = html_cons
            chosen = "consolidated"
        elif std_cells >= 10:
            html = html_std
            chosen = "standalone"
        else:
            print("    Screener page has insufficient data")
            return {}
        # Don't cache as JSON (too big); store separately
        try:
            os.makedirs(_CACHE_DIR, exist_ok=True)
            with open(os.path.join(_CACHE_DIR, "screener_%s.html" % hashlib.md5(symbol.encode()).hexdigest()), "w") as f:
                f.write("<!--CHOSEN=%s-->\n" % chosen + html)
        except Exception:
            pass
    else:
        chosen = "cached"

    # Re-load from disk if just written
    try:
        path = os.path.join(_CACHE_DIR, "screener_%s.html" % hashlib.md5(symbol.encode()).hexdigest())
        if not html and os.path.exists(path):
            with open(path) as f:
                html = f.read()
    except Exception:
        pass

    if not html:
        return {}

    out = {"source_url": url, "view": chosen}

    # Profit & Loss (annual)
    pl_heads, pl_rows = _screener_parse_table(html, "profit-loss")
    pl_df = _screener_section_to_df(pl_heads, pl_rows, _SCREENER_PNL_MAP)
    if pl_df is not None:
        # Convert period labels to timestamps for yfinance compatibility
        pl_df.columns = [_screener_period_to_ts(c) for c in pl_df.columns]
        out["income_annual"] = pl_df
        out["fy_labels"] = [_fy_label(c) if hasattr(c, "month") else str(c) for c in pl_df.columns]

    # Balance Sheet (annual)
    bs_heads, bs_rows = _screener_parse_table(html, "balance-sheet")
    bs_df = _screener_section_to_df(bs_heads, bs_rows, _SCREENER_BS_MAP)
    if bs_df is not None:
        bs_df.columns = [_screener_period_to_ts(c) for c in bs_df.columns]
        out["balance_annual"] = bs_df

    # Cash Flow (annual)
    cf_heads, cf_rows = _screener_parse_table(html, "cash-flow")
    cf_df = _screener_section_to_df(cf_heads, cf_rows, _SCREENER_CF_MAP)
    if cf_df is not None:
        cf_df.columns = [_screener_period_to_ts(c) for c in cf_df.columns]
        out["cashflow_annual"] = cf_df

    # Quarterly Results
    q_heads, q_rows = _screener_parse_table(html, "quarters")
    q_df = _screener_section_to_df(q_heads, q_rows, _SCREENER_PNL_MAP)
    if q_df is not None:
        q_df.columns = [_screener_period_to_ts(c) for c in q_df.columns]
        out["income_quarterly"] = q_df
        out["q_labels"] = [c.strftime("%b-%Y") if hasattr(c, "strftime") else str(c) for c in q_df.columns]

    n_pl = pl_df.shape[1] if pl_df is not None else 0
    n_bs = bs_df.shape[1] if bs_df is not None else 0
    n_cf = cf_df.shape[1] if cf_df is not None else 0
    n_q  = q_df.shape[1] if q_df is not None else 0
    print("    Screener (%s view): P&L=%d yrs, BS=%d yrs, CF=%d yrs, Qtr=%d" % (
        chosen, n_pl, n_bs, n_cf, n_q))
    return out


def _merge_screener_into_data(data, sc):
    """Backfill data.* with screener frames where yfinance gave less data.
    Preserves yfinance values whenever they exist (yfinance is more granular for
    main-board stocks); Screener fills gaps and adds missing years.
    """
    def _take_if_better(yf_df, sc_df):
        """Use Screener df only if it has strictly more columns than yfinance,
        OR yfinance is empty/None."""
        if yf_df is None or (hasattr(yf_df, "empty") and yf_df.empty):
            return sc_df
        if sc_df is None:
            return yf_df
        # yf has data — only augment by union of indices, prefer yf values
        merged = sc_df.copy()
        # Align on column timestamps where possible: just keep both column sets,
        # preferring yfinance columns for overlapping months
        for col in yf_df.columns:
            merged[col] = yf_df[col]
        # Sort cols newest-first
        try:
            merged = merged[sorted(merged.columns, reverse=True)]
        except Exception:
            pass
        # Fill missing rows from screener
        for idx in sc_df.index:
            if idx not in merged.index:
                merged.loc[idx] = sc_df.loc[idx]
        # Prefer yfinance values where present
        for col in merged.columns:
            if col in yf_df.columns:
                for idx in yf_df.index:
                    if idx in merged.index and pd.notna(yf_df.loc[idx, col]):
                        merged.loc[idx, col] = yf_df.loc[idx, col]
        return merged

    if "income_annual" in sc:
        data.income_annual = _take_if_better(data.income_annual, sc["income_annual"])
        if data.income_annual is not None and not data.income_annual.empty:
            data.years = data.income_annual.shape[1]
            data.fy_labels = [_fy_label(c) for c in data.income_annual.columns]
    if "balance_annual" in sc:
        data.balance_annual = _take_if_better(data.balance_annual, sc["balance_annual"])
    if "cashflow_annual" in sc:
        data.cashflow_annual = _take_if_better(data.cashflow_annual, sc["cashflow_annual"])
    if "income_quarterly" in sc:
        data.income_quarterly = _take_if_better(data.income_quarterly, sc["income_quarterly"])
        if data.income_quarterly is not None and not data.income_quarterly.empty:
            data.quarters = data.income_quarterly.shape[1]
            data.q_labels = [c.strftime("%b-%Y") if hasattr(c, "strftime") else str(c)
                             for c in data.income_quarterly.columns]


# ── Auto-Fetch: Financial Results Filings (NSE corporate-announcements) ─────
#    Pulls quarterly/annual financial-result PDFs filed by the company directly
#    with the exchange. These are independent of yfinance and work for any
#    listed stock (incl. SME, via the index=sme retry in _nse_get_json).

def fetch_financial_results_filings(symbol, max_filings=12):
    """Fetch quarterly/annual financial result filings (PDFs) from NSE.
    Returns list of dicts: {date, subject, pdf_url, period_hint}.
    """
    print("  Fetching financial result filings (P&L/BS/CF PDFs)...")
    results = []
    seen = set()
    subjects = [
        "Financial Results",
        "Financial Result",
        "Outcome of Board Meeting",
        "Outcome of Meeting",
    ]
    try:
        session = _nse_session()
        url = "https://www.nseindia.com/api/corporate-announcements"
        for subject in subjects:
            cache_key = "fr_%s_%s" % (symbol, subject.replace(" ", "_"))
            filings = _nse_get_json(session, url, params={
                "index": "equities", "symbol": symbol, "subject": subject,
            }, cache_key=cache_key)
            if not filings or not isinstance(filings, list):
                continue
            for item in filings:
                pdf_url = item.get("attchmntFile", "")
                if not pdf_url or pdf_url in seen:
                    continue
                seen.add(pdf_url)
                results.append({
                    "date": item.get("an_dt", "")[:11],
                    "subject": item.get("desc") or subject,
                    "pdf_url": pdf_url,
                    "period_hint": _quarter_from_date(item.get("an_dt", "")),
                })
                if len(results) >= max_filings:
                    break
            if len(results) >= max_filings:
                break
        if results:
            print("    Found %d financial-result filings (latest: %s)" % (
                len(results), results[0]["date"]))
        else:
            print("    No financial-result filings found")
    except Exception as e:
        print("    Financial-results fetch failed: %s" % e)
    return results


def fetch_annual_report_filings(symbol, max_filings=5):
    """Fetch annual report PDFs from NSE corporate-announcements / annual-reports."""
    print("  Fetching annual report filings...")
    results = []
    seen = set()
    try:
        session = _nse_session()
        # Approach 1: corporate-announcements with relevant subjects
        for subject in ("Annual Report", "Notice of Annual General Meeting", "Annual Return"):
            cache_key = "ar_%s_%s" % (symbol, subject.replace(" ", "_"))
            filings = _nse_get_json(session,
                "https://www.nseindia.com/api/corporate-announcements",
                params={"index": "equities", "symbol": symbol, "subject": subject},
                cache_key=cache_key,
            )
            if not filings or not isinstance(filings, list):
                continue
            for item in filings:
                pdf_url = item.get("attchmntFile", "")
                if not pdf_url or pdf_url in seen:
                    continue
                seen.add(pdf_url)
                results.append({
                    "date": item.get("an_dt", "")[:11],
                    "subject": item.get("desc") or subject,
                    "pdf_url": pdf_url,
                    "year": _year_from_date(item.get("an_dt", "")),
                })
                if len(results) >= max_filings:
                    break
            if len(results) >= max_filings:
                break

        # Approach 2: dedicated annual-reports endpoint
        if len(results) < max_filings:
            cache_key = "ar_dedicated_%s" % symbol
            ar_data = _nse_get_json(session,
                "https://www.nseindia.com/api/annual-reports",
                params={"index": "equities", "symbol": symbol},
                cache_key=cache_key,
            )
            if ar_data and isinstance(ar_data, dict):
                for item in (ar_data.get("data") or [])[:max_filings]:
                    pdf_url = item.get("fileName") or item.get("attchmntFile", "")
                    if not pdf_url or pdf_url in seen:
                        continue
                    seen.add(pdf_url)
                    results.append({
                        "date": item.get("submissionDate", "")[:11],
                        "subject": "Annual Report",
                        "pdf_url": pdf_url,
                        "year": item.get("fromYr") or item.get("toYr") or "",
                    })

        if results:
            print("    Found %d annual report filings (latest: %s)" % (
                len(results), results[0].get("date", "?")))
        else:
            print("    No annual report filings found")
    except Exception as e:
        print("    Annual report fetch failed: %s" % e)
    return results


# ── Order Book Extraction from NSE Filings ──────────────────────────────────
# Companies in capital-goods, defence, infra, and EPC sectors report order-book
# / order-inflow data in their press releases and investor presentations filed
# with the exchange.  We scan those PDFs for order-book mentions and extract
# the Rs Crore values to build a quarterly/annual time-series for YoY / QoQ
# comparison.  Companies that don't report order books (IT, pharma, FMCG)
# will simply return an empty list.

# Note: ` (backtick) is included because some PDFs render ₹ as backtick.
_OB_RUPEE = r'(?:Rs\.?|₹|`|INR)'

_OB_LAKH_CRORE_RE = re.compile(
    _OB_RUPEE + r'\s*([\d,.\s]+?)\s*lakh\s*crore', re.I)
_OB_CRORE_RE = re.compile(
    _OB_RUPEE + r'\s*([\d,.\s]+?)\s*(?:crores?|crs?\.?)\b', re.I)
_OB_BILLION_RE = re.compile(
    _OB_RUPEE + r'\s*([\d,.\s]+?)\s*(?:billion|bn\.?)\b', re.I)
# "X lakh" (NOT "lakh crore") → divide by 100 to get crore
_OB_LAKH_RE = re.compile(
    _OB_RUPEE + r'\s*([\d,.\s]+?)\s*(?:lakh|lakhs)\b(?!\s*crore)', re.I)
# Fallback: number + crore WITHOUT currency prefix (safe inside order-book context)
# Use tighter pattern (no spaces in number) to avoid table artifacts
_OB_CRORE_NOPFX_RE = re.compile(
    r'([\d,.]{3,})\s*(?:crores?|crs?\.?)\b', re.I)
_OB_LAKH_NOPFX_RE = re.compile(
    r'([\d,.]{3,})\s*(?:lakh|lakhs)\b(?!\s*crore)', re.I)
_OB_PLAIN_RE = re.compile(
    r'(?:stood\s*at|of\s*(?:about|approx\.?|around|~)?|at\s*(?:about|approx\.?|around|~)?)\s*'
    + _OB_RUPEE + r'?\s*([\d,.\s]{4,})', re.I)


def _parse_ob_number(raw):
    """Parse a number string that may have spaces inside (PDF artefact)."""
    cleaned = re.sub(r'\s+', '', raw).replace(',', '')
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_ob_value(text):
    """Extract order-book value in Rs Crore from a text snippet."""
    text = re.sub(r'\s+', ' ', text)

    # "X,XX,XXX crore" — check BEFORE lakh crore to avoid false matches
    m = _OB_CRORE_RE.search(text)
    if m:
        v = _parse_ob_number(m.group(1))
        if v and 10 < v < 500000:  # sanity: 10 – 5 lakh crore
            return v

    # "X.XX lakh crore" -> multiply by 1,00,000
    m = _OB_LAKH_CRORE_RE.search(text)
    if m:
        v = _parse_ob_number(m.group(1))
        if v and v < 50:  # sanity: < 50 lakh crore (~$600B)
            return v * 100000

    # "X,XXX billion" (LT style) -> ×100 to get approx crore
    m = _OB_BILLION_RE.search(text)
    if m:
        v = _parse_ob_number(m.group(1))
        if v and v > 1:
            return v * 100  # 1 billion ≈ 100 crore

    # "X,XX,XXX lakh" (NOT lakh crore) → ÷100 to get crore
    m = _OB_LAKH_RE.search(text)
    if m:
        v = _parse_ob_number(m.group(1))
        if v and v > 100:  # sanity: at least 100 lakh = 1 crore
            return v / 100

    # Fallback: "X,XXX crore" without currency prefix (safe in OB context)
    m = _OB_CRORE_NOPFX_RE.search(text)
    if m:
        v = _parse_ob_number(m.group(1))
        if v and 10 < v < 500000:  # tighter bounds for no-prefix match
            return v

    # Fallback: "X,XX,XXX lakh" without currency prefix → ÷100
    m = _OB_LAKH_NOPFX_RE.search(text)
    if m:
        v = _parse_ob_number(m.group(1))
        if v and v > 100:
            return v / 100

    # Plain large number near "stood at" / "of"
    m = _OB_PLAIN_RE.search(text)
    if m:
        v = _parse_ob_number(m.group(1))
        if v and 100 < v < 500000:
            return v

    return None


def _extract_order_book_from_pdf(pdf_bytes):
    """Extract order-book values from a single PDF.
    Returns list of dicts: {value_crore, type, context}.
    """
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages[:15]:
            text += (page.extract_text() or "") + "\n"
    except Exception:
        return []

    # Normalize line breaks that split words
    text = re.sub(r'(\w)\n(\w)', r'\1 \2', text)
    text = re.sub(r'\n+', '\n', text)

    results = []
    seen_vals = set()

    # Order Book / Order Backlog mentions
    for m in re.finditer(
        r'(?i)((?:order\s*book|order\s*backlog|unexecuted\s*order).{0,300})', text
    ):
        context = m.group(1)
        val = _extract_ob_value(context)
        if val and val not in seen_vals:
            seen_vals.add(val)
            results.append({
                "value_crore": val,
                "type": "book",
                "context": re.sub(r'\s+', ' ', context[:200]).strip(),
            })

    # Order Inflow mentions (separate metric)
    for m in re.finditer(r'(?i)(order\s*inflow.{0,300})', text):
        context = m.group(1)
        val = _extract_ob_value(context)
        if val and val not in seen_vals:
            seen_vals.add(val)
            results.append({
                "value_crore": val,
                "type": "inflow",
                "context": re.sub(r'\s+', ' ', context[:200]).strip(),
            })

    return results


def fetch_order_book_from_filings(symbol, max_pdfs=60):
    """Fetch order-book history from NSE filings (press releases, investor
    presentations, financial results, credit ratings, annual reports, board
    meeting outcomes).
    Returns list of dicts: {date, value_crore, type, context} sorted newest-first.
    """
    print("  Fetching order book data from NSE filings...")
    all_filings = []
    seen_urls = set()
    try:
        session = _nse_session()
        # Collect filing metadata from ALL categories (metadata is cheap)
        for subject in ("Press Release", "Investor Presentation",
                        "Outcome of Board Meeting", "Financial Results",
                        "Credit Rating", "Annual Report",
                        "Investor/Analyst Meet", "Updates"):
            cache_key = "ob_%s_%s" % (symbol, subject.replace(" ", "_"))
            filings = _nse_get_json(session,
                "https://www.nseindia.com/api/corporate-announcements",
                params={"index": "equities", "symbol": symbol, "subject": subject},
                cache_key=cache_key,
            )
            if not filings or not isinstance(filings, list):
                continue
            for item in filings[:10]:
                pdf_url = item.get("attchmntFile", "")
                if not pdf_url or pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                all_filings.append({
                    "date": item.get("an_dt", "")[:11],
                    "subject": item.get("desc", subject)[:60],
                    "pdf_url": pdf_url,
                })

        # Also scan annual reports from the dedicated endpoint
        ar_data = _nse_get_json(session,
            "https://www.nseindia.com/api/annual-reports",
            params={"index": "equities", "symbol": symbol},
            cache_key="ob_ar_%s" % symbol,
        )
        if ar_data and isinstance(ar_data, dict):
            for item in (ar_data.get("data") or [])[:3]:
                pdf_url = item.get("fileName") or ""
                if pdf_url and pdf_url not in seen_urls:
                    seen_urls.add(pdf_url)
                    year = item.get("fromYr") or item.get("toYr") or ""
                    all_filings.append({
                        "date": "01-Apr-%s" % year if year else "",
                        "subject": "Annual Report FY%s" % year,
                        "pdf_url": pdf_url,
                    })
    except Exception as e:
        print("    Order book fetch failed: %s" % e)
        return []

    if not all_filings:
        print("    No filings to scan for order book")
        return []

    # Sort newest-first before scanning (prioritise recent filings)
    def _filing_sort(f):
        try:
            return datetime.datetime.strptime(f["date"].strip(), "%d-%b-%Y")
        except Exception:
            return datetime.datetime(1970, 1, 1)
    all_filings.sort(key=_filing_sort, reverse=True)

    results = []
    seen_values = set()
    pdfs_scanned = 0
    for filing in all_filings:
        if pdfs_scanned >= max_pdfs:
            break
        try:
            pdf_bytes = _nse_download_pdf(session, filing["pdf_url"])
            if not pdf_bytes:
                continue
            pdfs_scanned += 1
            ob_entries = _extract_order_book_from_pdf(pdf_bytes)
            for entry in ob_entries:
                # De-duplicate: skip if we already have this exact value
                # (same filing may be scanned twice via different subjects)
                val_key = (round(entry["value_crore"], -1), entry["type"])
                if val_key in seen_values:
                    continue
                seen_values.add(val_key)
                entry["date"] = filing["date"]
                entry["source"] = filing["subject"]
                results.append(entry)
        except Exception:
            continue

    # Sort newest-first by date
    def _date_sort_key(d):
        try:
            return datetime.datetime.strptime(d["date"].strip(), "%d-%b-%Y")
        except Exception:
            return datetime.datetime(1970, 1, 1)
    results.sort(key=_date_sort_key, reverse=True)

    if results:
        book_entries = [r for r in results if r["type"] == "book"]
        inflow_entries = [r for r in results if r["type"] == "inflow"]
        print("    Order book: %d data points (%d book, %d inflow)" % (
            len(results), len(book_entries), len(inflow_entries)))
    else:
        print("    No order book data found in filings")
    return results


# ── Last-resort: parse a financial-results PDF for headline numbers ─────────

_FR_REVENUE_RE = re.compile(
    r'(?:total\s+(?:income|revenue)|revenue\s+from\s+operations|net\s+sales|total\s+revenue\s+from\s+operations)'
    r'[^\d\n\-]{0,40}([\-\(]?[\d,]+(?:\.\d+)?[\)]?)',
    re.IGNORECASE)
_FR_PAT_RE = re.compile(
    r'(?:profit\s+(?:after|for\s+the\s+period)|net\s+profit|profit\s*\(loss\)\s*for\s+the\s+period)'
    r'[^\d\n\-]{0,60}([\-\(]?[\d,]+(?:\.\d+)?[\)]?)',
    re.IGNORECASE)
_FR_EBITDA_RE = re.compile(
    r'(?:ebitda|operating\s+profit)[^\d\n\-]{0,40}([\-\(]?[\d,]+(?:\.\d+)?[\)]?)',
    re.IGNORECASE)


def _parse_pdf_money(s):
    """Parse a money-like number string (handles commas, parentheses for negatives)."""
    if not s:
        return float("nan")
    s = s.strip().replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()-")
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return float("nan")


def _augment_financials_from_filings(data):
    """If yfinance returned no annual data, try to extract revenue/PAT/EBITDA
    from the most recent financial-results PDF. Builds a 1-column synthetic
    income_annual DataFrame so downstream analyzers have something to chew on.
    Values are best-effort; the unit (lakhs vs crores) is heuristic.
    """
    print("\n  No yfinance financials available \u2014 attempting to parse latest result PDF...")
    try:
        session = _nse_session()
        for filing in data.financial_results_filings[:3]:
            pdf_url = filing.get("pdf_url")
            if not pdf_url:
                continue
            pdf_bytes = _nse_download_pdf(session, pdf_url)
            if not pdf_bytes:
                continue
            try:
                reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
                text = ""
                for page in reader.pages[:6]:
                    text += (page.extract_text() or "") + "\n"
            except Exception:
                continue
            rev_m = _FR_REVENUE_RE.search(text)
            pat_m = _FR_PAT_RE.search(text)
            if not rev_m and not pat_m:
                continue
            rev = _parse_pdf_money(rev_m.group(1)) if rev_m else float("nan")
            pat = _parse_pdf_money(pat_m.group(1)) if pat_m else float("nan")
            ebitda_m = _FR_EBITDA_RE.search(text)
            ebitda = _parse_pdf_money(ebitda_m.group(1)) if ebitda_m else float("nan")
            # Heuristic: PDF figures are usually in Rs. Lakhs or Rs. Crores.
            # Default assume Lakhs (10^5) -> convert to absolute INR (10^5).
            unit_mult = 1e5
            if "in crore" in text.lower() or "rs. in crore" in text.lower() or "(rs. crore" in text.lower():
                unit_mult = 1e7
            elif "in million" in text.lower():
                unit_mult = 1e6
            row_label = filing.get("period_hint") or filing.get("date") or "Latest"
            df = pd.DataFrame(
                {row_label: {
                    "Total Revenue": rev * unit_mult if not _nan(rev) else float("nan"),
                    "Net Income":   pat * unit_mult if not _nan(pat) else float("nan"),
                    "EBITDA":       ebitda * unit_mult if not _nan(ebitda) else float("nan"),
                }}
            )
            data.income_annual = df
            data.years = 1
            data.fy_labels = [row_label]
            print("    Parsed from filing %s: Revenue=%s | PAT=%s | EBITDA=%s (unit_mult=%g)" % (
                filing.get("date"),
                fmt_cr(rev * unit_mult) if not _nan(rev) else "N/A",
                fmt_cr(pat * unit_mult) if not _nan(pat) else "N/A",
                fmt_cr(ebitda * unit_mult) if not _nan(ebitda) else "N/A",
                unit_mult))
            return
        print("    Could not extract numbers from any result PDF.")
    except Exception as e:
        print("    Filing-PDF parse failed: %s" % e)


# ══════════════════════════════════════════════════════════════════════════════
# FORENSIC ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class ForensicAnalyzer:
    """Runs all forensic checks on the financial data."""

    def __init__(self, data):
        self.d = data
        self.inc = data.income_annual
        self.bal = data.balance_annual
        self.cf  = data.cashflow_annual
        self.inc_q = data.income_quarterly
        self.bal_q = data.balance_quarterly
        self.cf_q  = data.cashflow_quarterly
        self.info = data.info
        self.results = {}
        self.red_flags = []
        self.green_flags = []

    # ── shorthand getters ────────────────────────────────────────────────────
    def _i(self, key, col=0):
        return safe_get(self.inc, key, col)

    def _b(self, key, col=0):
        return safe_get(self.bal, key, col)

    def _c(self, key, col=0):
        return safe_get(self.cf, key, col)

    # ─────────────────────────────────────────────────────────────────────────
    # 1. BENEISH M-SCORE  (Earnings manipulation detector)
    # ─────────────────────────────────────────────────────────────────────────
    def beneish_m_score(self):
        """Compute 8-variable Beneish M-Score.
        M > -1.78 => likely earnings manipulation.
        """
        # Current year = col 0, previous year = col 1
        rev_t  = self._i("revenue", 0);  rev_t1  = self._i("revenue", 1)
        recv_t = self._b("receivables", 0); recv_t1 = self._b("receivables", 1)
        gp_t   = self._i("gross_profit", 0); gp_t1 = self._i("gross_profit", 1)
        ca_t   = self._b("current_assets", 0); ca_t1 = self._b("current_assets", 1)
        ppe_t  = self._b("ppe_net", 0); ppe_t1 = self._b("ppe_net", 1)
        ta_t   = self._b("total_assets", 0); ta_t1 = self._b("total_assets", 1)
        dep_t  = self._i("depreciation", 0); dep_t1 = self._i("depreciation", 1)
        sga_t  = self._i("sga", 0); sga_t1 = self._i("sga", 1)
        ni_t   = self._i("net_income", 0)
        cfo_t  = self._c("operating_cf", 0)
        debt_t = self._b("total_debt", 0); debt_t1 = self._b("total_debt", 1)

        # 1. DSRI — Days Sales in Receivables Index
        dsri = safe_div(safe_div(recv_t, rev_t), safe_div(recv_t1, rev_t1))
        # 2. GMI — Gross Margin Index
        gm_t = safe_div(gp_t, rev_t); gm_t1 = safe_div(gp_t1, rev_t1)
        gmi = safe_div(gm_t1, gm_t)
        # 3. AQI — Asset Quality Index
        aq_t = 1 - safe_div(ca_t + ppe_t, ta_t, 1)
        aq_t1 = 1 - safe_div(ca_t1 + ppe_t1, ta_t1, 1)
        aqi = safe_div(aq_t, aq_t1)
        # 4. SGI — Sales Growth Index
        sgi = safe_div(rev_t, rev_t1)
        # 5. DEPI — Depreciation Index
        dep_rate_t  = safe_div(dep_t, dep_t + ppe_t)
        dep_rate_t1 = safe_div(dep_t1, dep_t1 + ppe_t1)
        depi = safe_div(dep_rate_t1, dep_rate_t)
        # 6. SGAI — SGA Expense Index
        sgai_t = safe_div(sga_t, rev_t); sgai_t1 = safe_div(sga_t1, rev_t1)
        sgai = safe_div(sgai_t, sgai_t1)
        # 7. TATA — Total Accruals to Total Assets
        tata = safe_div(ni_t - cfo_t, ta_t) if not (_nan(ni_t) or _nan(cfo_t)) else float("nan")
        # 8. LVGI — Leverage Index
        lev_t = safe_div(debt_t, ta_t); lev_t1 = safe_div(debt_t1, ta_t1)
        lvgi = safe_div(lev_t, lev_t1)

        components = {
            "DSRI": dsri, "GMI": gmi, "AQI": aqi, "SGI": sgi,
            "DEPI": depi, "SGAI": sgai, "TATA": tata, "LVGI": lvgi,
        }

        # Any NaN component → use default value of 1.0 (neutral)
        for k, v in components.items():
            if _nan(v):
                components[k] = 1.0 if k != "TATA" else 0.0

        m = (-4.84
             + 0.920 * components["DSRI"]
             + 0.528 * components["GMI"]
             + 0.404 * components["AQI"]
             + 0.892 * components["SGI"]
             + 0.115 * components["DEPI"]
             - 0.172 * components["SGAI"]
             + 4.679 * components["TATA"]
             - 0.327 * components["LVGI"])

        is_manipulator = m > -1.78
        verdict = "LIKELY MANIPULATOR" if is_manipulator else "Non-Manipulator"
        # Score 0-10 (higher = better)
        if m < -2.50:
            score = 10
        elif m < -1.78:
            score = 7
        elif m < -1.0:
            score = 4
        else:
            score = 1

        result = {"m_score": m, "components": components, "verdict": verdict,
                  "is_red": is_manipulator, "score_10": score}

        if is_manipulator:
            self.red_flags.append(("Beneish M-Score = %.2f  (> -1.78: likely earnings manipulation)" % m, "critical"))
        else:
            self.green_flags.append("Beneish M-Score = %.2f  (< -1.78: no manipulation detected)" % m)

        print("  Beneish M-Score : %.2f  => %s" % (m, verdict))
        self.results["beneish"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 2. ALTMAN Z-SCORE  (Bankruptcy predictor)
    # ─────────────────────────────────────────────────────────────────────────
    def altman_z_score(self):
        """Altman Z-Score for listed companies.
        Z > 2.99 => Safe zone;  1.81-2.99 => Grey;  < 1.81 => Distress.
        """
        wc  = self._b("working_capital", 0)
        ta  = self._b("total_assets", 0)
        re  = self._b("retained_earnings", 0)
        ebit = self._i("ebit", 0)
        tl  = self._b("total_liabilities", 0)
        rev = self._i("revenue", 0)
        mkt_cap = float(self.info.get("marketCap", 0) or 0)

        x1 = safe_div(wc, ta)
        x2 = safe_div(re, ta)
        x3 = safe_div(ebit, ta)
        x4 = safe_div(mkt_cap, tl)
        x5 = safe_div(rev, ta)

        # Replace NaN with 0 for summation
        comps = {"X1_WC_TA": x1, "X2_RE_TA": x2, "X3_EBIT_TA": x3,
                 "X4_MktCap_TL": x4, "X5_Rev_TA": x5}
        for k in comps:
            if _nan(comps[k]):
                comps[k] = 0.0

        z = (1.2 * comps["X1_WC_TA"]
             + 1.4 * comps["X2_RE_TA"]
             + 3.3 * comps["X3_EBIT_TA"]
             + 0.6 * comps["X4_MktCap_TL"]
             + 1.0 * comps["X5_Rev_TA"])

        if z > 2.99:
            zone = "SAFE ZONE"
            score = 10
        elif z > 1.81:
            zone = "GREY ZONE"
            score = 5
        else:
            zone = "DISTRESS ZONE"
            score = 1

        result = {"z_score": z, "components": comps, "zone": zone,
                  "score_10": score}

        if z < 1.81:
            self.red_flags.append(("Altman Z-Score = %.2f  (< 1.81: distress zone, bankruptcy risk)" % z, "critical"))
        elif z < 2.99:
            self.red_flags.append(("Altman Z-Score = %.2f  (grey zone: some financial stress)" % z, "minor"))
        else:
            self.green_flags.append("Altman Z-Score = %.2f  (> 2.99: safe zone, low bankruptcy risk)" % z)

        print("  Altman Z-Score  : %.2f  => %s" % (z, zone))
        self.results["altman"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 3. PIOTROSKI F-SCORE  (Financial strength, 0-9)
    # ─────────────────────────────────────────────────────────────────────────
    def piotroski_f_score(self):
        """Piotroski F-Score: 0-9 (higher = stronger fundamentals).
        8-9 Strong, 5-7 Moderate, 0-4 Weak.
        """
        ta_t  = self._b("total_assets", 0); ta_t1 = self._b("total_assets", 1)
        ni_t  = self._i("net_income", 0);   ni_t1 = self._i("net_income", 1)
        cfo_t = self._c("operating_cf", 0)
        rev_t = self._i("revenue", 0); rev_t1 = self._i("revenue", 1)
        gp_t  = self._i("gross_profit", 0); gp_t1 = self._i("gross_profit", 1)
        ltd_t = self._b("long_term_debt", 0); ltd_t1 = self._b("long_term_debt", 1)
        ca_t  = self._b("current_assets", 0); ca_t1 = self._b("current_assets", 1)
        cl_t  = self._b("current_liabilities", 0); cl_t1 = self._b("current_liabilities", 1)
        shares_t = self._b("shares_outstanding", 0)
        shares_t1 = self._b("shares_outstanding", 1)

        roa_t  = safe_div(ni_t, ta_t)
        roa_t1 = safe_div(ni_t1, ta_t1)

        details = {}
        f = 0
        # Profitability
        # 1. ROA > 0
        p1 = 1 if (not _nan(roa_t) and roa_t > 0) else 0
        details["ROA > 0"] = p1; f += p1
        # 2. CFO > 0
        p2 = 1 if (not _nan(cfo_t) and cfo_t > 0) else 0
        details["CFO > 0"] = p2; f += p2
        # 3. Delta ROA > 0
        p3 = 1 if (not _nan(roa_t) and not _nan(roa_t1) and roa_t > roa_t1) else 0
        details["Delta ROA > 0"] = p3; f += p3
        # 4. CFO > Net Income (accrual quality)
        p4 = 1 if (not _nan(cfo_t) and not _nan(ni_t) and cfo_t > ni_t) else 0
        details["CFO > NI (accrual)"] = p4; f += p4

        # Leverage / Liquidity
        # 5. Long-term debt decreased
        p5 = 0
        if not _nan(ltd_t) and not _nan(ltd_t1):
            p5 = 1 if ltd_t <= ltd_t1 else 0
        details["LT Debt decreased"] = p5; f += p5
        # 6. Current ratio improved
        cr_t = safe_div(ca_t, cl_t); cr_t1 = safe_div(ca_t1, cl_t1)
        p6 = 1 if (not _nan(cr_t) and not _nan(cr_t1) and cr_t > cr_t1) else 0
        details["Current Ratio improved"] = p6; f += p6
        # 7. No new shares issued (dilution)
        p7 = 1
        if not _nan(shares_t) and not _nan(shares_t1) and shares_t > shares_t1 * 1.01:
            p7 = 0
        details["No dilution"] = p7; f += p7

        # Operating Efficiency
        # 8. Gross margin improved
        gm_t = safe_div(gp_t, rev_t); gm_t1 = safe_div(gp_t1, rev_t1)
        p8 = 1 if (not _nan(gm_t) and not _nan(gm_t1) and gm_t > gm_t1) else 0
        details["Gross Margin improved"] = p8; f += p8
        # 9. Asset turnover improved
        at_t = safe_div(rev_t, ta_t); at_t1 = safe_div(rev_t1, ta_t1)
        p9 = 1 if (not _nan(at_t) and not _nan(at_t1) and at_t > at_t1) else 0
        details["Asset Turnover improved"] = p9; f += p9

        if f >= 8:
            verdict = "STRONG"
            score = 10
        elif f >= 5:
            verdict = "MODERATE"
            score = 6
        else:
            verdict = "WEAK"
            score = 2

        result = {"f_score": f, "details": details, "verdict": verdict,
                  "score_10": score}

        if f <= 3:
            self.red_flags.append(("Piotroski F-Score = %d/9  (very weak fundamentals)" % f, "major"))
        elif f >= 7:
            self.green_flags.append("Piotroski F-Score = %d/9  (strong fundamentals)" % f)

        print("  Piotroski F-Score: %d/9   => %s" % (f, verdict))
        self.results["piotroski"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 4. DUPONT ANALYSIS (ROE decomposition)
    # ─────────────────────────────────────────────────────────────────────────
    def dupont_analysis(self):
        """Decompose ROE = Net Margin x Asset Turnover x Equity Multiplier."""
        rows = []
        for col in range(min(self.d.years, 4)):
            rev  = self._i("revenue", col)
            ni   = self._i("net_income", col)
            ta   = self._b("total_assets", col)
            eq   = self._b("equity", col)
            label = self.d.fy_labels[col] if col < len(self.d.fy_labels) else "Y%d" % col

            net_margin   = safe_div(ni, rev) * 100
            asset_turn   = safe_div(rev, ta)
            eq_mult      = safe_div(ta, eq)
            roe          = safe_div(ni, eq) * 100

            rows.append({
                "year": label, "net_margin": net_margin,
                "asset_turnover": asset_turn, "equity_multiplier": eq_mult,
                "roe": roe,
            })

        self.results["dupont"] = rows
        if rows:
            r = rows[0]
            print("  DuPont ROE (latest): %.1f%%  = %.1f%% margin x %.2f turn x %.2f leverage" % (
                r["roe"], r["net_margin"], r["asset_turnover"], r["equity_multiplier"]))
        return rows

    # ─────────────────────────────────────────────────────────────────────────
    # 5. PROFITABILITY ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────
    def profitability_analysis(self):
        """Multi-year profitability trends."""
        rows = []
        for col in range(min(self.d.years, 4)):
            rev  = self._i("revenue", col)
            gp   = self._i("gross_profit", col)
            oi   = self._i("operating_inc", col)
            ni   = self._i("net_income", col)
            ebitda = self._i("ebitda", col)
            label = self.d.fy_labels[col] if col < len(self.d.fy_labels) else "Y%d" % col

            rows.append({
                "year": label,
                "revenue": rev, "gross_profit": gp,
                "operating_income": oi, "net_income": ni, "ebitda": ebitda,
                "gross_margin": safe_div(gp, rev) * 100,
                "operating_margin": safe_div(oi, rev) * 100,
                "net_margin": safe_div(ni, rev) * 100,
                "ebitda_margin": safe_div(ebitda, rev) * 100,
            })

        # Trend checks (newest = idx 0, oldest = last)
        if len(rows) >= 3:
            gm_latest = rows[0]["gross_margin"]
            gm_oldest = rows[-1]["gross_margin"]
            if not _nan(gm_latest) and not _nan(gm_oldest):
                if gm_latest < gm_oldest - 5:
                    self.red_flags.append((
                        "Gross margin declined from %.1f%% to %.1f%% over %d years" % (
                            gm_oldest, gm_latest, len(rows)), "major"))
                elif gm_latest > gm_oldest + 2:
                    self.green_flags.append(
                        "Gross margin expanded from %.1f%% to %.1f%%" % (gm_oldest, gm_latest))

            om_latest = rows[0]["operating_margin"]
            om_oldest = rows[-1]["operating_margin"]
            if not _nan(om_latest) and not _nan(om_oldest):
                if om_latest < om_oldest - 3:
                    self.red_flags.append((
                        "Operating margin declined from %.1f%% to %.1f%%" % (
                            om_oldest, om_latest), "minor"))

        self.results["profitability"] = rows
        return rows

    # ─────────────────────────────────────────────────────────────────────────
    # 6. CASH FLOW QUALITY
    # ─────────────────────────────────────────────────────────────────────────
    def cash_flow_analysis(self):
        """Analyse operating cash flow quality and free cash flow trends."""
        rows = []
        neg_cfo_years = 0
        neg_fcf_years = 0
        cfo_lt_ni_years = 0
        for col in range(min(self.d.years, 4)):
            cfo  = self._c("operating_cf", col)
            capex = self._c("capex", col)
            fcf  = self._c("fcf", col)
            ni   = self._i("net_income", col)
            rev  = self._i("revenue", col)
            dep  = self._c("dep_cf", col)
            label = self.d.fy_labels[col] if col < len(self.d.fy_labels) else "Y%d" % col

            # Accrual ratio = (NI - CFO) / TA
            ta = self._b("total_assets", col)
            accrual = safe_div(ni - cfo, ta) if not (_nan(ni) or _nan(cfo)) else float("nan")
            cfo_ni = safe_div(cfo, ni) if not _nan(ni) else float("nan")
            cfo_rev = safe_div(cfo, rev) * 100 if not _nan(rev) else float("nan")

            if not _nan(cfo) and cfo < 0:
                neg_cfo_years += 1
            if not _nan(fcf) and fcf < 0:
                neg_fcf_years += 1
            if not _nan(cfo) and not _nan(ni) and ni > 0 and cfo < ni:
                cfo_lt_ni_years += 1

            rows.append({
                "year": label, "cfo": cfo, "capex": capex, "fcf": fcf,
                "net_income": ni, "depreciation": dep,
                "cfo_to_ni": cfo_ni, "cfo_to_rev": cfo_rev,
                "accrual_ratio": accrual,
            })

        # Flags
        if neg_cfo_years >= 2:
            self.red_flags.append(("Negative operating cash flow in %d of last %d years" % (
                neg_cfo_years, len(rows)), "critical"))
        elif neg_cfo_years == 0 and rows:
            self.green_flags.append("Positive operating cash flow every year")

        if neg_fcf_years >= 3 and len(rows) >= 3:
            self.red_flags.append(("Negative free cash flow in %d of last %d years" % (
                neg_fcf_years, len(rows)), "major"))
        elif neg_fcf_years == 0 and rows:
            self.green_flags.append("Positive free cash flow every year")

        if cfo_lt_ni_years >= 2:
            self.red_flags.append(("CFO < Net Income in %d years (earnings may be accrual-inflated)" % cfo_lt_ni_years, "major"))
        elif cfo_lt_ni_years == 0 and rows:
            self.green_flags.append("CFO exceeds Net Income every year (high earnings quality)")

        # Sloan Accrual Ratio for latest year
        if rows and not _nan(rows[0]["accrual_ratio"]):
            ar = rows[0]["accrual_ratio"]
            if abs(ar) > 0.10:
                self.red_flags.append(("High Sloan Accrual Ratio = %.2f (earnings quality concern)" % ar, "minor"))

        # Score
        score = 7  # default moderate
        if neg_cfo_years >= 2:
            score = 2
        elif cfo_lt_ni_years >= 2:
            score = 4
        elif neg_cfo_years == 0 and cfo_lt_ni_years == 0:
            score = 9

        self.results["cashflow"] = {"rows": rows, "score_10": score}
        return rows

    # ─────────────────────────────────────────────────────────────────────────
    # 7. WORKING CAPITAL & EFFICIENCY
    # ─────────────────────────────────────────────────────────────────────────
    def working_capital_analysis(self):
        """Analyse receivable days, inventory days, payable days, CCC."""
        rows = []
        for col in range(min(self.d.years, 4)):
            rev    = self._i("revenue", col)
            cogs   = self._i("cogs", col)
            recv   = self._b("receivables", col)
            inv    = self._b("inventory", col)
            pay    = self._b("payables", col)
            ca     = self._b("current_assets", col)
            cl     = self._b("current_liabilities", col)
            label  = self.d.fy_labels[col] if col < len(self.d.fy_labels) else "Y%d" % col

            dso = safe_div(recv, rev) * 365          # receivable days
            dio = safe_div(inv, cogs) * 365           # inventory days
            dpo = safe_div(pay, cogs) * 365           # payable days
            ccc = float("nan")
            if not (_nan(dso) or _nan(dio) or _nan(dpo)):
                ccc = dso + dio - dpo
            cr  = safe_div(ca, cl)

            rows.append({
                "year": label, "dso": dso, "dio": dio, "dpo": dpo,
                "ccc": ccc, "current_ratio": cr,
                "receivables": recv, "inventory": inv, "payables": pay,
            })

        # Trend checks
        if len(rows) >= 2:
            dso_now = rows[0]["dso"]; dso_prev = rows[1]["dso"]
            if not _nan(dso_now) and not _nan(dso_prev):
                chg = pct_change_val(dso_now, dso_prev)
                if chg > 30:
                    self.red_flags.append(("Receivable days surged %.0f%% YoY (%.0f -> %.0f days)" % (
                        chg, dso_prev, dso_now), "major"))

            dio_now = rows[0]["dio"]; dio_prev = rows[1]["dio"]
            if not _nan(dio_now) and not _nan(dio_prev):
                chg = pct_change_val(dio_now, dio_prev)
                if chg > 30:
                    self.red_flags.append(("Inventory days surged %.0f%% YoY (%.0f -> %.0f days)" % (
                        chg, dio_prev, dio_now), "major"))

            # Revenue vs Receivables divergence
            rev_now = self._i("revenue", 0); rev_prev = self._i("revenue", 1)
            recv_now = self._b("receivables", 0); recv_prev = self._b("receivables", 1)
            if not any(_nan(v) for v in [rev_now, rev_prev, recv_now, recv_prev]) and rev_prev > 0 and recv_prev > 0:
                rev_growth = (rev_now / rev_prev - 1) * 100
                recv_growth = (recv_now / recv_prev - 1) * 100
                if recv_growth > rev_growth + 20:
                    self.red_flags.append((
                        "Receivables growing (+%.0f%%) much faster than revenue (+%.0f%%) — possible channel stuffing" % (
                            recv_growth, rev_growth), "major"))

        # Score
        score = 6
        if rows:
            cr = rows[0]["current_ratio"]
            if not _nan(cr):
                if cr > 1.5:
                    score = 8
                elif cr < 1.0:
                    score = 3
                    self.red_flags.append(("Current ratio = %.2f (< 1.0: liquidity risk)" % cr, "major"))

        self.results["working_capital"] = {"rows": rows, "score_10": score}
        return rows

    # ─────────────────────────────────────────────────────────────────────────
    # 8. DEBT & LEVERAGE ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────
    def debt_analysis(self):
        """Analyse debt levels, leverage ratios, and interest coverage."""
        rows = []
        for col in range(min(self.d.years, 4)):
            td   = self._b("total_debt", col)
            eq   = self._b("equity", col)
            ta   = self._b("total_assets", col)
            ebit = self._i("ebit", col)
            ie   = self._i("interest_exp", col)
            cash = self._b("cash", col)
            label = self.d.fy_labels[col] if col < len(self.d.fy_labels) else "Y%d" % col

            de    = safe_div(td, eq)
            da    = safe_div(td, ta) * 100
            ic    = safe_div(ebit, ie) if not _nan(ie) and ie > 0 else float("nan")
            net_d = (td - cash) if not (_nan(td) or _nan(cash)) else float("nan")
            nd_eq = safe_div(net_d, eq)

            rows.append({
                "year": label, "total_debt": td, "equity": eq,
                "de_ratio": de, "da_ratio": da,
                "interest_coverage": ic, "net_debt": net_d,
                "net_de_ratio": nd_eq, "cash": cash,
            })

        # Flags
        if rows:
            de = rows[0]["de_ratio"]
            ic = rows[0]["interest_coverage"]
            if not _nan(de) and de > 2.0:
                self.red_flags.append(("Debt-to-Equity = %.2f (high leverage)" % de,
                                       "critical" if de > 3.0 else "major"))
            elif not _nan(de) and de < 0.5:
                self.green_flags.append("Low Debt-to-Equity = %.2f (conservatively financed)" % de)

            if not _nan(ic):
                if ic < 1.5:
                    self.red_flags.append(("Interest coverage = %.1fx (dangerously low)" % ic, "critical"))
                elif ic > 5:
                    self.green_flags.append("Strong interest coverage = %.1fx" % ic)

        # Trend
        if len(rows) >= 2:
            de_now = rows[0]["de_ratio"]; de_prev = rows[1]["de_ratio"]
            if not _nan(de_now) and not _nan(de_prev) and de_now > de_prev * 1.3:
                self.red_flags.append(("Debt-to-Equity increased significantly (%.2f -> %.2f)" % (
                    de_prev, de_now), "minor"))
            elif not _nan(de_now) and not _nan(de_prev) and de_now < de_prev * 0.8:
                self.green_flags.append("Debt-to-Equity improved (%.2f -> %.2f, deleveraging)" % (
                    de_prev, de_now))

        score = 6
        if rows:
            de = rows[0]["de_ratio"]
            ic = rows[0]["interest_coverage"]
            if not _nan(de):
                if de > 3.0:
                    score = 2
                elif de > 2.0:
                    score = 4
                elif de < 0.5:
                    score = 9
            if not _nan(ic) and ic < 1.5:
                score = min(score, 2)

        self.results["debt"] = {"rows": rows, "score_10": score}
        return rows

    # ─────────────────────────────────────────────────────────────────────────
    # 9. KEY RATIOS & ADDITIONAL CHECKS
    # ─────────────────────────────────────────────────────────────────────────
    def additional_checks(self):
        """ROE, ROCE, ROA, EPS trends, tax consistency, capex-to-dep."""
        rows = []
        for col in range(min(self.d.years, 4)):
            ni   = self._i("net_income", col)
            eq   = self._b("equity", col)
            ta   = self._b("total_assets", col)
            ebit = self._i("ebit", col)
            td   = self._b("total_debt", col)
            dep  = self._i("depreciation", col)
            capex = self._c("capex", col)
            tax  = self._i("tax", col)
            pbt  = self._i("pretax_income", col)
            eps  = self._i("diluted_eps", col)
            label = self.d.fy_labels[col] if col < len(self.d.fy_labels) else "Y%d" % col

            roe  = safe_div(ni, eq) * 100
            roa  = safe_div(ni, ta) * 100
            ce   = (eq + td) if not (_nan(eq) or _nan(td)) else float("nan")
            roce = safe_div(ebit, ce) * 100
            tax_rate = safe_div(tax, pbt) * 100
            capex_dep = safe_div(abs(capex) if not _nan(capex) else float("nan"), dep)

            rows.append({
                "year": label, "roe": roe, "roa": roa, "roce": roce,
                "eps": eps, "tax_rate": tax_rate, "capex_to_dep": capex_dep,
            })

        # Check ROE trend
        if len(rows) >= 2:
            roe_now = rows[0]["roe"]; roe_prev = rows[-1]["roe"]
            if not _nan(roe_now) and not _nan(roe_prev):
                if roe_now > 15:
                    self.green_flags.append("Strong ROE = %.1f%%" % roe_now)
                if roe_now < 8 and roe_prev > 12:
                    self.red_flags.append(("ROE declined significantly from %.1f%% to %.1f%%" % (
                        roe_prev, roe_now), "major"))

        # Tax rate consistency
        tax_rates = [r["tax_rate"] for r in rows if not _nan(r["tax_rate"])]
        if len(tax_rates) >= 3:
            avg_tax = sum(tax_rates) / len(tax_rates)
            for i, tr in enumerate(tax_rates):
                if abs(tr - avg_tax) > 10:
                    yr = rows[i]["year"]
                    self.red_flags.append((
                        "Unusual tax rate in %s: %.1f%% (avg: %.1f%%) — investigate" % (
                            yr, tr, avg_tax), "minor"))
                    break

        self.results["ratios"] = rows
        return rows

    # ─────────────────────────────────────────────────────────────────────────
    # 10. QUARTERLY TREND CHECK
    # ─────────────────────────────────────────────────────────────────────────
    def quarterly_trends(self):
        """Analyse quarterly revenue and profit trends for recent anomalies."""
        rows = []
        if self.inc_q is None or self.inc_q.empty:
            self.results["quarterly"] = rows
            return rows

        for col in range(min(self.d.quarters, 8)):
            rev = safe_get(self.inc_q, "revenue", col)
            ni  = safe_get(self.inc_q, "net_income", col)
            gp  = safe_get(self.inc_q, "gross_profit", col)
            ebitda = safe_get(self.inc_q, "ebitda", col)
            label = self.d.q_labels[col] if col < len(self.d.q_labels) else "Q%d" % col

            rows.append({
                "quarter": label, "revenue": rev, "net_income": ni,
                "gross_profit": gp, "ebitda": ebitda,
                "net_margin": safe_div(ni, rev) * 100,
            })

        # Check for sudden drops
        if len(rows) >= 2:
            rev_q0 = rows[0]["revenue"]; rev_q1 = rows[1]["revenue"]
            ni_q0 = rows[0]["net_income"]; ni_q1 = rows[1]["net_income"]
            if not _nan(rev_q0) and not _nan(rev_q1) and rev_q1 > 0:
                rev_chg = (rev_q0 / rev_q1 - 1) * 100
                if rev_chg < -20:
                    self.red_flags.append((
                        "Latest quarter revenue dropped %.0f%% QoQ" % rev_chg, "minor"))
            if not _nan(ni_q0) and not _nan(ni_q1) and ni_q1 > 0:
                ni_chg = (ni_q0 / ni_q1 - 1) * 100
                if ni_chg < -30:
                    self.red_flags.append((
                        "Latest quarter profit dropped %.0f%% QoQ" % ni_chg, "minor"))

        self.results["quarterly"] = rows
        return rows

    # ─────────────────────────────────────────────────────────────────────────
    # 11. GROWTH ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────
    def growth_analysis(self):
        """Compute CAGR and YoY growth for key metrics."""
        result = {}
        n = min(self.d.years, 4)
        if n < 2:
            self.results["growth"] = result
            return result

        # Revenue CAGR
        rev_latest = self._i("revenue", 0)
        rev_oldest = self._i("revenue", n - 1)
        if not _nan(rev_latest) and not _nan(rev_oldest) and rev_oldest > 0:
            years = n - 1
            ratio = rev_latest / rev_oldest
            if ratio > 0:
                cagr = (pow(ratio, 1.0 / years) - 1) * 100
            else:
                cagr = -100.0  # revenue went negative
            result["revenue_cagr"] = cagr
            if cagr > 15:
                self.green_flags.append("Strong revenue CAGR of %.1f%% over %d years" % (cagr, years))
            elif cagr < 0:
                self.red_flags.append(("Revenue declining at %.1f%% CAGR" % cagr, "major"))

        # Profit CAGR
        ni_latest = self._i("net_income", 0)
        ni_oldest = self._i("net_income", n - 1)
        if not _nan(ni_latest) and not _nan(ni_oldest) and ni_oldest > 0:
            years = n - 1
            ratio = ni_latest / ni_oldest
            if ratio > 0:
                cagr = (pow(ratio, 1.0 / years) - 1) * 100
            else:
                cagr = -100.0  # profit went negative
            result["profit_cagr"] = cagr
            if cagr > 20:
                self.green_flags.append("Strong profit CAGR of %.1f%% over %d years" % (cagr, years))
            elif cagr < -10:
                self.red_flags.append(("Profit declining at %.1f%% CAGR" % cagr, "major"))

        # YoY revenue growth
        yoy = []
        for col in range(n - 1):
            r0 = self._i("revenue", col)
            r1 = self._i("revenue", col + 1)
            if not _nan(r0) and not _nan(r1) and r1 > 0:
                yoy.append((r0 / r1 - 1) * 100)
        result["revenue_yoy"] = yoy

        # EPS growth
        eps_latest = self._i("diluted_eps", 0)
        eps_prev = self._i("diluted_eps", 1)
        if not _nan(eps_latest) and not _nan(eps_prev) and eps_prev > 0:
            result["eps_growth"] = (eps_latest / eps_prev - 1) * 100

        self.results["growth"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 12. ENHANCED FORENSIC CHECKS
    # ─────────────────────────────────────────────────────────────────────────
    def enhanced_forensic_checks(self):
        """Additional forensic checks: negative equity, consecutive losses,
        goodwill ratio, dividend sustainability, capex adequacy, cash
        conversion efficiency, ROIC, quick ratio."""
        result = {}
        n = min(self.d.years, 4)

        # --- Negative Equity Detection ---
        eq_latest = self._b("equity", 0)
        if not _nan(eq_latest) and eq_latest < 0:
            self.red_flags.append((
                "NEGATIVE EQUITY: Stockholders equity = %s (company technically insolvent)" % fmt_cr(eq_latest),
                "critical"))
            result["negative_equity"] = True
        else:
            result["negative_equity"] = False

        # --- Consecutive Loss Years ---
        loss_years = 0
        for col in range(n):
            ni = self._i("net_income", col)
            if not _nan(ni) and ni < 0:
                loss_years += 1
        result["loss_years"] = loss_years
        if loss_years >= 3:
            self.red_flags.append((
                "Net loss in %d of last %d years (persistent losses)" % (loss_years, n),
                "critical"))
        elif loss_years >= 2:
            self.red_flags.append((
                "Net loss in %d of last %d years" % (loss_years, n), "major"))
        elif loss_years == 0:
            self.green_flags.append("Profitable every year (no loss years)")

        # --- Goodwill & Intangibles to Total Assets ---
        gw = self._b("goodwill", 0)
        intang = self._b("intangibles", 0)
        ta = self._b("total_assets", 0)
        gw_val = gw if not _nan(gw) else 0
        int_val = intang if not _nan(intang) else 0
        intangible_total = gw_val + int_val
        intangible_ratio = safe_div(intangible_total, ta) * 100
        result["intangible_ratio"] = intangible_ratio
        if not _nan(intangible_ratio):
            if intangible_ratio > 50:
                self.red_flags.append((
                    "Goodwill + Intangibles = %.1f%% of total assets (very high - acquisition-heavy)" % intangible_ratio,
                    "critical"))
            elif intangible_ratio > 30:
                self.red_flags.append((
                    "Goodwill + Intangibles = %.1f%% of total assets (high impairment risk)" % intangible_ratio,
                    "major"))
            elif intangible_ratio < 5:
                self.green_flags.append(
                    "Low intangible assets (%.1f%% of total assets)" % intangible_ratio)

        # --- Dividend Sustainability ---
        div_paid = self._c("dividends_paid", 0)
        fcf = self._c("fcf", 0)
        ni = self._i("net_income", 0)
        if not _nan(div_paid) and div_paid < 0:  # dividends_paid is negative in CF
            abs_div = abs(div_paid)
            div_payout_ni = safe_div(abs_div, ni) * 100 if not _nan(ni) and ni > 0 else float("nan")
            div_payout_fcf = safe_div(abs_div, fcf) * 100 if not _nan(fcf) and fcf > 0 else float("nan")
            result["dividend_payout_ni"] = div_payout_ni
            result["dividend_payout_fcf"] = div_payout_fcf
            if not _nan(div_payout_fcf) and div_payout_fcf > 100:
                self.red_flags.append((
                    "Dividends (%.0f%% of FCF) exceed free cash flow - unsustainable" % div_payout_fcf,
                    "major"))
            elif not _nan(div_payout_ni) and div_payout_ni > 90:
                self.red_flags.append((
                    "Dividend payout ratio = %.0f%% of net income (very high)" % div_payout_ni,
                    "minor"))
            elif not _nan(div_payout_fcf) and div_payout_fcf < 60:
                self.green_flags.append(
                    "Sustainable dividend payout (%.0f%% of FCF)" % div_payout_fcf)
        else:
            result["dividend_payout_ni"] = float("nan")
            result["dividend_payout_fcf"] = float("nan")

        # --- Capex Adequacy ---
        capex_vals = []
        dep_vals = []
        for col in range(n):
            cx = self._c("capex", col)
            dp = self._i("depreciation", col)
            if not _nan(cx) and not _nan(dp):
                capex_vals.append(abs(cx))
                dep_vals.append(dp)
        if len(capex_vals) >= 2:
            under_investing = sum(1 for c, d in zip(capex_vals, dep_vals) if d > 0 and c < d * 0.8)
            result["capex_below_dep_years"] = under_investing
            if under_investing >= 2:
                self.red_flags.append((
                    "Capex < 80%% of depreciation in %d years (under-investing in assets)" % under_investing,
                    "major"))
            elif under_investing == 0:
                avg_ratio = safe_div(sum(capex_vals), sum(dep_vals))
                if not _nan(avg_ratio) and avg_ratio > 1.5:
                    self.green_flags.append(
                        "Strong capex (%.1fx depreciation avg) - investing in growth" % avg_ratio)

        # --- Cash Conversion Efficiency (FCF / Net Income) ---
        ni_latest = self._i("net_income", 0)
        if not _nan(fcf) and not _nan(ni_latest) and ni_latest > 0:
            cce = safe_div(fcf, ni_latest) * 100
            result["cash_conversion"] = cce
            if not _nan(cce):
                if cce > 80:
                    self.green_flags.append(
                        "Excellent cash conversion: %.0f%% of net income converts to free cash" % cce)
                elif cce < 30 and cce > 0:
                    self.red_flags.append((
                        "Poor cash conversion: only %.0f%% of net income converts to free cash" % cce,
                        "minor"))
        else:
            result["cash_conversion"] = float("nan")

        # --- ROIC (Return on Invested Capital) ---
        ebit = self._i("ebit", 0)
        tax_rate = 0.25  # approximate Indian corporate tax rate
        ratios_data = self.results.get("ratios", [])
        if ratios_data and not _nan(ratios_data[0].get("tax_rate", float("nan"))):
            tax_rate = ratios_data[0]["tax_rate"] / 100.0
        nopat = ebit * (1 - tax_rate) if not _nan(ebit) else float("nan")
        ic = self._b("invested_capital", 0)
        if _nan(ic):
            td = self._b("total_debt", 0)
            eq_fb = self._b("equity", 0)
            if not _nan(td) and not _nan(eq_fb):
                ic = td + eq_fb
        roic = safe_div(nopat, ic) * 100
        result["roic"] = roic
        if not _nan(roic):
            if roic > 15:
                self.green_flags.append("Strong ROIC = %.1f%% (excellent capital allocation)" % roic)
            elif roic < 5:
                self.red_flags.append(("Low ROIC = %.1f%% (poor capital allocation)" % roic, "minor"))

        # --- Quick Ratio ---
        ca = self._b("current_assets", 0)
        inv = self._b("inventory", 0)
        cl = self._b("current_liabilities", 0)
        inv_val = inv if not _nan(inv) else 0
        quick = safe_div(ca - inv_val, cl) if not _nan(ca) else float("nan")
        result["quick_ratio"] = quick
        if not _nan(quick):
            if quick < 0.8:
                self.red_flags.append((
                    "Quick ratio = %.2f (< 0.8: may struggle to meet short-term obligations)" % quick,
                    "minor"))
            elif quick > 1.5:
                self.green_flags.append("Strong quick ratio = %.2f (good short-term liquidity)" % quick)

        print("  Enhanced checks: %d items evaluated" % len(result))
        self.results["enhanced"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 13. BENFORD'S LAW ANALYSIS (First-digit fraud detection)
    # ─────────────────────────────────────────────────────────────────────────
    def benfords_law_analysis(self):
        """Apply Benford's Law: first-digit distribution of financial numbers
        should follow log10(1 + 1/d). Deviation suggests fabricated numbers.
        Uses chi-squared test; p < 0.05 => significant deviation (suspect).
        """
        # Collect all non-zero financial values from annual + quarterly data
        values = []
        for df in [self.inc, self.bal, self.cf, self.inc_q, self.bal_q, self.cf_q]:
            if df is None or df.empty:
                continue
            for col_idx in range(df.shape[1]):
                for row_idx in range(df.shape[0]):
                    try:
                        v = float(df.iloc[row_idx, col_idx])
                        if not math.isnan(v) and v != 0:
                            values.append(abs(v))
                    except (TypeError, ValueError):
                        continue

        if len(values) < 50:
            self.results["benford"] = {"available": False,
                                       "reason": "Need 50+ data points, got %d" % len(values)}
            print("  Benford's Law: Insufficient data (%d values, need 50+)" % len(values))
            return

        # Expected Benford distribution for digits 1-9
        expected_pct = {d: math.log10(1 + 1.0 / d) * 100 for d in range(1, 10)}

        # Extract first digit from each value
        digit_counts = {d: 0 for d in range(1, 10)}
        total = 0
        for v in values:
            s = "%.10e" % v  # scientific notation
            for ch in s:
                if ch.isdigit() and ch != '0':
                    d = int(ch)
                    digit_counts[d] += 1
                    total += 1
                    break

        if total < 50:
            self.results["benford"] = {"available": False,
                                       "reason": "Too few leading digits extracted"}
            return

        # Observed percentages
        observed_pct = {d: (digit_counts[d] / total) * 100 for d in range(1, 10)}

        # Chi-squared statistic
        chi_sq = 0
        for d in range(1, 10):
            expected_count = expected_pct[d] / 100 * total
            observed_count = digit_counts[d]
            if expected_count > 0:
                chi_sq += (observed_count - expected_count) ** 2 / expected_count

        # Chi-squared critical values for df=8 (9 digits - 1)
        # p=0.05 => 15.507,  p=0.01 => 20.090
        if chi_sq > 20.09:
            verdict = "SIGNIFICANT DEVIATION (p<0.01)"
            conformity = "FAIL"
            self.red_flags.append((
                "Benford's Law FAILED (chi-sq=%.1f, p<0.01) — financial numbers show unnatural distribution" % chi_sq,
                "critical"))
        elif chi_sq > 15.507:
            verdict = "MODERATE DEVIATION (p<0.05)"
            conformity = "MARGINAL"
            self.red_flags.append((
                "Benford's Law marginal (chi-sq=%.1f, p<0.05) — some deviation in number distribution" % chi_sq,
                "minor"))
        else:
            verdict = "CONFORMS (p>0.05)"
            conformity = "PASS"
            self.green_flags.append(
                "Financial numbers conform to Benford's Law (chi-sq=%.1f) — no evidence of fabrication" % chi_sq)

        # Mean Absolute Deviation (MAD) — more robust measure
        mad = sum(abs(observed_pct[d] - expected_pct[d]) for d in range(1, 10)) / 9

        result = {
            "available": True,
            "total_values": total,
            "chi_squared": chi_sq,
            "mad": mad,
            "verdict": verdict,
            "conformity": conformity,
            "expected": expected_pct,
            "observed": observed_pct,
            "digit_counts": digit_counts,
        }

        print("  Benford's Law  : chi-sq=%.1f  MAD=%.2f  => %s (%d values)" % (
            chi_sq, mad, conformity, total))
        self.results["benford"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 14. MONTIER C-SCORE (6-variable manipulation detector)
    # ─────────────────────────────────────────────────────────────────────────
    def montier_c_score(self):
        """Montier C-Score: count of 6 manipulation signals (0=clean, 6=alarm).
        1. Growing NI but declining CFO
        2. Growing receivable days (DSO)
        3. Growing inventory days (DIO)
        4. Growing other current assets to revenue
        5. Declining depreciation relative to PPE
        6. High total asset growth (>10%)
        """
        if self.d.years < 2:
            return

        c = 0
        details = {}

        # 1. Net income growing but CFO declining
        ni_t = self._i("net_income", 0); ni_t1 = self._i("net_income", 1)
        cfo_t = self._c("operating_cf", 0); cfo_t1 = self._c("operating_cf", 1)
        sig1 = False
        if not any(_nan(v) for v in [ni_t, ni_t1, cfo_t, cfo_t1]):
            sig1 = (ni_t > ni_t1) and (cfo_t < cfo_t1)
        details["NI up but CFO down"] = sig1
        if sig1: c += 1

        # 2. Receivable days increasing
        rev_t = self._i("revenue", 0); rev_t1 = self._i("revenue", 1)
        recv_t = self._b("receivables", 0); recv_t1 = self._b("receivables", 1)
        sig2 = False
        if not any(_nan(v) for v in [rev_t, rev_t1, recv_t, recv_t1]) and rev_t > 0 and rev_t1 > 0:
            dso_t = recv_t / rev_t * 365
            dso_t1 = recv_t1 / rev_t1 * 365
            sig2 = dso_t > dso_t1 * 1.05  # >5% increase
        details["DSO increasing"] = sig2
        if sig2: c += 1

        # 3. Inventory days increasing
        cogs_t = self._i("cogs", 0); cogs_t1 = self._i("cogs", 1)
        inv_t = self._b("inventory", 0); inv_t1 = self._b("inventory", 1)
        sig3 = False
        if not any(_nan(v) for v in [cogs_t, cogs_t1, inv_t, inv_t1]) and cogs_t > 0 and cogs_t1 > 0:
            dio_t = inv_t / cogs_t * 365
            dio_t1 = inv_t1 / cogs_t1 * 365
            sig3 = dio_t > dio_t1 * 1.05
        details["DIO increasing"] = sig3
        if sig3: c += 1

        # 4. Other current assets growing relative to revenue
        ca_t = self._b("current_assets", 0); ca_t1 = self._b("current_assets", 1)
        cash_t = self._b("cash", 0); cash_t1 = self._b("cash", 1)
        sig4 = False
        if not any(_nan(v) for v in [ca_t, ca_t1, cash_t, cash_t1, recv_t, recv_t1, inv_t, inv_t1, rev_t, rev_t1]):
            oca_t = ca_t - (cash_t if not _nan(cash_t) else 0) - (recv_t if not _nan(recv_t) else 0) - (inv_t if not _nan(inv_t) else 0)
            oca_t1 = ca_t1 - (cash_t1 if not _nan(cash_t1) else 0) - (recv_t1 if not _nan(recv_t1) else 0) - (inv_t1 if not _nan(inv_t1) else 0)
            if rev_t > 0 and rev_t1 > 0:
                sig4 = (oca_t / rev_t) > (oca_t1 / rev_t1) * 1.10
        details["Other CA/Rev rising"] = sig4
        if sig4: c += 1

        # 5. Depreciation rate declining (boosting profits)
        dep_t = self._i("depreciation", 0); dep_t1 = self._i("depreciation", 1)
        ppe_t = self._b("ppe_gross", 0); ppe_t1 = self._b("ppe_gross", 1)
        sig5 = False
        if not any(_nan(v) for v in [dep_t, dep_t1, ppe_t, ppe_t1]) and ppe_t > 0 and ppe_t1 > 0:
            dep_rate_t = dep_t / ppe_t
            dep_rate_t1 = dep_t1 / ppe_t1
            sig5 = dep_rate_t < dep_rate_t1 * 0.90  # >10% decline in dep rate
        details["Dep rate declining"] = sig5
        if sig5: c += 1

        # 6. High total asset growth (>10%)
        ta_t = self._b("total_assets", 0); ta_t1 = self._b("total_assets", 1)
        sig6 = False
        if not _nan(ta_t) and not _nan(ta_t1) and ta_t1 > 0:
            sig6 = (ta_t / ta_t1 - 1) > 0.10
        details["TA growth > 10%"] = sig6
        if sig6: c += 1

        if c >= 4:
            verdict = "HIGH MANIPULATION RISK"
            score = 2
            self.red_flags.append((
                "Montier C-Score = %d/6 (high manipulation risk — multiple signals triggered)" % c,
                "critical"))
        elif c >= 2:
            verdict = "MODERATE RISK"
            score = 5
            self.red_flags.append((
                "Montier C-Score = %d/6 (moderate manipulation signals)" % c,
                "minor"))
        else:
            verdict = "LOW RISK"
            score = 9
            self.green_flags.append("Montier C-Score = %d/6 (low manipulation risk)" % c)

        result = {"c_score": c, "details": details, "verdict": verdict, "score_10": score}
        print("  Montier C-Score: %d/6    => %s" % (c, verdict))
        self.results["montier"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 15. OHLSON O-SCORE (Logistic bankruptcy model)
    # ─────────────────────────────────────────────────────────────────────────
    def ohlson_o_score(self):
        """Ohlson O-Score: logistic bankruptcy probability.
        O = -1.32 - 0.407*SIZE + 6.03*TLTA - 1.43*WCTA + 0.0757*CLCA
            - 1.72*OENEG - 2.37*NITA - 1.83*FFOTL + 0.285*INTWO - 0.521*CHIN
        P(bankruptcy) = 1 / (1 + exp(-O))
        P > 0.5 => high risk.
        """
        ta = self._b("total_assets", 0)
        tl = self._b("total_liabilities", 0)
        wc = self._b("working_capital", 0)
        cl = self._b("current_liabilities", 0)
        ca = self._b("current_assets", 0)
        ni = self._i("net_income", 0)
        ni_prev = self._i("net_income", 1)
        cfo = self._c("operating_cf", 0)

        # SIZE = log(Total Assets / GNP price deflator) — we approximate with log(TA)
        size = math.log(ta) if not _nan(ta) and ta > 0 else 0
        tlta = safe_div(tl, ta)
        wcta = safe_div(wc, ta)
        clca = safe_div(cl, ca)
        oeneg = 1.0 if (not _nan(tl) and not _nan(ta) and tl > ta) else 0.0  # 1 if TL > TA
        nita = safe_div(ni, ta)
        ffotl = safe_div(cfo, tl) if not _nan(cfo) else 0.0  # FFO ≈ CFO
        intwo = 1.0 if (not _nan(ni) and ni < 0 and not _nan(ni_prev) and ni_prev < 0) else 0.0

        # CHIN = (NI_t - NI_t-1) / (|NI_t| + |NI_t-1|)
        chin = 0.0
        if not _nan(ni) and not _nan(ni_prev):
            denom = abs(ni) + abs(ni_prev)
            if denom > 0:
                chin = (ni - ni_prev) / denom

        # Replace NaN with 0
        comps = {"SIZE": size, "TLTA": tlta, "WCTA": wcta, "CLCA": clca,
                 "OENEG": oeneg, "NITA": nita, "FFOTL": ffotl,
                 "INTWO": intwo, "CHIN": chin}
        for k in comps:
            if _nan(comps[k]):
                comps[k] = 0.0

        o = (-1.32
             - 0.407 * comps["SIZE"]
             + 6.03 * comps["TLTA"]
             - 1.43 * comps["WCTA"]
             + 0.0757 * comps["CLCA"]
             - 1.72 * comps["OENEG"]
             - 2.37 * comps["NITA"]
             - 1.83 * comps["FFOTL"]
             + 0.285 * comps["INTWO"]
             - 0.521 * comps["CHIN"])

        # Probability of bankruptcy
        try:
            prob = 1.0 / (1.0 + math.exp(-o))
        except OverflowError:
            prob = 1.0 if o > 0 else 0.0

        if prob > 0.5:
            verdict = "HIGH BANKRUPTCY RISK"
            score = 1
            self.red_flags.append((
                "Ohlson O-Score: %.0f%% bankruptcy probability (very high)" % (prob * 100),
                "critical"))
        elif prob > 0.3:
            verdict = "ELEVATED RISK"
            score = 4
            self.red_flags.append((
                "Ohlson O-Score: %.0f%% bankruptcy probability (elevated)" % (prob * 100),
                "minor"))
        else:
            verdict = "LOW RISK"
            score = 9
            self.green_flags.append(
                "Ohlson O-Score: %.0f%% bankruptcy probability (low risk)" % (prob * 100))

        result = {"o_score": o, "probability": prob, "components": comps,
                  "verdict": verdict, "score_10": score}
        print("  Ohlson O-Score : %.2f  (P=%.0f%%)  => %s" % (o, prob * 100, verdict))
        self.results["ohlson"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 16. SUSTAINABLE GROWTH RATE (SGR)
    # ─────────────────────────────────────────────────────────────────────────
    def sustainable_growth_rate(self):
        """SGR = ROE x (1 - payout ratio). If actual growth > SGR, company must
        be borrowing more or diluting equity — unsustainable long-term."""
        ni = self._i("net_income", 0)
        eq = self._b("equity", 0)
        div_paid = self._c("dividends_paid", 0)

        roe = safe_div(ni, eq)
        # Payout ratio = dividends / net income (dividends_paid is negative in CF)
        abs_div = abs(div_paid) if not _nan(div_paid) and div_paid < 0 else 0
        payout = safe_div(abs_div, ni) if not _nan(ni) and ni > 0 else 0
        if _nan(payout):
            payout = 0
        retention = 1.0 - min(payout, 1.0)  # cap at 100%

        sgr = roe * retention * 100 if not _nan(roe) else float("nan")

        result = {"sgr": sgr, "roe": roe * 100 if not _nan(roe) else float("nan"),
                  "payout_ratio": payout * 100, "retention_ratio": retention * 100}

        # Compare with actual revenue growth
        rev_t = self._i("revenue", 0); rev_t1 = self._i("revenue", 1)
        actual_growth = float("nan")
        if not _nan(rev_t) and not _nan(rev_t1) and rev_t1 > 0:
            actual_growth = (rev_t / rev_t1 - 1) * 100
        result["actual_growth"] = actual_growth

        if not _nan(sgr) and not _nan(actual_growth):
            gap = actual_growth - sgr
            result["growth_gap"] = gap
            if gap > 10:
                self.red_flags.append((
                    "Actual growth (%.1f%%) exceeds sustainable rate (%.1f%%) by %.1f%% — funded by debt/dilution" % (
                        actual_growth, sgr, gap), "major"))
            elif gap > 5:
                self.red_flags.append((
                    "Growth (%.1f%%) slightly above sustainable rate (%.1f%%)" % (
                        actual_growth, sgr), "minor"))
            elif sgr > 15 and actual_growth > 0:
                self.green_flags.append(
                    "Growth (%.1f%%) within sustainable rate (%.1f%%) — self-funded" % (
                        actual_growth, sgr))

        sgr_v = sgr if not _nan(sgr) else 0
        ag_v = actual_growth if not _nan(actual_growth) else 0
        print("  Sustainable Growth Rate: %.1f%%  (actual: %.1f%%)" % (sgr_v, ag_v))
        self.results["sgr"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 17. EARNINGS VOLATILITY & PERSISTENCE
    # ─────────────────────────────────────────────────────────────────────────
    def earnings_volatility(self):
        """Measure earnings stability: coefficient of variation of net income
        and net margin over available years. High volatility = low quality."""
        n = min(self.d.years, 4)
        ni_vals = []
        margin_vals = []
        for col in range(n):
            ni = self._i("net_income", col)
            rev = self._i("revenue", col)
            if not _nan(ni):
                ni_vals.append(ni)
            if not _nan(ni) and not _nan(rev) and rev > 0:
                margin_vals.append(ni / rev * 100)

        result = {}

        if len(ni_vals) >= 3:
            mean_ni = sum(ni_vals) / len(ni_vals)
            std_ni = (sum((x - mean_ni) ** 2 for x in ni_vals) / len(ni_vals)) ** 0.5
            cv_ni = safe_div(std_ni, abs(mean_ni)) * 100 if mean_ni != 0 else float("nan")
            result["earnings_cv"] = cv_ni
            result["earnings_mean"] = mean_ni
            result["earnings_std"] = std_ni

            if not _nan(cv_ni):
                if cv_ni > 50:
                    self.red_flags.append((
                        "Highly volatile earnings (CV=%.0f%%) — unpredictable, low quality" % cv_ni,
                        "major"))
                elif cv_ni > 30:
                    self.red_flags.append((
                        "Moderately volatile earnings (CV=%.0f%%)" % cv_ni, "minor"))
                elif cv_ni < 15:
                    self.green_flags.append(
                        "Stable, persistent earnings (CV=%.0f%%) — high predictability" % cv_ni)

        if len(margin_vals) >= 3:
            mean_m = sum(margin_vals) / len(margin_vals)
            std_m = (sum((x - mean_m) ** 2 for x in margin_vals) / len(margin_vals)) ** 0.5
            result["margin_mean"] = mean_m
            result["margin_std"] = std_m
            result["margin_cv"] = safe_div(std_m, abs(mean_m)) * 100

        cv_v = result.get("earnings_cv", float("nan"))
        print("  Earnings Volatility: CV=%.0f%%" % cv_v if not _nan(cv_v) else "  Earnings Volatility: N/A")
        self.results["volatility"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 18. DEGREE OF OPERATING LEVERAGE (DOL)
    # ─────────────────────────────────────────────────────────────────────────
    def operating_leverage(self):
        """DOL = %change in EBIT / %change in Revenue.
        High DOL means small revenue drops cause massive profit drops.
        Critical for cyclical sectors."""
        n = min(self.d.years, 4)
        rows = []

        for col in range(n - 1):
            ebit_t = self._i("ebit", col)
            ebit_t1 = self._i("ebit", col + 1)
            rev_t = self._i("revenue", col)
            rev_t1 = self._i("revenue", col + 1)

            ebit_chg = safe_div(ebit_t - ebit_t1, abs(ebit_t1)) * 100 if not any(_nan(v) for v in [ebit_t, ebit_t1]) and ebit_t1 != 0 else float("nan")
            rev_chg = safe_div(rev_t - rev_t1, abs(rev_t1)) * 100 if not any(_nan(v) for v in [rev_t, rev_t1]) and rev_t1 != 0 else float("nan")
            dol = safe_div(ebit_chg, rev_chg) if not _nan(rev_chg) and rev_chg != 0 else float("nan")

            label_t = self.d.fy_labels[col] if col < len(self.d.fy_labels) else "Y%d" % col
            label_t1 = self.d.fy_labels[col + 1] if (col + 1) < len(self.d.fy_labels) else "Y%d" % (col + 1)
            rows.append({
                "period": "%s vs %s" % (label_t, label_t1),
                "rev_change": rev_chg, "ebit_change": ebit_chg, "dol": dol,
            })

        result = {"rows": rows}

        # Use most recent DOL
        dol_vals = [r["dol"] for r in rows if not _nan(r["dol"]) and abs(r["dol"]) < 50]  # filter outliers
        if dol_vals:
            avg_dol = sum(abs(d) for d in dol_vals) / len(dol_vals)
            result["avg_dol"] = avg_dol
            if avg_dol > 5:
                self.red_flags.append((
                    "Very high operating leverage (DOL=%.1fx) — profits extremely sensitive to revenue changes" % avg_dol,
                    "major"))
            elif avg_dol > 3:
                self.red_flags.append((
                    "High operating leverage (DOL=%.1fx) — moderate earnings sensitivity" % avg_dol,
                    "minor"))
            elif avg_dol < 1.5:
                self.green_flags.append(
                    "Low operating leverage (DOL=%.1fx) — stable earnings relative to revenue" % avg_dol)
            print("  Operating Leverage: DOL=%.1fx avg" % avg_dol)
        else:
            print("  Operating Leverage: N/A")

        self.results["op_leverage"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 19. SPRINGATE S-SCORE (Alternative bankruptcy model)
    # ─────────────────────────────────────────────────────────────────────────
    def springate_s_score(self):
        """Springate S-Score: S < 0.862 => likely bankrupt.
        S = 1.03*A + 3.07*B + 0.66*C + 0.40*D
        A = Working Capital / Total Assets
        B = EBIT / Total Assets
        C = EBT (Pretax Income) / Current Liabilities
        D = Revenue / Total Assets
        """
        wc  = self._b("working_capital", 0)
        ta  = self._b("total_assets", 0)
        ebit = self._i("ebit", 0)
        pbt = self._i("pretax_income", 0)
        cl  = self._b("current_liabilities", 0)
        rev = self._i("revenue", 0)

        a = safe_div(wc, ta)
        b = safe_div(ebit, ta)
        c = safe_div(pbt, cl)
        d = safe_div(rev, ta)

        comps = {"A_WC_TA": a, "B_EBIT_TA": b, "C_EBT_CL": c, "D_Rev_TA": d}
        for k in comps:
            if _nan(comps[k]):
                comps[k] = 0.0

        s = (1.03 * comps["A_WC_TA"]
             + 3.07 * comps["B_EBIT_TA"]
             + 0.66 * comps["C_EBT_CL"]
             + 0.40 * comps["D_Rev_TA"])

        if s >= 0.862:
            verdict = "HEALTHY"
            score = 9
        else:
            verdict = "DISTRESS (bankruptcy risk)"
            score = 2

        result = {"s_score": s, "components": comps, "verdict": verdict, "score_10": score}

        if s < 0.862:
            self.red_flags.append(("Springate S-Score = %.2f (< 0.862: bankruptcy risk)" % s, "critical"))
        else:
            self.green_flags.append("Springate S-Score = %.2f (> 0.862: healthy)" % s)

        print("  Springate S-Score: %.2f  => %s" % (s, verdict))
        self.results["springate"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 20. PROMOTER HOLDING ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────
    def promoter_holding_analysis(self):
        """Analyse promoter shareholding trends from NSE data."""
        ph = self.d.promoter_holding
        if not ph or not ph.get("data"):
            self.results["promoter"] = {"available": False}
            return

        data = ph["data"]
        result = {"available": True, "data": data}

        latest = data[0] if data else {}
        promoter_pct = latest.get("promoter_pct", float("nan"))
        pledge_pct = latest.get("pledge_pct", float("nan"))

        result["promoter_pct"] = promoter_pct
        result["pledge_pct"] = pledge_pct

        if not _nan(promoter_pct):
            if promoter_pct < 25:
                self.red_flags.append((
                    "Very low promoter holding: %.1f%% (risk of hostile takeover or low commitment)" % promoter_pct,
                    "major"))
            elif promoter_pct > 70:
                self.green_flags.append(
                    "High promoter holding: %.1f%% (strong skin in the game)" % promoter_pct)

        if not _nan(pledge_pct) and pledge_pct > 0:
            if pledge_pct > 50:
                self.red_flags.append((
                    "%.1f%% of promoter holding is pledged (very high - forced selling risk)" % pledge_pct,
                    "critical"))
            elif pledge_pct > 20:
                self.red_flags.append((
                    "%.1f%% of promoter holding is pledged (elevated risk)" % pledge_pct,
                    "major"))
            elif pledge_pct > 0:
                self.red_flags.append((
                    "%.1f%% of promoter holding is pledged" % pledge_pct,
                    "minor"))
        elif not _nan(pledge_pct) and pledge_pct == 0:
            self.green_flags.append("Zero promoter pledge (no forced-selling risk)")

        # Trend check
        if len(data) >= 2:
            prom_now = data[0].get("promoter_pct", float("nan"))
            prom_prev = data[1].get("promoter_pct", float("nan"))
            if not _nan(prom_now) and not _nan(prom_prev):
                change = prom_now - prom_prev
                result["promoter_change"] = change
                if change < -3:
                    self.red_flags.append((
                        "Promoter holding declined by %.1f%% (from %.1f%% to %.1f%%)" % (
                            abs(change), prom_prev, prom_now), "major"))
                elif change > 2:
                    self.green_flags.append(
                        "Promoter holding increased by %.1f%% (to %.1f%%)" % (change, prom_now))

        self.results["promoter"] = result
        pp = promoter_pct if not _nan(promoter_pct) else 0
        pl = pledge_pct if not _nan(pledge_pct) else 0
        print("  Promoter Holding: %.1f%%%s" % (
            pp, " (%.1f%% pledged)" % pl if pl > 0 else ""))
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 21. ESM (Enhanced Surveillance Measure) CHECK
    # ─────────────────────────────────────────────────────────────────────────
    def esm_analysis(self):
        """Check if stock is under ESM framework."""
        esm = self.d.esm_status
        if not esm:
            self.results["esm"] = {"in_esm": False, "stage": None}
            return

        result = {
            "in_esm": esm.get("in_esm", False),
            "stage": esm.get("stage"),
            "details": esm.get("details", ""),
        }

        if result["in_esm"]:
            stage = result["stage"] or "Unknown"
            self.red_flags.append((
                "STOCK IS IN ESM %s - Enhanced Surveillance Measure (restricted trading, possible manipulation concerns)" % stage,
                "critical"))
            print("  ESM Status: IN ESM %s (WARNING)" % stage)
        else:
            self.green_flags.append("Stock is NOT in any ESM stage (normal trading)")
            print("  ESM Status: Not in ESM (normal trading)")

        self.results["esm"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 22. CREDIT RATING ANALYSIS
    # ─────────────────────────────────────────────────────────────────────────
    def credit_rating_analysis(self):
        """Analyse credit ratings from NSE filings for red/green flags."""
        cr = self.d.credit_ratings
        if not cr:
            self.results["credit_ratings"] = {"entries": [], "summary": "No credit rating filings found."}
            return

        entries = cr
        agencies_seen = set()
        highest_ratings = {}  # agency -> best rating
        has_downgrade = False
        has_negative_outlook = False

        # Rating quality hierarchy (higher index = better)
        rating_rank = {
            "B": 1, "B+": 2, "BB": 3, "BB+": 4,
            "BBB": 5, "BBB+": 6, "A": 7, "A+": 8,
            "A1": 8, "A1+": 9, "AA": 9, "AA+": 10, "AAA": 11,
        }

        for entry in entries:
            agency = entry.get("agency", "Unknown")
            agencies_seen.add(agency)
            outlook = entry.get("outlook", "")
            ratings = entry.get("ratings", [])

            if outlook and outlook.lower() in ("negative", "watch"):
                has_negative_outlook = True

            for r in ratings:
                rank = rating_rank.get(r.upper(), 0)
                if agency not in highest_ratings or rank > highest_ratings[agency][1]:
                    highest_ratings[agency] = (r, rank)

        # Detect if any filing mentions downgrade/upgrade
        for entry in entries:
            pdf_url = entry.get("pdf_url", "").lower()
            if "downgrad" in pdf_url:
                has_downgrade = True

        # Flags
        investment_grade = True
        for agency, (rating, rank) in highest_ratings.items():
            if rank >= 9:  # AA or above
                self.green_flags.append(
                    "Credit rating %s from %s (investment grade, strong)" % (rating, agency))
            elif rank >= 7:  # A range
                self.green_flags.append(
                    "Credit rating %s from %s (investment grade)" % (rating, agency))
            elif rank >= 5:  # BBB range
                self.red_flags.append((
                    "Credit rating %s from %s (lower investment grade)" % (rating, agency), "minor"))
            elif rank > 0:
                investment_grade = False
                self.red_flags.append((
                    "Credit rating %s from %s (below investment grade — junk)" % (rating, agency), "critical"))

        if has_negative_outlook:
            self.red_flags.append(("Credit rating outlook is Negative/Watch — potential downgrade ahead", "major"))
        if has_downgrade:
            self.red_flags.append(("Recent credit rating downgrade detected in filings", "major"))

        summary = "%d filings from %d agencies. " % (len(entries), len(agencies_seen))
        if highest_ratings:
            parts = ["%s: %s" % (ag, r) for ag, (r, _) in highest_ratings.items()]
            summary += "Latest ratings: " + ", ".join(parts) + "."

        result = {
            "entries": entries,
            "agencies": list(agencies_seen),
            "highest_ratings": {ag: r for ag, (r, _) in highest_ratings.items()},
            "has_downgrade": has_downgrade,
            "has_negative_outlook": has_negative_outlook,
            "investment_grade": investment_grade,
            "summary": summary,
        }

        print("  Credit Ratings : %s" % summary)
        self.results["credit_ratings"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # 23. OVERALL SCORING
    # ─────────────────────────────────────────────────────────────────────────
    def compute_overall_score(self):
        """Weighted composite score 0-100 with investment recommendation."""
        # ── Weight table (must sum to 1.0) ──
        # Category A: Manipulation Detection (25%)
        # Category B: Bankruptcy / Distress (20%)
        # Category C: Fundamental Quality (35%)
        # Category D: Deep Forensic Checks (20%)
        weights = {
            # --- A: Manipulation Detection (25%) ---
            "beneish":       0.10,  # Beneish M-Score
            "montier":       0.08,  # Montier C-Score
            "benford":       0.07,  # Benford's Law
            # --- B: Bankruptcy / Distress (20%) ---
            "altman":        0.08,  # Altman Z-Score
            "springate":     0.06,  # Springate S-Score
            "ohlson":        0.06,  # Ohlson O-Score
            # --- C: Fundamental Quality (35%) ---
            "piotroski":     0.10,  # Piotroski F-Score
            "cashflow":      0.10,  # Cash Flow Quality
            "debt":          0.08,  # Debt & Leverage
            "profitability": 0.07,  # Profitability Margins
            # --- D: Deep Forensic Checks (20%) ---
            "working_capital": 0.06,  # Working Capital Efficiency
            "volatility":     0.05,  # Earnings Volatility
            "op_leverage":    0.04,  # Operating Leverage
            "sgr":            0.05,  # Sustainable Growth Rate
        }

        # Human-readable labels for the report
        weight_labels = {
            "beneish":        "Beneish M-Score (Manipulation)",
            "montier":        "Montier C-Score (Manipulation)",
            "benford":        "Benford's Law (Number Integrity)",
            "altman":         "Altman Z-Score (Bankruptcy)",
            "springate":      "Springate S-Score (Bankruptcy)",
            "ohlson":         "Ohlson O-Score (Bankruptcy Prob)",
            "piotroski":      "Piotroski F-Score (Strength)",
            "cashflow":       "Cash Flow Quality",
            "debt":           "Debt & Leverage Health",
            "profitability":  "Profitability Margins",
            "working_capital": "Working Capital Efficiency",
            "volatility":     "Earnings Volatility",
            "op_leverage":    "Operating Leverage (DOL)",
            "sgr":            "Sustainable Growth Rate",
        }

        weighted_sum = 0
        total_weight = 0
        score_details = []  # For the PDF table

        # ── Score extraction for each technique ──
        raw_scores = {}

        # Beneish
        if "beneish" in self.results:
            raw_scores["beneish"] = self.results["beneish"]["score_10"]
        # Montier
        if "montier" in self.results:
            raw_scores["montier"] = self.results["montier"]["score_10"]
        # Benford
        if "benford" in self.results and self.results["benford"].get("available"):
            bf = self.results["benford"]
            if bf["conformity"] == "PASS":
                raw_scores["benford"] = 9
            elif bf["conformity"] == "MARGINAL":
                raw_scores["benford"] = 5
            else:
                raw_scores["benford"] = 2
        # Altman
        if "altman" in self.results:
            raw_scores["altman"] = self.results["altman"]["score_10"]
        # Springate
        if "springate" in self.results:
            raw_scores["springate"] = self.results["springate"]["score_10"]
        # Ohlson
        if "ohlson" in self.results:
            raw_scores["ohlson"] = self.results["ohlson"]["score_10"]
        # Piotroski
        if "piotroski" in self.results:
            raw_scores["piotroski"] = self.results["piotroski"]["score_10"]
        # Cash flow
        if "cashflow" in self.results:
            raw_scores["cashflow"] = self.results["cashflow"]["score_10"]
        # Debt
        if "debt" in self.results:
            raw_scores["debt"] = self.results["debt"]["score_10"]
        # Profitability
        if "profitability" in self.results and self.results["profitability"]:
            latest = self.results["profitability"][0]
            om = latest.get("operating_margin", float("nan"))
            if not _nan(om):
                if om > 20:
                    raw_scores["profitability"] = 9
                elif om > 12:
                    raw_scores["profitability"] = 7
                elif om > 5:
                    raw_scores["profitability"] = 5
                else:
                    raw_scores["profitability"] = 3
            else:
                raw_scores["profitability"] = 5
        # Working capital
        if "working_capital" in self.results:
            raw_scores["working_capital"] = self.results["working_capital"]["score_10"]
        # Earnings Volatility
        vol = self.results.get("volatility", {})
        cv = vol.get("earnings_cv", float("nan"))
        if not _nan(cv):
            if cv < 15:
                raw_scores["volatility"] = 9
            elif cv < 30:
                raw_scores["volatility"] = 6
            elif cv < 50:
                raw_scores["volatility"] = 4
            else:
                raw_scores["volatility"] = 2
        # Operating Leverage
        ol = self.results.get("op_leverage", {})
        avg_dol = ol.get("avg_dol", float("nan"))
        if not _nan(avg_dol):
            if avg_dol < 1.5:
                raw_scores["op_leverage"] = 9
            elif avg_dol < 3:
                raw_scores["op_leverage"] = 6
            elif avg_dol < 5:
                raw_scores["op_leverage"] = 4
            else:
                raw_scores["op_leverage"] = 2
        # SGR
        sg = self.results.get("sgr", {})
        gap = sg.get("growth_gap", float("nan"))
        if not _nan(gap):
            if gap < 0:
                raw_scores["sgr"] = 9  # growing below sustainable = self-funded
            elif gap < 5:
                raw_scores["sgr"] = 7
            elif gap < 10:
                raw_scores["sgr"] = 4
            else:
                raw_scores["sgr"] = 2

        # ── Compute weighted sum ──
        for key, wt in weights.items():
            if key in raw_scores:
                sc = raw_scores[key]
                weighted_sum += sc * wt
                total_weight += wt
                score_details.append({
                    "technique": weight_labels.get(key, key),
                    "weight": wt * 100,
                    "raw_score": sc,
                    "weighted": sc * wt * 10,  # scale to 0-100 contribution
                })

        if total_weight > 0:
            base_score = (weighted_sum / total_weight) * 10  # 0-100
        else:
            base_score = 50

        # Red flag penalty
        penalty = 0
        for flag_text, severity in self.red_flags:
            if severity == "critical":
                penalty += 6
            elif severity == "major":
                penalty += 3
            else:
                penalty += 1.5

        # Green flag bonus (capped)
        bonus = min(len(self.green_flags) * 1.5, 10)

        final_score = max(0, min(100, base_score - penalty + bonus))

        if final_score >= 75:
            recommendation = "STRONG BUY"
            rec_detail = "Excellent financial health with strong fundamentals. Low manipulation risk and solid cash flows."
        elif final_score >= 60:
            recommendation = "BUY"
            rec_detail = "Good overall financials. Monitor the identified concerns but fundamentals are sound."
        elif final_score >= 45:
            recommendation = "HOLD"
            rec_detail = "Mixed signals. Some positive indicators but significant concerns need monitoring."
        elif final_score >= 30:
            recommendation = "SELL / AVOID"
            rec_detail = "Multiple red flags detected. Deteriorating fundamentals or manipulation risk."
        else:
            recommendation = "STRONG AVOID"
            rec_detail = "Serious financial distress, manipulation risk, or fundamental weakness. High risk of capital loss."

        result = {
            "base_score": base_score, "penalty": penalty, "bonus": bonus,
            "final_score": final_score, "recommendation": recommendation,
            "rec_detail": rec_detail, "score_details": score_details,
        }

        print("\n  OVERALL SCORE  : %.0f / 100" % final_score)
        print("  RECOMMENDATION : %s" % recommendation)

        self.results["overall"] = result
        return result

    # ─────────────────────────────────────────────────────────────────────────
    # RUN ALL ANALYSES
    # ─────────────────────────────────────────────────────────────────────────
    def run_all(self):
        """Execute all forensic checks."""
        print("\n[3/5] Running forensic analysis...")

        if self.d.years >= 2:
            self.beneish_m_score()
            self.altman_z_score()
            self.piotroski_f_score()
            self.dupont_analysis()
            self.profitability_analysis()
            self.cash_flow_analysis()
            self.working_capital_analysis()
            self.debt_analysis()
            self.additional_checks()
            self.quarterly_trends()
            self.growth_analysis()
            self.enhanced_forensic_checks()
            self.benfords_law_analysis()
            self.montier_c_score()
            self.ohlson_o_score()
            self.sustainable_growth_rate()
            self.earnings_volatility()
            self.operating_leverage()
            self.springate_s_score()
            self.promoter_holding_analysis()
            self.esm_analysis()
            self.credit_rating_analysis()
            self.compute_overall_score()
        else:
            print("  Insufficient data for full analysis (need >= 2 years).")
            self.results["overall"] = {
                "final_score": 0, "recommendation": "INSUFFICIENT DATA",
                "rec_detail": "Not enough historical data to perform forensic analysis.",
                "base_score": 0, "penalty": 0, "bonus": 0,
            }

        return self.results


# ══════════════════════════════════════════════════════════════════════════════
# DEEP FUNDAMENTAL ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class DeepFundamentalAnalyzer:
    """
    Institutional-grade deep fundamental analysis.
    Covers: DCF valuation, quality scoring, capital allocation,
    NLP parsing of concalls/annual reports, working capital efficiency,
    incremental ROCE, valuation bands, and risk analysis.
    """

    def __init__(self, data, forensic_analyzer=None):
        self.d = data
        self.fa = forensic_analyzer  # reference to ForensicAnalyzer for flags
        self.inc = data.income_annual
        self.bal = data.balance_annual
        self.cf = data.cashflow_annual
        self.inc_q = data.income_quarterly
        self.bal_q = data.balance_quarterly
        self.cf_q = data.cashflow_quarterly
        self.info = data.info
        self.prices = data.historical_prices
        self.results = {}
        self.insights = []       # qualitative insights from NLP
        self.risk_factors = []   # identified risks
        self.moat_signals = []   # competitive advantage signals

    # ── Shorthand getters ────────────────────────────────────────────────────
    def _i(self, key, col=0):
        return safe_get(self.inc, key, col)

    def _b(self, key, col=0):
        return safe_get(self.bal, key, col)

    def _c(self, key, col=0):
        return safe_get(self.cf, key, col)

    def _iq(self, key, col=0):
        return safe_get(self.inc_q, key, col)

    def _bq(self, key, col=0):
        return safe_get(self.bal_q, key, col)

    # ─────────────────────────────────────────────────────────────────────────
    # VALUATION: Discounted Cash Flow (3-Stage)
    # ─────────────────────────────────────────────────────────────────────────
    def dcf_valuation(self):
        """
        3-stage DCF model:
          Stage 1: High growth (5 years) — based on recent FCF CAGR
          Stage 2: Fade period (5 years) — linear decline to terminal
          Stage 3: Terminal value — perpetuity growth
        """
        print("  DCF Valuation...")

        # Get FCF history
        fcf_values = []
        if self.cf is not None:
            for col in range(min(self.cf.shape[1], 5)):
                fcf = safe_get(self.cf, "fcf", col)
                if not _nan(fcf):
                    fcf_values.append(fcf)

        if len(fcf_values) < 2:
            self.results["dcf"] = {"status": "insufficient_data"}
            return

        # Latest FCF (most recent year)
        fcf_latest = fcf_values[0]
        if fcf_latest <= 0:
            # Use average of positive FCFs or operating CF
            positive_fcfs = [f for f in fcf_values if f > 0]
            if positive_fcfs:
                fcf_latest = sum(positive_fcfs) / len(positive_fcfs)
            else:
                ocf = safe_get(self.cf, "operating_cf", 0)
                capex = abs(safe_get(self.cf, "capex", 0))
                fcf_latest = ocf - capex if not (_nan(ocf) or _nan(capex)) else 0

        if fcf_latest <= 0:
            self.results["dcf"] = {"status": "negative_fcf", "fcf_latest": fcf_latest}
            return

        # FCF growth rate (historical CAGR)
        if len(fcf_values) >= 3 and fcf_values[-1] > 0 and fcf_values[0] > 0:
            n_years = len(fcf_values) - 1
            fcf_cagr = (fcf_values[0] / fcf_values[-1]) ** (1 / n_years) - 1
        else:
            # Fallback to revenue growth
            rev_0 = self._i("revenue", 0)
            rev_n = self._i("revenue", min(3, self.d.years - 1))
            if not _nan(rev_0) and not _nan(rev_n) and rev_n > 0:
                n = min(3, self.d.years - 1)
                fcf_cagr = (rev_0 / rev_n) ** (1 / n) - 1
            else:
                fcf_cagr = 0.10  # default 10%

        # Cap growth assumptions
        high_growth = min(max(fcf_cagr, 0.05), 0.35)  # 5% to 35%
        terminal_growth = 0.05  # 5% perpetuity (India nominal GDP)

        # Cost of equity (CAPM approximation)
        beta = self.info.get("beta", 1.0) or 1.0
        risk_free = 0.07   # India 10Y G-Sec yield ~7%
        market_premium = 0.06  # equity risk premium India
        cost_of_equity = risk_free + beta * market_premium

        # WACC estimation
        total_debt = self._b("total_debt", 0)
        equity_val = self.info.get("marketCap", 0)
        if _nan(total_debt):
            total_debt = 0
        if not equity_val:
            equity_val = self._b("equity", 0)
            if _nan(equity_val):
                equity_val = 1

        interest_exp = abs(self._i("interest_exp", 0)) if not _nan(self._i("interest_exp", 0)) else 0
        cost_of_debt = safe_div(interest_exp, total_debt, 0.08) if total_debt > 0 else 0.08
        tax_rate_val = self._i("tax", 0)
        pretax = self._i("pretax_income", 0)
        effective_tax = safe_div(tax_rate_val, pretax, 0.25) if not (_nan(tax_rate_val) or _nan(pretax)) else 0.25
        effective_tax = min(max(effective_tax, 0.15), 0.35)

        total_capital = equity_val + total_debt
        we = safe_div(equity_val, total_capital, 0.8)
        wd = safe_div(total_debt, total_capital, 0.2)
        wacc = we * cost_of_equity + wd * cost_of_debt * (1 - effective_tax)
        wacc = max(wacc, 0.08)  # Floor at 8%

        # Stage 1: High growth (years 1-5)
        stage1_cf = []
        cf = fcf_latest
        for yr in range(1, 6):
            cf *= (1 + high_growth)
            stage1_cf.append(cf / (1 + wacc) ** yr)

        # Stage 2: Fade (years 6-10) — linear decline from high_growth to terminal
        stage2_cf = []
        for yr in range(6, 11):
            fade_rate = high_growth - (high_growth - terminal_growth) * ((yr - 5) / 5)
            cf *= (1 + fade_rate)
            stage2_cf.append(cf / (1 + wacc) ** yr)

        # Stage 3: Terminal value
        terminal_cf = cf * (1 + terminal_growth)
        terminal_value = terminal_cf / (wacc - terminal_growth)
        pv_terminal = terminal_value / (1 + wacc) ** 10

        # Enterprise value
        ev = sum(stage1_cf) + sum(stage2_cf) + pv_terminal

        # Equity value
        cash = self._b("cash", 0)
        if _nan(cash):
            cash = 0
        equity_value = ev - total_debt + cash

        # Per share
        shares = self.info.get("sharesOutstanding", 0)
        if not shares:
            shares_val = self._b("shares_outstanding", 0)
            shares = shares_val if not _nan(shares_val) else 1

        intrinsic_per_share = equity_value / shares if shares > 0 else 0
        cmp = self.info.get("currentPrice", self.info.get("previousClose", 0)) or 0

        margin_of_safety = safe_div(intrinsic_per_share - cmp, intrinsic_per_share) * 100 if intrinsic_per_share > 0 else 0

        self.results["dcf"] = {
            "status": "computed",
            "fcf_latest_cr": to_cr(fcf_latest),
            "fcf_cagr_pct": fcf_cagr * 100,
            "high_growth_pct": high_growth * 100,
            "terminal_growth_pct": terminal_growth * 100,
            "wacc_pct": wacc * 100,
            "cost_of_equity_pct": cost_of_equity * 100,
            "beta": beta,
            "ev_cr": to_cr(ev),
            "equity_value_cr": to_cr(equity_value),
            "intrinsic_per_share": intrinsic_per_share,
            "cmp": cmp,
            "margin_of_safety_pct": margin_of_safety,
            "pv_stage1_cr": to_cr(sum(stage1_cf)),
            "pv_stage2_cr": to_cr(sum(stage2_cf)),
            "pv_terminal_cr": to_cr(pv_terminal),
            "shares": shares,
        }

        # Flag interpretation
        if margin_of_safety > 30:
            self.moat_signals.append("DCF suggests %.0f%% margin of safety — significantly undervalued" % margin_of_safety)
        elif margin_of_safety < -30:
            self.risk_factors.append("DCF suggests stock is %.0f%% overvalued vs intrinsic value" % abs(margin_of_safety))

        print("    Intrinsic Value: Rs. %.0f | CMP: Rs. %.0f | MoS: %.1f%%" % (
            intrinsic_per_share, cmp, margin_of_safety))

    # ─────────────────────────────────────────────────────────────────────────
    # VALUATION: Reverse DCF (What growth is the market pricing in?)
    # ─────────────────────────────────────────────────────────────────────────
    def reverse_dcf(self):
        """Calculate implied growth rate from current market price."""
        print("  Reverse DCF...")

        dcf_data = self.results.get("dcf", {})
        if dcf_data.get("status") != "computed":
            self.results["reverse_dcf"] = {"status": "skipped"}
            return

        cmp = dcf_data["cmp"]
        shares = dcf_data["shares"]
        if cmp <= 0 or shares <= 0:
            self.results["reverse_dcf"] = {"status": "no_price"}
            return

        market_equity_value = cmp * shares
        total_debt = self._b("total_debt", 0)
        cash = self._b("cash", 0)
        if _nan(total_debt): total_debt = 0
        if _nan(cash): cash = 0
        market_ev = market_equity_value + total_debt - cash

        wacc = dcf_data["wacc_pct"] / 100
        terminal_growth = 0.05

        # Get latest FCF
        fcf_latest = dcf_data["fcf_latest_cr"] * 1e7  # back to absolute

        # Binary search for implied growth
        low, high = -0.10, 0.60
        for _ in range(50):
            mid = (low + high) / 2
            # Compute EV at this growth rate
            cf = fcf_latest
            pv_sum = 0
            for yr in range(1, 11):
                growth = mid - (mid - terminal_growth) * max(0, (yr - 5)) / 5 if yr > 5 else mid
                cf *= (1 + growth)
                pv_sum += cf / (1 + wacc) ** yr
            term_cf = cf * (1 + terminal_growth)
            term_val = term_cf / (wacc - terminal_growth)
            pv_sum += term_val / (1 + wacc) ** 10

            if pv_sum < market_ev:
                low = mid
            else:
                high = mid

        implied_growth = (low + high) / 2

        # Compare with historical growth
        hist_growth = dcf_data["fcf_cagr_pct"] / 100
        growth_premium = implied_growth - hist_growth

        self.results["reverse_dcf"] = {
            "status": "computed",
            "implied_growth_pct": implied_growth * 100,
            "historical_growth_pct": hist_growth * 100,
            "growth_premium_pct": growth_premium * 100,
        }

        if implied_growth > 0.30:
            self.risk_factors.append("Market pricing in %.0f%% growth — very aggressive expectation" % (implied_growth * 100))
        elif implied_growth < hist_growth * 0.5:
            self.moat_signals.append("Market pricing in only %.0f%% growth vs %.0f%% historical — potential re-rating candidate" % (
                implied_growth * 100, hist_growth * 100))

        print("    Implied Growth: %.1f%% | Historical: %.1f%%" % (
            implied_growth * 100, hist_growth * 100))

    # ─────────────────────────────────────────────────────────────────────────
    # VALUATION: Historical P/E and P/B Bands
    # ─────────────────────────────────────────────────────────────────────────
    def valuation_bands(self):
        """10-year historical P/E and P/B bands to assess relative valuation."""
        print("  Valuation Bands...")

        if self.prices is None or self.prices.empty:
            self.results["valuation_bands"] = {"status": "no_price_data"}
            return

        # Current ratios from yfinance
        pe_trailing = self.info.get("trailingPE", float("nan"))
        pe_forward = self.info.get("forwardPE", float("nan"))
        pb = self.info.get("priceToBook", float("nan"))
        ev_ebitda = self.info.get("enterpriseToEbitda", float("nan"))

        # Historical PE approximation from price history + EPS
        eps_trailing = self.info.get("trailingEps", 0) or 0

        # Get 5-year price stats for band analysis
        prices_5y = self.prices.tail(252 * 5) if len(self.prices) > 252 * 5 else self.prices
        price_high_5y = prices_5y["High"].max() if "High" in prices_5y.columns else float("nan")
        price_low_5y = prices_5y["Low"].min() if "Low" in prices_5y.columns else float("nan")
        price_avg_5y = prices_5y["Close"].mean() if "Close" in prices_5y.columns else float("nan")

        cmp = self.info.get("currentPrice", self.info.get("previousClose", 0)) or 0

        # PE band estimation
        pe_high = safe_div(price_high_5y, eps_trailing) if eps_trailing > 0 else float("nan")
        pe_low = safe_div(price_low_5y, eps_trailing) if eps_trailing > 0 else float("nan")
        pe_avg = safe_div(price_avg_5y, eps_trailing) if eps_trailing > 0 else float("nan")

        # Where does current PE sit in the band (percentile)
        if not (_nan(pe_trailing) or _nan(pe_low) or _nan(pe_high)) and pe_high > pe_low:
            pe_percentile = (pe_trailing - pe_low) / (pe_high - pe_low) * 100
        else:
            pe_percentile = float("nan")

        # Price position in 52-week range
        high_52w = self.info.get("fiftyTwoWeekHigh", float("nan"))
        low_52w = self.info.get("fiftyTwoWeekLow", float("nan"))
        if not (_nan(high_52w) or _nan(low_52w)) and high_52w > low_52w:
            price_position_52w = (cmp - low_52w) / (high_52w - low_52w) * 100
        else:
            price_position_52w = float("nan")

        self.results["valuation_bands"] = {
            "status": "computed",
            "pe_trailing": pe_trailing,
            "pe_forward": pe_forward,
            "pb_ratio": pb,
            "ev_ebitda": ev_ebitda,
            "pe_high_5y": pe_high,
            "pe_low_5y": pe_low,
            "pe_avg_5y": pe_avg,
            "pe_percentile": pe_percentile,
            "price_high_5y": price_high_5y,
            "price_low_5y": price_low_5y,
            "cmp": cmp,
            "high_52w": high_52w,
            "low_52w": low_52w,
            "price_position_52w": price_position_52w,
            "eps_trailing": eps_trailing,
        }

        # Interpretation
        if not _nan(pe_percentile):
            if pe_percentile > 85:
                self.risk_factors.append("PE at %.0f percentile of 5Y band — near historical highs" % pe_percentile)
            elif pe_percentile < 20:
                self.moat_signals.append("PE at %.0f percentile of 5Y band — near historical lows" % pe_percentile)

        print("    PE: %.1f (Fwd: %.1f) | P/B: %.1f | EV/EBITDA: %.1f" % (
            pe_trailing if not _nan(pe_trailing) else 0,
            pe_forward if not _nan(pe_forward) else 0,
            pb if not _nan(pb) else 0,
            ev_ebitda if not _nan(ev_ebitda) else 0))

    # ─────────────────────────────────────────────────────────────────────────
    # QUALITY: Capital Allocation & ROCE Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def capital_allocation_analysis(self):
        """
        Evaluate management's capital allocation:
        - ROCE vs WACC spread (value creation)
        - Incremental ROCE (returns on new capital deployed)
        - Reinvestment rate and growth quality
        """
        print("  Capital Allocation & ROCE...")

        years = min(self.d.years, 5)
        roce_series = []
        invested_capital_series = []
        ebit_series = []

        for col in range(years):
            ebit = self._i("operating_inc", col)
            tax_val = self._i("tax", col)
            pretax = self._i("pretax_income", col)
            tax_rate = safe_div(tax_val, pretax, 0.25) if not (_nan(tax_val) or _nan(pretax)) else 0.25
            tax_rate = min(max(tax_rate, 0.15), 0.35)
            nopat = ebit * (1 - tax_rate) if not _nan(ebit) else float("nan")

            total_assets = self._b("total_assets", col)
            current_liab = self._b("current_liabilities", col)
            cash_val = self._b("cash", col)
            if _nan(cash_val): cash_val = 0
            invested_cap = total_assets - current_liab - cash_val if not (_nan(total_assets) or _nan(current_liab)) else float("nan")

            roce = safe_div(nopat, invested_cap) * 100 if not (_nan(nopat) or _nan(invested_cap)) else float("nan")
            roce_series.append(roce)
            invested_capital_series.append(invested_cap)
            ebit_series.append(ebit)

        # Incremental ROCE (return on new capital deployed)
        incremental_roce_values = []
        for i in range(len(roce_series) - 1):
            delta_nopat = (ebit_series[i] - ebit_series[i + 1]) if not (_nan(ebit_series[i]) or _nan(ebit_series[i + 1])) else float("nan")
            delta_ic = (invested_capital_series[i] - invested_capital_series[i + 1]) if not (_nan(invested_capital_series[i]) or _nan(invested_capital_series[i + 1])) else float("nan")
            inc_roce = safe_div(delta_nopat, delta_ic) * 100 if not (_nan(delta_nopat) or _nan(delta_ic)) else float("nan")
            incremental_roce_values.append(inc_roce)

        avg_roce = sum(r for r in roce_series if not _nan(r)) / max(1, len([r for r in roce_series if not _nan(r)]))
        avg_inc_roce = sum(r for r in incremental_roce_values if not _nan(r)) / max(1, len([r for r in incremental_roce_values if not _nan(r)])) if incremental_roce_values else float("nan")

        # ROCE vs WACC spread
        dcf_data = self.results.get("dcf", {})
        wacc_pct = dcf_data.get("wacc_pct", 12)
        roce_wacc_spread = avg_roce - wacc_pct

        # Reinvestment rate
        capex_latest = abs(self._c("capex", 0)) if not _nan(self._c("capex", 0)) else 0
        dep_latest = abs(self._i("depreciation", 0)) if not _nan(self._i("depreciation", 0)) else 0
        net_capex = capex_latest - dep_latest
        nopat_latest = ebit_series[0] * 0.75 if not _nan(ebit_series[0]) else 0
        reinvestment_rate = safe_div(net_capex, nopat_latest) * 100 if nopat_latest > 0 else 0

        self.results["capital_allocation"] = {
            "roce_series_pct": roce_series,
            "avg_roce_pct": avg_roce,
            "incremental_roce_series": incremental_roce_values,
            "avg_incremental_roce_pct": avg_inc_roce,
            "roce_wacc_spread_pct": roce_wacc_spread,
            "wacc_pct": wacc_pct,
            "reinvestment_rate_pct": reinvestment_rate,
            "invested_capital_cr": [to_cr(ic) for ic in invested_capital_series],
        }

        # Flags
        if roce_wacc_spread > 10:
            self.moat_signals.append("ROCE-WACC spread of %.1f%% — strong value creation" % roce_wacc_spread)
        elif roce_wacc_spread < 0:
            self.risk_factors.append("ROCE below WACC — destroying shareholder value")

        if not _nan(avg_inc_roce) and avg_inc_roce > 20:
            self.moat_signals.append("Incremental ROCE %.1f%% — new capital deployed productively" % avg_inc_roce)

        print("    Avg ROCE: %.1f%% | ROCE-WACC Spread: %.1f%% | Incremental ROCE: %.1f%%" % (
            avg_roce, roce_wacc_spread, avg_inc_roce if not _nan(avg_inc_roce) else 0))

    # ─────────────────────────────────────────────────────────────────────────
    # QUALITY: Revenue & Margin Decomposition
    # ─────────────────────────────────────────────────────────────────────────
    def revenue_margin_analysis(self):
        """
        Multi-year revenue growth decomposition and margin trajectory.
        - Revenue CAGR (3Y, 5Y)
        - Gross/EBITDA/PAT margin trends
        - Operating leverage (margin expansion vs revenue growth)
        """
        print("  Revenue & Margin Analysis...")

        years = min(self.d.years, 5)
        rev_series, gp_series, ebitda_series, pat_series = [], [], [], []
        gm_series, em_series, pm_series = [], [], []

        for col in range(years):
            rev = self._i("revenue", col)
            gp = self._i("gross_profit", col)
            ebitda = self._i("ebitda", col)
            pat = self._i("net_income", col)

            rev_series.append(rev)
            gp_series.append(gp)
            ebitda_series.append(ebitda)
            pat_series.append(pat)

            gm_series.append(safe_div(gp, rev) * 100 if not (_nan(gp) or _nan(rev)) else float("nan"))
            em_series.append(safe_div(ebitda, rev) * 100 if not (_nan(ebitda) or _nan(rev)) else float("nan"))
            pm_series.append(safe_div(pat, rev) * 100 if not (_nan(pat) or _nan(rev)) else float("nan"))

        # CAGR calculations
        def _cagr(latest, oldest, n):
            if _nan(latest) or _nan(oldest) or oldest <= 0 or latest <= 0 or n <= 0:
                return float("nan")
            return ((latest / oldest) ** (1 / n) - 1) * 100

        rev_cagr_3y = _cagr(rev_series[0], rev_series[min(3, years - 1)], min(3, years - 1))
        rev_cagr_5y = _cagr(rev_series[0], rev_series[years - 1], years - 1) if years >= 4 else float("nan")
        pat_cagr_3y = _cagr(pat_series[0], pat_series[min(3, years - 1)], min(3, years - 1))

        # Margin trajectory (expanding or contracting?)
        valid_em = [e for e in em_series if not _nan(e)]
        margin_trend = "stable"
        if len(valid_em) >= 3:
            if valid_em[0] > valid_em[-1] + 2:
                margin_trend = "expanding"
            elif valid_em[0] < valid_em[-1] - 2:
                margin_trend = "contracting"

        self.results["revenue_margins"] = {
            "revenue_series_cr": [to_cr(r) for r in rev_series],
            "pat_series_cr": [to_cr(p) for p in pat_series],
            "gross_margin_series": gm_series,
            "ebitda_margin_series": em_series,
            "pat_margin_series": pm_series,
            "rev_cagr_3y_pct": rev_cagr_3y,
            "rev_cagr_5y_pct": rev_cagr_5y,
            "pat_cagr_3y_pct": pat_cagr_3y,
            "margin_trend": margin_trend,
        }

        if not _nan(rev_cagr_3y) and rev_cagr_3y > 20:
            self.moat_signals.append("Revenue CAGR %.1f%% (3Y) — strong topline growth" % rev_cagr_3y)
        if margin_trend == "expanding":
            self.moat_signals.append("EBITDA margins expanding — operating leverage playing out")
        elif margin_trend == "contracting":
            self.risk_factors.append("EBITDA margins contracting — competitive pressure or cost inflation")

        print("    Rev CAGR 3Y: %.1f%% | PAT CAGR 3Y: %.1f%% | Margin trend: %s" % (
            rev_cagr_3y if not _nan(rev_cagr_3y) else 0,
            pat_cagr_3y if not _nan(pat_cagr_3y) else 0,
            margin_trend))

    # ─────────────────────────────────────────────────────────────────────────
    # QUALITY: Working Capital Efficiency (DSO, DIO, DPO, Cash Conversion)
    # ─────────────────────────────────────────────────────────────────────────
    def working_capital_efficiency(self):
        """
        Compute working capital days:
        - DSO (Days Sales Outstanding)
        - DIO (Days Inventory Outstanding)
        - DPO (Days Payable Outstanding)
        - Cash Conversion Cycle = DSO + DIO - DPO
        """
        print("  Working Capital Efficiency...")

        years = min(self.d.years, 5)
        dso_series, dio_series, dpo_series, ccc_series = [], [], [], []

        for col in range(years):
            rev = self._i("revenue", col)
            cogs = self._i("cogs", col)
            recv = self._b("receivables", col)
            inv = self._b("inventory", col)
            pay = self._b("payables", col)

            daily_rev = rev / 365 if not _nan(rev) and rev > 0 else float("nan")
            daily_cogs = cogs / 365 if not _nan(cogs) and cogs > 0 else float("nan")

            dso = safe_div(recv, daily_rev) if not _nan(recv) else float("nan")
            dio = safe_div(inv, daily_cogs) if not _nan(inv) else float("nan")
            dpo = safe_div(pay, daily_cogs) if not _nan(pay) else float("nan")

            ccc = float("nan")
            if not (_nan(dso) or _nan(dio) or _nan(dpo)):
                ccc = dso + dio - dpo

            dso_series.append(dso)
            dio_series.append(dio)
            dpo_series.append(dpo)
            ccc_series.append(ccc)

        # Trend in cash conversion cycle
        valid_ccc = [c for c in ccc_series if not _nan(c)]
        ccc_trend = "stable"
        if len(valid_ccc) >= 3:
            if valid_ccc[0] < valid_ccc[-1] - 5:
                ccc_trend = "improving"
            elif valid_ccc[0] > valid_ccc[-1] + 10:
                ccc_trend = "deteriorating"

        self.results["working_capital_eff"] = {
            "dso_series": dso_series,
            "dio_series": dio_series,
            "dpo_series": dpo_series,
            "ccc_series": ccc_series,
            "ccc_trend": ccc_trend,
        }

        if ccc_trend == "deteriorating":
            self.risk_factors.append("Cash conversion cycle deteriorating — working capital bloat")
        elif ccc_trend == "improving":
            self.moat_signals.append("Cash conversion cycle improving — better working capital efficiency")

        latest_ccc = valid_ccc[0] if valid_ccc else 0
        print("    CCC: %.0f days | DSO: %.0f | DIO: %.0f | DPO: %.0f | Trend: %s" % (
            latest_ccc, dso_series[0] if not _nan(dso_series[0]) else 0,
            dio_series[0] if not _nan(dio_series[0]) else 0,
            dpo_series[0] if not _nan(dpo_series[0]) else 0, ccc_trend))

    # ─────────────────────────────────────────────────────────────────────────
    # QUALITY: Earnings Quality & Accrual Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def earnings_quality_analysis(self):
        """
        Assess quality of reported earnings:
        - CFO/PAT ratio (cash backing of profits)
        - Accrual ratio (balance sheet vs cash)
        - Revenue vs receivables growth divergence
        - Capex vs depreciation (maintenance vs growth)
        """
        print("  Earnings Quality...")

        years = min(self.d.years, 4)
        cfo_pat_series = []
        accrual_series = []

        for col in range(years):
            pat = self._i("net_income", col)
            cfo = self._c("operating_cf", col)
            ta_curr = self._b("total_assets", col)
            ta_prev = self._b("total_assets", col + 1) if col + 1 < self.d.years else float("nan")

            cfo_pat = safe_div(cfo, pat) if not (_nan(cfo) or _nan(pat) or pat == 0) else float("nan")
            cfo_pat_series.append(cfo_pat)

            # Balance sheet accrual ratio
            avg_ta = (ta_curr + ta_prev) / 2 if not (_nan(ta_curr) or _nan(ta_prev)) else ta_curr
            accrual = safe_div(pat - cfo, avg_ta) if not (_nan(pat) or _nan(cfo) or _nan(avg_ta)) else float("nan")
            accrual_series.append(accrual)

        # Revenue vs receivables growth divergence
        rev_growth = safe_div(self._i("revenue", 0) - self._i("revenue", 1), abs(self._i("revenue", 1))) * 100 if self.d.years >= 2 else float("nan")
        recv_growth = safe_div(self._b("receivables", 0) - self._b("receivables", 1), abs(self._b("receivables", 1))) * 100 if self.d.years >= 2 else float("nan")
        rev_recv_divergence = (recv_growth - rev_growth) if not (_nan(rev_growth) or _nan(recv_growth)) else float("nan")

        # Capex analysis
        capex = abs(self._c("capex", 0)) if not _nan(self._c("capex", 0)) else 0
        dep = abs(self._i("depreciation", 0)) if not _nan(self._i("depreciation", 0)) else 0
        capex_dep_ratio = safe_div(capex, dep) if dep > 0 else float("nan")

        avg_cfo_pat = sum(r for r in cfo_pat_series if not _nan(r)) / max(1, len([r for r in cfo_pat_series if not _nan(r)]))
        avg_accrual = sum(r for r in accrual_series if not _nan(r)) / max(1, len([r for r in accrual_series if not _nan(r)]))

        self.results["earnings_quality"] = {
            "cfo_pat_series": cfo_pat_series,
            "avg_cfo_pat": avg_cfo_pat,
            "accrual_ratio_series": accrual_series,
            "avg_accrual_ratio": avg_accrual,
            "rev_growth_pct": rev_growth,
            "receivables_growth_pct": recv_growth,
            "rev_recv_divergence_pct": rev_recv_divergence,
            "capex_dep_ratio": capex_dep_ratio,
        }

        # Flags
        if avg_cfo_pat < 0.6:
            self.risk_factors.append("Low CFO/PAT ratio (%.2f) — profits not backed by cash" % avg_cfo_pat)
        elif avg_cfo_pat > 1.0:
            self.moat_signals.append("Strong CFO/PAT ratio (%.2f) — earnings well backed by cash" % avg_cfo_pat)

        if not _nan(rev_recv_divergence) and rev_recv_divergence > 20:
            self.risk_factors.append("Receivables growing %.0f%% faster than revenue — channel stuffing risk" % rev_recv_divergence)

        if avg_accrual > 0.10:
            self.risk_factors.append("High accrual ratio (%.2f) — earnings driven by non-cash items" % avg_accrual)

        print("    CFO/PAT: %.2f | Accrual Ratio: %.3f | Rev-Recv Divergence: %.1f%%" % (
            avg_cfo_pat, avg_accrual, rev_recv_divergence if not _nan(rev_recv_divergence) else 0))

    # ─────────────────────────────────────────────────────────────────────────
    # QUALITY: Debt Maturity & Stress Test
    # ─────────────────────────────────────────────────────────────────────────
    def debt_stress_test(self):
        """
        Assess debt sustainability under stress scenarios:
        - Interest coverage at various rate scenarios
        - Debt/EBITDA
        - Net debt / equity
        - Short-term vs long-term debt mix
        """
        print("  Debt Stress Test...")

        ebitda = self._i("ebitda", 0)
        interest = abs(self._i("interest_exp", 0)) if not _nan(self._i("interest_exp", 0)) else 0
        total_debt = self._b("total_debt", 0)
        lt_debt = self._b("long_term_debt", 0)
        st_debt = self._b("current_debt", 0)
        cash_val = self._b("cash", 0)
        equity = self._b("equity", 0)

        if _nan(total_debt): total_debt = 0
        if _nan(lt_debt): lt_debt = 0
        if _nan(st_debt): st_debt = total_debt - lt_debt if not _nan(lt_debt) else 0
        if _nan(cash_val): cash_val = 0

        net_debt = total_debt - cash_val
        icr_current = safe_div(ebitda, interest) if interest > 0 else float("inf")
        debt_ebitda = safe_div(total_debt, ebitda) if not _nan(ebitda) and ebitda > 0 else float("nan")
        net_debt_equity = safe_div(net_debt, equity) if not _nan(equity) and equity > 0 else float("nan")
        st_lt_mix = safe_div(st_debt, total_debt) * 100 if total_debt > 0 else 0

        # Stress test: what if interest rates double?
        icr_stress_2x = safe_div(ebitda, interest * 2) if interest > 0 else float("inf")
        icr_stress_3x = safe_div(ebitda, interest * 3) if interest > 0 else float("inf")

        # EBITDA decline scenario
        icr_ebitda_minus_30 = safe_div(ebitda * 0.7, interest) if interest > 0 else float("inf")

        self.results["debt_stress"] = {
            "total_debt_cr": to_cr(total_debt),
            "net_debt_cr": to_cr(net_debt),
            "cash_cr": to_cr(cash_val),
            "icr_current": icr_current,
            "icr_stress_2x_rates": icr_stress_2x,
            "icr_stress_3x_rates": icr_stress_3x,
            "icr_ebitda_minus_30pct": icr_ebitda_minus_30,
            "debt_ebitda": debt_ebitda,
            "net_debt_equity": net_debt_equity,
            "short_term_pct": st_lt_mix,
            "is_net_cash": net_debt < 0,
        }

        if net_debt < 0:
            self.moat_signals.append("Net cash position (Rs. %.0f Cr) — zero debt risk" % abs(to_cr(net_debt)))
        elif not _nan(debt_ebitda) and debt_ebitda > 3:
            self.risk_factors.append("Debt/EBITDA = %.1fx — highly leveraged" % debt_ebitda)
        if icr_stress_2x < 2:
            self.risk_factors.append("Interest coverage drops below 2x if rates double — refinancing risk")

        print("    Net Debt: Rs. %.0f Cr | ICR: %.1fx | Debt/EBITDA: %.1fx | Net D/E: %.2f" % (
            to_cr(net_debt), icr_current if icr_current != float("inf") else 99,
            debt_ebitda if not _nan(debt_ebitda) else 0,
            net_debt_equity if not _nan(net_debt_equity) else 0))

    # ─────────────────────────────────────────────────────────────────────────
    # QUALITY: Dividend & Buyback Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def shareholder_returns_analysis(self):
        """Analyze dividend consistency and buyback track record."""
        print("  Shareholder Returns...")

        years = min(self.d.years, 5)
        div_series = []
        payout_series = []

        for col in range(years):
            div = self._c("dividends_paid", col)
            pat = self._i("net_income", col)
            div_abs = abs(div) if not _nan(div) else 0
            div_series.append(div_abs)
            payout = safe_div(div_abs, pat) * 100 if not (_nan(pat) or pat <= 0) else 0
            payout_series.append(payout)

        div_yield = self.info.get("dividendYield", 0) or 0
        div_yield_pct = div_yield * 100

        # Consistency: how many years paid dividend
        div_paying_years = sum(1 for d in div_series if d > 0)
        is_consistent = div_paying_years >= years - 1

        avg_payout = sum(payout_series) / max(1, len(payout_series))

        self.results["shareholder_returns"] = {
            "dividend_series_cr": [to_cr(d) for d in div_series],
            "payout_ratio_series": payout_series,
            "avg_payout_pct": avg_payout,
            "dividend_yield_pct": div_yield_pct,
            "div_paying_years": div_paying_years,
            "total_years": years,
            "is_consistent": is_consistent,
        }

        if is_consistent and avg_payout > 20:
            self.moat_signals.append("Consistent dividend payer (%.0f%% avg payout) — shareholder friendly" % avg_payout)

        print("    Div Yield: %.1f%% | Avg Payout: %.0f%% | Consistency: %d/%d years" % (
            div_yield_pct, avg_payout, div_paying_years, years))

    # ─────────────────────────────────────────────────────────────────────────
    # NLP: Concall Transcript Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def analyze_concall_transcripts(self):
        """
        NLP analysis of earnings call transcripts:
        - Sentiment scoring (positive/negative tone)
        - Forward guidance extraction (growth keywords)
        - Risk keyword detection
        - Management confidence indicators
        - Promise tracking (comparing guidance vs outcomes)
        """
        print("  Concall NLP Analysis...")

        if not self.d.concall_texts:
            self.results["concall_nlp"] = {"status": "no_transcripts"}
            print("    No concall transcripts provided.")
            return

        # Keyword dictionaries for sentiment analysis
        POSITIVE_KEYWORDS = [
            "growth", "strong", "robust", "exceeded", "outperformed",
            "confident", "optimistic", "momentum", "record", "milestone",
            "profitable", "margin expansion", "market share gain",
            "order book", "pipeline", "demand", "tailwind",
            "upgrade", "beat", "improve", "recovery", "acceleration",
            "strategic", "innovation", "scaling", "efficiency",
        ]
        NEGATIVE_KEYWORDS = [
            "challenge", "headwind", "slowdown", "decline", "pressure",
            "uncertain", "cautious", "concern", "risk", "disruption",
            "weak", "below", "miss", "delay", "impairment",
            "write-off", "loss", "restructuring", "downturn",
            "competitive pressure", "margin compression", "attrition",
            "default", "stress", "litigation", "contingent",
        ]
        GUIDANCE_KEYWORDS = [
            "guidance", "outlook", "expect", "target", "forecast",
            "aim", "plan to", "going forward", "next quarter",
            "full year", "FY", "projection", "aspiration",
            "medium term", "long term", "runway", "visibility",
        ]
        RISK_KEYWORDS = [
            "regulatory", "compliance", "litigation", "contingent liability",
            "related party", "promoter pledge", "auditor concern",
            "going concern", "qualification", "deviation",
            "fraud", "whistleblower", "investigation",
            "forex", "currency", "geopolitical", "election",
            "raw material", "input cost", "supply chain",
        ]
        CONFIDENCE_INDICATORS = [
            "we are confident", "i am confident", "strong visibility",
            "comfortable", "on track", "well positioned",
            "market leader", "competitive advantage", "moat",
        ]
        HEDGING_INDICATORS = [
            "subject to", "depending on", "uncertain",
            "cannot guarantee", "no assurance", "may not",
            "volatile", "unpredictable", "if conditions",
        ]

        all_results = []
        aggregate_sentiment = 0
        total_guidance_statements = []
        total_risk_mentions = []

        for transcript in self.d.concall_texts:
            quarter = transcript.get("quarter", "Unknown")
            text = transcript.get("text", "")
            if not text:
                continue

            text_lower = text.lower()
            words = text_lower.split()
            total_words = len(words)

            # Sentiment scoring
            pos_count = sum(text_lower.count(kw) for kw in POSITIVE_KEYWORDS)
            neg_count = sum(text_lower.count(kw) for kw in NEGATIVE_KEYWORDS)
            sentiment_score = safe_div(pos_count - neg_count, pos_count + neg_count)
            if _nan(sentiment_score):
                sentiment_score = 0

            # Guidance extraction
            guidance_mentions = []
            sentences = text.replace(".", ".\n").split("\n")
            for sent in sentences:
                sent_lower = sent.lower()
                if any(kw in sent_lower for kw in GUIDANCE_KEYWORDS):
                    # Extract the guidance sentence
                    clean = sent.strip()
                    if 20 < len(clean) < 300:
                        guidance_mentions.append(clean)

            # Risk mentions
            risk_mentions = []
            for sent in sentences:
                sent_lower = sent.lower()
                if any(kw in sent_lower for kw in RISK_KEYWORDS):
                    clean = sent.strip()
                    if 20 < len(clean) < 300:
                        risk_mentions.append(clean)

            # Confidence vs hedging
            confidence_count = sum(text_lower.count(kw) for kw in CONFIDENCE_INDICATORS)
            hedging_count = sum(text_lower.count(kw) for kw in HEDGING_INDICATORS)
            confidence_ratio = safe_div(confidence_count, confidence_count + hedging_count)
            if _nan(confidence_ratio):
                confidence_ratio = 0.5

            qr = {
                "quarter": quarter,
                "total_words": total_words,
                "positive_mentions": pos_count,
                "negative_mentions": neg_count,
                "sentiment_score": sentiment_score,  # -1 to +1
                "guidance_statements": len(guidance_mentions),
                "risk_mentions": len(risk_mentions),
                "confidence_ratio": confidence_ratio,
                "top_guidance": guidance_mentions[:5],
                "top_risks": risk_mentions[:5],
            }
            all_results.append(qr)
            aggregate_sentiment += sentiment_score
            total_guidance_statements.extend(guidance_mentions[:3])
            total_risk_mentions.extend(risk_mentions[:3])

        n_transcripts = len(all_results)
        avg_sentiment = aggregate_sentiment / max(1, n_transcripts)

        # Sentiment trend (improving or deteriorating?)
        sentiment_trend = "stable"
        if n_transcripts >= 2:
            recent = all_results[0]["sentiment_score"]
            older = all_results[-1]["sentiment_score"]
            if recent > older + 0.15:
                sentiment_trend = "improving"
            elif recent < older - 0.15:
                sentiment_trend = "deteriorating"

        self.results["concall_nlp"] = {
            "status": "analyzed",
            "n_transcripts": n_transcripts,
            "quarterly_results": all_results,
            "avg_sentiment": avg_sentiment,
            "sentiment_trend": sentiment_trend,
            "key_guidance": total_guidance_statements[:10],
            "key_risks": total_risk_mentions[:10],
        }

        # Flags
        if avg_sentiment > 0.3:
            self.moat_signals.append("Concall sentiment strongly positive (%.2f) — bullish management tone" % avg_sentiment)
        elif avg_sentiment < -0.1:
            self.risk_factors.append("Concall sentiment negative (%.2f) — management tone cautious/worried" % avg_sentiment)

        if sentiment_trend == "deteriorating":
            self.risk_factors.append("Management sentiment deteriorating across recent concalls")

        print("    Transcripts: %d | Avg Sentiment: %.2f | Trend: %s" % (
            n_transcripts, avg_sentiment, sentiment_trend))

    # ─────────────────────────────────────────────────────────────────────────
    # NLP: Annual Report Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def analyze_annual_reports(self):
        """
        Parse annual report text for:
        - Related party transaction detection
        - Contingent liability tracking
        - Accounting policy changes
        - Auditor qualification/emphasis of matter
        - Key risk disclosures
        """
        print("  Annual Report NLP Analysis...")

        if not self.d.annual_report_texts:
            self.results["annual_report_nlp"] = {"status": "no_reports"}
            print("    No annual report texts provided.")
            return

        RELATED_PARTY_PATTERNS = [
            r"related\s+party\s+transaction",
            r"transaction[s]?\s+with\s+related\s+part",
            r"key\s+managerial\s+personnel",
            r"promoter\s+group\s+entit",
            r"loan[s]?\s+(?:to|from)\s+(?:director|promoter|related)",
        ]
        CONTINGENT_PATTERNS = [
            r"contingent\s+liabilit",
            r"claims?\s+against\s+the\s+company",
            r"pending\s+(?:litigation|case|suit)",
            r"disputed\s+(?:tax|demand|liabilit)",
            r"guarantee[s]?\s+given",
            r"letter[s]?\s+of\s+credit",
        ]
        AUDITOR_PATTERNS = [
            r"emphasis\s+of\s+matter",
            r"qualifi(?:ed|cation)",
            r"material\s+(?:weakness|misstatement|uncertainty)",
            r"going\s+concern",
            r"disclaimer\s+of\s+opinion",
            r"adverse\s+opinion",
            r"key\s+audit\s+matter",
        ]
        POLICY_CHANGE_PATTERNS = [
            r"change[s]?\s+in\s+accounting\s+polic",
            r"(?:adopted|implemented)\s+(?:new|revised)\s+(?:Ind\s*AS|standard)",
            r"retrospective(?:ly)?\s+(?:applied|restated)",
            r"reclassif(?:ied|ication)",
            r"change\s+in\s+(?:useful\s+life|depreciation\s+method|estimate)",
        ]

        all_year_results = []

        for report in self.d.annual_report_texts:
            year = report.get("year", "Unknown")
            text = report.get("text", "")
            if not text:
                continue

            text_lower = text.lower()

            # Extract related party mentions
            rpt_mentions = []
            for pat in RELATED_PARTY_PATTERNS:
                matches = re.finditer(pat, text_lower)
                for m in matches:
                    start = max(0, m.start() - 50)
                    end = min(len(text), m.end() + 200)
                    context = text[start:end].strip()
                    rpt_mentions.append(context)

            # Extract contingent liabilities
            contingent_mentions = []
            for pat in CONTINGENT_PATTERNS:
                matches = re.finditer(pat, text_lower)
                for m in matches:
                    start = max(0, m.start() - 30)
                    end = min(len(text), m.end() + 250)
                    context = text[start:end].strip()
                    contingent_mentions.append(context)

            # Extract amounts from contingent liability sections
            contingent_amounts = []
            for mention in contingent_mentions:
                amounts = re.findall(r'(?:Rs\.?|INR|₹)\s*([\d,]+(?:\.\d+)?)\s*(?:Cr|crore|Lakh|lakh|million)?', mention)
                contingent_amounts.extend(amounts)

            # Auditor concerns
            auditor_mentions = []
            for pat in AUDITOR_PATTERNS:
                matches = re.finditer(pat, text_lower)
                for m in matches:
                    start = max(0, m.start() - 30)
                    end = min(len(text), m.end() + 200)
                    context = text[start:end].strip()
                    auditor_mentions.append(context)

            # Accounting policy changes
            policy_changes = []
            for pat in POLICY_CHANGE_PATTERNS:
                matches = re.finditer(pat, text_lower)
                for m in matches:
                    start = max(0, m.start() - 30)
                    end = min(len(text), m.end() + 200)
                    context = text[start:end].strip()
                    policy_changes.append(context)

            yr_result = {
                "year": year,
                "related_party_mentions": len(rpt_mentions),
                "related_party_excerpts": rpt_mentions[:5],
                "contingent_mentions": len(contingent_mentions),
                "contingent_excerpts": contingent_mentions[:5],
                "contingent_amounts": contingent_amounts[:5],
                "auditor_concerns": len(auditor_mentions),
                "auditor_excerpts": auditor_mentions[:3],
                "policy_changes": len(policy_changes),
                "policy_change_excerpts": policy_changes[:3],
            }
            all_year_results.append(yr_result)

        # Aggregate flags
        has_auditor_concerns = any(r["auditor_concerns"] > 0 for r in all_year_results)
        high_rpt = any(r["related_party_mentions"] > 10 for r in all_year_results)
        has_policy_changes = any(r["policy_changes"] > 2 for r in all_year_results)

        self.results["annual_report_nlp"] = {
            "status": "analyzed",
            "years_analyzed": len(all_year_results),
            "yearly_results": all_year_results,
            "has_auditor_concerns": has_auditor_concerns,
            "high_related_party": high_rpt,
            "has_policy_changes": has_policy_changes,
        }

        if has_auditor_concerns:
            self.risk_factors.append("Auditor emphasis/qualification detected in annual report — investigate")
        if high_rpt:
            self.risk_factors.append("High related party transaction volume — governance risk")
        if has_policy_changes:
            self.risk_factors.append("Accounting policy changes detected — verify impact on earnings comparability")

        print("    Years analyzed: %d | Auditor concerns: %s | RPT flag: %s" % (
            len(all_year_results), has_auditor_concerns, high_rpt))

    # ─────────────────────────────────────────────────────────────────────────
    # NLP: Load documents from PDF files in a directory
    # ─────────────────────────────────────────────────────────────────────────
    def load_documents_from_directory(self, directory_path):
        """
        Scan a directory for PDFs and text files.
        Categorize as concall transcripts or annual reports based on filename.
        Populate self.d.concall_texts and self.d.annual_report_texts.
        """
        print("  Loading documents from: %s" % directory_path)

        if not os.path.isdir(directory_path):
            print("    Directory not found: %s" % directory_path)
            return

        CONCALL_PATTERNS = ["concall", "transcript", "earnings call", "con-call",
                           "conference call", "analyst meet", "investor call"]
        AR_PATTERNS = ["annual report", "annual_report", "ar_", "ar-",
                      "director report", "chairman", "mda", "management discussion"]

        files = os.listdir(directory_path)
        pdf_files = [f for f in files if f.lower().endswith(".pdf")]
        txt_files = [f for f in files if f.lower().endswith(".txt")]

        print("    Found %d PDFs, %d text files" % (len(pdf_files), len(txt_files)))

        for fname in pdf_files + txt_files:
            fpath = os.path.join(directory_path, fname)
            fname_lower = fname.lower()

            # Extract text
            text = ""
            if fname.lower().endswith(".pdf"):
                try:
                    reader = PyPDF2.PdfReader(fpath)
                    pages_text = []
                    for page in reader.pages:
                        pt = page.extract_text()
                        if pt:
                            pages_text.append(pt)
                    text = "\n".join(pages_text)
                except Exception as e:
                    print("    Failed to parse PDF %s: %s" % (fname, e))
                    continue
            else:
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                        text = fh.read()
                except Exception as e:
                    print("    Failed to read %s: %s" % (fname, e))
                    continue

            if len(text) < 100:
                continue

            # Categorize
            is_concall = any(p in fname_lower for p in CONCALL_PATTERNS)
            is_ar = any(p in fname_lower for p in AR_PATTERNS)

            # Try to extract year/quarter from filename
            year_match = re.search(r'(20\d{2})', fname)
            year_str = year_match.group(1) if year_match else "Unknown"
            quarter_match = re.search(r'Q([1-4])', fname, re.IGNORECASE)
            quarter_str = "Q%s %s" % (quarter_match.group(1), year_str) if quarter_match else year_str

            if is_concall:
                self.d.concall_texts.append({"quarter": quarter_str, "text": text})
                print("    Loaded concall: %s (%d chars)" % (fname, len(text)))
            elif is_ar:
                self.d.annual_report_texts.append({"year": year_str, "text": text})
                print("    Loaded annual report: %s (%d chars)" % (fname, len(text)))
            else:
                # Try to auto-detect from content
                text_sample = text[:2000].lower()
                if any(p in text_sample for p in ["transcript", "earnings call", "conference call", "q&a session"]):
                    self.d.concall_texts.append({"quarter": quarter_str, "text": text})
                    print("    Auto-detected concall: %s" % fname)
                elif any(p in text_sample for p in ["annual report", "director", "board of directors", "auditor"]):
                    self.d.annual_report_texts.append({"year": year_str, "text": text})
                    print("    Auto-detected annual report: %s" % fname)
                else:
                    print("    Skipped (unknown type): %s" % fname)

        print("    Total: %d concalls, %d annual reports loaded" % (
            len(self.d.concall_texts), len(self.d.annual_report_texts)))

    # ─────────────────────────────────────────────────────────────────────────
    # QUALITY: Competitive Moat Scoring
    # ─────────────────────────────────────────────────────────────────────────
    def moat_scoring(self):
        """
        Quantitative moat assessment based on financial characteristics:
        - ROCE consistency (>15% for 5+ years = durable moat)
        - Gross margin stability (low variance = pricing power)
        - Revenue growth + margin expansion = scalable moat
        - Low capex intensity = asset-light model
        - High FCF conversion = real earnings
        """
        print("  Moat Scoring...")

        score = 0
        max_score = 10
        components = {}

        # 1. ROCE consistency (0-2 points)
        ca = self.results.get("capital_allocation", {})
        roce_series = ca.get("roce_series_pct", [])
        high_roce_years = sum(1 for r in roce_series if not _nan(r) and r > 15)
        if high_roce_years >= 4:
            score += 2
            components["ROCE consistency"] = "Strong (>15%% for %d/%d years)" % (high_roce_years, len(roce_series))
        elif high_roce_years >= 2:
            score += 1
            components["ROCE consistency"] = "Moderate"
        else:
            components["ROCE consistency"] = "Weak"

        # 2. Gross margin stability (0-2 points)
        rm = self.results.get("revenue_margins", {})
        gm_series = rm.get("gross_margin_series", [])
        valid_gm = [g for g in gm_series if not _nan(g)]
        if len(valid_gm) >= 3:
            gm_std = (sum((g - sum(valid_gm)/len(valid_gm))**2 for g in valid_gm) / len(valid_gm)) ** 0.5
            if gm_std < 3:
                score += 2
                components["Margin stability"] = "Very stable (std: %.1f%%)" % gm_std
            elif gm_std < 6:
                score += 1
                components["Margin stability"] = "Moderate (std: %.1f%%)" % gm_std
            else:
                components["Margin stability"] = "Volatile (std: %.1f%%)" % gm_std
        else:
            components["Margin stability"] = "Insufficient data"

        # 3. Growth + margin expansion (0-2 points)
        rev_cagr = rm.get("rev_cagr_3y_pct", 0) or 0
        margin_trend = rm.get("margin_trend", "stable")
        if rev_cagr > 15 and margin_trend == "expanding":
            score += 2
            components["Growth quality"] = "High growth + expanding margins"
        elif rev_cagr > 10:
            score += 1
            components["Growth quality"] = "Good growth"
        else:
            components["Growth quality"] = "Modest"

        # 4. Asset-light model (0-2 points)
        capex_dep = self.results.get("earnings_quality", {}).get("capex_dep_ratio", float("nan"))
        if not _nan(capex_dep):
            if capex_dep < 1.5:
                score += 2
                components["Asset intensity"] = "Asset-light (capex/dep: %.1fx)" % capex_dep
            elif capex_dep < 2.5:
                score += 1
                components["Asset intensity"] = "Moderate"
            else:
                components["Asset intensity"] = "Capital intensive (capex/dep: %.1fx)" % capex_dep
        else:
            components["Asset intensity"] = "N/A"

        # 5. FCF conversion (0-2 points)
        eq = self.results.get("earnings_quality", {})
        cfo_pat = eq.get("avg_cfo_pat", 0) or 0
        if cfo_pat > 0.9:
            score += 2
            components["FCF conversion"] = "Excellent (%.0f%%)" % (cfo_pat * 100)
        elif cfo_pat > 0.7:
            score += 1
            components["FCF conversion"] = "Good (%.0f%%)" % (cfo_pat * 100)
        else:
            components["FCF conversion"] = "Weak (%.0f%%)" % (cfo_pat * 100)

        # Classify moat
        if score >= 8:
            moat_type = "WIDE MOAT"
        elif score >= 6:
            moat_type = "NARROW MOAT"
        elif score >= 4:
            moat_type = "EMERGING MOAT"
        else:
            moat_type = "NO MOAT"

        self.results["moat_score"] = {
            "score": score,
            "max_score": max_score,
            "moat_type": moat_type,
            "components": components,
        }

        print("    Moat Score: %d/%d => %s" % (score, max_score, moat_type))

    # ─────────────────────────────────────────────────────────────────────────
    # QUARTERLY: Sequential Momentum Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def quarterly_momentum(self):
        """
        Analyze quarter-on-quarter trends:
        - Revenue sequential growth
        - Margin improvement/deterioration
        - Beat/miss vs street estimates (if available)
        """
        print("  Quarterly Momentum...")

        if self.inc_q is None or self.inc_q.empty:
            self.results["quarterly_momentum"] = {"status": "no_data"}
            return

        quarters = min(self.inc_q.shape[1], 8)
        rev_q = []
        pat_q = []
        em_q = []

        for col in range(quarters):
            rev = self._iq("revenue", col)
            pat = self._iq("net_income", col)
            ebitda = self._iq("ebitda", col)
            rev_q.append(rev)
            pat_q.append(pat)
            em_q.append(safe_div(ebitda, rev) * 100 if not (_nan(ebitda) or _nan(rev)) else float("nan"))

        # QoQ growth
        rev_qoq = []
        for i in range(len(rev_q) - 1):
            g = pct_change_val(rev_q[i], rev_q[i + 1]) if not (_nan(rev_q[i]) or _nan(rev_q[i + 1])) else float("nan")
            rev_qoq.append(g)

        # YoY growth (Q0 vs Q4)
        rev_yoy = pct_change_val(rev_q[0], rev_q[4]) if len(rev_q) > 4 and not (_nan(rev_q[0]) or _nan(rev_q[4])) else float("nan")
        pat_yoy = pct_change_val(pat_q[0], pat_q[4]) if len(pat_q) > 4 and not (_nan(pat_q[0]) or _nan(pat_q[4])) else float("nan")

        # Acceleration/deceleration
        accel = "stable"
        if len(rev_qoq) >= 2 and not (_nan(rev_qoq[0]) or _nan(rev_qoq[1])):
            if rev_qoq[0] > rev_qoq[1] + 3:
                accel = "accelerating"
            elif rev_qoq[0] < rev_qoq[1] - 3:
                accel = "decelerating"

        self.results["quarterly_momentum"] = {
            "status": "computed",
            "revenue_qoq_series": rev_qoq,
            "revenue_yoy_pct": rev_yoy,
            "pat_yoy_pct": pat_yoy,
            "ebitda_margin_q_series": em_q,
            "acceleration": accel,
            "quarters_analyzed": quarters,
        }

        if accel == "accelerating":
            self.moat_signals.append("Revenue growth accelerating QoQ — positive momentum")
        elif accel == "decelerating":
            self.risk_factors.append("Revenue growth decelerating QoQ — momentum fading")

        print("    Rev YoY: %.1f%% | PAT YoY: %.1f%% | Momentum: %s" % (
            rev_yoy if not _nan(rev_yoy) else 0,
            pat_yoy if not _nan(pat_yoy) else 0, accel))

    # ─────────────────────────────────────────────────────────────────────────
    # RISK: Concentration & Regulatory Risk Assessment
    # ─────────────────────────────────────────────────────────────────────────
    def risk_assessment(self):
        """
        Compile comprehensive risk profile:
        - Business concentration signals
        - Leverage risk
        - Valuation risk
        - Governance risk
        - Macro/regulatory risk (from NLP)
        """
        print("  Risk Assessment...")

        risk_score = 0  # Higher = more risky (0-10)
        risk_breakdown = {}

        # 1. Leverage risk (0-3)
        ds = self.results.get("debt_stress", {})
        debt_ebitda = ds.get("debt_ebitda", 0) or 0
        if not _nan(debt_ebitda):
            if debt_ebitda > 4:
                risk_score += 3
                risk_breakdown["Leverage"] = "HIGH (Debt/EBITDA: %.1fx)" % debt_ebitda
            elif debt_ebitda > 2:
                risk_score += 1.5
                risk_breakdown["Leverage"] = "MODERATE (Debt/EBITDA: %.1fx)" % debt_ebitda
            else:
                risk_breakdown["Leverage"] = "LOW"
        elif ds.get("is_net_cash"):
            risk_breakdown["Leverage"] = "NONE (Net cash)"

        # 2. Valuation risk (0-2)
        vb = self.results.get("valuation_bands", {})
        pe_pctile = vb.get("pe_percentile", 50)
        if not _nan(pe_pctile):
            if pe_pctile > 85:
                risk_score += 2
                risk_breakdown["Valuation"] = "HIGH (PE at %.0f pctile)" % pe_pctile
            elif pe_pctile > 65:
                risk_score += 1
                risk_breakdown["Valuation"] = "MODERATE"
            else:
                risk_breakdown["Valuation"] = "LOW"
        else:
            risk_breakdown["Valuation"] = "N/A"

        # 3. Earnings quality risk (0-2)
        eq = self.results.get("earnings_quality", {})
        cfo_pat = eq.get("avg_cfo_pat", 1)
        if cfo_pat < 0.5:
            risk_score += 2
            risk_breakdown["Earnings quality"] = "HIGH RISK (CFO/PAT: %.2f)" % cfo_pat
        elif cfo_pat < 0.7:
            risk_score += 1
            risk_breakdown["Earnings quality"] = "MODERATE"
        else:
            risk_breakdown["Earnings quality"] = "LOW"

        # 4. Governance risk (0-2)
        ar_nlp = self.results.get("annual_report_nlp", {})
        gov_risk = 0
        if ar_nlp.get("has_auditor_concerns"):
            gov_risk += 1
        if ar_nlp.get("high_related_party"):
            gov_risk += 1
        risk_score += gov_risk
        risk_breakdown["Governance"] = "HIGH" if gov_risk >= 2 else ("MODERATE" if gov_risk == 1 else "LOW")

        # 5. Growth sustainability risk (0-1)
        rdcf = self.results.get("reverse_dcf", {})
        implied_g = rdcf.get("implied_growth_pct", 0) or 0
        if implied_g > 30:
            risk_score += 1
            risk_breakdown["Growth expectations"] = "HIGH (Market pricing %.0f%% growth)" % implied_g
        else:
            risk_breakdown["Growth expectations"] = "MANAGEABLE"

        # Risk category
        if risk_score >= 7:
            risk_category = "HIGH RISK"
        elif risk_score >= 4:
            risk_category = "MODERATE RISK"
        else:
            risk_category = "LOW RISK"

        self.results["risk_assessment"] = {
            "risk_score": risk_score,
            "max_score": 10,
            "risk_category": risk_category,
            "breakdown": risk_breakdown,
        }

        print("    Risk Score: %.1f/10 => %s" % (risk_score, risk_category))

    # ─────────────────────────────────────────────────────────────────────────
    # OVERALL: Deep Fundamental Score & Recommendation
    # ─────────────────────────────────────────────────────────────────────────
    def compute_deep_score(self):
        """
        Compute final deep fundamental score (0-100) combining:
        - Valuation attractiveness (25%)
        - Business quality / moat (25%)
        - Growth & momentum (20%)
        - Earnings quality (15%)
        - Risk profile (15%)
        """
        print("  Computing Deep Fundamental Score...")

        scores = {}

        # 1. Valuation (25%) — from DCF margin of safety + PE band position
        dcf = self.results.get("dcf", {})
        mos = dcf.get("margin_of_safety_pct", 0)
        vb = self.results.get("valuation_bands", {})
        pe_pctile = vb.get("pe_percentile", 50)

        val_score = 50  # neutral default
        if dcf.get("status") == "computed":
            if mos > 40: val_score = 90
            elif mos > 20: val_score = 75
            elif mos > 0: val_score = 60
            elif mos > -20: val_score = 40
            elif mos > -40: val_score = 25
            else: val_score = 10

        if not _nan(pe_pctile):
            pe_adj = (100 - pe_pctile) / 100 * 20  # low PE = bonus
            val_score = min(100, val_score + pe_adj)
        scores["valuation"] = val_score

        # 2. Business quality / moat (25%)
        moat = self.results.get("moat_score", {})
        moat_sc = moat.get("score", 5)
        quality_score = moat_sc * 10  # 0-100
        scores["quality"] = quality_score

        # 3. Growth & momentum (20%)
        rm = self.results.get("revenue_margins", {})
        rev_cagr = rm.get("rev_cagr_3y_pct", 0) or 0
        qm = self.results.get("quarterly_momentum", {})
        accel = qm.get("acceleration", "stable")

        growth_score = 50
        if rev_cagr > 25: growth_score = 85
        elif rev_cagr > 15: growth_score = 70
        elif rev_cagr > 8: growth_score = 55
        elif rev_cagr > 0: growth_score = 40
        else: growth_score = 20

        if accel == "accelerating": growth_score = min(100, growth_score + 10)
        elif accel == "decelerating": growth_score = max(0, growth_score - 10)
        scores["growth"] = growth_score

        # 4. Earnings quality (15%)
        eq = self.results.get("earnings_quality", {})
        cfo_pat = eq.get("avg_cfo_pat", 0.7)
        accrual = eq.get("avg_accrual_ratio", 0.05)

        eq_score = 50
        if cfo_pat > 1.0: eq_score = 85
        elif cfo_pat > 0.8: eq_score = 70
        elif cfo_pat > 0.6: eq_score = 50
        else: eq_score = 25

        if accrual > 0.10: eq_score = max(0, eq_score - 15)
        scores["earnings_quality"] = eq_score

        # 5. Risk profile (15%) — inverted (lower risk = higher score)
        ra = self.results.get("risk_assessment", {})
        risk_sc = ra.get("risk_score", 5)
        risk_score_inv = max(0, (10 - risk_sc) * 10)  # 0-100
        scores["risk"] = risk_score_inv

        # Weighted composite
        weights = {"valuation": 0.25, "quality": 0.25, "growth": 0.20,
                   "earnings_quality": 0.15, "risk": 0.15}
        final_score = sum(scores[k] * weights[k] for k in weights)

        # ── Bonus/Penalty from Extended Analyses (up to +/-10 pts) ──
        bonus = 0

        # Insider sentiment
        insider = self.results.get("insider_trading", {})
        if insider.get("net_sentiment") == "BULLISH":
            bonus += 3
        elif insider.get("net_sentiment") == "BEARISH":
            bonus -= 3

        # Shareholding trend
        shp = self.results.get("shareholding_trend", {})
        if shp.get("promoter_trend") == "increasing":
            bonus += 2
        elif shp.get("promoter_trend") == "decreasing":
            bonus -= 2

        # Relative strength
        rs = self.results.get("relative_strength", {})
        if rs.get("avg_alpha", 0) > 15:
            bonus += 2
        elif rs.get("avg_alpha", 0) < -15:
            bonus -= 2

        # Credit trajectory
        cr = self.results.get("credit_intelligence", {})
        if cr.get("trajectory") == "UPGRADED":
            bonus += 2
        elif cr.get("trajectory") == "DOWNGRADED":
            bonus -= 3

        # Institutional interest
        inst = self.results.get("institutional_holdings", {})
        if inst.get("total_institutional_pct", 0) > 30:
            bonus += 1
        if inst.get("n_mf_holders", 0) > 10:
            bonus += 1

        # Technical setup
        tech = self.results.get("technical", {})
        if tech.get("trend") == "STRONG UPTREND":
            bonus += 2
        elif tech.get("trend") == "DOWNTREND":
            bonus -= 2

        # Cap bonus
        bonus = max(-10, min(10, bonus))
        final_score = max(0, min(100, final_score + bonus))

        # Recommendation
        if final_score >= 75:
            recommendation = "STRONG BUY"
            rec_detail = "Excellent fundamentals with attractive valuation. High conviction."
        elif final_score >= 60:
            recommendation = "BUY"
            rec_detail = "Good business at reasonable valuation. Favorable risk-reward."
        elif final_score >= 45:
            recommendation = "HOLD"
            rec_detail = "Decent business but valuation or growth concerns limit upside."
        elif final_score >= 30:
            recommendation = "REDUCE"
            rec_detail = "Fundamental concerns or rich valuation. Consider trimming."
        else:
            recommendation = "SELL"
            rec_detail = "Significant fundamental weakness or extreme overvaluation."

        self.results["deep_score"] = {
            "final_score": final_score,
            "component_scores": scores,
            "weights": weights,
            "recommendation": recommendation,
            "rec_detail": rec_detail,
            "moat_signals": self.moat_signals[:10],
            "risk_factors": self.risk_factors[:10],
        }

        print("\n    ══════════════════════════════════════")
        print("    DEEP FUNDAMENTAL SCORE: %.0f / 100" % final_score)
        print("    RECOMMENDATION: %s" % recommendation)
        print("    ══════════════════════════════════════")
        print("    Valuation: %.0f | Quality: %.0f | Growth: %.0f | EQ: %.0f | Risk: %.0f" % (
            val_score, quality_score, growth_score, eq_score, risk_score_inv))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Shareholding Pattern Trend Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def shareholding_trend_analysis(self):
        """Analyze quarterly shareholding pattern trends."""
        print("  Shareholding Trend Analysis...")

        shp = self.d.shareholding_quarterly
        if not shp or len(shp) < 2:
            self.results["shareholding_trend"] = {"status": "insufficient_data"}
            return

        latest = shp[0]
        prev_q = shp[1] if len(shp) > 1 else latest
        older_q = shp[min(4, len(shp) - 1)]

        promoter_change_1y = latest["promoter_pct"] - older_q["promoter_pct"]
        promoter_change_qoq = latest["promoter_pct"] - prev_q["promoter_pct"]
        public_change_1y = latest["public_pct"] - older_q["public_pct"]

        promoter_series = [s["promoter_pct"] for s in shp[:8]]
        promoter_trend = "stable"
        if len(promoter_series) >= 4:
            recent_avg = sum(promoter_series[:2]) / 2
            older_avg = sum(promoter_series[-2:]) / 2
            if recent_avg > older_avg + 1.5:
                promoter_trend = "increasing"
            elif recent_avg < older_avg - 1.5:
                promoter_trend = "decreasing"

        self.results["shareholding_trend"] = {
            "status": "computed",
            "latest_promoter_pct": latest["promoter_pct"],
            "latest_public_pct": latest["public_pct"],
            "promoter_change_1y_pct": promoter_change_1y,
            "promoter_change_qoq_pct": promoter_change_qoq,
            "public_change_1y_pct": public_change_1y,
            "promoter_trend": promoter_trend,
            "quarters_tracked": len(shp),
            "series": [{"date": s["date"], "promoter": s["promoter_pct"], "public": s["public_pct"]} for s in shp[:8]],
        }

        if promoter_trend == "decreasing":
            self.risk_factors.append("Promoter holding declining (%.1f%% over 1Y)" % promoter_change_1y)
        elif promoter_trend == "increasing":
            self.moat_signals.append("Promoter increasing stake (%.1f%% over 1Y)" % promoter_change_1y)
        if latest["promoter_pct"] < 30:
            self.risk_factors.append("Low promoter holding (%.1f%%)" % latest["promoter_pct"])
        elif latest["promoter_pct"] > 70:
            self.moat_signals.append("High promoter holding (%.1f%%)" % latest["promoter_pct"])

        print("    Promoter: %.1f%% | 1Y Change: %+.1f%% | Trend: %s" % (
            latest["promoter_pct"], promoter_change_1y, promoter_trend))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Insider Trading / SAST Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def insider_trading_analysis(self):
        """Analyze insider buy/sell patterns from SAST disclosures."""
        print("  Insider Trading (SAST) Analysis...")

        sast = self.d.sast_disclosures
        if not sast:
            self.results["insider_trading"] = {"status": "no_data"}
            return

        buys = [s for s in sast if s["action"] == "BUY"]
        sells = [s for s in sast if s["action"] == "SELL"]
        total_buy_shares = sum(s["shares"] for s in buys)
        total_sell_shares = sum(s["shares"] for s in sells)

        net_sentiment = "NEUTRAL"
        if len(buys) > len(sells) + 1:
            net_sentiment = "BULLISH"
        elif len(sells) > len(buys) + 1:
            net_sentiment = "BEARISH"

        self.results["insider_trading"] = {
            "status": "computed",
            "total_disclosures": len(sast),
            "buys": len(buys),
            "sells": len(sells),
            "total_buy_shares": total_buy_shares,
            "total_sell_shares": total_sell_shares,
            "net_sentiment": net_sentiment,
            "recent_actions": sast[:5],
        }

        if net_sentiment == "BULLISH":
            self.moat_signals.append("Insider net buying (%d buys vs %d sells)" % (len(buys), len(sells)))
        elif net_sentiment == "BEARISH":
            self.risk_factors.append("Insider net selling (%d sells vs %d buys)" % (len(sells), len(buys)))

        print("    Disclosures: %d | Buys: %d | Sells: %d | Sentiment: %s" % (
            len(sast), len(buys), len(sells), net_sentiment))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Peer Comparison & Relative Valuation
    # ─────────────────────────────────────────────────────────────────────────
    def peer_comparison(self):
        """Compare company against sector peers on PE, P/B, returns."""
        print("  Peer Comparison...")

        sp = self.d.sector_peers
        if not sp or not sp.get("peers"):
            self.results["peer_comparison"] = {"status": "no_peers"}
            return

        peers = sp["peers"]
        sector_info = sp["sector_info"]
        own_pe = self.info.get("trailingPE", float("nan"))
        own_pb = self.info.get("priceToBook", float("nan"))
        sector_pe = sector_info.get("sector_pe", 0)

        peer_data = []
        for peer in peers[:10]:
            try:
                pticker = yf.Ticker(peer["symbol"] + ".NS")
                pinfo = pticker.info or {}
                peer_data.append({
                    "symbol": peer["symbol"],
                    "pe": pinfo.get("trailingPE", float("nan")),
                    "pb": pinfo.get("priceToBook", float("nan")),
                    "ev_ebitda": pinfo.get("enterpriseToEbitda", float("nan")),
                    "roe": pinfo.get("returnOnEquity", 0) * 100 if pinfo.get("returnOnEquity") else 0,
                    "market_cap_cr": pinfo.get("marketCap", 0) / 1e7,
                    "change_1y": peer.get("change_1y", 0),
                })
            except Exception:
                continue
            if len(peer_data) >= 5:
                break

        if not peer_data:
            self.results["peer_comparison"] = {"status": "no_peer_data"}
            return

        valid_pe = [p["pe"] for p in peer_data if not _nan(p["pe"]) and p["pe"] > 0]
        valid_pb = [p["pb"] for p in peer_data if not _nan(p["pb"]) and p["pb"] > 0]
        avg_peer_pe = sum(valid_pe) / len(valid_pe) if valid_pe else 0
        avg_peer_pb = sum(valid_pb) / len(valid_pb) if valid_pb else 0
        pe_discount = ((avg_peer_pe - own_pe) / avg_peer_pe * 100) if avg_peer_pe > 0 and not _nan(own_pe) else 0

        own_1y_return = 0
        if self.prices is not None and len(self.prices) > 252:
            own_1y_return = (self.prices["Close"].iloc[-1] / self.prices["Close"].iloc[-252] - 1) * 100

        self.results["peer_comparison"] = {
            "status": "computed",
            "sector": sector_info.get("sector", ""),
            "sector_index": sp.get("sector_index", ""),
            "sector_pe": sector_pe,
            "own_pe": own_pe,
            "own_pb": own_pb,
            "avg_peer_pe": avg_peer_pe,
            "avg_peer_pb": avg_peer_pb,
            "pe_discount_to_peers_pct": pe_discount,
            "own_1y_return_pct": own_1y_return,
            "peers": peer_data,
        }

        if pe_discount > 20:
            self.moat_signals.append("Trading at %.0f%% PE discount to peers" % pe_discount)
        elif pe_discount < -20:
            self.risk_factors.append("Trading at %.0f%% PE premium to peers" % abs(pe_discount))

        print("    Sector: %s | Own PE: %.1f | Peer avg PE: %.1f | Discount: %.0f%%" % (
            sector_info.get("sector", "?"), own_pe if not _nan(own_pe) else 0, avg_peer_pe, pe_discount))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Relative Strength vs Nifty 50
    # ─────────────────────────────────────────────────────────────────────────
    def relative_strength_analysis(self):
        """Calculate relative strength vs Nifty 50 over multiple timeframes."""
        print("  Relative Strength vs Nifty...")

        if self.prices is None or self.prices.empty:
            self.results["relative_strength"] = {"status": "no_data"}
            return

        try:
            nifty = yf.Ticker("^NSEI")
            nifty_hist = nifty.history(period="2y")
            if nifty_hist is None or nifty_hist.empty:
                self.results["relative_strength"] = {"status": "nifty_fetch_failed"}
                return
        except Exception:
            self.results["relative_strength"] = {"status": "nifty_fetch_failed"}
            return

        stock_close = self.prices["Close"]
        nifty_close = nifty_hist["Close"]
        common_dates = stock_close.index.intersection(nifty_close.index)
        if len(common_dates) < 20:
            self.results["relative_strength"] = {"status": "insufficient_overlap"}
            return

        stock_aligned = stock_close.loc[common_dates]
        nifty_aligned = nifty_close.loc[common_dates]

        periods = {"1M": 21, "3M": 63, "6M": 126, "1Y": 252}
        rs_data = {}
        for label, days in periods.items():
            if len(stock_aligned) >= days:
                stock_ret = (stock_aligned.iloc[-1] / stock_aligned.iloc[-days] - 1) * 100
                nifty_ret = (nifty_aligned.iloc[-1] / nifty_aligned.iloc[-days] - 1) * 100
                rs_data[label] = {"stock_return": stock_ret, "nifty_return": nifty_ret, "alpha": stock_ret - nifty_ret}

        alphas = [rs_data[k]["alpha"] for k in rs_data]
        avg_alpha = sum(alphas) / len(alphas) if alphas else 0

        self.results["relative_strength"] = {
            "status": "computed",
            "periods": rs_data,
            "avg_alpha": avg_alpha,
            "outperforming": avg_alpha > 0,
        }

        if avg_alpha > 15:
            self.moat_signals.append("Strong relative strength — outperforming Nifty by %.0f%%" % avg_alpha)
        elif avg_alpha < -15:
            self.risk_factors.append("Weak relative strength — underperforming Nifty by %.0f%%" % abs(avg_alpha))

        print("    Avg Alpha vs Nifty: %+.1f%% | %s" % (avg_alpha, "OUTPERFORMING" if avg_alpha > 0 else "UNDERPERFORMING"))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Technical Structure (200DMA, RSI, Delivery)
    # ─────────────────────────────────────────────────────────────────────────
    def technical_structure(self):
        """Basic technical context: DMA, RSI, delivery, volume trend."""
        print("  Technical Structure...")

        if self.prices is None or len(self.prices) < 200:
            self.results["technical"] = {"status": "insufficient_data"}
            return

        close = self.prices["Close"]
        volume = self.prices["Volume"] if "Volume" in self.prices.columns else None
        cmp = close.iloc[-1]

        dma_50 = close.rolling(50).mean().iloc[-1]
        dma_200 = close.rolling(200).mean().iloc[-1]
        above_50dma = cmp > dma_50
        above_200dma = cmp > dma_200
        golden_cross = dma_50 > dma_200
        dist_200dma_pct = (cmp / dma_200 - 1) * 100

        # RSI-14
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss
        rsi_14 = 100 - (100 / (1 + rs.iloc[-1])) if loss.iloc[-1] != 0 else 50

        # Volume trend
        vol_trend = "N/A"
        if volume is not None and not volume.empty:
            vol_20 = volume.rolling(20).mean().iloc[-1]
            vol_50 = volume.rolling(50).mean().iloc[-1]
            if vol_50 > 0:
                vol_ratio = vol_20 / vol_50
                if vol_ratio > 1.3:
                    vol_trend = "RISING (accumulation)"
                elif vol_ratio < 0.7:
                    vol_trend = "FALLING (distribution)"
                else:
                    vol_trend = "NORMAL"

        delivery_pct = self.d.delivery_data.get("delivery_pct", 0)

        if above_200dma and golden_cross and rsi_14 > 50:
            trend = "STRONG UPTREND"
        elif above_200dma and rsi_14 > 40:
            trend = "UPTREND"
        elif not above_200dma and not golden_cross and rsi_14 < 50:
            trend = "DOWNTREND"
        elif not above_200dma:
            trend = "WEAK"
        else:
            trend = "SIDEWAYS"

        self.results["technical"] = {
            "status": "computed",
            "cmp": cmp,
            "dma_50": dma_50,
            "dma_200": dma_200,
            "above_50dma": above_50dma,
            "above_200dma": above_200dma,
            "golden_cross": golden_cross,
            "dist_200dma_pct": dist_200dma_pct,
            "rsi_14": rsi_14,
            "volume_trend": vol_trend,
            "delivery_pct": delivery_pct,
            "trend": trend,
        }

        if trend in ("STRONG UPTREND", "UPTREND"):
            self.moat_signals.append("Price in %s (RSI: %.0f)" % (trend, rsi_14))
        elif trend == "DOWNTREND":
            self.risk_factors.append("Price in DOWNTREND (below 200DMA, RSI: %.0f)" % rsi_14)
        if delivery_pct > 60:
            self.moat_signals.append("High delivery %% (%.0f%%) — institutional interest" % delivery_pct)

        print("    Trend: %s | RSI: %.0f | 200DMA: Rs.%.0f (%+.1f%%) | Delivery: %.0f%%" % (
            trend, rsi_14, dma_200, dist_200dma_pct, delivery_pct))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Graham Number & Magic Formula
    # ─────────────────────────────────────────────────────────────────────────
    def graham_magic_formula(self):
        """Graham Number, Magic Formula (earnings yield + ROIC), PEG ratio."""
        print("  Graham Number & Magic Formula...")

        eps = self.info.get("trailingEps", 0) or 0
        bvps = self.info.get("bookValue", 0) or 0
        cmp = self.info.get("currentPrice", self.info.get("previousClose", 0)) or 0
        pe = self.info.get("trailingPE", 0) or 0

        graham_num = (22.5 * eps * bvps) ** 0.5 if eps > 0 and bvps > 0 else 0
        graham_upside = (graham_num / cmp - 1) * 100 if cmp > 0 and graham_num > 0 else 0

        ebit = self._i("operating_inc", 0)
        ev = self.info.get("enterpriseValue", 0) or 0
        earnings_yield = safe_div(ebit, ev) * 100 if not _nan(ebit) and ev > 0 else 0

        total_assets = self._b("total_assets", 0)
        current_liab = self._b("current_liabilities", 0)
        cash_val = self._b("cash", 0)
        if _nan(cash_val): cash_val = 0
        invested_cap = total_assets - current_liab - cash_val if not (_nan(total_assets) or _nan(current_liab)) else 0
        nopat = ebit * 0.75 if not _nan(ebit) else 0
        roic = safe_div(nopat, invested_cap) * 100 if invested_cap > 0 else 0

        growth_rate = self.results.get("revenue_margins", {}).get("pat_cagr_3y_pct", 0) or 0
        peg = safe_div(pe, growth_rate) if growth_rate > 0 else float("nan")

        self.results["graham_magic"] = {
            "graham_number": graham_num,
            "graham_upside_pct": graham_upside,
            "earnings_yield_pct": earnings_yield,
            "roic_pct": roic,
            "peg_ratio": peg,
            "eps": eps,
            "bvps": bvps,
            "cmp": cmp,
        }

        if graham_upside > 20:
            self.moat_signals.append("Graham Number Rs.%.0f implies %.0f%% upside" % (graham_num, graham_upside))
        if not _nan(peg) and peg < 1:
            self.moat_signals.append("PEG ratio %.2f < 1 — growth at reasonable price" % peg)
        elif not _nan(peg) and peg > 2.5:
            self.risk_factors.append("PEG ratio %.2f — overvalued relative to growth" % peg)

        print("    Graham: Rs.%.0f (%+.0f%%) | EY: %.1f%% | ROIC: %.1f%% | PEG: %.2f" % (
            graham_num, graham_upside, earnings_yield, roic, peg if not _nan(peg) else 0))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Capex Cycle & Asset Efficiency Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def capex_cycle_analysis(self):
        """Capex intensity, growth vs maintenance capex, asset turnover trend."""
        print("  Capex Cycle Analysis...")

        years = min(self.d.years, 5)
        capex_series = []
        rev_series = []
        asset_turnover = []

        for col in range(years):
            capex = abs(safe_get(self.cf, "capex", col)) if not _nan(safe_get(self.cf, "capex", col)) else 0
            rev = self._i("revenue", col)
            total_assets = self._b("total_assets", col)
            capex_series.append(capex)
            rev_series.append(rev)
            at = safe_div(rev, total_assets) if not (_nan(rev) or _nan(total_assets)) else float("nan")
            asset_turnover.append(at)

        capex_intensity = [safe_div(capex_series[i], rev_series[i]) * 100
                          if not _nan(rev_series[i]) and rev_series[i] > 0 else 0
                          for i in range(years)]

        dep_latest = abs(self._i("depreciation", 0)) if not _nan(self._i("depreciation", 0)) else 0
        capex_latest = capex_series[0] if capex_series else 0
        growth_capex = max(0, capex_latest - dep_latest)
        growth_capex_pct = safe_div(growth_capex, capex_latest) * 100 if capex_latest > 0 else 0

        valid_at = [a for a in asset_turnover if not _nan(a)]
        at_trend = "stable"
        if len(valid_at) >= 3:
            if valid_at[0] > valid_at[-1] + 0.1:
                at_trend = "improving"
            elif valid_at[0] < valid_at[-1] - 0.1:
                at_trend = "deteriorating"

        self.results["capex_cycle"] = {
            "capex_intensity_series": capex_intensity,
            "capex_latest_cr": to_cr(capex_latest),
            "maintenance_capex_cr": to_cr(dep_latest),
            "growth_capex_cr": to_cr(growth_capex),
            "growth_capex_pct": growth_capex_pct,
            "asset_turnover_series": asset_turnover,
            "asset_turnover_trend": at_trend,
        }

        if growth_capex_pct > 50:
            self.moat_signals.append("%.0f%% growth capex — investing for expansion" % growth_capex_pct)
        if at_trend == "improving":
            self.moat_signals.append("Asset turnover improving")
        elif at_trend == "deteriorating":
            self.risk_factors.append("Asset turnover declining")

        print("    Capex Intensity: %.1f%% | Growth Capex: %.0f%% | AT Trend: %s" % (
            capex_intensity[0] if capex_intensity else 0, growth_capex_pct, at_trend))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Tax Sustainability Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def tax_sustainability_analysis(self):
        """Effective vs statutory tax rate, deferred tax trends."""
        print("  Tax Sustainability Analysis...")

        years = min(self.d.years, 5)
        effective_tax_series = []
        statutory_rate = 0.252

        for col in range(years):
            tax_exp = self._i("tax", col)
            pretax = self._i("pretax_income", col)
            eff_rate = safe_div(tax_exp, pretax) if not (_nan(tax_exp) or _nan(pretax) or pretax <= 0) else float("nan")
            effective_tax_series.append(eff_rate)

        valid_rates = [r for r in effective_tax_series if not _nan(r) and 0 < r < 0.5]
        avg_effective_rate = sum(valid_rates) / len(valid_rates) if valid_rates else 0
        tax_gap = (statutory_rate - avg_effective_rate) * 100

        self.results["tax_sustainability"] = {
            "effective_tax_series": [r * 100 if not _nan(r) else 0 for r in effective_tax_series],
            "avg_effective_rate_pct": avg_effective_rate * 100,
            "statutory_rate_pct": statutory_rate * 100,
            "tax_gap_pct": tax_gap,
            "has_tax_benefit": tax_gap > 5,
        }

        if tax_gap > 5:
            self.risk_factors.append("Effective tax %.1f%% vs statutory %.1f%% — tax benefit dependency" % (
                avg_effective_rate * 100, statutory_rate * 100))

        print("    Effective Tax: %.1f%% | Statutory: %.1f%% | Gap: %.1f%%" % (
            avg_effective_rate * 100, statutory_rate * 100, tax_gap))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Institutional Holding Analysis
    # ─────────────────────────────────────────────────────────────────────────
    def institutional_holding_analysis(self):
        """Analyze MF/institutional ownership structure."""
        print("  Institutional Holding Analysis...")

        mf_data = self.d.mf_institutional_data
        if not mf_data:
            self.results["institutional_holdings"] = {"status": "no_data"}
            return

        inst_holders = mf_data.get("institutional_holders", [])
        mf_holders = mf_data.get("mf_holders", [])

        total_inst_pct = sum(h.get("pct_out", 0) for h in inst_holders)
        total_mf_pct = sum(h.get("pct_out", 0) for h in mf_holders)
        top_5_inst = sorted(inst_holders, key=lambda x: x.get("pct_out", 0), reverse=True)[:5]
        top_5_mf = sorted(mf_holders, key=lambda x: x.get("pct_out", 0), reverse=True)[:5]

        self.results["institutional_holdings"] = {
            "status": "computed",
            "total_institutional_pct": total_inst_pct,
            "total_mf_pct": total_mf_pct,
            "n_institutional_holders": len(inst_holders),
            "n_mf_holders": len(mf_holders),
            "top_5_institutional": top_5_inst,
            "top_5_mf": top_5_mf,
        }

        if total_inst_pct > 30:
            self.moat_signals.append("Strong institutional ownership (%.1f%%)" % total_inst_pct)
        if len(mf_holders) > 10:
            self.moat_signals.append("%d mutual funds holding" % len(mf_holders))

        print("    Institutional: %.1f%% (%d holders) | MF: %.1f%% (%d funds)" % (
            total_inst_pct, len(inst_holders), total_mf_pct, len(mf_holders)))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Corporate Actions History
    # ─────────────────────────────────────────────────────────────────────────
    def corporate_actions_analysis(self):
        """Analyze dividend consistency, capital return track record."""
        print("  Corporate Actions Analysis...")

        ca = self.d.corporate_actions
        if not ca:
            self.results["corporate_actions"] = {"status": "no_data"}
            return

        dividends = ca.get("dividends", [])
        splits = ca.get("splits", [])

        years_with_div = set()
        for d in dividends:
            years_with_div.add(d["date"][:4])

        total_div_amount = sum(d["amount"] for d in dividends)
        avg_div = total_div_amount / len(dividends) if dividends else 0
        div_growth = 0
        if len(dividends) >= 4:
            recent_divs = sum(d["amount"] for d in dividends[:2])
            older_divs = sum(d["amount"] for d in dividends[-2:])
            if older_divs > 0:
                div_growth = (recent_divs / older_divs - 1) * 100

        self.results["corporate_actions"] = {
            "status": "computed",
            "dividend_count": len(dividends),
            "years_with_dividend": len(years_with_div),
            "total_dividend_per_share": total_div_amount,
            "avg_dividend_per_share": avg_div,
            "dividend_growth_pct": div_growth,
            "splits": splits,
            "recent_dividends": dividends[:5],
        }

        if len(years_with_div) >= 5:
            self.moat_signals.append("Consistent dividend payer (%d years)" % len(years_with_div))
        if div_growth > 20:
            self.moat_signals.append("Dividend growing at %.0f%%" % div_growth)

        print("    Dividends: %d payments over %d years | Growth: %.0f%%" % (
            len(dividends), len(years_with_div), div_growth))

    # ─────────────────────────────────────────────────────────────────────────
    # EXTENDED: Credit Rating Intelligence
    # ─────────────────────────────────────────────────────────────────────────
    def credit_rating_intelligence(self):
        """Multi-agency credit rating trajectory analysis."""
        print("  Credit Rating Intelligence...")

        cr = self.d.credit_ratings
        if not cr:
            self.results["credit_intelligence"] = {"status": "no_ratings"}
            return

        RATING_HIERARCHY = {
            "AAA": 10, "AA+": 9, "AA": 8, "AA-": 7,
            "A+": 6, "A": 5, "A-": 4, "A1+": 6, "A1": 5,
            "BBB+": 3, "BBB": 2, "BBB-": 1,
            "BB+": 0, "BB": -1, "B+": -2, "B": -3,
        }

        all_ratings = []
        agencies = {}
        for filing in cr:
            agency = filing.get("agency", "Unknown")
            ratings = filing.get("ratings", [])
            date_str = filing.get("date", "")
            outlook = filing.get("outlook", "")
            if agency not in agencies:
                agencies[agency] = {"latest_ratings": ratings, "outlook": outlook, "date": date_str}
            for r in ratings:
                score = RATING_HIERARCHY.get(r.upper(), -5)
                all_ratings.append({"date": date_str, "agency": agency, "rating": r, "score": score})

        latest_score = all_ratings[0]["score"] if all_ratings else 0
        oldest_score = all_ratings[-1]["score"] if all_ratings else 0
        trajectory = "STABLE"
        if latest_score > oldest_score:
            trajectory = "UPGRADED"
        elif latest_score < oldest_score:
            trajectory = "DOWNGRADED"

        is_ig = latest_score >= 2

        self.results["credit_intelligence"] = {
            "status": "computed",
            "agencies": agencies,
            "n_agencies": len(agencies),
            "latest_score": latest_score,
            "trajectory": trajectory,
            "is_investment_grade": is_ig,
            "all_ratings": all_ratings[:10],
        }

        if trajectory == "UPGRADED":
            self.moat_signals.append("Credit rating upgraded")
        elif trajectory == "DOWNGRADED":
            self.risk_factors.append("Credit rating downgraded")
        if is_ig:
            self.moat_signals.append("Investment grade from %d agencies" % len(agencies))
        elif latest_score < 0:
            self.risk_factors.append("Sub-investment grade (junk) rating")

        print("    Agencies: %d | Trajectory: %s | Inv. Grade: %s" % (len(agencies), trajectory, is_ig))

    # ─────────────────────────────────────────────────────────────────────────
    # ORDER BOOK ANALYSIS (YoY / QoQ)
    # ─────────────────────────────────────────────────────────────────────────
    def order_book_analysis(self):
        """Analyse order-book history extracted from NSE filings.
        Computes YoY and QoQ changes for both order-book position and
        order inflows (when available).
        """
        print("  Order Book Analysis...")
        ob = self.d.order_book_history
        if not ob:
            self.results["order_book"] = {"status": "no_data"}
            return

        book_entries = [e for e in ob if e.get("type") == "book"]
        inflow_entries = [e for e in ob if e.get("type") == "inflow"]

        def _parse_date(d):
            try:
                return datetime.datetime.strptime(d.strip(), "%d-%b-%Y")
            except Exception:
                return None

        def _compute_changes(entries):
            """Given a newest-first list, compute YoY & QoQ where possible."""
            if not entries:
                return {}
            latest = entries[0]
            latest_dt = _parse_date(latest.get("date", ""))
            latest_val = latest["value_crore"]

            result = {
                "latest_value": latest_val,
                "latest_date": latest.get("date", ""),
                "n_datapoints": len(entries),
                "entries": entries[:8],
            }

            if len(entries) < 2:
                return result

            qoq_pct = None
            yoy_pct = None
            prev_entry = None
            yoy_entry = None

            for e in entries[1:]:
                e_dt = _parse_date(e.get("date", ""))
                if not e_dt or not latest_dt:
                    continue
                diff_days = (latest_dt - e_dt).days
                # QoQ: 60-150 days apart
                if 60 <= diff_days <= 150 and qoq_pct is None:
                    if e["value_crore"] > 0:
                        qoq_pct = (latest_val / e["value_crore"] - 1) * 100
                        prev_entry = e
                # YoY: 300-450 days apart
                if 300 <= diff_days <= 450 and yoy_pct is None:
                    if e["value_crore"] > 0:
                        yoy_pct = (latest_val / e["value_crore"] - 1) * 100
                        yoy_entry = e

            result["qoq_pct"] = qoq_pct
            result["qoq_prev"] = prev_entry
            result["yoy_pct"] = yoy_pct
            result["yoy_prev"] = yoy_entry
            return result

        result = {"status": "computed"}
        if book_entries:
            result["book"] = _compute_changes(book_entries)
        if inflow_entries:
            result["inflow"] = _compute_changes(inflow_entries)

        # Generate signals
        for key, label in [("book", "Order Book"), ("inflow", "Order Inflow")]:
            info = result.get(key, {})
            yoy = info.get("yoy_pct")
            if yoy is not None:
                if yoy > 15:
                    self.moat_signals.append("%s growing %+.0f%% YoY" % (label, yoy))
                elif yoy < -10:
                    self.risk_factors.append("%s declining %+.0f%% YoY" % (label, yoy))

        self.results["order_book"] = result

        # Print summary
        bk = result.get("book", {})
        if bk:
            parts = ["Book: ₹%.0f Cr" % bk.get("latest_value", 0)]
            if bk.get("yoy_pct") is not None:
                parts.append("YoY %+.1f%%" % bk["yoy_pct"])
            if bk.get("qoq_pct") is not None:
                parts.append("QoQ %+.1f%%" % bk["qoq_pct"])
            print("    %s" % " | ".join(parts))
        inf = result.get("inflow", {})
        if inf:
            parts = ["Inflow: ₹%.0f Cr" % inf.get("latest_value", 0)]
            if inf.get("yoy_pct") is not None:
                parts.append("YoY %+.1f%%" % inf["yoy_pct"])
            print("    %s" % " | ".join(parts))
        if not bk and not inf:
            print("    No order book data available")

    # ─────────────────────────────────────────────────────────────────────────
    # RUN ALL DEEP ANALYSES
    # ─────────────────────────────────────────────────────────────────────────
    def run_all(self, documents_dir=None):
        """Execute all deep fundamental analysis modules."""
        print("\n[DEEP] Running Deep Fundamental Analysis...")

        # Load documents if directory provided
        if documents_dir:
            self.load_documents_from_directory(documents_dir)

        # Quantitative analysis
        self.dcf_valuation()
        self.reverse_dcf()
        self.valuation_bands()
        self.capital_allocation_analysis()
        self.revenue_margin_analysis()
        self.working_capital_efficiency()
        self.earnings_quality_analysis()
        self.debt_stress_test()
        self.shareholder_returns_analysis()
        self.quarterly_momentum()

        # Extended quantitative analysis
        self.shareholding_trend_analysis()
        self.insider_trading_analysis()
        self.peer_comparison()
        self.relative_strength_analysis()
        self.technical_structure()
        self.graham_magic_formula()
        self.capex_cycle_analysis()
        self.tax_sustainability_analysis()
        self.institutional_holding_analysis()
        self.corporate_actions_analysis()
        self.credit_rating_intelligence()
        self.order_book_analysis()

        # Qualitative / NLP analysis
        self.analyze_concall_transcripts()
        self.analyze_annual_reports()

        # Composite scoring
        self.moat_scoring()
        self.risk_assessment()
        self.compute_deep_score()

        return self.results


# ══════════════════════════════════════════════════════════════════════════════
# PDF REPORT GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

# Colour constants (R, G, B)
C_BLUE   = (41, 128, 185)
C_DARK   = (44, 62, 80)
C_RED    = (192, 57, 43)
C_GREEN  = (39, 174, 96)
C_YELLOW = (243, 156, 18)
C_GRAY   = (149, 165, 166)
C_LIGHT  = (245, 245, 245)
C_WHITE  = (255, 255, 255)


def _latin(text):
    """Sanitise text for PDF (latin-1 safe)."""
    if not isinstance(text, str):
        text = str(text)
    return text.encode("latin-1", "replace").decode("latin-1")


class ForensicReport(FPDF):
    """Generate the forensic accounting PDF report."""

    # Font paths (Arial as Calibri substitute — metrically similar)
    _FONT_DIR = "/System/Library/Fonts/Supplemental"

    def __init__(self, company, data, analyzer):
        super().__init__()
        self.company = company
        self.data = data
        self.analyzer = analyzer
        self.results = analyzer.results
        self.info = data.info
        self.set_auto_page_break(auto=True, margin=15)
        self._w = 190  # effective page width (A4 - margins)
        self._last_section_title = ""  # track section name for end separator
        self._section_num = 0  # auto-increment section numbering
        self._subsection_num = 0  # resets per section

        # Register Calibri font family (using Arial TTF as metric-compatible substitute)
        self.add_font("Calibri", "", os.path.join(self._FONT_DIR, "Arial.ttf"))
        self.add_font("Calibri", "B", os.path.join(self._FONT_DIR, "Arial Bold.ttf"))
        self.add_font("Calibri", "I", os.path.join(self._FONT_DIR, "Arial Italic.ttf"))
        self.add_font("Calibri", "BI", os.path.join(self._FONT_DIR, "Arial Bold Italic.ttf"))

    def header(self):
        if self.page_no() > 1:
            self.set_font("Calibri", "I", 8)
            self.set_text_color(*C_GRAY)
            name = self.info.get("shortName", self.company)
            self.cell(0, 5, _latin("Fundamental & Forensic Analysis: %s" % name), align="R", new_x="LMARGIN", new_y="NEXT")
            self.line(10, self.get_y(), 200, self.get_y())
            self.ln(3)

    def footer(self):
        self.set_y(-15)
        self.set_font("Calibri", "I", 7)
        self.set_text_color(*C_GRAY)
        self.cell(0, 5, _latin("Generated on %s | Data sourced from BSE/NSE via yfinance | For research purposes only" %
                                datetime.datetime.now().strftime("%d-%b-%Y %H:%M")),
                  align="L")
        self.cell(0, 5, _latin("Page %d" % self.page_no()), align="R")

    def _section_end(self):
        """Draw a prominent separator marking the end of the previous section."""
        if self._last_section_title:
            self.ln(4)
            y = self.get_y()
            # Bold dark separator line
            self.set_fill_color(44, 62, 80)
            self.rect(10, y, 190, 1.2, 'F')
            self.set_text_color(0, 0, 0)
            self.ln(8)

    def _section(self, title):
        """Add a numbered section header with clear visual hierarchy.
        Each section starts on a new page with a prominent header bar.
        Also registers the section in the PDF outline / TOC."""
        # Increment section number, reset subsection counter
        self._section_num += 1
        self._subsection_num = 0
        self._last_section_title = title
        # Start a new page for each major section (skip if already at top of page)
        if self.get_y() > 25:
            self.add_page()
        # Register with fpdf2's outline (powers both TOC and PDF bookmarks)
        numbered_title = "%d. %s" % (self._section_num, title)
        try:
            self.start_section(_latin(numbered_title), level=0)
        except Exception:
            pass
        # Dark navy full-width header bar with number
        self.set_fill_color(*C_DARK)
        self.set_text_color(*C_WHITE)
        self.set_font("Calibri", "B", 11)
        self.cell(0, 5.5, _latin("  %d.  %s" % (self._section_num, title)),
                  fill=True, new_x="LMARGIN", new_y="NEXT")
        # Accent underline
        self.set_fill_color(*C_BLUE)
        self.rect(10, self.get_y(), 190, 0.5, 'F')
        self.ln(3)
        self.set_text_color(0, 0, 0)

    def _render_toc(self, pdf, outline):
        """Render the Table of Contents using fpdf2's outline list.
        Each entry is auto-linked to its target page by fpdf2."""
        toc_pages_reserved = getattr(self, "_toc_pages_reserved", 2)
        start_page = pdf.page_no()
        # Disable auto page break — we'll handle manually
        pdf.set_auto_page_break(auto=False)
        max_y = 287  # hard limit before footer area

        # Title bar
        pdf.set_font("Calibri", "B", 11)
        pdf.set_fill_color(*C_DARK)
        pdf.set_text_color(*C_WHITE)
        pdf.cell(0, 5.5, _latin("  TABLE OF CONTENTS"), fill=True, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        pdf.set_text_color(0, 0, 0)

        # Hierarchical entries with numbering — compact to fit in reserved pages
        for i, entry in enumerate(outline):
            level = getattr(entry, "level", 0)
            name = getattr(entry, "name", "")
            page = getattr(entry, "page_number", 1)

            # Indentation and sizing based on level
            if level == 0:
                indent = 12
                pdf.set_font("Calibri", "B", 8.5)
                row_h = 5
            else:
                indent = 20
                pdf.set_font("Calibri", "", 7.5)
                row_h = 4.2

            # Manual page break if near bottom
            if pdf.get_y() + row_h > max_y:
                pdf.add_page()

            # Light background for main sections
            if level == 0:
                pdf.set_fill_color(240, 243, 247)
                pdf.rect(10, pdf.get_y(), 190, row_h, 'F')

            link = pdf.add_link()
            pdf.set_link(link, page=page)

            pdf.set_x(indent)
            title_w = 165 - indent
            pdf.set_text_color(*C_DARK)
            pdf.cell(title_w, row_h, _latin(name), border=0, link=link, align="L")
            pdf.set_font("Calibri", "B" if level == 0 else "", 8.5 if level == 0 else 7.5)
            pdf.set_text_color(*C_BLUE if level == 0 else (120, 120, 120))
            pdf.cell(15, row_h, str(page), border=0, link=link, align="R",
                     new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)

        # Restore auto page break
        pdf.set_auto_page_break(auto=True, margin=15)

        # Pad with blank pages so TOC spans exactly the reserved page count
        used = pdf.page_no() - start_page + 1
        while used < toc_pages_reserved:
            pdf.add_page()
            used += 1

    def _subsection(self, title):
        # Ensure space for subsection header
        remaining = self.h - self.get_y() - self.b_margin
        if remaining < 30:
            self.add_page()
        self._subsection_num += 1
        sub_num = "%d.%d" % (self._section_num, self._subsection_num)
        # Register subsection in TOC
        try:
            self.start_section(_latin("%s %s" % (sub_num, title)), level=1)
        except Exception:
            pass
        self.ln(2)
        y = self.get_y()
        # Light background strip for subsection
        self.set_fill_color(235, 240, 248)
        self.rect(10, y, 190, 5.5, 'F')
        # Blue left accent bar
        self.set_fill_color(*C_BLUE)
        self.rect(10, y, 3, 5.5, 'F')
        # Numbered subsection title
        self.set_xy(15, y + 0.2)
        self.set_font("Calibri", "B", 9)
        self.set_text_color(41, 128, 185)
        self.cell(12, 5, _latin(sub_num), align="L")
        self.set_text_color(*C_DARK)
        self.cell(0, 5, _latin(title), new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        self.set_text_color(0, 0, 0)

    def _intro(self, text):
        """Plain-English one-paragraph explainer placed under a section heading
        so a non-finance reader can grasp what the section is about."""
        # Light background box for the intro text
        self.set_fill_color(245, 248, 250)
        self.set_font("Calibri", "I", 8)
        self.set_text_color(80, 90, 100)
        x = self.get_x()
        y = self.get_y()
        self.set_x(12)
        self.multi_cell(186, 4, _latin(text), new_x="LMARGIN", new_y="NEXT", fill=True)
        self.set_text_color(0, 0, 0)
        self.ln(3)

    def _table(self, headers, rows, col_widths=None, align=None):
        """Render a data table with clean modern styling."""
        if col_widths is None:
            col_widths = [self._w / len(headers)] * len(headers)
        if align is None:
            align = ["C"] * len(headers)

        # Header row - dark background, white text
        self.set_font("Calibri", "B", 8)
        self.set_fill_color(52, 73, 94)
        self.set_text_color(*C_WHITE)
        self.set_draw_color(52, 73, 94)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 5.5, _latin(h), border=1, fill=True, align="C")
        self.ln()

        # Data rows - alternating with subtle borders
        self.set_font("Calibri", "", 8)
        self.set_text_color(30, 30, 30)
        self.set_draw_color(200, 200, 200)
        for r, row in enumerate(rows):
            fill = r % 2 == 0
            if fill:
                self.set_fill_color(248, 249, 250)
            else:
                self.set_fill_color(255, 255, 255)
            for i, val in enumerate(row):
                self.cell(col_widths[i], 6, _latin(str(val)), border="LR" if r < len(rows)-1 else 1,
                          fill=True, align=align[i])
            self.ln()
        # Bottom border
        self.set_draw_color(52, 73, 94)
        self.line(self.l_margin, self.get_y(), self.l_margin + sum(col_widths), self.get_y())
        self.set_draw_color(0, 0, 0)
        self.set_text_color(0, 0, 0)
        self.ln(3)

    def _metric(self, label, value, color=None):
        """Single metric row."""
        self.set_font("Calibri", "", 9)
        self.set_text_color(0, 0, 0)
        self.cell(80, 6, _latin(label))
        if color:
            self.set_text_color(*color)
        self.set_font("Calibri", "B", 9)
        self.cell(0, 6, _latin(str(value)), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def _score_box(self, label, score_text, color):
        """Coloured score box — full width, modern pill-style design."""
        self.set_fill_color(*color)
        self.set_text_color(*C_WHITE)
        self.set_font("Calibri", "B", 9)
        # Full-width bar
        y = self.get_y()
        self.rect(10, y, 190, 5.5, 'F')
        self.set_xy(12, y + 0.2)
        self.cell(95, 5, _latin(label), align="L")
        self.set_font("Calibri", "B", 10)
        self.cell(83, 5, _latin(score_text), align="R")
        self.set_xy(10, y + 5.5)
        self.set_text_color(0, 0, 0)
        self.ln(2)

    def _flag(self, text, is_red=True):
        """Add a red or green flag item."""
        if is_red:
            self.set_text_color(*C_RED)
            prefix = "[!] "
        else:
            self.set_text_color(*C_GREEN)
            prefix = "[+] "
        self.set_font("Calibri", "", 9)
        self.multi_cell(0, 5, _latin(prefix + text), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def _check_page_break(self, min_space=45):
        """Add a new page only if less than min_space mm remains."""
        remaining = self.h - self.get_y() - self.b_margin
        if remaining < min_space:
            self.add_page()
        else:
            self.ln(2)

    # ── PAGE BUILDERS ────────────────────────────────────────────────────────

    def add_cover_page(self):
        """Title page."""
        self.add_page()
        self.ln(40)
        self.set_font("Calibri", "B", 24)
        self.set_text_color(*C_DARK)
        self.cell(0, 15, "Fundamental & Forensic Analysis Report", align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(10)

        # Company name
        name = self.info.get("longName", self.info.get("shortName", self.company))
        self.set_font("Calibri", "B", 20)
        self.set_text_color(*C_BLUE)
        self.cell(0, 12, _latin(name), align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Calibri", "", 14)
        self.set_text_color(*C_GRAY)
        self.cell(0, 8, _latin("NSE: %s" % self.company), align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(10)

        # Key info
        self.set_text_color(*C_DARK)
        self.set_font("Calibri", "", 11)
        lines = [
            "Sector: %s" % self.info.get("sectorDisp", "N/A"),
            "Industry: %s" % self.info.get("industryDisp", "N/A"),
            "Market Cap: Rs. {:,.0f} Cr".format(self.info.get("marketCap", 0) / 1e7) if self.info.get("marketCap") else "Market Cap: N/A",
            "Report Date: %s" % datetime.datetime.now().strftime("%d %B %Y"),
            "Data Period: %s" % (", ".join(self.data.fy_labels) if self.data.fy_labels else "N/A"),
        ]
        for line in lines:
            self.cell(0, 7, _latin(line), align="C", new_x="LMARGIN", new_y="NEXT")

        # Overall score preview
        overall = self.results.get("overall", {})
        score = overall.get("final_score", 0)
        rec = overall.get("recommendation", "N/A")

        self.ln(15)
        color = C_GREEN if score >= 60 else C_YELLOW if score >= 45 else C_RED
        self.set_fill_color(*color)
        self.set_text_color(*C_WHITE)
        self.set_font("Calibri", "B", 10)
        self.cell(0, 5.5, _latin("  Overall Score: %.0f / 100   |   Recommendation: %s  " % (score, rec)),
                  fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        self.ln(15)
        self.set_font("Calibri", "I", 8)
        self.set_text_color(*C_GRAY)
        self.cell(0, 5, "DISCLAIMER: This report is for educational/research purposes only. Not financial advice.",
                  align="C", new_x="LMARGIN", new_y="NEXT")
        self.cell(0, 5, "Always consult a qualified financial advisor before making investment decisions.",
                  align="C", new_x="LMARGIN", new_y="NEXT")

    def add_key_red_flags_summary(self):
        """Prominent red flags at the top for quick understanding."""
        critical = [(f, s) for f, s in self.analyzer.red_flags if s == "critical"]
        major = [(f, s) for f, s in self.analyzer.red_flags if s == "major"]
        important = critical + major
        if not important:
            return

        # Only add page if we're not already at the top of a fresh page
        if self.get_y() > 30:
            self.add_page()
        self.set_fill_color(*C_RED)
        self.set_text_color(*C_WHITE)
        self.set_font("Calibri", "B", 9)
        self.cell(0, 5.5, _latin("  KEY RED FLAGS — READ FIRST"), fill=True,
                  new_x="LMARGIN", new_y="NEXT")
        self.ln(3)
        self.set_text_color(0, 0, 0)

        self.set_font("Calibri", "", 9)
        self.multi_cell(0, 5, _latin(
            "The following critical and major red flags were detected. "
            "These are the most important concerns an investor should evaluate "
            "before proceeding."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        for flag_text, severity in critical:
            self.set_text_color(*C_RED)
            self.set_font("Calibri", "B", 10)
            self.multi_cell(0, 6, _latin("[CRITICAL] " + flag_text),
                            new_x="LMARGIN", new_y="NEXT")
        for flag_text, severity in major:
            self.set_text_color(200, 80, 0)
            self.set_font("Calibri", "B", 9)
            self.multi_cell(0, 5, _latin("[MAJOR] " + flag_text),
                            new_x="LMARGIN", new_y="NEXT")

        self.set_text_color(0, 0, 0)
        self.ln(3)
        self.set_font("Calibri", "I", 8)
        self.set_text_color(*C_GRAY)
        self.cell(0, 5, _latin("Detailed analysis of each concern follows in subsequent sections."),
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def add_executive_summary(self):
        """Comprehensive executive summary — complete A-Z snapshot of the company.
        A reader should get full understanding without reading the rest."""
        self._section("EXECUTIVE SUMMARY")

        overall = self.results.get("overall", {})
        info = self.info
        score = overall.get("final_score", 0)
        rec = overall.get("recommendation", "N/A")

        # ── 1. Verdict Banner ──
        color = C_GREEN if score >= 60 else C_YELLOW if score >= 45 else C_RED
        self._score_box("OVERALL FORENSIC SCORE", "%.0f / 100  |  %s" % (score, rec), color)
        self.ln(2)

        # ── 2. Company Snapshot ──
        self._subsection("Company Snapshot")
        name = info.get("longName", info.get("shortName", self.company))
        sector = info.get("sectorDisp", "N/A")
        industry = info.get("industryDisp", "N/A")
        mcap = info.get("marketCap", 0)
        mcap_str = "Rs. {:,.0f} Cr".format(mcap / 1e7) if mcap else "N/A"
        cmp = info.get("currentPrice", 0)
        pe = info.get("trailingPE", 0)
        pb = info.get("priceToBook", 0)
        ev_ebitda = info.get("enterpriseToEbitda", 0)
        hi52 = info.get("fiftyTwoWeekHigh", 0)
        lo52 = info.get("fiftyTwoWeekLow", 0)
        self.set_font("Calibri", "", 9)
        snap = "%s | %s | %s | MCap: %s | CMP: Rs.%.0f | PE: %.1f | PB: %.1f | EV/EBITDA: %.1f" % (
            name, sector, industry, mcap_str, cmp, pe or 0, pb or 0, ev_ebitda or 0)
        self.multi_cell(0, 5, _latin(snap), new_x="LMARGIN", new_y="NEXT")
        if hi52 and lo52 and cmp:
            pct_from_hi = ((cmp - hi52) / hi52) * 100 if hi52 else 0
            self.set_font("Calibri", "", 8)
            self.cell(0, 4, _latin("52W Range: Rs.%.0f - Rs.%.0f | CMP is %.0f%% from 52W High" % (
                lo52, hi52, pct_from_hi)), new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        # ── 3. Financial Health At-a-Glance ──
        self._subsection("Financial Health At-a-Glance")
        b = self.results.get("beneish", {})
        a = self.results.get("altman", {})
        p = self.results.get("piotroski", {})
        cf = self.results.get("cashflow", {})
        dt = self.results.get("debt", {})
        sp = self.results.get("springate", {})
        oh = self.results.get("ohlson", {})
        mc = self.results.get("montier", {})
        bf = self.results.get("benford", {})

        def _color_for(score_10):
            if score_10 >= 7: return C_GREEN
            if score_10 >= 4: return C_YELLOW
            return C_RED

        # Manipulation checks
        if b:
            self._score_box("Beneish M-Score (Earnings Manipulation)", "%.2f  %s" % (
                b["m_score"], b["verdict"]), _color_for(b["score_10"]))
        if mc:
            self._score_box("Montier C-Score (Creative Accounting)", "%d/6  %s" % (
                mc["c_score"], mc["verdict"]), _color_for(mc["score_10"]))
        if bf and bf.get("available"):
            self._score_box("Benford's Law (Number Integrity)",
                            bf["conformity"],
                            C_GREEN if bf["conformity"] == "PASS" else
                            C_YELLOW if bf["conformity"] == "MARGINAL" else C_RED)
        # Bankruptcy checks
        if a:
            self._score_box("Altman Z-Score (Bankruptcy Risk)", "%.2f  %s" % (
                a["z_score"], a["zone"]), _color_for(a["score_10"]))
        if sp:
            self._score_box("Springate S-Score (Distress)", "%.2f  %s" % (
                sp["s_score"], sp["verdict"]), _color_for(sp["score_10"]))
        if oh:
            self._score_box("Ohlson O-Score (Failure Probability)", "%.0f%%  %s" % (
                oh["probability"] * 100, oh["verdict"]), _color_for(oh["score_10"]))
        # Quality checks
        if p:
            self._score_box("Piotroski F-Score (Financial Strength)", "%d / 9  %s" % (
                p["f_score"], p["verdict"]), _color_for(p["score_10"]))
        if cf:
            self._score_box("Cash Flow Quality", "Score: %d/10" % cf["score_10"],
                            _color_for(cf["score_10"]))
        if dt:
            self._score_box("Debt Sustainability", "Score: %d/10" % dt["score_10"],
                            _color_for(dt["score_10"]))

        # ── 4. Key Financials (most recent year) ──
        self._subsection("Key Financial Metrics (Latest Year)")
        prof = self.results.get("profitability", [])
        if prof:
            latest = prof[0]
            self._metric("Revenue", "Rs. {:,.0f} Cr".format(latest.get("revenue", 0) / 1e7) if latest.get("revenue") else "N/A")
            self._metric("Net Income", "Rs. {:,.0f} Cr".format(latest.get("net_income", 0) / 1e7) if latest.get("net_income") else "N/A")
            self._metric("Operating Margin", "%.1f%%" % (latest.get("opr_margin", 0) * 100) if latest.get("opr_margin") else "N/A")
            self._metric("Net Margin", "%.1f%%" % (latest.get("net_margin", 0) * 100) if latest.get("net_margin") else "N/A")
        debt_data = self.results.get("debt_details", [])
        if debt_data:
            d0 = debt_data[0]
            self._metric("Debt/Equity", "%.2f" % d0.get("de_ratio", 0) if d0.get("de_ratio") is not None else "N/A")
            self._metric("Interest Coverage", "%.1fx" % d0.get("int_cov", 0) if d0.get("int_cov") else "N/A")

        # ── 5. Promoter & Governance ──
        self._subsection("Promoter & Governance")
        promo = self.results.get("promoter_holding", {})
        if promo:
            self._metric("Promoter Holding", "%.1f%%" % promo.get("latest_pct", 0))
            chg = promo.get("change_1y", 0)
            self._metric("Promoter Change (1Y)", "%+.1f%%" % chg,
                         C_RED if chg < -2 else C_GREEN if chg > 0 else C_DARK)
            pledge = promo.get("pledge_pct", 0)
            self._metric("Shares Pledged", "%.1f%%" % pledge,
                         C_RED if pledge > 20 else C_GREEN if pledge == 0 else C_YELLOW)
        esm = self.results.get("esm", {})
        if esm.get("in_esm"):
            self._metric("ESM Status", "IN ESM — %s" % esm.get("stage", ""), C_RED)
        else:
            self._metric("ESM Status", "Not in ESM (Normal)", C_GREEN)

        # ── 6. Deep Analysis Summary (if available) ──
        if hasattr(self, "deep_analyzer") and self.deep_analyzer:
            da = self.deep_analyzer.results
            self._subsection("Deep Analysis Highlights")
            # Moat
            moat = da.get("moat_score", {})
            if moat:
                self._metric("Competitive Moat", "%d/10 — %s" % (
                    moat.get("total_score", 0), moat.get("verdict", "")),
                    C_GREEN if moat.get("total_score", 0) >= 6 else C_YELLOW if moat.get("total_score", 0) >= 4 else C_RED)
            # Risk
            risk = da.get("risk_assessment", {})
            if risk:
                self._metric("Risk Score", "%.1f/10 — %s" % (
                    risk.get("risk_score", 0), risk.get("risk_level", "")),
                    C_GREEN if risk.get("risk_score", 0) <= 3 else C_RED if risk.get("risk_score", 0) >= 7 else C_YELLOW)
            # Deep Fundamental Score
            ds = da.get("deep_score", {})
            if ds:
                self._metric("Deep Fundamental Score", "%.0f/100 — %s" % (
                    ds.get("total_score", 0), ds.get("recommendation", "")),
                    C_GREEN if ds.get("total_score", 0) >= 60 else C_RED if ds.get("total_score", 0) < 40 else C_YELLOW)
            # Technical
            tech = da.get("technical", {})
            if tech:
                self._metric("Trend & Technical", "%s | RSI: %.0f | Delivery: %.0f%%" % (
                    tech.get("trend", "N/A"), tech.get("rsi", 0), tech.get("delivery_pct", 0) * 100 if tech.get("delivery_pct", 0) < 1 else tech.get("delivery_pct", 0)))
            # Order Book
            ob = da.get("order_book", {})
            if ob and ob.get("order_book_cr"):
                yoy = ob.get("yoy_growth_pct", 0)
                self._metric("Order Book", "Rs.%.0f Cr | YoY %+.0f%%" % (
                    ob.get("order_book_cr", 0), yoy),
                    C_GREEN if yoy > 20 else C_RED if yoy < -10 else C_YELLOW)

        # ── 7. Red & Green Flags Count ──
        self._subsection("Flags Summary")
        crit = sum(1 for f, s in self.analyzer.red_flags if s == "critical")
        major = sum(1 for f, s in self.analyzer.red_flags if s == "major")
        minor = sum(1 for f, s in self.analyzer.red_flags if s == "minor")
        self._metric("Critical Red Flags", "%d" % crit, C_RED if crit else C_GREEN)
        self._metric("Major Red Flags", "%d" % major, C_RED if major else C_GREEN)
        self._metric("Minor Red Flags", "%d" % minor, C_YELLOW if minor else C_GREEN)
        self._metric("Green Flags", "%d" % len(self.analyzer.green_flags), C_GREEN)

        # ── 8. Top Red Flags (quick view) ──
        if self.analyzer.red_flags:
            self._subsection("Top Concerns (Red Flags)")
            self.set_font("Calibri", "", 8)
            shown = 0
            for flag, severity in self.analyzer.red_flags:
                if shown >= 8:
                    break
                marker = "[CRITICAL]" if severity == "critical" else "[MAJOR]" if severity == "major" else "[MINOR]"
                col = C_RED if severity in ("critical", "major") else C_YELLOW
                self.set_text_color(*col)
                self.multi_cell(0, 4, _latin("%s %s" % (marker, flag)), new_x="LMARGIN", new_y="NEXT")
                shown += 1
            self.set_text_color(0, 0, 0)

    def add_company_overview(self):
        """Company overview section."""
        self._check_page_break(60)
        self._section("COMPANY OVERVIEW")

        info = self.info
        fields = [
            ("Company Name", info.get("longName", "N/A")),
            ("Symbol (NSE)", self.company),
            ("Sector", info.get("sectorDisp", "N/A")),
            ("Industry", info.get("industryDisp", "N/A")),
            ("Market Cap", "Rs. {:,.0f} Cr".format(info.get("marketCap", 0) / 1e7) if info.get("marketCap") else "N/A"),
            ("Enterprise Value", "Rs. {:,.0f} Cr".format(info.get("enterpriseValue", 0) / 1e7) if info.get("enterpriseValue") else "N/A"),
            ("Current Price", "Rs. {:,.2f}".format(info.get("currentPrice", 0)) if info.get("currentPrice") else "N/A"),
            ("52-Week High / Low", "Rs. {:,.0f} / Rs. {:,.0f}".format(
                info.get("fiftyTwoWeekHigh", 0), info.get("fiftyTwoWeekLow", 0))
                if info.get("fiftyTwoWeekHigh") else "N/A"),
            ("P/E (Trailing)", fmt_num(info.get("trailingPE", float("nan")))),
            ("P/E (Forward)", fmt_num(info.get("forwardPE", float("nan")))),
            ("P/B Ratio", fmt_num(info.get("priceToBook", float("nan")))),
            ("EV/EBITDA", fmt_num(info.get("enterpriseToEbitda", float("nan")))),
            ("Dividend Yield", fmt_pct(info.get("dividendYield", 0) * 100) if info.get("dividendYield") else "N/A"),
            ("Beta", fmt_num(info.get("beta", float("nan")))),
            ("Employees", "{:,}".format(info.get("fullTimeEmployees", 0)) if info.get("fullTimeEmployees") else "N/A"),
        ]
        for label, value in fields:
            self._metric(label, value)

        # Business summary
        summary = info.get("longBusinessSummary", "")
        if summary:
            self.ln(4)
            self._subsection("Business Description")
            self.set_font("Calibri", "", 8)
            self.multi_cell(0, 4, _latin(summary[:1500]), new_x="LMARGIN", new_y="NEXT")

    def add_financial_tables(self):
        """Annual & quarterly financial summary tables."""
        self._check_page_break(60)
        self._section("FINANCIAL SUMMARY (Annual)")

        # ── Annual P&L table ──
        prof = self.results.get("profitability", [])
        if prof:
            self._subsection("Profit & Loss Statement (Rs. Cr)")
            headers = ["Year", "Revenue", "Gross Profit", "EBITDA", "Net Income",
                       "Gross %", "Opr %", "Net %"]
            rows = []
            for r in prof:
                rows.append([
                    r["year"], fmt_cr(r["revenue"], 0), fmt_cr(r["gross_profit"], 0),
                    fmt_cr(r["ebitda"], 0), fmt_cr(r["net_income"], 0),
                    fmt_pct(r["gross_margin"]), fmt_pct(r["operating_margin"]),
                    fmt_pct(r["net_margin"]),
                ])
            w = [22, 25, 25, 25, 25, 20, 20, 20]
            self._table(headers, rows, w)

        # ── Annual Balance Sheet ──
        debt = self.results.get("debt", {}).get("rows", [])
        wc = self.results.get("working_capital", {}).get("rows", [])
        if debt:
            self._subsection("Balance Sheet Highlights (Rs. Cr)")
            headers = ["Year", "Tot Debt", "Equity", "Cash", "D/E", "Int Cov", "Curr Ratio"]
            rows = []
            for i, r in enumerate(debt):
                cr_val = wc[i]["current_ratio"] if i < len(wc) else float("nan")
                rows.append([
                    r["year"], fmt_cr(r["total_debt"], 0), fmt_cr(r["equity"], 0),
                    fmt_cr(r["cash"], 0), fmt_num(r["de_ratio"]),
                    fmt_num(r["interest_coverage"], 1),
                    fmt_num(cr_val),
                ])
            w = [22, 27, 27, 27, 22, 22, 25]
            self._table(headers, rows, w)

        # ── Cash Flow table ──
        cf_rows = self.results.get("cashflow", {}).get("rows", [])
        if cf_rows:
            self._subsection("Cash Flow Statement (Rs. Cr)")
            headers = ["Year", "Op. CF", "Capex", "Free CF", "CFO/NI", "Accrual"]
            rows = []
            for r in cf_rows:
                rows.append([
                    r["year"], fmt_cr(r["cfo"], 0), fmt_cr(r["capex"], 0),
                    fmt_cr(r["fcf"], 0), fmt_num(r["cfo_to_ni"]),
                    fmt_num(r["accrual_ratio"], 3),
                ])
            w = [22, 32, 32, 32, 28, 28]
            self._table(headers, rows, w)

        # ── Key Ratios ──
        ratios = self.results.get("ratios", [])
        if ratios:
            self._subsection("Key Ratios")
            headers = ["Year", "ROE %", "ROA %", "ROCE %", "EPS", "Tax Rate %"]
            rows = []
            for r in ratios:
                rows.append([
                    r["year"], fmt_pct(r["roe"]), fmt_pct(r["roa"]),
                    fmt_pct(r["roce"]), fmt_num(r["eps"]),
                    fmt_pct(r["tax_rate"]),
                ])
            w = [25, 30, 30, 30, 30, 30]
            self._table(headers, rows, w)

        # ── Quarterly table ──
        qt = self.results.get("quarterly", [])
        if qt:
            self._check_page_break(50)
            self._section("QUARTERLY TRENDS")
            headers = ["Quarter", "Revenue (Cr)", "Net Income (Cr)", "Net Margin %"]
            rows = []
            for r in qt:
                rows.append([
                    r["quarter"], fmt_cr(r["revenue"], 0),
                    fmt_cr(r["net_income"], 0), fmt_pct(r["net_margin"]),
                ])
            w = [35, 50, 50, 40]
            self._table(headers, rows, w)

    def add_forensic_scores(self):
        """Detailed forensic score breakdown."""
        self._check_page_break(60)
        self._section("FORENSIC SCORE ANALYSIS")

        # ── Beneish M-Score ──
        b = self.results.get("beneish", {})
        if b:
            self._subsection("Beneish M-Score (Earnings Manipulation Detector)")
            self.set_font("Calibri", "", 8)
            self.multi_cell(0, 4, _latin(
                "The Beneish M-Score uses 8 financial variables to detect whether a company "
                "is likely manipulating its reported earnings. A score > -1.78 suggests likely "
                "manipulation. Developed by Prof. Messod Beneish, it successfully flagged "
                "Enron's manipulation before its collapse."),
                new_x="LMARGIN", new_y="NEXT")
            self.ln(2)
            color = C_RED if b["is_red"] else C_GREEN
            self._metric("M-Score", "%.2f" % b["m_score"], color)
            self._metric("Verdict", b["verdict"], color)
            self.ln(2)

            headers = ["Variable", "Value", "What It Measures"]
            desc = {
                "DSRI": "Receivables vs Revenue growth (channel stuffing)",
                "GMI": "Gross Margin decline (pressure to manipulate)",
                "AQI": "Asset capitalisation changes",
                "SGI": "Sales growth (high growth = more temptation)",
                "DEPI": "Depreciation rate changes (inflating profits)",
                "SGAI": "SGA expense changes",
                "TATA": "Accruals vs Total Assets (earnings quality)",
                "LVGI": "Leverage changes",
            }
            rows = [[k, fmt_num(v, 3), desc.get(k, "")] for k, v in b["components"].items()]
            self._table(headers, rows, [25, 25, 130])

        # ── Altman Z-Score ──
        a = self.results.get("altman", {})
        if a:
            self._subsection("Altman Z-Score (Bankruptcy Risk Predictor)")
            self.set_font("Calibri", "", 8)
            self.multi_cell(0, 4, _latin(
                "The Altman Z-Score predicts the probability of a company going bankrupt "
                "within 2 years. Z > 2.99 = Safe zone, 1.81-2.99 = Grey zone (caution), "
                "< 1.81 = Distress zone (high bankruptcy risk). Developed by Prof. Edward Altman."),
                new_x="LMARGIN", new_y="NEXT")
            self.ln(2)
            zone_color = C_GREEN if a["z_score"] > 2.99 else C_YELLOW if a["z_score"] > 1.81 else C_RED
            self._metric("Z-Score", "%.2f" % a["z_score"], zone_color)
            self._metric("Zone", a["zone"], zone_color)
            self.ln(2)

            comp_desc = {
                "X1_WC_TA": "Working Capital / Total Assets (liquidity)",
                "X2_RE_TA": "Retained Earnings / Total Assets (profitability history)",
                "X3_EBIT_TA": "EBIT / Total Assets (operating efficiency)",
                "X4_MktCap_TL": "Market Cap / Total Liabilities (solvency)",
                "X5_Rev_TA": "Revenue / Total Assets (asset utilisation)",
            }
            headers = ["Component", "Value", "Interpretation"]
            rows = [[k, fmt_num(v, 3), comp_desc.get(k, "")] for k, v in a["components"].items()]
            self._table(headers, rows, [30, 22, 128])

        # ── Piotroski F-Score ──
        p = self.results.get("piotroski", {})
        if p:
            self._subsection("Piotroski F-Score (Financial Strength: 0-9)")
            self.set_font("Calibri", "", 8)
            self.multi_cell(0, 4, _latin(
                "The Piotroski F-Score rates financial strength from 0-9 based on 9 binary "
                "tests across profitability, leverage/liquidity, and operating efficiency. "
                "8-9 = Strong, 5-7 = Moderate, 0-4 = Weak. Used to identify value stocks."),
                new_x="LMARGIN", new_y="NEXT")
            self.ln(2)
            self._metric("F-Score", "%d / 9  (%s)" % (p["f_score"], p["verdict"]),
                         C_GREEN if p["f_score"] >= 7 else C_YELLOW if p["f_score"] >= 4 else C_RED)
            self.ln(2)

            headers = ["Test", "Pass/Fail"]
            rows = [[k, "PASS" if v else "FAIL"] for k, v in p["details"].items()]
            self._table(headers, rows, [100, 80])

    def add_dupont_section(self):
        """DuPont analysis page."""
        dp = self.results.get("dupont", [])
        if not dp:
            return
        self._subsection("DuPont ROE Decomposition")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "ROE = Net Margin x Asset Turnover x Equity Multiplier. This shows whether ROE "
            "is driven by profitability (good), efficiency (good), or excessive leverage (risky)."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        headers = ["Year", "ROE %", "Net Margin %", "Asset Turnover", "Equity Multiplier"]
        rows = [[r["year"], fmt_pct(r["roe"]), fmt_pct(r["net_margin"]),
                 fmt_num(r["asset_turnover"], 3), fmt_num(r["equity_multiplier"], 2)] for r in dp]
        self._table(headers, rows, [28, 35, 35, 40, 42])

    def add_working_capital_section(self):
        """Working capital & efficiency."""
        wc = self.results.get("working_capital", {}).get("rows", [])
        if not wc:
            return
        self._subsection("Working Capital & Efficiency (days)")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "DSO = Days Sales Outstanding (receivable collection speed). "
            "DIO = Days Inventory Outstanding. DPO = Days Payable Outstanding. "
            "CCC = Cash Conversion Cycle (DSO + DIO - DPO). Lower CCC = faster cash recovery."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        headers = ["Year", "DSO", "DIO", "DPO", "CCC", "Curr Ratio"]
        rows = [[r["year"], fmt_num(r["dso"], 0), fmt_num(r["dio"], 0),
                 fmt_num(r["dpo"], 0), fmt_num(r["ccc"], 0),
                 fmt_num(r["current_ratio"])] for r in wc]
        self._table(headers, rows, [25, 28, 28, 28, 28, 30])

    def add_flags_page(self):
        """Red & green flags."""
        self._check_page_break(50)
        self._section("RED FLAGS & GREEN FLAGS")

        self._subsection("Red Flags (Concerns / Warning Signs)")
        if self.analyzer.red_flags:
            for flag_text, severity in self.analyzer.red_flags:
                sev_label = {"critical": "[CRITICAL]", "major": "[MAJOR]", "minor": "[MINOR]"}.get(severity, "")
                self._flag("%s %s" % (sev_label, flag_text), is_red=True)
        else:
            self.set_font("Calibri", "I", 9)
            self.cell(0, 6, "No red flags detected.", new_x="LMARGIN", new_y="NEXT")

        self.ln(5)
        self._subsection("Green Flags (Positive Indicators)")
        if self.analyzer.green_flags:
            for flag_text in self.analyzer.green_flags:
                self._flag(flag_text, is_red=False)
        else:
            self.set_font("Calibri", "I", 9)
            self.cell(0, 6, "No green flags detected.", new_x="LMARGIN", new_y="NEXT")

    def add_growth_section(self):
        """Growth analysis."""
        g = self.results.get("growth", {})
        if not g:
            return
        self._subsection("Growth Analysis")
        if "revenue_cagr" in g:
            self._metric("Revenue CAGR", fmt_pct(g["revenue_cagr"]),
                         C_GREEN if g["revenue_cagr"] > 10 else C_RED if g["revenue_cagr"] < 0 else None)
        if "profit_cagr" in g:
            self._metric("Profit CAGR", fmt_pct(g["profit_cagr"]),
                         C_GREEN if g["profit_cagr"] > 10 else C_RED if g["profit_cagr"] < 0 else None)
        if "eps_growth" in g:
            self._metric("EPS Growth (YoY)", fmt_pct(g["eps_growth"]),
                         C_GREEN if g["eps_growth"] > 0 else C_RED)
        yoy = g.get("revenue_yoy", [])
        if yoy:
            self._metric("Revenue Growth (recent years)", ", ".join([fmt_pct(y) for y in yoy]))

    def add_credit_rating_page(self):
        """Credit rating section from NSE filings."""
        cr = self.results.get("credit_ratings", {})
        entries = cr.get("entries", [])
        if not entries:
            return

        self._check_page_break(60)
        self._section("CREDIT RATINGS (from NSE Filings)")

        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "Credit ratings are sourced from mandatory exchange filings on NSE. "
            "Companies must disclose every rating action (new/reaffirmed/upgraded/downgraded) "
            "by agencies like CRISIL, ICRA, CARE, India Ratings (Fitch), S&P Global, etc. "
            "Investment grade = BBB and above; AAA is the highest."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        # Summary metrics
        hr = cr.get("highest_ratings", {})
        if hr:
            self._subsection("Latest Ratings by Agency")
            for agency, rating in hr.items():
                color = C_GREEN if rating.upper() in ("AAA", "AA+", "AA", "A1+") else \
                        C_YELLOW if rating.upper() in ("A+", "A", "A1", "BBB+", "BBB") else C_RED
                self._metric(agency, rating, color)
            self.ln(2)

            if cr.get("has_negative_outlook"):
                self._flag("Outlook is Negative/Watch — potential downgrade ahead", is_red=True)
            if cr.get("has_downgrade"):
                self._flag("Recent downgrade detected in filings", is_red=True)
            if cr.get("investment_grade"):
                self._flag("All ratings are investment grade", is_red=False)

        self.ln(3)
        self._subsection("Filing History (most recent first)")
        headers = ["Date", "Agency", "Rating(s)", "Outlook"]
        rows = []
        for e in entries[:10]:
            date = e.get("date", "")[:11]
            agency = e.get("agency", "Unknown")
            ratings = ", ".join(e.get("ratings", [])) or "(see PDF)"
            outlook = e.get("outlook", "") or "-"
            rows.append([date, agency, ratings, outlook])
        self._table(headers, rows, [35, 50, 60, 35])

    def add_enhanced_checks_section(self):
        """Enhanced forensic checks section in PDF."""
        enh = self.results.get("enhanced", {})
        if not enh:
            return

        self._check_page_break(60)
        self._section("ENHANCED FORENSIC CHECKS")

        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "Additional deep-dive checks covering asset quality, capital allocation, "
            "dividend sustainability, and liquidity. These complement the core forensic "
            "models (Beneish, Altman, Piotroski) with granular metrics."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        # Key metrics table
        headers = ["Check", "Value", "Assessment"]
        rows = []

        # Negative Equity
        neg_eq = enh.get("negative_equity", False)
        rows.append(["Negative Equity", "YES" if neg_eq else "No",
                     "CRITICAL - Technically insolvent" if neg_eq else "Equity is positive"])

        # Loss Years
        ly = enh.get("loss_years", 0)
        ly_assess = "CRITICAL" if ly >= 3 else "CONCERN" if ly >= 2 else "Good" if ly == 0 else "Monitor"
        rows.append(["Loss Years (of last %d)" % min(self.data.years, 4), str(ly), ly_assess])

        # Intangible Ratio
        ir = enh.get("intangible_ratio", float("nan"))
        ir_assess = "Very High Risk" if not _nan(ir) and ir > 50 else \
                    "High Risk" if not _nan(ir) and ir > 30 else \
                    "Low (Good)" if not _nan(ir) and ir < 5 else "Moderate"
        rows.append(["Goodwill + Intangibles / Assets", fmt_pct(ir), ir_assess])

        # ROIC
        roic = enh.get("roic", float("nan"))
        roic_assess = "Excellent" if not _nan(roic) and roic > 15 else \
                      "Poor" if not _nan(roic) and roic < 5 else "Adequate"
        rows.append(["ROIC (Return on Invested Capital)", fmt_pct(roic), roic_assess])

        # Quick Ratio
        qr = enh.get("quick_ratio", float("nan"))
        qr_assess = "Concern" if not _nan(qr) and qr < 0.8 else \
                    "Strong" if not _nan(qr) and qr > 1.5 else "Adequate"
        rows.append(["Quick Ratio", fmt_num(qr), qr_assess])

        # Cash Conversion
        cc = enh.get("cash_conversion", float("nan"))
        cc_assess = "Excellent" if not _nan(cc) and cc > 80 else \
                    "Weak" if not _nan(cc) and cc < 30 else "Adequate"
        rows.append(["Cash Conversion (FCF/NI)", fmt_pct(cc), cc_assess])

        # Dividend Sustainability
        dp_fcf = enh.get("dividend_payout_fcf", float("nan"))
        dp_ni = enh.get("dividend_payout_ni", float("nan"))
        if not _nan(dp_fcf):
            dp_assess = "Unsustainable" if dp_fcf > 100 else \
                        "High" if dp_fcf > 70 else "Sustainable"
            rows.append(["Dividend / FCF", fmt_pct(dp_fcf), dp_assess])
        elif not _nan(dp_ni):
            dp_assess = "Very High" if dp_ni > 90 else "Reasonable"
            rows.append(["Dividend / Net Income", fmt_pct(dp_ni), dp_assess])

        self._table(headers, rows, [65, 45, 70])

    def add_benfords_law_section(self):
        """Benford's Law analysis section in PDF."""
        bf = self.results.get("benford", {})
        if not bf or not bf.get("available"):
            return

        self._check_page_break(60)
        self._section("BENFORD'S LAW ANALYSIS (First-Digit Fraud Detection)")

        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "Benford's Law states that in naturally occurring datasets, the leading digit 1 "
            "appears ~30.1% of the time, digit 2 ~17.6%, etc. following log10(1 + 1/d). "
            "Financial numbers that deviate significantly from this pattern may indicate "
            "fabrication or manipulation. This technique is used by Big 4 audit firms, the "
            "IRS, and forensic accountants worldwide. Chi-squared test: p<0.05 = suspicious."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        color = C_GREEN if bf["conformity"] == "PASS" else C_YELLOW if bf["conformity"] == "MARGINAL" else C_RED
        self._metric("Chi-Squared Statistic", "%.2f" % bf["chi_squared"], color)
        self._metric("Mean Absolute Deviation", "%.2f%%" % bf["mad"])
        self._metric("Verdict", bf["verdict"], color)
        self._metric("Data Points Analysed", "%d financial values" % bf["total_values"])
        self.ln(3)

        # Digit distribution table
        headers = ["Digit", "Expected %", "Observed %", "Deviation"]
        rows = []
        for d in range(1, 10):
            exp = bf["expected"][d]
            obs = bf["observed"][d]
            dev = obs - exp
            rows.append([str(d), "%.1f%%" % exp, "%.1f%%" % obs,
                         "%+.1f%%" % dev])
        self._table(headers, rows, [30, 45, 45, 50])

    def add_montier_section(self):
        """Montier C-Score section in PDF."""
        mc = self.results.get("montier", {})
        if not mc:
            return

        self._subsection("Montier C-Score (6-Variable Manipulation Detector)")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "James Montier's C-Score uses 6 binary signals to detect manipulation. "
            "Unlike Beneish (continuous ratios), Montier uses simple yes/no flags. "
            "0-1 = Low risk, 2-3 = Moderate, 4-6 = High manipulation risk. "
            "Cross-validates the Beneish M-Score from a different analytical angle."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        c = mc["c_score"]
        color = C_GREEN if c <= 1 else C_YELLOW if c <= 3 else C_RED
        self._metric("C-Score", "%d / 6  (%s)" % (c, mc["verdict"]), color)
        self.ln(2)

        headers = ["Signal", "Triggered?"]
        rows = [[k, "YES" if v else "No"] for k, v in mc["details"].items()]
        self._table(headers, rows, [110, 60])

    def add_ohlson_section(self):
        """Ohlson O-Score section in PDF."""
        oh = self.results.get("ohlson", {})
        if not oh:
            return

        self._subsection("Ohlson O-Score (Logistic Bankruptcy Probability)")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "The Ohlson O-Score (1980) uses logistic regression with 9 variables to compute "
            "the probability of bankruptcy within 2 years. Unlike Altman (linear discriminant) "
            "and Springate, Ohlson outputs a direct probability. P > 50% = high risk, "
            "P > 30% = elevated, P < 30% = low risk. Uses size, leverage, liquidity, "
            "profitability, and earnings trend variables."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        prob = oh["probability"]
        color = C_GREEN if prob < 0.3 else C_YELLOW if prob < 0.5 else C_RED
        self._metric("O-Score", "%.2f" % oh["o_score"], color)
        self._metric("Bankruptcy Probability", "%.1f%%" % (prob * 100), color)
        self._metric("Verdict", oh["verdict"], color)
        self.ln(2)

        comp_desc = {
            "SIZE": "ln(Total Assets) — company size",
            "TLTA": "Total Liabilities / Total Assets — leverage",
            "WCTA": "Working Capital / Total Assets — liquidity",
            "CLCA": "Current Liabilities / Current Assets — short-term stress",
            "OENEG": "1 if Total Liabilities > Total Assets — negative equity",
            "NITA": "Net Income / Total Assets — profitability",
            "FFOTL": "Cash Flow / Total Liabilities — cash coverage",
            "INTWO": "1 if consecutive losses — persistent loss flag",
            "CHIN": "Change in Net Income (normalised)",
        }
        headers = ["Variable", "Value", "Meaning"]
        rows = [[k, fmt_num(v, 3), comp_desc.get(k, "")] for k, v in oh["components"].items()]
        self._table(headers, rows, [22, 22, 136])

    def add_sgr_section(self):
        """Sustainable Growth Rate section in PDF."""
        sg = self.results.get("sgr", {})
        if not sg:
            return

        self._subsection("Sustainable Growth Rate (SGR)")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "SGR = ROE x (1 - Payout Ratio). This is the maximum rate a company can grow "
            "using only internal funds (retained earnings). If actual growth exceeds SGR, "
            "the company must be increasing debt or diluting equity — which is unsustainable "
            "long-term. A key check that most retail investors overlook."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        sgr = sg.get("sgr", float("nan"))
        actual = sg.get("actual_growth", float("nan"))
        color = C_GREEN
        if not _nan(sgr) and not _nan(actual) and actual > sgr + 5:
            color = C_RED
        self._metric("ROE", fmt_pct(sg.get("roe", float("nan"))))
        self._metric("Payout Ratio", fmt_pct(sg.get("payout_ratio", float("nan"))))
        self._metric("Retention Ratio", fmt_pct(sg.get("retention_ratio", float("nan"))))
        self._metric("Sustainable Growth Rate", fmt_pct(sgr), C_GREEN)
        self._metric("Actual Revenue Growth", fmt_pct(actual), color)
        gap = sg.get("growth_gap", float("nan"))
        if not _nan(gap):
            gc = C_RED if gap > 5 else C_GREEN
            self._metric("Growth Gap (Actual - SGR)", "%+.1f%%" % gap, gc)

    def add_volatility_section(self):
        """Earnings volatility section in PDF."""
        vol = self.results.get("volatility", {})
        if not vol:
            return

        self._subsection("Earnings Volatility & Persistence")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "Coefficient of Variation (CV) of earnings measures stability. "
            "CV < 15% = highly stable/persistent (high quality). "
            "CV 15-30% = moderate. CV > 30% = volatile (low quality, hard to predict). "
            "Volatile earnings often indicate cyclicality, one-time items, or "
            "inconsistent business performance."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        cv = vol.get("earnings_cv", float("nan"))
        color = C_GREEN if not _nan(cv) and cv < 15 else C_RED if not _nan(cv) and cv > 30 else C_YELLOW
        self._metric("Earnings CV", fmt_pct(cv), color)
        self._metric("Mean Net Income", fmt_cr(vol.get("earnings_mean", float("nan"))))
        self._metric("Std Deviation", fmt_cr(vol.get("earnings_std", float("nan"))))
        mcv = vol.get("margin_cv", float("nan"))
        if not _nan(mcv):
            self._metric("Net Margin CV", fmt_pct(mcv))

    def add_operating_leverage_section(self):
        """Operating leverage section in PDF."""
        ol = self.results.get("op_leverage", {})
        if not ol or not ol.get("rows"):
            return

        self._subsection("Degree of Operating Leverage (DOL)")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "DOL = %change in EBIT / %change in Revenue. Measures how sensitive operating "
            "profit is to revenue changes. DOL of 3x means a 10% revenue drop causes "
            "a 30% EBIT drop. Critical for cyclical companies (energy, metals, autos). "
            "High DOL amplifies both gains and losses."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        avg = ol.get("avg_dol", float("nan"))
        if not _nan(avg):
            color = C_RED if avg > 3 else C_YELLOW if avg > 2 else C_GREEN
            self._metric("Average DOL", "%.1fx" % avg, color)

        headers = ["Period", "Rev Change", "EBIT Change", "DOL"]
        rows = []
        for r in ol["rows"]:
            rows.append([
                r["period"],
                fmt_pct(r["rev_change"]),
                fmt_pct(r["ebit_change"]),
                fmt_num(r["dol"], 1) + "x" if not _nan(r["dol"]) else "N/A",
            ])
        self._table(headers, rows, [50, 40, 45, 35])

    def add_springate_section(self):
        """Springate S-Score section in PDF."""
        sp = self.results.get("springate", {})
        if not sp:
            return

        self._subsection("Springate S-Score (Alternative Bankruptcy Model)")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "The Springate S-Score is an alternative to the Altman Z-Score for predicting "
            "bankruptcy. S = 1.03*A + 3.07*B + 0.66*C + 0.40*D where A=WC/TA, B=EBIT/TA, "
            "C=EBT/CL, D=Revenue/TA. S < 0.862 suggests bankruptcy risk. Cross-validates "
            "the Altman Z-Score result."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        color = C_GREEN if sp["s_score"] >= 0.862 else C_RED
        self._metric("S-Score", "%.2f" % sp["s_score"], color)
        self._metric("Verdict", sp["verdict"], color)
        self.ln(2)

        comp_desc = {
            "A_WC_TA": "Working Capital / Total Assets (liquidity)",
            "B_EBIT_TA": "EBIT / Total Assets (operating efficiency)",
            "C_EBT_CL": "Pretax Income / Current Liabilities (short-term solvency)",
            "D_Rev_TA": "Revenue / Total Assets (asset utilisation)",
        }
        headers = ["Component", "Value", "Interpretation"]
        rows = [[k, fmt_num(v, 3), comp_desc.get(k, "")] for k, v in sp["components"].items()]
        self._table(headers, rows, [30, 22, 128])

    def add_promoter_section(self):
        """Promoter holding section in PDF."""
        prom = self.results.get("promoter", {})
        if not prom or not prom.get("available"):
            return

        self._subsection("Promoter Shareholding")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "Promoter holding indicates insider confidence. Declining promoter stake "
            "or high pledge levels are warning signs. Data sourced from NSE shareholding "
            "pattern filings."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        pp = prom.get("promoter_pct", float("nan"))
        pl = prom.get("pledge_pct", float("nan"))

        if not _nan(pp):
            color = C_GREEN if pp > 50 else C_YELLOW if pp > 25 else C_RED
            self._metric("Promoter Holding", "%.1f%%" % pp, color)
        if not _nan(pl):
            color = C_RED if pl > 20 else C_YELLOW if pl > 0 else C_GREEN
            self._metric("Promoter Pledge", "%.1f%%" % pl, color)

        change = prom.get("promoter_change", float("nan"))
        if not _nan(change):
            color = C_RED if change < -2 else C_GREEN if change > 1 else None
            self._metric("Promoter Change (vs prev)", "%+.1f%%" % change, color)

        # Shareholding table if multiple periods
        data = prom.get("data", [])
        if len(data) > 1:
            self.ln(2)
            headers = ["Period", "Promoter %", "Pledge %"]
            rows = []
            for d in data[:6]:
                rows.append([
                    str(d.get("date", ""))[:20],
                    "%.1f" % d["promoter_pct"] if not _nan(d.get("promoter_pct", float("nan"))) else "N/A",
                    "%.1f" % d["pledge_pct"] if not _nan(d.get("pledge_pct", float("nan"))) else "N/A",
                ])
            self._table(headers, rows, [60, 50, 50])

    def add_esm_section(self):
        """ESM status section in PDF."""
        esm = self.results.get("esm", {})

        self._subsection("ESM (Enhanced Surveillance Measure) Status")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "The ESM framework was introduced by NSE/BSE to enhance market integrity. "
            "Stocks placed under ESM have additional surveillance due to concerns about "
            "price volatility, financial compliance, or potential manipulation. "
            "ESM Stage I has moderate restrictions; Stage II has severe restrictions "
            "including reduced trading sessions and higher margin requirements."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        if esm.get("in_esm"):
            stage = esm.get("stage", "Unknown")
            self.set_fill_color(*C_RED)
            self.set_text_color(*C_WHITE)
            self.set_font("Calibri", "B", 9)
            self.cell(0, 5.5, _latin("  WARNING: STOCK IS IN ESM %s  " % stage),
                      fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)
            self.ln(3)
            self.set_font("Calibri", "", 9)
            if "Stage II" in str(stage):
                self.multi_cell(0, 5, _latin(
                    "ESM Stage II means: Trade-to-trade settlement (no intraday), "
                    "trading only on Wednesday, 100% upfront margin, price band of 2%. "
                    "This is a serious warning sign and indicates significant exchange-level concerns."),
                    new_x="LMARGIN", new_y="NEXT")
            else:
                self.multi_cell(0, 5, _latin(
                    "ESM Stage I means: Trade-to-trade settlement (no intraday), "
                    "price band of 5%, applicable margin of 100%. "
                    "This indicates moderate exchange-level surveillance concerns."),
                    new_x="LMARGIN", new_y="NEXT")
        else:
            self.set_fill_color(*C_GREEN)
            self.set_text_color(*C_WHITE)
            self.set_font("Calibri", "B", 9)
            self.cell(0, 5.5, _latin("  Stock is NOT in any ESM stage (Normal Trading)  "),
                      fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)
        self.ln(3)

    def add_sector_analysis(self):
        """Sector & competitive overview."""
        self._check_page_break(60)
        self._section("SECTOR & COMPETITIVE ANALYSIS")

        sector = self.info.get("sectorDisp", "N/A")
        industry = self.info.get("industryDisp", "N/A")

        self._metric("Sector", sector)
        self._metric("Industry", industry)
        self.ln(3)

        self._subsection("Sector Context")
        self.set_font("Calibri", "", 9)
        self.multi_cell(0, 5, _latin(
            "The company operates in the %s sector, specifically in the %s industry. "
            "Investors should evaluate: (a) sector growth trajectory, (b) regulatory environment, "
            "(c) cyclicality, (d) competitive intensity, and (e) technological disruption risks. "
            "Cross-reference with industry reports for current market size and growth projections."
            % (sector, industry)), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        # Moat assessment from financials
        self._subsection("Competitive Moat Assessment (from financials)")
        prof = self.results.get("profitability", [])
        ratios = self.results.get("ratios", [])
        moat_signals = []
        if prof:
            gm = [r["gross_margin"] for r in prof if not _nan(r["gross_margin"])]
            om = [r["operating_margin"] for r in prof if not _nan(r["operating_margin"])]
            if gm and min(gm) > 30:
                moat_signals.append("Consistently high gross margins (>30%) suggest pricing power or brand strength.")
            if om and min(om) > 15:
                moat_signals.append("Sustained high operating margins (>15%) indicate competitive advantage.")
            if gm and max(gm) - min(gm) < 5:
                moat_signals.append("Stable gross margins indicate pricing discipline and cost control.")

        if ratios:
            roes = [r["roe"] for r in ratios if not _nan(r["roe"])]
            if roes and min(roes) > 15:
                moat_signals.append("Consistently high ROE (>15%) suggests economic moat.")

        g = self.results.get("growth", {})
        if g.get("revenue_cagr", 0) > 15:
            moat_signals.append("Strong revenue growth suggests market share gains or expanding market.")

        mkt_cap = self.info.get("marketCap", 0)
        if mkt_cap and mkt_cap > 1e12:  # > 1 lakh Cr
            moat_signals.append("Large-cap status (>Rs. 1 lakh Cr) provides scale advantages.")

        if moat_signals:
            for sig in moat_signals:
                self.set_font("Calibri", "", 9)
                self.set_text_color(*C_GREEN)
                self.multi_cell(0, 5, _latin("[+] " + sig), new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)
        else:
            self.set_font("Calibri", "I", 9)
            self.cell(0, 6, "No clear moat signals from financial data alone.", new_x="LMARGIN", new_y="NEXT")

        self.ln(3)
        self._subsection("Valuation Snapshot")
        val_metrics = [
            ("Trailing P/E", fmt_num(self.info.get("trailingPE", float("nan")))),
            ("Forward P/E", fmt_num(self.info.get("forwardPE", float("nan")))),
            ("Price/Book", fmt_num(self.info.get("priceToBook", float("nan")))),
            ("EV/EBITDA", fmt_num(self.info.get("enterpriseToEbitda", float("nan")))),
            ("PEG Ratio", fmt_num(self.info.get("pegRatio", float("nan")))),
            ("Price/Sales", fmt_num(self.info.get("priceToSalesTrailing12Months", float("nan")))),
            ("Dividend Yield", fmt_pct(self.info.get("dividendYield", 0) * 100) if self.info.get("dividendYield") else "N/A"),
        ]
        for label, value in val_metrics:
            self._metric(label, value)

    def add_recommendation_page(self):
        """Comprehensive investment recommendation — detailed verdict with full context.
        Should give complete understanding of whether to invest and why."""
        self._section("INVESTMENT RECOMMENDATION & VERDICT")

        overall = self.results.get("overall", {})
        info = self.info
        score = overall.get("final_score", 0)
        rec = overall.get("recommendation", "N/A")

        # ── Big Verdict Banner ──
        self.ln(3)
        color = C_GREEN if score >= 60 else C_YELLOW if score >= 45 else C_RED
        self.set_fill_color(*color)
        self.set_text_color(*C_WHITE)
        self.set_font("Calibri", "B", 10)
        self.cell(0, 5.5, _latin("  OVERALL SCORE: %.0f / 100  " % score),
                  fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(1)
        self.cell(0, 5.5, _latin("  RECOMMENDATION: %s  " % rec),
                  fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(3)

        # ── Detailed Recommendation Narrative ──
        self._subsection("Investment Thesis")
        self.set_font("Calibri", "", 9)
        self.multi_cell(0, 5, _latin(overall.get("rec_detail", "")), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        # ── Valuation Assessment ──
        self._subsection("Valuation Assessment")
        pe = info.get("trailingPE", 0)
        pb = info.get("priceToBook", 0)
        ev_ebitda = info.get("enterpriseToEbitda", 0)
        fwd_pe = info.get("forwardPE", 0)
        cmp = info.get("currentPrice", 0)
        self._metric("Current Price", "Rs. %.2f" % cmp if cmp else "N/A")
        self._metric("Trailing P/E", "%.1f" % pe if pe else "N/A")
        self._metric("Forward P/E", "%.1f" % fwd_pe if fwd_pe else "N/A")
        self._metric("P/B Ratio", "%.1f" % pb if pb else "N/A")
        self._metric("EV/EBITDA", "%.1f" % ev_ebitda if ev_ebitda else "N/A")

        # DCF and Graham from deep analyzer
        if hasattr(self, "deep_analyzer") and self.deep_analyzer:
            da = self.deep_analyzer.results
            dcf = da.get("dcf", {})
            if dcf and dcf.get("intrinsic_value"):
                iv = dcf["intrinsic_value"]
                upside = ((iv - cmp) / cmp * 100) if cmp else 0
                self._metric("DCF Intrinsic Value", "Rs. %.0f (%.0f%% %s)" % (
                    iv, abs(upside), "upside" if upside > 0 else "downside"),
                    C_GREEN if upside > 20 else C_RED if upside < -20 else C_YELLOW)
            graham = da.get("graham_magic", {})
            if graham and graham.get("graham_number"):
                gn = graham["graham_number"]
                self._metric("Graham Number", "Rs. %.0f" % gn,
                    C_GREEN if cmp < gn else C_RED)
            peer = da.get("peer_comparison", {})
            if peer and peer.get("pe_discount_pct") is not None:
                self._metric("PE Discount vs Peers", "%.0f%%" % peer["pe_discount_pct"],
                    C_GREEN if peer["pe_discount_pct"] > 20 else C_RED if peer["pe_discount_pct"] < -20 else C_YELLOW)

        # ── Growth Outlook ──
        self._subsection("Growth & Profitability Outlook")
        prof = self.results.get("profitability", [])
        if len(prof) >= 2:
            rev_curr = prof[0].get("revenue", 0)
            rev_prev = prof[1].get("revenue", 0)
            rev_growth = ((rev_curr - rev_prev) / rev_prev * 100) if rev_prev else 0
            ni_curr = prof[0].get("net_income", 0)
            ni_prev = prof[1].get("net_income", 0)
            ni_growth = ((ni_curr - ni_prev) / ni_prev * 100) if ni_prev else 0
            self._metric("Revenue Growth (YoY)", "%+.1f%%" % rev_growth,
                         C_GREEN if rev_growth > 10 else C_RED if rev_growth < 0 else C_YELLOW)
            self._metric("Net Income Growth (YoY)", "%+.1f%%" % ni_growth,
                         C_GREEN if ni_growth > 10 else C_RED if ni_growth < 0 else C_YELLOW)
            opm = prof[0].get("opr_margin", 0)
            npm = prof[0].get("net_margin", 0)
            if opm:
                self._metric("Operating Margin", "%.1f%%" % (opm * 100))
            if npm:
                self._metric("Net Margin", "%.1f%%" % (npm * 100))

        if hasattr(self, "deep_analyzer") and self.deep_analyzer:
            da = self.deep_analyzer.results
            ob = da.get("order_book", {})
            if ob and ob.get("order_book_cr"):
                self._metric("Order Book", "Rs.%.0f Cr (YoY %+.0f%%)" % (
                    ob["order_book_cr"], ob.get("yoy_growth_pct", 0)))

        # ── Risk Summary ──
        self._subsection("Risk Factors")
        crit = [(f, s) for f, s in self.analyzer.red_flags if s == "critical"]
        major = [(f, s) for f, s in self.analyzer.red_flags if s == "major"]
        if crit:
            self.set_font("Calibri", "B", 9)
            self.set_text_color(*C_RED)
            self.cell(0, 5, _latin("CRITICAL RISKS (%d):" % len(crit)), new_x="LMARGIN", new_y="NEXT")
            self.set_font("Calibri", "", 8)
            for flag, _ in crit[:5]:
                self.multi_cell(0, 4, _latin("  - %s" % flag), new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)
            self.ln(2)
        if major:
            self.set_font("Calibri", "B", 9)
            self.set_text_color(192, 100, 43)
            self.cell(0, 5, _latin("MAJOR RISKS (%d):" % len(major)), new_x="LMARGIN", new_y="NEXT")
            self.set_font("Calibri", "", 8)
            for flag, _ in major[:5]:
                self.multi_cell(0, 4, _latin("  - %s" % flag), new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)
            self.ln(2)

        # ── Positive Signals ──
        if self.analyzer.green_flags:
            self._subsection("Positive Signals")
            self.set_font("Calibri", "", 8)
            self.set_text_color(*C_GREEN)
            for flag in self.analyzer.green_flags[:8]:
                self.multi_cell(0, 4, _latin("  + %s" % flag), new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)
            self.ln(2)

        # ── Score Breakdown ──
        self._subsection("Score Breakdown")
        self._metric("Base Score (weighted analysis)", "%.1f / 100" % overall.get("base_score", 0))
        self._metric("Red Flag Penalty", "-%.1f" % overall.get("penalty", 0), C_RED)
        self._metric("Green Flag Bonus", "+%.1f" % overall.get("bonus", 0), C_GREEN)
        self._metric("Final Score", "%.0f / 100" % score)

        # Deep Score if available
        if hasattr(self, "deep_analyzer") and self.deep_analyzer:
            ds = self.deep_analyzer.results.get("deep_score", {})
            if ds:
                self.ln(2)
                self._metric("Deep Fundamental Score", "%.0f / 100" % ds.get("total_score", 0))
                sub = ds.get("sub_scores", {})
                if sub:
                    for k, v in sub.items():
                        self._metric("  " + k.replace("_", " ").title(), "%.0f" % v)

        # ── Scoring Methodology ──
        self.ln(3)
        self._subsection("Scoring Methodology")
        self.set_font("Calibri", "", 8)
        self.multi_cell(0, 4, _latin(
            "The overall score is computed as a weighted average of 14 forensic techniques "
            "across 4 categories: Manipulation Detection (25%), Bankruptcy/Distress (20%), "
            "Fundamental Quality (35%), and Deep Forensic Checks (20%). Each technique scores "
            "0-10, weighted, scaled to 0-100, then adjusted by red flag penalties (-6 critical, "
            "-3 major, -1.5 minor) and green flag bonuses (+1.5 each, max +10)."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

        details = overall.get("score_details", [])
        if details:
            headers = ["Technique", "Weight", "Score (0-10)", "Contribution"]
            rows = []
            cat_a, cat_b, cat_c, cat_d = [], [], [], []
            for d in details:
                name = d["technique"]
                if "Manipulation" in name or "Benford" in name or "Integrity" in name:
                    cat_a.append(d)
                elif "Bankruptcy" in name:
                    cat_b.append(d)
                elif any(k in name for k in ["Piotroski", "Cash Flow", "Debt", "Profitability"]):
                    cat_c.append(d)
                else:
                    cat_d.append(d)

            def _add_cat(label, items):
                rows.append([label, "", "", ""])
                for d in items:
                    rows.append(["  " + d["technique"], "%.0f%%" % d["weight"],
                                 "%d / 10" % d["raw_score"], "%.1f" % d["weighted"]])

            _add_cat("A. MANIPULATION DETECTION (25%)", cat_a)
            _add_cat("B. BANKRUPTCY / DISTRESS (20%)", cat_b)
            _add_cat("C. FUNDAMENTAL QUALITY (35%)", cat_c)
            _add_cat("D. DEEP FORENSIC CHECKS (20%)", cat_d)
            total_contrib = sum(d["weighted"] for d in details)
            rows.append(["TOTAL", "100%", "", "%.1f" % total_contrib])
            self._table(headers, rows, [80, 28, 38, 34], align=["L", "C", "C", "C"])

        # ── Final Verdict ──
        self.ln(5)
        self._subsection("Score Interpretation")
        scale = [
            ("75-100: STRONG BUY", "Excellent financial health with strong fundamentals."),
            ("60-74: BUY", "Good financials. Monitor identified concerns."),
            ("45-59: HOLD", "Mixed signals. Significant concerns need monitoring."),
            ("30-44: SELL / AVOID", "Multiple red flags. Deteriorating fundamentals."),
            ("0-29: STRONG AVOID", "Serious financial distress or manipulation risk."),
        ]
        for label, desc in scale:
            self.set_font("Calibri", "B", 9)
            self.cell(50, 5, _latin(label))
            self.set_font("Calibri", "", 9)
            self.cell(0, 5, _latin(desc), new_x="LMARGIN", new_y="NEXT")

    # ── GENERATE FULL REPORT ─────────────────────────────────────────────────
    def generate(self, output_path):
        """Build and save the complete PDF report."""
        print("\n[4/5] Generating PDF report...")

        self.add_cover_page()
        # Table of Contents — starts on its own page
        self.add_page()
        try:
            self._toc_pages_reserved = 2
            self.insert_toc_placeholder(self._render_toc, pages=self._toc_pages_reserved)
        except Exception as e:
            print("  TOC placeholder failed: %s" % e)
        self.add_key_red_flags_summary()
        self.add_executive_summary()
        self.add_recommendation_page()  # Section 2: right after executive summary

        # ── SECTION ORDER: Logical analysis flow ──
        # Phase 1: Company Background
        self.add_company_overview()
        self.add_financial_tables()

        # Phase 2: Forensic & Manipulation Detection (most critical first)
        self.add_forensic_scores()
        self.add_benfords_law_section()
        self.add_enhanced_checks_section()
        self.add_springate_section()
        self.add_ohlson_section()
        self.add_montier_section()

        # Phase 3: Financial Quality & Efficiency
        self.add_dupont_section()
        self.add_working_capital_section()
        self.add_growth_section()
        self.add_sgr_section()
        self.add_volatility_section()
        self.add_operating_leverage_section()

        # Phase 4: Governance & Promoter Integrity
        self.add_esm_section()
        self.add_promoter_section()

        # Phase 5: Deep Fundamental Analysis
        if hasattr(self, "deep_analyzer") and self.deep_analyzer:
            self.add_deep_dcf_section()
            self.add_deep_valuation_bands_section()
            self.add_deep_capital_allocation_section()
            self.add_deep_earnings_quality_section()
            self.add_deep_working_capital_section()
            self.add_deep_debt_stress_section()
            self.add_deep_moat_section()
            self.add_deep_quarterly_momentum_section()
            self.add_deep_concall_nlp_section()
            self.add_deep_annual_report_nlp_section()
            self.add_deep_risk_section()
            self.add_deep_score_section()
            # Phase 6: Market & Technical
            self.add_deep_shareholding_section()
            self.add_deep_insider_trading_section()
            self.add_deep_peer_comparison_section()
            self.add_deep_relative_strength_section()
            self.add_deep_technical_section()
            self.add_deep_graham_section()
            self.add_deep_capex_section()
            self.add_deep_institutional_section()
            self.add_deep_corporate_actions_section()
            self.add_deep_credit_intelligence_section()
            self.add_deep_order_book_section()

        # Phase 7: Comparative & Sector Context
        self.add_user_comparison_section()
        self.add_flags_page()
        self.add_credit_rating_page()
        self.add_sector_analysis()

        self.output(output_path)
        print("  PDF saved: %s" % output_path)
        return output_path

    # ── DEEP FUNDAMENTAL PDF SECTIONS ────────────────────────────────────────

    def add_deep_dcf_section(self):
        """DCF Valuation page."""
        dr = self.deep_analyzer.results.get("dcf", {})
        if dr.get("status") != "computed":
            return
        self._check_page_break(90)
        self._section("DCF VALUATION (3-Stage Model)")
        self._intro(
            "What it is: A discounted-cash-flow model that projects the company's free "
            "cash flows over a high-growth phase, a fade phase and a terminal phase, "
            "discounts them back at WACC, and compares the result to the current price. "
            "A positive Margin of Safety means the stock trades below estimated intrinsic value."
        )
        self.set_font("Calibri", "", 9)

        # Key metrics
        rows = [
            ["Latest FCF", "Rs. %.0f Cr" % dr["fcf_latest_cr"]],
            ["FCF CAGR (Historical)", "%.1f%%" % dr["fcf_cagr_pct"]],
            ["High Growth Rate (Stage 1)", "%.1f%%" % dr["high_growth_pct"]],
            ["Terminal Growth Rate", "%.1f%%" % dr["terminal_growth_pct"]],
            ["WACC", "%.1f%%" % dr["wacc_pct"]],
            ["Cost of Equity", "%.1f%%" % dr["cost_of_equity_pct"]],
            ["Beta", "%.2f" % dr["beta"]],
            ["", ""],
            ["PV Stage 1 (High Growth)", "Rs. %.0f Cr" % dr["pv_stage1_cr"]],
            ["PV Stage 2 (Fade)", "Rs. %.0f Cr" % dr["pv_stage2_cr"]],
            ["PV Terminal Value", "Rs. %.0f Cr" % dr["pv_terminal_cr"]],
            ["Enterprise Value", "Rs. %.0f Cr" % dr["ev_cr"]],
            ["Equity Value", "Rs. %.0f Cr" % dr["equity_value_cr"]],
            ["", ""],
            ["Intrinsic Value / Share", "Rs. %.0f" % dr["intrinsic_per_share"]],
            ["Current Market Price", "Rs. %.0f" % dr["cmp"]],
            ["Margin of Safety", "%.1f%%" % dr["margin_of_safety_pct"]],
        ]
        self._table(["Parameter", "Value"], rows, col_widths=[100, 80])

        # Interpretation
        mos = dr["margin_of_safety_pct"]
        self.ln(3)
        if mos > 20:
            self._metric("Verdict", "UNDERVALUED (%.0f%% upside to intrinsic)" % mos, C_GREEN)
        elif mos > -10:
            self._metric("Verdict", "FAIRLY VALUED", C_YELLOW)
        else:
            self._metric("Verdict", "OVERVALUED (%.0f%% above intrinsic)" % abs(mos), C_RED)

    def add_deep_valuation_bands_section(self):
        """Valuation bands and relative valuation."""
        vb = self.deep_analyzer.results.get("valuation_bands", {})
        if vb.get("status") != "computed":
            return
        self._check_page_break(80)
        self._section("VALUATION BANDS & RELATIVE METRICS")
        self._intro(
            "What it is: Where today's PE sits inside its own 5-year high-low band. "
            "A percentile near 0% = historically cheap; near 100% = historically expensive."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Trailing P/E", fmt_num(vb.get("pe_trailing", 0), 1)],
            ["Forward P/E", fmt_num(vb.get("pe_forward", 0), 1)],
            ["P/B Ratio", fmt_num(vb.get("pb_ratio", 0), 1)],
            ["EV/EBITDA", fmt_num(vb.get("ev_ebitda", 0), 1)],
            ["", ""],
            ["5Y PE High (approx)", fmt_num(vb.get("pe_high_5y", 0), 1)],
            ["5Y PE Low (approx)", fmt_num(vb.get("pe_low_5y", 0), 1)],
            ["5Y PE Average", fmt_num(vb.get("pe_avg_5y", 0), 1)],
            ["PE Percentile (5Y band)", fmt_num(vb.get("pe_percentile", 0), 0) + "%"],
            ["", ""],
            ["52W High", "Rs. %.0f" % vb.get("high_52w", 0) if not _nan(vb.get("high_52w", 0)) else "N/A"],
            ["52W Low", "Rs. %.0f" % vb.get("low_52w", 0) if not _nan(vb.get("low_52w", 0)) else "N/A"],
            ["Price Position (52W)", "%.0f%%" % vb.get("price_position_52w", 0) if not _nan(vb.get("price_position_52w", 0)) else "N/A"],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[100, 80])

    def add_deep_capital_allocation_section(self):
        """Capital allocation & ROCE section."""
        ca = self.deep_analyzer.results.get("capital_allocation", {})
        if not ca:
            return
        self._check_page_break(70)
        self._section("CAPITAL ALLOCATION & ROCE")
        self._intro(
            "What it is: How efficiently management converts every rupee of capital into "
            "profit. ROCE > WACC means value is being created; ROCE < WACC means "
            "reinvestment is destroying shareholder value."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Avg ROCE", "%.1f%%" % ca.get("avg_roce_pct", 0)],
            ["Avg Incremental ROCE", "%.1f%%" % ca.get("avg_incremental_roce_pct", 0) if not _nan(ca.get("avg_incremental_roce_pct", 0)) else "N/A"],
            ["ROCE - WACC Spread", "%.1f%%" % ca.get("roce_wacc_spread_pct", 0)],
            ["WACC", "%.1f%%" % ca.get("wacc_pct", 0)],
            ["Reinvestment Rate", "%.1f%%" % ca.get("reinvestment_rate_pct", 0)],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[100, 80])

        # ROCE trend
        roce_s = ca.get("roce_series_pct", [])
        if roce_s:
            self.ln(2)
            self.set_font("Calibri", "B", 9)
            self.cell(0, 5, _latin("ROCE Trend: " + " -> ".join(
                ["%.1f%%" % r if not _nan(r) else "N/A" for r in roce_s])),
                new_x="LMARGIN", new_y="NEXT")

    def add_deep_earnings_quality_section(self):
        """Earnings quality analysis."""
        eq = self.deep_analyzer.results.get("earnings_quality", {})
        if not eq:
            return
        self._check_page_break(60)
        self._section("EARNINGS QUALITY ANALYSIS")
        self._intro(
            "What it is: Are reported profits backed by real cash? CFO/PAT close to or "
            "above 1.0 = high quality; persistently low = profits exist on paper but "
            "cash isn't following, often a precursor to write-downs."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Avg CFO / PAT Ratio", "%.2f" % eq.get("avg_cfo_pat", 0)],
            ["Avg Accrual Ratio", "%.3f" % eq.get("avg_accrual_ratio", 0)],
            ["Revenue Growth", fmt_pct(eq.get("rev_growth_pct", 0))],
            ["Receivables Growth", fmt_pct(eq.get("receivables_growth_pct", 0))],
            ["Rev-Recv Divergence", fmt_pct(eq.get("rev_recv_divergence_pct", 0))],
            ["Capex / Depreciation", "%.1fx" % eq.get("capex_dep_ratio", 0) if not _nan(eq.get("capex_dep_ratio", 0)) else "N/A"],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[100, 80])

        # Interpretation
        cfo_pat = eq.get("avg_cfo_pat", 0)
        self.ln(2)
        if cfo_pat > 0.9:
            self._metric("Quality", "HIGH — Earnings well backed by operating cash flow", C_GREEN)
        elif cfo_pat > 0.6:
            self._metric("Quality", "MODERATE — Some gap between profits and cash", C_YELLOW)
        else:
            self._metric("Quality", "LOW — Profits significantly exceed cash generation", C_RED)

    def add_deep_working_capital_section(self):
        """Working capital efficiency (DSO/DIO/DPO/CCC)."""
        wc = self.deep_analyzer.results.get("working_capital_eff", {})
        if not wc:
            return
        self._check_page_break(60)
        self._section("WORKING CAPITAL EFFICIENCY")
        self._intro(
            "What it is: How many days cash is locked in receivables (DSO) and inventory "
            "(DIO) net of supplier credit (DPO). A shrinking Cash Conversion Cycle frees "
            "up cash; a stretching one signals tightening operations or aggressive sales."
        )
        self.set_font("Calibri", "", 9)

        headers = ["Metric"] + ["Y%d" % (i+1) for i in range(len(wc.get("dso_series", [])))]
        dso_row = ["DSO (days)"] + [fmt_num(d, 0) for d in wc.get("dso_series", [])]
        dio_row = ["DIO (days)"] + [fmt_num(d, 0) for d in wc.get("dio_series", [])]
        dpo_row = ["DPO (days)"] + [fmt_num(d, 0) for d in wc.get("dpo_series", [])]
        ccc_row = ["Cash Conv. Cycle"] + [fmt_num(d, 0) for d in wc.get("ccc_series", [])]

        self._table(headers, [dso_row, dio_row, dpo_row, ccc_row],
                    col_widths=[45] + [28] * len(wc.get("dso_series", [])))
        self.ln(2)
        self._metric("Trend", wc.get("ccc_trend", "N/A").upper(),
                     C_GREEN if wc.get("ccc_trend") == "improving" else
                     (C_RED if wc.get("ccc_trend") == "deteriorating" else C_YELLOW))

    def add_deep_debt_stress_section(self):
        """Debt stress test section."""
        ds = self.deep_analyzer.results.get("debt_stress", {})
        if not ds:
            return
        self._check_page_break(70)
        self._section("DEBT STRESS TEST")
        self._intro(
            "What it is: Can the company still pay interest if rates double, triple, or "
            "if EBITDA drops 30%? Interest-Coverage Ratio (ICR) above 3x in stress "
            "scenarios = resilient; below 1.5x = vulnerable."
        )
        self.set_font("Calibri", "", 9)

        icr = ds.get("icr_current", 0)
        icr_str = "%.1fx" % icr if icr != float("inf") else "No Debt"
        icr_2x = ds.get("icr_stress_2x_rates", 0)
        icr_2x_str = "%.1fx" % icr_2x if icr_2x != float("inf") else "N/A"
        icr_3x = ds.get("icr_stress_3x_rates", 0)
        icr_3x_str = "%.1fx" % icr_3x if icr_3x != float("inf") else "N/A"
        icr_ebitda = ds.get("icr_ebitda_minus_30pct", 0)
        icr_ebitda_str = "%.1fx" % icr_ebitda if icr_ebitda != float("inf") else "N/A"

        rows = [
            ["Total Debt", "Rs. %.0f Cr" % ds.get("total_debt_cr", 0)],
            ["Net Debt", "Rs. %.0f Cr" % ds.get("net_debt_cr", 0)],
            ["Cash & Equivalents", "Rs. %.0f Cr" % ds.get("cash_cr", 0)],
            ["Net Debt / Equity", fmt_num(ds.get("net_debt_equity", 0), 2)],
            ["Debt / EBITDA", "%.1fx" % ds.get("debt_ebitda", 0) if not _nan(ds.get("debt_ebitda", 0)) else "N/A"],
            ["Short-term Debt %", "%.0f%%" % ds.get("short_term_pct", 0)],
            ["", ""],
            ["ICR (Current)", icr_str],
            ["ICR (If rates 2x)", icr_2x_str],
            ["ICR (If rates 3x)", icr_3x_str],
            ["ICR (EBITDA -30%)", icr_ebitda_str],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[100, 80])

        if ds.get("is_net_cash"):
            self.ln(2)
            self._metric("Status", "NET CASH — Zero debt risk", C_GREEN)

    def add_deep_moat_section(self):
        """Competitive moat scoring."""
        moat = self.deep_analyzer.results.get("moat_score", {})
        if not moat:
            return
        self._check_page_break(90)
        self._section("COMPETITIVE MOAT ASSESSMENT")
        self._intro(
            "What it is: A 0-10 score of the company's durable competitive advantage, "
            "derived from sustained high ROCE, gross margins, market position and "
            "pricing power. Higher = harder for rivals to erode profitability."
        )
        self.set_font("Calibri", "", 9)

        score = moat.get("score", 0)
        max_sc = moat.get("max_score", 10)
        moat_type = moat.get("moat_type", "N/A")

        color = C_GREEN if score >= 7 else (C_YELLOW if score >= 4 else C_RED)
        self._score_box("Moat Score: %d/%d" % (score, max_sc), moat_type, color)
        self.ln(5)

        # Component breakdown
        components = moat.get("components", {})
        if components:
            rows = [[k, v] for k, v in components.items()]
            self._table(["Factor", "Assessment"], rows, col_widths=[80, 110])

        # Moat signals
        signals = self.deep_analyzer.moat_signals
        if signals:
            self.ln(3)
            self._subsection("Moat Signals Detected")
            for sig in signals[:8]:
                self.set_font("Calibri", "", 8)
                self.set_text_color(*C_GREEN)
                self.multi_cell(0, 4, _latin("+ " + sig), new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(*C_DARK)

    def add_deep_quarterly_momentum_section(self):
        """Quarterly momentum section."""
        qm = self.deep_analyzer.results.get("quarterly_momentum", {})
        if qm.get("status") != "computed":
            return
        self._check_page_break(50)
        self._section("QUARTERLY MOMENTUM")
        self._intro(
            "What it is: Is the business accelerating or decelerating right now? Compares "
            "the latest quarter's YoY revenue and PAT growth to recent quarters to detect "
            "a turn before it shows up in annual numbers."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Revenue YoY Growth", fmt_pct(qm.get("revenue_yoy_pct", 0))],
            ["PAT YoY Growth", fmt_pct(qm.get("pat_yoy_pct", 0))],
            ["Growth Momentum", qm.get("acceleration", "N/A").upper()],
            ["Quarters Analyzed", str(qm.get("quarters_analyzed", 0))],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[100, 80])

    def add_deep_concall_nlp_section(self):
        """Concall NLP analysis section."""
        cn = self.deep_analyzer.results.get("concall_nlp", {})
        if cn.get("status") != "analyzed":
            return
        self._check_page_break(100)
        self._section("CONCALL TRANSCRIPT ANALYSIS (NLP)")
        self._intro(
            "What it is: NLP scoring of management's tone across earnings concalls. "
            "Sentiment > 0 = optimistic, < 0 = cautious. Trend matters more than the "
            "absolute number; a downshift quarter-over-quarter is a leading warning sign."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Transcripts Analyzed", str(cn.get("n_transcripts", 0))],
            ["Average Sentiment Score", "%.2f (-1 to +1)" % cn.get("avg_sentiment", 0)],
            ["Sentiment Trend", cn.get("sentiment_trend", "N/A").upper()],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[100, 80])

        # Per-quarter breakdown
        qr = cn.get("quarterly_results", [])
        if qr:
            self.ln(3)
            self._subsection("Quarter-wise Sentiment")
            q_rows = []
            for q in qr[:6]:
                q_rows.append([
                    q.get("quarter", "?"),
                    "%.2f" % q.get("sentiment_score", 0),
                    str(q.get("positive_mentions", 0)),
                    str(q.get("negative_mentions", 0)),
                    "%.0f%%" % (q.get("confidence_ratio", 0) * 100),
                ])
            self._table(["Quarter", "Sentiment", "+ve", "-ve", "Confidence"],
                        q_rows, col_widths=[35, 30, 25, 25, 35])

        # Key guidance
        guidance = cn.get("key_guidance", [])
        if guidance:
            self.ln(3)
            self._subsection("Key Forward Guidance Statements")
            for g in guidance[:5]:
                self.set_font("Calibri", "", 7)
                self.multi_cell(0, 3.5, _latin(">> " + g[:200]), new_x="LMARGIN", new_y="NEXT")

        # Key risks from concalls
        risks = cn.get("key_risks", [])
        if risks:
            self.ln(3)
            self._subsection("Risk Mentions from Concalls")
            for r in risks[:5]:
                self.set_font("Calibri", "", 7)
                self.set_text_color(*C_RED)
                self.multi_cell(0, 3.5, _latin("!! " + r[:200]), new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(*C_DARK)

    def add_deep_annual_report_nlp_section(self):
        """Annual report NLP section."""
        ar = self.deep_analyzer.results.get("annual_report_nlp", {})
        if ar.get("status") != "analyzed":
            return
        self._check_page_break(70)
        self._section("ANNUAL REPORT ANALYSIS (NLP)")
        self._intro(
            "What it is: Automated scan of annual reports for governance red flags - "
            "auditor caveats, related-party transactions, contingent liabilities and "
            "accounting-policy changes. 'YES' on any of these warrants deeper reading."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Years Analyzed", str(ar.get("years_analyzed", 0))],
            ["Auditor Concerns Found", "YES" if ar.get("has_auditor_concerns") else "No"],
            ["High Related Party Txns", "YES" if ar.get("high_related_party") else "No"],
            ["Accounting Policy Changes", "YES" if ar.get("has_policy_changes") else "No"],
        ]
        self._table(["Check", "Result"], rows, col_widths=[100, 80])

        # Year-wise details
        yearly = ar.get("yearly_results", [])
        if yearly:
            self.ln(3)
            y_rows = []
            for yr in yearly:
                y_rows.append([
                    str(yr.get("year", "?")),
                    str(yr.get("related_party_mentions", 0)),
                    str(yr.get("contingent_mentions", 0)),
                    str(yr.get("auditor_concerns", 0)),
                    str(yr.get("policy_changes", 0)),
                ])
            self._table(["Year", "RPT Mentions", "Contingent", "Auditor", "Policy Chg"],
                        y_rows, col_widths=[30, 35, 35, 35, 35])

    def add_deep_risk_section(self):
        """Risk assessment section."""
        ra = self.deep_analyzer.results.get("risk_assessment", {})
        if not ra:
            return
        self._check_page_break(60)
        self._section("COMPREHENSIVE RISK ASSESSMENT")
        self._intro(
            "What it is: A 0-10 aggregate risk score combining financial, operational, "
            "governance and market risks. Below 4 = low risk; 4-7 = moderate; above 7 = "
            "high. Use the breakdown to see which risk type dominates."
        )
        self.set_font("Calibri", "", 9)

        risk_sc = ra.get("risk_score", 5)
        category = ra.get("risk_category", "N/A")
        color = C_RED if risk_sc >= 7 else (C_YELLOW if risk_sc >= 4 else C_GREEN)
        self._score_box("Risk Score: %.1f/10" % risk_sc, category, color)
        self.ln(3)

        breakdown = ra.get("breakdown", {})
        if breakdown:
            rows = [[k, v] for k, v in breakdown.items()]
            self._table(["Risk Type", "Level"], rows, col_widths=[80, 110])

        # Risk factors list
        risks = self.deep_analyzer.risk_factors
        if risks:
            self.ln(3)
            self._subsection("Identified Risk Factors")
            for rf in risks[:8]:
                self.set_font("Calibri", "", 8)
                self.set_text_color(*C_RED)
                self.multi_cell(0, 4, _latin("- " + rf), new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(*C_DARK)

    def add_deep_score_section(self):
        """Deep fundamental score summary page."""
        ds = self.deep_analyzer.results.get("deep_score", {})
        if not ds:
            return
        self.add_page()
        self._section("DEEP FUNDAMENTAL SCORE")
        self._intro(
            "What it is: The blended 0-100 score from valuation, quality, growth, "
            "earnings quality and risk - each weighted as shown below. This is the "
            "single number to glance at; the recommendation is derived directly from it."
        )
        self.set_font("Calibri", "", 9)

        final = ds.get("final_score", 0)
        rec = ds.get("recommendation", "N/A")
        detail = ds.get("rec_detail", "")

        # Score box
        if final >= 60:
            color = C_GREEN
        elif final >= 40:
            color = C_YELLOW
        else:
            color = C_RED
        self._score_box("Score: %.0f / 100" % final, rec, color)
        self.ln(3)
        self.set_font("Calibri", "I", 9)
        self.multi_cell(0, 4, _latin(detail), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        # Component scores
        scores = ds.get("component_scores", {})
        weights = ds.get("weights", {})
        if scores:
            rows = []
            for k in ["valuation", "quality", "growth", "earnings_quality", "risk"]:
                label = k.replace("_", " ").title()
                sc = scores.get(k, 0)
                wt = weights.get(k, 0) * 100
                rows.append([label, "%.0f / 100" % sc, "%.0f%%" % wt, "%.1f" % (sc * wt / 100)])
            rows.append(["TOTAL", "%.0f / 100" % final, "100%", "%.1f" % final])
            self._table(["Component", "Score", "Weight", "Weighted"], rows,
                        col_widths=[55, 40, 30, 40])

    # ── EXTENDED PDF SECTIONS ────────────────────────────────────────────────

    def add_deep_shareholding_section(self):
        """Shareholding pattern trend section."""
        dr = self.deep_analyzer.results.get("shareholding_trend", {})
        if dr.get("status") != "computed":
            return
        self._check_page_break(90)
        self._section("SHAREHOLDING PATTERN TREND")
        self._intro(
            "What it is: Quarter-on-quarter movement in promoter and public holding. "
            "Rising promoter stake signals insider confidence; falling stake (especially "
            "alongside pledges) is a major red flag."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Current Promoter Holding", "%.1f%%" % dr["latest_promoter_pct"]],
            ["Current Public Holding", "%.1f%%" % dr["latest_public_pct"]],
            ["Promoter Change (1Y)", "%+.1f%%" % dr["promoter_change_1y_pct"]],
            ["Promoter Change (QoQ)", "%+.1f%%" % dr["promoter_change_qoq_pct"]],
            ["Promoter Trend", dr["promoter_trend"].upper()],
            ["Quarters Tracked", str(dr["quarters_tracked"])],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[80, 80])

        # Quarterly series table
        series = dr.get("series", [])
        if series:
            self.ln(3)
            self._subsection("Quarterly History")
            qrows = [[s["date"][:11], "%.1f%%" % s["promoter"], "%.1f%%" % s["public"]] for s in series]
            self._table(["Quarter", "Promoter %", "Public %"], qrows, col_widths=[60, 50, 50])

    def add_deep_insider_trading_section(self):
        """Insider/SAST trading section."""
        dr = self.deep_analyzer.results.get("insider_trading", {})
        if dr.get("status") != "computed":
            return
        self._check_page_break(60)
        self._section("INSIDER TRADING (SAST DISCLOSURES)")
        self._intro(
            "What it is: SEBI-mandated disclosures of share dealings by promoters and "
            "key insiders. Net buying = insider confidence; net selling, especially "
            "clustered, deserves scrutiny against business fundamentals."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Total Disclosures", str(dr["total_disclosures"])],
            ["Buys", str(dr["buys"])],
            ["Sells", str(dr["sells"])],
            ["Net Sentiment", dr["net_sentiment"]],
            ["Total Buy Shares", "{:,}".format(dr["total_buy_shares"])],
            ["Total Sell Shares", "{:,}".format(dr["total_sell_shares"])],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[80, 80])

        # Recent actions
        recent = dr.get("recent_actions", [])
        if recent:
            self.ln(3)
            self._subsection("Recent Insider Actions")
            arows = [[a["date"][:11], a["action"], "{:,}".format(a["shares"]) if a["shares"] else "N/A"] for a in recent[:5]]
            self._table(["Date", "Action", "Shares"], arows, col_widths=[50, 40, 70])

    def add_deep_peer_comparison_section(self):
        """Peer comparison section."""
        dr = self.deep_analyzer.results.get("peer_comparison", {})
        if dr.get("status") != "computed":
            return
        self._check_page_break(100)
        self._section("PEER COMPARISON & RELATIVE VALUATION")
        self._intro(
            "What it is: Side-by-side valuation against same-sector peers. A material "
            "discount to peer average PE/PB usually means either undervaluation or "
            "market-recognised weakness; cross-check with the moat and growth sections."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Sector", dr.get("sector", "N/A")],
            ["Sector Index", dr.get("sector_index", "N/A")],
            ["Sector PE", "%.1f" % _parse_float(dr.get("sector_pe", 0)) if not _nan(_parse_float(dr.get("sector_pe", 0))) else "N/A"],
            ["Own PE", "%.1f" % dr.get("own_pe", 0) if not _nan(dr.get("own_pe", 0)) else "N/A"],
            ["Peer Avg PE", "%.1f" % _parse_float(dr.get("avg_peer_pe", 0)) if not _nan(_parse_float(dr.get("avg_peer_pe", 0))) else "N/A"],
            ["PE Discount to Peers", "%+.0f%%" % _parse_float(dr.get("pe_discount_to_peers_pct", 0)) if not _nan(_parse_float(dr.get("pe_discount_to_peers_pct", 0))) else "N/A"],
            ["Own 1Y Return", "%.1f%%" % _parse_float(dr.get("own_1y_return_pct", 0)) if not _nan(_parse_float(dr.get("own_1y_return_pct", 0))) else "N/A"],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[80, 80])

        # Peer table
        peers = dr.get("peers", [])
        if peers:
            self.ln(3)
            self._subsection("Sector Peers")
            prows = [[p["symbol"], "%.1f" % p["pe"] if not _nan(p["pe"]) else "N/A",
                      "%.1f" % p["pb"] if not _nan(p["pb"]) else "N/A",
                      "%.1f%%" % p.get("change_1y", 0)]
                     for p in peers[:8]]
            self._table(["Symbol", "PE", "P/B", "1Y Return"], prows, col_widths=[45, 35, 35, 45])

    def add_deep_relative_strength_section(self):
        """Relative strength vs Nifty section."""
        dr = self.deep_analyzer.results.get("relative_strength", {})
        if dr.get("status") != "computed":
            return
        self._check_page_break(50)
        self._section("RELATIVE STRENGTH VS NIFTY 50")
        self._intro(
            "What it is: How the stock has performed against the Nifty 50 over 1M / 3M / "
            "6M / 1Y. 'Alpha' is the excess return over the index. Persistent positive "
            "alpha = market is rewarding the story."
        )
        self.set_font("Calibri", "", 9)

        periods = dr.get("periods", {})
        rows = []
        for label in ["1M", "3M", "6M", "1Y"]:
            p = periods.get(label, {})
            if p:
                rows.append([label, "%.1f%%" % p["stock_return"], "%.1f%%" % p["nifty_return"],
                            "%+.1f%%" % p["alpha"]])
        if rows:
            self._table(["Period", "Stock Return", "Nifty Return", "Alpha"], rows,
                        col_widths=[35, 45, 45, 40])

        avg_alpha = dr.get("avg_alpha", 0)
        status = "OUTPERFORMING" if avg_alpha > 0 else "UNDERPERFORMING"
        self.ln(3)
        self.set_font("Calibri", "B", 10)
        self.cell(0, 6, _latin("Average Alpha: %+.1f%% | %s" % (avg_alpha, status)),
                  new_x="LMARGIN", new_y="NEXT")

    def add_deep_technical_section(self):
        """Technical structure section."""
        dr = self.deep_analyzer.results.get("technical", {})
        if dr.get("status") != "computed":
            return
        self._check_page_break(60)
        self._section("TECHNICAL STRUCTURE")
        self._intro(
            "What it is: A snapshot of price action - moving averages, RSI, golden cross "
            "and volume/delivery trend. Helps time entries: even a great fundamental "
            "story is best bought when the technical trend confirms."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Current Price (CMP)", "Rs. %.0f" % dr["cmp"]],
            ["50 DMA", "Rs. %.0f" % dr["dma_50"]],
            ["200 DMA", "Rs. %.0f" % dr["dma_200"]],
            ["Distance from 200 DMA", "%+.1f%%" % dr["dist_200dma_pct"]],
            ["RSI (14)", "%.0f" % dr["rsi_14"]],
            ["Trend", dr["trend"]],
            ["Golden Cross (50>200)", "Yes" if dr["golden_cross"] else "No"],
            ["Volume Trend", dr["volume_trend"]],
            ["Delivery %", "%.1f%%" % dr["delivery_pct"] if dr["delivery_pct"] else "N/A"],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[80, 80])

    def add_deep_graham_section(self):
        """Graham Number & Magic Formula section."""
        dr = self.deep_analyzer.results.get("graham_magic", {})
        if not dr:
            return
        self._check_page_break(50)
        self._section("GRAHAM NUMBER & MAGIC FORMULA")
        self._intro(
            "What it is: Two classic value screens. Graham Number = sqrt(22.5 * EPS * "
            "BVPS) is the maximum price a defensive investor should pay. Magic Formula "
            "combines Earnings Yield + ROIC; higher is better on both."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Trailing EPS", "Rs. %.1f" % dr.get("eps", 0)],
            ["Book Value / Share", "Rs. %.1f" % dr.get("bvps", 0)],
            ["Graham Number", "Rs. %.0f" % dr.get("graham_number", 0)],
            ["Graham Upside", "%+.0f%%" % dr.get("graham_upside_pct", 0)],
            ["Earnings Yield", "%.1f%%" % dr.get("earnings_yield_pct", 0)],
            ["ROIC", "%.1f%%" % dr.get("roic_pct", 0)],
            ["PEG Ratio", "%.2f" % dr.get("peg_ratio", 0) if not _nan(dr.get("peg_ratio", 0)) else "N/A"],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[80, 80])

    def add_deep_capex_section(self):
        """Capex cycle analysis section."""
        dr = self.deep_analyzer.results.get("capex_cycle", {})
        if not dr:
            return
        self._check_page_break(50)
        self._section("CAPEX CYCLE & ASSET EFFICIENCY")
        self._intro(
            "What it is: Splits capex into 'maintenance' (just to keep the lights on) and "
            "'growth' (adding capacity). Heavy growth capex paired with rising asset "
            "turnover = expansion paying off; rising capex with falling turnover = warning."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Latest Capex", "Rs. %.0f Cr" % dr.get("capex_latest_cr", 0)],
            ["Maintenance Capex (Depreciation)", "Rs. %.0f Cr" % dr.get("maintenance_capex_cr", 0)],
            ["Growth Capex", "Rs. %.0f Cr" % dr.get("growth_capex_cr", 0)],
            ["Growth Capex %", "%.0f%%" % dr.get("growth_capex_pct", 0)],
            ["Asset Turnover Trend", dr.get("asset_turnover_trend", "N/A").upper()],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[80, 80])

        # Capex intensity series
        series = dr.get("capex_intensity_series", [])
        if series:
            self.ln(3)
            self._subsection("Capex Intensity (% of Revenue)")
            srows = [["Year -%d" % i, "%.1f%%" % v] for i, v in enumerate(series)]
            self._table(["Year", "Capex/Revenue"], srows, col_widths=[60, 60])

    def add_deep_institutional_section(self):
        """Institutional/MF holdings section."""
        dr = self.deep_analyzer.results.get("institutional_holdings", {})
        if dr.get("status") != "computed":
            return
        self._check_page_break(60)
        self._section("INSTITUTIONAL & MUTUAL FUND HOLDINGS")
        self._intro(
            "What it is: How much 'smart money' (FIIs, MFs, insurers) holds the stock and "
            "who the top holders are. Concentrated quality holders = validation; sudden "
            "exits across funds = institutional sell-signal."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Total Institutional %", "%.1f%%" % dr.get("total_institutional_pct", 0)],
            ["Total Mutual Fund %", "%.1f%%" % dr.get("total_mf_pct", 0)],
            ["# Institutional Holders", str(dr.get("n_institutional_holders", 0))],
            ["# MF Holders", str(dr.get("n_mf_holders", 0))],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[80, 80])

        # Top holders
        top5 = dr.get("top_5_institutional", [])
        if top5:
            self.ln(3)
            self._subsection("Top Institutional Holders")
            hrows = [[h["holder"][:40], "%.2f%%" % h["pct_out"]] for h in top5]
            self._table(["Holder", "% Holding"], hrows, col_widths=[120, 40])

        top5mf = dr.get("top_5_mf", [])
        if top5mf:
            self.ln(3)
            self._subsection("Top Mutual Fund Holders")
            mrows = [[h["holder"][:40], "%.2f%%" % h["pct_out"]] for h in top5mf]
            self._table(["Fund", "% Holding"], mrows, col_widths=[120, 40])

    def add_deep_corporate_actions_section(self):
        """Corporate actions history section."""
        dr = self.deep_analyzer.results.get("corporate_actions", {})
        if dr.get("status") != "computed":
            return
        self._check_page_break(50)
        self._section("CORPORATE ACTIONS HISTORY")
        self._intro(
            "What it is: Track-record of dividends, splits and bonuses. A long, growing "
            "dividend record signals consistent cash generation and shareholder-friendly "
            "capital allocation."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Total Dividends", str(dr.get("dividend_count", 0))],
            ["Years with Dividend", str(dr.get("years_with_dividend", 0))],
            ["Total Div/Share (lifetime)", "Rs. %.1f" % dr.get("total_dividend_per_share", 0)],
            ["Avg Dividend/Share", "Rs. %.1f" % dr.get("avg_dividend_per_share", 0)],
            ["Dividend Growth", "%.0f%%" % dr.get("dividend_growth_pct", 0)],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[80, 80])

        # Recent dividends
        recent = dr.get("recent_dividends", [])
        if recent:
            self.ln(3)
            self._subsection("Recent Dividends")
            drows = [[d["date"], "Rs. %.1f" % d["amount"]] for d in recent[:5]]
            self._table(["Date", "Amount/Share"], drows, col_widths=[60, 60])

        # Splits
        splits = dr.get("splits", [])
        if splits:
            self.ln(3)
            self._subsection("Stock Splits")
            srows = [[s["date"], s["ratio"]] for s in splits]
            self._table(["Date", "Ratio"], srows, col_widths=[60, 60])

    def add_deep_credit_intelligence_section(self):
        """Enhanced credit rating intelligence section."""
        dr = self.deep_analyzer.results.get("credit_intelligence", {})
        if dr.get("status") != "computed":
            return
        self._check_page_break(50)
        self._section("CREDIT RATING INTELLIGENCE")
        self._intro(
            "What it is: Combined view of all credit-rating agencies covering the company "
            "plus their trajectory (upgrades / downgrades / stable). Investment-grade = "
            "BBB- and above; below that, debt repayment capacity is materially weaker."
        )
        self.set_font("Calibri", "", 9)

        rows = [
            ["Agencies Covering", str(dr.get("n_agencies", 0))],
            ["Rating Trajectory", dr.get("trajectory", "N/A")],
            ["Investment Grade", "Yes" if dr.get("is_investment_grade") else "No"],
            ["Latest Score", str(dr.get("latest_score", 0))],
        ]
        self._table(["Metric", "Value"], rows, col_widths=[80, 80])

        # Agency breakdown
        agencies = dr.get("agencies", {})
        if agencies:
            self.ln(3)
            self._subsection("Agency Breakdown")
            arows = []
            for agency, info in agencies.items():
                ratings_str = ", ".join(info.get("latest_ratings", [])) or "N/A"
                arows.append([agency, ratings_str, info.get("outlook", "N/A"), info.get("date", "")[:11]])
            self._table(["Agency", "Ratings", "Outlook", "Date"], arows, col_widths=[40, 50, 35, 40])

    def add_deep_order_book_section(self):
        """Order book position & inflow YoY/QoQ section."""
        dr = self.deep_analyzer.results.get("order_book", {})
        if dr.get("status") != "computed":
            return
        book = dr.get("book", {})
        inflow = dr.get("inflow", {})
        if not book and not inflow:
            return
        self._check_page_break(80)
        self._section("ORDER BOOK & INFLOW ANALYSIS")
        self._intro(
            "What it is: Order-book position and inflow data extracted from the "
            "company's press releases and investor presentations filed with NSE. "
            "Growing order books signal strong revenue visibility; declining books "
            "are an early warning for future revenue slowdowns. Not all companies "
            "report this metric \u2014 it is most common in defence, capital goods, "
            "infra, and EPC sectors."
        )
        self.set_font("Calibri", "", 9)

        # Summary metrics
        rows = []
        if book:
            rows.append(["Order Book (Latest)",
                         fmt_cr(book["latest_value"] * 1e7) if book.get("latest_value") else "N/A"])
            rows.append(["  as of", book.get("latest_date", "N/A")])
            if book.get("yoy_pct") is not None:
                rows.append(["  YoY Change", fmt_pct(book["yoy_pct"])])
            if book.get("qoq_pct") is not None:
                rows.append(["  QoQ Change", fmt_pct(book["qoq_pct"])])
            rows.append(["  Data Points", str(book.get("n_datapoints", 0))])
        if inflow:
            rows.append(["Order Inflow (Latest)",
                         fmt_cr(inflow["latest_value"] * 1e7) if inflow.get("latest_value") else "N/A"])
            rows.append(["  as of", inflow.get("latest_date", "N/A")])
            if inflow.get("yoy_pct") is not None:
                rows.append(["  YoY Change", fmt_pct(inflow["yoy_pct"])])
            rows.append(["  Data Points", str(inflow.get("n_datapoints", 0))])
        if rows:
            self._table(["Metric", "Value"], rows, col_widths=[80, 80])

        # Historical order book table
        entries = book.get("entries", []) or inflow.get("entries", [])
        if entries:
            self.ln(3)
            self._subsection("Order Book History")
            hrows = []
            for e in entries[:8]:
                hrows.append([
                    e.get("date", "")[:11],
                    e.get("type", "").title(),
                    fmt_cr(e["value_crore"] * 1e7) if e.get("value_crore") else "N/A",
                    e.get("source", "")[:30],
                ])
            self._table(["Date", "Type", "Value", "Source"], hrows,
                        col_widths=[35, 25, 50, 55])

    # ── PEER COMPARISON SECTION (user-specified companies) ───────────────────

    def add_user_comparison_section(self):
        """Add a comprehensive comparison table with user-specified companies."""
        comp_data = getattr(self.data, "comparison_data", None)
        if not comp_data:
            return

        self.add_page()
        self._section("COMPARATIVE ANALYSIS")
        self._intro(
            "Side-by-side comparison of key financial metrics across user-specified "
            "companies. This helps assess relative valuation, profitability, growth, "
            "and financial health. All data sourced from latest available filings via yfinance."
        )

        # --- Valuation Metrics ---
        self._subsection("Valuation Metrics")
        headers = ["Metric"] + [c["symbol"] for c in comp_data]
        col_w = min(30, int(160 / len(comp_data)))
        col_widths = [40] + [col_w] * len(comp_data)
        align = ["L"] + ["C"] * len(comp_data)

        val_rows = []
        for metric, key, fmt in [
            ("Market Cap (Cr)", "market_cap_cr", "%.0f"),
            ("Trailing P/E", "trailing_pe", "%.1f"),
            ("Forward P/E", "forward_pe", "%.1f"),
            ("P/B Ratio", "pb", "%.2f"),
            ("EV/EBITDA", "ev_ebitda", "%.1f"),
            ("P/S Ratio", "ps", "%.2f"),
            ("Dividend Yield %", "div_yield_pct", "%.2f"),
        ]:
            row = [metric]
            for c in comp_data:
                v = c.get(key)
                row.append(fmt % v if v and not _nan(v) else "N/A")
            val_rows.append(row)
        self._table(headers, val_rows, col_widths=col_widths, align=align)

        # --- Profitability Metrics ---
        self._check_page_break(60)
        self._subsection("Profitability & Returns")
        prof_rows = []
        for metric, key, fmt in [
            ("ROE %", "roe_pct", "%.1f"),
            ("ROA %", "roa_pct", "%.1f"),
            ("ROCE %", "roce_pct", "%.1f"),
            ("Operating Margin %", "opm_pct", "%.1f"),
            ("Net Profit Margin %", "npm_pct", "%.1f"),
            ("EPS (Rs)", "eps", "%.1f"),
        ]:
            row = [metric]
            for c in comp_data:
                v = c.get(key)
                row.append(fmt % v if v and not _nan(v) else "N/A")
            prof_rows.append(row)
        self._table(headers, prof_rows, col_widths=col_widths, align=align)

        # --- Growth Metrics ---
        self._check_page_break(50)
        self._subsection("Growth")
        growth_rows = []
        for metric, key, fmt in [
            ("Revenue Growth %", "rev_growth_pct", "%.1f"),
            ("Earnings Growth %", "earn_growth_pct", "%.1f"),
            ("Revenue 3Y CAGR %", "rev_cagr_3y_pct", "%.1f"),
            ("Profit 3Y CAGR %", "pat_cagr_3y_pct", "%.1f"),
        ]:
            row = [metric]
            for c in comp_data:
                v = c.get(key)
                row.append(fmt % v if v and not _nan(v) else "N/A")
            growth_rows.append(row)
        self._table(headers, growth_rows, col_widths=col_widths, align=align)

        # --- Financial Health ---
        self._check_page_break(50)
        self._subsection("Financial Health")
        health_rows = []
        for metric, key, fmt in [
            ("Debt/Equity", "de_ratio", "%.2f"),
            ("Current Ratio", "current_ratio", "%.2f"),
            ("Interest Coverage", "interest_coverage", "%.1f"),
            ("Promoter Holding %", "promoter_pct", "%.1f"),
            ("Pledged %", "pledged_pct", "%.1f"),
        ]:
            row = [metric]
            for c in comp_data:
                v = c.get(key)
                row.append(fmt % v if v and not _nan(v) else "N/A")
            health_rows.append(row)
        self._table(headers, health_rows, col_widths=col_widths, align=align)

        # --- Price Performance ---
        self._check_page_break(50)
        self._subsection("Price Performance")
        perf_rows = []
        for metric, key, fmt in [
            ("CMP (Rs)", "cmp", "%.0f"),
            ("52W High (Rs)", "high_52w", "%.0f"),
            ("52W Low (Rs)", "low_52w", "%.0f"),
            ("1Y Return %", "return_1y_pct", "%.1f"),
            ("Beta", "beta", "%.2f"),
        ]:
            row = [metric]
            for c in comp_data:
                v = c.get(key)
                row.append(fmt % v if v and not _nan(v) else "N/A")
            perf_rows.append(row)
        self._table(headers, perf_rows, col_widths=col_widths, align=align)

        # --- Summary verdict ---
        self._check_page_break(30)
        self._subsection("Quick Comparison Verdict")
        self.set_font("Calibri", "", 9)
        # Find cheapest PE, highest ROE, etc.
        valid_pe = [(c["symbol"], c.get("trailing_pe", float("inf")))
                    for c in comp_data if c.get("trailing_pe") and not _nan(c.get("trailing_pe", float("nan")))]
        valid_roe = [(c["symbol"], c.get("roe_pct", 0))
                     for c in comp_data if c.get("roe_pct") and not _nan(c.get("roe_pct", float("nan")))]
        valid_growth = [(c["symbol"], c.get("rev_growth_pct", 0))
                        for c in comp_data if c.get("rev_growth_pct") and not _nan(c.get("rev_growth_pct", float("nan")))]

        verdicts = []
        if valid_pe:
            cheapest = min(valid_pe, key=lambda x: x[1])
            verdicts.append("Cheapest P/E: %s (%.1f)" % cheapest)
        if valid_roe:
            best_roe = max(valid_roe, key=lambda x: x[1])
            verdicts.append("Highest ROE: %s (%.1f%%)" % best_roe)
        if valid_growth:
            fastest = max(valid_growth, key=lambda x: x[1])
            verdicts.append("Fastest Revenue Growth: %s (%.1f%%)" % fastest)

        for v in verdicts:
            self.cell(0, 5, _latin("  * %s" % v), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)


# ══════════════════════════════════════════════════════════════════════════════
# COMPARISON DATA FETCHER
# ══════════════════════════════════════════════════════════════════════════════

def fetch_comparison_metrics(symbols):
    """Fetch key financial metrics for a list of symbols for comparison.
    Returns list of dicts with standardised metrics per company.
    """
    print("\n  Fetching comparison data for: %s" % ", ".join(symbols))
    results = []

    for sym in symbols:
        sym = sym.strip().upper()
        print("    %s..." % sym, end=" ")
        try:
            ticker, yf_sym, resolved_info, _is_sme = _resolve_yf_ticker(sym)
            info = ticker.info or {}

            # Market cap in crore
            mkt_cap = info.get("marketCap", 0) or 0
            mkt_cap_cr = mkt_cap / 1e7 if mkt_cap else None

            # Valuation
            trailing_pe = info.get("trailingPE")
            forward_pe = info.get("forwardPE")
            pb = info.get("priceToBook")
            ev_ebitda = info.get("enterpriseToEbitda")
            ps = info.get("priceToSalesTrailing12Months")
            div_yield = info.get("dividendYield")
            div_yield_pct = div_yield * 100 if div_yield else None

            # Profitability
            roe = info.get("returnOnEquity")
            roe_pct = roe * 100 if roe else None
            roa = info.get("returnOnAssets")
            roa_pct = roa * 100 if roa else None
            opm = info.get("operatingMargins")
            opm_pct = opm * 100 if opm else None
            npm = info.get("profitMargins")
            npm_pct = npm * 100 if npm else None
            eps = info.get("trailingEps")

            # Growth
            rev_growth = info.get("revenueGrowth")
            rev_growth_pct = rev_growth * 100 if rev_growth else None
            earn_growth = info.get("earningsGrowth")
            earn_growth_pct = earn_growth * 100 if earn_growth else None

            # Financial health
            de_ratio = info.get("debtToEquity")
            de_ratio_val = de_ratio / 100 if de_ratio else None  # yfinance gives in %
            current_ratio = info.get("currentRatio")

            # Price performance
            cmp = info.get("currentPrice") or info.get("regularMarketPrice")
            high_52w = info.get("fiftyTwoWeekHigh")
            low_52w = info.get("fiftyTwoWeekLow")
            beta = info.get("beta")

            # Promoter holding (from yfinance majorHolders if available)
            promoter_pct = None
            pledged_pct = None
            try:
                holders = ticker.major_holders
                if holders is not None and not holders.empty:
                    for _, row in holders.iterrows():
                        desc = str(row.iloc[1]).lower() if len(row) > 1 else ""
                        if "insider" in desc or "promoter" in desc:
                            promoter_pct = float(row.iloc[0])
            except Exception:
                pass

            # ROCE — compute from financials if possible
            roce_pct = None
            try:
                inc = ticker.financials
                bs = ticker.balance_sheet
                if inc is not None and bs is not None and not inc.empty and not bs.empty:
                    ebit = None
                    for label in ["EBIT", "Operating Income"]:
                        if label in inc.index:
                            ebit = inc.loc[label].iloc[0]
                            break
                    total_assets = None
                    current_liab = None
                    for label in ["Total Assets"]:
                        if label in bs.index:
                            total_assets = bs.loc[label].iloc[0]
                    for label in ["Current Liabilities", "Total Current Liabilities"]:
                        if label in bs.index:
                            current_liab = bs.loc[label].iloc[0]
                            break
                    if ebit and total_assets and current_liab:
                        ce = total_assets - current_liab
                        if ce > 0:
                            roce_pct = (ebit / ce) * 100
            except Exception:
                pass

            # Interest coverage
            interest_coverage = None
            try:
                inc = ticker.financials
                if inc is not None and not inc.empty:
                    ebit = None
                    interest = None
                    for label in ["EBIT", "Operating Income"]:
                        if label in inc.index:
                            ebit = inc.loc[label].iloc[0]
                            break
                    for label in ["Interest Expense"]:
                        if label in inc.index:
                            interest = abs(inc.loc[label].iloc[0])
                            break
                    if ebit and interest and interest > 0:
                        interest_coverage = ebit / interest
            except Exception:
                pass

            # Revenue & PAT 3Y CAGR
            rev_cagr_3y_pct = None
            pat_cagr_3y_pct = None
            try:
                inc = ticker.financials
                if inc is not None and inc.shape[1] >= 4:
                    rev_now = None
                    rev_3y = None
                    pat_now = None
                    pat_3y = None
                    for label in ["Total Revenue", "Operating Revenue", "Revenue"]:
                        if label in inc.index:
                            rev_now = inc.loc[label].iloc[0]
                            rev_3y = inc.loc[label].iloc[3]
                            break
                    for label in ["Net Income", "Net Income Common Stockholders"]:
                        if label in inc.index:
                            pat_now = inc.loc[label].iloc[0]
                            pat_3y = inc.loc[label].iloc[3]
                            break
                    if rev_now and rev_3y and rev_3y > 0:
                        rev_cagr_3y_pct = ((rev_now / rev_3y) ** (1/3) - 1) * 100
                    if pat_now and pat_3y and pat_3y > 0:
                        pat_cagr_3y_pct = ((pat_now / pat_3y) ** (1/3) - 1) * 100
            except Exception:
                pass

            # 1Y Return
            return_1y_pct = None
            try:
                hist = ticker.history(period="1y")
                if hist is not None and len(hist) > 20:
                    p_start = hist["Close"].iloc[0]
                    p_end = hist["Close"].iloc[-1]
                    if p_start > 0:
                        return_1y_pct = ((p_end / p_start) - 1) * 100
            except Exception:
                pass

            entry = {
                "symbol": sym,
                "name": info.get("shortName", sym),
                "market_cap_cr": mkt_cap_cr,
                "trailing_pe": trailing_pe,
                "forward_pe": forward_pe,
                "pb": pb,
                "ev_ebitda": ev_ebitda,
                "ps": ps,
                "div_yield_pct": div_yield_pct,
                "roe_pct": roe_pct,
                "roa_pct": roa_pct,
                "roce_pct": roce_pct,
                "opm_pct": opm_pct,
                "npm_pct": npm_pct,
                "eps": eps,
                "rev_growth_pct": rev_growth_pct,
                "earn_growth_pct": earn_growth_pct,
                "rev_cagr_3y_pct": rev_cagr_3y_pct,
                "pat_cagr_3y_pct": pat_cagr_3y_pct,
                "de_ratio": de_ratio_val,
                "current_ratio": current_ratio,
                "interest_coverage": interest_coverage,
                "promoter_pct": promoter_pct,
                "pledged_pct": pledged_pct,
                "cmp": cmp,
                "high_52w": high_52w,
                "low_52w": low_52w,
                "return_1y_pct": return_1y_pct,
                "beta": beta,
            }
            results.append(entry)
            print("OK (Mkt Cap: %.0f Cr)" % (mkt_cap_cr or 0))

        except Exception as e:
            print("FAILED (%s)" % e)
            results.append({"symbol": sym, "name": sym})

    return results


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run(symbol=None, documents_dir=None, compare=None):
    """Run the complete forensic + deep fundamental analysis.
    
    Args:
        symbol: NSE symbol (e.g. 'RELIANCE'). Defaults to COMPANY_SYMBOL.
        documents_dir: Optional path to a folder containing concall PDFs
                       and annual report PDFs for NLP analysis.
        compare: List of NSE symbols to include in comparative analysis section.
    """
    if symbol is None:
        symbol = COMPANY_SYMBOL

    symbol = symbol.strip().upper()

    print("=" * 64)
    print("  FORENSIC + DEEP FUNDAMENTAL ANALYSIS")
    print("  Symbol: %s" % symbol)
    if compare:
        print("  Compare: %s" % ", ".join(compare))
    print("  Date  : %s" % datetime.datetime.now().strftime("%d-%b-%Y %H:%M"))
    print("=" * 64)

    # Step 1: Fetch data
    data = fetch_financial_data(symbol)

    has_any_data = (
        data.years >= 1
        or (data.quarters or 0) >= 1
        or (data.historical_prices is not None and len(data.historical_prices) >= 30)
        or bool(data.info.get("longName") or data.info.get("shortName"))
    )
    if not has_any_data:
        print("\nCannot proceed — no usable data found for '%s'." % symbol)
        print("Tried .NS, .BO and yfinance search — no financials, no quarterly,")
        print("no price history and no company info were returned.")
        return
    if data.years < 2:
        print("\nProceeding with limited data (%d annual yr(s), %d qtr(s), %s price days)." % (
            data.years, data.quarters or 0,
            len(data.historical_prices) if data.historical_prices is not None else 0))
        print("Some forensic checks (Beneish, Piotroski trends) need t vs t-1 and will")
        print("degrade gracefully to neutral defaults where prior-year values are missing.")

    # Step 2: Forensic Analysis
    analyzer = ForensicAnalyzer(data)
    results = analyzer.run_all()

    # Step 3: Auto-fetch extended data (concalls, investor pres, shareholding, peers, etc.)
    if not documents_dir:
        data.concall_texts = fetch_concall_transcripts(symbol)
        data.annual_report_texts = fetch_investor_presentations(symbol)

    data.shareholding_quarterly = fetch_shareholding_history(symbol)
    data.sast_disclosures = fetch_sast_disclosures(symbol)
    data.delivery_data = fetch_delivery_data(symbol)
    data.sector_peers = fetch_sector_peers(symbol)
    data.corporate_actions = fetch_corporate_actions(symbol)
    data.related_party_filings = fetch_related_party_filings(symbol)
    data.mf_institutional_data = fetch_mutual_fund_data(symbol)

    # Filings library (independent of yfinance — works for SME via index=sme retry)
    data.financial_results_filings = fetch_financial_results_filings(symbol)
    data.annual_report_filings = fetch_annual_report_filings(symbol)
    data.filings_summary = {
        "concalls": len(data.concall_texts or []),
        "investor_pres": len(data.annual_report_texts or []),
        "financial_results": len(data.financial_results_filings or []),
        "annual_reports": len(data.annual_report_filings or []),
        "sast": len(data.sast_disclosures or []),
        "rpt": len(data.related_party_filings or []),
        "credit_ratings": len(data.credit_ratings or []),
    }
    print("\n  Filings library summary: %s" % ", ".join(
        "%s=%d" % (k, v) for k, v in data.filings_summary.items() if v))

    # If yfinance gave us nothing numeric, try parsing the latest results PDF for revenue/PAT
    if data.years == 0 and data.financial_results_filings:
        _augment_financials_from_filings(data)

    # Order book history (press releases + investor presentations)
    data.order_book_history = fetch_order_book_from_filings(symbol)

    # Comparative analysis (user-specified peer companies)
    if compare:
        all_compare_symbols = [symbol] + [s.strip().upper() for s in compare if s.strip()]
        data.comparison_data = fetch_comparison_metrics(all_compare_symbols)
    else:
        data.comparison_data = None

    # Step 4: Deep Fundamental Analysis
    deep_analyzer = DeepFundamentalAnalyzer(data, forensic_analyzer=analyzer)
    deep_results = deep_analyzer.run_all(documents_dir=documents_dir)
    data.deep_results = deep_results

    # Step 5: Generate PDF
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_name = "forensic_report_%s_%s.pdf" % (symbol, timestamp)
    pdf_path = os.path.join(SCRIPT_DIR, pdf_name)

    report = ForensicReport(symbol, data, analyzer)
    report.deep_analyzer = deep_analyzer
    report.generate(pdf_path)

    # Step 6: Console summary
    print("\n" + "=" * 64)
    print("  ANALYSIS COMPLETE")
    print("=" * 64)
    overall = results.get("overall", {})
    deep_sc = deep_results.get("deep_score", {})
    print("  Forensic Score    : %.0f / 100" % overall.get("final_score", 0))
    print("  Deep Fund. Score  : %.0f / 100" % deep_sc.get("final_score", 0))
    print("  Recommendation    : %s" % deep_sc.get("recommendation", overall.get("recommendation", "N/A")))
    print("  Red Flags         : %d" % len(analyzer.red_flags))
    print("  Green Flags       : %d" % len(analyzer.green_flags))
    print("  Moat Signals      : %d" % len(deep_analyzer.moat_signals))
    print("  Risk Factors      : %d" % len(deep_analyzer.risk_factors))
    print("  Report            : %s" % pdf_path)
    print("=" * 64)

    return results, deep_results, pdf_path


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Forensic Accounting Report Generator",
        usage="python3 forensic_accounting.py SYMBOL [--compare PEER1,PEER2,...]",
    )
    parser.add_argument("symbol", nargs="?", default=None,
                        help="NSE symbol (e.g. TCS, RELIANCE). "
                             "Defaults to COMPANY_SYMBOL in script.")
    parser.add_argument("--compare", "-c", type=str, default=None,
                        help="Comma-separated list of peer symbols for comparative analysis "
                             "(e.g. --compare DANISH,VOLTAMP,INDOTECH,SHILCTECH)")
    args = parser.parse_args()

    compare_list = None
    if args.compare:
        compare_list = [s.strip() for s in args.compare.split(",") if s.strip()]

    run(symbol=args.symbol, compare=compare_list)
