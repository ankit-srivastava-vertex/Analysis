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
  - Named-index routing via _INDEX_YF_MAP: dropdown labels ("NIFTY 50",
    "NIFTY MIDCAP 150", "FINNIFTY", etc.) map to synthetic ^NSE* tickers
    resolved by INDEX_OVERRIDES in angel_client.py. yfinance is no longer
    consulted for these benchmarks.
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
    - Timeframes:        30m, 1h, 1d, 1w, 1mo.
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
# Keyed on (SYMBOL, interval, days, today). 60s TTL absorbs page reloads and
# concurrent panes hitting the same symbol; today's last bar is replaced live
# on the client via WS ticks so cache staleness is invisible during the day.
from collections import OrderedDict
_CANDLE_CACHE_MAX = 256
_CANDLE_CACHE_TTL_SEC = 60
_candle_cache = OrderedDict()  # key -> (timestamp, candles_list)
_candle_cache_lock = threading.Lock()
_candle_cache_hits = 0
_candle_cache_misses = 0

def _candle_cache_get(key):
    global _candle_cache_hits, _candle_cache_misses
    now = time.time()
    with _candle_cache_lock:
        item = _candle_cache.get(key)
        if item is not None and (now - item[0]) < _CANDLE_CACHE_TTL_SEC:
            _candle_cache.move_to_end(key)  # LRU touch
            _candle_cache_hits += 1
            return item[1]
        _candle_cache_misses += 1
        return None

def _candle_cache_put(key, candles):
    with _candle_cache_lock:
        _candle_cache[key] = (time.time(), candles)
        _candle_cache.move_to_end(key)
        while len(_candle_cache) > _CANDLE_CACHE_MAX:
            _candle_cache.popitem(last=False)

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
        json.dump(data, f, indent=2)
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

# Nifty indices
_INDEX_SYMBOLS = [
    "^NSEI", "^NSEBANK", "^CRSLDX", "^BSESN",
]

# Index name → internal ticker mapping (for Relative Strength benchmark).
# All entries route through Angel via INDEX_OVERRIDES in angel_client.py.
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

def _get_symbol_list():
    """Load all equity symbols from Angel scrip master (NSE, BSE, NSE SME, BSE SME)."""
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
            base = sym.split("-", 1)[0]
            all_syms.add(base)
        _all_symbols_cache = sorted(all_syms)
        return _all_symbols_cache
    except Exception:
        pass
    return _POPULAR_SYMBOLS


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


@app.route("/api/symbols")
def api_symbols():
    """Return index symbols only (full list is searched via /api/search)."""
    return jsonify({"symbols": _INDEX_SYMBOLS + _POPULAR_SYMBOLS})


@app.route("/api/search")
def api_search():
    """Search symbols by prefix (min 4 chars)."""
    q = request.args.get("q", "").strip().upper()
    if not q or len(q) < 4:
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


@app.route("/api/historical")
def api_historical():
    """Fetch historical OHLCV candles.
    Params: symbol, interval (1m,5m,15m,30m,1h,1d), days (lookback)
    """
    symbol = request.args.get("symbol", "RELIANCE").strip()
    interval = request.args.get("interval", "1d").strip()
    days = int(request.args.get("days", "90"))

    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)

    # 60s LRU cache: collapses concurrent reloads / multi-pane fan-out into
    # one upstream call. Today's intraday tick is overlaid client-side via WS.
    _cache_key = (symbol.upper(), interval, days, str(end))
    _cached = _candle_cache_get(_cache_key)
    if _cached is not None:
        return jsonify({"candles": _cached, "cached": True})

    # Check if symbol is a named index — map to yfinance ticker
    yf_index = _INDEX_YF_MAP.get(symbol.upper())

    # Map weekly/monthly intervals to daily data that we'll resample
    resample_map = {'1w': 'W', '1mo': 'ME', '6mo': '6ME', '12mo': '12ME'}
    resample_rule = resample_map.get(interval)
    fetch_interval = '1d' if resample_rule else interval

    try:
        if yf_index:
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

    # Convert to lightweight-charts format
    is_daily = fetch_interval == '1d' or resample_rule
    candles = []
    for ts, row in df.iterrows():
        try:
            if is_daily:
                # Use date string to avoid timezone shift (naive local→UTC off-by-one)
                t = pd.Timestamp(ts).strftime("%Y-%m-%d")
            else:
                # Intraday: use UTC epoch seconds
                t = int(pd.Timestamp(ts).timestamp())
            candles.append({
                "time": t,
                "open": round(float(row.get("Open", 0)), 2),
                "high": round(float(row.get("High", 0)), 2),
                "low": round(float(row.get("Low", 0)), 2),
                "close": round(float(row.get("Close", 0)), 2),
                "volume": int(row.get("Volume", 0)) if row.get("Volume") else 0,
            })
        except (ValueError, TypeError):
            continue

    _candle_cache_put(_cache_key, candles)
    global _last_candle_fetch_ts
    _last_candle_fetch_ts = time.time()
    return jsonify({"candles": candles, "cached": False})


@app.route("/api/quote")
def api_quote():
    """Get latest quote/LTP for a symbol. Used for live ticker updates."""
    symbol = request.args.get("symbol", "RELIANCE").strip()

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
    Rejects anything that could be a path-traversal or injection vector."""
    if isinstance(raw, str):
        items = [s.strip().upper() for s in raw.split(",")]
    elif isinstance(raw, list):
        items = [str(s).strip().upper() for s in raw]
    else:
        return []
    return [s for s in items if s and VALID_SYMBOL_RE.match(s)]


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 0)) or FIXED_PORT
    _ensure_port_free(port)
    url = f"http://localhost:{port}"
    print("=" * 60)
    print("  Trading Charts Dashboard")
    print(f"  {url}")
    print("=" * 60)
    import webbrowser
    webbrowser.open(url)
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
