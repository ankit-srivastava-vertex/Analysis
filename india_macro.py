"""
India Macro Dashboard
=====================

End-to-end India macro/fiscal/financial-markets dashboard tracking 33
monthly indicators across 7 categories. Fetches data automatically from
10+ government/regulator sources, computes MoM and YoY growth rates,
and produces an interactive HTML dashboard plus a multi-sheet Excel workbook.

Integrated into run_all.py as Scenario 8/8 — runs `--fetch-direct`, then
builds outputs. The standalone Excel and chart are attached to the daily
email alongside the unified market analysis workbook.

INDICATORS (33 total, 7 categories)
------------------------------------
 Fiscal (1):
   gst_gross                — GST Gross Collections (₹ Cr)

 Industrial (6):
   core8                    — IIP Core-8 Index
   cement_production        — Cement Production (lakh tonnes)
   steel_production         — Crude Steel Production (lakh tonnes)
   electricity_generation   — Electricity Generation (BU)
   steel_dispatch           — Steel Dispatches (lakh tonnes)
   fertilizer_dispatch      — Fertilizer Dispatches (lakh tonnes)

 External Sector (4):
   forex_reserves           — Forex Reserves Total ($ Bn)
   forex_fca                — Forex FCA ($ Bn)
   forex_gold               — Forex Gold ($ Bn)
   fdi_inflows              — FDI Equity Inflows ($ Mn)

 Energy (6):
   petroleum_consumption    — Petroleum Consumption (TMT)
   crude_oil_production     — Crude Oil Production (TMT)
   lpg_connections          — LPG Active Domestic Customers (Cr)
   png_connections          — PNG Domestic Connections (Lakh)
   renewable_capacity       — Renewable Energy Capacity (GW)
   power_generation_state   — State Sector Power Generation (BU)

 Banking (2):
   bank_credit_total        — SCB Total Credit Outstanding (₹ Lakh Cr)
   bank_deposit_total       — SCB Total Deposits (₹ Lakh Cr)

 Capital Markets (14):
   fpi_equity               — FPI Equity Net Investment (₹ Cr)
   fpi_debt                 — FPI Debt Net Investment (₹ Cr)
   fpi_custodian_top5       — FPI Top-5 Custodian AUC Share (%)
   fpi_country_top5         — FPI Top-5 Country AUC Share (%)
   mf_aum_total             — MF Industry AUM Total (₹ Lakh Cr)
   mf_aum_equity            — MF Equity AUM (₹ Lakh Cr)
   mf_aum_debt              — MF Debt AUM (₹ Lakh Cr)
   mf_aum_hybrid            — MF Hybrid AUM (₹ Lakh Cr)
   sip_inflow               — MF SIP Monthly Inflow (₹ Cr)
   folios_equity            — MF Equity Folios (Cr)
   folios_debt              — MF Debt Folios (Cr)
   folios_hybrid            — MF Hybrid Folios (Cr)
   depository_demat_nsdl    — NSDL Demat Accounts (Cr)
   depository_demat_cdsl    — CDSL Demat Accounts (Cr)

DATA SOURCES & FETCHERS (14 direct fetchers)
---------------------------------------------
 Source                     | Fetcher Function            | Indicators Updated
 --------------------------+-----------------------------+--------------------
 RBI WSS (DBIE Excel)      | fetch_rbi_wss()             | forex_reserves, forex_fca,
                           |                             | forex_gold, bank_credit_total,
                           |                             | bank_deposit_total
 AMFI Monthly Report       | fetch_amfi()                | mf_aum_total, mf_aum_equity,
                           |                             | mf_aum_debt, mf_aum_hybrid,
                           |                             | sip_inflow, folios_equity,
                           |                             | folios_debt, folios_hybrid
 CEA Executive Summary PDF | fetch_cea_executive_summary()| electricity_generation
 PPAC Oil & Gas PDF        | fetch_ppac_snapshot()       | petroleum_consumption,
                           |                             | crude_oil_production
 NSDL FPI Monthly          | fetch_nsdl_fpi_monthly()    | fpi_equity, fpi_debt
 Ministry of Steel PDF     | fetch_steel_monthly()       | steel_production, steel_dispatch
 Dept of Fertilizers PDF   | fetch_fertilizer_monthly()  | fertilizer_dispatch
 NSDL Demat HTML           | fetch_nsdl_demat()          | depository_demat_nsdl
 CDSL Periodic PDF         | fetch_cdsl_demat()          | depository_demat_cdsl
 PPAC LPG XLSX             | fetch_ppac_lpg()            | lpg_connections
 PPAC PNG XLSX             | fetch_ppac_png()            | png_connections
 OEA Core-8 XLSX           | fetch_core8_cement()        | core8, cement_production
 NSDL FPI Country Top-5    | fetch_nsdl_fpi_country_top5()| fpi_country_top5
 NSDL FPI Custodian Top-5  | fetch_nsdl_fpi_custodian_top5()| fpi_custodian_top5

 Additional (manual/OGD):
   data.gov.in OGD API     — Free key from data.gov.in/user/register;
                              set DATA_GOV_IN_API_KEY in .env to enable.
   Manual --add             — gst_gross, fdi_inflows, renewable_capacity,
                              power_generation_state

ARCHITECTURE
------------
  INDICATORS   — List of dicts defining each indicator (id, category, title,
                 unit, source, metrics). Single source of truth.
  SEED         — Dict of {id: [(YYYY-MM, value), ...]} providing 24+ months
                 of verified historical data for auto-seeding on first run.
  DIRECT_FETCHERS — List of (label, fn) tuples; each fn() returns
                    {indicator_id: (period_str, value)} or None.
  CSV Storage  — One CSV per indicator at data/india_macro/<id>.csv
                 (columns: period, value). Append-only, deduped on period.
  Growth Calc  — compute_growth() adds MoM% and/or YoY% columns based on
                 each indicator's configured "metrics" list.
  Dashboard    — build_dashboard() assembles a multi-tab Plotly HTML page
                 with one chart per indicator, grouped by category.
  Excel        — build_excel() writes an Overview sheet + one data sheet
                 per indicator into india_macro_data.xlsx.

USAGE
-----
    # Build dashboard from current CSVs (no fetch):
    python3 india_macro.py

    # List all indicators and their populated/pending status:
    python3 india_macro.py --list

    # Manually add a data point:
    python3 india_macro.py --add gst_gross 2025-05 215000

    # Print one indicator's data table with growth rates:
    python3 india_macro.py --print gst_gross

    # Run all 14 direct fetchers then rebuild dashboard + Excel:
    python3 india_macro.py --fetch-direct

    # data.gov.in (OGD) operations:
    python3 india_macro.py --ogd-test <resource_uuid>   # inspect dataset fields
    python3 india_macro.py --fetch <indicator_id>       # pull single from OGD
    python3 india_macro.py --fetch-all                  # pull all OGD + direct + browser

    # To add an OGD mapping for an indicator:
    #  1. Browse https://www.data.gov.in/ and locate the dataset.
    #  2. Open the dataset page; the URL path ends in the resource UUID,
    #     e.g. .../resource/<uuid> or click "API" tab to copy the UUID.
    #  3. Run:  python3 india_macro.py --ogd-test <uuid>
    #     This prints the dataset's available fields.
    #  4. Add a one-line entry in OGD_RESOURCES mapping the
    #     indicator id -> {resource_id, period_field, value_field}.

OUTPUT
------
  india_macro_dashboard.html   — Interactive Plotly dashboard (33 charts,
                                  tabbed by category, MoM/YoY overlays)
  india_macro_data.xlsx        — Multi-sheet workbook (Overview + 33 data sheets)
  data/india_macro/<id>.csv    — Per-indicator CSV store (period, value)

DEPENDENCIES
------------
  Required:  pandas, plotly, openpyxl, requests
  Optional:  pdfplumber (for CEA, PPAC, Steel, Fertilizer, CDSL PDF parsing)
             playwright (for --fetch-browser SPA fetchers)
"""

import argparse
import os
import sys
from datetime import datetime
from collections import defaultdict

import pandas as pd

try:
    import requests
    import socket
    import urllib3.util.connection as _u3c
    # Force IPv4 — api.data.gov.in IPv6 hangs on some macOS networks.
    _u3c.allowed_gai_family = lambda: socket.AF_INET
except ImportError:
    sys.exit("Requires: pip install requests")

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import plotly.io as pio
except ImportError:
    sys.exit("Requires: pip install plotly openpyxl pandas")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data", "india_macro")
HTML_PATH = os.path.join(SCRIPT_DIR, "india_macro_dashboard.html")
XLSX_PATH = os.path.join(SCRIPT_DIR, "india_macro_data.xlsx")

os.makedirs(DATA_DIR, exist_ok=True)


# ===========================================================================
# data.gov.in (OGD) configuration
# ===========================================================================

OGD_BASE = "https://api.data.gov.in/resource"
OGD_HEADERS = {"User-Agent": "curl/8.0"}  # default python-requests UA is blocked
OGD_TIMEOUT = (10, 60)  # (connect, read)


def _load_env_key():
    """Read DATA_GOV_IN_API_KEY from .env (or environment)."""
    env = os.environ.get("DATA_GOV_IN_API_KEY")
    if env:
        return env.strip()
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line.startswith("DATA_GOV_IN_API_KEY"):
                _, _, val = line.partition("=")
                return val.strip().strip('"').strip("'")
    return None


OGD_KEY = _load_env_key()


# ---------------------------------------------------------------------------
# OGD resource mappings — fill in as you discover correct resource UUIDs
# from data.gov.in. Use --ogd-test <uuid> to inspect a dataset's fields.
#
# Schema per entry:
#   indicator_id: {
#       "resource_id":  "<uuid from data.gov.in>",
#       "period_field": "<column-name with month/date>",
#       "value_field":  "<column-name with the numeric value>",
#       "period_format": "YYYY-MM" | "DD/MM/YYYY" | etc. (Python strptime fmt)
#       "filter":       optional dict of field->value to narrow rows
#       "agg":          "sum"|"mean"|None — collapse multiple rows per period
#       "scale":        optional float to multiply value by
#   }
# ---------------------------------------------------------------------------
OGD_RESOURCES = {
    # Example placeholder — replace with real working IDs as you find them:
    # "cement_production": {
    #     "resource_id":  "<paste-from-data.gov.in>",
    #     "period_field": "month",
    #     "value_field":  "production_in_million_tonnes",
    #     "period_format": "%b-%y",
    #     "agg": None,
    # },
}


# ===========================================================================
# INDICATOR REGISTRY — curated 34-indicator subset (auto-fetchable focus)
# Each indicator declares: id, category, title, unit, source, growth metrics
# Growth metrics: any subset of ["MoM", "QoQ", "YoY"]
# ===========================================================================

