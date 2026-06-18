"""
tradingcharts/app.py — Flask backend for the TradingCharts UI.

Bridges Angel One SmartAPI (primary) and data_provider/yfinance (fallback)
into the single-page chart frontend. Serves historical OHLCV, symbol search,
live quote polling, and a batched live-tick endpoint backed by an in-memory
cache that the Angel WebSocket thread populates.

Key responsibilities:
  - GET /api/historical: daily candles primarily via angel_download (one call
    returns full history including today's bar after market close); falls
    back to yfinance only if Angel fails. Intraday goes Angel-only when
    credentials are present.
  - Named-index routing via _INDEX_YF_MAP: the RS-benchmark labels
    ("NIFTY 50", "NIFTY MIDCAP 150", "FINNIFTY", etc.) map to synthetic
    ^NSE* tickers resolved by INDEX_OVERRIDES in angel_client.py. Used
    exclusively by the Relative Strength indicator's benchmark fetch;
    direct index charting is not supported (^-tickers and non-benchmark
    index names return empty candles).
  - GET / POST /api/state: server-side persistence of UI state
    (watchlists, drawings, alerts, pane configs, theme,
    SmartVPSG sub-toggles) to state/state.json via atomic os.replace()
    under _STATE_LOCK. Survives port flips and cross-day restarts since
    localStorage is origin-keyed. The set of mirrored keys is defined
    client-side (TRACKED_KEYS in static/index.html) and accepted verbatim
    here — adding a new key requires no backend change.
  - Self-healing port 5050: _ensure_port_free() kills stale tradingcharts
    processes on boot; aborts with a clear error if a foreign process holds
    the port.
  - Live ticks: Angel WS thread (server-side) writes the latest tick into
    an in-memory _LAST_TICKS dict; browser polls GET /api/ticks once per
    second to pull the latest LTPs for all visible panes in one round-trip.
    No socket.io, no long-poll, no FD leak — served by waitress (threaded
    pure-Python WSGI, cross-platform).

Frontend features (entirely in static/index.html + static/drawing-tools.js;
no backend change required to support any of these):

  Charting
    - Chart types:       Candles, Bars, Heikin Ashi (client-side OHLC
                         transform; indicators continue to use real OHLC).
    - Themes:            Dark / Light, toggle in sidebar, persisted via
                         chartTheme key; recolors lightweight-charts and
                         CSS panels in-place.
    - Grid layouts:      1 / 2 / 4 / 6 / 8 panes.
    - Timeframes:        5m, 10m, 15m, 30m, 1h, 1d, 1w, 1mo.
    - View ranges:       1M – 10Y.

  Indicators (overlay + sub-pane)
    - SmartVPSG          Gap/volume-spike markers, 52w stats, R.Vol.
                         Optional sub-toggle: Volume Profile (canvas overlay
                         with POC highlighted, configurable buckets).
    - SupResEPS          MAs (10/20/50/200) + pivot S/R lines.
    - RSI                Standard Wilder, OB/OS lines, configurable.
    - MACD               Fast/slow/signal, histogram, configurable.
    - Rel. Strength      Normalized RS vs any of 9 benchmark indices
                         (NIFTY 50, 500, BANK, MIDCAP 100/150,
                         SMALLCAP 100/250, MIDSMALL 400, FINNIFTY).

  Drawings (TradingView-style, canvas overlay per pane)
    - 14 tools           Trendline, ray, horizontal, vertical, parallel
                         channel, rectangle, price-range, date-range,
                         date+price range, text, comment, fib retracement,
                         fib extension.
    - Drag & adjust      Pixel-space drag during interaction; commits
                         to time/price coords on release.
    - IDs                Every drawing carries a stable id (backfilled on
                         load) so it can be referenced by alerts.

  Alerts (browser notification + audio beep, per-symbol throttling)
    - Price / Volume     Crossing up, crossing down, in-range.
    - Drawing Cross      Triggers when LTP crosses a referenced
                         horizontal / trendline / ray. Trendline level is
                         linearly interpolated at "now"; ray extrapolates.
    - Triggers           Once, hourly (1-12h), or daily at specified time.

  Watchlists
    - Up to 45 lists × 450 stocks each, file upload (TV-style CSV with
      "NSE:" prefix supported), per-row live quote + intraday change.

Hardware sizing (measured June 2026 on macOS, 14-core / 48 GB host):

  Component        Idle      Active (4 panes, 5y daily, live ticks)
  ─────────────    ──────    ──────────────────────────────────────
  Backend RSS      ~120 MB   ~120 MB  (stable; pandas DFs GC quickly)
  Backend CPU      ~0%       <1%      (network-bound on Angel REST)
  Browser tab      ~80 MB    ~250 MB  (4 lightweight-chart instances
                                       + drawings + indicators)

  Min spec:    2 cores · 4 GB RAM · 1 GB free disk · 2 Mbps internet
  Comfortable: 4 cores · 8 GB RAM
  Disk usage:  ~33 MB scrip-master cache (weekly refresh)
               ~1 GB venv (one-time)
               state.json grows with watchlists/drawings (typ. <1 MB)

  Notes:
    - Backend is network-bound; CPU never the bottleneck.
    - Steady-state WS bandwidth: ~1-5 KB/s; cold chart load: ~50-200
      KB per symbol-year of daily candles.
    - Stable internet matters more than raw CPU/RAM — the Angel WS
      auto-reconnects but ticks are lost during disconnect windows.
"""

import os
import sys
import json
import gzip as _gzip_mod
import datetime
import threading
import time

# Add parent dir so we can import angel_client / data_provider
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS

app = Flask(__name__, static_folder="static")
CORS(app, resources={r"/api/*": {"origins": [
    "http://127.0.0.1:5050", "http://localhost:5050"
]}})

# ─── Transparent gzip (stdlib only — no new deps) ────────────────────────────
# ~4x lighter wire payloads (index.html 169KB→36KB, candles JSON ~4-5x).
# Skipped automatically for non-200s, already-encoded bodies (e.g. cached
# pre-gzipped candles), tiny bodies, and clients without gzip support.
_GZIP_TYPES = ("application/json", "text/html", "application/javascript",
               "text/javascript", "text/css", "text/plain", "image/svg+xml")

@app.after_request
def _maybe_gzip(resp):
    try:
        if (resp.status_code != 200
                or resp.headers.get("Content-Encoding")
                or "gzip" not in (request.headers.get("Accept-Encoding") or "").lower()):
            return resp
        ctype = (resp.content_type or "").split(";")[0].strip().lower()
        if ctype not in _GZIP_TYPES:
            return resp
        resp.direct_passthrough = False
        data = resp.get_data()
        if len(data) < 1024:
            return resp
        gz = _gzip_mod.compress(data, 6)
        if len(gz) >= len(data):
            return resp
        resp.set_data(gz)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(gz))
        vary = resp.headers.get("Vary", "")
        if "accept-encoding" not in vary.lower():
            resp.headers["Vary"] = (vary + ", Accept-Encoding").lstrip(", ")
    except Exception:
        pass
    return resp

# ─── Live-tick cache (browser polls /api/ticks; Angel WS thread populates) ───
# Replaces flask-socketio push. Browser pulls latest LTPs once per second.
_LAST_TICKS = {}            # symbol -> {ltp, open, high, low, close, change, volume, ts}
_LAST_TICKS_LOCK = threading.Lock()

# ─── Server-side persisted UI state ─────────────────────────────────────────
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
_STATE_LOCK = threading.Lock()
# ─── Health / observability state ──────────────────────────────────
_BOOT_TS = time.time()
_last_candle_fetch_ts = 0.0  # epoch seconds; updated on successful Angel candle fetch
_ws_connected = False        # True when Angel WS on_open fires; False on close/init fail

# ─── Historical candle cache (LRU + TTL) ─────────────────────────────
# Keyed on (SYMBOL, interval, days, today). Market-aware TTL: entries built
# during market hours contain a still-forming bar → 60s; entries built
# off-hours are immutable → 4h. The key embeds today's date, so day rollover
# can never serve stale data. The cache stores the fully serialized JSON body
# (and its gzip) so a hit is a pure memcpy — no jsonify/compress per request.
from collections import OrderedDict
_CANDLE_CACHE_MAX = 256
_CANDLE_CACHE_TTL_SEC = 60
_CANDLE_CACHE_TTL_OFFHOURS_SEC = 4 * 3600
_candle_cache = OrderedDict()  # key -> (timestamp, market_at_put, body_bytes, gz_bytes)
_candle_cache_lock = threading.Lock()
_candle_cache_hits = 0
_candle_cache_misses = 0

