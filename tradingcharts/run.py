#!/usr/bin/env python3
"""
tradingcharts/run.py — Universal cross-platform launcher for Trading Charts Dashboard.
================================================================================

WHAT THIS PROJECT DOES
----------------------
A self-contained, browser-based multi-chart trading dashboard for Indian equities.
Displays interactive OHLCV candlestick charts with TradingView-style UI, including:
  - Multi-pane grid layout (1, 2, 4, 6, or 8 charts simultaneously)
  - Symbol search across all NSE/BSE equities
  - Multiple timeframes: 1m, 5m, 15m, 30m, 1h, 1D, 1W, 1M
  - Live quote ticker (LTP, change, OHLC)
  - Drawing tools (trendlines, etc.) via drawing-tools.js
  - Dark theme (TradingView-inspired)

PROJECT STRUCTURE
-----------------
  tradingcharts/
  ├── run.py              ← THIS FILE: cross-platform launcher
  ├── run.sh              ← Shell launcher for macOS / Linux
  ├── run.bat             ← Batch launcher for Windows (double-click)
  ├── app.py              ← Flask backend (REST API + static file server)
  ├── requirements.txt    ← Python deps: flask, flask-cors, pandas
  └── static/
      ├── index.html      ← Single-page frontend (HTML/CSS/JS)
      └── drawing-tools.js← Chart annotation/drawing tool logic

DATA SOURCES (via parent project's modules)
-------------------------------------------
  1. Angel One SmartAPI (primary) — Free historical OHLCV for all NSE/BSE stocks.
     Requires: Angel One demat account + SmartAPI credentials in ../.env
     Module: ../angel_client.py
  2. jugaad-data (fallback #2) — Scrapes NSE for daily OHLCV. No auth needed.
  3. Yahoo Finance / yfinance (fallback #3) — Global fallback, no auth.
  All three are abstracted behind ../data_provider.py which tries them in order.

HOW FILES CONNECT (WORKFLOW)
----------------------------
  1. run.py (or run.sh / run.bat) installs deps and launches app.py
  2. app.py starts a Flask server on an auto-detected free port (default: 5050)
  3. app.py imports ../data_provider.py and ../angel_client.py from the parent
     Analysis project (adds parent dir to sys.path)
  4. Browser opens automatically to http://localhost:<port>
  5. static/index.html loads:
     - lightweight-charts v4.1.1 (TradingView's open-source charting library, via CDN)
     - drawing-tools.js (local drawing annotations)
  6. Frontend calls these REST endpoints on the Flask backend:
     - GET /api/symbols       → Full list of available NSE symbols
     - GET /api/search?q=REL  → Symbol search by prefix
     - GET /api/historical?symbol=RELIANCE&interval=1d&days=90 → OHLCV candles
     - GET /api/quote?symbol=RELIANCE → Live LTP/OHLC quote
  7. Backend fetches data via Angel One → jugaad → yfinance fallback chain
  8. Frontend renders candles using lightweight-charts library

DEPENDENCIES
------------
  Python (≥3.8):
    - flask (≥2.3)       — Web server
    - flask-cors (≥4.0)  — Cross-origin requests (dev convenience)
    - pandas             — DataFrame handling for OHLCV
  JavaScript (CDN, no npm/node required):
    - lightweight-charts v4.1.1 (https://unpkg.com/lightweight-charts@4.1.1)
  Optional (for full data access):
    - smartapi-python, pyotp — Angel One SmartAPI (in parent venv)
    - jugaad-data            — NSE scraper fallback
    - yfinance               — Yahoo Finance fallback

ENVIRONMENT / CONFIGURATION
----------------------------
  - NO configuration files needed to run.
  - If ../.env exists with Angel One credentials, full intraday + historical data
    is available. Without it, falls back to yfinance (daily data only).
  - Port is auto-detected: tries 5050, then 5051-5053, then any free port.
  - Override port: set PORT=<number> environment variable.

HOW TO RUN
----------
  OPTION 1 — Universal (all OS):
      cd tradingcharts/
      python3 run.py          # or: python run.py (on Windows)

  OPTION 2 — macOS / Linux:
      ./tradingcharts/run.sh

  OPTION 3 — Windows:
      Double-click tradingcharts\\run.bat

  All three methods:
    1. Activate the parent project's virtualenv (if present)
    2. Install/upgrade Python dependencies (silently)
    3. Start the Flask server on an available port
    4. Auto-open the dashboard in your default browser
    5. Print the URL to terminal for reference

  To stop: Ctrl+C in the terminal.

NOTES
-----
  - This project is INDEPENDENT of run_all.py (the main Analysis pipeline).
    It does not generate any output files or interact with the Output/ folder.
  - It is a real-time interactive tool, not a batch report generator.
  - The parent venv (../venv/) has all required packages pre-installed.
  - Works offline for cached/local data; needs internet for live symbol fetch.
================================================================================
"""
import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(os.path.dirname(HERE), "venv")


def _get_python():
    """Return venv python if available, else current interpreter."""
    if sys.platform == "win32":
        venv_py = os.path.join(VENV_DIR, "Scripts", "python.exe")
    else:
        venv_py = os.path.join(VENV_DIR, "bin", "python3")
    if os.path.isfile(venv_py):
        return venv_py
    return sys.executable


def main():
    python = _get_python()

    # Install deps quietly
    subprocess.run(
        [python, "-m", "pip", "install", "-q", "-r",
         os.path.join(HERE, "requirements.txt")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    # Run the app
    os.chdir(HERE)
    os.execv(python, [python, os.path.join(HERE, "app.py")])


if __name__ == "__main__":
    main()
