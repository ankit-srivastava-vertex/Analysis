"""
NSE Ready-Made Sector Relative Strength Analyzer (Official Indices)
==================================================================

SUMMARY
-------
Computes Mansfield Relative Strength for the *ready-made* NSE sectoral
index family (Nifty Auto, Bank, IT, Power, Capital Goods, Telecom,
Hospitals, Insurance, NBFC, … 30 sectors) versus two benchmarks —
Nifty 500 (primary) and Nifty MidSmall 400 (secondary).  Unlike
sector_momentum.py (which builds equal-weighted custom baskets from
index_constituents.json), this uses the *official* free-float index
closing values, so the RS is the true index RS.

This module is self-contained: it bundles both the official-index price
provider and the RS analyzer (previously split across nse_index_provider.py
and nse_sector_rs.py).

PRICE PROVIDER (bundled)
------------------------
1. PRIMARY source — niftyindices.com historical-data backend
   (`Backpage.aspx/getHistoricaldatatabletoString`).  One POST call per
   index returns the full OHLC history for the requested date range.
   This is the official index provider's own backend and is the fast,
   reliable path (~one call per index, ~140 KB each for ~2 years).
2. FALLBACK source — NSE daily index bhavcopy
   (`archives.nseindia.com/.../ind_close_all_<DDMMYYYY>.csv`).  Static,
   no-auth CSVs, one per trading day.  Only used if the niftyindices
   call returns no data for an index.
3. CACHING — each index series is cached under
   data/nse_index_history/<slug>.csv (columns: Date, Close).  A cached
   series is reused without any network call if its most recent date is
   within CACHE_FRESH_DAYS of the requested end date.

RELATIVE STRENGTH (Mansfield)
-----------------------------
  RS > 0  = sector index outperforming the benchmark
  RS < 0  = sector index underperforming the benchmark
  Both sector and benchmark are normalised to 100 at the common start
  date; RS = (sector_norm / bench_norm) * 100, then rebased so 0 = neutral.

WORKFLOW
--------
1. Resolve each sector to its verified niftyindices identifier (and an
   NSE-bhavcopy fallback name).
2. Fetch Nifty 500 + Nifty MidSmall 400 benchmark closes
   (niftyindices primary, bhavcopy fallback, cached).
3. Fetch each sector index's daily close; rebase to BASE_VALUE for the
   "% change" panel.
4. Compute RS vs each benchmark, rank sectors by RS vs Nifty 500.
5. Reuse sector_momentum's chart/Excel writers to emit one tabbed HTML
   (RS vs 500 · RS vs MidSmall 400 · per-sector dual) + a multi-sheet xlsx.

DATA SOURCES
------------
- niftyindices.com  — official historical index OHLC (primary)
- archives.nseindia.com index bhavcopy — daily all-index close (fallback)

OUTPUT (default prefix: nse_sector_rs)
--------------------------------------
- nse_sector_rs.xlsx        — RS Ranking, RS History (vs 500),
                              RS History vs MidSmall 400, Index Values
- nse_sector_rs_chart.html  — single tabbed chart with 3 views:
                              1. RS vs Nifty 500
                              2. RS vs Nifty MidSmall 400
                              3. Per-Sector (both benchmarks)

USAGE
-----
    python3 nse_ready_sectors.py                # build & plot all sectors
    python3 nse_ready_sectors.py -o my_report   # custom output prefix

RUN_ALL INTEGRATION
-------------------
    Scenario name: nse_sector_rs
    Called as: nse_ready_sectors.run()  →  returns
        (rs_dict, indices_dict, ranking_df, fig, excel_path, html_path)
    Skip with: python3 run_all.py --skip nse_sector_rs

DEPENDENCIES
------------
pandas, plotly, requests, sector_momentum
"""

import os
import io
import json
import time
import datetime

import pandas as pd
import requests