INDICATORS = [
    # ── FISCAL ────────────────────────────────────────────────────────────
    {"id": "gst_gross", "category": "Fiscal",
     "title": "GST Gross Collection", "unit": "₹ Cr", "freq": "monthly",
     "source": "PIB (Ministry of Finance)", "metrics": ["MoM", "YoY"]},

    # ── INDUSTRIAL ────────────────────────────────────────────────────────
    {"id": "core8", "category": "Industrial",
     "title": "8 Core Industries Index (YoY %)", "unit": "%",
     "freq": "monthly", "source": "Office of Economic Adviser",
     "metrics": []},
    {"id": "cement_production", "category": "Industrial",
     "title": "Cement Production", "unit": "Mn Tonnes", "freq": "monthly",
     "source": "OEA Core-8", "metrics": ["MoM", "YoY"]},
    {"id": "steel_production", "category": "Industrial",
     "title": "Crude Steel Production", "unit": "Mn Tonnes", "freq": "monthly",
     "source": "JPC / Ministry of Steel", "metrics": ["MoM", "YoY"]},
    {"id": "electricity_generation", "category": "Industrial",
     "title": "Electricity Generation", "unit": "BU", "freq": "monthly",
     "source": "CEA", "metrics": ["MoM", "YoY"]},
    {"id": "steel_dispatch", "category": "Industrial",
     "title": "Finished Steel Dispatch", "unit": "Mn Tonnes", "freq": "monthly",
     "source": "JPC", "metrics": ["MoM", "YoY"]},
    {"id": "fertilizer_dispatch", "category": "Industrial",
     "title": "Fertilizer Dispatch", "unit": "Lakh Tonnes", "freq": "monthly",
     "source": "Department of Fertilizers", "metrics": ["MoM", "YoY"]},

    # ── EXTERNAL SECTOR ───────────────────────────────────────────────────
    {"id": "forex_reserves", "category": "External Sector",
     "title": "Forex Reserves Total", "unit": "$ Bn", "freq": "monthly",
     "source": "RBI Weekly Statistical Supplement", "metrics": ["MoM", "YoY"]},
    {"id": "forex_fca", "category": "External Sector",
     "title": "Forex Reserves: Foreign Currency Assets", "unit": "$ Bn",
     "freq": "monthly", "source": "RBI WSS", "metrics": ["MoM"]},
    {"id": "forex_gold", "category": "External Sector",
     "title": "Forex Reserves: Gold", "unit": "$ Bn",
     "freq": "monthly", "source": "RBI WSS", "metrics": ["MoM"]},
    {"id": "fdi_inflows", "category": "External Sector",
     "title": "FDI Inflows (Equity)", "unit": "$ Mn", "freq": "quarterly",
     "source": "DPIIT FDI Factsheet", "metrics": ["YoY"]},

    # ── ENERGY ────────────────────────────────────────────────────────────
    {"id": "petroleum_consumption", "category": "Energy",
     "title": "Petroleum Products Consumption", "unit": "MMT", "freq": "monthly",
     "source": "PPAC", "metrics": ["MoM", "YoY"]},
    {"id": "crude_oil_production", "category": "Energy",
     "title": "Crude Oil Production", "unit": "MMT", "freq": "monthly",
     "source": "PPAC", "metrics": ["MoM", "YoY"]},
    {"id": "lpg_connections", "category": "Energy",
     "title": "Active LPG Connections", "unit": "Cr",
     "freq": "monthly", "source": "PPAC", "metrics": ["MoM"]},
    {"id": "png_connections", "category": "Energy",
     "title": "Domestic PNG Connections", "unit": "Lakh",
     "freq": "quarterly", "source": "PNGRB CGD Snapshot", "metrics": ["MoM"]},
    {"id": "renewable_capacity", "category": "Energy",
     "title": "Renewable Installed Capacity (cumulative)", "unit": "GW",
     "freq": "monthly", "source": "CEA Installed Capacity Report",
     "metrics": ["MoM"]},
    {"id": "power_generation_state", "category": "Energy",
     "title": "Power Generation (all-India total)", "unit": "BU",
     "freq": "monthly", "source": "CEA Executive Summary",
     "metrics": ["MoM", "YoY"]},

    # ── BANKING ───────────────────────────────────────────────────────────
    {"id": "bank_credit_total", "category": "Banking",
     "title": "Bank Credit (YoY %)", "unit": "%", "freq": "monthly",
     "source": "RBI Weekly Statistical Supplement", "metrics": []},
    {"id": "bank_deposit_total", "category": "Banking",
     "title": "Bank Deposits (YoY %)", "unit": "%", "freq": "monthly",
     "source": "RBI Weekly Statistical Supplement", "metrics": []},

    # ── CAPITAL MARKETS ───────────────────────────────────────────────────
    {"id": "fpi_equity", "category": "Capital Markets",
     "title": "FPI Net Investment — Equity", "unit": "₹ Cr",
     "freq": "monthly", "source": "NSDL FPI Statistics", "metrics": []},
    {"id": "fpi_debt", "category": "Capital Markets",
     "title": "FPI Net Investment — Debt", "unit": "₹ Cr",
     "freq": "monthly", "source": "NSDL FPI Statistics", "metrics": []},
    {"id": "fpi_custodian_top5", "category": "Capital Markets",
     "title": "Top-5 FPI Custodians (AUC % share)", "unit": "%",
     "freq": "monthly", "source": "NSDL", "metrics": []},
    {"id": "fpi_country_top5", "category": "Capital Markets",
     "title": "Top-5 FPI Country-of-Origin (AUC % share)", "unit": "%",
     "freq": "monthly", "source": "NSDL", "metrics": []},
    {"id": "mf_aum_total", "category": "Capital Markets",
     "title": "Mutual Fund Industry AUM", "unit": "₹ Lakh Cr",
     "freq": "monthly", "source": "AMFI", "metrics": ["MoM", "YoY"]},
    {"id": "mf_aum_equity", "category": "Capital Markets",
     "title": "MF AUM — Equity Schemes", "unit": "₹ Lakh Cr",
     "freq": "monthly", "source": "AMFI", "metrics": ["MoM", "YoY"]},
    {"id": "mf_aum_debt", "category": "Capital Markets",
     "title": "MF AUM — Debt Schemes", "unit": "₹ Lakh Cr",
     "freq": "monthly", "source": "AMFI", "metrics": ["MoM", "YoY"]},
    {"id": "mf_aum_hybrid", "category": "Capital Markets",
     "title": "MF AUM — Hybrid Schemes", "unit": "₹ Lakh Cr",
     "freq": "monthly", "source": "AMFI", "metrics": ["MoM", "YoY"]},
    {"id": "sip_inflow", "category": "Capital Markets",
     "title": "Monthly SIP Inflow", "unit": "₹ Cr",
     "freq": "monthly", "source": "AMFI", "metrics": ["MoM", "YoY"]},
    {"id": "folios_equity", "category": "Capital Markets",
     "title": "MF Folios — Equity", "unit": "Cr",
     "freq": "monthly", "source": "AMFI", "metrics": ["MoM"]},
    {"id": "folios_debt", "category": "Capital Markets",
     "title": "MF Folios — Debt", "unit": "Cr",
     "freq": "monthly", "source": "AMFI", "metrics": ["MoM"]},
    {"id": "folios_hybrid", "category": "Capital Markets",
     "title": "MF Folios — Hybrid", "unit": "Cr",
     "freq": "monthly", "source": "AMFI", "metrics": ["MoM"]},
    {"id": "depository_demat_nsdl", "category": "Capital Markets",
     "title": "NSDL Demat Accounts (cumulative)", "unit": "Cr",
     "freq": "monthly", "source": "NSDL", "metrics": ["MoM"]},
    {"id": "depository_demat_cdsl", "category": "Capital Markets",
     "title": "CDSL Demat Accounts (cumulative)", "unit": "Cr",
     "freq": "monthly", "source": "CDSL", "metrics": ["MoM"]},
]


# ===========================================================================
# SEED DATA — only indicators with verified recent headline numbers from
# official PIB / RBI / CGA / AMFI / NPCI press releases. The rest start as
# empty CSVs and the user fills them via --add.
# ===========================================================================

SEED = {
    "gst_gross": [
        ("2022-04", 167540), ("2022-05", 140885), ("2022-06", 144616),
        ("2022-07", 148995), ("2022-08", 143612), ("2022-09", 147686),
        ("2022-10", 151718), ("2022-11", 145867), ("2022-12", 149507),
        ("2023-01", 155922), ("2023-02", 149577), ("2023-03", 160122),
        ("2023-04", 187035), ("2023-05", 157090), ("2023-06", 161497),
        ("2023-07", 165105), ("2023-08", 159069), ("2023-09", 162712),
        ("2023-10", 172003), ("2023-11", 167929), ("2023-12", 164882),
        ("2024-01", 172129), ("2024-02", 168337), ("2024-03", 178484),
        ("2024-04", 210267), ("2024-05", 172739), ("2024-06", 173813),
        ("2024-07", 182075), ("2024-08", 174962), ("2024-09", 173240),
        ("2024-10", 187346), ("2024-11", 182269), ("2024-12", 176857),
        ("2025-01", 195506), ("2025-02", 183646), ("2025-03", 196141),
        ("2025-04", 236716),
    ],
    # Forex reserves total ($ Bn) — RBI WSS month-end
    "forex_reserves": [
        ("2024-04", 637.92), ("2024-05", 651.51), ("2024-06", 651.99),
        ("2024-07", 670.85), ("2024-08", 681.69), ("2024-09", 704.89),
        ("2024-10", 682.13), ("2024-11", 658.09), ("2024-12", 644.39),
        ("2025-01", 630.61), ("2025-02", 638.69), ("2025-03", 665.40),
        ("2025-04", 686.16),
    ],
    # AMFI Industry AUM (₹ Lakh Cr) month-end
    "mf_aum_total": [
        ("2024-04", 57.26), ("2024-05", 58.91), ("2024-06", 61.16),
        ("2024-07", 64.97), ("2024-08", 66.70), ("2024-09", 67.09),
        ("2024-10", 67.26), ("2024-11", 68.08), ("2024-12", 66.93),
        ("2025-01", 67.25), ("2025-02", 64.53), ("2025-03", 65.74),
        ("2025-04", 70.00),
    ],
    # Monthly SIP inflows (₹ Cr) — AMFI
    "sip_inflow": [
        ("2024-04", 20371), ("2024-05", 20904), ("2024-06", 21262),
        ("2024-07", 23332), ("2024-08", 23547), ("2024-09", 24509),
        ("2024-10", 25323), ("2024-11", 25320), ("2024-12", 26459),
        ("2025-01", 26400), ("2025-02", 25999), ("2025-03", 25926),
        ("2025-04", 26632),
    ],
    # 8 Core Industries Index YoY %
    "core8": [
        ("2024-04", 6.70), ("2024-05", 6.90), ("2024-06", 5.10),
        ("2024-07", 6.30), ("2024-08", -1.50), ("2024-09", 2.40),
        ("2024-10", 3.70), ("2024-11", 4.30), ("2024-12", 4.40),
        ("2025-01", 5.10), ("2025-02", 2.90), ("2025-03", 3.80),
    ],
    # Bank credit YoY % (RBI WSS)
    "bank_credit_total": [
        ("2024-04", 19.0), ("2024-05", 19.8), ("2024-06", 17.4),
        ("2024-07", 15.0), ("2024-08", 13.6), ("2024-09", 13.0),
        ("2024-10", 12.8), ("2024-11", 11.8), ("2024-12", 11.5),
        ("2025-01", 11.4), ("2025-02", 11.0), ("2025-03", 11.1),
    ],
    "bank_deposit_total": [
        ("2024-04", 12.5), ("2024-05", 12.2), ("2024-06", 11.8),
        ("2024-07", 10.6), ("2024-08", 10.7), ("2024-09", 11.5),
        ("2024-10", 11.7), ("2024-11", 11.1), ("2024-12", 10.0),
        ("2025-01", 10.2), ("2025-02", 10.6), ("2025-03", 10.3),
    ],
    # FDI Equity Inflows ($ Mn) — quarterly, DPIIT FDI Factsheet / RBI BOP
    # Period = last month of the quarter (Jun/Sep/Dec/Mar)
    "fdi_inflows": [
        ("2023-06", 17791), ("2023-09", 10420), ("2023-12", 12385),
        ("2024-03", 11489), ("2024-06", 16173), ("2024-09", 13602),
        ("2024-12", 17394),
    ],
}


# ===========================================================================
# CSV helpers — one CSV per indicator under data/india_macro/
# ===========================================================================

def csv_path(indicator_id):
    return os.path.join(DATA_DIR, "%s.csv" % indicator_id)


def ensure_csv(ind):
    """Create the indicator's CSV from SEED if available, else empty header."""
    p = csv_path(ind["id"])
    if os.path.exists(p):
        return
    rows = SEED.get(ind["id"], [])
    df = pd.DataFrame(rows, columns=["Period", "Value"])
    df.to_csv(p, index=False)


