"""
FII Equity Cash Market Tracker
===============================
Tracks daily FII buying/selling in the Indian equity cash market.

Charts:
  - FII Daily Net Inflow (₹ Cr) — bar chart
  - FII Cumulative Net Inflow (₹ Cr) — running total
  - FII Daily Change in Net Inflow (₹ Cr)

Data Source:
  - NSDL FPI Monitor: https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx
    (historical data, month by month, from Jan 2024)
  - NSE API: https://www.nseindia.com/api/fiidiiTradeReact
    (today's provisional data)

The script caches daily data in fii_equity_cache.csv. First run fetches
the full history from NSDL. Subsequent runs add today's data from NSE.

Usage:
  python fii_flows.py                 # Fetch history + today & plot
  python fii_flows.py -o my_report    # Custom output prefix
  python fii_flows.py --refresh       # Force re-fetch all NSDL data
"""

import os
import re
import datetime
import time
import requests
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from io import StringIO


# ─── Config ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(SCRIPT_DIR, "fii_equity_cache.csv")
NSE_CASH_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSDL_ARCHIVE_URL = "https://www.fpi.nsdl.co.in/web/Reports/Archive.aspx"
HISTORY_START = datetime.date(2024, 1, 1)
OI_CACHE_FILE = os.path.join(SCRIPT_DIR, "fii_oi_cache.csv")

# Equity-related derivative products for OI aggregation
_EQUITY_DERIV_PRODUCTS = {
    "index futures", "index options",
    "stock futures", "stock options",
}


# ─── Data Fetching ───────────────────────────────────────────────────────────

def create_nse_session():
    """Create a requests session for NSE."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    return session


def create_nsdl_session():
    """Create a requests session for NSDL FPI Monitor."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
    })
    return session


def fetch_today():
    """Fetch today's cash-market FII data from NSE API (provisional)."""
    session = create_nse_session()
    try:
        session.get("https://www.nseindia.com/", timeout=10)
        time.sleep(1)
        r = session.get(NSE_CASH_URL, timeout=10)
        if r.status_code == 200:
            return pd.DataFrame(r.json())
    except Exception:
        pass
    return pd.DataFrame()


def _parse_nsdl_amount(val):
    """Parse NSDL amount string like '(1428.27)' or '14497.08' to float."""
    s = str(val).strip().replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        return -float(s[1:-1])
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _fetch_nsdl_tables(session, year, month):
    """Fetch one month of NSDL data, return parsed HTML tables or None."""
    if year == datetime.date.today().year and month == datetime.date.today().month:
        query_date = datetime.date.today()
    else:
        if month == 12:
            query_date = datetime.date(year + 1, 1, 1) - datetime.timedelta(days=1)
        else:
            query_date = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)

    date_str = query_date.strftime("%m/%d/%Y")

    r = session.get(NSDL_ARCHIVE_URL, timeout=15)
    if r.status_code != 200:
        return None

    text = r.text
    vs_match = re.search(r'id="__VIEWSTATE"\s+value="([^"]*)"', text)
    vsg_match = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]*)"', text)
    ev_match = re.search(r'id="__EVENTVALIDATION"\s+value="([^"]*)"', text)

    if not (vs_match and ev_match):
        return None

    data = {
        "__EVENTTARGET": "btnSubmit1",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": vs_match.group(1),
        "__VIEWSTATEGENERATOR": vsg_match.group(1) if vsg_match else "",
        "__EVENTVALIDATION": ev_match.group(1),
        "hdnDate": date_str,
        "HdnValexceldata": "",
        "hdnFlag": "",
    }

    r2 = session.post(NSDL_ARCHIVE_URL, data=data, timeout=15)
    if r2.status_code != 200 or "Gross Purchases" not in r2.text:
        return None

    try:
        return pd.read_html(StringIO(r2.text))
    except Exception:
        return None


