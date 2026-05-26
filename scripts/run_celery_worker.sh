#!/usr/bin/env bash
#
# Long-running Celery worker for background tasks (BACKEND_SCALING_AUDIT.md
# INF-5): push fan-out (SY-2 / WS-3) and media transcoding (SY-1).
#
# Mirrors the other scripts/run_*.sh wrappers: resolves the backend dir from
# this script's location, prefers the project venv's python, logs to
# backend/logs/. Unlike the cron jobs this is a SERVICE (stays running) — run it
# under systemd (deploy/systemd/celery-worker.service) or any supervisor.
#
# Requires CELERY_BROKER_URL (or REDIS_URL) to point at Redis. Without it the
# app is in eager mode and a separate worker isn't used (tasks run inline), so
# this wrapper exits with a clear message rather than idling pointlessly.
#
#   bash backend/scripts/run_celery_worker.sh
#
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -z "${CELERY_BROKER_URL:-}" && -z "${REDIS_URL:-}" ]]; then
  echo "[run_celery_worker] CELERY_BROKER_URL/REDIS_URL unset — app is in eager"
  echo "[run_celery_worker] mode (tasks run inline); no worker needed. Exiting."
  exit 0
fi

# Choose an interpreter: explicit $PYTHON, then the project venv, then python3.
if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif [[ -x "$BACKEND_DIR/venv/bin/python" ]]; then
  PY="$BACKEND_DIR/venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

LOG_DIR="${CELERY_LOG_DIR:-$BACKEND_DIR/logs}"
mkdir -p "$LOG_DIR"

CONCURRENCY="${CELERY_CONCURRENCY:-4}"
LOGLEVEL="${CELERY_LOGLEVEL:-info}"

cd "$BACKEND_DIR"
echo "[$(date -Is)] starting celery worker (py=$PY, concurrency=$CONCURRENCY)" >> "$LOG_DIR/celery_worker.log"
exec "$PY" -m celery -A backend worker \
  --loglevel="$LOGLEVEL" \
  --concurrency="$CONCURRENCY" \
  --logfile="$LOG_DIR/celery_worker.log"
