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
  2. overlap-overwrite    : the last OVERLAP_DAYS calendar days are re-fetched
     and merged with keep="last", so provisional bars get finalized and
     split/adjustment restatements overwrite stale values. See the tail-refresh
     note below for how often that sweep runs.
  3. repair-or-rebuild    : on load, individually bad rows (NaN OHLC, High<Low,
     negative volume, dup/unsorted index, unparseable dates) are dropped and the
     rest kept; only a structurally unusable / unreadable file is discarded and
     re-fetched in full. The cache is a performance layer, never a source of
     truth, so a corrupt entry is always safe to repair or throw away.
  + atomic writes         : write to a temp file then os.replace(), so a crash
     mid-write leaves either the old file or the new one — never a half file.

Head watermark (schema v3)
  A symbol listed after the caller's requested start can never satisfy the
  coverage test on its own first bar, so without help every consumer re-fetches
  its entire window on every run, forever — the newest listings are the most
  expensive rows in the cache. Each entry therefore records the earliest start
  that was actually asked for and answered (`head`) and when (`probed`); while
  that is fresh, the requested start is clamped to the frame's first bar and the
  head fetch is skipped. Nothing in a single response distinguishes "genuinely
  young" from "truncated under load", so the watermark expires after
  HEAD_REPROBE_DAYS and a fetch that returns nothing never sets it.

