"""Flask app for Screener.in-backed valuation and financial charts."""

from __future__ import annotations

import calendar
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yfinance as yf
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC_DIR = HERE / "static"
TRADINGCHARTS_STATIC_DIR = ROOT / "tradingcharts" / "static"
STATE_DIR = HERE / "state"
STATE_FILE = STATE_DIR / "state.json"
PORT = 5052

SCREENER_BASE = "https://www.screener.in"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

SERIES_METRICS = {
    "Sales": {"unit": "Rs Cr", "kind": "annual", "freq": "Q/A"},
    "Expenses": {"unit": "Rs Cr", "kind": "annual", "freq": "Q/A"},
    "Operating Profit": {"unit": "Rs Cr", "kind": "annual", "freq": "Q/A"},
    "Net Profit": {"unit": "Rs Cr", "kind": "annual", "freq": "Q/A"},
    "EPS": {"unit": "Rs", "kind": "annual", "freq": "Q/A"},
    "PE": {"unit": "x", "kind": "annual", "freq": "A"},
    "PB": {"unit": "x", "kind": "annual", "freq": "A"},
    "Reserves": {"unit": "Rs Cr", "kind": "annual", "freq": "A"},
    "Borrowings": {"unit": "Rs Cr", "kind": "annual", "freq": "A"},
    "Total liabilities": {"unit": "Rs Cr", "kind": "annual", "freq": "A"},
    "Total assets": {"unit": "Rs Cr", "kind": "annual", "freq": "A"},
    "Free cash flow": {"unit": "Rs Cr", "kind": "annual", "freq": "A"},
    "CFO/OP": {"unit": "%", "kind": "annual", "freq": "A"},
    "Fixed Assets": {"unit": "Rs Cr", "kind": "annual", "freq": "A"},
    "CFO": {"unit": "Rs Cr", "kind": "annual", "freq": "A"},
    "Net Cash Flow": {"unit": "Rs Cr", "kind": "annual", "freq": "A"},
    "Cash Conversion Cycle": {"unit": "Days", "kind": "annual", "freq": "A"},
    "Working Capital Days": {"unit": "Days", "kind": "annual", "freq": "A"},
    "Stock Price": {"unit": "Rs", "kind": "annual", "freq": "A"},
    "Market Cap": {"unit": "Rs Cr", "kind": "annual", "freq": "A"},
    "Net Profit Margin": {"unit": "%", "kind": "annual", "freq": "A"},
    "OPM %": {"unit": "%", "kind": "annual", "freq": "A"},
    "ROCE %": {"unit": "%", "kind": "annual", "freq": "A"},
    "Debtor Days": {"unit": "Days", "kind": "annual", "freq": "A"},
    "Inventory Days": {"unit": "Days", "kind": "annual", "freq": "A"},
    "ROE %": {"unit": "%", "kind": "annual", "freq": "A"},
    "Debt/Equity": {"unit": "x", "kind": "annual", "freq": "A"},
}

ALL_METRICS = [
    "Sales",
    "Expenses",
    "Operating Profit",
    "Net Profit",
    "EPS",
    "PE",
    "PB",
    "Reserves",
    "Borrowings",
    "Total liabilities",
    "Total assets",
    "Free cash flow",
    "CFO/OP",
    "Fixed Assets",
    "CFO",
    "Net Cash Flow",
    "Cash Conversion Cycle",
    "Working Capital Days",
    "Stock Price",
    "Market Cap",
    "Net Profit Margin",
    "OPM %",
    "ROCE %",
    "Debtor Days",
    "Inventory Days",
    "ROE %",
    "Debt/Equity",
]

# Source-specific row-name aliases per metric. Screener and Yahoo use different
# terminology, and that terminology further varies by sector (banks/NBFCs vs
# manufacturers). The first matching alias (in order) wins.
#
# SCREENER aliases are matched against normalized row labels (trailing "+"
# stripped, whitespace collapsed) from the profit-loss / balance-sheet /
# cash-flow / ratios tables.
SCREENER_ALIASES: dict[str, list[str]] = {
    "Sales": ["Sales", "Revenue", "Total Revenue", "Sales/Turnover"],
    "Expenses": ["Expenses", "Total Expenses"],
    "Operating Profit": ["Operating Profit", "Financing Profit", "EBIT"],
    "Net Profit": ["Net Profit", "Profit After Tax", "PAT"],
    "EPS": ["EPS in Rs", "EPS", "Adjusted EPS in Rs"],
    "PE": ["Price to Earning", "Price to Earnings", "PE"],
    "PB": ["Price to Book value", "Price to Book", "PB"],
    "Reserves": ["Reserves", "Reserves and Surplus"],
    "Borrowings": ["Borrowings", "Borrowing", "Deposits"],
    "Total liabilities": ["Total Liabilities"],
    "Total assets": ["Total Assets"],
    "Free cash flow": ["Free Cash Flow"],
    "CFO/OP": ["CFO/OP"],
    "Fixed Assets": ["Fixed Assets"],
    "CFO": ["Cash from Operating Activity"],
    "Net Cash Flow": ["Net Cash Flow"],
    "Cash Conversion Cycle": ["Cash Conversion Cycle"],
    "Working Capital Days": ["Working Capital Days"],
    "OPM %": ["OPM %", "Financing Margin %"],
    "ROCE %": ["ROCE %"],
    "Debtor Days": ["Debtor Days"],
    "Inventory Days": ["Inventory Days"],
    "ROE %": ["ROE %"],
}

# YAHOO aliases are matched against the row index of t.financials /
# t.balance_sheet / t.cashflow (and their quarterly variants).
YAHOO_ALIASES: dict[str, list[str]] = {
    "Sales": ["Total Revenue", "Operating Revenue"],
    "Expenses": ["Total Expenses", "Operating Expense"],
    # Banks/NBFCs have no "Operating Income" on Yahoo -> fall back to Net
    # Interest Income, then Pretax Income as a proxy for operating profit.
    "Operating Profit": ["Operating Income", "EBIT", "Net Interest Income", "Pretax Income"],
    "Net Profit": ["Net Income", "Net Income Common Stockholders"],
    "EPS": ["Diluted EPS", "Basic EPS"],
    "Reserves": ["Retained Earnings", "Stockholders Equity", "Common Stock Equity"],
    "Borrowings": ["Total Debt", "Long Term Debt", "Current Debt"],
    "Total liabilities": ["Total Liabilities Net Minority Interest", "Total Liabilities"],
    "Total assets": ["Total Assets"],
    "Free cash flow": ["Free Cash Flow"],
}

