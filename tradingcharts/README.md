# tradingcharts

Browser-based multi-chart trading dashboard for Indian equities. Self-contained
Flask + vanilla-JS app that runs on top of the parent Analysis project's data
plumbing (Angel One SmartAPI primary, yfinance / jugaad-data fallback).

## URL
- `http://localhost:5050/` — main dashboard

For 4 isolated workspace replicas on a separate port, see
[multiscreen/README.md](multiscreen/README.md).

## Run

```bash
python tradingcharts/run.py
```

The launcher activates the parent venv if present, installs missing deps,
auto-detects a free port (default 5050), and opens the browser. Stop with
Ctrl+C.

Cross-platform shortcuts:
- macOS / Linux: `./tradingcharts/run.sh`
- Windows: double-click `tradingcharts\run.bat`

## Layout

```
tradingcharts/
├── app.py              Flask backend (REST API + static)
├── run.py / run.sh     Cross-platform launchers
│   run.bat
├── requirements.txt    flask, flask-cors, waitress, pandas
├── RULES.md            Non-negotiable engineering rules
├── state/
│   └── state.json      Server-side persisted UI state (atomic writes)
├── static/
│   ├── index.html      Single-page frontend
│   └── drawing-tools.js
├── logs/               Per-day request/error logs
└── multiscreen/        Sidecar for 4 isolated workspaces (port 5051)
```

## Data sources
1. **Angel One SmartAPI** (primary) — full NSE/BSE intraday + historical.
   Requires Angel One demat account + credentials in `../.env`. Module:
   `../angel_client.py`.
2. **jugaad-data** (fallback) — NSE scraper, no auth.
3. **yfinance** (fallback) — global, no auth.

All three are abstracted behind `../data_provider.py`.

## REST endpoints

| Endpoint | Purpose |
| -------- | ------- |
| `GET /api/symbols` | Full NSE symbol list |
| `GET /api/search?q=REL` | Symbol prefix search |
| `GET /api/historical?symbol=&interval=&days=` | OHLCV candles |
| `GET /api/quote?symbol=` | Live LTP / OHLC |
| `GET /api/ticks` | Batched live ticks (1s poll, populated by Angel WS) |
| `POST /api/subscribe` / `unsubscribe` | Manage WS subscriptions |
| `GET / POST /api/state` | Persist UI state to `state/state.json` |
| `GET /api/health` | Server / WS / cache health |

## Frontend features (all in `static/index.html` + `drawing-tools.js`)

- **Chart types:** Candles, Bars, Heikin Ashi (client-side OHLC transform).
- **Themes:** Dark / Light, persisted via `chartTheme`.
- **Layouts:** 1 / 2 / 4 / 6 / 8 panes.
- **Timeframes:** 30m, 1h, 1d, 1w, 1mo. View ranges 1M – 10Y.
- **Indicators:** SmartVPSG (gap/volume markers, 52w stats, R.Vol, optional
  Volume Profile), SupResEPS (MAs 10/20/50/200 + pivots), RSI, MACD, Relative
  Strength vs 9 benchmark indices.
- **Drawings:** 14 TradingView-style tools (trendline, ray, horizontal,
  vertical, parallel channel, rectangle, price/date/date+price ranges, text,
  comment, fib retracement, fib extension). Stable IDs, alert-referenceable.
- **Alerts:** Price / Volume crossings, Drawing-Cross (LTP crosses a
  horizontal/trendline/ray), Once / Hourly / Daily triggers, browser
  notification + audio beep.
- **Watchlists:** up to 45 lists × 450 stocks each, TV-style CSV upload, per-row
  live quote.

## State persistence

UI state is mirrored to `state/state.json` via atomic `os.replace()` under a
threading lock. The set of mirrored keys lives client-side in `TRACKED_KEYS`
inside `static/index.html` — adding a new key requires no backend change.
Survives port flips and cross-day restarts.

## Live ticks

Angel WS thread runs server-side, writes the latest tick into an in-memory
`_LAST_TICKS` dict; browser polls `GET /api/ticks` once per second to pull
LTPs for all visible panes in one round-trip. No socket.io, no long-poll,
no FD leak — served by waitress (threaded pure-Python WSGI).

## Self-healing port

`_ensure_port_free()` kills stale tradingcharts processes on boot; aborts with
a clear error if a foreign process holds 5050. Port override:
`PORT=<n> python run.py`.

## Hardware

| | Idle | Active (4 panes, 5y daily, live ticks) |
| --- | --- | --- |
| Backend RSS | ~120 MB | ~120 MB (stable) |
| Backend CPU | ~0% | <1% (network-bound) |
| Browser tab | ~80 MB | ~250 MB |

Min: 2 cores · 4 GB RAM · 1 GB free disk · 2 Mbps internet.
Comfortable: 4 cores · 8 GB RAM.

## Independence

This project is independent of `run_all.py` (the main Analysis pipeline). It
does not write to `Output/` and is not part of any batch report.

## Rules

See [RULES.md](RULES.md) for the eight non-negotiable engineering pillars
every change must satisfy.