Tail refresh (schema v4)
  The overlap sweep of guard 2 is what keeps restatements correct, but it also
  means every symbol costs a network round trip on its first touch in a process
  even when the cache already holds the newest closed bar. For a universe of
  ~1000 symbols that alone saturates the vendor's rate limit. Each entry
  therefore records the date its overlap was last re-fetched (`refreshed`), and
  the sweep is skipped when it already ran today AND the cache already covers
  every closed session the caller asked for. The second half of that test is
  what keeps a later run on the same day correct: once a new session closes,
  cmax falls behind the cutoff and the sweep runs again. The cost is that a
  restatement published between two runs on the same day is picked up the next
  day rather than immediately; set ANGEL_CACHE_TAIL_REFRESH_DAILY=0 to restore
  the per-run sweep.

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
# first line. A file whose tag is missing or unreadable is treated as
# incompatible and rebuilt — so upgrading pandas/Python can never leave the
# cache in a state where one interpreter silently can't read another's files.
# v3 adds the head watermark and v4 the tail-refresh date; the row format is
# unchanged across all of them, so older files are read in place (watermarks
# unknown) and upgraded on their next write rather than invalidating the cache.
_SCHEMA_VERSION = 4
_READABLE_SCHEMAS = (2, 3, 4)
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
# A head watermark can be wrong if the vendor truncated a response under load
# rather than genuinely having no older bars, and nothing in a single response
# distinguishes those. Re-probing on this cadence bounds how long such a
# mistake can persist while still skipping the head fetch on most runs.
HEAD_REPROBE_DAYS = int(os.environ.get("ANGEL_CACHE_HEAD_REPROBE_DAYS", "7"))
# Run the OVERLAP_DAYS sweep once per calendar day instead of once per process.
# Set to 0/false to re-fetch the overlap on every run (stricter, much slower).
TAIL_REFRESH_DAILY = os.environ.get(
    "ANGEL_CACHE_TAIL_REFRESH_DAILY", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")


def enabled() -> bool:
    """Cache is on unless ANGEL_OHLCV_CACHE is explicitly falsey."""
    return os.environ.get("ANGEL_OHLCV_CACHE", "1").strip().lower() \
        not in ("0", "false", "no", "off", "")


# ─────────────────────────── in-memory (L1) state ──────────────────────────
_l1: dict = {}         # (ticker, interval) -> full-history DataFrame (may incl live bar)
_l1_time: dict = {}    # (ticker, interval) -> epoch of last refresh
_l1_marks: dict = {}   # (ticker, interval) -> (head, probed, refreshed) dates
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


def _parse_header(line):
    """Parse the schema tag line into (version, head, probed, refreshed).

    Returns all-None if the line is not one of ours. The watermarks are None for
    files written by an older schema, which simply means "unknown" — the caller
    then re-probes rather than trusting a value it does not have.
    """
    if not line.startswith("# ohlcv_cache schema="):
        return None, None, None, None
    ver, head, probed, refreshed = None, None, None, None
    for tok in line.strip().split():
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        try:
            if k == "schema":
                ver = int(v)
            elif k == "head":
                head = datetime.date.fromisoformat(v)
            elif k == "probed":
                probed = datetime.date.fromisoformat(v)
            elif k == "refreshed":
                refreshed = datetime.date.fromisoformat(v)
        except Exception:
            if k == "schema":
                return None, None, None, None   # unusable version → rebuild
            head, probed, refreshed = None, None, None   # bad mark → re-probe
    return ver, head, probed, refreshed


def _header_line(head, probed, refreshed) -> str:
    tag = _SCHEMA_TAG
    if head is not None and probed is not None:
        tag += " head=%s probed=%s" % (head.isoformat(), probed.isoformat())
    if refreshed is not None:
        tag += " refreshed=%s" % refreshed.isoformat()
    return tag


def _load_l2(ticker, interval):
    """Return (DataFrame, head, probed, refreshed), or all-None to rebuild."""
    path = _cache_file(ticker, interval)
    if not os.path.exists(path):
        return None, None, None, None
    try:
        with gzip.open(path, "rt", newline="") as gz:
            first = gz.readline()
            ver, head, probed, refreshed = _parse_header(first)
            if ver is None or ver not in _READABLE_SCHEMAS:
                _discard(path)          # not our format / unknown schema
                return None, None, None, None
            df = pd.read_csv(gz, index_col=0, parse_dates=[0])
    except Exception:
        _discard(path)                  # unreadable / truncated → rebuild
        return None, None, None, None
    repaired = _repair(df)              # drop any bad rows instead of nuking all
    if repaired is None:
        _discard(path)                  # structurally unusable → rebuild
        return None, None, None, None
    return repaired, head, probed, refreshed


def _atomic_write(df, ticker, interval, head=None, probed=None, refreshed=None):
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
                gz.write(_header_line(head, probed, refreshed) + "\n")
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


def _clamp_start(df, start_ts, head, probed, start_d, today):
    """Requested start, raised to the frame's first bar when the head watermark
    proves no older bars exist.

    Without this a symbol listed after `start_d` never satisfies `_covers`, so
    every caller re-fetches its whole window on every run forever. The clamp
    only applies while the watermark is fresh, so a watermark recorded from a
    truncated vendor response self-corrects at the next re-probe.
    """
    if head is None or probed is None or df is None or df.empty:
        return start_ts
    if head > start_d:
        return start_ts                 # watermark covers a later start only
    if (today - probed).days >= HEAD_REPROBE_DAYS:
        return start_ts                 # stale → re-probe the head
    return max(start_ts, df.index.min())


def _tail_is_fresh(cmax, refreshed, end_d, cutoff_d, today) -> bool:
    """True when the trailing overlap sweep can be skipped for this call.

    Both halves matter. `refreshed == today` means the restatement sweep has
    already run once today, and `cmax >= min(end_d, cutoff_d)` means the cache
    already holds every closed session the caller asked for — so a second run
    after a new session closes still fetches, because cmax has fallen behind
    the cutoff by then.
    """
    if not TAIL_REFRESH_DAILY or refreshed is None or refreshed != today:
        return False
    return cmax >= min(end_d, cutoff_d)


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

    A request that starts before the symbol's listing date is satisfied from
    cache alone once the head watermark has been recorded, instead of re-fetching
    the full window every run (see the head-watermark note in the module
    docstring). The trailing overlap sweep is likewise skipped once it has run
    today and the cache already covers every closed session asked for (see the
    tail-refresh note). The returned frame is always sliced to the caller's
    [start, end], so a clamped start never changes what the caller sees.

    `fetch_fn` must return a yfinance-shaped DataFrame (same as angel_download):
    DatetimeIndex + columns [Open, High, Low, Close, Volume].
    """
    start_d = _as_date(start)
    end_d = _as_date(end)
    start_ts = pd.Timestamp(start_d)
    end_ts = pd.Timestamp(end_d)
    cutoff_d = _persist_cutoff()
    cutoff_ts = pd.Timestamp(cutoff_d)

    key = (ticker, interval)
    with _lock_for(key):
        now = time.time()
        today = datetime.date.today()

        # ---- L1 fast path (in-memory dedupe within the process) ----
        cached = _l1.get(key)
        head, probed, refreshed = _l1_marks.get(key, (None, None, None))
        if cached is not None and (now - _l1_time.get(key, 0.0)) < _L1_TTL_SEC:
            eff_ts = _clamp_start(cached, start_ts, head, probed, start_d, today)
            if _covers(cached, eff_ts, min(end_ts, cutoff_ts)):
                return _slice(cached, start_ts, end_ts)

        # ---- L2 load (validate-or-rebuild) ----
        if cached is None:
            cached, head, probed, refreshed = _load_l2(ticker, interval)
        marks_before = (head, probed, refreshed)

        # ---- decide the minimal fetch window ----
        if cached is None or cached.empty:
            fetches = [(start_d, end_d)]                       # full build
        else:
            cmin = cached.index.min().date()
            cmax = cached.index.max().date()
            eff_start_d = _clamp_start(
                cached, start_ts, head, probed, start_d, today).date()
            need_head = eff_start_d < cmin                     # want older history
            refresh_from = cmax - datetime.timedelta(days=OVERLAP_DAYS)
            need_tail = end_d >= refresh_from                  # want recent/overlap
            if need_tail and _tail_is_fresh(cmax, refreshed, end_d, cutoff_d, today):
                need_tail = False                              # swept already today
            if need_head and need_tail:
                fetches = [(start_d, end_d)]
            elif need_head:
                fetches = [(start_d, cmin)]
            elif need_tail:
                fetches = [(refresh_from, end_d)]
            else:
                fetches = []                                   # fully covered → 0 calls

        merged = cached
        asked_head = any(fs <= start_d for fs, fe in fetches)
        asked_tail = any(fe >= end_d for fs, fe in fetches)
        for fs, fe in fetches:
            if fs > fe:
                continue
            try:
                fresh = fetch_fn(fs, fe)
            except Exception:
                fresh = None
            if fresh is not None and not fresh.empty:
                merged = _merge(merged, fresh)
            else:
                # Inconclusive: trust no watermark this call rather than record
                # one that would suppress future fetches.
                asked_head = asked_tail = False

        # Clean any individually-bad rows the vendor returned (repair, not reject).
        merged = _repair(merged)
        if merged is None or merged.empty:
            return _empty()

        # A completed fetch from `start_d` that returned data proves the vendor
        # has nothing older, so record it and stop re-asking until the re-probe.
        if asked_head:
            head = start_d if head is None else min(head, start_d)
            probed = today
        if asked_tail:
            refreshed = today

        # ---- persist closed sessions only (atomic; repairs internally) ----
        # Skip the rewrite when nothing changed: re-gzipping every symbol on a
        # fully-cached run costs more than the fetches it replaced.
        if fetches or (head, probed, refreshed) != marks_before:
            to_store = merged[merged.index <= cutoff_ts]
            _atomic_write(to_store, ticker, interval, head, probed, refreshed)

        # L1 keeps the full merged frame (incl. any live bar) for in-run reuse.
        _l1[key] = merged
        _l1_time[key] = now
        _l1_marks[key] = (head, probed, refreshed)
        return _slice(merged, start_ts, end_ts)
