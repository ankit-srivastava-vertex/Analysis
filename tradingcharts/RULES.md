# tradingcharts — Non-Negotiable Rules

**Goal:** ship something *better than TradingView*. The eight pillars below are
**RULES, not preferences**. Every code change to anything under
`tradingcharts/`, `angel_client.py`, or `data_provider.py` MUST pass all eight.
A change that violates any pillar is rejected, even if it ships a feature.

Read this file before touching the code. Re-read after writing the code.
Verify, then ship.

---

## Pillar 1 — Fully functional & durable
- Every external call (Angel REST/WS, yfinance, jugaad, browser fetch) **MUST**
  have an explicit wall-clock timeout and a recovery path. No bare network
  call without a timeout. Ever.
- Every Angel SDK call **MUST** go through `angel_client._call_with_timeout`,
  which on timeout invokes `_reset_session()` so the next call rebuilds the
  SDK with a fresh urllib3 pool.
- No silent breakage. Failures must surface in logs and in `/api/health`.

## Pillar 2 — Highly portable & compatible
- **MUST** run on macOS, Linux, Windows.
- **MUST** run on Python 3.9+ with **only** the deps in
  `tradingcharts/requirements.txt` plus the Analysis-wide `requirements.txt`.
- **MUST NOT** use `lsof`, `ps`, `os.kill`, or any platform-specific shell.
  Port checks use `socket.bind`. Path joins use `os.path` / `pathlib`.
- **MUST** support modern Chrome/Safari/Firefox without polyfills or
  bundlers. Vanilla JS only — no React, no TypeScript build, no webpack.
- Upstream fallback: Angel unavailable → yfinance. Both unavailable → 502
  with a clear error message; never hang.

## Pillar 3 — Scalable
- Per-click work is **O(visible bars)**, not O(history × panes).
- Adding panes, indicators, watchlists, drawings **MUST NOT** degrade
  existing pane interaction latency.
- Cross-pane fan-out (e.g. RS benchmark, sync flyout) **MUST** dedupe to a
  single upstream call via Promise/in-flight cache.
- Server-side caches are LRU + TTL bounded. No unbounded growth.

## Pillar 4 — Click-and-go, zero hang-ups
- **No Flask handler may block forever.** All upstream calls have wall-clock
  timeouts (see Pillar 1).
- **Indicator toggles MUST NOT refetch.** Reuse `pane._candles` /
  `pane._volumes` and `applyAllIndicators(pane)`.
- Page reload **MUST** paint the first pane immediately; remaining panes
  hydrate sequentially after pane 0 (no thundering herd).
- Symbol change, timeframe change, theme change, sync toggle: **none** may
  cause a UI freeze longer than 100 ms.
- Re-entrancy: any cross-pane broadcast must use a guard flag cleared in a
  microtask (`Promise.resolve().then(...)`) to swallow same-tick echoes.

## Pillar 5 — Extremely lightweight
- Total first-paint payload (gzipped) **MUST** stay **< 200 KB**. Current
  budget headroom: ~108 KB used.
- **No heavy frameworks.** Vanilla JS + lightweight-charts only.
- **No build step.** HTML/JS/CSS shipped as-is.
- Vendored libs (`static/vendor/`) load locally first; CDN is `onerror`
  fallback only. No required CDN at boot.
- Python deps: minimal. Adding a dep requires justification in PR/commit
  message.
- **No over-engineering.** No premature abstractions, no helpers for
  one-time operations, no comments that re-state code.

## Pillar 6 — Blazing fast
- First paint on warm cache: **< 1 s**.
- `/api/historical` cache hit: **< 50 ms** (current: ~30 ms).
- Indicator toggle: **< 50 ms** (zero refetch — must be local compute only).
- Pan / zoom: **60 fps** sustained.
- WS tick → chart update: **< 50 ms**.
- Server health endpoint: **< 5 ms**.

## Pillar 7 — Absolutely accurate, zero bugs
- **Numbers are sacred.** OHLCV, indicators, P&L, % change, volume, RS
  ratios, watchlist counts, alert triggers — every number rendered MUST
  be verifiable against a trusted source (Angel official quote /
  yfinance / NSE bhavcopy). No silent rounding drift, no off-by-one
  bars, no timezone slips (IST throughout, no UTC bleed-through).
- **No silent failures.** Every `try/except` MUST either fully recover
  or surface the failure in logs **and** in the UI / `/api/health`.
  Bare `except: pass` is forbidden except where the only sane action
  is to skip a single tick.
- **No off-by-one in time-series.** Bar timestamps, lookback windows,
  resample boundaries, and "today vs yesterday" logic MUST be tested
  against an Angel ground-truth before merging.
- **Indicator math MUST match the textbook.** RSI = Wilder's, MACD =
  12/26/9 EMA default, BB = 20/2σ, ATR = 14 Wilder, etc. If a series
  needs a warmup period, hide the warmup region (do not render NaN
  bars as zero).
- **State integrity.** localStorage round-trips (`TRACKED_KEYS`) MUST
  survive reload, port change, theme change, chart-count change. No
  config can silently revert.
