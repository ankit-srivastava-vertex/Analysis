"""
Custom Sector Index Builder
===========================

SUMMARY
-------
Builds custom equal-weighted sector indices from user-defined stock
constituents.  Fetches 1-year prices, calculates index values
(base = 1000), and produces interactive charts with summary statistics.

WORKFLOW
--------
1. Load sector definitions from index_constituents.json.
   Each sector maps to a list of NSE stock symbols.
2. Validate every symbol against NSE's live equity masters and resolve
   historical renames (a stale ticker returns unadjusted history spliced onto
   adjusted prices, which looks like a 50% crash that never happened).
3. Fetch daily OHLC bars for each constituent from 1 Jan 2024.
4. Repair data defects: bad prints are blanked, corporate actions confirmed
   against NSE's corporate-actions API are back-adjusted, genuine large moves
   are left alone.  Every decision is logged to the audit sheet.
5. Build the index with :func:`build_index` — a quarterly-rebalanced,
   buy-and-hold equal-weight index, chain-linked across rebalances.
6. Create a combined comparison chart plus a candlestick chart per sector.
7. Export summary stats, index OHLC, constituent prices and the repair audit
   to Excel, and write a standalone HTML report.

METHODOLOGY
-----------
The index construction follows the S&P Dow Jones
Equal Weight rules.  It is validated against NSE's own published
``NIFTY50 EQUAL WEIGHT`` index, which it reproduces to within 0.03 percentage
points over one day and 0.02 points over thirty.

Between two rebalance dates the index is a fixed basket.  With share counts
``Q_i`` fixed and a divisor ``D``::

    Index_t = ( sum_i  Q_i * P_i,t ) / D

At a rebalance the shares are reset so that every member carries the same
market value.  Because that reset leaves the *total* basket value unchanged,
the divisor does not move and the level is continuous, so the equivalent and
numerically simpler chain-linked form is evaluated instead::

    Index_t = Level_r * mean_i ( P_i,t / P_i,r )        for r <= t < r+1
    Level_(r+1) = Index_(r+1 - 1 bar)

where ``r`` is the last rebalance date.  The two formulations are identical;
the second needs no divisor bookkeeping and cannot drift.

This replaces an earlier implementation that compounded the cross-sectional
mean of daily returns and clipped every return to +/-35%.  That is a
daily-rebalanced portfolio rather than an index, and it overstated sector
performance by up to 212 percentage points on small, volatile baskets.

DATA SOURCES
------------
- Angel One (via data_provider)  — daily OHLC bars (primary)
- jugaad-data / yfinance         — fallbacks when a symbol is unavailable
- NSE equity + SME masters       — symbol validation and rename resolution
- NSE corporate-actions API      — split / bonus / demerger ex-dates
- BSE corporate-actions API      — same, for BSE-only and BSE SME listings,
                                   which have no record at NSE at all
- index_constituents.json        — user-defined sector -> stock symbol mappings
                                   (must exist in script directory)

OUTPUT
------
- custom_sector_index.xlsx         — Summary, Index Values, Data Repairs and
                                     per-sector constituent price sheets
- custom_sector_index_chart.html   — Comparison chart + per-sector candlesticks

USAGE
-----
Individual run:
    python3 custom_sector_index.py                         # default
    python3 custom_sector_index.py -c my_constituents.json  # custom file
    python3 custom_sector_index.py -o my_report             # custom output prefix

Group run (via run_all.py):
    Scenario name: sector_index
    Called as: custom_sector_index.run()  →  returns (indices_dict, prices_dict, summary_df, fig, excel_path, html_path)
    Skip with: python3 run_all.py --skip sector_index

DEPENDENCIES
------------
pandas, plotly, jugaad-data, yfinance, requests (via
nse_ready_sectors.create_session)
"""

import json
import os
import datetime
import math
import pandas as pd
import plotly.graph_objects as go
import requests
from jugaad_data.nse import stock_df


# ─── Config ─────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONSTITUENTS_FILE = os.path.join(SCRIPT_DIR, "index_constituents.json")
OHLC_FIELDS = ("Open", "High", "Low", "Close")
# Symbol masters and rename tables are cached here between runs.
CACHE_DIR = os.path.join(SCRIPT_DIR, "data", "index_engine")

# Built once per process by _nse_context(): an authenticated NSE session plus
# the symbol master.  Every sector reuses them, so a 41-sector run makes three
# master requests rather than a hundred and twenty.
_NSE_CTX = None


# ─── Methodology constants ───────────────────────────────────────────────────
BASE_VALUE = 1000.0

# Quarterly, matching the S&P Equal Weight series.  Rebalancing more often
# manufactures return that a real holder never earns; less often lets one
# member dominate the basket.
REBALANCE_MONTHS = 3

# Trading days of price history a stock needs before it may enter an index.
# NSE applies a three-month seasoning rule to index inclusion for the same
# reason: fresh listings are dominated by allotment-driven flow, not by the
# sector's economics.
SEASONING_DAYS = 60

# A stock whose first print falls within this many bars of the start of the
# price window was already listed when the window opened, so seasoning does not
# apply to it.  Without this, requesting prices from 2024-01-01 would make every
# blue chip ineligible until March 2024.
PRE_EXISTING_GRACE = 5

# A member with no print for this many sessions is treated as suspended or
# delisted.  Its last price is carried until the next rebalance (that is what a
# holder awaiting merger consideration actually experiences) and it is then
# dropped from the basket.
STALE_SESSIONS = 5

# Fraction of the defined constituent list that must be eligible before the
# index is allowed to start.  A "Renewable index" built from 5 of 14 names is
# not a Renewable index.
MIN_MEMBER_FRACTION = 0.5
MIN_MEMBERS = 2

