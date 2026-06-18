"""Launcher for the Tickertape screener app."""

from __future__ import annotations

import os
import subprocess
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
VENV_DIR = os.path.join(ROOT, ".venv")


def _get_python() -> str:
    if os.name == "nt":
        venv_py = os.path.join(VENV_DIR, "Scripts", "python.exe")
    else:
        venv_py = os.path.join(VENV_DIR, "bin", "python3")
    if os.path.isfile(venv_py):
        return venv_py
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