def _candle_cache_get(key):
    """Return (body_bytes, gz_bytes) on hit, else None."""
    global _candle_cache_hits, _candle_cache_misses
    now = time.time()
    with _candle_cache_lock:
        item = _candle_cache.get(key)
        if item is not None:
            ts, market_at_put, body, gz = item
            # Short TTL whenever the data may still be moving: either the
            # entry was built during market hours (non-final last bar) or the
            # market is open right now (pre-open entry must refresh at 09:15).
            ttl = (_CANDLE_CACHE_TTL_SEC
                   if (market_at_put or _is_market_hours())
                   else _CANDLE_CACHE_TTL_OFFHOURS_SEC)
            if (now - ts) < ttl:
                _candle_cache.move_to_end(key)  # LRU touch
                _candle_cache_hits += 1
                return body, gz
        _candle_cache_misses += 1
        return None

def _candle_cache_put(key, candles):
    """Serialize once at insert; returns the candles JSON fragment so the
    miss path can reuse it without a second json.dumps."""
    candles_json = json.dumps(candles, separators=(",", ":"))
    hit_body = ('{"candles":' + candles_json + ',"cached":true}').encode("utf-8")
    hit_gz = _gzip_mod.compress(hit_body, 6)
    with _candle_cache_lock:
        _candle_cache[key] = (time.time(), _is_market_hours(), hit_body, hit_gz)
        _candle_cache.move_to_end(key)
        while len(_candle_cache) > _CANDLE_CACHE_MAX:
            _candle_cache.popitem(last=False)
    return candles_json

# ─── Custom equal-weight indices (CIDX:<name>) ────────────────────────────────
# User-defined thematic baskets from the Analysis-root index_constituents.json.
# Plotted like any symbol: api_historical synthesizes a daily OHLCV series so
# the whole frontend (panes, indicators, RS, drawings) works unchanged.

_ANALYSIS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONSTITUENTS_FILE = os.path.join(_ANALYSIS_ROOT, "index_constituents.json")
_CUSTOM_IDX_BASE = 1000.0      # index base value (matches custom_sector_index.py)
_CUSTOM_RET_CLIP = 0.35        # clip daily constituent returns (splits/demergers)
_custom_index_defs = None      # {UPPER_NAME: {"name", "members", "description"}}


def _load_custom_index_defs():
    """Parse index_constituents.json into {UPPER_NAME: meta}. Cached."""
    global _custom_index_defs
    if _custom_index_defs is not None:
        return _custom_index_defs
    defs = {}
    try:
        with open(_CONSTITUENTS_FILE) as f:
            raw = json.load(f)
        for name, val in raw.items():
            if isinstance(val, dict) and "constituents" in val:
                members, desc = val["constituents"], val.get("description", name)
            elif isinstance(val, list):
                members, desc = val, name
            else:
                continue
            members = [str(m).strip().upper() for m in members if str(m).strip()]
            if members:
                defs[name.strip().upper()] = {
                    "name": name.strip(), "members": members, "description": desc,
                }
    except Exception:
        defs = {}
    _custom_index_defs = defs
    return defs


def _custom_index_symbols():
    """CIDX: symbol strings for search / popular-symbol registration."""
    return [f"CIDX:{k}" for k in _load_custom_index_defs().keys()]


def _build_custom_index(name, start, end):
    """Equal-weight (daily-rebalanced) custom index OHLCV from its constituents.

    Each constituent's clipped close-to-close return is equal-weighted (skipping
    constituents with no data that day, so members can list mid-window without
    dragging the index), chained from base 1000. Intraday shape (O/H/L) comes
    from the average per-constituent open/high/low-to-close ratio. Returns a
    daily DataFrame[Open,High,Low,Close,Volume] or None.
    """
    meta = _load_custom_index_defs().get(name.strip().upper())
    if not meta:
        return None
    members = meta["members"]
    from data_provider import download
    raw = download(members, start=start, end=end, interval="1d")
    if raw is None or raw.empty:
        return None
    raw = raw.sort_index()
    if isinstance(raw.columns, pd.MultiIndex):
        present = [t for t in members if ("Close", t) in raw.columns]
        if not present:
            return None
        def _field(f):
            return pd.concat({t: raw[(f, t)] for t in present}, axis=1)
        close, openp = _field("Close"), _field("Open")
        high, low, vol = _field("High"), _field("Low"), _field("Volume")
    else:
        t0 = members[0]
        close = raw[["Close"]].rename(columns={"Close": t0})
        openp = raw[["Open"]].rename(columns={"Open": t0})
        high = raw[["High"]].rename(columns={"High": t0})
        low = raw[["Low"]].rename(columns={"Low": t0})
        vol = raw[["Volume"]].rename(columns={"Volume": t0})

    rets = close.pct_change().clip(-_CUSTOM_RET_CLIP, _CUSTOM_RET_CLIP)
    idx_ret = rets.mean(axis=1, skipna=True).fillna(0.0)
    close_idx = _CUSTOM_IDX_BASE * (1.0 + idx_ret).cumprod()

    o_ratio = (openp / close).mean(axis=1, skipna=True)
    h_ratio = (high / close).mean(axis=1, skipna=True)
    l_ratio = (low / close).mean(axis=1, skipna=True)

    out = pd.DataFrame(index=close_idx.index)
    out["Open"] = (close_idx * o_ratio).where(o_ratio.notna(), close_idx)
    out["High"] = (close_idx * h_ratio).where(h_ratio.notna(), close_idx)
    out["Low"] = (close_idx * l_ratio).where(l_ratio.notna(), close_idx)
    out["Close"] = close_idx
    # High/Low must bound Open/Close (ratio averaging can otherwise nudge them).
    out["High"] = out[["High", "Open", "Close"]].max(axis=1)
    out["Low"] = out[["Low", "Open", "Close"]].min(axis=1)
    out["Volume"] = vol.sum(axis=1, skipna=True).fillna(0.0)
    return out.dropna(subset=["Close"])


# Cache + concurrency guard for the (heavy) custom-index builds. A single CIDX
# request synchronously fan-fetches ALL its constituents, so a burst of cold
# CIDX loads (e.g. 8 multiscreen panes at once) could otherwise tie up the
# waitress worker pool and starve light endpoints (/api/ticks, /api/state,
# /api/health). The semaphore bounds concurrent cold builds; the TTL cache +
# in-flight dedupe make repeats and duplicate concurrent loads near-free.
_CIDX_BUILD_SEM = threading.BoundedSemaphore(3)   # max concurrent COLD builds
_CIDX_DF_TTL = 1800.0                              # built-df cache TTL (sec)
_cidx_df_cache = {}                                # key -> (ts, DataFrame)
_cidx_df_cache_lock = threading.Lock()
_cidx_inflight = {}                                # key -> threading.Lock
_cidx_inflight_lock = threading.Lock()


def _cidx_cache_get(key):
    with _cidx_df_cache_lock:
        v = _cidx_df_cache.get(key)
        if v is None:
            return None
        ts, df = v
        if (time.time() - ts) > _CIDX_DF_TTL:
            _cidx_df_cache.pop(key, None)
            return None
        return df


def _cidx_cache_put(key, df):
    with _cidx_df_cache_lock:
        _cidx_df_cache[key] = (time.time(), df)
        if len(_cidx_df_cache) > 64:        # bound: keep newest 64 by insert time
            for k in sorted(_cidx_df_cache, key=lambda x: _cidx_df_cache[x][0])[:-64]:
                _cidx_df_cache.pop(k, None)


def _build_custom_index_cached(name, start, end):
    """_build_custom_index with a TTL cache, in-flight dedupe, and a concurrency
    cap. Concurrent identical requests collapse to a single build; a burst of
    cold distinct builds is bounded by _CIDX_BUILD_SEM so it can never saturate
    the worker pool. Same return contract as _build_custom_index."""
    key = (name.strip().upper(), str(start), str(end))
    df = _cidx_cache_get(key)
    if df is not None:
        return df
    with _cidx_inflight_lock:
        lk = _cidx_inflight.get(key)
        if lk is None:
            if len(_cidx_inflight) > 128:   # rare; bound the dedupe map
                _cidx_inflight.clear()
            lk = threading.Lock()
            _cidx_inflight[key] = lk
    with lk:
        df = _cidx_cache_get(key)           # another thread may have built it
        if df is not None:
            return df
        with _CIDX_BUILD_SEM:
            df = _build_custom_index(name, start, end)
        if df is not None:
            _cidx_cache_put(key, df)
        return df


