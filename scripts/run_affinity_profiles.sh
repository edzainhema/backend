#!/usr/bin/env bash
#
# Nightly taste-profile precompute for the home feed's activity rail
# (ACTIVITY_AND_FEED_AUDIT.md item C4). Companion to run_collaborative_recs.sh
# — same robustness shape (path resolution, venv, flock, logging).
#
# Run by hand any time (also the easiest way to seed the table the first
# time, rather than waiting for the first scheduled run):
#
#     bash backend/scripts/run_affinity_profiles.sh
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$BACKEND_DIR/venv/bin/python" ]]; then
  PY="$BACKEND_DIR/venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

LOG_DIR="${CF_LOG_DIR:-$BACKEND_DIR/logs}"
LOG_FILE="$LOG_DIR/affinity_profiles.log"
LOCK_FILE="${AFFINITY_LOCK_FILE:-/tmp/affinity_profiles.lock}"
mkdir -p "$LOG_DIR"

echo "[$(date -Is)] starting build_affinity_profiles (py=$PY)" >> "$LOG_FILE"

if command -v flock >/dev/null 2>&1; then
  if flock -n "$LOCK_FILE" "$PY" "$BACKEND_DIR/manage.py" build_affinity_profiles >> "$LOG_FILE" 2>&1; then
    echo "[$(date -Is)] finished build_affinity_profiles" >> "$LOG_FILE"
  else
    echo "[$(date -Is)] skipped (already running) or failed — see log above" >> "$LOG_FILE"
  fi
else
  if "$PY" "$BACKEND_DIR/manage.py" build_affinity_profiles >> "$LOG_FILE" 2>&1; then
    echo "[$(date -Is)] finished build_affinity_profiles" >> "$LOG_FILE"
  else
    echo "[$(date -Is)] failed — see log above" >> "$LOG_FILE"
  fi
fi
