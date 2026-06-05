"""
angel_client.py — yfinance-shaped adapter for Angel One SmartAPI (SDK)
=====================================================================

Free Indian-market historical OHLCV via the official smartapi-python SDK.
Thread-safe; designed to serve concurrent chart panes without ever tripping
Angel's rate limit.

Public surface:
  - angel_download(ticker, start, end, interval="1d") -> pd.DataFrame
        DataFrame[Open,High,Low,Close,Volume] indexed by Timestamp.
  - angel_download_many(tickers, start, end, max_workers=RATE_LIMIT_PER_SEC) -> dict
        Bulk fetch, rate-limit safe.
  - get_angel_session() -> (api_key, jwt_token)  (lazy, auto-relogin)
  - refresh_token(force=False) -> bool
  - INDEX_OVERRIDES: dict of synthetic index tickers → (exch, token, name)
        Includes Nifty 50/100/500, Bank, Midcap 100/150, Smallcap 100/250,
        MidSmall 400, FinNifty, Sensex.

Concurrency / robustness:
  - _init_lock (RLock) serializes session bootstrap + scrip-master parse so
    parallel callers never fire two logins or two master-loads.
  - _RateLimiter: adaptive sliding-window limiter with dual windows
    (per-second + per-minute), an in-flight semaphore, jittered backoff,
    and self-healing throttle/recover on AB1004 errors.
      * Halves effective rate on rate-limit hit; restores it gradually
        after sustained successful calls.
      * Sleeps outside the internal lock to avoid stacking thread waits.
      * One-line log on state transitions only; silent during steady state.

.env keys required:
  ANGEL_API_KEY=...
  ANGEL_CLIENT_CODE=...
  ANGEL_PIN=...
  ANGEL_TOTP_SECRET=...

References:
  - https://smartapi.angelbroking.com/docs/Historical
  - https://smartapi.angelbroking.com/docs/User
  - pip install smartapi-python pyotp
"""

import os
import sys
import json
import time
import random
import threading
import datetime
import warnings
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import pandas as pd

warnings.filterwarnings("ignore")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(SCRIPT_DIR, ".env")

SCRIP_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)
SCRIP_MASTER_CACHE = os.path.join(SCRIPT_DIR, ".angel_scrip_master.json")
SCRIP_MASTER_TTL_DAYS = 7

# Adaptive limiter tuned for Angel's documented caps:
#   getCandleData: 3 req/sec, 180 req/min.
# RATE_LIMIT_PER_SEC stays exported for back-compat (angel_download_many uses it
# as max_workers default and for ETA print).
RATE_LIMIT_PER_SEC = 3
_RATE_LIMIT_PER_MIN = 180
_RATE_LIMIT_MAX_INFLIGHT = 4

# Single lock guarding session bootstrap (scrip master load + SmartConnect login).
# Concurrent callers (e.g. 4 chart panes hitting /api/historical at the same time
# plus the WS bootstrap thread) all serialize through this so we never fire two
# parallel logins or scrip-master parses, which trips Angel's per-second cap.
_init_lock = threading.RLock()

_smart_api = None   # SmartConnect instance (None until logged in)
_api_key_cache = ""  # cached for get_angel_session() return value
_refresh_token_cache = None  # stored from generateSession for renewAccessToken
_master_df: Optional[pd.DataFrame] = None
_symbol_index: Optional[dict] = None  # (exch, symbol_upper) -> token

# Wall-clock watchdog for outbound Angel HTTPS calls. Without this the SDK's
# urllib3 pool can hand back a stale TCP socket and the SDK call blocks
# forever on the next read. Wrapping each call with a future + hard timeout
# guarantees the handler returns. On timeout we drop _smart_api so the next
# attempt builds a fresh SmartConnect (and a fresh connection pool).
import concurrent.futures as _cf
_call_executor = _cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="angel-call")
ANGEL_CALL_TIMEOUT_SEC = float(os.environ.get("ANGEL_CALL_TIMEOUT", "10"))

def _call_with_timeout(fn, *args, timeout=None, **kwargs):
    """Run fn(*args, **kwargs) with a hard wall-clock timeout.
    Returns (result, timed_out). The runaway thread is left to finish in the
    background (executor caps concurrency at 4)."""
    fut = _call_executor.submit(fn, *args, **kwargs)
    try:
        return fut.result(timeout=timeout or ANGEL_CALL_TIMEOUT_SEC), False
    except _cf.TimeoutError:
        return None, True

