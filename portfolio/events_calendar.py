"""
events_calendar.py — upcoming corporate events for owned & watchlist names
==========================================================================

SUMMARY
-------
For every position in the portfolio, fetches the next 30 days of:
  - Board meetings  (results / dividend / fund-raising / buy-back)
  - Corporate actions (ex-dividend, split, bonus, AGM, record dates)
  - Recent corporate announcements (last 7 days)

Critical for a 3–9 month holder: the bulk of price re-rating happens
around 1–2 earnings cycles inside the holding window — the trader
should never be surprised by results day on a major position.

WORKFLOW
--------
1. holdings_loader.load_holdings()  -> owned symbols
2. Fetch NSE board-meetings JSON for the next 30 days (one network call,
   filtered locally by symbol).
3. Fetch NSE corporate-actions JSON for the next 30 days (one call).
4. Fetch NSE announcements JSON for last 7 days (one call).
5. Cross-tag each row with whether the symbol is OWNED.
6. Return four sheets (Owned-Board-Meetings, Owned-Corp-Actions,
   Owned-Announcements, Notes) and emit a 'today/this-week' summary.

DATA SOURCES
------------
- NSE board meetings   : https://www.nseindia.com/api/corporate-board-meetings
                         params: index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
- NSE corporate actions: https://www.nseindia.com/api/corporates-corporateActions
                         params: index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
- NSE announcements    : https://www.nseindia.com/api/corporate-announcements
                         params: index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
NSE endpoints require a session cookie (handled via _nse_session).

OUTPUT
------
Sheets returned by run():
  Owned Board Meetings  — results / dividend / etc. for owned names (next 30d)
  Owned Corp Actions    — ex-div, splits, bonus, AGM (next 30d)
  Owned Announcements   — last 7 days of disclosures
  Events Notes          — methodology, source URLs, refresh cadence

USAGE
-----
    from portfolio.events_calendar import run
    result = run()
    # CLI:
    python3 -m portfolio.events_calendar

DEPENDENCIES
------------
pandas, openpyxl, requests, holdings_loader (sibling module)
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio.holdings_loader import load_holdings  # noqa: E402

WINDOW_FUTURE_DAYS = 30
WINDOW_PAST_DAYS = 7
NSE_BASE = "https://www.nseindia.com/api"


# ─────────────────────────── NSE session ────────────────────────────────────

def _nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36"),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    for attempt in range(3):
        try:
            r = s.get("https://www.nseindia.com/", timeout=10)
            if r.status_code == 200:
                return s
        except Exception:
            time.sleep(1.5 ** attempt)
    return s


def _nse_get(session: requests.Session, url: str, params: dict) -> Optional[list]:
    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    for key in ("data", "rows", "result"):
                        if key in data and isinstance(data[key], list):
                            return data[key]
                    return []
                return data if isinstance(data, list) else []
        except Exception:
            time.sleep(1.5 ** attempt)
    return None


# ─────────────────────────── fetchers ───────────────────────────────────────

def _fmt(d: dt.date) -> str:
    return d.strftime("%d-%m-%Y")


def fetch_board_meetings(session, frm: dt.date, to: dt.date) -> pd.DataFrame:
    rows = _nse_get(session, f"{NSE_BASE}/corporate-board-meetings",
                    {"index": "equities", "from_date": _fmt(frm),
                     "to_date": _fmt(to)})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df


def fetch_corp_actions(session, frm: dt.date, to: dt.date) -> pd.DataFrame:
    rows = _nse_get(session, f"{NSE_BASE}/corporates-corporateActions",
                    {"index": "equities", "from_date": _fmt(frm),
                     "to_date": _fmt(to)})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def fetch_announcements(session, frm: dt.date, to: dt.date) -> pd.DataFrame:
    rows = _nse_get(session, f"{NSE_BASE}/corporate-announcements",
                    {"index": "equities", "from_date": _fmt(frm),
                     "to_date": _fmt(to)})
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# ─────────────────────────── filtering ──────────────────────────────────────

def _filter_owned(df: pd.DataFrame, owned: set, sym_col_candidates: list) -> pd.DataFrame:
    if df.empty:
        return df
    sym_col = next((c for c in sym_col_candidates if c in df.columns), None)
    if not sym_col:
        return pd.DataFrame()
    out = df[df[sym_col].astype(str).str.upper().isin(owned)].copy()
    return out.reset_index(drop=True)


def _trim_columns(df: pd.DataFrame, keep: list) -> pd.DataFrame:
    if df.empty:
        return df
    cols = [c for c in keep if c in df.columns]
    return df[cols] if cols else df


def _notes_df() -> pd.DataFrame:
    return pd.DataFrame([
        ("Source", "NSE board meetings: /api/corporate-board-meetings"),
        ("Source", "NSE corporate actions: /api/corporates-corporateActions"),
        ("Source", "NSE announcements: /api/corporate-announcements"),
        ("Window", f"Future: next {WINDOW_FUTURE_DAYS} days. "
                   f"Past announcements: last {WINDOW_PAST_DAYS} days."),
        ("Note", "Filtered to symbols in your portfolio. Run daily before "
                 "market open to avoid surprise on results / ex-div day."),
        ("Note", "Board-meeting 'purpose' often includes 'Financial Results' — "
                 "this is the canonical results-date signal."),
    ], columns=["Field", "Value"])


# ─────────────────────────── public API ─────────────────────────────────────

def run(verbose: bool = True) -> dict:
    holdings = load_holdings(verbose=verbose)
    if holdings.empty:
        if verbose:
            print("  [events_calendar] No holdings — nothing to fetch")
        return {"sheets": {}}

    owned = set(holdings["Symbol"].astype(str).str.upper())
    owned.discard("")
    if verbose:
        print(f"  [events_calendar] {len(owned)} unique owned NSE symbols")

    today = dt.date.today()
    future_to = today + dt.timedelta(days=WINDOW_FUTURE_DAYS)
    past_from = today - dt.timedelta(days=WINDOW_PAST_DAYS)

    s = _nse_session()

    if verbose:
        print(f"  [events_calendar] Board meetings {today} → {future_to}")
    bm = fetch_board_meetings(s, today, future_to)
    bm_owned = _filter_owned(bm, owned, ["bm_symbol", "symbol"])
    bm_owned = _trim_columns(bm_owned, [
        "bm_symbol", "symbol", "sm_name", "bm_purpose", "bm_desc",
        "bm_date", "attachment"])

    if verbose:
        print(f"  [events_calendar] Corporate actions {today} → {future_to}")
    ca = fetch_corp_actions(s, today, future_to)
    ca_owned = _filter_owned(ca, owned, ["symbol"])
    ca_owned = _trim_columns(ca_owned, [
        "symbol", "comp", "series", "subject", "exDate",
        "recDate", "bcStartDate", "bcEndDate"])

    if verbose:
        print(f"  [events_calendar] Announcements {past_from} → {today}")
    an = fetch_announcements(s, past_from, today)
    an_owned = _filter_owned(an, owned, ["symbol"])
    an_owned = _trim_columns(an_owned, [
        "symbol", "sm_name", "desc", "attchmntText", "an_dt",
        "attchmntFile"])

    if verbose:
        print(f"  [events_calendar] Owned: BM={len(bm_owned)}  "
              f"CA={len(ca_owned)}  Ann={len(an_owned)}")

    return {"sheets": {
        "Owned Board Meetings": bm_owned,
        "Owned Corp Actions": ca_owned,
        "Owned Announcements": an_owned,
        "Events Notes": _notes_df(),
    }}


def main():
    ap = argparse.ArgumentParser(description="Events Calendar — owned-name events")
    ap.add_argument("--out", default=str(PORTFOLIO_DIR / "events_calendar.xlsx"))
    args = ap.parse_args()

    result = run()
    sheets = result["sheets"]
    if not sheets:
        return
    with pd.ExcelWriter(args.out, engine="openpyxl") as w:
        for name, df in sheets.items():
            df.to_excel(w, sheet_name=name[:31], index=False)
    print(f"\n  ✓ Wrote {args.out}")


if __name__ == "__main__":
    main()