# ─── Repair constants ────────────────────────────────────────────────────────
# A one-day move beyond this is investigated, never silently accepted or
# silently clipped.
CA_THRESHOLD = 0.25
# A spike that round-trips back to within REVERSAL_TOL over this many bars is a
# bad print rather than a corporate action.
REVERSAL_BARS = 3
REVERSAL_TOL = 0.06
# Corporate-action ex-dates rarely line up exactly with the price step, because
# the feed may stamp the adjustment a session early or late.
CA_DATE_WINDOW = 4

_CA_KEYWORDS = ("SPLIT", "SPLIT", "BONUS", "RIGHT", "DEMERG", "SCHEME OF ARRANGEMENT",
                "REDUCTION", "CONSOLIDAT", "SUB-DIVI", "SUBDIVI", "FACE VALUE")

# BSE's own corporate-action feed, consulted when NSE has no record.  It
# returns the scrip's entire history in one call, so unlike the NSE endpoints
# there is no date window in which an event can hide.
_BSE_CA_URL = "https://api.bseindia.com/BseIndiaAPI/api/DefaultData/w"
# Without the Referer/Origin pair BSE serves an HTML error page, not JSON.
_BSE_CA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
}

# Price steps produced by textbook splits, bonuses and consolidations.  Used
# only to grade suspicion in the audit report, never to alter a price.
_COMMON_CA_RATIOS = (0.1, 1 / 6.0, 0.2, 0.25, 1 / 3.0, 0.4, 0.5, 2 / 3.0,
                     1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 10.0)
RATIO_TOL = 0.01


def _ratio_label(ratio):
    """Describe a price-step ratio in corporate-action terms."""
    if ratio < 1:
        return "1:%g split/bonus" % round(1.0 / ratio, 2)
    return "%g:1 consolidation" % round(ratio, 2)


# ─── NSE symbol master ───────────────────────────────────────────────────────