def _parse_equity_rows(tables):
    """Extract FPI equity (Stock Exchange) rows from NSDL tables."""
    equity_table = None
    for tbl in tables:
        cols_str = " ".join(str(c) for c in tbl.columns)
        if "Gross Purchases" in cols_str and "Reporting Date" in cols_str:
            equity_table = tbl
            break

    if equity_table is None:
        return []

    df = equity_table.copy()
    df.columns = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in df.columns]

    mask = (
        df["Debt/Equity"].astype(str).str.strip().str.lower() == "equity"
    ) & (
        df["Investment Route"].astype(str).str.strip().str.lower() == "stock exchange"
    )
    eq_rows = df[mask].copy()

    rows = []
    for _, row in eq_rows.iterrows():
        date_val = str(row["Reporting Date"]).strip()
        if "total" in date_val.lower() or date_val == "nan":
            continue
        try:
            dt = pd.to_datetime(date_val, format="%d-%b-%Y").date()
        except Exception:
            continue
        if dt < HISTORY_START:
            continue

        buy = _parse_nsdl_amount(row.get("Gross Purchases(Rs Crore)", 0))
        sell = _parse_nsdl_amount(row.get("Gross Sales(Rs Crore)", 0))
        net = _parse_nsdl_amount(row.get("Net Investment (Rs Crore)", 0))

        if buy is not None and sell is not None and net is not None:
            rows.append({
                "Date": dt.isoformat(),
                "FII_Buy_Cr": round(buy, 2),
                "FII_Sell_Cr": round(sell, 2),
                "FII_Net_Cr": round(net, 2),
            })

    return rows


def _parse_oi_rows(tables):
    """Extract per-date total FII equity-derivative OI (₹ Cr) from NSDL tables.

    Sums OI Amount in Crore across Index Futures/Options and Stock
    Futures/Options for each trading date.
    """
    deriv_table = None
    for tbl in tables:
        cols_str = " ".join(str(c) for c in tbl.columns)
        if "Derivative" in cols_str and "Reporting Date" in cols_str:
            deriv_table = tbl
            break

    if deriv_table is None:
        return []

    df = deriv_table.copy()
    # Use positional columns: 0=Date, 1=Product, last=OI Amount Cr
    df.columns = range(len(df.columns))

    raw = []
    for _, row in df.iterrows():
        date_val = str(row[0]).strip()
        product = str(row[1]).strip()

        if "total" in date_val.lower() or date_val == "nan" or "compiled" in date_val.lower():
            continue
        if product.lower() not in _EQUITY_DERIV_PRODUCTS:
            continue

        try:
            dt = pd.to_datetime(date_val, format="%d-%b-%Y").date()
        except Exception:
            continue
        if dt < HISTORY_START:
            continue

        oi_val = _parse_nsdl_amount(row[df.columns[-1]])  # last col = OI Amount Cr
        if oi_val is not None:
            raw.append({"Date": dt.isoformat(), "OI_Cr": oi_val})

    if not raw:
        return []

    tmp = pd.DataFrame(raw)
    daily_oi = tmp.groupby("Date")["OI_Cr"].sum().reset_index()
    return [{"Date": r["Date"], "FII_OI_Cr": round(r["OI_Cr"], 2)}
            for _, r in daily_oi.iterrows()]


def fetch_nsdl_month(session, year, month):
    """Fetch one month of FPI data from NSDL Archive.

    Returns (equity_rows, oi_rows) tuple.
    """
    tables = _fetch_nsdl_tables(session, year, month)
    if tables is None:
        return [], []
    return _parse_equity_rows(tables), _parse_oi_rows(tables)


def fetch_nsdl_history(start_date=None):
    """Fetch full FPI equity + OI history from NSDL, month by month.

    Returns (equity_df, oi_df) tuple.
    """
    if start_date is None:
        start_date = HISTORY_START

    today = datetime.date.today()
    session = create_nsdl_session()
    all_equity_rows = []
    all_oi_rows = []

    # Generate list of (year, month) pairs
    months = []
    d = start_date.replace(day=1)
    while d <= today:
        months.append((d.year, d.month))
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1)
        else:
            d = d.replace(month=d.month + 1)

    print("  Fetching %d months from NSDL (%s to %s)..." % (
        len(months), months[0], months[-1]))

    for i, (year, month) in enumerate(months):
        label = datetime.date(year, month, 1).strftime("%b-%Y")
        eq_rows, oi_rows = fetch_nsdl_month(session, year, month)
        all_equity_rows.extend(eq_rows)
        all_oi_rows.extend(oi_rows)
        print("    [%d/%d] %s: %d equity / %d OI days" % (
            i + 1, len(months), label, len(eq_rows), len(oi_rows)))
        time.sleep(0.5)  # Be polite to NSDL servers

    def _to_df(rows):
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["Date"] = pd.to_datetime(df["Date"]).dt.date
        df = df.drop_duplicates(subset=["Date"]).sort_values("Date").reset_index(drop=True)
        return df

    return _to_df(all_equity_rows), _to_df(all_oi_rows)