def _load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":"))  # compact: ~2x smaller writes
    os.replace(tmp, STATE_FILE)


# ─── Symbol list cache ────────────────────────────────────────────────────────

_POPULAR_SYMBOLS = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "KOTAKBANK",
    "SBIN", "BHARTIARTL", "ITC", "HINDUNILVR", "LT", "AXISBANK",
    "BAJFINANCE", "MARUTI", "TATAMOTORS", "SUNPHARMA", "TITAN",
    "ASIANPAINT", "ULTRACEMCO", "NESTLEIND", "WIPRO", "HCLTECH",
    "ADANIENT", "ADANIPORTS", "POWERGRID", "NTPC", "ONGC",
    "JSWSTEEL", "TATASTEEL", "HINDALCO", "COALINDIA", "BPCL",
    "GRASIM", "TECHM", "INDUSINDBK", "DIVISLAB", "DRREDDY",
    "CIPLA", "APOLLOHOSP", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO",
    "BRITANNIA", "DABUR", "GODREJCP", "MARICO", "PIDILITIND",
    "HAVELLS", "VOLTAS", "TATAPOWER", "IRCTC", "ZOMATO",
    "PAYTM", "NYKAA", "DELHIVERY", "POLICYBZR", "ASTRAMICRO",
    "DEEPAKNTR", "AARTI", "CLEAN", "AFFLE", "HAPPSTMNDS",
    "ROUTE", "LICI", "TATAELXSI", "PERSISTENT", "COFORGE",
    "LTIM", "MPHASIS", "BANKBARODA", "CANBK", "PNB",
    "IDEA", "VEDL", "SAIL", "NMDC", "GAIL",
    "IOC", "HDFCLIFE", "SBILIFE", "ICICIPRULI", "BAJAJFINSV",
]

# Index name → internal ticker mapping. Used ONLY by the Relative Strength
# indicator's benchmark fetch (entries route through Angel via
# INDEX_OVERRIDES in angel_client.py). Direct index charting is not
# supported — see the gate at the top of api_historical.
_INDEX_YF_MAP = {
    "NIFTY 50":           "^NSEI",
    "NIFTY 500":          "^CRSLDX",
    "NIFTY BANK":         "^NSEBANK",
    "NIFTY MIDCAP 150":   "^NSEMID150",
    "NIFTY SMALLCAP 250": "^NSESML250",
    "NIFTY MIDCAP 100":   "^NSEMID100",
    "NIFTY SMALLCAP 100": "^NSESML100",
    "NIFTY MIDSMALL 400": "^NSEMS400",
    "FINNIFTY":           "^NSEFIN",
}


_all_symbols_cache = None
_index_names_cache = set()   # AMXIDX instrument symbols from the scrip master

def _get_symbol_list():
    """Load all equity symbols from Angel scrip master (NSE, BSE, NSE SME,
    BSE SME). Index instruments (AMXIDX) are excluded from search and
    recorded in _index_names_cache so they don't resolve anywhere."""
    global _all_symbols_cache
    if _all_symbols_cache is not None:
        return _all_symbols_cache
    try:
        from angel_client import SCRIP_MASTER_CACHE, _master_is_fresh, _download_scrip_master
        if not _master_is_fresh():
            _download_scrip_master()
        with open(SCRIP_MASTER_CACHE) as f:
            rows = json.load(f)
        valid_exchanges = {"NSE", "BSE"}
        all_syms = set()
        for r in rows:
            exch = str(r.get("exch_seg", "")).strip().upper()
            sym = str(r.get("symbol", "")).strip().upper()
            if not sym or not exch:
                continue
            if exch not in valid_exchanges:
                continue
            if str(r.get("instrumenttype", "")).strip().upper() == "AMXIDX":
                _index_names_cache.add(sym)
                continue
            base = sym.split("-", 1)[0]
            all_syms.add(base)
        _all_symbols_cache = sorted(all_syms) + _custom_index_symbols()
        return _all_symbols_cache
    except Exception:
        pass
    return _POPULAR_SYMBOLS + _custom_index_symbols()


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


# ─── Bulk/Block deals (institutional footprints) ──────────────────────────────
# Source reality (verified): NSE's historical bulk/block API returns 503, so
# past deals cannot be backfilled. The live snapshot endpoint (today's deals,
# all symbols) works, so history is ACCRUED daily into a local cache — same
# model as delivery%. Saved BULK_BLOCK_Deals_*.xlsx files (superstar-filtered)
# add whatever sparse history they contain.
_BULKBLOCK_DIR = os.path.join(_ANALYSIS_ROOT, "data", "bulkblock")
_BULKBLOCK_CSV = os.path.join(_BULKBLOCK_DIR, "deals.csv")
_BULKBLOCK_COLS = ["date", "symbol", "exchange", "deal_type", "side", "client", "qty", "price"]
_bulkblock_lock = threading.Lock()
_bulkblock_refreshed_on = None   # date-string guard: snapshot fetched once/day

_MONTHS = {"jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05",
           "jun": "06", "jul": "07", "aug": "08", "sep": "09", "oct": "10",
           "nov": "11", "dec": "12"}


def _bb_norm_date(s):
    """Normalise '16-Jun-2026' / '15/06/2026' / '2026-06-16' → 'YYYY-MM-DD'."""
    s = str(s).strip()
    if not s:
        return None
    try:
        if "-" in s and s[:4].isdigit():           # already ISO
            return s[:10]
        if "/" in s:                                 # DD/MM/YYYY
            d, m, y = s.split("/")[:3]
            return f"{y}-{int(m):02d}-{int(d):02d}"
        if "-" in s:                                 # DD-Mon-YYYY
            d, mon, y = s.split("-")[:3]
            mm = _MONTHS.get(mon[:3].lower())
            if mm:
                return f"{y}-{mm}-{int(d):02d}"
    except Exception:
        return None
    return None


def _bb_side(v):
    v = str(v).strip().upper()
    if v.startswith("B"):
        return "BUY"
    if v.startswith("S"):
        return "SELL"
    return v or "?"


# Client tier keywords — order matters (T3 short-circuit, then T1, then T2).
# T1 = smart money (MFs, insurance, pension, FPIs, sovereign).
# T2 = corporates / treasuries / generic Ltd entities.
# T3 = retail / HUF / broker-arb / individual (often noise).
_BB_T3_KEYS = ("HUF", "BROKING", "STOCK BROK")
_BB_T1_KEYS = (
    "MUTUAL FUND", " MF ", " MF.", "ASSET MANAG", "PENSION", "NPS TRUST",
    "LIFE INSURANCE", " LIC ", "LIC OF INDIA", "GENERAL INSURANCE",
    "FPI", "FII", "FOREIGN PORTFOLIO", "FOREIGN INVEST",
    "NORGES", "BLACKROCK", "VANGUARD",
    "MORGAN STANLEY", "GOLDMAN", "JPMORGAN", "JP MORGAN",
    "DEUTSCHE BANK", "CITIGROUP", "BARCLAYS", "CREDIT SUISSE",
    "SOCIETE GENERALE", "BNP PARIBAS", "HSBC", "UBS AG", "NOMURA",
    "MERRILL LYNCH", "BANK OF AMERICA", "MACQUARIE",
    "FIDELITY", "FRANKLIN", "TEMPLETON", "INVESCO", "CAPITAL GROUP",
    "GIC SINGAPORE", "ABU DHABI INVESTMENT",
    "MONETARY AUTHORITY", "SOVEREIGN",
    "KOTAK FUNDS", "HDFC FUNDS", "AXIS FUNDS", "SBI FUNDS", "ICICI PRUDENTIAL",
    " AMC ", "AMC LIMITED", "AMC LTD",
)
_BB_T2_KEYS = (
    "LIMITED", " LTD", "PVT", "PRIVATE", "CORPORATION", " CORP",
    " INC", " PLC", "COMPANY", "TRUST", "FOUNDATION",
    "ENTERPRISES", "HOLDINGS", "INDUSTRIES", " LLP", " LLC",
    "PARTNERS", "CAPITAL", "INVESTMENT",
)