def _reset_session():
    """Drop the current SmartConnect so the next call rebuilds it with a
    fresh HTTP connection pool. Used to recover from stale sockets."""
    global _smart_api
    with _init_lock:
        _smart_api = None


# ─────────────────────────── env / credentials ─────────────────────────────

def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH, override=True)
        return
    except ImportError:
        pass
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH) as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")


def _get_credentials():
    keys = {}
    for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE",
              "ANGEL_PIN", "ANGEL_TOTP_SECRET"):
        v = os.environ.get(k, "").strip()
        if not v or v.startswith("your_"):
            return None
        keys[k] = v
    return keys


# ─────────────────────────── SDK login ─────────────────────────────────────

def _sdk_login(creds: dict):
    """Login via official SmartConnect SDK. Returns (SmartConnect, api_key, refresh_token)."""
    try:
        import pyotp
    except ImportError as e:
        raise RuntimeError(
            "pyotp not installed. Run: python3 -m pip install pyotp") from e
    try:
        from SmartApi import SmartConnect
    except ImportError as e:
        missing = getattr(e, "name", None) or str(e)
        raise RuntimeError(
            "Failed to import SmartApi (missing module: %s). "
            "Run: python3 -m pip install smartapi-python logzero websocket-client"
            % missing) from e

    api_key = creds["ANGEL_API_KEY"]
    obj = SmartConnect(api_key=api_key)
    totp = pyotp.TOTP(creds["ANGEL_TOTP_SECRET"]).now()
    data, timed_out = _call_with_timeout(
        obj.generateSession, creds["ANGEL_CLIENT_CODE"], creds["ANGEL_PIN"], totp,
        timeout=12,
    )
    if timed_out:
        raise RuntimeError("Angel login timed out (network/upstream stalled)")
    if not data or not data.get("status"):
        msg = data.get("message", data) if data else "No response"
        raise RuntimeError("Login failed: %s" % msg)
    rt = (data.get("data") or {}).get("refreshToken")
    return obj, api_key, rt


def _try_refresh_access_token() -> bool:
    """Renew JWT via refresh token (no TOTP needed). Returns True on success."""
    global _smart_api, _refresh_token_cache
    if _smart_api is None or not _refresh_token_cache:
        return False
    try:
        new_data, timed_out = _call_with_timeout(
            _smart_api.renewAccessToken,
            {"refreshToken": _refresh_token_cache},
            timeout=8,
        )
        if timed_out:
            print("Token refresh timed out; will fall back to full login.")
            return False
        if new_data and new_data.get("status"):
            jwt = (new_data.get("data") or {}).get("jwtToken")
            if jwt:
                _smart_api.setAccessToken(jwt)
                _refresh_token_cache = (
                    (new_data.get("data") or {}).get("refreshToken")
                    or _refresh_token_cache
                )
                print("Angel token refreshed (no TOTP).")
                return True
    except Exception as e:
        print("Token refresh failed: %s" % e)
    return False


def refresh_token(force: bool = False) -> bool:
    """Re-establish session. Tries renewAccessToken first, then full TOTP login.
    Thread-safe: serializes all callers through _init_lock so we never fire
    parallel logins (which trips Angel's per-second rate limit)."""
    with _init_lock:
        return _refresh_token_locked(force)


def _refresh_token_locked(force: bool = False) -> bool:
    global _smart_api, _api_key_cache, _refresh_token_cache
    # Fast path: another thread already logged in while we waited on the lock.
    if _smart_api is not None and not force:
        return True
    # Fast path: renew with refresh token (no TOTP)
    if _try_refresh_access_token():
        return True
    # Slow path: full TOTP re-login
    _load_env()
    creds = _get_credentials()
    if not creds:
        if force:
            print("\n" + "=" * 70)
            print("  ANGEL ONE CREDENTIALS MISSING")
            print("=" * 70)
            print("  Required keys in %s :" % ENV_PATH)
            print("    ANGEL_API_KEY=...")
            print("    ANGEL_CLIENT_CODE=...   (e.g. R12345)")
            print("    ANGEL_PIN=...           (4-digit MPIN)")
            print("    ANGEL_TOTP_SECRET=...   (base32 from TOTP setup)")
            print("=" * 70)
            try:
                input("  Press Enter when .env is ready... ")
            except (KeyboardInterrupt, EOFError):
                return False
            _load_env()
            creds = _get_credentials()
        if not creds:
            return False
    try:
        obj, api_key, rt = _sdk_login(creds)
    except Exception as e:
        print("Angel login failed: %s" % e)
        return False
    _smart_api = obj
    _api_key_cache = api_key
    _refresh_token_cache = rt
    return True


