# multiscreen — 4 isolated workspace replicas of tradingcharts

Sidecar server that hosts four independent dashboards on top of the
existing tradingcharts app **without touching it**.

## URLs
- `http://localhost:5051/w/default`
- `http://localhost:5051/w/ws2`
- `http://localhost:5051/w/ws3`
- `http://localhost:5051/w/ws4`

Each URL is bookmarkable, refresh-safe, and keeps its own panes,
drawings, alerts, indicators, layout, theme, sync, watchlist.

## Run

The main server must be running first (port 5050):

```bash
python tradingcharts/run.py
```

Then in a separate terminal, start multiscreen:

```bash
python tradingcharts/multiscreen/run.py
```

Open the four workspace URLs above in browser tabs.

## How it works
- All `/api/*` calls except `/api/state` are reverse-proxied to
  `http://127.0.0.1:5050`. One Angel WS, one cache, shared by all
  workspaces.
- `/api/state` is handled locally; each workspace persists to
  `multiscreen/state/<wsid>.json`.
- `/w/<wsid>` serves the original `../static/index.html` with a small
  `shim.js` injected after `<head>`. The shim namespaces every
  `localStorage` key with `tc:<wsid>:` so the four tabs cannot stomp
  on each other.
- Static assets are served from `../static/` directly — no duplication.

## Config
- `MULTISCREEN_PORT` (default `5051`)
- `MULTISCREEN_UPSTREAM` (default `http://127.0.0.1:5050`)

## Safety
- The main app at `http://localhost:5050/` is never modified or
  restarted by multiscreen. Killing the multiscreen process leaves the
  main server fully operational.
- `http://localhost:5050/` continues to use the original unified
  `state/state.json`. The four workspace state files in
  `multiscreen/state/` are entirely separate.
