"""
holdings_loader.py — unified parser for broker holdings exports
================================================================

SUMMARY
-------
Reads broker holdings .xlsx exports and returns a single normalised
DataFrame consumed by every other portfolio/ module (portfolio_tracker,
position_health, events_calendar). Supports two formats out of the box:

  1. Angel One holdings.xlsx   (3 sheets: Equity / Mutual Funds / Combined)
  2. Groww-style Stocks_Holdings_Statement.xlsx (single Sheet1, company-name
     based)

WORKFLOW
--------
1. Auto-discover holdings files in (a) PORTFOLIO_DIR (this folder),
   (b) PROJECT_ROOT, then (c) ~/Downloads.
   File names matched (case-insensitive):
       holdings.xlsx                       -> Angel format
       Stocks_Holdings_Statement.xlsx      -> Groww format
2. Parse each format's Equity sheet into rows with normalised columns.
3. For Groww rows (Stock Name only, no symbol), resolve ISIN -> NSE/SME
   symbol via the cached NSE EQUITY_L.csv + SME_EQUITY_L.csv masters.
4. Merge / dedupe (prefer Angel row when same ISIN appears in both,
   because Angel exposes Sector + Symbol directly).
5. Return a single DataFrame.

DATA SOURCES
------------
- User broker exports (local files):
    * Angel One:  holdings.xlsx
    * Groww:      Stocks_Holdings_Statement.xlsx
- ISIN -> Symbol resolution (cached, refreshed weekly):
    * NSE main board : https://archives.nseindia.com/content/equities/EQUITY_L.csv
    * NSE SME board  : https://archives.nseindia.com/content/equities/SME_EQUITY_L.csv

OUTPUT
------
DataFrame columns:
    Source        — 'Angel' | 'Groww'
    Symbol        — NSE trading symbol (e.g. 'BDL', 'AERON-SM') ; '' if unresolved
    Series        — 'EQ' | 'SM' | 'BE' | '' (NSE series)
    Company       — company name
    ISIN          — 12-char ISIN
    Sector        — sector tag (Angel only ; '' for Groww unless filled later)
    Quantity      — int shares
    AvgCost       — average buy price (Rs)
    LastClose     — broker-reported previous close (Rs)
    InvestedValue — Quantity * AvgCost
    PresentValue  — Quantity * LastClose
    PnL           — PresentValue - InvestedValue
    PnLPct        — PnL / InvestedValue * 100

DEPENDENCIES
------------
pandas, openpyxl, requests
"""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

# Locate project root (parent of portfolio/)
PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
CACHE_DIR = PROJECT_ROOT / ".cache" / "portfolio"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

NSE_EQ_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_SME_URL = "https://archives.nseindia.com/content/equities/SME_EQUITY_L.csv"
NSE_MASTER_TTL = 7 * 86400  # 7 days

DOWNLOAD_DIRS = [PORTFOLIO_DIR, PROJECT_ROOT, Path.home() / "Downloads"]
ANGEL_FILE = "holdings.xlsx"
GROWW_FILE = "Stocks_Holdings_Statement.xlsx"


# ─────────────────────────── NSE master cache ───────────────────────────────

def _fetch_nse_csv(url: str, cache_name: str) -> pd.DataFrame:
    """Download NSE EQUITY_L / SME_EQUITY_L csv with weekly cache."""
    cache_path = CACHE_DIR / cache_name
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < NSE_MASTER_TTL:
        return pd.read_csv(cache_path)
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        r.raise_for_status()
        cache_path.write_bytes(r.content)
        return pd.read_csv(io.BytesIO(r.content))
    except Exception as e:
        if cache_path.exists():
            print(f"  [holdings_loader] NSE fetch failed ({e}); using stale cache")
            return pd.read_csv(cache_path)
        print(f"  [holdings_loader] NSE fetch failed ({e}); ISIN resolution disabled")
        return pd.DataFrame()


_isin_map: Optional[dict] = None


