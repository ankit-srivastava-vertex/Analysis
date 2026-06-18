"""Launcher for the Tickertape screener app."""

from __future__ import annotations

import os
import subprocess
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _get_python() -> str:
    sub = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python3"
    for name in (".venv", "venv"):
        cand = os.path.join(ROOT, name, sub, exe)
        if os.path.isfile(cand):
            return cand
    return sys.executable


def main() -> None:
    python = _get_python()
    subprocess.run(
        [python, "-m", "pip", "install", "-q", "-r", os.path.join(ROOT, "requirements.txt")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    os.chdir(HERE)
    os.environ["PORT"] = "5052"
    os.execv(python, [python, os.path.join(HERE, "app.py")])


if __name__ == "__main__":
    main()