- **Cache correctness > cache hit-rate.** A cache key MUST include
  every parameter that affects the result (symbol, interval, days,
  *today*). Stale data is worse than a cache miss.
- **Verification before claiming done.** "It works" requires:
  1. server log shows the request returning 200,
  2. response payload spot-checked against a known value,
  3. UI rendered without console errors,
  4. existing features re-tested for regression.

## Pillar 8 — Highly, highly, highly secure
- **Localhost-only by default.** Server **MUST** bind to `127.0.0.1`, never
  `0.0.0.0`. Exposing beyond loopback requires an explicit env flag, TLS
  termination in front, and an auth layer — none of which exist today, so
  today the only legal bind is `127.0.0.1`.
- **Secrets never leave the server.** `ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`,
  `ANGEL_PIN`, `ANGEL_TOTP_SECRET`, `accessToken`, `refreshToken`, `feedToken`,
  session JWTs — **MUST NOT** appear in any HTTP response body, in any
  WebSocket payload, in `/api/health`, in client-side JS, or in browser
  DevTools. Ever.
- **Secrets never appear in logs.** No `print(token)`, no `logger.info(env)`,
  no `repr(creds)`. If a debug dump is needed, redact (`****`) before
  logging.
- **`.env` is sacred.** It **MUST NOT** be committed (`.gitignore` enforced),
  **MUST NOT** be served as a static file, **MUST NOT** be read by any
  endpoint. Only `angel_client._load_env()` reads it.
- **No remote code execution surface.** No `eval()`, no `exec()`, no
  `pickle.loads` on any externally-sourced bytes, no `subprocess` with
  `shell=True`, no `subprocess` arg list built from user input, no
  `os.system`. The single existing `subprocess.run` in `run.py` uses a
  fixed argv (`pip install`) — do not generalise it.
- **Input validation at every boundary.** Every API parameter
  (`symbol`, `interval`, `days`, pane index, watchlist name, drawing
  payload, alert threshold) **MUST** be validated against an allowlist
  or strict regex/type before reaching Angel/yfinance/disk. Reject with
  400, never silently coerce.
- **No XSS.** Anything user-typed or upstream-sourced (symbol names, alert
  notes, watchlist labels, error messages from Angel) **MUST** be rendered
  via `textContent` / safe DOM APIs, never `innerHTML` with concatenation.
  No `eval(jsonString)`, only `JSON.parse`.
- **CORS locked down.** `flask_cors` **MUST** allow only `http://127.0.0.1:<port>`
  and `http://localhost:<port>`. Wildcard `*` is forbidden.
- **No path traversal.** Any path derived from a request parameter
  **MUST** be resolved with `os.path.realpath` and confirmed to live under
  an explicit allowed root before open/read/write. No raw `open(user_path)`.
- **No SSRF.** The server fetches **only** Angel, yfinance, jugaad, and
  vendor CDN endpoints — all hard-coded. The server **MUST NOT** accept a
  URL from the client and fetch it.
- **Dependencies pinned + audited.** `requirements.txt` uses pinned versions.
  Adding a dep requires checking PyPI for known CVEs and a maintained
  release within the last 12 months.
- **No third-party telemetry, analytics, or remote logging.** Zero outbound
  calls except to the four whitelisted upstreams above. No Google Analytics,
  no Sentry, no font CDN, no avatar service, nothing.
- **Browser storage hygiene.** `localStorage` keys (`TRACKED_KEYS`)
  store **only** UI state — never tokens, never PII, never API keys.
  Anything sensitive lives server-side or in `.env`.