def _bb_tier_client(name):
    n = (name or "").upper().strip()
    if not n:
        return "T3"
    for k in _BB_T3_KEYS:
        if k in n:
            return "T3"
    for k in _BB_T1_KEYS:
        if k in n:
            return "T1"
    if "FUND" in n:           # generic fund (e.g. "ABC India Fund") — smart money proxy
        return "T1"
    for k in _BB_T2_KEYS:
        if k in n:
            return "T2"
    return "T3"


def _bb_load_cache():
    try:
        if os.path.exists(_BULKBLOCK_CSV):
            df = pd.read_csv(_BULKBLOCK_CSV, dtype=str)
            for c in _BULKBLOCK_COLS:
                if c not in df.columns:
                    df[c] = ""
            return df[_BULKBLOCK_COLS]
    except Exception:
        pass
    return pd.DataFrame(columns=_BULKBLOCK_COLS)


def _bb_write_cache(df):
    try:
        os.makedirs(_BULKBLOCK_DIR, exist_ok=True)
        df = df.drop_duplicates(subset=["date", "symbol", "deal_type", "side", "client", "qty"])
        df = df.sort_values(["date", "symbol"]).reset_index(drop=True)
        tmp = _BULKBLOCK_CSV + ".tmp"
        df.to_csv(tmp, index=False)
        os.replace(tmp, _BULKBLOCK_CSV)
    except Exception:
        pass


def _bb_rows_from_snapshot():
    """Best-effort fetch of today's NSE bulk+block deals (all symbols)."""
    rows = []
    try:
        import requests
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com/market-data/live-market-action/bulk-block-deals",
        })
        s.get("https://www.nseindia.com", timeout=8)
        r = s.get("https://www.nseindia.com/api/snapshot-capital-market-largedeal", timeout=12)
        if r.status_code != 200:
            return rows
        payload = r.json()
        for key, dtype in (("BULK_DEALS_DATA", "bulk"), ("BLOCK_DEALS_DATA", "block")):
            for d in payload.get(key, []) or []:
                dt = _bb_norm_date(d.get("date"))
                sym = str(d.get("symbol", "")).strip().upper()
                if not dt or not sym:
                    continue
                rows.append({
                    "date": dt, "symbol": sym, "exchange": "NSE", "deal_type": dtype,
                    "side": _bb_side(d.get("buySell")), "client": str(d.get("clientName", "")).strip(),
                    "qty": str(d.get("qty", "")).replace(",", ""), "price": str(d.get("watp", "")),
                })
    except Exception:
        pass
    return rows


# Strip these suffix tokens from BSE Scrip Names when guessing NSE symbol.
_BSE_NAME_DROP = {
    "LIMITED", "LTD", "LTD.", "PVT", "PRIVATE", "COMPANY", "CO", "CO.",
    "INDIA", "INDIAN", "GROUP", "THE", "AND", "&", "OF", "CORPORATION",
    "CORP", "CORP.", "HOLDINGS", "ENTERPRISES",
}
_bse_symbol_index = None   # lazy set of all known NSE symbols (from index_constituents.json)


def _nse_symbol_universe():
    """Set of all NSE symbols from index_constituents.json (used for BSE name mapping)."""
    global _bse_symbol_index
    if _bse_symbol_index is None:
        s = set()
        for meta in _load_custom_index_defs().values():
            for m in meta.get("members", []):
                s.add(m.upper())
        _bse_symbol_index = s
    return _bse_symbol_index


def _bse_to_nse_symbol(scrip_name):
    """Map a BSE Scrip Name to its likely NSE symbol. Returns the best guess
    string (uppercased, spaces removed). Tries: full-name-joined, first-2-tokens-
    joined, first-token \u2014 returning the first that hits the NSE symbol universe.
    Falls back to the joined-stripped-name so the row is still cached and can be
    looked up later if a manual map is added."""
    raw = str(scrip_name or "").upper().strip()
    if not raw:
        return ""
    toks = [t for t in raw.replace(".", " ").replace("&", " ").split() if t and t not in _BSE_NAME_DROP]
    if not toks:
        toks = raw.split()
    universe = _nse_symbol_universe()
    candidates = []
    if toks:
        candidates.append("".join(toks))
        if len(toks) >= 2:
            candidates.append("".join(toks[:2]))
        candidates.append(toks[0])
    # First match against known NSE universe wins
    for c in candidates:
        if c in universe:
            return c
    # Fallback: longest candidate (most specific) \u2014 cached under that key
    return candidates[0] if candidates else raw


def _bb_rows_from_bse_snapshot():
    """Best-effort fetch of today's BSE bulk+block deals via api.bseindia.com.
    The BSE endpoint only returns the most recent trading day's data (Fdate/Tdate
    are silently ignored) \u2014 so call this once per day to grow forward coverage."""
    rows = []
    try:
        import requests
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bseindia.com/markets/equity/EQReports/bulk_deals.aspx",
            "Origin": "https://www.bseindia.com",
        })
        endpoints = [
            ("https://api.bseindia.com/BseIndiaAPI/api/BulkDeal_Beta/w", "bulk"),
            ("https://api.bseindia.com/BseIndiaAPI/api/BlockDeal_Beta/w", "block"),
        ]
        for url, dtype in endpoints:
            try:
                r = s.get(url, timeout=15)
                if r.status_code != 200:
                    continue
                for d in (r.json() or {}).get("Table", []) or []:
                    dt = _bb_norm_date(d.get("DEAL_DATE"))
                    scrip_name = d.get("ScripName") or d.get("Scrip Name") or ""
                    sym = _bse_to_nse_symbol(scrip_name)
                    if not dt or not sym:
                        continue
                    rows.append({
                        "date": dt, "symbol": sym, "exchange": "BSE", "deal_type": dtype,
                        "side": _bb_side(d.get("TRANSACTION_TYPE")),
                        "client": str(d.get("CLIENT_NAME", "")).strip(),
                        "qty": str(d.get("QUANTITY", "")).replace(",", ""),
                        "price": str(d.get("PRICE", "")),
                    })
            except Exception:
                continue
    except Exception:
        pass
    return rows


def _bb_rows_from_excels():
    """Parse saved BULK_BLOCK_Deals_*.xlsx (nse_*/bse_* sheets) for backfill."""
    import glob
    rows = []
    patterns = [os.path.join(_ANALYSIS_ROOT, "BULK_BLOCK_Deals_*.xlsx"),
                os.path.join(_ANALYSIS_ROOT, "Output", "BULK_BLOCK_Deals_*.xlsx")]
    files = []
    for p in patterns:
        files.extend(glob.glob(p))
    for f in files:
        try:
            xl = pd.ExcelFile(f)
        except Exception:
            continue
        for sheet in xl.sheet_names:
            sl = sheet.lower()
            if not (sl.startswith("nse_") or sl.startswith("bse_")):
                continue
            exch = "NSE" if sl.startswith("nse_") else "BSE"
            dtype = "block" if "block" in sl else "bulk"
            try:
                df = xl.parse(sheet)
            except Exception:
                continue
            cols = {c.lower().strip(): c for c in df.columns}
            if "status" in cols and len(df.columns) == 1:
                continue
            def pick(*names):
                for n in names:
                    if n in cols:
                        return cols[n]
                return None
            c_date = pick("date", "deal date")
            c_sym = pick("symbol", "scrip name", "scrip code")
            c_side = pick("buysell", "buy/sell")
            c_cli = pick("clientname", "client name")
            c_qty = pick("qty", "quantity")
            c_px = pick("watp", "price")
            if not (c_date and c_sym):
                continue
            for _, r in df.iterrows():
                dt = _bb_norm_date(r.get(c_date))
                sym = str(r.get(c_sym, "")).strip().upper()
                if not dt or not sym or sym == "NAN":
                    continue
                rows.append({
                    "date": dt, "symbol": sym, "exchange": exch, "deal_type": dtype,
                    "side": _bb_side(r.get(c_side)) if c_side else "?",
                    "client": str(r.get(c_cli, "")).strip() if c_cli else "",
                    "qty": str(r.get(c_qty, "")).replace(",", "") if c_qty else "",
                    "price": str(r.get(c_px, "")) if c_px else "",
                })
    return rows


