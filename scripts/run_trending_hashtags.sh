#!/usr/bin/env bash
#
# Rolling trending-hashtags recompute for the home feed's activity rail
# (ACTIVITY_AND_FEED_AUDIT.md item D2). Companion to run_affinity_profiles.sh
# / run_drain_impressions.sh — same robustness shape (path resolution, venv,
# flock, logging).
#
# Unlike the nightly jobs this runs on a tight cadence (every few minutes);
# flock prevents overlapping runs if one tick is slow. If it stops, the cached
# trending map simply expires and the rail reverts to no boost — nothing
# breaks. Run by hand any time (also the easiest way to seed the cache):
#
#     bash backend/scripts/run_trending_hashtags.sh
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
LOG_FILE="$LOG_DIR/trending_hashtags.log"
LOCK_FILE="${TRENDING_HASHTAGS_LOCK_FILE:-/tmp/trending_hashtags.lock}"
mkdir -p "$LOG_DIR"

echo "[$(date -Is)] starting build_trending_hashtags (py=$PY)" >> "$LOG_FILE"

if command -v flock >/dev/null 2>&1; then
  if flock -n "$LOCK_FILE" "$PY" "$BACKEND_DIR/manage.py" build_trending_hashtags >> "$LOG_FILE" 2>&1; then
    echo "[$(date -Is)] finished build_trending_hashtags" >> "$LOG_FILE"
  else
    echo "[$(date -Is)] skipped (already running) or failed — see log above" >> "$LOG_FILE"
  fi
else
  if "$PY" "$BACKEND_DIR/manage.py" build_trending_hashtags >> "$LOG_FILE" 2>&1; then
    echo "[$(date -Is)] finished build_trending_hashtags" >> "$LOG_FILE"
  else
    echo "[$(date -Is)] failed — see log above" >> "$LOG_FILE"
  fi
fi