def load_csv(ind):
    """Return a normalized DataFrame for the indicator (Date, Value).
    Empty DataFrame if no data."""
    ensure_csv(ind)
    df = pd.read_csv(csv_path(ind["id"]))
    if df.empty:
        return df.assign(Date=pd.NaT)
    df["Period"] = df["Period"].astype(str).str.strip()
    df["Value"] = pd.to_numeric(df["Value"], errors="coerce")
    df = df.dropna(subset=["Value"])
    df["Date"] = pd.to_datetime(df["Period"] + "-01", errors="coerce")
    df = df.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)
    return df


def add_value(indicator_id, period, value):
    ind = next((i for i in INDICATORS if i["id"] == indicator_id), None)
    if ind is None:
        sys.exit("Unknown indicator id: %s. Use --list to see all." % indicator_id)
    try:
        datetime.strptime(period, "%Y-%m")
    except ValueError:
        sys.exit("Period must be YYYY-MM (e.g. 2025-04)")
    value = float(value)
    df = load_csv(ind)
    if df.empty:
        df = pd.DataFrame(columns=["Period", "Value", "Date"])
    df = df[df["Period"] != period]
    new = pd.DataFrame([{"Period": period, "Value": value,
                         "Date": pd.to_datetime(period + "-01")}])
    df = pd.concat([df, new], ignore_index=True).sort_values("Date")
    df[["Period", "Value"]].to_csv(csv_path(ind["id"]), index=False)
    print("  ✓ %s [%s] = %s saved." % (indicator_id, period, value))


# ===========================================================================
# Metric computation
# ===========================================================================

def compute_growth(df, metrics):
    """Add MoM/YoY columns based on requested metrics."""
    if df.empty:
        return df
    out = df.copy()
    if "MoM" in metrics:
        out["MoM_Pct"] = out["Value"].pct_change() * 100.0
    if "YoY" in metrics:
        out["YoY_Pct"] = out["Value"].pct_change(periods=12) * 100.0
    if "QoQ" in metrics:
        out["QoQ_Pct"] = out["Value"].pct_change(periods=3) * 100.0
    return out


# ===========================================================================
# Chart rendering — single HTML with one panel per indicator that has data,
# grouped by category. Empty indicators land in the "Pending" panel.
# ===========================================================================

CATEGORY_COLORS = {
    "Fiscal — Tax Revenue":     "#1f77b4",
    "Fiscal — Receipts":        "#2ca02c",
    "Fiscal — Expenditure":     "#9467bd",
    "Fiscal — Deficit":         "#d62728",
    "Inflation":                "#ff7f0e",
    "Industrial":               "#8c564b",
    "External Sector":          "#17becf",
    "Energy":                   "#bcbd22",
    "Infrastructure":           "#7f7f7f",
    "Employment":               "#e377c2",
    "Banking — RBI":            "#1f77b4",
    "Capital Markets — SEBI":   "#2ca02c",
}


def build_indicator_figure(ind, df):
    """Return a Plotly figure for one indicator, with growth metric subplots
    when applicable."""
    has_growth = bool(ind["metrics"]) and any(
        c in df.columns for c in ("MoM_Pct", "YoY_Pct", "QoQ_Pct"))
    if has_growth:
        rows = 1 + sum(1 for c in ("MoM_Pct", "QoQ_Pct", "YoY_Pct")
                       if c in df.columns)
    else:
        rows = 1

    titles = ["%s (%s)" % (ind["title"], ind["unit"])]
    for c in ("MoM_Pct", "QoQ_Pct", "YoY_Pct"):
        if c in df.columns:
            titles.append(c.replace("_Pct", " %"))

    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.55] + [0.45 / (rows - 1)] * (rows - 1) if rows > 1 else [1.0],
        subplot_titles=titles,
    )

    color = CATEGORY_COLORS.get(ind["category"], "#1f77b4")

    fig.add_trace(go.Bar(
        x=df["Date"], y=df["Value"],
        name=ind["title"], marker_color=color,
        hovertemplate="%{x|%b %Y}<br>" + ind["unit"] +
                      ": %{y:,.2f}<extra></extra>",
        showlegend=False,
    ), row=1, col=1)

    # 3M MA overlay where it adds value
    if len(df) >= 3:
        ma = df["Value"].rolling(3).mean()
        fig.add_trace(go.Scatter(
            x=df["Date"], y=ma, mode="lines",
            name="3M MA", line=dict(color="#ff7f0e", width=2),
            hovertemplate="%{x|%b %Y}<br>3M MA: %{y:,.2f}<extra></extra>",
            showlegend=False,
        ), row=1, col=1)

    r = 2
    for col_name, label in (("MoM_Pct", "MoM %"),
                            ("QoQ_Pct", "QoQ %"),
                            ("YoY_Pct", "YoY %")):
        if col_name not in df.columns:
            continue
        colors = ["#2ca02c" if v is not None and v >= 0 else "#d62728"
                  for v in df[col_name].fillna(0)]
        fig.add_trace(go.Bar(
            x=df["Date"], y=df[col_name], name=label,
            marker_color=colors,
            hovertemplate="%{x|%b %Y}<br>" + label +
                          ": %{y:.2f}%<extra></extra>",
            showlegend=False,
        ), row=r, col=1)
        fig.add_hline(y=0, line_color="gray", line_width=1, row=r, col=1)
        r += 1

    latest = df.iloc[-1]
    subtitle = "Latest: %s = %s %s" % (
        latest["Date"].strftime("%b %Y"),
        "{:,.2f}".format(latest["Value"]).rstrip("0").rstrip("."),
        ind["unit"])
    if "YoY_Pct" in df.columns and pd.notna(latest.get("YoY_Pct", None)):
        subtitle += "  |  YoY %+.2f%%" % latest["YoY_Pct"]
    if "MoM_Pct" in df.columns and pd.notna(latest.get("MoM_Pct", None)):
        subtitle += "  |  MoM %+.2f%%" % latest["MoM_Pct"]

    fig.update_layout(
        height=200 + 180 * rows,
        template="plotly_white",
        margin=dict(l=50, r=30, t=70, b=30),
        hovermode="x unified",
        title=dict(text="<b>%s</b><br><sub>%s — Source: %s</sub>" %
                        (ind["title"], subtitle, ind["source"]),
                   x=0.02, xanchor="left", font=dict(size=14)),
    )
    return fig


def build_dashboard():
    """Build a single combined HTML file containing per-indicator figures
    grouped by category, plus a 'Pending population' summary."""

    # Group indicators by category preserving registry order
    by_cat = defaultdict(list)
    for ind in INDICATORS:
        by_cat[ind["category"]].append(ind)

    populated, empty = [], []
    figures_by_cat = defaultdict(list)

    for cat, inds in by_cat.items():
        for ind in inds:
            df = load_csv(ind)
            if df.empty:
                empty.append(ind)
                continue
            df = compute_growth(df, ind["metrics"])
            populated.append(ind)
            figures_by_cat[cat].append((ind, df))

    # ── Build the HTML manually so we can interleave section headers and
    #    a Pending list. Each fig is embedded as a div via fig.to_html().
    parts = []
    parts.append("""<!DOCTYPE html>
<html><head><meta charset="utf-8"/>
<title>India Macro Dashboard</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial; margin: 0; background: #f4f6f8; color: #222; }
  header { background: linear-gradient(90deg,#1f3a5f,#2e75b6); color: #fff; padding: 24px 36px; }
  header h1 { margin: 0 0 6px 0; font-weight: 600; }
  header p { margin: 0; opacity: 0.9; font-size: 13px; }
  nav { background: #fff; padding: 14px 36px; border-bottom: 1px solid #d8dde2; position: sticky; top: 0; z-index: 10; }
  nav a { color: #1f3a5f; text-decoration: none; margin-right: 18px; font-size: 13px; font-weight: 500; }
  nav a:hover { text-decoration: underline; }
  section { padding: 24px 36px; }
  section h2 { color: #1f3a5f; border-bottom: 2px solid #2e75b6; padding-bottom: 6px; }
  .ind { background: #fff; border-radius: 6px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin: 16px 0; padding: 4px; }
  .pending { background: #fffbe6; border-left: 4px solid #fadb14; padding: 16px 24px; }
  .pending-grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(280px,1fr)); gap: 6px 18px; font-size: 12px; }
  .pending-grid div { padding: 4px 0; border-bottom: 1px dotted #eed; }
  code { background: #eef; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
  footer { padding: 18px 36px; font-size: 12px; color: #666; }
</style>
</head><body>""")

    parts.append("""<header>
  <h1>India Macro Dashboard</h1>
  <p>{populated} of {total} indicators populated • Sources: data.gov.in (OGD), CGA, SEBI, RBI • Generated {ts}</p>
</header>""".format(populated=len(populated), total=len(INDICATORS),
                   ts=datetime.now().strftime("%d-%b-%Y %H:%M IST")))

    # Nav
    nav_links = []
    for cat in by_cat:
        if figures_by_cat.get(cat):
            anchor = cat.lower().replace(" ", "-").replace("—", "")
            nav_links.append('<a href="#%s">%s</a>' % (anchor, cat))
    nav_links.append('<a href="#pending">Pending</a>')
    parts.append("<nav>%s</nav>" % " ".join(nav_links))

    # First fig: include plotly.js once via CDN; subsequent figs reference it
    first = True
    for cat, items in figures_by_cat.items():
        if not items:
            continue
        anchor = cat.lower().replace(" ", "-").replace("—", "")
        parts.append('<section id="%s"><h2>%s</h2>' % (anchor, cat))
        for ind, df in items:
            fig = build_indicator_figure(ind, df)
            html = pio.to_html(fig, include_plotlyjs="cdn" if first else False,
                               full_html=False, default_height="auto")
            first = False
            parts.append('<div class="ind">%s</div>' % html)
        parts.append("</section>")

    # Pending section
    parts.append('<section id="pending"><h2>Pending Population</h2>')
    parts.append('<div class="pending">')
    parts.append("<p><b>%d indicators</b> have no data yet. Populate them with: "
                 "<code>python3 india_macro.py --add &lt;id&gt; YYYY-MM &lt;value&gt;</code> "
                 "— values come from each indicator's official source noted "
                 "below.</p>" % len(empty))
    parts.append('<div class="pending-grid">')
    for ind in empty:
        parts.append('<div><b>%s</b> — %s<br><small>%s • %s</small></div>'
                     % (ind["id"], ind["title"], ind["category"], ind["source"]))
    parts.append('</div></div></section>')

    parts.append("""<footer>
Methodology: each indicator is stored in <code>data/india_macro/&lt;id&gt;.csv</code>
(Period,Value). On first run, indicators with built-in seed values are auto-populated;
the rest start empty. Update incrementally with the <code>--add</code> command. The
dashboard recomputes MoM / QoQ / YoY automatically per indicator metadata.
</footer></body></html>""")

    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))

    print("  ✓ Dashboard : %s   (%d populated, %d pending)"
          % (HTML_PATH, len(populated), len(empty)))


def build_excel():
    """Single workbook with one sheet per populated indicator + an overview."""
    overview_rows = []
    sheets = {}
    for ind in INDICATORS:
        df = load_csv(ind)
        latest_period = df["Period"].iloc[-1] if not df.empty else ""
        latest_value = df["Value"].iloc[-1] if not df.empty else None
        overview_rows.append({
            "ID": ind["id"], "Category": ind["category"],
            "Title": ind["title"], "Unit": ind["unit"],
            "Frequency": ind["freq"], "Source": ind["source"],
            "Rows": len(df), "Latest Period": latest_period,
            "Latest Value": latest_value,
        })
        if not df.empty:
            df = compute_growth(df, ind["metrics"])
            sheets[ind["id"][:31]] = df.drop(columns=["Date"])

    overview = pd.DataFrame(overview_rows)
    with pd.ExcelWriter(XLSX_PATH, engine="openpyxl") as w:
        overview.to_excel(w, sheet_name="_Overview", index=False)
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name, index=False)
    print("  ✓ Workbook  : %s   (%d data sheets + Overview)"
          % (XLSX_PATH, len(sheets)))


