"""
ohlcv_cache.py — persistent incremental daily-OHLCV cache for angel_client
==========================================================================

A two-tier cache placed in front of Angel's getCandleData, for DAILY bars only
("1d"). Intraday intervals (5m/15m/…) are never cached here — they bypass this
module entirely so live/intraday behaviour is unchanged.

Tiers
  L1 (in-memory): per-process dict, deduplicates repeat fetches within a single
      run / process. Bounded by _L1_TTL_SEC so long-running servers still
      re-check for new bars periodically.
  L2 (on-disk):   one gzipped-CSV file per (symbol, interval), giving cross-run
      incremental history so a re-run pulls only new/adjusted bars. The format
      is version-stable (readable by any pandas/Python) and tagged with a schema
      version; filenames carry a hash of the raw ticker so two symbols can never
      collide onto the same file.

Correctness guards
  1. closed-sessions-only : today's in-progress bar (before market close) is
     served live to the caller but NEVER written to disk, so a partial bar can
     never be persisted.
  2. overlap-overwrite    : every run re-fetches the last OVERLAP_DAYS calendar
     days and merges with keep="last", so provisional bars get finalized and
     split/adjustment restatements overwrite stale values.
  3. repair-or-rebuild    : on load, individually bad rows (NaN OHLC, High<Low,
     negative volume, dup/unsorted index, unparseable dates) are dropped and the
     rest kept; only a structurally unusable / unreadable file is discarded and
     re-fetched in full. The cache is a performance layer, never a source of
     truth, so a corrupt entry is always safe to repair or throw away.
  + atomic writes         : write to a temp file then os.replace(), so a crash
     mid-write leaves either the old file or the new one — never a half file.

The cache never masks a hard failure with an exception: any internal error in
`get()` is caught by the caller (angel_client) which falls back to a direct
fetch, so enabling the cache can never break a download.
"""

import os
import time
import gzip
import hashlib
import tempfile
import threading
import datetime
from typing import Callable, Optional

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get(
    "ANGEL_OHLCV_CACHE_DIR", os.path.join(SCRIPT_DIR, ".ohlcv_cache"))

_COLS = ["Open", "High", "Low", "Close", "Volume"]

# On-disk format version. Files are stored as gzipped CSV (a universal,
# interpreter-/pandas-version-independent format) with this schema tag on the
# first line. A file whose tag is missing or different is treated as
# incompatible and rebuilt — so upgrading pandas/Python can never leave the
# cache in a state where one interpreter silently can't read another's files.
_SCHEMA_VERSION = 2
_SCHEMA_TAG = "# ohlcv_cache schema=%d" % _SCHEMA_VERSION

# India market close ~15:30 IST; use a small buffer so the settled EOD bar is
# available before we treat today as a "closed session".
_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_CLOSE_H, _CLOSE_M = 15, 45

# Re-fetch this many trailing calendar days each run and overwrite (keep="last").
OVERLAP_DAYS = int(os.environ.get("ANGEL_CACHE_OVERLAP_DAYS", "7"))
# Within one process, treat a symbol refreshed this recently as fresh (skip the
# network). Bounds staleness for long-running servers (e.g. tradingcharts).
_L1_TTL_SEC = float(os.environ.get("ANGEL_CACHE_L1_TTL", "300"))


def enabled() -> bool:
    """Cache is on unless ANGEL_OHLCV_CACHE is explicitly falsey."""
    return os.environ.get("ANGEL_OHLCV_CACHE", "1").strip().lower() \
        not in ("0", "false", "no", "off", "")


# ─────────────────────────── in-memory (L1) state ──────────────────────────
_l1: dict = {}         # (ticker, interval) -> full-history DataFrame (may incl live bar)
_l1_time: dict = {}    # (ticker, interval) -> epoch of last refresh
_locks: dict = {}      # (ticker, interval) -> Lock (serialize per-symbol work)
_locks_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _locks[key] = lk
        return lk


# ─────────────────────────── helpers ───────────────────────────────────────
def _as_date(d) -> datetime.date:
    if d is None:
        return datetime.date.today()
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    return pd.Timestamp(d).date()