def _ensure_bulkblock_cache():
    """Build/refresh the deal cache. Backfill from Excels once (if cache empty);
    fetch the live snapshot at most once per calendar day. Best-effort."""
    global _bulkblock_refreshed_on
    with _bulkblock_lock:
        df = _bb_load_cache()
        new_rows = []
        if df.empty:
            new_rows.extend(_bb_rows_from_excels())
        today = str(datetime.date.today())
        if _bulkblock_refreshed_on != today:
            new_rows.extend(_bb_rows_from_snapshot())
            new_rows.extend(_bb_rows_from_bse_snapshot())
            _bulkblock_refreshed_on = today
        if new_rows:
            df = pd.concat([df, pd.DataFrame(new_rows, columns=_BULKBLOCK_COLS)], ignore_index=True)
            _bb_write_cache(df)
            df = _bb_load_cache()
        return df


@app.route("/api/bulkblock")
def api_bulkblock():
    """Return bulk/block deals for a symbol (institutional footprint markers).
    Params: symbol. Returns {deals:[{date,type,side,client,qty,price}]}."""
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"deals": []})
    try:
        df = _ensure_bulkblock_cache()
        sub = df[df["symbol"] == symbol]
        deals = [{
            "date": r["date"], "type": r["deal_type"], "side": r["side"],
            "client": r["client"], "qty": r["qty"], "price": r["price"],
            "tier": _bb_tier_client(r["client"]),
        } for _, r in sub.iterrows()]
        return jsonify({"deals": deals})
    except Exception as e:
        return jsonify({"deals": [], "error": str(e)})


@app.route("/api/symbols")
def api_symbols():
    """Return popular symbols + custom indices (full list searched via /api/search)."""
    return jsonify({"symbols": _POPULAR_SYMBOLS + _custom_index_symbols()})


@app.route("/api/search")
def api_search():
    """Search symbols by prefix (min 3 chars)."""
    q = request.args.get("q", "").strip().upper()
    if not q or len(q) < 3:
        return jsonify({"results": []})
    all_syms = _get_symbol_list()
    matches = [s for s in all_syms if s.startswith(q)][:30]
    if not matches:
        matches = [s for s in all_syms if q in s][:30]
    return jsonify({"results": matches})


@app.route("/api/state", methods=["GET"])
def api_state_get():
    """Return server-side persisted UI state (watchlists, drawings, alerts, etc)."""
    with _STATE_LOCK:
        return jsonify(_load_state())