# ─── Cache ───────────────────────────────────────────────────────────────────

def load_cache():
    """Load cached equity data from local CSV."""
    if os.path.exists(CACHE_FILE):
        df = pd.read_csv(CACHE_FILE)
        df["Date"] = pd.to_datetime(df["Date"]).dt.date
        return df
    return pd.DataFrame()


def save_cache(df):
    """Save equity data to local CSV cache."""
    df.to_csv(CACHE_FILE, index=False)


def load_oi_cache():
    """Load cached OI data from local CSV."""
    if os.path.exists(OI_CACHE_FILE):
        df = pd.read_csv(OI_CACHE_FILE)
        df["Date"] = pd.to_datetime(df["Date"]).dt.date
        return df
    return pd.DataFrame()


def save_oi_cache(df):
    """Save OI data to local CSV cache."""
    df.to_csv(OI_CACHE_FILE, index=False)


def ensure_history(force_refresh=False):
    """Ensure caches have historical data from NSDL. Fetch if missing.

    Returns (equity_df, oi_df) tuple.
    """
    cache_df = load_cache()
    oi_cache_df = load_oi_cache()

    # Check if we already have substantial history
    if not force_refresh and not cache_df.empty and not oi_cache_df.empty:
        earliest = cache_df["Date"].min()
        if earliest <= HISTORY_START + datetime.timedelta(days=7):
            return cache_df, oi_cache_df

    # Determine what months we need
    if force_refresh or cache_df.empty:
        start = HISTORY_START
    else:
        latest_cached = cache_df["Date"].max()
        start = latest_cached.replace(day=1)

    hist_eq_df, hist_oi_df = fetch_nsdl_history(start_date=start)

    if hist_eq_df.empty:
        print("  WARNING: Could not fetch NSDL history")
        return cache_df, oi_cache_df

    # Merge equity cache
    if cache_df.empty:
        cache_df = hist_eq_df
    else:
        existing_dates = set(cache_df["Date"].tolist())
        new_dates_df = hist_eq_df[~hist_eq_df["Date"].isin(existing_dates)]
        cache_df = pd.concat([cache_df, new_dates_df], ignore_index=True)

    cache_df = cache_df.sort_values("Date").reset_index(drop=True)
    save_cache(cache_df)

    # Merge OI cache
    if not hist_oi_df.empty:
        if oi_cache_df.empty:
            oi_cache_df = hist_oi_df
        else:
            existing_oi_dates = set(oi_cache_df["Date"].tolist())
            new_oi = hist_oi_df[~hist_oi_df["Date"].isin(existing_oi_dates)]
            oi_cache_df = pd.concat([oi_cache_df, new_oi], ignore_index=True)
        oi_cache_df = oi_cache_df.sort_values("Date").reset_index(drop=True)
        save_oi_cache(oi_cache_df)

    print("  History loaded: %d equity / %d OI trading days (%s to %s)" % (
        len(cache_df), len(oi_cache_df),
        cache_df["Date"].min().strftime("%d-%b-%Y"),
        cache_df["Date"].max().strftime("%d-%b-%Y"),
    ))
    return cache_df, oi_cache_df


def add_today(cache_df):
    """Fetch today's provisional data from NSE and add to cache if new.

    Returns updated cache DataFrame.
    """
    cash_df = fetch_today()
    if cash_df.empty:
        return cache_df

    fii_rows = cash_df[cash_df["category"].str.contains("FII", case=False, na=False)]
    if fii_rows.empty:
        return cache_df

    fii = fii_rows.iloc[0]
    today = datetime.date.today()

    if not cache_df.empty and today in set(cache_df["Date"].tolist()):
        return cache_df

    buy_val = float(str(fii.get("buyValue", 0)).replace(",", ""))
    sell_val = float(str(fii.get("sellValue", 0)).replace(",", ""))
    net_val = float(str(fii.get("netValue", 0)).replace(",", ""))

    new_row = pd.DataFrame([{
        "Date": today,
        "FII_Buy_Cr": buy_val,
        "FII_Sell_Cr": sell_val,
        "FII_Net_Cr": net_val,
    }])
    cache_df = pd.concat([cache_df, new_row], ignore_index=True)
    cache_df = cache_df.sort_values("Date").reset_index(drop=True)
    save_cache(cache_df)
    print("  Today's NSE data added (provisional)")
    return cache_df