# ===========================================================================
# CLI
# ===========================================================================

# ---- OGD fetcher ---------------------------------------------------------

def ogd_fetch_all(resource_id, page_size=1000, max_pages=20):
    """Pull every record for a given OGD resource UUID. Returns list of dicts."""
    if not OGD_KEY:
        sys.exit("DATA_GOV_IN_API_KEY missing in .env")
    url = "%s/%s" % (OGD_BASE, resource_id)
    out = []
    offset = 0
    for _ in range(max_pages):
        params = {"api-key": OGD_KEY, "format": "json",
                  "limit": page_size, "offset": offset}
        try:
            r = requests.get(url, params=params, headers=OGD_HEADERS,
                             timeout=OGD_TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print("  ! OGD fetch failed: %s" % e)
            break
        records = data.get("records", [])
        if not records:
            break
        out.extend(records)
        if len(records) < page_size:
            break
        offset += page_size
    return out


def ogd_test(resource_id):
    """Print a sample row + field names for a given resource UUID."""
    if not OGD_KEY:
        sys.exit("DATA_GOV_IN_API_KEY missing in .env")
    url = "%s/%s" % (OGD_BASE, resource_id)
    r = requests.get(url, params={"api-key": OGD_KEY, "format": "json",
                                  "limit": 3}, headers=OGD_HEADERS,
                     timeout=OGD_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    print("Title  :", data.get("title", "?"))
    print("Org    :", " / ".join(data.get("org", [])))
    print("Total  :", data.get("total", "?"), "records")
    print("Updated:", data.get("updated_date", "?"))
    print("\nFields:")
    for f in data.get("field", []):
        print("  - %-30s  type=%s  label=%s" %
              (f.get("name"), f.get("type", "?"), f.get("label", "")))
    print("\nSample rows:")
    for rec in data.get("records", [])[:3]:
        print("  ", rec)


_UUID_RE = __import__("re").compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    __import__("re").IGNORECASE,
)


def ogd_resolve(query):
    """Take a data.gov.in URL, slug, or search term and resolve to UUID(s).

    - If `query` is already a UUID, return it.
    - If it's a data.gov.in URL, fetch the page and extract embedded UUIDs.
    - Otherwise treat as a search term: hit the public search page and
      extract the first few resource UUIDs from the rendered HTML.
    For each UUID found, probe the API to print title + record count
    so you can pick the right one.
    """
    q = query.strip()

    # Already a UUID?
    if _UUID_RE.fullmatch(q):
        ogd_test(q)
        return

    # URL or slug?
    if q.startswith("http"):
        url = q
    elif "/" not in q and " " not in q:
        url = "https://www.data.gov.in/resource/" + q
    else:
        # Search term — data.gov.in's own search is JS-rendered (no scrape),
        # so use DuckDuckGo HTML mirror with site:data.gov.in. Pull both
        # slug-style and UUID-style result links, then probe them.
        from urllib.parse import quote
        ddg = ("https://html.duckduckgo.com/html/?q=site%3Adata.gov.in+"
               + quote(q))
        print("Searching:", ddg)
        try:
            sr = requests.get(ddg, headers={"User-Agent": "Mozilla/5.0"},
                              timeout=30)
            sr.raise_for_status()
        except Exception as e:
            sys.exit("Search failed: %s" % e)
        import re as _re
        hits = _re.findall(r"data\.gov\.in/resource/([A-Za-z0-9_-]+)",
                           sr.text)
        # Dedup, preserve order
        seen, slugs = set(), []
        for h in hits:
            if h.lower() not in seen:
                seen.add(h.lower())
                slugs.append(h)
        if not slugs:
            sys.exit("No data.gov.in results found via search.")
        print("\nFound %d candidate page(s):" % len(slugs))
        # If a hit is already a UUID, probe it directly. Otherwise fetch
        # the page and pull UUIDs from it.
        all_uuids = []
        for slug in slugs[:5]:
            if _UUID_RE.fullmatch(slug):
                all_uuids.append(slug)
                continue
            page_url = "https://www.data.gov.in/resource/" + slug
            print("  → probing page:", slug[:70])
            try:
                pr = requests.get(page_url,
                                  headers={"User-Agent": "Mozilla/5.0"},
                                  timeout=30)
                for u in _UUID_RE.findall(pr.text):
                    if u.lower() not in [x.lower() for x in all_uuids]:
                        all_uuids.append(u)
            except Exception as e:
                print("    ! page fetch failed: %s" % e)
        if not all_uuids:
            sys.exit("Pages found but no UUIDs extracted.")
        print("\nResolved %d UUID(s):" % len(all_uuids))
        for u in all_uuids[:8]:
            print("\n--- %s ---" % u)
            try:
                rr = requests.get("%s/%s" % (OGD_BASE, u),
                                  params={"api-key": OGD_KEY,
                                          "format": "json", "limit": 1},
                                  headers=OGD_HEADERS, timeout=OGD_TIMEOUT)
                d = rr.json()
                print("  Title :", d.get("title", "?"))
                print("  Org   :", " / ".join(d.get("org", [])))
                print("  Total :", d.get("total", "?"), "records")
            except Exception as e:
                print("  ! probe failed: %s" % e)
        return

    print("Fetching:", url)
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                         timeout=30)
        r.raise_for_status()
    except Exception as e:
        sys.exit("Page fetch failed: %s" % e)

    uuids = []
    for u in _UUID_RE.findall(r.text):
        if u.lower() not in [x.lower() for x in uuids]:
            uuids.append(u)

    if not uuids:
        sys.exit("No UUIDs found on that page. Try a different URL or "
                 "search term.")

    print("\nFound %d candidate UUID(s):" % len(uuids))
    for u in uuids[:8]:
        print("\n--- %s ---" % u)
        try:
            rr = requests.get("%s/%s" % (OGD_BASE, u),
                              params={"api-key": OGD_KEY,
                                      "format": "json", "limit": 1},
                              headers=OGD_HEADERS, timeout=OGD_TIMEOUT)
            d = rr.json()
            print("  Title :", d.get("title", "?"))
            print("  Org   :", " / ".join(d.get("org", [])))
            print("  Total :", d.get("total", "?"), "records")
        except Exception as e:
            print("  ! probe failed: %s" % e)


def _parse_period(value, fmt):
    """Coerce a raw period value into YYYY-MM string."""
    s = str(value).strip()
    if fmt == "auto":
        # Try common formats
        for f in ("%Y-%m-%d", "%Y-%m", "%d/%m/%Y", "%d-%m-%Y",
                  "%b-%y", "%B-%Y", "%b %Y", "%B %Y", "%Y%m"):
            try:
                return datetime.strptime(s, f).strftime("%Y-%m")
            except ValueError:
                continue
        return None
    try:
        return datetime.strptime(s, fmt).strftime("%Y-%m")
    except ValueError:
        return None


def cmd_fetch(indicator_id):
    cfg = OGD_RESOURCES.get(indicator_id)
    if not cfg:
        sys.exit("No OGD mapping for '%s'. Add one in OGD_RESOURCES."
                 % indicator_id)
    ind = next((i for i in INDICATORS if i["id"] == indicator_id), None)
    if not ind:
        sys.exit("Unknown indicator: %s" % indicator_id)

    print("  → Fetching OGD %s (resource %s)" %
          (indicator_id, cfg["resource_id"]))
    records = ogd_fetch_all(cfg["resource_id"])
    if not records:
        print("  ! No records returned.")
        return 0

    pf = cfg["period_field"]
    vf = cfg["value_field"]
    fmt = cfg.get("period_format", "auto")
    flt = cfg.get("filter") or {}
    scale = cfg.get("scale", 1.0)
    agg = cfg.get("agg")  # 'sum' or 'mean' or None

    # Apply filter
    rows = []
    for rec in records:
        if any(str(rec.get(k, "")).strip().lower() != str(v).strip().lower()
               for k, v in flt.items()):
            continue
        period = _parse_period(rec.get(pf), fmt)
        try:
            val = float(str(rec.get(vf, "")).replace(",", "").strip())
        except (ValueError, TypeError):
            continue
        if period is None:
            continue
        rows.append((period, val * scale))

    if not rows:
        print("  ! No usable rows after parsing (check field names / format).")
        return 0

    df = pd.DataFrame(rows, columns=["Period", "Value"])
    if agg == "sum":
        df = df.groupby("Period", as_index=False)["Value"].sum()
    elif agg == "mean":
        df = df.groupby("Period", as_index=False)["Value"].mean()
    else:
        df = df.drop_duplicates(subset=["Period"], keep="last")
    df = df.sort_values("Period")

    # Merge with existing CSV (new rows + replace any overlap)
    existing = load_csv(ind)
    if not existing.empty:
        keep = existing[~existing["Period"].isin(df["Period"])]
        merged = pd.concat([keep[["Period", "Value"]], df], ignore_index=True)
    else:
        merged = df
    merged = merged.sort_values("Period")
    merged.to_csv(csv_path(ind["id"]), index=False)
    print("  ✓ %s: wrote %d rows (latest=%s)" %
          (indicator_id, len(merged), merged["Period"].iloc[-1]))
    return len(merged)


def cmd_fetch_all():
    if not OGD_RESOURCES:
        print("No OGD mappings registered yet. Add entries to OGD_RESOURCES.")
        return
    for indicator_id in OGD_RESOURCES:
        try:
            cmd_fetch(indicator_id)
        except SystemExit as e:
            print("  ! %s" % e)
        except Exception as e:
            print("  ! %s failed: %s" % (indicator_id, e))


# ===========================================================================
# DIRECT FETCHERS — non-OGD sources (RBI WSS, AMFI monthly Excel, etc.)
# Each fetcher returns a list of (period_YYYY_MM, value) tuples that get
# merged into the indicator's CSV.
# ===========================================================================

_BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                                  "Chrome/120.0 Safari/537.36"}


def _store_indicator_value(indicator_id, period, value):
    """Merge a single (period, value) into the indicator's CSV."""
    ind = next((i for i in INDICATORS if i["id"] == indicator_id), None)
    if not ind:
        return False
    ensure_csv(ind)
    df = load_csv(ind)
    if not df.empty and period in df["Period"].values:
        # Update existing row
        df.loc[df["Period"] == period, "Value"] = value
    else:
        new = pd.DataFrame([[period, value]], columns=["Period", "Value"])
        df = pd.concat([df[["Period", "Value"]] if not df.empty else
                        pd.DataFrame(columns=["Period", "Value"]),
                        new], ignore_index=True)
    df = df.sort_values("Period").drop_duplicates("Period", keep="last")
    df[["Period", "Value"]].to_csv(csv_path(indicator_id), index=False)
    return True


# ---- RBI Weekly Statistical Supplement ------------------------------------

RBI_WSS_LIST_URL = "https://www.rbi.org.in/Scripts/BS_viewWssExtract.aspx"


