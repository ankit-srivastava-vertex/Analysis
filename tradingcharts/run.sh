#!/bin/bash
# Run the Trading Charts Dashboard (macOS / Linux)
cd "$(dirname "$0")"
source ../venv/bin/activate 2>/dev/null || true
pip install -q -r requirements.txt 2>/dev/null
python3 app.py || python app.py