# ─── Charts ──────────────────────────────────────────────────────────────────

def create_chart(equity_df, oi_df=None, title="FII Equity Cash Market Activity"):
    """Create interactive Plotly chart with 3 panels.

    Panels:
      1. Daily Net Inflow (₹ Cr) — bar chart (green=buying, red=selling)
      2. Cumulative Net Inflow (₹ Cr) — running total area chart
      3. Daily Change in Net Inflow (₹ Cr) — bar chart
    """
    edf = equity_df.copy()
    edf["Date"] = pd.to_datetime(edf["Date"])
    edf = edf.sort_values("Date").reset_index(drop=True)
    edf["FII_Cumulative_Cr"] = edf["FII_Net_Cr"].cumsum()
    edf["FII_Net_Change"] = edf["FII_Net_Cr"].diff()

    n_rows = 3
    titles = [
        "FII Daily Net Equity Inflow (₹ Cr)<br><sup>Buy minus Sell for each day — green = net buying, red = net selling</sup>",
        "FII Cumulative Net Equity Inflow (₹ Cr)",
        "FII Daily Change in Net Inflow (₹ Cr)<br><sup>Today's net minus yesterday's net — shows acceleration/deceleration of FII activity</sup>",
    ]
    heights = [0.4, 0.35, 0.25]

    fig = make_subplots(
        rows=n_rows, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        subplot_titles=titles,
        row_heights=heights,
    )

    # Panel 1: Daily net inflow bars
    net_colors = ["#4CAF50" if v >= 0 else "#F44336" for v in edf["FII_Net_Cr"]]
    fig.add_trace(go.Bar(
        x=edf["Date"], y=edf["FII_Net_Cr"],
        name="Daily Net",
        marker_color=net_colors,
        hovertemplate=(
            "<b>FII Daily Net</b><br>"
            "Date: %{x|%d-%b-%Y}<br>"
            "Net: ₹%{y:,.1f} Cr<extra></extra>"
        ),
    ), row=1, col=1)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", row=1, col=1)

    # Panel 2: Cumulative inflow area
    cum_color = "rgb(33, 150, 243)"
    fig.add_trace(go.Scatter(
        x=edf["Date"], y=edf["FII_Cumulative_Cr"],
        mode="lines",
        name="Cumulative Net",
        line=dict(width=2, color=cum_color),
        fill="tozeroy",
        fillcolor="rgba(33, 150, 243, 0.15)",
        hovertemplate=(
            "<b>FII Cumulative Net</b><br>"
            "Date: %{x|%d-%b-%Y}<br>"
            "Cumulative: ₹%{y:,.1f} Cr<extra></extra>"
        ),
    ), row=2, col=1)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", row=2, col=1)

    # Panel 3: Daily change bars
    change = edf["FII_Net_Change"].fillna(0)
    chg_colors = ["#4CAF50" if v >= 0 else "#F44336" for v in change]
    fig.add_trace(go.Bar(
        x=edf["Date"], y=change,
        name="Daily Change",
        marker_color=chg_colors,
        hovertemplate=(
            "<b>FII Daily Change</b><br>"
            "Date: %{x|%d-%b-%Y}<br>"
            "Change: ₹%{y:,.1f} Cr<extra></extra>"
        ),
    ), row=3, col=1)
    fig.add_hline(y=0, line_dash="dash", line_color="gray", row=3, col=1)

    fig.update_layout(
        title=dict(text=title, font=dict(size=20)),
        hovermode="x unified",
        template="plotly_white",
        height=900,
        showlegend=False,
    )

    fig.update_xaxes(
        rangeselector=dict(
            buttons=[
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=3, label="3M", step="month", stepmode="backward"),
                dict(count=6, label="6M", step="month", stepmode="backward"),
                dict(step="all", label="All"),
            ],
        ),
        row=n_rows, col=1,
    )

    return fig


# ─── Output ──────────────────────────────────────────────────────────────────