def get_angel_session():
    """Return (api_key, jwt_token). Logs in lazily on first call. Thread-safe."""
    global _smart_api
    with _init_lock:
        if _smart_api is not None:
            return (_api_key_cache, _smart_api.access_token)
        if not _refresh_token_locked(force=True):
            raise RuntimeError("Angel One auth failed; check .env")
        return (_api_key_cache, _smart_api.access_token)


def _ensure_session():
    """Ensure SmartConnect is logged in. Returns the SmartConnect instance.
    Thread-safe: concurrent callers queue on _init_lock and reuse one session."""
    global _smart_api
    if _smart_api is not None:
        return _smart_api
    with _init_lock:
        if _smart_api is not None:
            return _smart_api
        if not _refresh_token_locked(force=True):
            raise RuntimeError("Angel One auth failed; check .env")
        return _smart_api


# ─────────────────────────── scrip master ──────────────────────────────────

def _master_is_fresh() -> bool:
    if not os.path.exists(SCRIP_MASTER_CACHE):
        return False
    age = time.time() - os.path.getmtime(SCRIP_MASTER_CACHE)
    return age < SCRIP_MASTER_TTL_DAYS * 86400


def _download_scrip_master():
    print("-> Downloading Angel One scrip master (~25 MB, weekly)...")
    req = urllib.request.Request(
        SCRIP_MASTER_URL, headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=120) as r, \
            open(SCRIP_MASTER_CACHE, "wb") as f:
        f.write(r.read())
    print("   Saved to %s" % SCRIP_MASTER_CACHE)


def _load_scrip_master() -> pd.DataFrame:
    global _master_df, _symbol_index
    if _master_df is not None:
        return _master_df
    with _init_lock:
        # Re-check after acquiring lock; another thread may have populated it.
        if _master_df is not None:
            return _master_df
        if not _master_is_fresh():
            _download_scrip_master()
        with open(SCRIP_MASTER_CACHE) as f:
            rows = json.load(f)
        df = pd.DataFrame(rows)
        if "instrumenttype" in df.columns:
            df = df[df["instrumenttype"].astype(str).isin(["", "AMXIDX"])
                    | df["instrumenttype"].isna()]
        keep = [c for c in ("token", "symbol", "name", "exch_seg", "lotsize")
                if c in df.columns]
        df = df[keep].copy()
        _master_df = df.reset_index(drop=True)
        idx = {}
        for r in _master_df.itertuples(index=False):
            try:
                sym_full = str(r.symbol).strip().upper()
                exch = str(r.exch_seg).strip().upper()
                tok = str(r.token).strip()
                name = str(getattr(r, "name", "")).strip().upper()
                if not (sym_full and exch and tok):
                    continue
                base = sym_full.split("-", 1)[0]
                idx.setdefault((exch, sym_full), tok)
                idx.setdefault((exch, base), tok)
                if name and name != base:
                    idx.setdefault((exch, name), tok)
            except Exception:
                continue
        _symbol_index = idx
        print("   Indexed %d (exch, symbol) -> token pairs" % len(idx))
        return _master_df


# ─────────────────────────── ticker resolution ─────────────────────────────

INDEX_OVERRIDES = {
    "^NSEI":      ("NSE", "99926000", "Nifty 50"),
    "^CRSLDX":    ("NSE", "99926004", "Nifty 500"),
    "^NSEBANK":   ("NSE", "99926009", "Nifty Bank"),
    "^BSESN":     ("BSE", "99919000", "Sensex"),
    "^NSEMID150": ("NSE", "99926060", "Nifty Midcap 150"),
    "^NSESML250": ("NSE", "99926062", "Nifty Smallcap 250"),
    "^NSEMID100": ("NSE", "99926011", "Nifty Midcap 100"),
    "^NSESML100": ("NSE", "99926032", "Nifty Smallcap 100"),
    "^NSEMS400":  ("NSE", "99926063", "Nifty MidSmall 400"),
    "^NSEFIN":    ("NSE", "99926037", "Nifty Financial Services"),
}