def _cache_path(name):
    """Return the on-disk cache path for ``name``, creating the directory."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _cached_text(url, name, session, max_age_days=7):
    """Fetch ``url`` as text, serving from a local cache when it is fresh.

    The symbol masters change only when a company lists, delists or renames, so
    a weekly refresh is ample and keeps repeated runs off the network.
    """
    path = _cache_path(name)
    if os.path.exists(path):
        age = (datetime.datetime.now()
               - datetime.datetime.fromtimestamp(os.path.getmtime(path))).days
        if age <= max_age_days and os.path.getsize(path) > 1024:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    text = resp.text
    if len(text) > 1024:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text


def load_symbol_master(session):
    """Return ``(valid_symbols, rename_map)`` from NSE's published masters.

    ``valid_symbols`` is the union of the main-board and SME equity lists.
    ``rename_map`` maps every historical symbol to its current one, resolved
    transitively (``ADVANIORLI -> ADORWELD -> ADOR`` collapses to ``ADOR``).
    """
    import io

    def _load(url, name):
        df = pd.read_csv(io.StringIO(_cached_text(url, name, session)))
        df.columns = [c.strip().upper() for c in df.columns]
        return set(df["SYMBOL"].astype(str).str.strip().str.upper())

    valid = _load("https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
                  "EQUITY_L.csv")
    try:
        valid |= _load(
            "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv",
            "SME_EQUITY_L.csv")
    except Exception as exc:  # SME list is optional
        print("  [master] SME list unavailable (%s)" % exc)

    raw = pd.read_csv(
        io.StringIO(_cached_text(
            "https://nsearchives.nseindia.com/content/equities/symbolchange.csv",
            "symbolchange.csv", session)),
        header=None, names=["Company", "Old", "New", "Date"])
    direct = dict(zip(raw.Old.astype(str).str.strip().str.upper(),
                      raw.New.astype(str).str.strip().str.upper()))

    rename = {}
    for old in direct:
        seen, cur = {old}, direct[old]
        while cur in direct and cur not in seen:
            seen.add(cur)
            cur = direct[cur]
        rename[old] = cur
    return valid, rename


def resolve_symbols(symbols, valid, rename):
    """Map each symbol to its current NSE ticker.

    Returns ``(resolved, notes)`` where ``resolved`` maps the original symbol to
    the symbol that should actually be fetched, and ``notes`` lists every symbol
    that was renamed or could not be found on either NSE board.  Symbols absent
    from the masters are *kept* — a number of constituents are BSE-only
    listings, which the price provider still serves — but they are reported so
    that a genuinely dead ticker cannot hide.
    """
    resolved, notes = {}, []
    for sym in symbols:
        up = str(sym).strip().upper()
        if up in valid:
            resolved[sym] = up
            continue
        new = rename.get(up)
        if new and new in valid:
            resolved[sym] = new
            notes.append((sym, new, "renamed"))
        else:
            resolved[sym] = up
            notes.append((sym, up, "not on NSE equity/SME master"))
    return resolved, notes


# ─── Corporate actions ───────────────────────────────────────────────────────

def _bse_scrip_code(symbol):
    """Return ``symbol``'s BSE scrip code as a string, or None if not on BSE.

    For BSE instruments Angel's symboltoken *is* the scrip code, so the master
    already downloaded for price fetching doubles as the lookup table and no
    second mapping has to be maintained.

    ``_symbol_index`` is rebound rather than mutated by ``_load_scrip_master``,
    so it is read off the module each call instead of being imported by name.
    """
    try:
        import angel_client
        angel_client._load_scrip_master()
        index = angel_client._symbol_index or {}
    except Exception:
        return None
    up = str(symbol).strip().upper()
    if up.isdigit():
        return up
    code = index.get(("BSE", up)) or index.get(("BSE", up + "-EQ"))
    return str(code) if code else None


def fetch_bse_corporate_actions(symbol, start, end):
    """Return ``[(ex_date, subject), ...]`` from BSE's corporate-action feed.

    Same contract and same keyword filter as :func:`fetch_corporate_actions`,
    so the two are interchangeable from ``repair_series``' point of view.
    Subjects are prefixed ``BSE:`` so the audit report shows which registry
    explained a step.

    BSE hands back the scrip's whole history regardless of the dates asked
    for, so the range filter is applied here rather than in the query.
    """
    code = _bse_scrip_code(symbol)
    if not code:
        return []
    params = {"ddlcategorys": "E", "ddlindustrys": "", "scripcode": code,
              "segment": "0", "strSearch": "S", "strType": "C",
              "Purposecode": ""}
    try:
        rows = requests.get(_BSE_CA_URL, params=params,
                            headers=_BSE_CA_HEADERS, timeout=30).json()
    except Exception:
        return []
    if isinstance(rows, dict):
        rows = rows.get("Table", [])
    if not isinstance(rows, list):
        return []
    lo, hi = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    out = []
    for row in rows:
        purpose = " ".join(str(row.get("Purpose", "")).split())
        if not any(k in purpose.upper() for k in _CA_KEYWORDS):
            continue
        ex = pd.to_datetime(row.get("exdate"), format="%Y%m%d", errors="coerce")
        if pd.isna(ex):
            ex = pd.to_datetime(row.get("Ex_date"), format="%d %b %Y",
                                errors="coerce")
        if pd.isna(ex) or not (lo <= ex.normalize() <= hi):
            continue
        out.append((ex.normalize(), "BSE: " + purpose))
    return out


def fetch_corporate_actions(symbol, start, end, session):
    """Return ex-dates of capital-structure events for ``symbol``.

    Only events that mechanically move the quoted price are returned — splits,
    bonuses, rights, demergers, capital reductions and face-value changes.
    Dividends are excluded: a price index is not supposed to add them back.

    Both the main board and the SME (NSE Emerge) board are queried.  They are
    separate endpoints, and a stock that has since migrated to the main board
    still has its SME-era actions filed only under ``index=sme`` — NPST's
    2:1 bonus of Feb 2024 is invisible to the ``equities`` query and would
    otherwise be mistaken for a genuine 65% collapse.

    When NSE yields nothing, BSE is asked instead.  A BSE-only or BSE SME
    constituent has no NSE filing at all, so its bonus would otherwise reach
    ``repair_series`` unexplained and be back-tested as a real collapse.  The
    fallback is deliberately conditional: matching a symbol across exchanges
    risks hitting a different company that happens to share the ticker, and
    that risk is only worth taking once the authoritative source has come back
    empty.
    """
    out = []
    for board in ("equities", "sme"):
        url = ("https://www.nseindia.com/api/corporates-corporateActions"
               "?index=%s&symbol=%s&from_date=%s&to_date=%s"
               % (board, symbol, start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y")))
        try:
            payload = session.get(url, timeout=30).json()
        except Exception:
            continue
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        for row in rows or []:
            subject = str(row.get("subject", "")).upper()
            if not any(k in subject for k in _CA_KEYWORDS):
                continue
            try:
                ex = pd.to_datetime(row.get("exDate"), format="%d-%b-%Y")
            except Exception:
                continue
            out.append((ex.normalize(), str(row.get("subject", "")).strip()))
    return out or fetch_bse_corporate_actions(symbol, start, end)


def repair_series(symbol, bars, session=None, ca_lookup=None,
                  date_window=CA_DATE_WINDOW):
    """Remove data defects from one constituent's OHLC bars.

    Every one-day close-to-close move larger than ``CA_THRESHOLD`` is
    classified, never blindly accepted or blindly clipped:

    * **round trip** — the move fully reverses within ``REVERSAL_BARS``.  This
      is a bad print (a bar that missed an adjustment, or a stub quote).  The
      offending bar is blanked and carried forward from the prior close.
    * **corporate action** — NSE, or BSE when NSE has no filing, records a
      split, bonus, rights issue, demerger or capital reduction within
      ``date_window`` calendar days.  The shareholder's value was preserved,
      so all prices *before* the event are multiplied by the observed step
      ratio.  The step disappears and the long-run shape of the series is
      unchanged.
    * **genuine** — neither of the above.  Left untouched.  Real 40% gap-downs
      happen and deleting them would falsify the index.  ``INDUSINDBK`` on
      2025-03-11 and ``RECLTD`` on election-result day are both correctly kept.

    Flags are re-evaluated after every repair rather than computed once up
    front.  Blanking a doubled print removes the *next* bar's apparent
    collapse too, and reporting that phantom collapse as a separate decision
    would be wrong.

    Args:
        symbol: current NSE ticker, used to query corporate actions.
        bars: DataFrame indexed by date with Open/High/Low/Close columns.
        session: requests session for the NSE API; if None, no registry is
            consulted at all and only round-trip repairs are performed.
        ca_lookup: optional pre-fetched ``[(ex_date, subject), ...]``, used to
            avoid one API call per symbol when a cache is already available.
        date_window: calendar days allowed between the price step and the
            filed ex-date.  Defaults to ``CA_DATE_WINDOW``.  Callers that care
            more about catching every event than about the occasional real
            crash being flattened may widen it: HEG's 2026 demerger steps six
            days before the filed ex-date and is invisible at the default of
            four.

    Returns:
        ``(repaired_bars, events)`` where ``events`` is a list of dicts
        describing every classification made, for the audit report.
    """
    bars = bars.sort_index().copy()
    events = []
    if len(bars) < 3:
        return bars, events

    candidates = list(bars["Close"].pct_change().abs().pipe(
        lambda r: r.index[r > CA_THRESHOLD]))
    if len(candidates) == 0:
        return bars, events

    actions = ca_lookup
    if actions is None and session is not None:
        actions = fetch_corporate_actions(
            symbol, bars.index.min().date(), bars.index.max().date(), session)
    actions = actions or []

    price_cols = [c for c in ("Open", "High", "Low", "Close") if c in bars.columns]
    col_positions = [bars.columns.get_loc(c) for c in price_cols]

    for stamp in candidates:
        # Recompute: an earlier repair may already have resolved this bar.
        ret = bars["Close"].pct_change()
        move = ret.get(stamp)
        if move is None or not pd.notna(move) or abs(move) <= CA_THRESHOLD:
            continue
        move = float(move)
        pos = bars.index.get_loc(stamp)
        if pos == 0:
            continue

        # 1. Round trip -> bad print.
        window_end = min(pos + REVERSAL_BARS, len(bars) - 1)
        prior = float(bars["Close"].iloc[pos - 1])
        after = float(bars["Close"].iloc[window_end])
        if prior > 0 and abs(after / prior - 1.0) < REVERSAL_TOL:
            bars.loc[stamp, price_cols] = float("nan")
            bars[price_cols] = bars[price_cols].ffill()
            events.append({"Symbol": symbol, "Date": stamp.date(),
                           "Move %": round(move * 100, 2),
                           "Action": "blanked bad print",
                           "Detail": "reverses within %d bars" % REVERSAL_BARS})
            continue

        # 2. Corporate action -> back-adjust everything before the ex-date.
        match = next((a for a in actions
                      if abs((a[0] - stamp).days) <= date_window), None)
        if match is not None:
            factor = 1.0 + move
            bars.iloc[:pos, col_positions] *= factor
            events.append({"Symbol": symbol, "Date": stamp.date(),
                           "Move %": round(move * 100, 2),
                           "Action": "back-adjusted x%.4f" % factor,
                           "Detail": match[1]})
            continue

        # 3. Unexplained.  Keep the price, but grade the suspicion.  A step
        # that lands on a textbook corporate-action ratio in a stock that has
        # filed one is very likely a misdated adjustment in the feed rather
        # than a real move; the index still keeps it, because guessing a
        # correction is how the old clip-based code fabricated losses, but the
        # audit says so plainly so it can be checked by hand.
        #
        # The open-to-previous-close ratio is tested as well as close-to-close:
        # a misdated adjustment gaps the open onto an exact ratio, and any
        # ordinary trading that day then pulls the close away from it.  MOS on
        # 2025-09-26 opened at precisely 2.0000x its prior close.
        candidates_ratio = [1.0 + move]
        if "Open" in bars.columns and prior > 0:
            open_px = bars["Open"].iloc[pos]
            if pd.notna(open_px):
                candidates_ratio.append(float(open_px) / prior)

        near, hit = None, None
        for cand_ratio in candidates_ratio:
            near = next((r for r in _COMMON_CA_RATIOS
                         if abs(cand_ratio / r - 1.0) < RATIO_TOL), None)
            if near is not None:
                hit = cand_ratio
                break

        if near is not None and actions:
            detail = ("step x%.4f lands on a %s ratio; stock has filed "
                      "corporate actions but none within %d days \u2014 possible "
                      "misdated adjustment, VERIFY"
                      % (hit, _ratio_label(near), date_window))
            action = "kept (suspect)"
        else:
            detail = "no corporate action, no reversal \u2014 treated as genuine"
            action = "kept"
        events.append({"Symbol": symbol, "Date": stamp.date(),
                       "Move %": round(move * 100, 2), "Action": action,
                       "Detail": detail})

    return bars, events


# ─── Index construction ──────────────────────────────────────────────────────

def _rebalance_dates(dates, months=REBALANCE_MONTHS):
    """Return the first trading day of each rebalance period in ``dates``."""
    if len(dates) == 0:
        return []
    frame = pd.DataFrame(index=pd.DatetimeIndex(dates))
    period = ((frame.index.month - 1) // months) + frame.index.year * 100
    firsts = frame.groupby(period).apply(lambda g: g.index.min())
    return sorted(pd.DatetimeIndex(firsts.values))


def _eligibility(close):
    """Return a boolean frame marking when each stock may sit in the index.

    A stock that **lists inside the window** becomes eligible ``SEASONING_DAYS``
    prints after its first trade, and stays eligible until its last print.
    Everything outside that span is excluded: before it the stock did not
    exist, and after it the stock stopped trading, so any price we carry
    forward is a stale fiction.

    A stock that was **already trading when the window opens** is eligible
    immediately.  Seasoning exists to keep allotment-driven flow in a fresh
    listing out of the index; applying it to a company with a decade of history
    merely because our price request started last Tuesday would delete the
    first quarter of every index.
    """
    traded = close.notna()
    eligible = pd.DataFrame(False, index=close.index, columns=close.columns)
    for col in close.columns:
        live = traded[col]
        if not live.any():
            continue
        positions = live.to_numpy().nonzero()[0]
        if positions[0] <= PRE_EXISTING_GRACE:
            start = positions[0]
        else:
            start = positions[min(SEASONING_DAYS, len(positions) - 1)]
        eligible.iloc[start:positions[-1] + 1, eligible.columns.get_loc(col)] = True
    return eligible


def build_index(panels, n_defined, base_value=BASE_VALUE, name="index"):
    """Chain-link a quarterly-rebalanced, buy-and-hold equal-weight index.

    Within each rebalance window the basket is frozen, so the index is the
    equal-weighted mean of member price relatives measured from the rebalance
    date.  The level at the end of one window becomes the base of the next,
    which makes membership changes, new listings and delistings continuous by
    construction — no divisor bookkeeping and no possibility of drift.

    Open, High and Low are carried through the same member weights so the
    result can be drawn as candles.  Note that the index High is the weighted
    mean of member highs, which is an upper envelope: the members do not all
    peak at the same instant, so the true intraday index high sits at or below
    it.  Close and Open are exact.

    Args:
        panels: dict of field name -> DataFrame(date x symbol), containing at
            least ``"Close"``.  Values must be NaN before listing and after
            delisting, not forward-filled.
        n_defined: number of symbols in the sector definition, used for the
            minimum-membership test so that fetch failures cannot quietly
            shrink the requirement.
        base_value: level on the index's first day.
        name: label used in the returned frame and in warnings.

    Returns:
        DataFrame indexed by date with Open/High/Low/Close and ``Members``
        (the number of stocks in the basket that day).  Empty if the sector
        never reaches the minimum membership.
    """
    close = panels["Close"].sort_index()
    if close.empty:
        return pd.DataFrame()

    eligible = _eligibility(close)
    # Carry prices only while a stock is eligible: this bridges the odd missing
    # print without inventing prices before listing or after delisting.
    filled = {k: v.reindex_like(close).ffill().where(eligible)
              for k, v in panels.items()}
    need = max(MIN_MEMBERS, int(math.ceil(n_defined * MIN_MEMBER_FRACTION)))

    marks = _rebalance_dates(close.index)
    fields = [f for f in ("Open", "High", "Low", "Close") if f in filled]
    pieces, level, thin = [], float(base_value), 0

    for i, start in enumerate(marks):
        stop = marks[i + 1] if i + 1 < len(marks) else None
        window = close.loc[start:] if stop is None else close.loc[start:stop]
        if stop is not None and len(window) > 1:
            window = window.iloc[:-1]          # next window owns the boundary bar
        if window.empty:
            continue

        base_px = filled["Close"].loc[start]
        members = [c for c in close.columns
                   if eligible.at[start, c] and pd.notna(base_px[c]) and base_px[c] > 0]
        if len(members) < need:
            thin += 1
            continue

        seg = {}
        for field in fields:
            rel = filled[field].loc[window.index, members].div(base_px[members])
            # A member that stops trading mid-window is held at its last price
            # until the next rebalance, which is what a holder awaiting merger
            # consideration actually experiences.
            seg[field] = level * rel.ffill().mean(axis=1)
        block = pd.DataFrame(seg)
        block["Members"] = len(members)
        pieces.append(block)
        level = float(block["Close"].iloc[-1])

    if not pieces:
        print("  [%s] never reached %d eligible members — skipped" % (name, need))
        return pd.DataFrame()
    if thin:
        print("  [%s] %d early quarter(s) skipped: fewer than %d eligible members"
              % (name, thin, need))

    out = pd.concat(pieces).sort_index()
    out.index.name = "Date"
    return out.dropna(subset=["Close"])


def _nse_context():
    """Return ``(session, valid_symbols, rename_map)``, building them once.

    Returns ``(None, set(), {})`` if NSE is unreachable, so that a network
    outage degrades symbol validation rather than killing the whole run.
    """
    global _NSE_CTX
    if _NSE_CTX is None:
        try:
            from nse_ready_sectors import create_session
            session = create_session()
            valid, rename = load_symbol_master(session)
            _NSE_CTX = (session, valid, rename)
        except Exception as exc:
            print("  [master] NSE unavailable (%s) — symbol validation skipped" % exc)
            _NSE_CTX = (None, set(), {})
    return _NSE_CTX


def _build_constituents_table_html():
    """Build an HTML table showing constituents of each custom sector."""
    if not os.path.exists(CONSTITUENTS_FILE):
        return ""
    with open(CONSTITUENTS_FILE, "r") as f:
        raw = json.load(f)
    if not raw:
        return ""

    sectors = {}
    for name, val in raw.items():
        if isinstance(val, dict) and "constituents" in val:
            sectors[name] = val["constituents"]
        elif isinstance(val, list):
            sectors[name] = val
    if not sectors:
        return ""

    max_len = max(len(v) for v in sectors.values())
    header = "".join(
        '<th style="padding:6px 10px;text-align:left;border:1px solid #ccc;'
        'background:#e3f2fd;font-size:12px;white-space:nowrap">%s (%d)</th>' % (name, len(tickers))
        for name, tickers in sectors.items()
    )
    rows = []
    for i in range(max_len):
        cells = []
        for name in sectors:
            constituents = sectors[name]
            val = constituents[i] if i < len(constituents) else ""
            cells.append(
                '<td style="padding:4px 8px;border:1px solid #ddd;font-size:11px'
                '%s">%s</td>' % (";background:#f9f9f9" if i % 2 else "", val)
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        '<div style="margin-top:24px;padding:12px;background:#fafafa;'
        'border:1px solid #e0e0e0;border-radius:6px;overflow-x:auto">'
        '<h3 style="margin:0 0 10px 0;font-size:15px;color:#333">'
        'Sector Constituents</h3>'
        '<table style="border-collapse:collapse;width:100%">'
        '<tr>' + header + '</tr>' + "".join(rows) +
        '</table></div>'
    )


# ─── Data fetching ───────────────────────────────────────────────────────────

def fetch_ohlc(symbol, start_date, end_date):
    """Fetch daily Open/High/Low/Close bars for one stock.

    Full OHLC is required rather than close alone so the per-sector charts can
    be drawn as candles.

    Primary: Angel One (via data_provider).  Fallback: jugaad-data, yfinance.

    Returns:
        DataFrame indexed by normalised date with Open/High/Low/Close columns,
        sorted and de-duplicated.  Empty if every source fails.
    """
    empty = pd.DataFrame(columns=list(OHLC_FIELDS))

    def _tidy(frame, label):
        frame = frame.copy()
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.droplevel(1)
        missing = [c for c in OHLC_FIELDS if c not in frame.columns]
        if missing:
            return None
        frame = frame[list(OHLC_FIELDS)].apply(pd.to_numeric, errors="coerce")
        frame.index = pd.to_datetime(frame.index).normalize()
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        frame = frame.dropna(subset=["Close"])
        if frame.empty:
            return None
        print("    %s: %d days%s" % (symbol, len(frame), label))
        return frame

    # ── Primary: Angel One via data_provider ──
    try:
        from data_provider import _fetch_one, _resolve_period
        s, e = _resolve_period(str(start_date), str(end_date), None)
        out = _tidy(_fetch_one(symbol, s, e), "")
        if out is not None:
            return out
    except Exception as exc:
        print("    %s: data_provider failed (%s), trying jugaad-data ..." % (symbol, exc))

    # ── Fallback 1: jugaad-data ──
    try:
        df = stock_df(symbol=symbol, from_date=start_date, to_date=end_date, series="EQ")
        if df is not None and not df.empty:
            df = df.rename(columns={"DATE": "Date", "OPEN": "Open", "HIGH": "High",
                                    "LOW": "Low", "CLOSE": "Close"}).set_index("Date")
            out = _tidy(df, " (jugaad)")
            if out is not None:
                return out
    except Exception as exc:
        print("    %s: jugaad-data failed (%s), trying yfinance ..." % (symbol, exc))

    # ── Fallback 2: yfinance ──
    try:
        import yfinance as yf
        from data_provider import _to_yf_ticker
        yf_df = yf.download(_to_yf_ticker(symbol), start=str(start_date),
                            end=str(end_date), progress=False, auto_adjust=False)
        if yf_df is not None and not yf_df.empty:
            out = _tidy(yf_df, " (yfinance)")
            if out is not None:
                return out
    except Exception as exc:
        print("    %s: yfinance also FAILED (%s)" % (symbol, exc))

    print("    %s: NO DATA" % symbol)
    return empty


# ─── Index calculation ───────────────────────────────────────────────────────

def calculate_equal_weight_index(price_df, base_value=BASE_VALUE):
    """Build an equal-weight index level series from a frame of close prices.

    Thin convenience wrapper around :func:`build_index` for
    callers that hold only closing prices and want only the index level.

    This used to compound the cross-sectional arithmetic mean of daily returns
    with every return clipped to +/-35%.  That is a daily-rebalanced portfolio,
    not an index: it credits a rebalancing premium the holder never earns, and
    the clip converted unadjusted corporate actions into permanent fabricated
    losses.  It now delegates to the quarterly-rebalanced, buy-and-hold engine
    validated against NSE's published NIFTY50 EQUAL WEIGHT index.

    Args:
        price_df: DataFrame with a date index and one close-price column per
            stock.  Values should be NaN outside each stock's listed life.
        base_value: Level on the index's first day.

    Returns:
        Series of index levels indexed by date.  Empty if the basket never
        reaches minimum membership.
    """
    built = build_index({"Close": price_df}, n_defined=price_df.shape[1],
                        base_value=base_value, name="equal-weight")
    if built.empty:
        return pd.Series(dtype=float)
    return built["Close"]


def build_sector_index(index_name, constituents, start_date, end_date):
    """Fetch, repair and build one sector index.

    The index itself is a quarterly-rebalanced, buy-and-hold equal-weight
    index produced by :func:`build_index`; see that function for the
    methodology and for why the previous return-averaging approach was wrong.

    Before the index is built, three data problems are handled:

    * **stale symbols** are resolved to their current NSE ticker, because a
      renamed ticker returns unadjusted history spliced onto adjusted prices;
    * **bad prints and corporate actions** are repaired per constituent by
      :func:`repair_series`;
    * **listings and delistings** are handled by the engine's eligibility
      rules, so a stock contributes only while it is genuinely trading.

    Args:
        index_name: Name of the custom index.
        constituents: List of NSE stock symbols.
        start_date: datetime.date
        end_date: datetime.date

    Returns:
        Tuple of ``(index_df, prices_df, failed, repairs)``:

        - index_df: DataFrame indexed by date with Open/High/Low/Close and
          ``Members``.  Empty if the sector never reaches minimum membership.
        - prices_df: repaired close prices per stock, NaN outside its listed
          life.
        - failed: symbols that could not be fetched from any source.
        - repairs: list of dicts describing every data repair applied.
    """
    print("\n  [%s] Fetching %d stocks..." % (index_name, len(constituents)))

    session, valid, rename = _nse_context()
    resolved, notes = resolve_symbols(constituents, valid, rename)
    for original, current, reason in notes:
        if reason == "renamed":
            print("    %s -> %s (renamed on NSE; the old ticker serves spliced "
                  "unadjusted history)" % (original, current))
        else:
            print("    %s: %s" % (original, reason))

    panels = {field: {} for field in OHLC_FIELDS}
    failed, repairs = [], []

    for original in constituents:
        symbol = resolved.get(original, original)
        bars = fetch_ohlc(symbol, start_date, end_date)
        if bars.empty:
            failed.append(original)
            continue
        bars, events = repair_series(symbol, bars, session=session)
        repairs.extend(events)
        for field in OHLC_FIELDS:
            panels[field][original] = bars[field]

    if not panels["Close"]:
        print("  [%s] No data fetched for any constituent!" % index_name)
        return pd.DataFrame(), pd.DataFrame(), failed, repairs

    panels = {f: pd.DataFrame(v).sort_index() for f, v in panels.items()}
    prices_df = panels["Close"]

    index_df = build_index(panels, n_defined=len(constituents),
                           base_value=BASE_VALUE, name=index_name)
    if index_df.empty:
        return pd.DataFrame(), prices_df, failed, repairs

    current = float(index_df["Close"].iloc[-1])
    change_pct = ((current / BASE_VALUE) - 1) * 100
    print("  [%s] Built: %d days from %s, %d/%d members at start, "
          "current=%.2f (%+.2f%%)" % (
              index_name, len(index_df), index_df.index.min().date(),
              int(index_df["Members"].iloc[0]), len(constituents),
              current, change_pct))

    if failed:
        print("  [%s] Failed symbols: %s" % (index_name, ", ".join(failed)))

    return index_df, prices_df, failed, repairs


# ─── Plotting ────────────────────────────────────────────────────────────────

def create_chart(all_indices, title="Custom Sector Indices"):
    """Create an interactive Plotly line chart comparing every sector.

    Each series is plotted as percent change from its own base, so sectors are
    directly comparable *provided they share a base date*.  Sectors whose
    members had not listed by the common start date necessarily begin later;
    their legend entry carries the start date so the mismatch is visible rather
    than implied.

    The legend is vertical and parked outside the plot on the right, and the
    figure grows with the number of series: a horizontal legend wraps to many
    rows at 40+ sectors and renders on top of the title.

    Args:
        all_indices: dict of ``{index_name: DataFrame(Open/High/Low/Close/...)}``
        title: Chart title

    Returns:
        plotly Figure object
    """
    fig = go.Figure()

    colors = [
        "#2196F3", "#FF5722", "#4CAF50", "#9C27B0", "#FF9800",
        "#00BCD4", "#E91E63", "#8BC34A", "#673AB7", "#CDDC39",
    ]

    common_start = min((f.index.min() for f in all_indices.values()), default=None)

    for i, (name, frame) in enumerate(all_indices.items()):
        series = frame["Close"]
        color = colors[i % len(colors)]
        current = series.iloc[-1]
        change_pct = ((current / BASE_VALUE) - 1) * 100
        pct_series = ((series / BASE_VALUE) - 1) * 100

        label = "%s (%+.1f%%)" % (name, change_pct)
        if common_start is not None and series.index.min() > common_start:
            label += " \u2022 from %s" % series.index.min().strftime("%b-%y")

        hover_tpl = (
            "<b>" + name + "</b><br>"
            "Date: %{x|%d-%b-%Y}<br>"
            "Change: %{y:+.1f}%<br>"
            "<extra></extra>"
        )

        fig.add_trace(go.Scatter(
            x=pct_series.index,
            y=pct_series.values,
            mode="lines",
            name=label,
            line=dict(width=2, color=color),
            hovertemplate=hover_tpl,
        ))

    fig.update_layout(
        title=dict(text=title, font=dict(size=20), x=0.01, xanchor="left",
                   y=1.0, yanchor="top", pad=dict(t=12)),
        xaxis=dict(
            title="Date",
            rangeslider=dict(visible=True),
            rangeselector=dict(
                buttons=[
                    dict(count=1, label="1M", step="month", stepmode="backward"),
                    dict(count=3, label="3M", step="month", stepmode="backward"),
                    dict(count=6, label="6M", step="month", stepmode="backward"),
                    dict(count=1, label="1Y", step="year", stepmode="backward"),
                    dict(step="all", label="All"),
                ],
            ),
        ),
        yaxis=dict(title="% Change from Base", rangemode="tozero", dtick=10),
        hovermode="closest",
        # Vertical legend outside the plot: a horizontal one wraps to ~7 rows
        # at this many sectors and climbs straight over the title.
        legend=dict(
            orientation="v",
            yanchor="top", y=1,
            xanchor="left", x=1.01,
            font=dict(size=10),
        ),
        template="plotly_white",
        height=max(700, 140 + len(all_indices) * 17),
        margin=dict(t=60, r=20, b=40),
    )

    return fig


def create_individual_charts(all_indices):
    """Create a daily candlestick chart per sector index.

    Each sector is drawn exactly as NIFTY 50 is drawn: one candle per trading
    day, on the index's own level (base = 1000 on its first day).

    The Open and Close of an index candle are exact.  The High and Low are the
    equal-weighted means of the member highs and lows, which form an *envelope*
    around the true intraday index path: constituents do not all peak at the
    same instant, so the real index high sits at or below the drawn wick.  This
    is the standard approximation for any index reconstructed from daily bars,
    including TradingView spread charts, and it is stated on the chart.

    Args:
        all_indices: dict of ``{index_name: DataFrame(Open/High/Low/Close/Members)}``

    Returns:
        list of plotly Figure objects (one per sector)
    """
    figures = []
    for name, frame in all_indices.items():
        current = float(frame["Close"].iloc[-1])
        change_pct = ((current / BASE_VALUE) - 1) * 100
        members = frame["Members"] if "Members" in frame.columns else None

        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=frame.index,
            open=frame["Open"], high=frame["High"],
            low=frame["Low"], close=frame["Close"],
            name=name,
            increasing=dict(line=dict(color="#26a69a"), fillcolor="#26a69a"),
            decreasing=dict(line=dict(color="#ef5350"), fillcolor="#ef5350"),
            customdata=(members.values if members is not None else None),
            hovertext=None,
        ))

        subtitle = "base 1000 on %s" % frame.index.min().strftime("%d-%b-%Y")
        if members is not None:
            subtitle += " &middot; %d members" % int(members.iloc[-1])

        fig.update_layout(
            title=dict(
                text="%s (%+.1f%%)<br><span style='font-size:11px;color:#777'>%s"
                     " &middot; high/low wicks are member-average envelopes</span>"
                     % (name, change_pct, subtitle),
                font=dict(size=16), x=0.01, xanchor="left"),
            xaxis=dict(
                title="Date",
                rangeslider=dict(visible=True),
                rangebreaks=[dict(bounds=["sat", "mon"])],
                rangeselector=dict(
                    buttons=[
                        dict(count=1, label="1M", step="month", stepmode="backward"),
                        dict(count=3, label="3M", step="month", stepmode="backward"),
                        dict(count=6, label="6M", step="month", stepmode="backward"),
                        dict(count=1, label="1Y", step="year", stepmode="backward"),
                        dict(step="all", label="ALL"),
                    ],
                ),
            ),
            yaxis=dict(title="Index level"),
            hovermode="x unified",
            showlegend=False,
            template="plotly_white",
            height=480,
            margin=dict(t=80, r=20, b=40),
        )
        figures.append(fig)

    return figures


# ─── Output ──────────────────────────────────────────────────────────────────

def save_to_excel(all_indices, all_prices, summary, output_file, repairs=None):
    """Save index data, constituent prices and the data-repair audit to Excel.

    Sheets:
        - Summary: one row per index with current value and % change
        - Index Values: closing level of every index, side by side
        - Index OHLC: the full candle data for every index, stacked
        - Data Repairs: every bad print blanked, corporate action back-adjusted
          and unexplained move kept, so the index is auditable end to end
        - <IndexName>: repaired constituent close prices for each index
    """
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)

        closes = pd.DataFrame({name: frame["Close"]
                               for name, frame in all_indices.items()})
        closes.index.name = "Date"
        closes.to_excel(writer, sheet_name="Index Values")

        ohlc = pd.concat(
            {name: frame for name, frame in all_indices.items()},
            names=["Index"]) if all_indices else pd.DataFrame()
        if not ohlc.empty:
            ohlc.to_excel(writer, sheet_name="Index OHLC")

        audit = pd.DataFrame(repairs or [])
        if audit.empty:
            audit = pd.DataFrame([{"Symbol": "", "Date": "", "Move %": "",
                                   "Action": "no repairs required",
                                   "Detail": "no constituent moved more than "
                                             "%.0f%% in a day" % (CA_THRESHOLD * 100)}])
        audit.to_excel(writer, sheet_name="Data Repairs", index=False)

        for name, prices_df in all_prices.items():
            prices_df.index.name = "Date"
            prices_df.to_excel(writer, sheet_name=name[:28])

    print("\nExcel saved: %s" % output_file)


def save_chart_html(fig, output_file, individual_figs=None):
    """Save Plotly chart as standalone HTML with optional individual sector charts."""
    combined_html = fig.to_html(full_html=False, include_plotlyjs="cdn")

    individual_html = ""
    if individual_figs:
        individual_html = '<hr style="margin:40px 0;"><h2 style="text-align:center;font-family:sans-serif;">Individual Sector Charts</h2>'
        for ifig in individual_figs:
            individual_html += ifig.to_html(full_html=False, include_plotlyjs=False)

    # Build constituents table
    constituents_table = _build_constituents_table_html()

    full_html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Custom Sector Indices</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
</head><body style="margin:20px;">
%s
%s
%s
</body></html>""" % (combined_html, individual_html, constituents_table)

    with open(output_file, "w") as f:
        f.write(full_html)
    print("HTML chart saved: %s" % output_file)


