#!/bin/bash
# Wrapper for the daily market analysis pipeline (run_all.py).
# Invoked by launchd (com.analysis.runall) at 18:00 IST Mon-Fri.
#
# Responsibilities:
#   - cd into the project directory
#   - activate the project venv
#   - load .env (EMAIL_*, ANGEL_*) so child processes inherit them
#   - run run_all.py with all output redirected to a timestamped log
#   - re-verify weekday in case the agent fires manually on a weekend

set -u

PROJECT_DIR="/Users/ankit.srivastava/Documents/Analysis"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$LOG_DIR"

TS=$(date +"%Y%m%d_%H%M%S")
LOG_FILE="$LOG_DIR/run_all_${TS}.log"

# Re-check day-of-week (Mon=1 ... Sun=7); skip weekends defensively.
DOW=$(date +%u)
if [ "$DOW" -ge 6 ]; then
    echo "[$(date)] Weekend (DOW=$DOW); skipping run." >> "$LOG_FILE"
    exit 0
fi

cd "$PROJECT_DIR" || exit 1

# Load .env if present (export every KEY=VALUE line).
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# Activate venv.
if [ -f venv/bin/activate ]; then
    # shellcheck disable=SC1091
    . venv/bin/activate
fi

{
    echo "==============================================================="
    echo "Daily Market Analysis Run — $(date)"
    echo "==============================================================="
    python3 run_all.py
    RC=$?
    echo "---------------------------------------------------------------"
    echo "Exit code: $RC"
    echo "Finished : $(date)"
} >> "$LOG_FILE" 2>&1

# Prune logs older than 30 days.
find "$LOG_DIR" -name "run_all_*.log" -type f -mtime +30 -delete 2>/dev/null

exit 0
