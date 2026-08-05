#!/bin/sh
# Production entrypoint for Railway / Docker.
# Dev still uses: uvicorn app.main:app --reload --port 8000
set -eu

PORT="${PORT:-8000}"
# Prefer WEB_CONCURRENCY (12-factor); fall back to GUNICORN_WORKERS.
# Default 4 (not 24): InsightFace/OpenBLAS + many UvicornWorkers exhausted
# container thread limits. Raise only after OPENBLAS_NUM_THREADS=1 is proven.
WORKERS="${WEB_CONCURRENCY:-${GUNICORN_WORKERS:-4}}"
# Long carousel/Drive jobs can run ~15m; do not kill silent workers early.
TIMEOUT="${GUNICORN_TIMEOUT:-900}"
GRACEFUL="${GUNICORN_GRACEFUL_TIMEOUT:-120}"
KEEPALIVE="${GUNICORN_KEEPALIVE:-5}"

# Prevent OpenBLAS/OpenMP from spawning a thread storm per worker
# (prior crashes: WEB_CONCURRENCY=24 × default BLAS threads).
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"
export VECLIB_MAXIMUM_THREADS="${VECLIB_MAXIMUM_THREADS:-1}"

echo "Starting gunicorn workers=${WORKERS} bind=0.0.0.0:${PORT} timeout=${TIMEOUT}s OPENBLAS_NUM_THREADS=${OPENBLAS_NUM_THREADS} OMP_NUM_THREADS=${OMP_NUM_THREADS}"

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
