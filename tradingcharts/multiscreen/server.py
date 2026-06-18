"""
multiscreen/server.py — sidecar Flask server providing 4 isolated workspace
replicas of the main tradingcharts app, without modifying any existing file.

URLs:
    http://localhost:5051/             -> redirects to /w/default
    http://localhost:5051/w/<wsid>     -> chart UI for a workspace

Workspaces:
    default, ws2, ws3, ws4

Architecture:
    - This server runs on port 5051. It does NOT replace the main server
      on port 5050 — it sits next to it.
    - All data calls (/api/symbols, /api/historical, /api/quote, /api/health,
      /api/subscribe, /api/unsubscribe, /api/ticks, /api/search) are
      reverse-proxied to http://127.0.0.1:5050. One Angel WS, one cache,
      shared by all workspaces.
    - /api/state is handled LOCALLY here: each workspace persists to
      multiscreen/state/<wsid>.json. Never touches the main server's state.json.
    - /w/<wsid> serves the original ../static/index.html with a 2-line
      <script> shim injected after <head> — the shim namespaces every
      localStorage key with "tc:<wsid>:" so the four browser tabs cannot
      stomp on each other.
    - /static/<path> serves the SAME files as the main server reads
      (../static/), no duplication.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Optional

from flask import Flask, Response, abort, jsonify, redirect, request, send_from_directory
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TC_DIR = os.path.dirname(HERE)
STATIC_DIR = os.path.join(TC_DIR, "static")
STATE_DIR = os.path.join(HERE, "state")
os.makedirs(STATE_DIR, exist_ok=True)

UPSTREAM = os.environ.get("MULTISCREEN_UPSTREAM", "http://127.0.0.1:5050")
PORT = int(os.environ.get("MULTISCREEN_PORT", "5051"))

WORKSPACES = ("default", "ws2", "ws3", "ws4")
_WSID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")

app = Flask(__name__, static_folder=None)
_state_lock = threading.Lock()


# ───────────────────── helpers ─────────────────────
def _validate(wsid: str) -> str:
    if wsid not in WORKSPACES:
        abort(404, description=f"unknown workspace; valid: {WORKSPACES}")
    if not _WSID_RE.match(wsid):
        abort(400, description="invalid workspace id")
    return wsid


def _state_path(wsid: str) -> str:
    return os.path.join(STATE_DIR, f"{wsid}.json")


def _read_state(wsid: str) -> dict:
    p = _state_path(wsid)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_state(wsid: str, data: dict) -> None:
    p = _state_path(wsid)
    tmp = p + ".tmp"
    with _state_lock:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, p)


# ───────────────────── routes ──────────────────────
@app.route("/")
def root():
    return redirect("/w/default", code=302)


@app.route("/w/<wsid>")
def workspace(wsid: str):
    _validate(wsid)
    index_path = os.path.join(STATIC_DIR, "index.html")
    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()
    inject = (
        f'<script>window.WSID="{wsid}";</script>\n'
        f'<script src="/multiscreen/shim.js"></script>\n'
    )
    # Inject right after the opening <head> tag so the shim runs before
    # any existing inline script in index.html.
    new_html, n = re.subn(r"(<head[^>]*>)", r"\1\n" + inject, html, count=1)
    if n == 0:
        # Fallback: prepend.
        new_html = inject + html
    # no-store disables bfcache, so every refresh runs the hydrate XHR fresh
    # instead of restoring an older in-memory snapshot from cache.
    return Response(
        new_html,
        mimetype="text/html",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/multiscreen/shim.js")
def shim_js():
    return send_from_directory(HERE, "shim.js", mimetype="application/javascript")


@app.route("/static/<path:path>")
def static_proxy(path: str):
    # Serve the SAME static files the main server uses; no duplication.
    return send_from_directory(STATIC_DIR, path)


# ─── per-workspace state (LOCAL — does not touch upstream) ───
@app.route("/api/state", methods=["GET"])
def state_get():
    wsid = request.args.get("wsid", "default")
    _validate(wsid)
    resp = jsonify(_read_state(wsid))
    # Block any browser caching so the boot hydrate XHR can never read a
    # stale response from before the most recent push/beacon.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


@app.route("/api/state", methods=["POST"])
def state_post():
    wsid = request.args.get("wsid", "default")
    _validate(wsid)
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "expected JSON object"}), 400
    _write_state(wsid, data)
    return jsonify({"ok": True})


# ─── reverse proxy for everything else ───────────────────
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length", "host",
}


def _proxy(path: str) -> Response:
    url = f"{UPSTREAM}/{path}"
    try:
        upstream_resp = requests.request(
            method=request.method,
            url=url,
            params=request.args,
            data=request.get_data(),
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            cookies=request.cookies,
            allow_redirects=False,
            timeout=60,
        )
    except requests.RequestException as e:
        return Response(f"upstream error: {e}", status=502, mimetype="text/plain")
    headers = [(k, v) for k, v in upstream_resp.headers.items()
               if k.lower() not in _HOP_BY_HOP]
    return Response(upstream_resp.content, status=upstream_resp.status_code,
                    headers=headers)


@app.route("/api/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def api_proxy(path: str):
    # /api/state is handled above; everything else proxies to upstream.
    return _proxy(f"api/{path}")


@app.route("/multiscreen/health")
def health():
    return jsonify({
        "ok": True,
        "port": PORT,
        "upstream": UPSTREAM,
        "workspaces": list(WORKSPACES),
    })


# ───────────────────── entrypoint ──────────────────
def main():
    try:
        from waitress import serve
        print(f"[multiscreen] serving on http://127.0.0.1:{PORT}  upstream={UPSTREAM}")
        print(f"[multiscreen] workspaces: {', '.join(WORKSPACES)}")
        serve(app, host="127.0.0.1", port=PORT, threads=24)
    except ImportError:
        app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