def _isin_to_symbol_map() -> dict:
    """Build ISIN -> (symbol, series) map from NSE main + SME."""
    global _isin_map
    if _isin_map is not None:
        return _isin_map
    out: dict = {}
    for url, name, default_series in [
        (NSE_EQ_URL, "EQUITY_L.csv", "EQ"),
        (NSE_SME_URL, "SME_EQUITY_L.csv", "SM"),
    ]:
        df = _fetch_nse_csv(url, name)
        if df.empty:
            continue
        df.columns = [c.strip().upper() for c in df.columns]
        sym_col = next((c for c in df.columns if "SYMBOL" in c), None)
        isin_col = next((c for c in df.columns if "ISIN" in c), None)
        ser_col = next((c for c in df.columns if "SERIES" in c), None)
        if not (sym_col and isin_col):
            continue
        for _, row in df.iterrows():
            isin = str(row[isin_col]).strip().upper()
            sym = str(row[sym_col]).strip().upper()
            ser = (str(row[ser_col]).strip().upper()
                   if ser_col else default_series)
            if isin and sym and isin not in out:
                out[isin] = (sym, ser or default_series)
    _isin_map = out
    return out


# ─────────────────────────── file discovery ─────────────────────────────────

def _find_file(name: str) -> Optional[Path]:
    for base in DOWNLOAD_DIRS:
        p = base / name
        if p.exists():
            return p
    return None


# ─────────────────────────── parsers ────────────────────────────────────────

def _parse_angel(path: Path) -> pd.DataFrame:
    """Parse Angel One holdings.xlsx Equity sheet."""
    df = pd.read_excel(path, sheet_name="Equity")
    # Normalise column names (some exports prepend stray NaN col)
    df = df.dropna(axis=1, how="all")
    df.columns = [str(c).strip() for c in df.columns]

    rename = {
        "Symbol": "Symbol",
        "ISIN": "ISIN",
        "Sector": "Sector",
        "Quantity Available": "Quantity",
        "Average Price": "AvgCost",
        "Previous Closing Price": "LastClose",
        "Unrealized P&L": "PnL",
        "Unrealized P&L Pct.": "PnLPct",
    }
    keep = [c for c in rename if c in df.columns]
    df = df[keep].rename(columns=rename).copy()

    df["Source"] = "Angel"
    df["Company"] = ""  # Angel sheet doesn't expose long name; symbol used
    # Series: parse from -SM / -BE suffix in symbol if present
    df["Series"] = df["Symbol"].astype(str).str.extract(
        r"-([A-Z]+)$", expand=False).fillna("EQ")

    # Strip series suffix from symbol (so 'AERON-SM' -> 'AERON')
    df["Symbol"] = df["Symbol"].astype(str).str.replace(
        r"-[A-Z]+$", "", regex=True).str.upper()

    df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0).astype(int)
    df["AvgCost"] = pd.to_numeric(df["AvgCost"], errors="coerce").fillna(0.0)
    df["LastClose"] = pd.to_numeric(df["LastClose"], errors="coerce").fillna(0.0)
    df["InvestedValue"] = df["Quantity"] * df["AvgCost"]
    df["PresentValue"] = df["Quantity"] * df["LastClose"]
    df["PnL"] = df["PresentValue"] - df["InvestedValue"]
    df["PnLPct"] = df.apply(
        lambda r: (r["PnL"] / r["InvestedValue"] * 100) if r["InvestedValue"] else 0.0,
        axis=1)
    return df[df["Quantity"] > 0]