def _parse_ticker(ticker: str):
    if not ticker:
        return None, None
    if ticker in INDEX_OVERRIDES:
        ex, tok, _ = INDEX_OVERRIDES[ticker]
        return ex, tok
    t = ticker.strip()
    if ":" in t:
        prefix, raw = t.split(":", 1)
        prefix = prefix.upper()
        raw = raw.strip().upper()
        _load_scrip_master()
        if prefix == "BSE":
            if raw.isdigit():
                return "BSE", raw
            tok = (_symbol_index.get(("BSE", raw))
                   or _symbol_index.get(("BSE", raw + "-EQ")))
            return ("BSE", tok) if tok else (None, None)
        if prefix == "NSE":
            tok = (_symbol_index.get(("NSE", raw))
                   or _symbol_index.get(("NSE", raw + "-EQ")))
            return ("NSE", tok) if tok else (None, None)
        return None, None
    if t.upper().endswith(".BO"):
        raw = t[:-3].strip()
        if raw.isdigit():
            return "BSE", raw
        _load_scrip_master()
        tok = (_symbol_index.get(("BSE", raw.upper()))
               or _symbol_index.get(("BSE", raw.upper() + "-EQ")))
        return ("BSE", tok) if tok else (None, None)
    if t.upper().endswith(".NS"):
        raw = t[:-3].strip().upper()
        _load_scrip_master()
        tok = (_symbol_index.get(("NSE", raw + "-EQ"))
               or _symbol_index.get(("NSE", raw)))
        return ("NSE", tok) if tok else (None, None)
    _load_scrip_master()
    # 1. NSE regular equity
    tok = (_symbol_index.get(("NSE", t.upper() + "-EQ"))
           or _symbol_index.get(("NSE", t.upper())))
    if tok:
        return "NSE", tok
    # 2. BSE regular equity
    tok = (_symbol_index.get(("BSE", t.upper() + "-EQ"))
           or _symbol_index.get(("BSE", t.upper())))
    if tok:
        return "BSE", tok
    # 3. NSE SME
    tok = _symbol_index.get(("NSE", t.upper() + "-SM"))
    if tok:
        return "NSE", tok
    # 4. BSE SME (no special suffix — try name match)
    tok = _symbol_index.get(("BSE", t.upper() + "-SM"))
    return ("BSE", tok) if tok else (None, None)


# ─────────────────────────── rate limiter ──────────────────────────────────

