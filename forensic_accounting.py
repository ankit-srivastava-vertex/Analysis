"""
Forensic Accounting Analysis Tool
===================================
Deep-dive financial forensic analysis of any listed Indian company.
Pulls 3+ years of financial data (Balance Sheet, P&L, Cash Flow) from
BSE/NSE via yfinance, computes forensic scores (Beneish M-Score,
Altman Z-Score, Piotroski F-Score, DuPont decomposition), detects
red/green flags, and generates a professional PDF report with
investment recommendation.

Usage:
    1. Set COMPANY_SYMBOL below to the NSE symbol (e.g. "RELIANCE")
    2. Run:  python forensic_accounting.py
    3. PDF report is saved in the same directory.
"""

import os
import sys
import math
import datetime
import warnings

warnings.filterwarnings("ignore")

# ── Auto-install fpdf2 if missing ────────────────────────────────────────────
try:
    from fpdf import FPDF
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "fpdf2"])
    from fpdf import FPDF

import re
import io

import pandas as pd
import yfinance as yf
import requests

try:
    import PyPDF2
except ImportError:
    import subprocess as _sp
    _sp.check_call([sys.executable, "-m", "pip", "install", "PyPDF2"])
    import PyPDF2

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — Change this symbol each time you run the analysis
# ══════════════════════════════════════════════════════════════════════════════
COMPANY_SYMBOL = "POWERMECH"       # NSE symbol (e.g. RELIANCE, TCS, INFY, HDFCBANK)
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