def _rbi_wss_latest_extract():
    """Find the URL of the latest WSS weekly extract.

    The WSS landing page contains javascript postback links of the form
    BS_viewWssExtract.aspx?SelectedDate=M/DD/YYYY. We pick the first
    (most recent) one and fetch it.
    """
    r = requests.get(RBI_WSS_LIST_URL, headers=_BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    import re as _re
    matches = _re.findall(r"BS_viewWssExtract\.aspx\?SelectedDate=(\d+/\d+/\d+)",
                          r.text)
    if not matches:
        return None, None
    selected = matches[0]
    detail_url = "%s?SelectedDate=%s" % (RBI_WSS_LIST_URL, selected)
    dr = requests.get(detail_url, headers=_BROWSER_HEADERS, timeout=30)
    dr.raise_for_status()
    return selected, dr.text


def _rbi_wss_pick_period(html):
    """Read the 'As on <date>' string from forex table to derive YYYY-MM."""
    import re as _re
    m = _re.search(r"As on ([A-Z][a-z]+\.?\s+\d{1,2},\s*\d{4})", html)
    if not m:
        return None
    raw = m.group(1).replace(".", "")
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return None


def fetch_rbi_wss():
    """Returns dict {indicator_id: (period, value)} for forex + bank series.

    Uses the latest WSS extract only — call monthly to accumulate history.
    """
    selected, html = _rbi_wss_latest_extract()
    if not html:
        return {}
    period = _rbi_wss_pick_period(html) or datetime.now().strftime("%Y-%m")

    out = {}
    try:
        tables = pd.read_html(html)
    except Exception as e:
        print("  ! WSS table parse failed:", e)
        return {}

    # T3: Forex Reserves — find by header keyword
    forex_t = None
    scb_t = None
    for t in tables:
        head = str(t.iloc[0:2].to_string()).lower()
        if "foreign exchange reserves" in head:
            forex_t = t
        elif "scheduled commercial banks" in head and "business in india" in head:
            scb_t = t

    if forex_t is not None:
        # Find rows by label in column 0
        def _find_row(df, label):
            for i, row in df.iterrows():
                if str(row.iloc[0]).strip().lower().startswith(label.lower()):
                    return row
            return None

        def _to_float(x):
            try:
                return float(str(x).replace(",", "").strip())
            except (ValueError, TypeError):
                return None

        # Column layout: 0=Item, 1=₹Cr, 2=US$ Mn, 3=Variation, ...
        # We want US$ Mn (col index 2) divided by 1000 to get $ Bn.
        r_total = _find_row(forex_t, "1 Total Reserves")
        r_fca   = _find_row(forex_t, "1.1 Foreign Currency Assets")
        r_gold  = _find_row(forex_t, "1.2 Gold")
        if r_total is not None:
            v = _to_float(r_total.iloc[2])
            if v is not None:
                out["forex_reserves"] = (period, round(v / 1000.0, 2))
        if r_fca is not None:
            v = _to_float(r_fca.iloc[2])
            if v is not None:
                out["forex_fca"] = (period, round(v / 1000.0, 2))
        if r_gold is not None:
            v = _to_float(r_gold.iloc[2])
            if v is not None:
                out["forex_gold"] = (period, round(v / 1000.0, 2))

    if scb_t is not None:
        # YoY growth % is in the last column. Find Aggregate Deposits & Bank Credit.
        def _find_row(df, label):
            for i, row in df.iterrows():
                if str(row.iloc[0]).strip().lower().startswith(label.lower()):
                    return i, row
            return None, None

        # The "Growth (Per cent)" row is right after the absolute row.
        # Last numeric column is the YoY % growth for the latest year.
        i_dep, _ = _find_row(scb_t, "2.1 Aggregate Deposits")
        if i_dep is not None and i_dep + 1 < len(scb_t):
            growth_row = scb_t.iloc[i_dep + 1]
            try:
                # The YoY% is the LAST column with the latest year header
                v = float(str(growth_row.iloc[-1]).strip())
                out["bank_deposit_total"] = (period, round(v, 2))
            except (ValueError, TypeError):
                pass
        i_cr, _ = _find_row(scb_t, "7 Bank Credit")
        if i_cr is not None and i_cr + 1 < len(scb_t):
            growth_row = scb_t.iloc[i_cr + 1]
            try:
                v = float(str(growth_row.iloc[-1]).strip())
                out["bank_credit_total"] = (period, round(v, 2))
            except (ValueError, TypeError):
                pass

    return out


# ---- AMFI Monthly Excel ----------------------------------------------------

AMFI_URL_TPL = "https://portal.amfiindia.com/spages/am%s%drepo.xls"
_MONTHS_LC = ["jan", "feb", "mar", "apr", "may", "jun",
              "jul", "aug", "sep", "oct", "nov", "dec"]


def _amfi_url(period):
    """period 'YYYY-MM' -> AMFI URL."""
    y, m = period.split("-")
    return AMFI_URL_TPL % (_MONTHS_LC[int(m) - 1], int(y))


def _amfi_latest_available():
    """Find the most recent month for which AMFI Excel exists. Returns YYYY-MM."""
    today = datetime.now()
    # Walk back up to 6 months from this month.
    for offset in range(0, 6):
        y, m = today.year, today.month - offset
        while m <= 0:
            m += 12
            y -= 1
        period = "%04d-%02d" % (y, m)
        url = _amfi_url(period)
        try:
            r = requests.head(url, headers=_BROWSER_HEADERS, timeout=15,
                              allow_redirects=True)
            if r.status_code == 200:
                return period, url
        except Exception:
            continue
    return None, None


def fetch_amfi():
    """Fetch latest available AMFI Monthly Report and extract aggregates.

    Returns dict {indicator_id: (period, value)} for:
      mf_aum_total, mf_aum_debt, mf_aum_equity, mf_aum_hybrid,
      folios_debt, folios_equity, folios_hybrid
    """
    period, url = _amfi_latest_available()
    if not period:
        print("  ! No recent AMFI monthly file found.")
        return {}
    print("  ↓ AMFI %s : %s" % (period, url))
    try:
        r = requests.get(url, headers=_BROWSER_HEADERS, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print("  ! AMFI download failed:", e)
        return {}
    import io
    try:
        xls = pd.ExcelFile(io.BytesIO(r.content))
        # Sheet name changed from "AMFI MONTHLY" to "MCR_Report" circa 2026
        sheet = None
        for candidate in ("AMFI MONTHLY", "MCR_Report"):
            if candidate in xls.sheet_names:
                sheet = candidate
                break
        if sheet is None:
            sheet = xls.sheet_names[0]
        df = pd.read_excel(xls, sheet_name=sheet, header=None)
    except Exception as e:
        print("  ! AMFI parse failed:", e)
        return {}

    # Sub Total - I = Debt (Income/Debt Oriented)
    # Sub Total - II = Equity (Growth/Equity Oriented)
    # Sub Total - III = Hybrid
    # Grand Total = industry total
    out = {}

    def _row_matching(label):
        for i, row in df.iterrows():
            c1 = str(row.iloc[1]) if pd.notna(row.iloc[1]) else ""
            if c1.strip().lower().startswith(label.lower()):
                return row
        return None

    def _f(x):
        try:
            return float(str(x).replace(",", "").strip())
        except (ValueError, TypeError):
            return None

    grand = _row_matching("Grand Total")
    if grand is not None:
        aum_cr = _f(grand.iloc[7])      # ₹ Cr
        folios = _f(grand.iloc[3])
        if aum_cr is not None:
            out["mf_aum_total"] = (period, round(aum_cr / 1e5, 2))  # ₹ Lakh Cr

    sub_i   = _row_matching("Sub Total - I")    # Debt
    sub_ii  = _row_matching("Sub Total - II")   # Equity
    sub_iii = _row_matching("Sub Total - III")  # Hybrid

    for srow, aum_key, folio_key in (
        (sub_i,   "mf_aum_debt",   "folios_debt"),
        (sub_ii,  "mf_aum_equity", "folios_equity"),
        (sub_iii, "mf_aum_hybrid", "folios_hybrid"),
    ):
        if srow is None:
            continue
        aum = _f(srow.iloc[7])
        fol = _f(srow.iloc[3])
        if aum is not None:
            out[aum_key] = (period, round(aum / 1e5, 2))   # ₹ Lakh Cr
        if fol is not None:
            out[folio_key] = (period, round(fol / 1e7, 2))  # Crore (divide by 1cr)

    return out


# ---- PDF helpers ----------------------------------------------------------
# Some Indian government domains (cea.nic.in, dpiit.gov.in, etc.) negotiate
# TLS versions that LibreSSL on macOS rejects. We shell out to the system
# `curl` (which uses Secure Transport / OpenSSL) as the download backend so
# the fetcher works on stock macOS Python.

import subprocess as _subprocess
import tempfile as _tempfile


def _curl_download(url, timeout=60):
    """Download `url` via system curl, return bytes or None on failure."""
    try:
        with _tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tf:
            tmp_path = tf.name
        proc = _subprocess.run(
            ["curl", "-skL", "-A", _BROWSER_HEADERS["User-Agent"],
             "--max-time", str(timeout), "-o", tmp_path, url],
            capture_output=True, timeout=timeout + 10)
        if proc.returncode != 0:
            return None
        with open(tmp_path, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _http_get_html(url, timeout=30):
    """GET text via curl (handles TLS quirks)."""
    data = _curl_download(url, timeout=timeout)
    if data is None:
        return None
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return None


def _pdf_pages_text(pdf_bytes):
    """Yield (page_number, text) for each page of an in-memory PDF."""
    try:
        import pdfplumber
    except ImportError:
        print("  ! pdfplumber not installed; run: pip install pdfplumber")
        return
    import io as _io
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore")
        with pdfplumber.open(_io.BytesIO(pdf_bytes)) as pdf:
            for i, page in enumerate(pdf.pages):
                yield i + 1, (page.extract_text() or "")


# ---- CEA Executive Summary (monthly PDF) ----------------------------------
# Source landing page lists all monthly PDFs.  We pick the most recent one
# from the page HTML, then parse it for:
#   - electricity_generation (BU, "All India" row, current-month achievement)
#   - power_generation_state (same number — all-India total)
#   - renewable_capacity     (GW, "RES (including large hydro)" total)

CEA_EXEC_INDEX_URL = "https://cea.nic.in/executive-summary-report/?lang=en"
_MONTH_ABBR_TO_NUM = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"])}


def _cea_discover_latest_executive_pdf():
    """Return (period_YYYY_MM, pdf_url) for the most recent monthly Exec Summary."""
    html = _http_get_html(CEA_EXEC_INDEX_URL, timeout=20)
    if not html:
        return None, None
    import re as _re
    # URLs look like .../executive/2026/03/Executive_Summary_March_2026_Actual.pdf
    pat = _re.compile(
        r'(https://cea\.nic\.in/wp-content/uploads/executive/(\d{4})/(\d{2})/'
        r'Executive_Summary_(\w+)_(\d{4})[^"\s]+\.pdf)',
        _re.IGNORECASE)
    best = None
    for m in pat.finditer(html):
        url, y_dir, m_dir, mon_name, y_name = m.groups()
        mn = _MONTH_ABBR_TO_NUM.get(mon_name.capitalize())
        if mn is None:
            try:
                mn = int(m_dir)
            except ValueError:
                continue
        period = "%s-%02d" % (y_name, mn)
        if best is None or period > best[0]:
            best = (period, url)
    return best if best else (None, None)


def fetch_cea_executive_summary():
    period, url = _cea_discover_latest_executive_pdf()
    if not url:
        print("  ! Could not discover latest CEA Executive Summary PDF.")
        return {}
    print("  ↓ CEA %s : %s" % (period, url))
    pdf = _curl_download(url, timeout=60)
    if not pdf:
        print("  ! Download failed.")
        return {}

    out = {}
    import re as _re

    # We scan the first ~15 pages for two anchor sections.
    gen_bu = None
    res_mw = None
    for pn, txt in _pdf_pages_text(pdf):
        if pn > 15:
            break

        # --- Electricity Generation page (1A) ---------------------------
        # Look for the "All India" total row in the monthly table.
        # Line shape: "All India 160.32 173.26 161.70 0.86"
        if gen_bu is None and "Electricity Generation" in txt:
            for line in txt.splitlines():
                if line.strip().startswith("All India"):
                    nums = _re.findall(r"[-+]?\d+\.\d+", line)
                    # 4 numbers expected: prev-yr achievement, target,
                    # current achievement, % change.
                    if len(nums) >= 3:
                        try:
                            gen_bu = float(nums[2])
                        except ValueError:
                            pass
                    break

        # --- Installed Capacity (Region-wise) -------------------------
        # Look for a "Total=" or summary line; otherwise sum the RES col.
        if res_mw is None and "All India Installed Capacity" in txt:
            # Try to find the totals row that contains the date marker.
            # Pattern: "<date> 221939.98 6620.00 20122.42 589.20 249271.59 8780.00 51414.66 274688.09 532739.68"
            for line in txt.splitlines():
                nums = _re.findall(r"\d+\.\d+", line)
                # Need a long numeric row with >=8 floats (8 cols + grand total).
                if len(nums) >= 9:
                    try:
                        # Column ordering in the PDF:
                        #  Coal, Lignite, Gas, Diesel, Total(Thermal), Nuclear,
                        #  Hydro(Large), RES(incl large hydro), Grand Total
                        # Position 7 (0-indexed) = RES including large hydro
                        candidate = float(nums[7])
                        # Sanity: should be > 200,000 MW for current India
                        if candidate > 100000:
                            res_mw = candidate
                            break
                    except ValueError:
                        continue

        if gen_bu is not None and res_mw is not None:
            break

    if gen_bu is not None:
        out["electricity_generation"] = (period, round(gen_bu, 2))
        out["power_generation_state"] = (period, round(gen_bu, 2))
    if res_mw is not None:
        out["renewable_capacity"] = (period, round(res_mw / 1000.0, 2))

    return out


# ---- PPAC Monthly "Snapshot of India's Oil & Gas data" (PDF) -------------
# Source landing page lists the latest monthly snapshot under
# /download.php?file=rep_studies/<ts>_Snapshot_of_Indias_Oil_Gas_data_<Month>_<Year>_Final_A5.pdf
# Page 9 of the PDF has the "Crude oil, LNG and petroleum products at a glance"
# table with the structure (after de-headering):
#   Row "2 Crude oil production in India# MMT a b c d e f"
#   Row "3 Consumption of petroleum products* MMT a b c d e f"
# where columns are: 2023-24, 2024-25, Mar prev-yr, Mar curr-yr (= latest month),
# Apr-Mar prev, Apr-Mar curr.

PPAC_HOME = "https://www.ppac.gov.in/"


def _ppac_discover_latest_snapshot():
    """Return (period_YYYY_MM, url) for the most recent PPAC Snapshot PDF."""
    html = _http_get_html(PPAC_HOME, timeout=20)
    if not html:
        return None, None
    import re as _re
    pat = _re.compile(
        r'(https://ppac\.gov\.in/download\.php\?file=rep_studies/[^"\s]*?'
        r'Snapshot_of_Indias_Oil_Gas_data_([A-Za-z]+)_(\d{4})[^"\s]*\.pdf)',
        _re.IGNORECASE)
    best = None
    for m in pat.finditer(html):
        url, mon_name, year = m.groups()
        mn = _MONTH_ABBR_TO_NUM.get(mon_name.capitalize())
        if mn is None:
            continue
        period = "%s-%02d" % (year, mn)
        if best is None or period > best[0]:
            best = (period, url)
    return best if best else (None, None)


def fetch_ppac_snapshot():
    period, url = _ppac_discover_latest_snapshot()
    if not url:
        print("  ! Could not discover latest PPAC Snapshot PDF.")
        return {}
    print("  ↓ PPAC %s : %s" % (period, url))
    pdf = _curl_download(url, timeout=60)
    if not pdf:
        print("  ! Download failed.")
        return {}

    out = {}
    import re as _re
    crude = None
    consumption = None

    for pn, txt in _pdf_pages_text(pdf):
        if pn > 15:
            break
        for line in txt.splitlines():
            stripped = line.strip()
            # Crude oil production row.  Match flexibly to allow # / * markers.
            if crude is None and _re.match(
                    r"^2\s+Crude\s*oil\s+production\s+in\s+India",
                    stripped, _re.IGNORECASE):
                nums = _re.findall(r"\d+\.?\d*", stripped)
                # nums[0] is the leading "2".  Skip it.
                # Then 6 data cols: full-yr 2023-24, full-yr 2024-25,
                # Mar prev, Mar curr, Apr-Mar prev, Apr-Mar curr.
                vals = nums[1:]
                if len(vals) >= 4:
                    try:
                        crude = float(vals[3])
                    except ValueError:
                        pass
            elif consumption is None and _re.match(
                    r"^3\s+Consumption\s+of\s+petroleum\s+products",
                    stripped, _re.IGNORECASE):
                nums = _re.findall(r"\d+\.?\d*", stripped)
                vals = nums[1:]
                if len(vals) >= 4:
                    try:
                        consumption = float(vals[3])
                    except ValueError:
                        pass
        if crude is not None and consumption is not None:
            break

    if crude is not None:
        out["crude_oil_production"] = (period, round(crude, 2))
    if consumption is not None:
        out["petroleum_consumption"] = (period, round(consumption, 2))
    return out


# ---- NSDL FPI Monthly aggregation -----------------------------------------
# Source: https://www.fpi.nsdl.co.in/web/Reports/Monthly.aspx
# Returns a table of daily FPI investments for the current month with rows
# tagged Equity / Debt-General Limit / Debt-VRR / Hybrid / AIFs and an
# "Investment Route" sub-classification. We aggregate the "Sub-total" rows
# for the latest calendar month to produce monthly net flows in ₹ Cr.

NSDL_FPI_MONTHLY = "https://www.fpi.nsdl.co.in/web/Reports/Monthly.aspx"


def _parse_paren_num(s):
    """NSDL prints negatives as '(1234.56)'. Convert to float."""
    if s is None:
        return None
    txt = str(s).strip().replace(",", "")
    if not txt or txt.lower() == "nan":
        return None
    neg = txt.startswith("(") and txt.endswith(")")
    txt = txt.strip("()")
    try:
        v = float(txt)
        return -v if neg else v
    except ValueError:
        return None


def fetch_nsdl_fpi_monthly():
    """Return monthly FPI net investment by category for the latest month."""
    headers = dict(_BROWSER_HEADERS)
    headers["Accept"] = "text/html,application/xhtml+xml,*/*"
    headers["Accept-Language"] = "en-US,en;q=0.9"
    try:
        r = requests.get(NSDL_FPI_MONTHLY, headers=headers, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print("  ! NSDL FPI download failed:", e)
        return {}

    try:
        tables = pd.read_html(r.text)
    except Exception as e:
        print("  ! NSDL FPI parse failed:", e)
        return {}
    if not tables:
        return {}

    df = tables[0]
    # Flatten multi-level columns
    df.columns = [c[-1] if isinstance(c, tuple) else c for c in df.columns]
    cmap = {c: c for c in df.columns}
    # Standardise expected names
    date_col = next((c for c in df.columns if "Reporting" in str(c)), None)
    cat_col = next((c for c in df.columns if "Equity" in str(c) or "Debt" in str(c)
                    and "Investment" not in str(c)), None)
    route_col = next((c for c in df.columns if "Investment Route" in str(c)), None)
    net_col = next((c for c in df.columns if "Net Investment (Rs" in str(c)), None)
    if not (date_col and cat_col and route_col and net_col):
        print("  ! NSDL FPI: columns not recognised:", list(df.columns))
        return {}

    df = df[[date_col, cat_col, route_col, net_col]].rename(columns={
        date_col: "date", cat_col: "category",
        route_col: "route", net_col: "net_cr"})
    df["date"] = pd.to_datetime(df["date"], format="%d-%b-%Y", errors="coerce")
    df = df.dropna(subset=["date"]).copy()
    df["net_cr"] = df["net_cr"].map(_parse_paren_num)
    df = df.dropna(subset=["net_cr"])

    # Pick the most recent month present in the data
    latest = df["date"].max()
    period = latest.strftime("%Y-%m")
    same_month = df[df["date"].dt.strftime("%Y-%m") == period]

    # Sum only Sub-total rows so we don't double-count Stock+Primary+Sub-total.
    sub = same_month[same_month["route"].astype(str)
                     .str.strip().str.lower() == "sub-total"]

    def _sum_category(cat_predicate):
        mask = sub["category"].astype(str).map(cat_predicate)
        return round(float(sub.loc[mask, "net_cr"].sum()), 2)

    eq_net = _sum_category(lambda c: c.strip().lower() == "equity")
    # All Debt buckets: General Limit, VRR, Long Term, etc.
    debt_net = _sum_category(lambda c: c.strip().lower().startswith("debt"))

    out = {}
    if not pd.isna(eq_net):
        out["fpi_equity"] = (period, eq_net)
    if not pd.isna(debt_net):
        out["fpi_debt"] = (period, debt_net)
    return out


# ---- Ministry of Steel: Monthly Economic Report (PDF) ---------------------
# Index page lists monthly PDFs; Annexure-I (typically last page) holds clean
# fiscal-YTD figures for Crude Steel & Finished Steel Production (in Mt).

STEEL_INDEX_URL = "https://steel.gov.in/monthly-summary"


def _steel_discover_latest_pdf():
    html = _http_get_html(STEEL_INDEX_URL, timeout=30)
    if not html:
        return None, None
    import re as _re
    # filenames vary; just locate "<Month> <YYYY>" inside any .pdf href
    href_pat = _re.compile(r'href="(/sites/default/files/[^"]+\.pdf)"', _re.IGNORECASE)
    month_pat = _re.compile(
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)[%\s_,]+(20\d{2})",
        _re.IGNORECASE)
    best = None
    for m in href_pat.finditer(html):
        href = m.group(1)
        decoded = href.replace("%20", " ")
        if "monthly economic report" not in decoded.lower():
            continue
        mm = month_pat.search(decoded)
        if not mm:
            continue
        mn = _MONTH_ABBR_TO_NUM.get(mm.group(1).capitalize())
        if mn is None:
            continue
        period = "%s-%02d" % (mm.group(2), mn)
        url = "https://steel.gov.in" + href
        if best is None or period > best[0]:
            best = (period, url)
    return best if best else (None, None)


def fetch_steel_monthly():
    period, url = _steel_discover_latest_pdf()
    if not url:
        print("  ! Could not discover latest Ministry of Steel PDF.")
        return {}
    print("  ↓ Steel %s : %s" % (period, url))
    pdf = _curl_download(url, timeout=60)
    if not pdf:
        print("  ! Download failed.")
        return {}

    import re as _re
    crude = None
    finished = None
    for pn, txt in _pdf_pages_text(pdf):
        for line in txt.splitlines():
            stripped = line.strip()
            if crude is None and stripped.lower().startswith("1 crude steel production"):
                nums = _re.findall(r"-?\d+\.\d+|-?\d+", stripped)
                # leading "1" then 4 fiscal-year columns; take the last one (current FY)
                if len(nums) >= 5:
                    try:
                        crude = float(nums[-1])
                    except ValueError:
                        pass
            elif finished is None and stripped.lower().startswith("2 finished steel production"):
                nums = _re.findall(r"-?\d+\.\d+|-?\d+", stripped)
                if len(nums) >= 5:
                    try:
                        finished = float(nums[-1])
                    except ValueError:
                        pass
        if crude is not None and finished is not None:
            break

    out = {}
    if crude is not None:
        out["steel_production"] = (period, round(crude, 2))
    if finished is not None:
        out["steel_dispatch"] = (period, round(finished, 2))
    return out


# ---- Department of Fertilizers: Monthly Bulletin (PDF) --------------------
# Page with "Production, Import, Availability and Sales of Fertilizers during
# <Month>, <Year>" contains rows for Urea / DAP / MOP / Complexes; last
# column is Sales (in Lakh Metric Tonnes). We sum them as fertilizer_dispatch.

FERT_INDEX_URL = "https://fert.gov.in/documents/reports/monthly-bulletin"


def _fert_discover_latest_pdf():
    html = _http_get_html(FERT_INDEX_URL, timeout=30)
    if not html:
        return None, None
    import re as _re
    href_pat = _re.compile(r'href="(/sites/default/files/[^"]+\.pdf)"', _re.IGNORECASE)
    month_pat = _re.compile(
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)[%\s_,]+(20\d{2})",
        _re.IGNORECASE)
    best = None
    for m in href_pat.finditer(html):
        href = m.group(1)
        # skip Hindi versions (URL-encoded Devanagari)
        if "%E0%A4" in href or "%e0%a4" in href:
            continue
        decoded = href.replace("%20", " ").replace("%2C", ",")
        if "monthly" not in decoded.lower() or "bulletin" not in decoded.lower():
            continue
        mm = month_pat.search(decoded)
        if not mm:
            continue
        mn = _MONTH_ABBR_TO_NUM.get(mm.group(1).capitalize())
        if mn is None:
            continue
        period = "%s-%02d" % (mm.group(2), mn)
        url = "https://fert.gov.in" + href
        if best is None or period > best[0]:
            best = (period, url)
    return best if best else (None, None)


def fetch_fertilizer_monthly():
    period, url = _fert_discover_latest_pdf()
    if not url:
        print("  ! Could not discover latest Fertilizer monthly bulletin.")
        return {}
    print("  ↓ Fert  %s : %s" % (period, url))
    pdf = _curl_download(url, timeout=60)
    if not pdf:
        print("  ! Download failed.")
        return {}

    import re as _re
    row_pat = _re.compile(r"^(Urea|DAP|MOP|Complexes)\b\s+(.+)$", _re.IGNORECASE)
    sales = {}
    for pn, txt in _pdf_pages_text(pdf):
        for line in txt.splitlines():
            m = row_pat.match(line.strip())
            if not m:
                continue
            label = m.group(1).capitalize()
            if label in sales:
                continue
            nums = _re.findall(r"\d+\.\d+|\d+", m.group(2))
            if not nums:
                continue
            try:
                sales[label] = float(nums[-1])
            except ValueError:
                pass
        if len(sales) == 4:
            break

    if not sales:
        print("  ! No Urea/DAP/MOP/Complexes row located.")
        return {}
    total = round(sum(sales.values()), 2)
    print("  · Sales (LMT): %s  -> total %.2f" %
          (", ".join("%s=%s" % (k, sales[k]) for k in sales), total))
    return {"fertilizer_dispatch": (period, total)}


# ---- Playwright-based fetchers (lazy import) ------------------------------
# These require:  pip install playwright  &&  playwright install chromium
# Imports live inside each fetcher so missing dep does not break --fetch-direct.

NSDL_STATS_URL = "https://nsdl.co.in/about/statistics.php"


def fetch_nsdl_demat():
    """Cumulative NSDL demat investor account count (Cr).

    NSDL renders the 'Statistics' summary card server-side, so a plain
    requests.get with a browser UA suffices (no Playwright needed).
    """
    html = _http_get_html(NSDL_STATS_URL, timeout=30)
    if not html:
        print("  ! NSDL statistics page fetch failed.")
        return {}

    import re as _re
    tables = _re.findall(r"<table[^>]*>.*?</table>", html, _re.DOTALL | _re.IGNORECASE)
    period, accounts, raw_accounts = None, None, None
    date_pat = _re.compile(
        r"(January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2},?\s+(20\d{2})",
        _re.IGNORECASE)
    acct_pat = _re.compile(
        r"Investor\s+Accounts[^0-9]*([\d,]+)", _re.IGNORECASE)
    for tab in tables:
        text = _re.sub(r"<[^>]+>", " ", tab)
        text = _re.sub(r"\s+", " ", text).strip()
        dm = date_pat.search(text)
        am = acct_pat.search(text)
        if dm and am:
            mn = _MONTH_ABBR_TO_NUM.get(dm.group(1).capitalize())
            if mn is not None:
                period = "%s-%02d" % (dm.group(2), mn)
            try:
                # Indian comma format: 4,43,89,501 -> 44389501
                raw_accounts = am.group(1)
                accounts = int(raw_accounts.replace(",", ""))
            except ValueError:
                pass
            break

    if period is None or accounts is None:
        print("  ! Could not locate NSDL summary row (date=%r accounts=%r)" %
              (period, accounts))
        return {}

    val = round(accounts / 1e7, 4)
    print("  · NSDL %s : %s accounts -> %.4f Cr" % (period, raw_accounts, val))
    return {"depository_demat_nsdl": (period, val)}


# ---- CDSL Periodic Stats (PDF) -------------------------------------------
# Index page (server-rendered HTML) lists monthly PDFs at
#   /Downloads/Publications/Periodic Stats/<Mon>-<YYYY>.pdf
# Each PDF contains a table whose row "9 Total ... <closing accounts>" is
# the cumulative number of Beneficiary Owners (BO) accounts at month-end.

CDSL_INDEX_URL = "https://www.cdslindia.com/Publications/periodicstats.aspx"
_MONTH_FULL_TO_NUM = dict(_MONTH_ABBR_TO_NUM)
_MONTH_FULL_TO_NUM.update({m[:3]: i for m, i in _MONTH_ABBR_TO_NUM.items()})


def _cdsl_discover_latest_pdf():
    html = _http_get_html(CDSL_INDEX_URL, timeout=30)
    if not html:
        return None, None
    import re as _re
    pat = _re.compile(
        r'href="([^"]*?Periodic[%\s_]*Stats/([A-Za-z]+)-(\d{4})\.pdf)"',
        _re.IGNORECASE)
    best = None
    for m in pat.finditer(html):
        href, mon, year = m.group(1), m.group(2), m.group(3)
        # Normalise: take first 3 chars, capitalised
        key = mon.capitalize()[:3]
        # Build month number using {Jan: 1, Feb: 2, ...}
        mn = next((i for n, i in _MONTH_ABBR_TO_NUM.items()
                   if n.startswith(key)), None)
        if mn is None:
            continue
        period = "%s-%02d" % (year, mn)
        url = href if href.startswith("http") else (
            "https://www.cdslindia.com" + href.lstrip(".").replace(" ", "%20"))
        # The href in index uses '../Downloads/...'; canonicalise:
        if url.startswith("https://www.cdslindia.com../"):
            url = url.replace("https://www.cdslindia.com../",
                              "https://www.cdslindia.com/")
        if best is None or period > best[0]:
            best = (period, url)
    return best if best else (None, None)


def fetch_cdsl_demat():
    period, url = _cdsl_discover_latest_pdf()
    if not url:
        print("  ! Could not discover latest CDSL Periodic Stats PDF.")
        return {}
    print("  ↓ CDSL %s : %s" % (period, url))
    pdf = _curl_download(url, timeout=60)
    if not pdf:
        print("  ! Download failed.")
        return {}

    import re as _re
    accounts = None
    raw = None
    # Look for the "Total" row in Section I A(i): last numeric cell is closing BO.
    # Format observed:  "9Total 180123835 2080094 152825 182051087"
    # (digits may be glued with the leading sr-no.)
    total_pat = _re.compile(r"^\s*\d?\s*Total\b(.*)$", _re.IGNORECASE)
    for pn, txt in _pdf_pages_text(pdf):
        if pn > 2:
            break
        for line in txt.splitlines():
            m = total_pat.match(line)
            if not m:
                continue
            # Ignore Custody-value Total (page 1 lower); accept first match.
            tail = m.group(1)
            nums = _re.findall(r"\d+", tail)
            if len(nums) >= 4:
                try:
                    accounts = int(nums[-1])
                    raw = nums[-1]
                except ValueError:
                    pass
                break
        if accounts is not None:
            break

    if accounts is None:
        print("  ! Could not locate CDSL 'Total' row.")
        return {}

    val = round(accounts / 1e7, 4)
    print("  · CDSL %s : %s accounts -> %.4f Cr" % (period, raw, val))
    return {"depository_demat_cdsl": (period, val)}


# ---- PPAC LPG: Active domestic customers (XLSX) --------------------------
PPAC_LPG_URL = "https://ppac.gov.in/uploads/page-images/1747729777_active-domestic-cus-lpg.xlsx"


def fetch_ppac_lpg():
    """LPG active domestic connections (Cr) — PPAC monthly XLSX."""
    from io import BytesIO
    data = _curl_download(PPAC_LPG_URL, timeout=45)
    if not data:
        print("  ! PPAC LPG download failed")
        return {}
    df = pd.read_excel(BytesIO(data), sheet_name="Active domestic customers ", header=None)
    # Header date in row index 3, col 1 (e.g. Timestamp 2025-04-01)
    period_cell = df.iloc[3, 1]
    try:
        ts = pd.to_datetime(period_cell)
        period = "%04d-%02d" % (ts.year, ts.month)
    except Exception:
        print("  ! Could not parse PPAC LPG date cell: %r" % period_cell)
        return {}
    # Find the ALL INDIA total row
    val = None
    for i in range(df.shape[0]):
        cell = df.iloc[i, 0]
        if isinstance(cell, str) and "ALL INDIA" in cell.upper():
            try:
                val = float(df.iloc[i, 1])
                break
            except Exception:
                continue
    if val is None:
        print("  ! Could not find ALL INDIA row in PPAC LPG sheet")
        return {}
    # Source units = Lakhs, target = Cr (1 Cr = 100 Lakh)
    return {"lpg_connections": (period, round(val / 100.0, 4))}


# ---- PPAC CGD network: PNG domestic connections (XLSX) -------------------
PPAC_CGD_URL = "https://ppac.gov.in/uploads/page-images/1777265473_8_CGD-Network-Web.xlsx"


def fetch_ppac_png():
    """PNG domestic connections (Cr) — PPAC CGD Network XLSX."""
    from io import BytesIO
    import re
    data = _curl_download(PPAC_CGD_URL, timeout=45)
    if not data:
        print("  ! PPAC CGD download failed")
        return {}
    xls = pd.ExcelFile(BytesIO(data))
    # Sheet name pattern: "Latest as on DD.MM.YYYY"
    target_sheet = None
    target_date = None
    for s in xls.sheet_names:
        m = re.search(r"Latest\s+as\s+on\s+(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", s, re.IGNORECASE)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            target_sheet = s
            target_date = (y, mo)
            break
    if target_sheet is None:
        print("  ! Could not find a 'Latest as on' sheet in PPAC CGD")
        return {}
    df = pd.read_excel(BytesIO(data), sheet_name=target_sheet, header=None)
    # Find Grand Total row (col 2), Domestic value at col 4
    val = None
    for i in range(df.shape[0]):
        cell = df.iloc[i, 2] if df.shape[1] > 2 else None
        if isinstance(cell, str) and "GRAND TOTAL" in cell.upper():
            try:
                val = float(df.iloc[i, 4])
                break
            except Exception:
                continue
    if val is None:
        # Fallback: any row whose first non-null cell == 'Grand Total'
        for i in range(df.shape[0]):
            row = df.iloc[i].dropna().astype(str).tolist()
            if row and "GRAND TOTAL" in row[0].upper():
                # Use the first numeric > 1e6 in the row as Domestic
                for v in df.iloc[i].tolist():
                    try:
                        x = float(v)
                        if x > 1e6:
                            val = x
                            break
                    except Exception:
                        continue
                if val is not None:
                    break
    if val is None:
        print("  ! Could not find Grand Total row in PPAC CGD")
        return {}
    period = "%04d-%02d" % target_date
    # Source units = absolute connections, target = Cr (1 Cr = 1e7)
    return {"png_connections": (period, round(val / 1e7, 4))}


# ---- OEA Core-8 Industries: Cement Index (XLSX) --------------------------
EAINDUSTRY_HOME = "https://eaindustry.nic.in/"


def fetch_core8_cement():
    """Cement production proxy (Core-8 Index, base 2011-12=100). OEA monthly XLSX.

    Note: source publishes an *index*, not Mn Tonnes; stored as index value.
    """
    from io import BytesIO
    import re
    from urllib.parse import urljoin
    home = _http_get_html(EAINDUSTRY_HOME, timeout=30)
    if not home:
        print("  ! eaindustry.nic.in not reachable")
        return {}
    # Find the latest Core_Industries_2011_12_<date>.xlsx link
    matches = re.findall(r'href="([^"]*Core_Industries_2011_12_[^"]+\.xlsx)"', home, flags=re.IGNORECASE)
    if not matches:
        print("  ! No Core_Industries XLSX link found on eaindustry homepage")
        return {}
    # Pick lexicographically last (date in filename → newest)
    rel = sorted(matches)[-1]
    url = rel if rel.startswith("http") else urljoin(EAINDUSTRY_HOME, rel)
    data = _curl_download(url, timeout=45)
    if not data:
        print("  ! Core_Industries XLSX download failed: %s" % url)
        return {}
    df = pd.read_excel(BytesIO(data), sheet_name="Index", header=None)
    # Iterate rows: take last row whose col 0 parses as a Timestamp
    last_period = None
    last_val = None
    for i in range(df.shape[0]):
        cell = df.iloc[i, 0]
        try:
            ts = pd.to_datetime(cell)
        except Exception:
            continue
        if pd.isna(ts):
            continue
        try:
            v = float(df.iloc[i, 8])  # col 8 = Cement Index
        except Exception:
            continue
        if pd.isna(v):
            continue
        last_period = "%04d-%02d" % (ts.year, ts.month)
        last_val = v
    if last_val is None:
        print("  ! Could not parse Core-8 Cement series")
        return {}
    return {"cement_production": (last_period, round(last_val, 2))}


# ---- NSDL FPI AUC: top-5 country share (HTML) ----------------------------
NSDL_FPI_COUNTRY_URL = "https://www.fpi.nsdl.co.in/web/Reports/ReportDetail.aspx?RepID=14"
NSDL_FPI_CLIENT_URL  = "https://www.fpi.nsdl.co.in/web/Reports/ReportDetail.aspx?RepID=22"


def _nsdl_fpi_period_from_html(html):
    """Extract reporting month (YYYY-MM) from page heading like 'April 2026'."""
    import re
    m = re.search(r"(January|February|March|April|May|June|July|August|"
                  r"September|October|November|December)\s+(20\d{2})", html, re.IGNORECASE)
    if not m:
        return None
    mon = _MONTH_ABBR_TO_NUM.get(m.group(1).title()[:3]) or _MONTH_ABBR_TO_NUM.get(m.group(1).title())
    if mon is None:
        # Full month name fallback
        full = {"January":1,"February":2,"March":3,"April":4,"May":5,"June":6,
                "July":7,"August":8,"September":9,"October":10,"November":11,"December":12}
        mon = full.get(m.group(1).title())
    if mon is None:
        return None
    return "%04d-%02d" % (int(m.group(2)), mon)


def _nsdl_fpi_top5_share(url, name_col_keyword):
    """Generic helper: fetch NSDL FPI AUC table, return (period, top5_share_pct)."""
    from io import StringIO
    html = _http_get_html(url, timeout=45)
    if not html:
        return (None, None)
    period = _nsdl_fpi_period_from_html(html)
    try:
        tables = pd.read_html(StringIO(html))
    except Exception as e:
        print("  ! pd.read_html failed: %s" % e)
        return (period, None)
    # The relevant table has multi-level header; locate by presence of 'Total' col + name keyword
    target = None
    for t in tables:
        cols_flat = " ".join([str(c) for c in t.columns.tolist()]).lower()
        if name_col_keyword.lower() in cols_flat and "total" in cols_flat:
            target = t
            break
    if target is None:
        print("  ! Could not locate NSDL FPI AUC table for %s" % name_col_keyword)
        return (period, None)
    # Flatten columns; find the rightmost Total column
    flat_cols = []
    for c in target.columns:
        if isinstance(c, tuple):
            flat_cols.append(" | ".join([str(x) for x in c if str(x) != 'nan']))
        else:
            flat_cols.append(str(c))
    target.columns = flat_cols
    total_cols = [c for c in flat_cols if c.strip().lower().endswith("total")]
    if not total_cols:
        print("  ! No Total column in NSDL FPI table")
        return (period, None)
    total_col = total_cols[-1]
    # Find name column (first text column containing the keyword)
    name_col = None
    for c in flat_cols:
        if name_col_keyword.lower() in c.lower():
            name_col = c
            break
    if name_col is None:
        name_col = flat_cols[1]  # fallback
    # Coerce Total to numeric; drop non-numeric/Total/Other rows
    df = target.copy()
    df[total_col] = pd.to_numeric(df[total_col], errors="coerce")
    df = df.dropna(subset=[total_col])
    df["_name_str"] = df[name_col].astype(str).str.strip()
    grand = df[df["_name_str"].str.lower() == "total"][total_col]
    if grand.empty:
        # Sometimes "Total" in Sr.No. col
        sr_col = flat_cols[0]
        df_grand = target[target[sr_col].astype(str).str.strip().str.lower() == "total"]
        if not df_grand.empty:
            grand_val = pd.to_numeric(df_grand[total_col], errors="coerce").iloc[0]
        else:
            print("  ! Grand Total row missing in NSDL FPI table")
            return (period, None)
    else:
        grand_val = float(grand.iloc[0])
    # Exclude Total / Others / blanks
    body = df[~df["_name_str"].str.lower().isin(["total", "others", "other", "nan", ""])]
    body = body.sort_values(by=total_col, ascending=False)
    top5_sum = float(body.head(5)[total_col].sum())
    if grand_val <= 0:
        return (period, None)
    return (period, round(top5_sum / grand_val * 100.0, 2))


def fetch_nsdl_fpi_country_top5():
    """Top-5 country-of-origin AUC share (%) from NSDL RepID=14."""
    period, val = _nsdl_fpi_top5_share(NSDL_FPI_COUNTRY_URL, "Country")
    if period is None or val is None:
        return {}
    return {"fpi_country_top5": (period, val)}


def fetch_nsdl_fpi_custodian_top5():
    """Top-5 client-category AUC share (%) from NSDL RepID=22.

    NSDL does not publish per-custodian AUC; this uses 'Type of Client'
    categories (FPIs, MFs, Insurance, FDI, etc.) as the closest available
    public proxy for the custodian-concentration indicator.
    """
    period, val = _nsdl_fpi_top5_share(NSDL_FPI_CLIENT_URL, "Type of Client")
    if period is None or val is None:
        return {}
    return {"fpi_custodian_top5": (period, val)}


BROWSER_FETCHERS = [
]


# ---- Direct fetchers registry --------------------------------------------

DIRECT_FETCHERS = [
    ("RBI WSS (forex + bank credit/deposit)", fetch_rbi_wss),
    ("AMFI Monthly (MF AUM + Folios)",        fetch_amfi),
    ("CEA Executive Summary (PDF)",           fetch_cea_executive_summary),
    ("PPAC Oil & Gas Snapshot (PDF)",         fetch_ppac_snapshot),
    ("NSDL FPI Monthly (Equity + Debt)",      fetch_nsdl_fpi_monthly),
    ("Ministry of Steel Monthly Report (PDF)", fetch_steel_monthly),
    ("Dept of Fertilizers Monthly Bulletin (PDF)", fetch_fertilizer_monthly),
    ("NSDL Demat Statistics (HTML)",           fetch_nsdl_demat),
    ("CDSL Periodic Stats (PDF)",              fetch_cdsl_demat),
    ("PPAC LPG Active Domestic Customers (XLSX)", fetch_ppac_lpg),
    ("PPAC CGD Network — PNG Domestic (XLSX)", fetch_ppac_png),
    ("OEA Core-8 Industries (XLSX)",           fetch_core8_cement),
    ("NSDL FPI AUC Country-wise Top-5 (HTML)", fetch_nsdl_fpi_country_top5),
    ("NSDL FPI AUC Client-Type Top-5 (HTML)",  fetch_nsdl_fpi_custodian_top5),
]


def _run_fetcher_group(fetchers, label_prefix):
    total_updates = 0
    for label, fn in fetchers:
        print("\n[%s]" % label)
        try:
            updates = fn()
        except Exception as e:
            print("  ! Fetcher failed: %s" % e)
            continue
        if not updates:
            print("  (no data returned)")
            continue
        for indicator_id, (period, value) in updates.items():
            if _store_indicator_value(indicator_id, period, value):
                print("  ✓ %-25s  %s = %s" % (indicator_id, period, value))
                total_updates += 1
            else:
                print("  ? %s (not in INDICATORS)" % indicator_id)
    print("\n%s: %d indicator values updated." % (label_prefix, total_updates))


def cmd_fetch_direct():
    """Run every direct fetcher; merge results into per-indicator CSVs."""
    _run_fetcher_group(DIRECT_FETCHERS, "Direct fetch")


def cmd_fetch_browser():
    """Run Playwright-based fetchers (requires playwright + chromium)."""
    _run_fetcher_group(BROWSER_FETCHERS, "Browser fetch")


def cmd_list():
    print("=" * 90)
    print("  INDIA MACRO — INDICATOR REGISTRY (%d indicators)" % len(INDICATORS))
    print("=" * 90)
    print("  OGD key: %s   |   OGD mappings: %d" %
          ("set" if OGD_KEY else "MISSING", len(OGD_RESOURCES)))
    last_cat = None
    for ind in INDICATORS:
        if ind["category"] != last_cat:
            print("\n[%s]" % ind["category"])
            last_cat = ind["category"]
        df = load_csv(ind)
        state = "✓ %3d rows" % len(df) if not df.empty else "  empty   "
        ogd = " [OGD]" if ind["id"] in OGD_RESOURCES else ""
        print("  %s  %-30s  %-50s  %s%s" %
              (state, ind["id"], ind["title"][:50],
               ind["source"][:30], ogd))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="store_true",
                    help="List every indicator and whether it has data.")
    ap.add_argument("--add", nargs=3,
                    metavar=("ID", "YYYY-MM", "VALUE"),
                    help="Append/replace one data point.")
    ap.add_argument("--print", dest="print_id",
                    help="Print one indicator's data table.")
    ap.add_argument("--ogd-test", dest="ogd_test_id",
                    help="Inspect an OGD dataset's fields by resource UUID.")
    ap.add_argument("--ogd-find", dest="ogd_find",
                    help="Resolve a data.gov.in URL/slug/search term to UUIDs.")
    ap.add_argument("--fetch", dest="fetch_id",
                    help="Fetch an indicator from data.gov.in OGD into its CSV.")
    ap.add_argument("--fetch-all", action="store_true",
                    help="Fetch every indicator that has an OGD mapping.")
    ap.add_argument("--fetch-direct", action="store_true",
                    help="Run direct (non-OGD) fetchers: RBI WSS, AMFI, etc.")
    ap.add_argument("--fetch-browser", action="store_true",
                    help="Run Playwright-based SPA fetchers (NSDL demat, ...).")
    args = ap.parse_args()

    if args.list:
        cmd_list()
        return

    if args.ogd_test_id:
        ogd_test(args.ogd_test_id)
        return

    if args.ogd_find:
        ogd_resolve(args.ogd_find)
        return

    if args.fetch_id:
        cmd_fetch(args.fetch_id)
        # Continue to rebuild outputs

    if args.fetch_all:
        cmd_fetch_all()
        cmd_fetch_direct()
        cmd_fetch_browser()
        # Continue to rebuild outputs

    if args.fetch_direct:
        cmd_fetch_direct()
        # Continue to rebuild outputs

    if args.fetch_browser:
        cmd_fetch_browser()
        # Continue to rebuild outputs

    if args.add:
        add_value(args.add[0], args.add[1], args.add[2])
        # Continue to rebuild outputs

    if args.print_id:
        ind = next((i for i in INDICATORS if i["id"] == args.print_id), None)
        if not ind:
            sys.exit("Unknown id: %s" % args.print_id)
        df = compute_growth(load_csv(ind), ind["metrics"])
        print(df.to_string(index=False))
        return

    # Ensure all CSVs exist (seed where available)
    for ind in INDICATORS:
        ensure_csv(ind)

    print("=" * 70)
    print("  INDIA MACRO DASHBOARD BUILD")
    print("=" * 70)

    build_dashboard()
    build_excel()

    print("\nDone.")


if __name__ == "__main__":
    main()