_PAGE_CACHE: dict[str, tuple[float, str]] = {}
PAGE_TTL_SECONDS = 10 * 60
_SEARCH_CACHE: dict[str, tuple[float, list[CompanyEntry]]] = {}
_ENTRY_CACHE: dict[str, tuple[float, CompanyEntry]] = {}
SEARCH_TTL_SECONDS = 15 * 60
_STOCK_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
STOCK_TTL_SECONDS = 30 * 60

load_dotenv(ROOT / ".env")
SCREENER_USER = os.getenv("SCREENER_USER", "").strip().strip("'").strip('"')
SCREENER_PASS = os.getenv("SCREENER_PASS", "").strip().strip("'").strip('"')

_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)
_SESSION_LOCK = threading.Lock()

_LOGIN_STATE = {"attempted": False, "ok": False}

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


@dataclass(frozen=True)
class CompanyEntry:
    ticker: str
    name: str
    url: str


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--"}:
        return None
    text = text.replace(",", "").replace("₹", "").replace("Cr.", "").replace("Cr", "")
    text = text.replace("%", "").replace("x", "")
    neg = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        num = float(text)
    except Exception:
        return None
    if neg:
        num = -num
    return num


def _safe_ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    return a / b


def _period_to_date(label: str) -> str:
    parts = label.split()
    if len(parts) < 2:
        return ""
    month = MONTHS.get(parts[0][:3].title())
    year = _to_float(parts[1])
    if month is None or year is None:
        return ""
    y = int(year)
    day = calendar.monthrange(y, month)[1]
    return f"{y:04d}-{month:02d}-{day:02d}"


def _normalize_row_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.replace("+", "")).strip()


def _ensure_login() -> None:
    if _LOGIN_STATE["attempted"]:
        return
    _LOGIN_STATE["attempted"] = True
    if not (SCREENER_USER and SCREENER_PASS):
        return

    try:
        login_url = f"{SCREENER_BASE}/login/"
        page = _request_with_retry("GET", login_url, timeout=6)
        page.raise_for_status()
        soup = BeautifulSoup(page.text, "html.parser")
        token_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
        csrf_token = token_input.get("value") if token_input else _SESSION.cookies.get("csrftoken", "")
        data = {
            "username": SCREENER_USER,
            "password": SCREENER_PASS,
            "next": "/",
            "csrfmiddlewaretoken": csrf_token,
        }
        headers = {"Referer": login_url}
        resp = _request_with_retry(
            "POST",
            login_url,
            data=data,
            headers=headers,
            timeout=6,
            allow_redirects=True,
        )
        resp.raise_for_status()
        _LOGIN_STATE["ok"] = ("logout" in resp.text.lower()) or ("/user/" in resp.text)
    except Exception:
        _LOGIN_STATE["ok"] = False


def _request_with_retry(method: str, url: str, retries: int = 2, backoff: float = 0.2, **kwargs: Any) -> requests.Response:
    kwargs.setdefault("timeout", 10)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with _SESSION_LOCK:
                return _SESSION.request(method, url, **kwargs)
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError("Request failed")


def _fetch_company_html(url: str) -> str:
    _ensure_login()
    abs_url = url if url.startswith("http") else f"{SCREENER_BASE}{url}"
    cached = _PAGE_CACHE.get(abs_url)
    if cached and time.time() - cached[0] < PAGE_TTL_SECONDS:
        return cached[1]
    try:
        resp = _request_with_retry("GET", abs_url, timeout=8)
        resp.raise_for_status()
        _PAGE_CACHE[abs_url] = (time.time(), resp.text)
        return resp.text
    except Exception:
        # Under burst traffic, use stale cached page instead of failing the whole API call.
        if cached:
            return cached[1]
        raise


def _parse_top_ratios(soup: BeautifulSoup) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for li in soup.select("#top-ratios li"):
        name_node = li.select_one(".name")
        val_node = li.select_one(".value")
        if not name_node or not val_node:
            continue
        name = name_node.get_text(" ", strip=True)
        out[name] = _to_float(val_node.get_text(" ", strip=True))
    return out