- **WebSocket trust boundary.** Server-side Angel WS is server→Angel only;
  the **browser has no WebSocket connection** (Invariant #1). Browser-side
  inputs (`/api/subscribe`, `/api/unsubscribe`, `/api/ticks`) are validated
  identically to other REST endpoints — a malicious client cannot subscribe
  to an arbitrary token to crash the Angel WS pool.
- **Rate-limit / DoS resistance.** A misbehaving tab cannot exhaust the
  server: LRU cache absorbs reload bursts, watchdog timeouts cap upstream
  hangs, sequential pane hydration prevents thundering herds. Adding a new
  endpoint **MUST** preserve this property.
- **Production deployment is out of scope.** Server runs **localhost-only**
  on waitress; do not announce it on the network. If multi-user hosting is
  ever required, add TLS + auth in front before exposing.

---

## Engineering invariants (derived from the pillars)

These are HOW the pillars stay true. Do not regress them silently.

| # | Invariant | Why |
|---|---|---|
| 1 | **No persistent browser↔server connection.** Browser pulls live ticks via 1 Hz `GET /api/ticks?symbols=...`; server runs Angel WS in a daemon thread that writes into an in-memory `_LAST_TICKS` dict. **No socket.io, no WebSocket, no SSE, no long-poll.** | Eliminates URL-bar "loading" spinner, eliminates Werkzeug FD leak, eliminates reconnect-dance after Cmd+Tab / tab background. Self-heal becomes "just resume the setTimeout chain". |
| 2 | **Server runs on `waitress`** (pure-Python threaded WSGI), 8 threads, bound to `127.0.0.1`. **Werkzeug dev server forbidden in production code path.** | No FD leak, no eventlet collision with Angel SDK threads, cross-platform (Win/Mac/Linux). |
| 3 | All Angel SDK calls wrapped in `_call_with_timeout` | Stale TCP sockets in urllib3 pool hang for 60+ s. Watchdog + `_reset_session` is mandatory. |
| 4 | `/api/historical` LRU cache (60 s TTL, 256 entries, keyed `(SYMBOL, interval, days, today)`) | Absorbs reload-bursts so 4 panes reloading the same symbol = 1 Angel call. |
| 5 | `_rsBenchmarkInFlight` Promise dedup in client | 4 panes' RS indicator = 1 benchmark fetch, not 4. |
| 6 | `setChartCount` uses `skipInitialLoad=true` then `await loadChartData(panes[0])` then fan-out | Pane 0 paints before others start fetching. Cache primed for the rest. |
| 7 | `pane._candles` / `pane._volumes` cached client-side | Indicator toggle does **not** refetch. |
| 8 | `socket.bind` port probe; no `lsof` / `os.kill` | Cross-platform. |
| 9 | `/api/health` exposes `angel_session`, `angel_ws_connected`, `last_candle_fetch_ago_sec`, `candle_cache {size,hits,misses,hit_rate}`, `uptime_sec`, `subscribed_tokens` | Operability. UI polls every 10 s; dot is green/amber/red. |
| 10 | Vendored libs first, CDN fallback on `onerror`. **Vendor list = lightweight-charts only.** | App boots offline / behind firewalls. No socket.io vendor. |
| 11 | Re-entrancy guard `_syncing` flag for cross-pane broadcasts | Avoids same-tick echoes that would loop forever. |
| 12 | `/api/subscribe`, `/api/unsubscribe`, `/api/ticks` all reject symbols not matching `^[\^A-Z0-9._-]{1,30}$` | Allowlist input validation per Pillar 8. |
| 13 | Self-heal triggers: `visibilitychange` + `focus` + `pageshow.persisted`, throttled to one heal per 3 s | Covers Chrome tab background, Safari Cmd+Tab (App Nap), Safari bfcache. With polling there is no reconnect to do — heal is just `scheduleHealth(0); scheduleTicks(0)`. |

## Known traps (do not repeat)
- `eventlet.monkey_patch()` deadlocks SmartConnect. **Eventlet is permanently banned.**
- `flask-socketio` long-poll holds a `GET /socket.io/...` request open by design — Chrome's URL bar shows "loading" forever and Werkzeug accumulates CLOSED sockets. **Do not reintroduce socket.io.**
- Angel SDK keeps stale TCP sockets after idle — watchdog mandatory.
- DNS lookup for `api.ipify.org` inside SmartConnect init can fail; falls
  back to local IP. Harmless but noisy in logs.

---

## Change-acceptance checklist

Every PR / commit touching `tradingcharts/`, `angel_client.py`,
`data_provider.py` MUST be able to answer YES to all of these:

- [ ] No new bare network call without a wall-clock timeout.
- [ ] No new platform-specific shell command (lsof, ps, taskkill, etc.).
- [ ] No new heavy JS dep, build step, or framework.
- [ ] No new Python dep without justification.
- [ ] No regression in `/api/historical` cache-hit latency (check
      `/api/health` `candle_cache.hit_rate` after typical use).
- [ ] No regression in first-paint time for a 4-pane reload.
- [ ] Indicator toggle still does **not** call `/api/historical`.
- [ ] `/api/health` still returns 200 with all fields populated.
- [ ] Server log under steady use shows zero 5xx and zero stalled handlers.
- [ ] Page works with internet **off** after first load (vendor fallback).
- [ ] **Numerical spot-check** done: at least one OHLCV / indicator /
      P&L value cross-checked against Angel or NSE ground truth.
- [ ] **No `except: pass`** added without a recovery path or surfaced
      failure (log + `/api/health`).
- [ ] **No new console errors** in browser DevTools after the change.
- [ ] **Regression sweep**: existing features (theme toggle, sync flyout,
      indicator stack, watchlists, drawings, alerts) still work.
- [ ] **No secret in any HTTP response, WS payload, log line, or client JS**
      (grep the diff for `ANGEL_`, `accessToken`, `refreshToken`,
      `feedToken`, `TOTP`).
- [ ] **No new bind to `0.0.0.0`** or wildcard CORS origin.
      `serve(..., host="127.0.0.1", ...)` stays.
- [ ] **No new `eval` / `exec` / `pickle.loads` / `shell=True` /
      `innerHTML` with concatenation.**
- [ ] **Every new API parameter validated** against an allowlist or
      strict type before reaching Angel / yfinance / disk.
- [ ] **No new outbound host.** Only Angel, yfinance, jugaad, and the
      vendored-CDN fallbacks may be contacted from the server or browser.
- [ ] **`.env` still gitignored** and not served by any route.