def _persist_cutoff() -> datetime.date:
    """Newest date allowed on disk: today only once the session has closed,
    otherwise yesterday. Guarantees today's provisional bar is never stored."""
    now = datetime.datetime.now(_IST)
    if (now.hour, now.minute) >= (_CLOSE_H, _CLOSE_M):
        return now.date()
    return now.date() - datetime.timedelta(days=1)


def _safe_name(ticker: str) -> str:
    return "".join(c if (c.isalnum() or c in "._^-") else "_" for c in str(ticker))


def _cache_file(ticker: str, interval: str) -> str:
    # A short hash of the RAW ticker guarantees uniqueness even when _safe_name
    # maps two different tickers (e.g. "NSE:ABC" and "NSE_ABC") to the same
    # sanitized string — without it they would collide onto one file.
    h = hashlib.sha1(str(ticker).encode("utf-8")).hexdigest()[:8]
    return os.path.join(CACHE_DIR, "%s_%s__%s.csv.gz"
                        % (_safe_name(ticker), h, interval))


def _empty():
    return pd.DataFrame(columns=_COLS)


def _repair(df) -> Optional[pd.DataFrame]:
    """Return a clean, valid frame by DROPPING individually-bad rows, or None if
    the frame is structurally unusable. Unlike a strict reject, one bad vendor
    row no longer throws away a symbol's whole history."""
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return None
        if not set(_COLS).issubset(set(df.columns)):
            return None
        out = df.loc[:, _COLS].copy()
        # Coerce index to datetime; drop rows with unparseable dates.
        if not isinstance(out.index, pd.DatetimeIndex):
            out.index = pd.to_datetime(out.index, errors="coerce")
        out = out[~out.index.isna()]
        # Coerce values to numeric.
        for c in _COLS:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        # Drop rows with any NaN OHLC.
        out = out.dropna(subset=["Open", "High", "Low", "Close"])
        # Drop structurally impossible rows.
        out = out[out["High"] >= out["Low"]]
        out["Volume"] = out["Volume"].fillna(0)
        out = out[out["Volume"] >= 0]
        # De-dup (keep newest) + sort.
        out = out[~out.index.duplicated(keep="last")].sort_index()
        out.index.name = "Date"
        if out.empty:
            return None
        return out
    except Exception:
        return None


def _validate(df) -> bool:
    """Strict integrity check (used by the store gate and tests). Returns False
    on anything suspicious; `_repair` is the lenient counterpart used on load."""
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return False
        if list(df.columns) != _COLS:
            return False
        if not isinstance(df.index, pd.DatetimeIndex):
            return False
        if df.index.hasnans or df.index.duplicated().any():
            return False
        if not df.index.is_monotonic_increasing:
            return False
        if df[["Open", "High", "Low", "Close"]].isna().any().any():
            return False
        if (df["High"] < df["Low"]).any():
            return False
        if (df["Volume"] < 0).any():
            return False
        return True
    except Exception:
        return False