from sector_momentum import (
    compute_rs, create_rs_chart, create_individual_dual_chart,
    save_combined_chart_html, save_to_excel, BASE_VALUE,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
START_DATE = datetime.date(2024, 1, 1)

# ─── Price-provider configuration ────────────────────────────────────────────

CACHE_DIR = os.path.join(SCRIPT_DIR, "data", "nse_index_history")
CACHE_FRESH_DAYS = 4  # reuse cache without a network call if this fresh
# Abort the day-by-day bhavcopy stitch after this many consecutive failed
# requests (NSE archive down/blocking) instead of grinding all ~400 days.
_BHAVCOPY_MAX_CONSEC_FAIL = 10

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")

_NIFTYINDICES_HIST = (
    "https://niftyindices.com/Backpage.aspx/getHistoricaldatatabletoString")
_NIFTYINDICES_REFERER = "https://niftyindices.com/reports/historical-data"
_BHAVCOPY_URL = (
    "https://archives.nseindia.com/content/indices/ind_close_all_%s.csv")

# ─── Benchmarks & sector universe ────────────────────────────────────────────

# Benchmarks: (display_name, niftyindices_name, bhavcopy_official_name)
PRIMARY_BENCHMARK = ("Nifty 500", "NIFTY 500", "Nifty 500")
SECONDARY_BENCHMARK = ("Nifty MidSmall 400", "NIFTY MIDSMALLCAP 400",
                       "Nifty MidSmallcap 400")

# Ready-made NSE sector indices.
# (display_name, niftyindices_name, bhavcopy_official_name) — all verified
# against the niftyindices historical API.
SECTORS = [
    # ── Classic sectoral indices ──
    ("Auto", "NIFTY AUTO", "Nifty Auto"),
    ("Bank", "NIFTY BANK", "Nifty Bank"),
    ("Private Bank", "NIFTY PVT BANK", "Nifty Private Bank"),
    ("PSU Bank", "NIFTY PSU BANK", "Nifty PSU Bank"),
    ("Financial Services", "NIFTY FIN SERVICE", "Nifty Financial Services"),
    ("IT", "NIFTY IT", "Nifty IT"),
    ("FMCG", "NIFTY FMCG", "Nifty FMCG"),
    ("Pharma", "NIFTY PHARMA", "Nifty Pharma"),
    ("Healthcare", "NIFTY HEALTHCARE", "Nifty Healthcare Index"),
    ("Metal", "NIFTY METAL", "Nifty Metal"),
    ("Media", "NIFTY MEDIA", "Nifty Media"),
    ("Realty", "NIFTY REALTY", "Nifty Realty"),
    ("Energy", "NIFTY ENERGY", "Nifty Energy"),
    ("Oil & Gas", "NIFTY OIL AND GAS", "Nifty Oil & Gas"),
    ("Consumer Durables", "NIFTY CONSR DURBL", "Nifty Consumer Durables"),
    # ── Newer sectoral indices ──
    ("Capital Goods", "NIFTY CAPITAL GOODS", "Nifty Capital Goods"),
    ("Capital Markets", "NIFTY CAPITAL MARKETS", "Nifty Capital Markets"),
    ("Cement", "NIFTY CEMENT", "Nifty Cement"),
    ("Chemicals", "NIFTY CHEMICALS", "Nifty Chemicals"),
    ("Construction", "NIFTY CONSTRUCTION", "Nifty Construction"),
    ("Consumer Services", "NIFTY CONSUMER SERVICES", "Nifty Consumer Services"),
    ("Commercial & Transport Svcs", "NIFTY COMMERCIAL & TRANSPORT SERVICES",
     "Nifty Commercial & Transport Services"),
    ("Hospitals", "NIFTY HOSPITALS", "Nifty Hospitals"),
    ("Housing Finance", "NIFTY HOUSING FINANCE", "Nifty Housing Finance"),
    ("Insurance", "NIFTY INSURANCE", "Nifty Insurance"),
    ("NBFC", "NIFTY NBFC", "Nifty NBFC"),
    ("Power", "NIFTY POWER", "Nifty Power"),
    ("Retail", "NIFTY RETAIL", "Nifty Retail"),
    ("Telecommunications", "NIFTY TELECOMMUNICATIONS", "Nifty Telecommunications"),
    ("Transportation & Logistics", "NIFTY TRANSPORTATION & LOGISTICS",
     "Nifty Transportation & Logistics"),
]


# ─── Price provider: session ─────────────────────────────────────────────────

def create_session():
    """Return a requests.Session warmed up for niftyindices + NSE archives."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        s.get(_NIFTYINDICES_REFERER, timeout=15)
    except Exception:
        pass
    return s


# ─── Price provider: cache helpers ───────────────────────────────────────────

def _slug(name):
    out = name.lower()
    for a, b in ((" ", "_"), ("&", "and"), ("/", "_"), (".", ""), ("-", "_")):
        out = out.replace(a, b)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_")


def _cache_path(nifty_name):
    return os.path.join(CACHE_DIR, _slug(nifty_name) + ".csv")


def _load_cache(nifty_name):
    path = _cache_path(nifty_name)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, parse_dates=["Date"])
        s = pd.Series(df["Close"].values, index=pd.DatetimeIndex(df["Date"]))
        return s.sort_index().dropna()
    except Exception:
        return None


def _save_cache(nifty_name, series):
    if series is None or series.empty:
        return
    os.makedirs(CACHE_DIR, exist_ok=True)
    df = pd.DataFrame({"Date": series.index, "Close": series.values})
    df.to_csv(_cache_path(nifty_name), index=False)


# ─── Price provider: niftyindices (primary) ──────────────────────────────────

def fetch_niftyindices(session, nifty_name, start_date, end_date):
    """Fetch one index's daily close from niftyindices.  Returns a
    date-indexed pd.Series (may be empty on failure)."""
    payload = {"cinfo": json.dumps({
        "name": nifty_name,
        "startDate": start_date.strftime("%d-%b-%Y"),
        "endDate": end_date.strftime("%d-%b-%Y"),
        "indexName": nifty_name,
    })}
    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": _NIFTYINDICES_REFERER,
        "Origin": "https://niftyindices.com",
    }
    try:
        r = session.post(_NIFTYINDICES_HIST, data=json.dumps(payload),
                         headers=headers, timeout=15)
        if r.status_code != 200:
            return pd.Series(dtype=float)
        rows = json.loads(r.json().get("d", "[]"))
        if not rows:
            return pd.Series(dtype=float)
        dates, closes = [], []
        for row in rows:
            raw_d = str(row.get("HistoricalDate", "")).strip()
            raw_c = str(row.get("CLOSE", "")).replace(",", "").strip()
            try:
                dt = datetime.datetime.strptime(raw_d, "%d %b %Y").date()
                cl = float(raw_c)
            except (ValueError, TypeError):
                continue
            dates.append(pd.Timestamp(dt))
            closes.append(cl)
        if not dates:
            return pd.Series(dtype=float)
        s = pd.Series(closes, index=pd.DatetimeIndex(dates))
        return s[~s.index.duplicated(keep="last")].sort_index().dropna()
    except Exception:
        return pd.Series(dtype=float)


# ─── Price provider: bhavcopy (fallback) ─────────────────────────────────────

def fetch_bhavcopy_range(session, official_name, start_date, end_date):
    """Fallback: stitch daily NSE index bhavcopy CSVs to build one index's
    close series over [start_date, end_date].  Returns a date-indexed
    pd.Series (may be empty).  Skips weekends and missing (holiday) files.

    Guards against multi-hour hangs when the NSE archive is unreachable:
    each request uses a short timeout, and after a sustained run of
    consecutive failures (archive down/blocking) the stitch aborts early
    instead of grinding through ~400 trading days."""
    dates, closes = [], []
    day = start_date
    one = datetime.timedelta(days=1)
    consecutive_failures = 0
    for_break = False
    while day <= end_date:
        if day.weekday() < 5:  # Mon-Fri only
            url = _BHAVCOPY_URL % day.strftime("%d%m%Y")
            ok = False
            try:
                r = session.get(url, timeout=8)
                if r.status_code == 200 and len(r.content) > 200:
                    df = pd.read_csv(io.StringIO(r.text))
                    hit = df[df["Index Name"].astype(str).str.strip()
                             == official_name]
                    if not hit.empty:
                        cl = float(str(hit.iloc[0]["Closing Index Value"])
                                   .replace(",", ""))
                        dates.append(pd.Timestamp(day))
                        closes.append(cl)
                    ok = True  # file fetched (row may be a holiday gap)
            except Exception:
                ok = False
            if ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures >= _BHAVCOPY_MAX_CONSEC_FAIL:
                    # NSE archive is unreachable/blocking — stop early
                    # rather than iterating hundreds more dead requests.
                    for_break = True
                    break
            time.sleep(0.15)
        day += one
    if for_break and not dates:
        return pd.Series(dtype=float)
    if not dates:
        return pd.Series(dtype=float)
    s = pd.Series(closes, index=pd.DatetimeIndex(dates))
    return s.sort_index().dropna()


# ─── Price provider: public entry point ──────────────────────────────────────

def get_index_close(nifty_name, official_name, start_date, end_date,
                    session=None, use_cache=True):
    """Return a date-indexed close Series for one NSE index.

    Order of resolution:
      1. Fresh local cache (no network) if available.
      2. niftyindices.com (primary).
      3. NSE bhavcopy stitch (fallback) if niftyindices yields nothing.

    On a successful network fetch the series is cached for next time.
    """
    if use_cache:
        cached = _load_cache(nifty_name)
        if cached is not None and not cached.empty:
            last = cached.index.max().date()
            if (end_date - last).days <= CACHE_FRESH_DAYS:
                return cached.loc[:pd.Timestamp(end_date)]

    own_session = session is None
    if own_session:
        session = create_session()

    series = fetch_niftyindices(session, nifty_name, start_date, end_date)

    if series.empty and official_name:
        series = fetch_bhavcopy_range(session, official_name,
                                      start_date, end_date)

    if not series.empty:
        _save_cache(nifty_name, series)

    return series


# ─── Analyzer helpers ────────────────────────────────────────────────────────

def _fetch_close(session, nifty_name, official_name, start_dt, end_dt):
    """Fetch one index's close series, rebased to BASE_VALUE at its first
    available date (so the chart's "% change from base" panel is correct).
    Returns (raw_for_rs, rebased_for_chart) — both date-indexed Series."""
    series = get_index_close(
        nifty_name, official_name, start_dt, end_dt, session=session)
    if series is None or series.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    rebased = series / series.iloc[0] * BASE_VALUE
    return rebased, rebased


# ─── Main ────────────────────────────────────────────────────────────────────

def run(output_prefix=None):
    """Main entry point. Returns the same 6-tuple contract as
    sector_momentum.run(): (all_rs, all_indices, ranking_df, fig,
    excel_path, html_path)."""
    print("=" * 60)
    print("NSE Ready-Made Sector Relative Strength Analyzer (Official Indices)")
    print("=" * 60)

    end_dt = datetime.date.today()
    start_dt = START_DATE
    print("\nDate range: %s to %s" % (
        start_dt.strftime("%d-%m-%Y"), end_dt.strftime("%d-%m-%Y")))

    session = create_session()

    # Benchmarks
    p_disp, p_nifty, p_off = PRIMARY_BENCHMARK
    s_disp, s_nifty, s_off = SECONDARY_BENCHMARK

    benchmark = get_index_close(p_nifty, p_off, start_dt, end_dt,
                                session=session)
    if benchmark is None or benchmark.empty:
        print("ERROR: Could not fetch primary benchmark (%s)!" % p_disp)
        return
    benchmark2 = get_index_close(s_nifty, s_off, start_dt, end_dt,
                                 session=session)
    if benchmark2 is None:
        benchmark2 = pd.Series(dtype=float)

    all_indices = {}
    all_rs = {}     # RS vs primary (Nifty 500)
    all_rs2 = {}    # RS vs secondary (Nifty MidSmall 400)
    ranking_rows = []

    print("\nFetching %d sector indices ..." % len(SECTORS))
    for disp, nifty_name, official in SECTORS:
        raw, rebased = _fetch_close(session, nifty_name, official,
                                    start_dt, end_dt)
        if rebased.empty:
            print("  - %-28s (no data, skipped)" % disp)
            continue

        all_indices[disp] = rebased

        rs = compute_rs(rebased, benchmark)
        if rs.empty:
            continue
        all_rs[disp] = rs

        rs2 = (compute_rs(rebased, benchmark2)
               if not benchmark2.empty else pd.Series(dtype=float))
        if not rs2.empty:
            all_rs2[disp] = rs2

        current_rs = rs.iloc[-1] - 100
        lookback = min(20, len(rs))
        rs_trend = rs.iloc[-1] - rs.iloc[-lookback]
        trend_str = ("\u2191 %.1f" % rs_trend if rs_trend > 0
                     else "\u2193 %.1f" % abs(rs_trend))
        current_rs2 = (rs2.iloc[-1] - 100) if not rs2.empty else None
        current_val = rebased.iloc[-1]
        change_pct = ((current_val / BASE_VALUE) - 1) * 100

        ranking_rows.append({
            "Sector": disp,
            "Index": official,
            "RS vs Nifty 500": round(current_rs, 1),
            "RS vs MidSmall 400": (round(current_rs2, 1)
                                   if current_rs2 is not None else None),
            "20D Trend": trend_str,
            "RS Status": "Outperforming" if current_rs >= 0 else "Underperforming",
            "Index Value": round(current_val, 2),
            "Change %": round(change_pct, 2),
        })

    if not all_rs:
        print("No sectors could be analysed!")
        return

    ranking_df = pd.DataFrame(ranking_rows).sort_values(
        "RS vs Nifty 500", ascending=False)

    print("\n" + "=" * 60)
    print("NSE SECTOR RS RANKING (vs Nifty 500)")
    print("=" * 60)
    for _, row in ranking_df.iterrows():
        star = "\u2605" if row["RS vs Nifty 500"] >= 0 else " "
        print("  %s %-28s RS=%+-6.1f %-8s [%s]" % (
            star, row["Sector"], row["RS vs Nifty 500"],
            row["20D Trend"], row["RS Status"]))

    if output_prefix is None:
        output_prefix = os.path.join(SCRIPT_DIR, "nse_sector_rs")
    excel_path = output_prefix + ".xlsx"
    html_path = output_prefix + "_chart.html"

    fig = create_rs_chart(all_rs, all_indices, benchmark_name=p_disp)

    save_to_excel(all_rs, all_indices, ranking_df, excel_path,
                  all_rs_secondary=all_rs2, secondary_name=s_disp)

    sections = [("RS vs %s" % p_disp, fig)]
    if all_rs2:
        fig2 = create_rs_chart(all_rs2, all_indices, benchmark_name=s_disp)
        fig3 = create_individual_dual_chart(all_rs, all_rs2, p_disp, s_disp)
        sections.append(("RS vs %s" % s_disp, fig2))
        sections.append(("Per-Sector (%s vs %s)" % (p_disp, s_disp), fig3))
    else:
        print("  WARN: %s data unavailable; combined chart shows %s only"
              % (s_disp, p_disp))

    save_combined_chart_html(sections, html_path)

    print("\nDone! %d NSE sector indices analysed." % len(all_rs))
    return all_rs, all_indices, ranking_df, fig, excel_path, html_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="NSE Ready-Made Sector Relative Strength Analyzer "
                    "(Official Indices)")
    parser.add_argument("--output", "-o", help="Output filename prefix")
    args = parser.parse_args()

    run(output_prefix=args.output)