def _parse_table_section(soup: BeautifulSoup, section_id: str) -> tuple[list[str], dict[str, list[float | None]]]:
    section = soup.find("section", id=section_id)
    if not section:
        return ([], {})
    table = section.find("table")
    if not table:
        return ([], {})

    headers = [th.get_text(" ", strip=True) for th in table.select("thead th")]
    period_labels = headers[1:]

    rows: dict[str, list[float | None]] = {}
    for tr in table.select("tbody tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        row_name = _normalize_row_label(cells[0].get_text(" ", strip=True))
        values = [_to_float(c.get_text(" ", strip=True)) for c in cells[1 : 1 + len(period_labels)]]
        rows[row_name] = values
    return (period_labels, rows)


def _series_points(periods: list[str], values: list[float | None]) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for label, value in zip(periods, values):
        if value is None:
            continue
        points.append({"label": label, "date": _period_to_date(label), "value": value})
    return points


def _merge_annual_ttm(
    a_periods: list[str],
    a_values: list[float | None],
    q_periods: list[str],
    q_values: list[float | None],
) -> list[dict[str, Any]]:
    """Merge long annual history with recent quarterly TTM for a flow metric.

    Screener exposes ~12 years of annual P&L but only ~13 quarters. To keep the
    full history while still showing recent quarterly cadence, older periods use
    the annual value and recent periods use a trailing-twelve-month (rolling
    4-quarter sum) which is on the same scale as the annual figure (so the line
    stays continuous instead of dropping ~4x at the annual->quarterly seam).
    """
    annual = _series_points(a_periods, a_values)
    ttm: list[dict[str, Any]] = []
    if q_periods and q_values:
        for i in range(3, len(q_values)):
            window = q_values[i - 3 : i + 1]
            if any(v is None for v in window):
                continue
            try:
                total = round(float(sum(window)), 2)
            except Exception:
                continue
            ttm.append({"label": q_periods[i], "date": _period_to_date(q_periods[i]), "value": total})
    if not ttm:
        return annual
    first_ttm_date = ttm[0]["date"]
    merged = [p for p in annual if p["date"] and p["date"] < first_ttm_date]
    merged.extend(ttm)
    merged.sort(key=lambda p: p["date"])
    return merged


def _latest(points: list[dict[str, Any]]) -> float | None:
    if not points:
        return None
    return _to_float(points[-1].get("value"))


def _is_rupee_metric(label: str) -> bool:
    return label in {
        "Sales",
        "Expenses",
        "Operating Profit",
        "Net Profit",
        "Reserves",
        "Borrowings",
        "Total liabilities",
        "Total assets",
        "Free cash flow",
    }


def _compute_pe_pb_yahoo(
    financials: Any,
    balance_sheet: Any,
    history: Any,
) -> tuple[list[dict], list[dict]]:
    """Compute annual PE and PB series from Yahoo annual financials + price history."""
    pe_points: list[dict] = []
    pb_points: list[dict] = []
    try:
        eps_series = _pick_row(financials, ["Diluted EPS", "Basic EPS"])
        equity_series = _pick_row(balance_sheet, ["Stockholders Equity", "Common Stock Equity"])
        shares_series = _pick_row(financials, ["Basic Average Shares", "Diluted Average Shares"])

        if history is None or history.empty:
            return pe_points, pb_points

        # Build a dict of date→close for fast lookup
        price_index = {str(dt.date()): float(close) for dt, close in zip(history.index, history["Close"])}
        sorted_dates = sorted(price_index.keys())

        def _price_at(target_date_str: str) -> float | None:
            """Return closing price on or before target_date (search up to 7 trading days back)."""
            from datetime import datetime, timedelta
            try:
                tgt = datetime.strptime(target_date_str, "%Y-%m-%d")
            except Exception:
                return None
            for offset in range(8):
                candidate = (tgt - timedelta(days=offset)).strftime("%Y-%m-%d")
                if candidate in price_index:
                    return price_index[candidate]
            return None

        # Annual PE
        if eps_series is not None:
            for dt, raw_eps in sorted(eps_series.items(), key=lambda x: str(x[0])):
                try:
                    eps_val = float(raw_eps)
                    if eps_val <= 0 or eps_val != eps_val:
                        continue
                    date_str = dt.strftime("%Y-%m-%d")
                    price = _price_at(date_str)
                    if price is None or price <= 0:
                        continue
                    pe = round(price / eps_val, 2)
                    if pe <= 0 or pe > 500:  # sanity: skip outliers
                        continue
                    pe_points.append({"date": date_str, "label": dt.strftime("%b %Y"), "value": pe})
                except Exception:
                    continue

        # Annual PB
        if equity_series is not None and shares_series is not None:
            for dt, raw_eq in sorted(equity_series.items(), key=lambda x: str(x[0])):
                try:
                    equity = float(raw_eq)
                    if equity <= 0 or equity != equity:
                        continue
                    shares_raw = shares_series.get(dt)
                    if shares_raw is None:
                        continue
                    shares = float(shares_raw)
                    if shares <= 0 or shares != shares:
                        continue
                    bvps = equity / shares
                    if bvps <= 0:
                        continue
                    date_str = dt.strftime("%Y-%m-%d")
                    price = _price_at(date_str)
                    if price is None or price <= 0:
                        continue
                    pb = round(price / bvps, 2)
                    if pb <= 0 or pb > 200:  # sanity
                        continue
                    pb_points.append({"date": date_str, "label": dt.strftime("%b %Y"), "value": pb})
                except Exception:
                    continue

    except Exception:
        pass
    return pe_points, pb_points


def _fetch_yahoo_pe_pb(symbol: str) -> tuple[list[dict], list[dict]]:
    """Fetch Yahoo data for ``symbol`` and derive annual PE/PB series.

    Used to enrich Screener-resolved payloads, which lack a per-year PE/PB
    series (Screener exposes only scalar Stock P/E and Book Value).
    """
    if yf is None:
        return [], []
    candidates = [f"{symbol.upper()}.NS", f"{symbol.upper()}.BO", symbol.upper()]
    for tk in candidates:
        try:
            t = yf.Ticker(tk)
            financials = t.financials
            balance_sheet = t.balance_sheet
            if financials is None or financials.empty:
                continue
            history = t.history(period="10y", interval="1d")
            pe_points, pb_points = _compute_pe_pb_yahoo(financials, balance_sheet, history)
            if pe_points or pb_points:
                return pe_points, pb_points
        except Exception:
            continue
    return [], []


def _pe_from_eps_and_prices(symbol: str, eps_points: list[dict]) -> list[dict]:
    """Compute a full-history PE series from Screener EPS points x Yahoo prices.

    Screener provides EPS back to ~2015 (and TTM for recent quarters), while the
    Yahoo-derived PE only reaches ~4 years (Yahoo financials EPS depth). Pairing
    Screener EPS with Yahoo's full daily price history yields PE for the entire
    available history. EPS points already use annual / trailing-12-month values,
    so price / EPS is a valid trailing PE at each date.
    """
    if yf is None or not eps_points:
        return []
    from datetime import datetime, timedelta

    candidates = [f"{symbol.upper()}.NS", f"{symbol.upper()}.BO", symbol.upper()]
    for tk in candidates:
        try:
            hist = yf.Ticker(tk).history(period="max", interval="1d")
            if hist is None or hist.empty:
                continue
            price_index = {str(d.date()): float(c) for d, c in zip(hist.index, hist["Close"])}

            def _price_at(ds: str) -> float | None:
                try:
                    tgt = datetime.strptime(ds, "%Y-%m-%d")
                except Exception:
                    return None
                for off in range(8):
                    cand = (tgt - timedelta(days=off)).strftime("%Y-%m-%d")
                    if cand in price_index:
                        return price_index[cand]
                return None

            out: list[dict] = []
            for pt in eps_points:
                eps = pt.get("value")
                ds = pt.get("date")
                if not eps or eps <= 0 or not ds:
                    continue
                price = _price_at(ds)
                if price is None or price <= 0:
                    continue
                pe = round(price / eps, 2)
                if pe <= 0 or pe > 500:
                    continue
                out.append({"label": pt.get("label", ds), "date": ds, "value": pe})
            if out:
                return out
        except Exception:
            continue
    return []


def _pb_from_screener_and_prices(
    symbol: str,
    b_periods: list[str],
    b_rows: dict[str, list],
    book_value_current: float | None,
) -> list[dict]:
    """Compute a full-history PB series from Screener net worth x Yahoo prices.

    Screener provides ~12y of net worth (Equity Capital + Reserves) but only a
    scalar current Book Value (BVPS). We back out the face value from the latest
    net worth and current BVPS, then derive per-year BVPS as
    ``face_value * net_worth_year / equity_capital_year`` (share count tracks
    Equity Capital, so this absorbs issuances/buybacks). PB = price / BVPS.
    """
    if yf is None or not book_value_current or book_value_current <= 0:
        return []
    equity = _pick_screener_row(b_rows, ["Equity Capital"])
    reserves = _pick_screener_row(b_rows, ["Reserves"])
    if not equity or not reserves or not b_periods:
        return []
    n = min(len(b_periods), len(equity), len(reserves))
    net_worth = []
    for i in range(n):
        e, r = equity[i], reserves[i]
        net_worth.append((e + r) if (e is not None and r is not None) else None)
    # Latest period with usable net worth / equity to anchor face value.
    latest = None
    for i in range(n - 1, -1, -1):
        if net_worth[i] and equity[i] and equity[i] > 0 and net_worth[i] > 0:
            latest = i
            break
    if latest is None:
        return []
    face_value = book_value_current * equity[latest] / net_worth[latest]
    if face_value <= 0:
        return []

    from datetime import datetime, timedelta

    candidates = [f"{symbol.upper()}.NS", f"{symbol.upper()}.BO", symbol.upper()]
    for tk in candidates:
        try:
            hist = yf.Ticker(tk).history(period="max", interval="1d")
            if hist is None or hist.empty:
                continue
            price_index = {str(d.date()): float(c) for d, c in zip(hist.index, hist["Close"])}

            def _price_at(ds: str) -> float | None:
                try:
                    tgt = datetime.strptime(ds, "%Y-%m-%d")
                except Exception:
                    return None
                for off in range(8):
                    cand = (tgt - timedelta(days=off)).strftime("%Y-%m-%d")
                    if cand in price_index:
                        return price_index[cand]
                return None

            out: list[dict] = []
            for i in range(n):
                if not net_worth[i] or not equity[i] or equity[i] <= 0:
                    continue
                ds = _period_to_date(b_periods[i])
                if not ds:
                    continue
                bvps = face_value * net_worth[i] / equity[i]
                if bvps <= 0:
                    continue
                price = _price_at(ds)
                if price is None or price <= 0:
                    continue
                pb = round(price / bvps, 2)
                if pb <= 0 or pb > 200:
                    continue
                out.append({"label": b_periods[i], "date": ds, "value": pb})
            if out:
                return out
        except Exception:
            continue
    return []


def _derive_ratio(
    num_periods: list[str],
    num_values: list[float | None],
    den_periods: list[str],
    den_values: list[float | None],
    scale: float = 1.0,
) -> list[dict[str, Any]]:
    """Derive a per-period ratio (num / den * scale), aligned by period label."""
    den_map = {p: v for p, v in zip(den_periods, den_values)}
    out: list[dict[str, Any]] = []
    for p, nv in zip(num_periods, num_values):
        dv = den_map.get(p)
        if nv is None or dv in (None, 0):
            continue
        out.append({"label": p, "date": _period_to_date(p), "value": round(nv / dv * scale, 2)})
    return out


def _screener_networth(
    b_periods: list[str], b_rows: dict[str, list]
) -> tuple[list[str], list[float | None]]:
    """Net worth (Equity Capital + Reserves) per balance-sheet period."""
    equity = _pick_screener_row(b_rows, ["Equity Capital"])
    reserves = _pick_screener_row(b_rows, ["Reserves"])
    if not equity or not reserves or not b_periods:
        return [], []
    n = min(len(b_periods), len(equity), len(reserves))
    vals: list[float | None] = []
    for i in range(n):
        e, r = equity[i], reserves[i]
        vals.append((e + r) if (e is not None and r is not None) else None)
    return b_periods[:n], vals


def _shares_annual(
    b_periods: list[str], b_rows: dict[str, list], book_value_current: float | None
) -> list[tuple[str, float]]:
    """Per-year shares outstanding (crore), derived from Screener equity capital.

    shares = equity_capital / face_value, where face_value is inferred from the
    latest net worth and current Book Value (BVPS). Tracks dilution via the
    annual Equity Capital row. Returned ascending by period date.
    """
    if not book_value_current or book_value_current <= 0:
        return []
    equity = _pick_screener_row(b_rows, ["Equity Capital"])
    nw_periods, nw_vals = _screener_networth(b_periods, b_rows)
    if not equity or not nw_vals:
        return []
    n = min(len(nw_periods), len(equity), len(nw_vals))
    latest = None
    for i in range(n - 1, -1, -1):
        if nw_vals[i] and equity[i] and equity[i] > 0 and nw_vals[i] > 0:
            latest = i
            break
    if latest is None:
        return []
    face_value = book_value_current * equity[latest] / nw_vals[latest]
    if face_value <= 0:
        return []
    out: list[tuple[str, float]] = []
    for i in range(n):
        if equity[i] and equity[i] > 0:
            ds = _period_to_date(nw_periods[i])
            if ds:
                out.append((ds, equity[i] / face_value))
    out.sort(key=lambda x: x[0])
    return out


def _price_and_mcap_series(
    symbol: str, start_date: str | None, shares_annual: list[tuple[str, float]]
) -> tuple[list[dict], list[dict]]:
    """Monthly Stock Price and Market Cap (Rs Cr) from Yahoo.

    Market Cap = monthly close x shares (most recent annual share count on or
    before the month). Both series are clipped to ``start_date``'s month to
    align with the fundamental history window.
    """
    if yf is None:
        return [], []
    cutoff = start_date[:7] if start_date else None
    candidates = [f"{symbol.upper()}.NS", f"{symbol.upper()}.BO", symbol.upper()]
    for tk in candidates:
        try:
            hist = yf.Ticker(tk).history(period="max", interval="1mo")
            if hist is None or hist.empty:
                continue
            price_pts: list[dict] = []
            mcap_pts: list[dict] = []
            for d, c in zip(hist.index, hist["Close"]):
                if c is None:
                    continue
                close = float(c)
                if close <= 0:
                    continue
                ds = str(d.date())
                if cutoff and ds[:7] < cutoff:
                    continue
                lbl = d.strftime("%b %Y")
                price_pts.append({"label": lbl, "date": ds, "value": round(close, 2)})
                shares = None
                for sd, sv in shares_annual:
                    if sd <= ds:
                        shares = sv
                    else:
                        break
                if shares:
                    mcap_pts.append({"label": lbl, "date": ds, "value": round(close * shares, 0)})
            if price_pts:
                return price_pts, mcap_pts
        except Exception:
            continue
    return [], []


def _to_crore(value: float | None, label: str) -> float | None:
    if value is None:
        return None
    if _is_rupee_metric(label):
        return value / 10_000_000.0
    return value


def _pick_row(frame: Any, candidates: list[str]) -> Any:
    if frame is None or getattr(frame, "empty", True):
        return None
    idx = [str(x) for x in frame.index]
    lower_to_real = {name.lower(): name for name in idx}
    for cand in candidates:
        real = lower_to_real.get(cand.lower())
        if real is not None:
            return frame.loc[real]
    # Fuzzy fallback
    for cand in candidates:
        c = cand.lower()
        for real in idx:
            if c in real.lower() or real.lower() in c:
                return frame.loc[real]
    return None


def _points_from_pd_series(series: Any, label: str) -> list[dict[str, Any]]:
    if series is None:
        return []
    points: list[dict[str, Any]] = []
    items = []
    try:
        items = list(series.items())
    except Exception:
        return []
    items.sort(key=lambda it: str(it[0]))
    for dt, raw in items:
        try:
            if raw is None:
                continue
            value = float(raw)
        except Exception:
            continue
        if value != value:
            continue
        date_str = ""
        label_str = ""
        try:
            date_str = dt.strftime("%Y-%m-%d")
            label_str = dt.strftime("%b %Y")
        except Exception:
            date_str = str(dt)[:10]
            label_str = str(dt)[:10]
        scaled = _to_crore(value, label)
        if scaled is None:
            continue
        points.append({"date": date_str, "label": label_str, "value": round(float(scaled), 4)})
    return points


def _build_yahoo_payload(symbol: str, selected: list[str]) -> dict[str, Any] | None:
    ticker_candidates = [symbol.upper()]
    if "." not in symbol:
        ticker_candidates = [f"{symbol.upper()}.NS", f"{symbol.upper()}.BO", symbol.upper()]

    last_error: Exception | None = None
    for tk in ticker_candidates:
        try:
            t = yf.Ticker(tk)
            financials = t.financials
            balance_sheet = t.balance_sheet
            cashflow = t.cashflow

            # Build base rows from Yahoo statements using sector-aware aliases.
            sales = _pick_row(financials, YAHOO_ALIASES["Sales"])
            op_profit = _pick_row(financials, YAHOO_ALIASES["Operating Profit"])
            net_profit = _pick_row(financials, YAHOO_ALIASES["Net Profit"])
            expenses = _pick_row(financials, YAHOO_ALIASES["Expenses"])
            eps = _pick_row(financials, YAHOO_ALIASES["EPS"])
            reserves = _pick_row(balance_sheet, YAHOO_ALIASES["Reserves"])
            borrowings = _pick_row(balance_sheet, YAHOO_ALIASES["Borrowings"])
            total_liabilities = _pick_row(balance_sheet, YAHOO_ALIASES["Total liabilities"])
            total_assets = _pick_row(balance_sheet, YAHOO_ALIASES["Total assets"])
            free_cash_flow = _pick_row(cashflow, YAHOO_ALIASES["Free cash flow"])
            op_cash_flow = _pick_row(cashflow, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"])

            # Derive expenses if unavailable.
            if expenses is None and sales is not None and op_profit is not None:
                try:
                    expenses = sales - op_profit
                except Exception:
                    expenses = None

            # Use quarterly financials for P&L metrics if any are requested
            # (individual-quarter data, denser than annual but same unit scale)
            need_pl = any(label in selected for label in _PL_METRICS)
            q_sales, q_expenses, q_op_profit, q_net_profit, q_eps = sales, expenses, op_profit, net_profit, eps
            if need_pl:
                try:
                    q_fin = t.quarterly_financials
                    if q_fin is not None and not q_fin.empty:
                        _qs = _pick_row(q_fin, YAHOO_ALIASES["Sales"])
                        _qop = _pick_row(q_fin, YAHOO_ALIASES["Operating Profit"])
                        _qnp = _pick_row(q_fin, YAHOO_ALIASES["Net Profit"])
                        _qex = _pick_row(q_fin, YAHOO_ALIASES["Expenses"])
                        _qeps = _pick_row(q_fin, YAHOO_ALIASES["EPS"])
                        if _qs is not None:
                            q_sales = _qs
                        if _qop is not None:
                            q_op_profit = _qop
                        if _qnp is not None:
                            q_net_profit = _qnp
                        if _qeps is not None:
                            q_eps = _qeps
                        if _qex is not None:
                            q_expenses = _qex
                        elif _qs is not None and _qop is not None:
                            try:
                                q_expenses = _qs - _qop
                            except Exception:
                                pass
                except Exception:
                    pass

            cfo_op_points: list[dict[str, Any]] = []
            if op_cash_flow is not None and op_profit is not None:
                try:
                    cfo_items = list(op_cash_flow.items())
                    op_items = dict(op_profit.items())
                    for dt, cfo_raw in sorted(cfo_items, key=lambda it: str(it[0])):
                        op_raw = op_items.get(dt)
                        try:
                            cfo_val = float(cfo_raw)
                            op_val = float(op_raw)
                            if op_val == 0:
                                continue
                            ratio = (cfo_val / op_val) * 100.0
                            cfo_op_points.append(
                                {
                                    "date": dt.strftime("%Y-%m-%d"),
                                    "label": dt.strftime("%b %Y"),
                                    "value": round(ratio, 4),
                                }
                            )
                        except Exception:
                            continue
                except Exception:
                    cfo_op_points = []

            # Compute PE / PB if requested
            need_pe_pb = "PE" in selected or "PB" in selected
            pe_points: list[dict] = []
            pb_points: list[dict] = []
            if need_pe_pb:
                history = t.history(period="10y")
                pe_points, pb_points = _compute_pe_pb_yahoo(financials, balance_sheet, history)

            series_map = {
                "Sales": _points_from_pd_series(q_sales, "Sales"),
                "Expenses": _points_from_pd_series(q_expenses, "Expenses"),
                "Operating Profit": _points_from_pd_series(q_op_profit, "Operating Profit"),
                "Net Profit": _points_from_pd_series(q_net_profit, "Net Profit"),
                "EPS": _points_from_pd_series(q_eps, "EPS"),
                "PE": pe_points,
                "PB": pb_points,
                "Reserves": _points_from_pd_series(reserves, "Reserves"),
                "Borrowings": _points_from_pd_series(borrowings, "Borrowings"),
                "Total liabilities": _points_from_pd_series(total_liabilities, "Total liabilities"),
                "Total assets": _points_from_pd_series(total_assets, "Total assets"),
                "Free cash flow": _points_from_pd_series(free_cash_flow, "Free cash flow"),
                "CFO/OP": cfo_op_points,
            }

            metrics = [
                {
                    "label": label,
                    "type": "series",
                    "unit": SERIES_METRICS[label]["unit"],
                    "period": SERIES_METRICS[label]["kind"],
                    "points": series_map.get(label, []),
                }
                for label in selected
                if label in SERIES_METRICS
            ]

            if not any(item.get("points") for item in metrics):
                continue

            price = None
            try:
                fi = getattr(t, "fast_info", None)
                if fi:
                    price = _to_float(getattr(fi, "last_price", None))
            except Exception:
                price = None

            return {
                "stock": {
                    "ticker": symbol.upper(),
                    "name": symbol.upper(),
                    "url": f"https://finance.yahoo.com/quote/{tk}",
                    "price": price,
                    "changePct": None,
                },
                "metrics": metrics,
                "fallbackProvider": "yahoo_finance",
            }
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        return None
    return None


def _search_entries(query: str, limit: int = 20) -> list[CompanyEntry]:
    q = (query or "").strip()
    if not q:
        return []

    cache_key = q.lower()
    cached = _SEARCH_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < SEARCH_TTL_SECONDS:
        return cached[1][:limit]

    _ensure_login()
    url = f"{SCREENER_BASE}/api/company/search/"
    resp = _request_with_retry("GET", url, params={"q": q}, timeout=6)
    resp.raise_for_status()
    rows = resp.json()
    out: list[CompanyEntry] = []
    for row in rows[:limit]:
        rel_url = str(row.get("url") or "")
        ticker_match = re.search(r"/company/([^/]+)/", rel_url + "/")
        ticker = ticker_match.group(1).upper() if ticker_match else ""
        out.append(
            CompanyEntry(
                ticker=ticker,
                name=str(row.get("name") or ticker),
                url=rel_url,
            )
        )
    _SEARCH_CACHE[cache_key] = (time.time(), out)
    return out


def _looks_like_symbol(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9\-]{0,19}", text or ""))


def _try_direct_entry(symbol: str) -> CompanyEntry | None:
    if not _looks_like_symbol(symbol):
        return None
    candidates = [
        f"/company/{symbol}/consolidated/",
        f"/company/{symbol}/",
    ]
    for rel in candidates:
        try:
            html = _fetch_company_html(rel)
            soup = BeautifulSoup(html, "html.parser")
            title = soup.find("h1")
            if title:
                return CompanyEntry(ticker=symbol, name=title.get_text(" ", strip=True), url=rel)
        except Exception:
            continue
    return None


def _resolve_entry(symbol: str) -> CompanyEntry:
    query = (symbol or "").strip()
    if not query:
        raise ValueError("Missing stock symbol")

    upper = query.upper()
    cached = _ENTRY_CACHE.get(upper)
    if cached and time.time() - cached[0] < SEARCH_TTL_SECONDS:
        return cached[1]

    # For stock chart fetches, avoid Screener search API dependency and resolve directly.
    if _looks_like_symbol(upper):
        guessed = CompanyEntry(ticker=upper, name=upper, url=f"/company/{upper}/consolidated/")
        _ENTRY_CACHE[upper] = (time.time(), guessed)
        return guessed

    direct = _try_direct_entry(upper)
    if direct:
        _ENTRY_CACHE[upper] = (time.time(), direct)
        return direct

    matches = _search_entries(query, limit=10)
    if not matches:
        raise ValueError(f"No Screener.in company found for '{query}'")

    for item in matches:
        if item.ticker == upper:
            _ENTRY_CACHE[upper] = (time.time(), item)
            return item

    _ENTRY_CACHE[upper] = (time.time(), matches[0])
    return matches[0]


_PL_METRICS = {"Sales", "Expenses", "Operating Profit", "Net Profit", "EPS"}


def _pick_screener_row(rows: dict[str, list], aliases: list[str]) -> list:
    """Return the first row whose (normalized) name matches an alias.

    Tries exact case-insensitive match first, then a substring fallback so
    minor wording differences (e.g. 'Price to Earning' vs 'Price to Earnings')
    still resolve.
    """
    if not rows:
        return []
    lower_to_real = {str(k).lower(): k for k in rows.keys()}
    for alias in aliases:
        real = lower_to_real.get(alias.lower())
        if real is not None:
            return rows.get(real, [])
    for alias in aliases:
        al = alias.lower()
        for real_lower, real in lower_to_real.items():
            if al == real_lower or al in real_lower or real_lower in al:
                return rows.get(real, [])
    return []


def _build_metric_payload(entry: CompanyEntry, html: str, selected: list[str]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    top = _parse_top_ratios(soup)

    a_periods, a_rows = _parse_table_section(soup, "profit-loss")
    b_periods, b_rows = _parse_table_section(soup, "balance-sheet")
    c_periods, c_rows = _parse_table_section(soup, "cash-flow")
    r_periods, r_rows = _parse_table_section(soup, "ratios")

    # Quarterly P&L lives in the `quarters` section of the same page (real
    # quarter periods like 'Jun 2023'); the `profit-loss` section is annual.
    q_periods: list[str] = []
    q_rows: dict[str, list] = {}
    if any(label in selected for label in _PL_METRICS):
        try:
            q_periods, q_rows = _parse_table_section(soup, "quarters")
        except Exception:
            pass

    # Flow P&L metrics merge full annual history with recent quarterly TTM so
    # the chart keeps ~12y of history AND quarterly cadence in recent years.
    def _flow(metric: str) -> list[dict[str, Any]]:
        return _merge_annual_ttm(
            a_periods,
            _pick_screener_row(a_rows, SCREENER_ALIASES[metric]),
            q_periods,
            _pick_screener_row(q_rows, SCREENER_ALIASES[metric]),
        )

    # Build series using sector-aware aliases (banks/NBFCs name rows differently).
    _nw_periods, _nw_values = _screener_networth(b_periods, b_rows)
    series_map = {
        "Sales": _flow("Sales"),
        "Expenses": _flow("Expenses"),
        "Operating Profit": _flow("Operating Profit"),
        "Net Profit": _flow("Net Profit"),
        "EPS": _flow("EPS"),
        "PE": _series_points(r_periods, _pick_screener_row(r_rows, SCREENER_ALIASES["PE"])),
        "PB": _series_points(r_periods, _pick_screener_row(r_rows, SCREENER_ALIASES["PB"])),
        "Reserves": _series_points(b_periods, _pick_screener_row(b_rows, SCREENER_ALIASES["Reserves"])),
        "Borrowings": _series_points(b_periods, _pick_screener_row(b_rows, SCREENER_ALIASES["Borrowings"])),
        "Total liabilities": _series_points(b_periods, _pick_screener_row(b_rows, SCREENER_ALIASES["Total liabilities"])),
        "Total assets": _series_points(b_periods, _pick_screener_row(b_rows, SCREENER_ALIASES["Total assets"])),
        "Free cash flow": _series_points(c_periods, _pick_screener_row(c_rows, SCREENER_ALIASES["Free cash flow"])),
        "CFO/OP": _series_points(c_periods, _pick_screener_row(c_rows, SCREENER_ALIASES["CFO/OP"])),
        "Fixed Assets": _series_points(b_periods, _pick_screener_row(b_rows, SCREENER_ALIASES["Fixed Assets"])),
        "CFO": _series_points(c_periods, _pick_screener_row(c_rows, SCREENER_ALIASES["CFO"])),
        "Net Cash Flow": _series_points(c_periods, _pick_screener_row(c_rows, SCREENER_ALIASES["Net Cash Flow"])),
        "Cash Conversion Cycle": _series_points(r_periods, _pick_screener_row(r_rows, SCREENER_ALIASES["Cash Conversion Cycle"])),
        "Working Capital Days": _series_points(r_periods, _pick_screener_row(r_rows, SCREENER_ALIASES["Working Capital Days"])),
        "Net Profit Margin": _derive_ratio(
            a_periods, _pick_screener_row(a_rows, SCREENER_ALIASES["Net Profit"]),
            a_periods, _pick_screener_row(a_rows, SCREENER_ALIASES["Sales"]), 100.0,
        ),
        "OPM %": _series_points(a_periods, _pick_screener_row(a_rows, SCREENER_ALIASES["OPM %"])),
        "ROCE %": _series_points(r_periods, _pick_screener_row(r_rows, SCREENER_ALIASES["ROCE %"])),
        "Debtor Days": _series_points(r_periods, _pick_screener_row(r_rows, SCREENER_ALIASES["Debtor Days"])),
        "Inventory Days": _series_points(r_periods, _pick_screener_row(r_rows, SCREENER_ALIASES["Inventory Days"])),
        "ROE %": _series_points(r_periods, _pick_screener_row(r_rows, SCREENER_ALIASES["ROE %"])),
        "Debt/Equity": _derive_ratio(
            b_periods, _pick_screener_row(b_rows, SCREENER_ALIASES["Borrowings"]),
            _nw_periods, _nw_values, 1.0,
        ),
        "Stock Price": [],
        "Market Cap": [],
    }

    # ROE% is a direct ratios row for banks/NBFCs; for other sectors derive it
    # from Net Profit / net worth (Equity Capital + Reserves).
    if not series_map["ROE %"]:
        series_map["ROE %"] = _derive_ratio(
            a_periods, _pick_screener_row(a_rows, SCREENER_ALIASES["Net Profit"]),
            _nw_periods, _nw_values, 100.0,
        )

    # Screener has no per-year PE/PB series (only scalar Stock P/E and Book
    # Value). Derive PE/PB time series when requested.
    if "PE" in selected and not series_map["PE"]:
        try:
            # Prefer full-history PE from Screener EPS x Yahoo prices (~12y);
            # fall back to Yahoo-only PE (~4y) if that yields nothing.
            pe_pts = _pe_from_eps_and_prices(entry.ticker, series_map.get("EPS", []))
            if not pe_pts:
                pe_pts, _ = _fetch_yahoo_pe_pb(entry.ticker)
            if pe_pts:
                series_map["PE"] = pe_pts
        except Exception:
            pass
    if "PB" in selected and not series_map["PB"]:
        try:
            # Prefer full-history PB from Screener net worth x Yahoo prices
            # (~12y); fall back to Yahoo-only PB (~4y) if that yields nothing.
            pb_pts = _pb_from_screener_and_prices(
                entry.ticker, b_periods, b_rows, top.get("Book Value")
            )
            if not pb_pts:
                _, pb_pts = _fetch_yahoo_pe_pb(entry.ticker)
            if pb_pts:
                series_map["PB"] = pb_pts
        except Exception:
            pass

    # Stock Price (monthly close) and Market Cap (price x derived shares) come
    # from Yahoo, clipped to the fundamental history window for alignment.
    if "Stock Price" in selected or "Market Cap" in selected:
        try:
            _start = None
            if a_periods:
                _start = _period_to_date(a_periods[0])
            elif b_periods:
                _start = _period_to_date(b_periods[0])
            _shares = _shares_annual(b_periods, b_rows, top.get("Book Value"))
            _price_pts, _mcap_pts = _price_and_mcap_series(entry.ticker, _start, _shares)
            if "Stock Price" in selected and _price_pts:
                series_map["Stock Price"] = _price_pts
            if "Market Cap" in selected and _mcap_pts:
                series_map["Market Cap"] = _mcap_pts
        except Exception:
            pass

    metrics: list[dict[str, Any]] = []
    for label in selected:
        if label in SERIES_METRICS:
            cfg = SERIES_METRICS[label]
            metrics.append(
                {
                    "label": label,
                    "type": "series",
                    "unit": cfg["unit"],
                    "period": cfg["kind"],
                    "points": series_map.get(label, []),
                }
            )

    title = soup.find("h1")
    company_name = title.get_text(" ", strip=True) if title else entry.name

    return {
        "stock": {
            "ticker": entry.ticker,
            "name": company_name,
            "url": f"{SCREENER_BASE}{entry.url}" if entry.url.startswith("/") else entry.url,
            "price": top.get("Current Price"),
            "changePct": None,
        },
        "metrics": metrics,
    }


@app.route("/")
def index() -> Any:
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/tc-static/<path:asset_path>")
def tradingcharts_static(asset_path: str) -> Any:
    return send_from_directory(TRADINGCHARTS_STATIC_DIR, asset_path)


@app.route("/api/health")
def health() -> Any:
    _ensure_login()
    return jsonify(
        {
            "ok": True,
            "provider": "screener.in",
            "auth_configured": bool(SCREENER_USER and SCREENER_PASS),
            "auth_ok": bool(_LOGIN_STATE.get("ok")),
        }
    )


def _read_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _write_state(payload: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = STATE_FILE.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(payload))
    tmp_file.replace(STATE_FILE)


@app.route("/api/state", methods=["GET", "POST"])
def state() -> Any:
    if request.method == "GET":
        return jsonify(_read_state())
    data = request.get_json(silent=True) or {}
    _write_state(data)
    return jsonify({"ok": True})


@app.route("/api/metrics")
def metrics_catalog() -> Any:
    return jsonify({
        "metrics": ALL_METRICS,
        "metricMeta": {k: {"freq": v["freq"]} for k, v in SERIES_METRICS.items()},
    })


@app.route("/api/search")
def search() -> Any:
    query = request.args.get("q", "")
    try:
        matches = _search_entries(query)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(
        {
            "results": [
                {
                    "ticker": entry.ticker,
                    "name": entry.name,
                    "slug": entry.ticker.lower(),
                    "url": f"{SCREENER_BASE}{entry.url}" if entry.url.startswith("/") else entry.url,
                }
                for entry in matches
            ]
        }
    )


@app.route("/api/stock")
def stock_data() -> Any:
    symbol = request.args.get("symbol", "")
    metrics_param = request.args.get("metrics", "")
    selected = [item.strip() for item in metrics_param.split(",") if item.strip()]
    if not selected:
        selected = ["Sales", "Operating Profit", "Net Profit"]
    invalid = [item for item in selected if item not in ALL_METRICS]
    if invalid:
        return jsonify({"error": f"Unsupported metrics: {', '.join(invalid)}"}), 400

    cache_key = f"{symbol.upper()}|{'|'.join(selected)}"
    cached = _STOCK_CACHE.get(cache_key)

    try:
        entry = _resolve_entry(symbol)
        html = _fetch_company_html(entry.url)
        payload = _build_metric_payload(entry, html, selected)
        _STOCK_CACHE[cache_key] = (time.time(), payload)
        return jsonify(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        yahoo_payload = _build_yahoo_payload(symbol, selected)
        if yahoo_payload:
            _STOCK_CACHE[cache_key] = (time.time(), yahoo_payload)
            return jsonify(yahoo_payload)

        if cached and (time.time() - cached[0] < STOCK_TTL_SECONDS):
            fallback = dict(cached[1])
            fallback["cached"] = True
            fallback["cacheAgeSec"] = int(time.time() - cached[0])
            return jsonify(fallback)

        # Return a safe empty payload to keep UI responsive when upstream is unavailable.
        empty_metrics = [
            {
                "label": label,
                "type": "series",
                "unit": SERIES_METRICS[label]["unit"],
                "period": SERIES_METRICS[label]["kind"],
                "points": [],
            }
            for label in selected
            if label in SERIES_METRICS
        ]
        return jsonify(
            {
                "stock": {
                    "ticker": symbol.upper() or symbol,
                    "name": symbol.upper() or symbol,
                    "url": f"{SCREENER_BASE}/company/{(symbol or '').upper()}/consolidated/",
                    "price": None,
                    "changePct": None,
                },
                "metrics": empty_metrics,
                "cached": False,
                "upstreamError": str(exc),
            }
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
