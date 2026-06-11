#!/usr/bin/env python3
"""
multiscreen/run.py — independent launcher for the multiscreen sidecar server.

Usage:
    python tradingcharts/multiscreen/run.py

This is strictly separate from tradingcharts/run.py. It does NOT start,
stop, or touch the main server on port 5050 — you must run that yourself
(it provides the data plane that multiscreen proxies to).

Env vars:
    MULTISCREEN_PORT      (default 5051)
    MULTISCREEN_UPSTREAM  (default http://127.0.0.1:5050)
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TC_DIR = os.path.dirname(HERE)
ANALYSIS_DIR = os.path.dirname(TC_DIR)


def _python():
    venv_a = os.path.join(ANALYSIS_DIR, ".venv", "bin", "python3")
    venv_b = os.path.join(ANALYSIS_DIR, "venv", "bin", "python3")
    if sys.platform == "win32":
        venv_a = os.path.join(ANALYSIS_DIR, ".venv", "Scripts", "python.exe")
        venv_b = os.path.join(ANALYSIS_DIR, "venv", "Scripts", "python.exe")
    for p in (venv_a, venv_b):
        if os.path.isfile(p):
            return p
    return sys.executable


def main():
    py = _python()
    # Best-effort dep check (Flask + requests + waitress should already be
    # installed because the main app uses them).
    try:
        subprocess.run(
            [py, "-m", "pip", "install", "-q", "flask", "requests", "waitress"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    except Exception:
        pass
    os.chdir(HERE)
    os.execv(py, [py, os.path.join(HERE, "server.py")])


if __name__ == "__main__":
    main()
