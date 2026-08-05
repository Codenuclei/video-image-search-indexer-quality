#!/bin/sh
# Production entrypoint for Railway / Docker.
# Dev still uses: uvicorn app.main:app --reload --port 8000
set -eu

PORT="${PORT:-8000}"
# Prefer WEB_CONCURRENCY (12-factor); fall back to GUNICORN_WORKERS; default 24 cores.
WORKERS="${WEB_CONCURRENCY:-${GUNICORN_WORKERS:-24}}"
# Long carousel/Drive jobs can run ~15m; do not kill silent workers early.
TIMEOUT="${GUNICORN_TIMEOUT:-900}"
GRACEFUL="${GUNICORN_GRACEFUL_TIMEOUT:-120}"
KEEPALIVE="${GUNICORN_KEEPALIVE:-5}"

echo "Starting gunicorn workers=${WORKERS} bind=0.0.0.0:${PORT} timeout=${TIMEOUT}s"

exec gunicorn app.main:app \
  -k uvicorn.workers.UvicornWorker \
  -w "${WORKERS}" \
  -b "0.0.0.0:${PORT}" \
  --timeout "${TIMEOUT}" \
  --graceful-timeout "${GRACEFUL}" \
  --keep-alive "${KEEPALIVE}" \
  --access-logfile - \
  --error-logfile - \
  --capture-output \
  --log-level info