def save_to_excel(equity_df, output_file):
    """Save equity data to Excel."""
    edf = equity_df.copy()
    edf["FII_Cumulative_Cr"] = edf["FII_Net_Cr"].cumsum()

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        latest = edf.iloc[-1]
        summary_data = {
            "Metric": [
                "Date Range",
                "Trading Days",
                "Today's Net (₹ Cr)",
                "Today's Buy (₹ Cr)",
                "Today's Sell (₹ Cr)",
                "Cumulative Net (₹ Cr)",
                "Max Daily Net (₹ Cr)",
                "Min Daily Net (₹ Cr)",
                "Avg Daily Net (₹ Cr)",
            ],
            "Value": [
                "%s to %s" % (
                    edf["Date"].min().strftime("%d-%b-%Y") if hasattr(edf["Date"].min(), "strftime")
                    else str(edf["Date"].min()),
                    edf["Date"].max().strftime("%d-%b-%Y") if hasattr(edf["Date"].max(), "strftime")
                    else str(edf["Date"].max()),
                ),
                len(edf),
                latest["FII_Net_Cr"],
                latest["FII_Buy_Cr"],
                latest["FII_Sell_Cr"],
                latest["FII_Cumulative_Cr"],
                edf["FII_Net_Cr"].max(),
                edf["FII_Net_Cr"].min(),
                round(edf["FII_Net_Cr"].mean(), 2),
            ],
        }
        pd.DataFrame(summary_data).to_excel(writer, sheet_name="Summary", index=False)
        edf.to_excel(writer, sheet_name="Daily Data", index=False)

    print("Excel saved: %s" % output_file)


def save_chart_html(fig, output_file):
    """Save chart as standalone HTML."""
    html = fig.to_html(full_html=True, include_plotlyjs="cdn")
    with open(output_file, "w") as f:
        f.write(html)
    print("HTML chart saved: %s" % output_file)


# ─── Main ────────────────────────────────────────────────────────────────────

def run(output_prefix=None, force_refresh=False):
    """Main entry point: fetch history + today, chart, export."""
    print("=" * 60)
    print("FII Equity Cash Market Tracker")
    print("=" * 60)

    # Step 1: Ensure we have historical data from NSDL
    print("\n[1] Loading FPI equity + OI history from NSDL...")
    equity_df, oi_df = ensure_history(force_refresh=force_refresh)

    # Step 2: Add today's provisional data from NSE
    print("\n[2] Fetching today's provisional data from NSE...")
    equity_df = add_today(equity_df)

    if equity_df.empty:
        print("\nNo equity data available!")
        return

    # Summary
    latest = equity_df.iloc[-1]
    cumulative = equity_df["FII_Net_Cr"].sum()
    avg_net = equity_df["FII_Net_Cr"].mean()
    print("\n" + "=" * 60)
    date_str = latest["Date"].strftime("%d-%b-%Y") if hasattr(latest["Date"], "strftime") \
        else str(latest["Date"])
    first_date = equity_df["Date"].min()
    first_str = first_date.strftime("%d-%b-%Y") if hasattr(first_date, "strftime") \
        else str(first_date)
    print("SUMMARY (%d trading days: %s to %s)" % (len(equity_df), first_str, date_str))
    print("=" * 60)
    print(f"  Latest Net:       ₹{latest['FII_Net_Cr']:+,.1f} Cr")
    print(f"  Latest Buy:       ₹{latest['FII_Buy_Cr']:,.1f} Cr")
    print(f"  Latest Sell:      ₹{latest['FII_Sell_Cr']:,.1f} Cr")
    print(f"  Cumulative Net:   ₹{cumulative:+,.1f} Cr")
    print(f"  Avg Daily Net:    ₹{avg_net:+,.1f} Cr")

    # Output files
    if output_prefix is None:
        output_prefix = os.path.join(SCRIPT_DIR, "fii_flows")

    excel_path = output_prefix + ".xlsx"
    html_path = output_prefix + "_chart.html"

    # Chart
    fig = create_chart(equity_df, oi_df=oi_df)

    # Save
    save_to_excel(equity_df, excel_path)
    save_chart_html(fig, html_path)

    print("\nDone! %d trading days of FII equity data." % len(equity_df))
    return equity_df, oi_df, fig, excel_path, html_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="FII Equity Cash Market Tracker")
    parser.add_argument("--output", "-o", help="Output filename prefix")
    parser.add_argument("--refresh", action="store_true",
                        help="Force re-fetch all historical data from NSDL")
    args = parser.parse_args()

    run(output_prefix=args.output, force_refresh=args.refresh)