def _parse_groww(path: Path) -> pd.DataFrame:
    """Parse Groww Stocks_Holdings_Statement.xlsx (Sheet1)."""
    df = pd.read_excel(path, sheet_name="Sheet1")
    df = df.dropna(axis=1, how="all")
    df.columns = [str(c).strip() for c in df.columns]

    rename = {
        "Stock Name": "Company",
        "ISIN": "ISIN",
        "Quantity": "Quantity",
        "Average buy price": "AvgCost",
        "Closing price": "LastClose",
        "Buy value": "InvestedValue",
        "Closing value": "PresentValue",
        "Unrealised P&L": "PnL",
    }
    keep = [c for c in rename if c in df.columns]
    df = df[keep].rename(columns=rename).copy()

    df["Source"] = "Groww"
    df["Sector"] = ""
    df["Quantity"] = pd.to_numeric(df["Quantity"], errors="coerce").fillna(0).astype(int)
    df["AvgCost"] = pd.to_numeric(df["AvgCost"], errors="coerce").fillna(0.0)
    df["LastClose"] = pd.to_numeric(df["LastClose"], errors="coerce").fillna(0.0)
    df["InvestedValue"] = pd.to_numeric(df["InvestedValue"], errors="coerce").fillna(0.0)
    df["PresentValue"] = pd.to_numeric(df["PresentValue"], errors="coerce").fillna(0.0)
    df["PnL"] = pd.to_numeric(df["PnL"], errors="coerce").fillna(0.0)
    df["PnLPct"] = df.apply(
        lambda r: (r["PnL"] / r["InvestedValue"] * 100) if r["InvestedValue"] else 0.0,
        axis=1)

    # Resolve ISIN -> Symbol
    isin_map = _isin_to_symbol_map()
    df["Symbol"] = df["ISIN"].astype(str).str.strip().str.upper().map(
        lambda i: isin_map.get(i, ("", ""))[0])
    df["Series"] = df["ISIN"].astype(str).str.strip().str.upper().map(
        lambda i: isin_map.get(i, ("", ""))[1])

    return df[df["Quantity"] > 0]


# ─────────────────────────── public API ─────────────────────────────────────

COLUMNS = ["Source", "Symbol", "Series", "Company", "ISIN", "Sector",
           "Quantity", "AvgCost", "LastClose",
           "InvestedValue", "PresentValue", "PnL", "PnLPct"]


def load_holdings(angel_path: Optional[str] = None,
                  groww_path: Optional[str] = None,
                  verbose: bool = True) -> pd.DataFrame:
    """Load and merge holdings from both broker xlsx exports.

    Args:
      angel_path : explicit path to Angel holdings.xlsx (else auto-discover).
      groww_path : explicit path to Groww file (else auto-discover).
      verbose    : print discovery / row counts.

    Returns:
      Unified DataFrame with COLUMNS schema. Empty if nothing found.
    """
    frames = []

    a_path = Path(angel_path) if angel_path else _find_file(ANGEL_FILE)
    g_path = Path(groww_path) if groww_path else _find_file(GROWW_FILE)

    if a_path and a_path.exists():
        if verbose:
            print(f"  [holdings_loader] Angel: {a_path}")
        try:
            frames.append(_parse_angel(a_path))
        except Exception as e:
            print(f"  [holdings_loader] Angel parse FAILED: {e}")

    if g_path and g_path.exists():
        if verbose:
            print(f"  [holdings_loader] Groww: {g_path}")
        try:
            frames.append(_parse_groww(g_path))
        except Exception as e:
            print(f"  [holdings_loader] Groww parse FAILED: {e}")

    if not frames:
        if verbose:
            print("  [holdings_loader] No holdings files found.")
        return pd.DataFrame(columns=COLUMNS)

    out = pd.concat(frames, ignore_index=True)

    # Ensure all expected columns exist
    for c in COLUMNS:
        if c not in out.columns:
            out[c] = "" if c in ("Source", "Symbol", "Series", "Company",
                                 "ISIN", "Sector") else 0.0
    out = out[COLUMNS]

    # Dedupe by ISIN: prefer Angel (richer data)
    if "ISIN" in out.columns and out["ISIN"].notna().any():
        out["_pri"] = out["Source"].map({"Angel": 0, "Groww": 1}).fillna(2)
        out = (out.sort_values("_pri")
                  .drop_duplicates(subset=["ISIN"], keep="first")
                  .drop(columns="_pri"))

    out = out.sort_values("PresentValue", ascending=False).reset_index(drop=True)
    if verbose:
        print(f"  [holdings_loader] {len(out)} unique positions loaded")
    return out


if __name__ == "__main__":
    df = load_holdings()
    print(df.head(20).to_string())
    print(f"\nTotal positions: {len(df)}")
    print(f"Invested: ₹{df['InvestedValue'].sum():,.0f}")
    print(f"Present:  ₹{df['PresentValue'].sum():,.0f}")
    print(f"P&L:      ₹{df['PnL'].sum():,.0f}")