def fetch_financial_data(symbol):
    """Fetch all financial data from yfinance (sources from BSE/NSE filings)."""
    data = FinancialData()
    data.symbol = symbol
    yf_symbol = symbol + ".NS"

    print("\n[1/5] Fetching financial data for %s ..." % symbol)

    ticker = yf.Ticker(yf_symbol)

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

    if data.years < 2:
        print("\n  ERROR: Need at least 2 years of annual data for forensic analysis.")
        print("  Only %d year(s) available. Check if the symbol is correct." % data.years)

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
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json, application/pdf",
            "Referer": "https://www.nseindia.com/",
        })
        session.get("https://www.nseindia.com/", timeout=10)

        url = "https://www.nseindia.com/api/corporate-announcements"
        resp = session.get(url, params={
            "index": "equities", "symbol": symbol, "subject": "Credit Rating",
        }, timeout=15)

        if resp.status_code != 200:
            print("  NSE API returned %d — skipping credit ratings" % resp.status_code)
            return results

        filings = resp.json()
        if not isinstance(filings, list) or not filings:
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
            try:
                pdf_resp = session.get(pdf_url, timeout=20)
                if pdf_resp.status_code == 200:
                    extracted_ratings, outlook = _extract_ratings_from_pdf(pdf_resp.content)
            except Exception:
                pass

            # If agency unknown, try to detect from PDF text
            if agency == "Unknown" and extracted_ratings:
                try:
                    reader = PyPDF2.PdfReader(io.BytesIO(pdf_resp.content))
                    page_text = (reader.pages[0].extract_text() or "").lower()
                    for key, name in _AGENCY_PATTERNS:
                        if key in page_text:
                            agency = name
                            break
                    # Also check for S&P
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

    def __init__(self, company, data, analyzer):
        super().__init__()
        self.company = company
        self.data = data
        self.analyzer = analyzer
        self.results = analyzer.results
        self.info = data.info
        self.set_auto_page_break(auto=True, margin=15)
        self._w = 190  # effective page width (A4 - margins)

    def header(self):
        if self.page_no() > 1:
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(*C_GRAY)
            name = self.info.get("shortName", self.company)
            self.cell(0, 5, _latin("Forensic Analysis: %s" % name), align="L")
            self.cell(0, 5, _latin("Page %d" % self.page_no()), align="R", new_x="LMARGIN", new_y="NEXT")
            self.line(10, self.get_y(), 200, self.get_y())
            self.ln(3)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*C_GRAY)
        self.cell(0, 5, _latin("Generated on %s | Data sourced from BSE/NSE via yfinance | For research purposes only" %
                                datetime.datetime.now().strftime("%d-%b-%Y %H:%M")),
                  align="C")

    def _section(self, title):
        """Add a coloured section header."""
        self.set_font("Helvetica", "B", 13)
        self.set_fill_color(*C_BLUE)
        self.set_text_color(*C_WHITE)
        self.cell(0, 9, _latin("  " + title), fill=True, new_x="LMARGIN", new_y="NEXT")
        self.ln(3)
        self.set_text_color(0, 0, 0)

    def _subsection(self, title):
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*C_DARK)
        self.cell(0, 7, _latin(title), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)
        self.set_text_color(0, 0, 0)

    def _table(self, headers, rows, col_widths=None, align=None):
        """Render a data table with alternating row colours."""
        if col_widths is None:
            col_widths = [self._w / len(headers)] * len(headers)
        if align is None:
            align = ["C"] * len(headers)

        # Header row
        self.set_font("Helvetica", "B", 8)
        self.set_fill_color(*C_BLUE)
        self.set_text_color(*C_WHITE)
        for i, h in enumerate(headers):
            self.cell(col_widths[i], 7, _latin(h), border=1, fill=True, align="C")
        self.ln()

        # Data rows
        self.set_font("Helvetica", "", 8)
        self.set_text_color(0, 0, 0)
        for r, row in enumerate(rows):
            fill = r % 2 == 0
            if fill:
                self.set_fill_color(*C_LIGHT)
            for i, val in enumerate(row):
                self.cell(col_widths[i], 6, _latin(str(val)), border=1,
                          fill=fill, align=align[i])
            self.ln()
        self.ln(2)

    def _metric(self, label, value, color=None):
        """Single metric row."""
        self.set_font("Helvetica", "", 9)
        self.set_text_color(0, 0, 0)
        self.cell(80, 6, _latin(label))
        if color:
            self.set_text_color(*color)
        self.set_font("Helvetica", "B", 9)
        self.cell(0, 6, _latin(str(value)), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def _score_box(self, label, score_text, color):
        """Coloured score box."""
        x = self.get_x(); y = self.get_y()
        self.set_fill_color(*color)
        self.set_text_color(*C_WHITE)
        self.set_font("Helvetica", "B", 10)
        self.cell(60, 10, _latin(label), fill=True, align="C")
        self.cell(30, 10, _latin(score_text), fill=True, align="C",
                  new_x="LMARGIN", new_y="NEXT")
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
        self.set_font("Helvetica", "", 9)
        self.multi_cell(0, 5, _latin(prefix + text), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def _check_page_break(self, min_space=45):
        """Add a new page only if less than min_space mm remains."""
        remaining = self.h - self.get_y() - self.b_margin
        if remaining < min_space:
            self.add_page()
        else:
            self.ln(4)

    # ── PAGE BUILDERS ────────────────────────────────────────────────────────

    def add_cover_page(self):
        """Title page."""
        self.add_page()
        self.ln(40)
        self.set_font("Helvetica", "B", 28)
        self.set_text_color(*C_DARK)
        self.cell(0, 15, "FORENSIC ACCOUNTING", align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "B", 22)
        self.cell(0, 12, "ANALYSIS REPORT", align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(10)

        # Company name
        name = self.info.get("longName", self.info.get("shortName", self.company))
        self.set_font("Helvetica", "B", 20)
        self.set_text_color(*C_BLUE)
        self.cell(0, 12, _latin(name), align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_font("Helvetica", "", 14)
        self.set_text_color(*C_GRAY)
        self.cell(0, 8, _latin("NSE: %s" % self.company), align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(10)

        # Key info
        self.set_text_color(*C_DARK)
        self.set_font("Helvetica", "", 11)
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
        self.set_font("Helvetica", "B", 16)
        self.cell(0, 14, _latin("  Overall Score: %.0f / 100   |   Recommendation: %s  " % (score, rec)),
                  fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        self.ln(15)
        self.set_font("Helvetica", "I", 8)
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

        self.add_page()
        self.set_fill_color(*C_RED)
        self.set_text_color(*C_WHITE)
        self.set_font("Helvetica", "B", 13)
        self.cell(0, 9, _latin("  KEY RED FLAGS — READ FIRST"), fill=True,
                  new_x="LMARGIN", new_y="NEXT")
        self.ln(4)
        self.set_text_color(0, 0, 0)

        self.set_font("Helvetica", "", 9)
        self.multi_cell(0, 5, _latin(
            "The following critical and major red flags were detected. "
            "These are the most important concerns an investor should evaluate "
            "before proceeding."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        for flag_text, severity in critical:
            self.set_text_color(*C_RED)
            self.set_font("Helvetica", "B", 10)
            self.multi_cell(0, 6, _latin("[CRITICAL] " + flag_text),
                            new_x="LMARGIN", new_y="NEXT")
        for flag_text, severity in major:
            self.set_text_color(200, 80, 0)
            self.set_font("Helvetica", "B", 9)
            self.multi_cell(0, 5, _latin("[MAJOR] " + flag_text),
                            new_x="LMARGIN", new_y="NEXT")

        self.set_text_color(0, 0, 0)
        self.ln(3)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(*C_GRAY)
        self.cell(0, 5, _latin("Detailed analysis of each concern follows in subsequent sections."),
                  new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

    def add_executive_summary(self):
        """Page 2: Executive summary with scores and flags overview."""
        self._check_page_break(80)
        self._section("EXECUTIVE SUMMARY")

        overall = self.results.get("overall", {})

        # Score boxes
        b = self.results.get("beneish", {})
        a = self.results.get("altman", {})
        p = self.results.get("piotroski", {})

        def _color_for(score_10):
            if score_10 >= 7: return C_GREEN
            if score_10 >= 4: return C_YELLOW
            return C_RED

        if b:
            self._score_box("Beneish M-Score (Manipulation)", "%.2f  %s" % (
                b["m_score"], b["verdict"]), _color_for(b["score_10"]))
        if a:
            self._score_box("Altman Z-Score (Bankruptcy)", "%.2f  %s" % (
                a["z_score"], a["zone"]), _color_for(a["score_10"]))
        if p:
            self._score_box("Piotroski F-Score (Strength)", "%d / 9  %s" % (
                p["f_score"], p["verdict"]), _color_for(p["score_10"]))

        cf = self.results.get("cashflow", {})
        if cf:
            self._score_box("Cash Flow Quality", "Score: %d/10" % cf["score_10"],
                            _color_for(cf["score_10"]))
        dt = self.results.get("debt", {})
        if dt:
            self._score_box("Debt Health", "Score: %d/10" % dt["score_10"],
                            _color_for(dt["score_10"]))

        # Springate S-Score
        sp = self.results.get("springate", {})
        if sp:
            self._score_box("Springate S-Score (Bankruptcy)", "%.2f  %s" % (
                sp["s_score"], sp["verdict"]), _color_for(sp["score_10"]))

        # Ohlson O-Score
        oh = self.results.get("ohlson", {})
        if oh:
            self._score_box("Ohlson O-Score (Bankruptcy Prob)", "%.0f%%  %s" % (
                oh["probability"] * 100, oh["verdict"]), _color_for(oh["score_10"]))

        # Montier C-Score
        mc = self.results.get("montier", {})
        if mc:
            self._score_box("Montier C-Score (Manipulation)", "%d/6  %s" % (
                mc["c_score"], mc["verdict"]), _color_for(mc["score_10"]))

        # Benford's Law
        bf = self.results.get("benford", {})
        if bf and bf.get("available"):
            self._score_box("Benford's Law (Number Integrity)",
                            bf["conformity"],
                            C_GREEN if bf["conformity"] == "PASS" else
                            C_YELLOW if bf["conformity"] == "MARGINAL" else C_RED)

        # ESM Status
        esm = self.results.get("esm", {})
        if esm.get("in_esm"):
            self._score_box("ESM Status", "IN ESM %s" % esm.get("stage", ""), C_RED)
        else:
            self._score_box("ESM Status", "Not in ESM (Normal)", C_GREEN)

        self.ln(3)
        self._metric("Red Flags Detected", "%d" % len(self.analyzer.red_flags), C_RED if self.analyzer.red_flags else C_GREEN)
        self._metric("Green Flags Detected", "%d" % len(self.analyzer.green_flags), C_GREEN)
        self._metric("Overall Score", "%.0f / 100" % overall.get("final_score", 0))
        self._metric("Recommendation", overall.get("recommendation", "N/A"),
                     C_GREEN if overall.get("final_score", 0) >= 60 else C_RED)

        self.ln(3)
        self.set_font("Helvetica", "", 9)
        self.multi_cell(0, 5, _latin(overall.get("rec_detail", "")), new_x="LMARGIN", new_y="NEXT")

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
            self.set_font("Helvetica", "", 8)
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
            self.set_font("Helvetica", "", 8)
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
            self.set_font("Helvetica", "", 8)
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
            self.set_font("Helvetica", "", 8)
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
        self.set_font("Helvetica", "", 8)
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
        self.set_font("Helvetica", "", 8)
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
            self.set_font("Helvetica", "I", 9)
            self.cell(0, 6, "No red flags detected.", new_x="LMARGIN", new_y="NEXT")

        self.ln(5)
        self._subsection("Green Flags (Positive Indicators)")
        if self.analyzer.green_flags:
            for flag_text in self.analyzer.green_flags:
                self._flag(flag_text, is_red=False)
        else:
            self.set_font("Helvetica", "I", 9)
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

        self.set_font("Helvetica", "", 8)
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

        self.set_font("Helvetica", "", 8)
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

        self.set_font("Helvetica", "", 8)
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
        self.set_font("Helvetica", "", 8)
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
        self.set_font("Helvetica", "", 8)
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
        self.set_font("Helvetica", "", 8)
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
        self.set_font("Helvetica", "", 8)
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
        self.set_font("Helvetica", "", 8)
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
        self.set_font("Helvetica", "", 8)
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
        self.set_font("Helvetica", "", 8)
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
        self.set_font("Helvetica", "", 8)
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
            self.set_font("Helvetica", "B", 12)
            self.cell(0, 10, _latin("  WARNING: STOCK IS IN ESM %s  " % stage),
                      fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)
            self.ln(3)
            self.set_font("Helvetica", "", 9)
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
            self.set_font("Helvetica", "B", 11)
            self.cell(0, 9, _latin("  Stock is NOT in any ESM stage (Normal Trading)  "),
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
        self.set_font("Helvetica", "", 9)
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
                self.set_font("Helvetica", "", 9)
                self.set_text_color(*C_GREEN)
                self.multi_cell(0, 5, _latin("[+] " + sig), new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)
        else:
            self.set_font("Helvetica", "I", 9)
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
        """Final recommendation page with score breakdown and weight table."""
        self.add_page()
        self._section("INVESTMENT RECOMMENDATION")

        overall = self.results.get("overall", {})
        score = overall.get("final_score", 0)
        rec = overall.get("recommendation", "N/A")

        self.ln(5)
        color = C_GREEN if score >= 60 else C_YELLOW if score >= 45 else C_RED
        self.set_fill_color(*color)
        self.set_text_color(*C_WHITE)
        self.set_font("Helvetica", "B", 18)
        self.cell(0, 16, _latin("  OVERALL SCORE: %.0f / 100  " % score),
                  fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(3)
        self.cell(0, 14, _latin("  RECOMMENDATION: %s  " % rec),
                  fill=True, align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)

        self.ln(8)
        self.set_font("Helvetica", "", 10)
        self.multi_cell(0, 6, _latin(overall.get("rec_detail", "")), new_x="LMARGIN", new_y="NEXT")

        self.ln(5)
        self._subsection("Score Breakdown")
        self._metric("Base Score (from weighted analysis)", "%.1f / 100" % overall.get("base_score", 0))
        self._metric("Red Flag Penalty", "-%.1f" % overall.get("penalty", 0), C_RED)
        self._metric("Green Flag Bonus", "+%.1f" % overall.get("bonus", 0), C_GREEN)
        self._metric("Final Score", "%.0f / 100" % score)

        # ── SCORING METHODOLOGY TABLE ──
        self.ln(5)
        self._subsection("Scoring Methodology — Technique Weightages")
        self.set_font("Helvetica", "", 8)
        self.multi_cell(0, 4, _latin(
            "The overall score is computed as a weighted average of 14 forensic techniques "
            "across 4 categories: Manipulation Detection (25%), Bankruptcy/Distress (20%), "
            "Fundamental Quality (35%), and Deep Forensic Checks (20%). Each technique scores "
            "0-10, weighted, scaled to 0-100, then adjusted by red flag penalties and green flag bonuses."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        details = overall.get("score_details", [])
        if details:
            headers = ["Technique", "Weight", "Raw Score (0-10)", "Contribution"]
            rows = []
            # Group by category
            cat_a = []  # Manipulation Detection
            cat_b = []  # Bankruptcy
            cat_c = []  # Fundamental Quality
            cat_d = []  # Deep Forensic
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
                    rows.append([
                        "  " + d["technique"],
                        "%.0f%%" % d["weight"],
                        "%d / 10" % d["raw_score"],
                        "%.1f" % d["weighted"],
                    ])

            _add_cat("A. MANIPULATION DETECTION (25%)", cat_a)
            _add_cat("B. BANKRUPTCY / DISTRESS (20%)", cat_b)
            _add_cat("C. FUNDAMENTAL QUALITY (35%)", cat_c)
            _add_cat("D. DEEP FORENSIC CHECKS (20%)", cat_d)

            # Totals
            total_wt = sum(d["weight"] for d in details)
            total_contrib = sum(d["weighted"] for d in details)
            rows.append(["TOTAL", "%.0f%%" % total_wt, "", "%.1f" % total_contrib])

            self._table(headers, rows, [80, 28, 38, 34],
                        align=["L", "C", "C", "C"])

        # ── Flag penalty/bonus detail ──
        self.ln(3)
        self._subsection("Flag Adjustments")
        self.set_font("Helvetica", "", 8)
        self.multi_cell(0, 4, _latin(
            "Red flags penalise the score: CRITICAL = -6 pts, MAJOR = -3 pts, MINOR = -1.5 pts. "
            "Green flags add +1.5 pts each (capped at +10). This ensures that even a company "
            "with good ratios gets penalised for specific danger signals."),
            new_x="LMARGIN", new_y="NEXT")
        self.ln(2)
        self._metric("Critical flags", "%d  (x -6 pts each)" % sum(
            1 for f, s in self.analyzer.red_flags if s == "critical"), C_RED)
        self._metric("Major flags", "%d  (x -3 pts each)" % sum(
            1 for f, s in self.analyzer.red_flags if s == "major"), C_RED)
        self._metric("Minor flags", "%d  (x -1.5 pts each)" % sum(
            1 for f, s in self.analyzer.red_flags if s == "minor"), C_RED)
        self._metric("Green flags", "%d  (x +1.5 pts, max +10)" % len(self.analyzer.green_flags), C_GREEN)

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
            self.set_font("Helvetica", "B", 9)
            self.cell(50, 5, _latin(label))
            self.set_font("Helvetica", "", 9)
            self.cell(0, 5, _latin(desc), new_x="LMARGIN", new_y="NEXT")

        self.ln(5)
        self._subsection("Key Considerations")
        self.set_font("Helvetica", "", 9)
        considerations = [
            "1. This analysis is based on publicly reported financial statements only.",
            "2. Review the annual report and auditor's notes for qualitative factors.",
            "3. Check for related party transactions in the notes to accounts.",
            "4. Monitor management commentary in earnings calls for forward guidance.",
            "5. Consider macroeconomic factors and sector-specific headwinds/tailwinds.",
            "6. Check for any pending litigation, regulatory actions, or SEBI orders.",
            "7. Verify promoter pledge status from latest shareholding pattern.",
            "8. Cross-reference with credit rating agency reports (CRISIL, ICRA, etc.).",
        ]
        for c in considerations:
            self.multi_cell(0, 5, _latin(c), new_x="LMARGIN", new_y="NEXT")

    # ── GENERATE FULL REPORT ─────────────────────────────────────────────────
    def generate(self, output_path):
        """Build and save the complete PDF report."""
        print("\n[4/5] Generating PDF report...")

        self.add_cover_page()
        self.add_key_red_flags_summary()
        self.add_executive_summary()
        self.add_company_overview()
        self.add_financial_tables()
        self.add_forensic_scores()

        # Additional sections on current page (after forensic scores)
        self.add_springate_section()
        self.add_ohlson_section()
        self.add_montier_section()
        self.add_dupont_section()
        self.add_working_capital_section()
        self.add_growth_section()
        self.add_sgr_section()
        self.add_volatility_section()
        self.add_operating_leverage_section()

        self.add_benfords_law_section()
        self.add_enhanced_checks_section()
        self.add_esm_section()
        self.add_promoter_section()

        self.add_flags_page()
        self.add_credit_rating_page()
        self.add_sector_analysis()
        self.add_recommendation_page()

        self.output(output_path)
        print("  PDF saved: %s" % output_path)
        return output_path


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run(symbol=None):
    """Run the complete forensic accounting analysis."""
    if symbol is None:
        symbol = COMPANY_SYMBOL

    symbol = symbol.strip().upper()

    print("=" * 64)
    print("  FORENSIC ACCOUNTING ANALYSIS")
    print("  Symbol: %s" % symbol)
    print("  Date  : %s" % datetime.datetime.now().strftime("%d-%b-%Y %H:%M"))
    print("=" * 64)

    # Step 1: Fetch data
    data = fetch_financial_data(symbol)

    if data.years < 2:
        print("\nCannot proceed — insufficient financial data.")
        print("Check if '%s' is a valid NSE symbol with at least 2 years of filings." % symbol)
        return

    # Step 2: Analyse
    analyzer = ForensicAnalyzer(data)
    results = analyzer.run_all()

    # Step 3: Generate PDF
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_name = "forensic_report_%s_%s.pdf" % (symbol, timestamp)
    pdf_path = os.path.join(SCRIPT_DIR, pdf_name)

    report = ForensicReport(symbol, data, analyzer)
    report.generate(pdf_path)

    # Step 4: Console summary
    print("\n" + "=" * 64)
    print("  ANALYSIS COMPLETE")
    print("=" * 64)
    overall = results.get("overall", {})
    print("  Score         : %.0f / 100" % overall.get("final_score", 0))
    print("  Recommendation: %s" % overall.get("recommendation", "N/A"))
    print("  Red Flags     : %d" % len(analyzer.red_flags))
    print("  Green Flags   : %d" % len(analyzer.green_flags))
    print("  Report        : %s" % pdf_path)
    print("=" * 64)

    return results, pdf_path


if __name__ == "__main__":
    run()