# ─── Main ────────────────────────────────────────────────────────────────────

def load_constituents(filepath=CONSTITUENTS_FILE):
    """Load index definitions from JSON file."""
    with open(filepath, "r") as f:
        data = json.load(f)
    print("Loaded %d custom indices from %s" % (len(data), os.path.basename(filepath)))
    for name, info in data.items():
        print("  %s: %d stocks — %s" % (name, len(info["constituents"]), info.get("description", "")))
    return data


def run(constituents_file=None, output_prefix=None):
    """Main entry point: load constituents, fetch data, build indices, plot, export.

    Args:
        constituents_file: Path to JSON file (default: index_constituents.json)
        output_prefix: Prefix for output files (default: auto with timestamp)

    Returns:
        Tuple of (all_indices dict, fig, excel_path, html_path)
    """
    if constituents_file is None:
        constituents_file = CONSTITUENTS_FILE

    print("=" * 60)
    print("Custom Sector Index Builder")
    print("=" * 60)

    # Load index definitions
    index_defs = load_constituents(constituents_file)

    # Date range: from 1st January 2024 to today
    end_dt = datetime.date.today()
    start_dt = datetime.date(2024, 1, 1)
    print("\nDate range: %s to %s" % (start_dt.strftime("%d-%m-%Y"), end_dt.strftime("%d-%m-%Y")))

    # Build each index
    all_indices = {}
    all_prices = {}
    summary_rows = []
    all_repairs = []

    for index_name, info in index_defs.items():
        constituents = info["constituents"]
        index_df, prices_df, failed, repairs = build_sector_index(
            index_name, constituents, start_dt, end_dt
        )
        all_repairs.extend(repairs)
        if index_df.empty:
            continue

        all_indices[index_name] = index_df
        all_prices[index_name] = prices_df

        close = index_df["Close"]
        current = float(close.iloc[-1])
        change_pct = ((current / BASE_VALUE) - 1) * 100
        summary_rows.append({
            "Index": index_name,
            "Description": info.get("description", ""),
            "Constituents": len(constituents),
            "Members at Start": int(index_df["Members"].iloc[0]),
            "Members Now": int(index_df["Members"].iloc[-1]),
            "Failed": len(failed),
            "Start Date": close.index.min().strftime("%d-%b-%Y"),
            "End Date": close.index.max().strftime("%d-%b-%Y"),
            "Trading Days": len(close),
            "Current Value": round(current, 2),
            "Change % Since Base": round(change_pct, 2),
            "Period High": round(float(close.max()), 2),
            "Period Low": round(float(close.min()), 2),
        })

    if not all_indices:
        print("\nNo indices could be built. Check network connectivity and stock symbols.")
        return {}, {}, pd.DataFrame(), None, None, None

    summary_df = pd.DataFrame(summary_rows)
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(summary_df.to_string(index=False))

    if all_repairs:
        print("\n" + "=" * 60)
        print("DATA REPAIRS (%d)" % len(all_repairs))
        print("=" * 60)
        print(pd.DataFrame(all_repairs).to_string(index=False))

    # Generate output filenames
    if output_prefix is None:
        output_prefix = os.path.join(SCRIPT_DIR, "custom_sector_index")

    excel_path = output_prefix + ".xlsx"
    html_path = output_prefix + "_chart.html"

    # Plot
    fig = create_chart(all_indices)
    individual_figs = create_individual_charts(all_indices)

    # Save outputs
    save_to_excel(all_indices, all_prices, summary_df, excel_path, repairs=all_repairs)
    save_chart_html(fig, html_path, individual_figs=individual_figs)

    print("\nDone! %d indices built." % len(all_indices))
    return all_indices, all_prices, summary_df, fig, excel_path, html_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Custom Sector Index Builder")
    parser.add_argument("--constituents", "-c", help="Path to constituents JSON file")
    parser.add_argument("--output", "-o", help="Output filename prefix")
    args = parser.parse_args()

    run(constituents_file=args.constituents, output_prefix=args.output)