@app.route("/api/state", methods=["POST"])
def api_state_post():
    """Replace server-side persisted UI state with the posted JSON object.
    Body must be a JSON object; non-objects are rejected."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    with _STATE_LOCK:
        _save_state(payload)
    return jsonify({"ok": True})


def _merge_today_candle(df, symbol):
    """If `df` is daily and today's bar is absent, fetch today from Angel
    (daily endpoint first, then 5m aggregated) and append. Silent no-op on
    failure or weekends."""
    try:
        today = datetime.date.today()
        if today.weekday() >= 5:
            return df
        today_ts = pd.Timestamp(today).normalize()
        if df is not None and not df.empty:
            last_ts = pd.Timestamp(df.index[-1]).normalize()
            if last_ts >= today_ts:
                return df
        from angel_client import angel_download, _load_env, _get_credentials
        _load_env()
        if not _get_credentials():
            return df
        td = angel_download(symbol, today, today, interval="1d")
        new_row = None
        if td is not None and not td.empty:
            new_row = td.iloc[-1:].copy()
        else:
            intra = angel_download(symbol, today, today, interval="5m")
            if intra is not None and not intra.empty:
                new_row = pd.DataFrame({
                    "Open":   [float(intra["Open"].iloc[0])],
                    "High":   [float(intra["High"].max())],
                    "Low":    [float(intra["Low"].min())],
                    "Close":  [float(intra["Close"].iloc[-1])],
                    "Volume": [int(intra["Volume"].sum())],
                }, index=[today_ts])
        if new_row is None or new_row.empty:
            return df
        new_row.index = pd.to_datetime(new_row.index).normalize()
        if df is None or df.empty:
            return new_row
        df = df.copy()
        df.index = pd.to_datetime(df.index).normalize()
        return pd.concat([df[df.index < new_row.index[0]], new_row])
    except Exception:
        return df


def _is_index_symbol(symbol):
    """True for symbols that denote an index rather than a tradeable
    instrument: ^-prefixed tickers (^NSEI, ^BSESN, ...), NSE index names
    (NIFTY FMCG, SENSEX, ...), and any AMXIDX instrument from the scrip
    master. RS-benchmark names in _INDEX_YF_MAP are exempt — the Relative
    Strength indicator fetches those."""
    s = symbol.upper()
    if s in _INDEX_YF_MAP:
        return False
    return (s.startswith("^")
            or s.startswith("NIFTY ")
            or s in ("SENSEX", "BANKEX")
            or s in _index_names_cache)


@app.route("/api/historical")
def api_historical():
    """Fetch historical OHLCV candles.
    Params: symbol, interval (1m,5m,10m,15m,30m,1h,1d), days (lookback)
    """
    symbol = request.args.get("symbol", "RELIANCE").strip()
    interval = request.args.get("interval", "1d").strip()
    days = int(request.args.get("days", "90"))

    # Custom equal-weight indices (CIDX:<name>) are daily+ only: there are no
    # time-aligned intraday bars across constituents to synthesize from.
    if symbol.upper().startswith("CIDX:") and interval in (
            "1m", "5m", "10m", "15m", "30m", "1h"):
        return jsonify({"candles": []})

    # Direct index charting removed: ^-tickers and non-benchmark index names
    # resolve to nothing, same as an unknown symbol. RS-benchmark names
    # (_INDEX_YF_MAP) still resolve — the Relative Strength indicator needs
    # their closes. CIDX: names match none of these, so they pass through.
    if _is_index_symbol(symbol):
        return jsonify({"candles": []})

    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)

    # LRU cache: collapses concurrent reloads / multi-pane fan-out into one
    # upstream call. Hits are pre-serialized (and pre-gzipped) at insert time.
    _cache_key = (symbol.upper(), interval, days, str(end))
    _cached = _candle_cache_get(_cache_key)
    if _cached is not None:
        body, gz = _cached
        if gz and "gzip" in (request.headers.get("Accept-Encoding") or "").lower():
            resp = app.response_class(gz, mimetype="application/json")
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Vary"] = "Accept-Encoding"
            return resp
        return app.response_class(body, mimetype="application/json")

    # Check if symbol is a named index — map to yfinance ticker
    yf_index = _INDEX_YF_MAP.get(symbol.upper())

    # Map weekly/monthly intervals to daily data that we'll resample
    resample_map = {'1w': 'W', '1mo': 'ME', '6mo': '6ME', '12mo': '12ME'}
    resample_rule = resample_map.get(interval)
    fetch_interval = '1d' if resample_rule else interval

    try:
        if symbol.upper().startswith("CIDX:"):
            # Custom equal-weight index: synthesize daily OHLCV from members;
            # resample below handles weekly/monthly just like a real symbol.
            # Cached + concurrency-capped so a burst of cold CIDX loads can't
            # starve the worker pool.
            df = _build_custom_index_cached(symbol[5:], start, end)
        elif yf_index:
            # Named index: try Angel first (^NSEI/^CRSLDX/^NSEBANK/^BSESN have
            # tokens in INDEX_OVERRIDES); fall back to yfinance otherwise.
            df = None
            try:
                from angel_client import angel_download, _load_env, _get_credentials
                _load_env()
                if _get_credentials():
                    df = angel_download(yf_index, start, end, interval=fetch_interval)
            except Exception:
                df = None
            if df is None or df.empty:
                import yfinance as yf
                df = yf.download(yf_index, start=str(start), end=str(end),
                                 interval=fetch_interval, progress=False)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
        elif fetch_interval not in ('1d',):
            from angel_client import angel_download, _load_env, _get_credentials
            _load_env()
            if _get_credentials():
                df = angel_download(symbol, start, end, interval=fetch_interval)
            else:
                # Fallback: yfinance for intraday
                import yfinance as yf
                yf_ticker = symbol + ".NS" if not symbol.endswith((".NS", ".BO")) and not symbol.startswith("^") else symbol
                df = yf.download(yf_ticker, start=str(start), end=str(end),
                                 interval=fetch_interval, progress=False)
                if isinstance(df.columns, __import__('pandas').MultiIndex):
                    df.columns = df.columns.get_level_values(0)
        else:
            # Daily: prefer Angel (one call returns full history + today's bar
            # after market close). Fall back to yfinance only if Angel fails.
            df = None
            try:
                from angel_client import angel_download, _load_env, _get_credentials
                _load_env()
                if _get_credentials():
                    df = angel_download(symbol, start, end, interval="1d")
            except Exception:
                df = None
            if df is None or df.empty:
                from data_provider import download
                df = download(symbol, start=start, end=end, interval="1d")
                # yfinance lags one day; merge today's bar from Angel intraday
                df = _merge_today_candle(df, symbol)

        # Indices: Angel returns Volume=0; RS only consumes closes, so the
        # zero volume on benchmark candles is irrelevant.

        # Resample daily data into weekly/monthly candles
        if resample_rule and df is not None and not df.empty:
            df = df.resample(resample_rule).agg({
                'Open': 'first', 'High': 'max', 'Low': 'min',
                'Close': 'last', 'Volume': 'sum'
            }).dropna(subset=['Open'])
    except Exception as e:
        return jsonify({"error": str(e), "candles": []})

    if df is None or df.empty:
        return jsonify({"candles": []})

    # Filter ghost candles: remove weekends (data source date errors)
    if fetch_interval == '1d' and not resample_rule:
        idx = pd.to_datetime(df.index)
        df = df[idx.dayofweek < 5]
        if df.empty:
            return jsonify({"candles": []})

    # Convert to lightweight-charts format. Column-array fast path (replaces
    # iterrows — no per-row Series construction). Output is byte-identical to
    # the old loop: missing columns default to 0, rows whose values can't
    # coerce (e.g. NaN volume) are skipped, daily bars keep date strings
    # (avoids tz off-by-one), intraday keeps naive-local epoch seconds via
    # pd.Timestamp.timestamp().
    is_daily = fetch_interval == '1d' or resample_rule
    o = df["Open"].to_numpy() if "Open" in df.columns else None
    h = df["High"].to_numpy() if "High" in df.columns else None
    lo = df["Low"].to_numpy() if "Low" in df.columns else None
    c = df["Close"].to_numpy() if "Close" in df.columns else None
    v = df["Volume"].to_numpy() if "Volume" in df.columns else None
    if is_daily:
        times = pd.DatetimeIndex(df.index).strftime("%Y-%m-%d").tolist()
    else:
        times = []
        for ts in df.index:
            try:
                times.append(int(pd.Timestamp(ts).timestamp()))
            except (ValueError, TypeError, OverflowError):
                times.append(None)
    candles = []
    for i in range(len(df)):
        t = times[i]
        if t is None or t != t:  # skip NaT-derived times (NaN != NaN)
            continue
        try:
            vol = v[i] if v is not None else 0
            candles.append({
                "time": t,
                "open": round(float(o[i]) if o is not None else 0.0, 2),
                "high": round(float(h[i]) if h is not None else 0.0, 2),
                "low": round(float(lo[i]) if lo is not None else 0.0, 2),
                "close": round(float(c[i]) if c is not None else 0.0, 2),
                "volume": int(vol) if vol else 0,
            })
        except (ValueError, TypeError):
            continue

    candles_json = _candle_cache_put(_cache_key, candles)
    global _last_candle_fetch_ts
    _last_candle_fetch_ts = time.time()
    return app.response_class('{"candles":' + candles_json + ',"cached":false}',
                              mimetype="application/json")


@app.route("/api/quote")
def api_quote():
    """Get latest quote/LTP for a symbol. Used for live ticker updates."""
    symbol = request.args.get("symbol", "RELIANCE").strip()

    # Quotes serve tradeable instruments only (indices not chartable).
    if _is_index_symbol(symbol) or symbol.upper() in _INDEX_YF_MAP:
        return jsonify({"symbol": symbol, "ltp": 0, "change": 0, "error": "no data"})

    # Try Angel One LTP API first
    try:
        from angel_client import _ensure_session, _parse_ticker, _load_env, _get_credentials, _rate_limit_acquire, _call_with_timeout
        _load_env()
        if _get_credentials():
            exch, tok = _parse_ticker(symbol)
            if exch and tok:
                _rate_limit_acquire()
                obj = _ensure_session()
                ltp_data, _t = _call_with_timeout(
                    obj.ltpData, exch, symbol.replace("-EQ", ""), tok, timeout=6
                )
                if ltp_data and ltp_data.get("status") and ltp_data.get("data"):
                    d = ltp_data["data"]
                    return jsonify({
                        "symbol": symbol,
                        "ltp": float(d.get("ltp", 0)),
                        "open": float(d.get("open", 0)),
                        "high": float(d.get("high", 0)),
                        "low": float(d.get("low", 0)),
                        "close": float(d.get("close", 0)),
                        "change": float(d.get("ltp", 0)) - float(d.get("close", 0)),
                    })
    except Exception:
        pass

    # Fallback: use last candle from historical data
    try:
        from data_provider import download
        end = datetime.date.today()
        start = end - datetime.timedelta(days=5)
        df = download(symbol, start=start, end=end, interval="1d")
        if df is not None and not df.empty:
            last = df.iloc[-1]
            prev_close = float(df.iloc[-2]["Close"]) if len(df) > 1 else float(last["Open"])
            return jsonify({
                "symbol": symbol,
                "ltp": round(float(last["Close"]), 2),
                "open": round(float(last["Open"]), 2),
                "high": round(float(last["High"]), 2),
                "low": round(float(last["Low"]), 2),
                "close": round(prev_close, 2),
                "change": round(float(last["Close"]) - prev_close, 2),
            })
    except Exception:
        pass

    return jsonify({"symbol": symbol, "ltp": 0, "change": 0, "error": "no data"})


@app.route("/api/quotes")
def api_quotes():
    """Batched quotes: one Angel getMarketData call covers up to 50 symbols
    (vs one ltpData round-trip per symbol via /api/quote, which stays for
    back-compat). Symbols already streaming over the Angel WS are answered
    from _LAST_TICKS for free. Returns {"quotes": {SYM: {...}}}; symbols that
    can't be resolved are simply absent — same contract as /api/quote failing.
    """
    symbols = _validate_symbols(request.args.get("symbols", ""))
    if not symbols:
        return jsonify({"quotes": {}})
    out = {}
    remaining = []
    with _LAST_TICKS_LOCK:
        for sym in symbols:
            t = _LAST_TICKS.get(sym)
            if t and t.get("ltp"):
                out[sym] = {
                    "symbol": sym,
                    "ltp": t.get("ltp", 0),
                    "open": t.get("open", 0),
                    "high": t.get("high", 0),
                    "low": t.get("low", 0),
                    "close": t.get("close", 0),
                    "change": t.get("change", 0),
                }
            else:
                remaining.append(sym)
    if not remaining:
        return jsonify({"quotes": out})
    try:
        from angel_client import (_ensure_session, _parse_ticker, _load_env,
                                  _get_credentials, _call_with_timeout,
                                  _rate_limiter)
        _load_env()
        if not _get_credentials():
            return jsonify({"quotes": out})
        by_exch = {}   # exch -> [token, ...]
        tok2sym = {}   # (exch, token) -> requested symbol
        for sym in remaining:
            try:
                exch, tok = _parse_ticker(sym)
            except Exception:
                continue
            if exch and tok:
                by_exch.setdefault(exch, []).append(str(tok))
                tok2sym[(exch, str(tok))] = sym
        if not tok2sym:
            return jsonify({"quotes": out})
        obj = _ensure_session()
        first = True
        for exch, tokens in by_exch.items():
            for i in range(0, len(tokens), 50):   # Angel caps 50 tokens/call
                if not first:
                    time.sleep(0.35)  # getMarketData ~1 req/s rate limit
                first = False
                chunk = tokens[i:i + 50]
                _rate_limiter.acquire()
                try:
                    md, timed_out = _call_with_timeout(
                        obj.getMarketData, "OHLC", {exch: chunk}, timeout=8)
                finally:
                    _rate_limiter.release()
                if timed_out or not md or not md.get("status"):
                    continue
                _rate_limiter.report_success()
                for item in (md.get("data") or {}).get("fetched") or []:
                    sym = tok2sym.get((item.get("exchange", exch),
                                       str(item.get("symbolToken", ""))))
                    if not sym:
                        continue
                    ltp = float(item.get("ltp", 0) or 0)
                    close = float(item.get("close", 0) or 0)
                    out[sym] = {
                        "symbol": sym,
                        "ltp": ltp,
                        "open": float(item.get("open", 0) or 0),
                        "high": float(item.get("high", 0) or 0),
                        "low": float(item.get("low", 0) or 0),
                        "close": close,
                        "change": ltp - close,
                    }
    except Exception:
        pass  # partial result is still useful; client has a per-symbol fallback
    return jsonify({"quotes": out})


@app.route("/api/health")
def api_health():
    """Lightweight liveness/observability endpoint. Used by the UI to render
    a status dot and decide whether the data plane is healthy."""
    try:
        from angel_client import _smart_api  # noqa: WPS433  (introspection)
        angel_session = _smart_api is not None
    except Exception:
        angel_session = False
    with _candle_cache_lock:
        cache_size = len(_candle_cache)
        hits = _candle_cache_hits
        misses = _candle_cache_misses
    return jsonify({
        "ok": True,
        "uptime_sec": int(time.time() - _BOOT_TS),
        "angel_session": angel_session,
        "angel_ws_connected": bool(_ws_connected),
        "last_candle_fetch_ago_sec": (
            int(time.time() - _last_candle_fetch_ts) if _last_candle_fetch_ts else None
        ),
        "subscribed_tokens": len(_subscribed_tokens),
        "candle_cache": {"size": cache_size, "hits": hits, "misses": misses,
                          "hit_rate": (hits / (hits + misses)) if (hits + misses) else 0.0},
    })


# ─── Angel WebSocket Streaming ────────────────────────────────────────────────

_angel_ws = None
_angel_ws_lock = threading.Lock()
_angel_ws_started_at = 0.0  # time.time() when current sws was assigned
_subscribed_tokens = {}  # { "NSE|2885": {"symbol": "RELIANCE", "exch": "NSE", "token": "2885"} }
_prev_close_cache = {}   # { "RELIANCE": prev_close_price }
_ws_reconnect_event = threading.Event()  # set by on_close to wake the watchdog

# Exchange type mapping for Angel WebSocket
_EXCH_TYPE_MAP = {"NSE": 1, "BSE": 3, "NFO": 2, "MCX": 5}


def _get_angel_ws_creds():
    """Get credentials needed for WebSocket: auth_token, api_key, client_code, feed_token."""
    try:
        from angel_client import _ensure_session, _load_env, _get_credentials
        _load_env()
        creds = _get_credentials()
        if not creds:
            return None
        obj = _ensure_session()
        auth_token = obj.access_token
        api_key = creds.get("ANGEL_API_KEY", "")
        client_code = creds.get("ANGEL_CLIENT_CODE", "")
        feed_token = obj.feed_token or (obj.getfeedToken() if hasattr(obj, 'getfeedToken') else None)
        if not (auth_token and api_key and client_code and feed_token):
            return None
        return auth_token, api_key, client_code, feed_token
    except Exception as e:
        print(f"[WS] Failed to get Angel WS creds: {e}")
        return None


def _start_angel_ws():
    """Start Angel One WebSocket connection in background thread."""
    global _angel_ws
    creds = _get_angel_ws_creds()
    if not creds:
        print("[WS] Angel WebSocket credentials not available. Using REST fallback.")
        return False

    auth_token, api_key, client_code, feed_token = creds
    try:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        sws = SmartWebSocketV2(auth_token, api_key, client_code, feed_token)

        def on_data(wsapp, message):
            """Called when tick data received from Angel."""
            try:
                if not message or not isinstance(message, dict):
                    return
                token = str(message.get("token", ""))
                exch_type = message.get("exchange_type", 1)
                exch = "NSE" if exch_type == 1 else "BSE" if exch_type == 3 else "NFO"
                key = f"{exch}|{token}"
                info = _subscribed_tokens.get(key)
                if not info:
                    return
                symbol = info["symbol"]
                ltp = message.get("last_traded_price", 0)
                if isinstance(ltp, (int, float)):
                    ltp = ltp / 100.0  # Angel sends in paise
                open_p = (message.get("open_price_of_the_day", 0) or 0) / 100.0
                high_p = (message.get("high_price_of_the_day", 0) or 0) / 100.0
                low_p = (message.get("low_price_of_the_day", 0) or 0) / 100.0
                close_p = (message.get("closed_price", 0) or 0) / 100.0
                volume = message.get("volume_trade_for_the_day", 0) or 0

                if close_p > 0:
                    _prev_close_cache[symbol] = close_p

                prev_close = _prev_close_cache.get(symbol, close_p or ltp)
                change = ltp - prev_close if prev_close else 0

                tick = {
                    "symbol": symbol,
                    "ltp": round(ltp, 2),
                    "open": round(open_p, 2),
                    "high": round(high_p, 2),
                    "low": round(low_p, 2),
                    "close": round(prev_close, 2),
                    "change": round(change, 2),
                    "volume": volume,
                    "ts": time.time(),
                }
                with _LAST_TICKS_LOCK:
                    _LAST_TICKS[symbol] = tick
            except Exception:
                pass  # silent fail on malformed tick

        def on_open(wsapp):
            global _ws_connected
            _ws_connected = True
            print("[WS] Angel WebSocket connected.")
            # Re-subscribe any pending tokens
            _resubscribe_all()

        def on_error(wsapp, error):
            print(f"[WS] Angel WebSocket error: {error}")

        def on_close(wsapp):
            global _angel_ws, _ws_connected
            _ws_connected = False
            _angel_ws = None
            # Hand reconnect off to the watchdog so we don't recurse from
            # inside the SDK's callback thread.
            _ws_reconnect_event.set()
            print("[WS] Angel WebSocket closed; reconnect scheduled.")

        sws.on_data = on_data
        sws.on_open = on_open
        sws.on_error = on_error
        sws.on_close = on_close

        _angel_ws = sws
        global _angel_ws_started_at
        _angel_ws_started_at = time.time()

        ws_thread = threading.Thread(target=sws.connect, daemon=True)
        ws_thread.start()
        print("[WS] Angel WebSocket thread started.")
        return True
    except Exception as e:
        print(f"[WS] Failed to start Angel WebSocket: {e}")
        return False


def _is_market_hours():
    """Indian equities cash market: Mon-Fri, 09:15-15:30 IST (UTC+05:30)."""
    now_utc = datetime.datetime.utcnow()
    ist = now_utc + datetime.timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:  # Sat/Sun
        return False
    minutes = ist.hour * 60 + ist.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


def _ws_reconnect_watchdog():
    """Daemon thread: reconnects Angel WS with market-hours-aware backoff.

    Wakes up either every poll_interval (default 30s when idle and connected)
    or immediately when on_close fires _ws_reconnect_event. Reconnect cadence:

      - Market hours:  1s, 2s, 5s, 10s, 30s (capped) between attempts
      - Off-hours:     5 minutes between attempts (no point hammering)
    """
    market_backoff = [1, 2, 5, 10, 30]
    off_hours_wait = 300  # 5 min
    stuck_timeout = 60    # if sws assigned but on_open never fires within this, force reconnect
    attempt = 0
    while True:
        # Wait until either an explicit reconnect signal or a periodic check
        triggered = _ws_reconnect_event.wait(timeout=30)
        _ws_reconnect_event.clear()
        if _ws_connected:
            attempt = 0  # healthy; reset backoff
            continue
        # If a sws is assigned but on_open hasn't fired yet, give it some time
        # before piling on a second connection attempt.
        if _angel_ws is not None and not triggered:
            if time.time() - _angel_ws_started_at < stuck_timeout:
                continue
            # Stuck: drop the dead handle so _start_angel_ws can replace it.
            print("[WS] connect attempt stuck > 60s; forcing reconnect.")
        if not _is_market_hours():
            # Reconnect once per off-hours window so we're warm at 09:15 IST,
            # but don't burn CPU/log noise hammering Angel.
            ok = _start_angel_ws()
            if not ok:
                # sleep in small chunks so we still react to a manual signal
                slept = 0
                while slept < off_hours_wait:
                    if _ws_reconnect_event.wait(timeout=10):
                        _ws_reconnect_event.clear()
                        break
                    slept += 10
            continue
        # Market hours: aggressive backoff.
        wait_s = market_backoff[min(attempt, len(market_backoff) - 1)]
        attempt += 1
        time.sleep(wait_s)
        ok = _start_angel_ws()
        if ok:
            attempt = 0


def _resubscribe_all():
    """Re-subscribe all currently tracked tokens after reconnect."""
    if not _angel_ws or not _subscribed_tokens:
        return
    # Group by exchange type
    by_exch = {}
    for key, info in _subscribed_tokens.items():
        exch = info["exch"]
        exch_type = _EXCH_TYPE_MAP.get(exch, 1)
        by_exch.setdefault(exch_type, []).append(info["token"])

    token_list = [{"exchangeType": et, "tokens": toks} for et, toks in by_exch.items()]
    try:
        _angel_ws.subscribe("chart_sub", 2, token_list)  # mode 2 = QUOTE
    except Exception as e:
        print(f"[WS] Resubscribe failed: {e}")


def _subscribe_symbol(symbol):
    """Subscribe to a symbol's live feed via Angel WebSocket."""
    try:
        from angel_client import _parse_ticker, _load_env, _get_credentials
        _load_env()
        if not _get_credentials():
            return
        exch, token = _parse_ticker(symbol)
        if not exch or not token:
            return
        key = f"{exch}|{token}"
        if key in _subscribed_tokens:
            return  # already subscribed
        _subscribed_tokens[key] = {"symbol": symbol, "exch": exch, "token": token}
        if _angel_ws:
            exch_type = _EXCH_TYPE_MAP.get(exch, 1)
            try:
                _angel_ws.subscribe("sub_" + symbol, 2, [{"exchangeType": exch_type, "tokens": [token]}])
            except Exception as e:
                print(f"[WS] Subscribe {symbol} failed: {e}")
    except Exception as e:
        print(f"[WS] _subscribe_symbol error: {e}")


