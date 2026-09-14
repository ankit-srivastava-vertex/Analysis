"""Shared screener.in client: one login, one cache, one global rate limiter.

Five modules used to scrape screener.in, each with its own session, its own
login, its own cache and its own idea of how fast it was allowed to go
(breakout_scanner_angel, fii_stake_tracker, forensic_accounting,
ipo_listing_gainers and screener/app.py). Screener bans by IP, so the effective
request rate was whatever all of them happened to add up to on a given run —
run_all.py could have four of them talking to the host at once. This module is
the single throat: every request goes through one session, one credential load,
one disk cache and one process-wide pacer.

Why the pacing looks paranoid
-----------------------------
Screener does not answer an abusive client with 429. It refuses the TCP
connection outright, silently, and the block outlives the process — so a run
that gets greedy poisons every later run too. Hence the fixed inter-request
gap, the long backoff on connection errors, and the circuit breaker that gives
up the whole stage rather than keep knocking. Everything fetched before the
breaker trips stays cached, so the next run resumes instead of starting over.

Cache hits never touch the network and never wait on the pacer, which keeps the
interactive Flask app (screener/app.py) responsive while still holding cold
fetches to a safe rate.

Public surface:
  - get(path, ttl_hours=..., params=..., force=...) -> str | None
        Cached, paced GET. `path` may be site-relative or absolute.
  - get_json(path, ...) -> parsed JSON | None
  - session() -> requests.Session
        The shared, logged-in session, for callers that need raw access.
  - login_ok() -> bool          Whether authentication succeeded.
  - have_credentials() -> bool  Whether SCREENER_USER / SCREENER_PASS are set.
  - blocked() -> bool           Whether the circuit breaker has tripped.
  - reset_circuit()             Re-arm the breaker (used by tests).
  - BASE                        "https://www.screener.in"

Environment:
  SCREENER_USER / SCREENER_PASS   credentials (optional; anonymous still works
                                  for public company pages)
  SCREENER_MIN_INTERVAL           override the inter-request gap, in seconds
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is optional; env vars may already be exported
    load_dotenv = None

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache" / "screener_client"

BASE = "https://www.screener.in"
LOGIN_URL = f"{BASE}/login/"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

# The most cautious of the five values previously in use. Going faster is what
# gets the IP blocked, and the block is not observable until it is too late.
MIN_INTERVAL = float(os.getenv("SCREENER_MIN_INTERVAL", "1.1"))
BACKOFF = 20.0          # after a refused connection
MAX_FAILS = 4           # consecutive failures before the breaker trips
DEFAULT_TTL_HOURS = 24 * 7
TIMEOUT = 25

_lock = threading.Lock()          # guards session construction + login
_pace_lock = threading.Lock()     # guards the inter-request clock
_last_request = 0.0
_session: Optional[requests.Session] = None
_login_state = {"attempted": False, "ok": False}
_fails = 0

_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken"[^>]*?value="([^"]+)"')
_CSRF_RE_ALT = re.compile(r'value="([^"]+)"[^>]*?name="csrfmiddlewaretoken"')


# ── credentials ─────────────────────────────────────────────────────────────

def _creds() -> tuple[str, str]:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")
    user = (os.getenv("SCREENER_USER") or "").strip().strip("'\" ")
    pwd = (os.getenv("SCREENER_PASS") or "").strip().strip("'\" ")
    return user, pwd


def have_credentials() -> bool:
    user, pwd = _creds()
    return bool(user and pwd)


# ── session + login ─────────────────────────────────────────────────────────

def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _login(s: requests.Session) -> bool:
    """Authenticate `s`. Returns False (never raises) when login is impossible."""
    user, pwd = _creds()
    if not (user and pwd):
        return False
    try:
        page = s.get(LOGIN_URL, timeout=TIMEOUT)
        m = _CSRF_RE.search(page.text) or _CSRF_RE_ALT.search(page.text)
        if not m:
            return False
        resp = s.post(
            LOGIN_URL,
            data={"username": user, "password": pwd, "next": "/",
                  "csrfmiddlewaretoken": m.group(1)},
            headers={"Referer": LOGIN_URL},
            timeout=TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException:
        return False
    if resp.status_code >= 400:
        return False
    body = resp.text or ""
    if "Please enter a correct" in body or "Invalid username" in body:
        return False
    # Django bounces a failed login back onto /login/; a good one redirects away.
    if resp.url and "/login/" in resp.url:
        return False
    return True


def session() -> requests.Session:
    """The shared session, logging in once on first use."""
    global _session
    with _lock:
        if _session is None:
            _session = _build_session()
        if not _login_state["attempted"]:
            _login_state["attempted"] = True
            _login_state["ok"] = _login(_session)
        return _session


def login_ok() -> bool:
    session()
    return bool(_login_state["ok"])


# ── circuit breaker ─────────────────────────────────────────────────────────

def blocked() -> bool:
    return _fails >= MAX_FAILS


def reset_circuit() -> None:
    global _fails
    _fails = 0


# ── pacing ──────────────────────────────────────────────────────────────────

def _pace() -> None:
    """Hold every network call to MIN_INTERVAL apart, process-wide."""
    global _last_request
    with _pace_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()


# ── disk cache ──────────────────────────────────────────────────────────────

def _cache_path(key: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")


def _cache_read(key: str, ttl_hours: float) -> Optional[str]:
    if ttl_hours <= 0:
        return None
    fp = _cache_path(key)
    if not fp.exists():
        return None
    try:
        blob = json.loads(fp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if time.time() - blob.get("t", 0) > ttl_hours * 3600:
        return None
    return blob.get("body")


def _cache_write(key: str, body: str, ttl_hours: float) -> None:
    """Persist a body. A non-positive TTL means the caller keeps its own cache,
    so storing the raw HTML here would only duplicate it on disk."""
    if ttl_hours <= 0:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_text(
            json.dumps({"t": time.time(), "key": key, "body": body}),
            encoding="utf-8")
    except OSError:
        pass


# ── fetch ───────────────────────────────────────────────────────────────────

def _abs(path: str) -> str:
    return path if path.startswith("http") else BASE + path


def _is_api(url: str) -> bool:
    """Whether a URL is an XHR endpoint. Checked on the path component so that
    absolute and site-relative forms behave the same."""
    return urlparse(url).path.startswith("/api/")


def get(path: str, ttl_hours: float = DEFAULT_TTL_HOURS,
        params: Optional[dict] = None, force: bool = False,
        headers: Optional[dict] = None) -> Optional[str]:
    """Cached, paced GET. Returns the body, or None if it could not be had.

    A non-200 is cached as a miss because 404s do not heal, but a refused
    connection is not — that is the host pushing back, not a missing page.
    """
    global _fails
    url = _abs(path)
    key = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                 if params else "")

    if not force:
        hit = _cache_read(key, ttl_hours)
        if hit is not None:
            return hit or None

    if blocked():
        return None

    s = session()
    hdrs = dict(headers or {})
    # /api/ paths are XHR endpoints; asking for an ordinary page with this
    # header returns a fragment instead of the document.
    if _is_api(url):
        hdrs.setdefault("X-Requested-With", "XMLHttpRequest")

    for attempt in range(3):
        _pace()
        try:
            r = s.get(url, params=params, timeout=TIMEOUT, headers=hdrs)
        except requests.RequestException:
            time.sleep(BACKOFF * (attempt + 1))
            continue
        _fails = 0
        if r.status_code == 429:
            time.sleep(BACKOFF)
            continue
        if r.status_code != 200:
            _cache_write(key, "", ttl_hours)
            return None
        _cache_write(key, r.text, ttl_hours)
        return r.text

    _fails += 1
    if _fails == MAX_FAILS:
        print("  [screener] connection repeatedly refused — screener.in is rate "
              "limiting this IP. Skipping the rest; cached data is kept and the "
              "next run resumes from it.")
    return None


def fetch(path: str, params: Optional[dict] = None,
          headers: Optional[dict] = None) -> Optional[requests.Response]:
    """Paced, uncached request returning the raw Response.

    For the callers that need more than the body — the final URL after
    redirects (used to canonicalise a /company/<slug>/), the status code, or
    headers. Returns None only when the request could not be made at all.
    """
    if blocked():
        return None
    s = session()
    url = _abs(path)
    hdrs = dict(headers or {})
    if _is_api(url):
        hdrs.setdefault("X-Requested-With", "XMLHttpRequest")
    _pace()
    try:
        return s.get(url, params=params, timeout=TIMEOUT, headers=hdrs)
    except requests.RequestException:
        return None


def get_json(path: str, ttl_hours: float = DEFAULT_TTL_HOURS,
             params: Optional[dict] = None, force: bool = False) -> Any:
    body = get(path, ttl_hours=ttl_hours, params=params, force=force)
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _selftest() -> None:
    print("screener_client self-test")
    print("-" * 40)
    print("credentials present :", have_credentials())
    print("login               :", "OK" if login_ok() else "FAILED/anonymous")
    body = get("/company/RELIANCE/", ttl_hours=0)
    print("RELIANCE page       :", f"{len(body)} bytes" if body else "FAILED")
    print("  has P&L table     :", bool(body and "Profit & Loss" in body))
    hits = get_json("/api/company/search/", params={"q": "reliance"}, ttl_hours=0)
    print("search API          :", f"{len(hits)} hits" if hits else "FAILED")
    t0 = time.time()
    get("/company/TCS/", ttl_hours=0)
    get("/company/INFY/", ttl_hours=0)
    print(f"two cold fetches    : {time.time() - t0:.1f}s "
          f"(pacing >= {MIN_INTERVAL}s apart)")
    t0 = time.time()
    get("/company/TCS/")
    print(f"cached fetch        : {time.time() - t0:.3f}s (no pacing)")


if __name__ == "__main__":
    _selftest()