class _RateLimiter:
    """Adaptive sliding-window limiter for Angel's getCandleData endpoint.

    Guarantees the union of:
      * <= per_sec calls in any rolling 1-second window
      * <= per_min calls in any rolling 60-second window
      * <= max_inflight concurrent calls (back-pressures N-pane bursts)

    Self-healing:
      * On AB1004 (rate-limited), halves the effective per-sec budget and
        forces a jittered cooldown so all waiting threads stagger.
      * After `recovery_threshold` consecutive successes, climbs the budget
        back toward nominal one step at a time.
      * Sleeps happen OUTSIDE the internal lock so threads compute fresh
        deadlines instead of stacking sequentially.
    """

    def __init__(self, per_sec=3, per_min=180, max_inflight=4,
                 recovery_threshold=20):
        self._lock = threading.Lock()
        self._sec_win = deque()
        self._min_win = deque()
        self._nominal_per_sec = per_sec
        self._per_min = per_min
        self._cur_per_sec = per_sec
        self._inflight = threading.BoundedSemaphore(max_inflight)
        self._consecutive_ok = 0
        self._recovery_threshold = recovery_threshold
        self._cooldown_until = 0.0
        self._throttled = False  # state flag for one-shot log on transition

    def _purge_locked(self, now):
        cutoff_s = now - 1.0
        while self._sec_win and self._sec_win[0] <= cutoff_s:
            self._sec_win.popleft()
        cutoff_m = now - 60.0
        while self._min_win and self._min_win[0] <= cutoff_m:
            self._min_win.popleft()

    def acquire(self):
        """Block until a slot is available. Caller MUST call release()."""
        self._inflight.acquire()
        try:
            while True:
                with self._lock:
                    now = time.time()
                    wait = max(0.0, self._cooldown_until - now)
                    if wait == 0.0:
                        self._purge_locked(now)
                        if len(self._sec_win) >= self._cur_per_sec:
                            wait = max(wait, self._sec_win[0] + 1.0 - now)
                        if len(self._min_win) >= self._per_min:
                            wait = max(wait, self._min_win[0] + 60.0 - now)
                        if wait <= 0.0:
                            self._sec_win.append(now)
                            self._min_win.append(now)
                            return
                # Jitter prevents waking herd at exact same instant.
                time.sleep(wait + random.uniform(0.0, 0.05))
        except BaseException:
            self._inflight.release()
            raise

    def release(self):
        self._inflight.release()

    def report_rate_limited(self):
        """Called after Angel returns AB1004. Throttle down + cooldown."""
        with self._lock:
            self._consecutive_ok = 0
            prev = self._cur_per_sec
            self._cur_per_sec = max(1, self._cur_per_sec // 2)
            self._cooldown_until = time.time() + 1.5 + random.uniform(0.0, 0.5)
            if not self._throttled:
                self._throttled = True
                print("[Angel] rate-limit hit — throttling %d → %d req/sec"
                      % (prev, self._cur_per_sec))

    def report_success(self):
        """Called after a successful Angel call. Gradually restore rate."""
        with self._lock:
            self._consecutive_ok += 1
            if (self._consecutive_ok >= self._recovery_threshold
                    and self._cur_per_sec < self._nominal_per_sec):
                self._cur_per_sec += 1
                self._consecutive_ok = 0
                if self._cur_per_sec >= self._nominal_per_sec:
                    if self._throttled:
                        self._throttled = False
                        print("[Angel] rate-limit recovered — back to %d req/sec"
                              % self._nominal_per_sec)


_rate_limiter = _RateLimiter(
    per_sec=RATE_LIMIT_PER_SEC,
    per_min=_RATE_LIMIT_PER_MIN,
    max_inflight=_RATE_LIMIT_MAX_INFLIGHT,
)


def _rate_limit_acquire():
    """Back-compat shim. Caller must pair with `_rate_limiter.release()`."""
    _rate_limiter.acquire()


# ─────────────────────────── public download API ───────────────────────────

def _to_date_str(d, with_time=True) -> str:
    if isinstance(d, str):
        if with_time and len(d) == 10:
            return d + " 09:15"
        return d
    if isinstance(d, (datetime.date, datetime.datetime)):
        if with_time:
            return d.strftime("%Y-%m-%d") + " 09:15"
        return d.strftime("%Y-%m-%d")
    return str(d)


def _empty_df():
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


_INTERVAL_MAP = {
    "1d":  "ONE_DAY",
    "1h":  "ONE_HOUR",
    "30m": "THIRTY_MINUTE",
    "15m": "FIFTEEN_MINUTE",
    "5m":  "FIVE_MINUTE",
    "1m":  "ONE_MINUTE",
}


def _is_auth_error_msg(msg: str) -> bool:
    """Check if an error message indicates an auth/session problem."""
    low = msg.lower()
    return any(s in low for s in (
        "ag8001", "ab1010", "invalid token", "expired",
        "session", "unauthor"
    ))


def angel_download(ticker: str,
                   start,
                   end=None,
                   interval: str = "1d",
                   retries: int = 2) -> pd.DataFrame:
    """Drop-in replacement for `yf.download(ticker, start, end)`.

    Returns DataFrame indexed by Timestamp with columns
    ['Open','High','Low','Close','Volume']. Empty on failure.
    Note: Angel daily candles cap at 2 000 days per request.
    """
    interval_const = _INTERVAL_MAP.get(interval)
    if interval_const is None:
        raise NotImplementedError("interval=%r not supported" % interval)
    end = end or datetime.date.today()
    fromdate = _to_date_str(start)
    todate = _to_date_str(end).replace("09:15", "15:30")

    exch, tok = _parse_ticker(ticker)
    if not tok:
        return _empty_df()

    historicParam = {
        "exchange":    exch,
        "symboltoken": tok,
        "interval":    interval_const,
        "fromdate":    fromdate,
        "todate":      todate,
    }

    for attempt in range(retries + 1):
        _rate_limiter.acquire()
        try:
            try:
                obj = _ensure_session()
                resp, timed_out = _call_with_timeout(obj.getCandleData, historicParam)
                if timed_out:
                    # Stale connection in SDK pool — drop session so the next
                    # attempt rebuilds it with a fresh urllib3 pool.
                    print("angel_download: getCandleData timed out for %s; resetting session" % ticker)
                    _reset_session()
                    if attempt < retries:
                        time.sleep(0.3)
                        continue
                    return _empty_df()
            except Exception:
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1)
                               + random.uniform(0.0, 0.25))
                    continue
                return _empty_df()

            if resp is None or not isinstance(resp, dict):
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1)
                               + random.uniform(0.0, 0.25))
                    continue
                return _empty_df()

            if not resp.get("status"):
                err_code = str(resp.get("errorcode", "")).upper()
                err_msg = str(resp.get("message", ""))
                # Rate limit — adaptive backoff: shrink internal budget,
                # then exponential sleep with jitter.
                if err_code == "AB1004":
                    _rate_limiter.report_rate_limited()
                    if attempt < retries:
                        time.sleep((1.5 ** attempt)
                                   + random.uniform(0.0, 0.5))
                        continue
                    return _empty_df()
                # Auth error — refresh token first, then full re-login
                if (_is_auth_error_msg(err_code + " " + err_msg)
                        and attempt < retries):
                    if _try_refresh_access_token() or refresh_token(force=False):
                        continue
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1)
                               + random.uniform(0.0, 0.25))
                    continue
                return _empty_df()

            data = resp.get("data") or []
            _rate_limiter.report_success()
            if not data:
                return _empty_df()
            df = pd.DataFrame(
                data, columns=["Date", "Open", "High", "Low", "Close", "Volume"],
            )
            df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
            df = df.set_index("Date").sort_index()
            df = df[~df.index.duplicated(keep="last")]
            return df
        finally:
            _rate_limiter.release()
    return _empty_df()