def _unsubscribe_symbol(symbol):
    """Unsubscribe a symbol from the live feed."""
    try:
        from angel_client import _parse_ticker
        exch, token = _parse_ticker(symbol)
        if not exch or not token:
            return
        key = f"{exch}|{token}"
        if key not in _subscribed_tokens:
            return
        del _subscribed_tokens[key]
        if _angel_ws:
            exch_type = _EXCH_TYPE_MAP.get(exch, 1)
            try:
                _angel_ws.unsubscribe("unsub_" + symbol, 2, [{"exchangeType": exch_type, "tokens": [token]}])
            except Exception:
                pass
    except Exception:
        pass


# ─── Browser subscribe/unsubscribe (REST replaces socket.io events) ─────────
VALID_SYMBOL_RE = __import__("re").compile(r"^[\^A-Z0-9._\-]{1,30}$")

def _validate_symbols(raw):
    """Allowlist filter: only short uppercase tickers (with optional ^/./_/-).
    Rejects anything that could be a path-traversal or injection vector.
    Index symbols are dropped too — quotes/ticks serve tradeable
    instruments only."""
    if isinstance(raw, str):
        items = [s.strip().upper() for s in raw.split(",")]
    elif isinstance(raw, list):
        items = [str(s).strip().upper() for s in raw]
    else:
        return []
    return [s for s in items
            if s and VALID_SYMBOL_RE.match(s) and not _is_index_symbol(s)
            and s not in _INDEX_YF_MAP]