def _discard(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _load_l2(ticker, interval):
    path = _cache_file(ticker, interval)
    if not os.path.exists(path):
        return None
    try:
        with gzip.open(path, "rt", newline="") as gz:
            first = gz.readline()
            if not first.startswith("# ohlcv_cache schema="):
                _discard(path)          # not our format → rebuild
                return None
            try:
                ver = int(first.strip().rsplit("=", 1)[1])
            except Exception:
                _discard(path)
                return None
            if ver != _SCHEMA_VERSION:
                _discard(path)          # older/newer schema → rebuild
                return None
            df = pd.read_csv(gz, index_col=0, parse_dates=[0])
    except Exception:
        _discard(path)                  # unreadable / truncated → rebuild
        return None
    repaired = _repair(df)              # drop any bad rows instead of nuking all
    if repaired is None:
        _discard(path)                  # structurally unusable → rebuild
        return None
    return repaired


def _atomic_write(df, ticker, interval):
    try:
        clean = _repair(df)
        if clean is None:
            return                      # nothing valid to store
        os.makedirs(CACHE_DIR, exist_ok=True)
        path = _cache_file(ticker, interval)
        fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        os.close(fd)
        try:
            with gzip.open(tmp, "wt", newline="") as gz:
                gz.write(_SCHEMA_TAG + "\n")
                clean.to_csv(gz)        # index (Date) + OHLCV columns
            os.replace(tmp, path)       # atomic on POSIX
        finally:
            if os.path.exists(tmp):
                _discard(tmp)
    except Exception:
        pass                            # persistence is best-effort; never fatal


def _merge(old, new):
    if old is None or getattr(old, "empty", True):
        m = new
    elif new is None or getattr(new, "empty", True):
        m = old
    else:
        m = pd.concat([old, new])
        m = m[~m.index.duplicated(keep="last")]   # newest bar wins (overlap-overwrite)
    return m.sort_index()


def _covers(df, start_ts, end_ts) -> bool:
    if df is None or df.empty:
        return False
    return df.index.min() <= start_ts and df.index.max() >= end_ts


def _slice(df, start_ts, end_ts):
    try:
        out = df.loc[start_ts:end_ts]
    except Exception:
        out = df[(df.index >= start_ts) & (df.index <= end_ts)]
    return out.copy()


# ─────────────────────────── public entry point ────────────────────────────
def get(ticker: str, start, end, interval: str,
        fetch_fn: Callable[[datetime.date, datetime.date], pd.DataFrame]) -> pd.DataFrame:
    """Return daily OHLCV for `ticker` over [start, end], using the L1/L2 cache
    and calling `fetch_fn(from_date, to_date)` only for the missing/overlap span.

    `fetch_fn` must return a yfinance-shaped DataFrame (same as angel_download):
    DatetimeIndex + columns [Open, High, Low, Close, Volume].
    """
    start_d = _as_date(start)
    end_d = _as_date(end)
    start_ts = pd.Timestamp(start_d)
    end_ts = pd.Timestamp(end_d)
    cutoff_ts = pd.Timestamp(_persist_cutoff())

    key = (ticker, interval)
    with _lock_for(key):
        now = time.time()

        # ---- L1 fast path (in-memory dedupe within the process) ----
        cached = _l1.get(key)
        if (cached is not None
                and (now - _l1_time.get(key, 0.0)) < _L1_TTL_SEC
                and _covers(cached, start_ts, min(end_ts, cutoff_ts))):
            return _slice(cached, start_ts, end_ts)

        # ---- L2 load (validate-or-rebuild) ----
        if cached is None:
            cached = _load_l2(ticker, interval)

        # ---- decide the minimal fetch window ----
        if cached is None or cached.empty:
            fetches = [(start_d, end_d)]                       # full build
        else:
            cmin = cached.index.min().date()
            cmax = cached.index.max().date()
            need_head = start_d < cmin                         # want older history
            refresh_from = cmax - datetime.timedelta(days=OVERLAP_DAYS)
            need_tail = end_d >= refresh_from                  # want recent/overlap
            if need_head and need_tail:
                fetches = [(start_d, end_d)]
            elif need_head:
                fetches = [(start_d, cmin)]
            elif need_tail:
                fetches = [(refresh_from, end_d)]
            else:
                fetches = []                                   # fully covered → 0 calls

        merged = cached
        for fs, fe in fetches:
            if fs > fe:
                continue
            try:
                fresh = fetch_fn(fs, fe)
            except Exception:
                fresh = None
            if fresh is not None and not fresh.empty:
                merged = _merge(merged, fresh)

        # Clean any individually-bad rows the vendor returned (repair, not reject).
        merged = _repair(merged)
        if merged is None or merged.empty:
            return _empty()

        # ---- persist closed sessions only (atomic; repairs internally) ----
        to_store = merged[merged.index <= cutoff_ts]
        _atomic_write(to_store, ticker, interval)

        # L1 keeps the full merged frame (incl. any live bar) for in-run reuse.
        _l1[key] = merged
        _l1_time[key] = now
        return _slice(merged, start_ts, end_ts)