def angel_download_many(tickers,
                        start,
                        end=None,
                        max_workers: int = RATE_LIMIT_PER_SEC) -> dict:
    """Bulk fetch. Returns {ticker: DataFrame}, omitting empties."""
    out = {}
    if not tickers:
        return out
    _load_scrip_master()
    _ensure_session()
    print("  Angel bulk fetch: %d tickers (max_workers=%d, ~%.0fs minimum)"
          % (len(tickers), max_workers,
             len(tickers) / RATE_LIMIT_PER_SEC))
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(angel_download, t, start, end): t for t in tickers}
        for fut in as_completed(futs):
            t = futs[fut]
            done += 1
            try:
                df = fut.result()
            except Exception:
                df = _empty_df()
            if df is not None and not df.empty:
                out[t] = df
            if done % 50 == 0 or done == len(futs):
                print("    %d/%d (%.1fs, usable=%d)"
                      % (done, len(futs), time.time() - t0, len(out)))
    return out


# ─────────────────────────── self-test ─────────────────────────────────────

def _selftest():
    print("Angel One client self-test (SDK)")
    print("--------------------------------")
    _load_env()
    creds = _get_credentials()
    print("Credentials present : %s"
          % ("yes" if creds else "NO (fill .env)"))
    if not creds:
        return 1
    try:
        api_key, jwt = get_angel_session()
        print("Login (TOTP)        : OK (jwt len=%d)" % len(jwt))
    except Exception as e:
        print("Login FAILED        : %s" % e)
        return 2
    for t in ("RELIANCE.NS", "TCS.NS", "500325.BO", "^NSEI"):
        ex, tok = _parse_ticker(t)
        print("  resolve %-14s -> %s / %s" % (t, ex, tok))
    end = datetime.date.today()
    start = end - datetime.timedelta(days=40)
    df = angel_download("RELIANCE.NS", start, end)
    print("RELIANCE.NS rows    : %d" % len(df))
    if not df.empty:
        print(df.tail(3))
    return 0 if not df.empty else 4


if __name__ == "__main__":
    sys.exit(_selftest())