@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    """Browser asks server to start streaming live ticks for these symbols
    via the Angel WS thread. Client then polls /api/ticks."""
    payload = request.get_json(silent=True) or {}
    symbols = _validate_symbols(payload.get("symbols", []))
    for sym in symbols:
        _subscribe_symbol(sym)
    return jsonify({"ok": True, "subscribed": symbols})


@app.route("/api/unsubscribe", methods=["POST"])
def api_unsubscribe():
    payload = request.get_json(silent=True) or {}
    symbols = _validate_symbols(payload.get("symbols", []))
    for sym in symbols:
        _unsubscribe_symbol(sym)
    return jsonify({"ok": True, "unsubscribed": symbols})


@app.route("/api/ticks")
def api_ticks():
    """Batched live-tick read. Returns last known tick per requested symbol
    (populated by the Angel WS thread). Client polls this once per second
    in place of the old socket.io 'tick' event stream."""
    symbols = _validate_symbols(request.args.get("symbols", ""))
    if not symbols:
        return jsonify({"ticks": {}})
    out = {}
    with _LAST_TICKS_LOCK:
        for sym in symbols:
            t = _LAST_TICKS.get(sym)
            if t is not None:
                out[sym] = t
    return jsonify({"ticks": out, "server_ts": time.time()})


# ─── Main ─────────────────────────────────────────────────────────────────────

FIXED_PORT = 5050


def _ensure_port_free(port):
    """Cross-platform port probe: try to bind once. If something else holds
    the port, refuse to start with a clear message (no platform-specific
    auto-kill via lsof/ps — keeps tradingcharts portable to Linux/Windows)."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
    except OSError as e:
        print(f"[port] Port {port} is already in use ({e}). Free it and retry.")
        print(f"[port]   macOS/Linux: lsof -ti:{port} | xargs kill -9")
        print(f"[port]   Windows:    netstat -ano | findstr :{port}  then  taskkill /F /PID <pid>")
        sys.exit(1)
    finally:
        s.close()


def _prewarm_caches():
    """Background warm-up: parse the ~33MB Angel scrip master once at boot so
    the first /api/search and first subscribe don't pay ~1-2s on the request
    path. Failures are silent — lazy loading still works exactly as before."""
    try:
        _get_symbol_list()
    except Exception:
        pass
    try:
        from angel_client import _load_scrip_master
        _load_scrip_master()
    except Exception:
        pass


def _prewarm_custom_indices():
    """Gentle background warm-up of custom (CIDX) baskets so the first chart load
    of a theme hits cache instead of a cold multi-stock fan-fetch. Runs well
    after boot, one basket at a time (and through _CIDX_BUILD_SEM), throttled —
    so it never competes with Angel session init or the first page paint.
    Only the default 1D window (365d) is warmed; weekly/monthly build on demand
    (still protected by the semaphore + cache)."""
    try:
        time.sleep(15.0)                     # let boot / WS / first paint settle
        end = datetime.date.today()
        start = end - datetime.timedelta(days=365)   # default 1D pane lookback
        for nm in list(_load_custom_index_defs().keys()):
            try:
                _build_custom_index_cached(nm, start, end)
            except Exception:
                pass
            time.sleep(2.0)                  # throttle: ~one basket every 2s
    except Exception:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 0)) or FIXED_PORT
    _ensure_port_free(port)
    url = f"http://localhost:{port}"
    print("=" * 60)
    print("  Trading Charts Dashboard")
    print(f"  {url}")
    print("=" * 60)
    import webbrowser
    # Open the browser slightly after serve() starts listening, so the first
    # page load never races the server (was: open before serve → occasional
    # connection refused on slow boots).
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    # Warm symbol/scrip-master caches off the request path.
    threading.Thread(target=_prewarm_caches, daemon=True).start()
    # Gently pre-warm custom (CIDX) index baskets off the request path.
    threading.Thread(target=_prewarm_custom_indices, daemon=True).start()
    # Start Angel WebSocket in background (non-blocking) — it populates
    # _LAST_TICKS, which the browser pulls via /api/ticks every second.
    threading.Thread(target=_start_angel_ws, daemon=True).start()
    # Watchdog: reconnects WS with market-hours-aware backoff (1/2/5/10/30s
    # during market hours, 5min off-hours). Iterative, no recursion.
    threading.Thread(target=_ws_reconnect_watchdog, daemon=True).start()
    # Production WSGI server (waitress): threaded, no FD leak, no eventlet,
    # cross-platform (Windows/macOS/Linux). Replaces Werkzeug dev server.
    # threads=16 so a reload doesn't head-of-line block on prior page's
    # in-flight /api/historical fetches still occupying worker threads.
    from waitress import serve
    serve(app, host="127.0.0.1", port=port, threads=16, ident="tradingcharts")
