#!/bin/bash
# Durable local FE/BE for LAN access. Run via: screen -dmS dfi-stack bash this-script
set -uo pipefail
export PATH="$HOME/miniconda3/bin:$HOME/miniconda3/envs/dfi/bin:$HOME/.local/bin:$HOME/.bun/bin:$PATH"
ROOT=/Users/mu-mac_3/Projects/video-image-search-indexer-quality
LOGDIR=$ROOT/data/imports
BE_LOG=$LOGDIR/backend-uvicorn.log
FE_LOG=$LOGDIR/frontend-next.log
KEEP_LOG=$LOGDIR/keep-local-stack.log
CS=$ROOT/backend/data/videos-cs
BUNDLE=$ROOT/data/dfi-videos.sparsebundle

mkdir -p "$LOGDIR"
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$KEEP_LOG"; }

# Postgres
if ! pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
  log "starting postgres"
  pg_ctl -D "$HOME/pgdata/dfi" -l "$HOME/pgdata/dfi.log" -o "-p 5432" start || true
  sleep 2
fi

# Case-sensitive video volume
if ! mount | grep -q " on $CS "; then
  log "mounting videos-cs"
  mkdir -p "$CS"
  hdiutil attach "$BUNDLE" -mountpoint "$CS" -nobrowse || log "WARN mount failed"
fi

# All public-URL wiring (frontend/.env.local + backend/.env) is derived from one
# source of truth — see scripts/tunnel-config.sh and docs/local-tunnels.md.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/tunnel-config.sh"
bash "$SCRIPT_DIR/sync-tunnel-env.sh" 2>&1 | tee -a "$KEEP_LOG"

start_be() {
  log "starting backend on 0.0.0.0:${BE_PORT}"
  cd "$ROOT/backend"
  ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$BE_PORT" >>"$BE_LOG" 2>&1
  rc=$?
  log "backend exited rc=$rc"
  return $rc
}

start_fe() {
  log "starting frontend on 0.0.0.0:${FE_PORT}"
  cd "$ROOT/frontend"
  npx next dev -H 0.0.0.0 -p "$FE_PORT" >>"$FE_LOG" 2>&1
  rc=$?
  log "frontend exited rc=$rc"
  return $rc
}

# Respawn loops in background; this script then sleeps forever
(
  while true; do
    start_be
    sleep 2
  done
) &
BE_WATCH=$!

(
  while true; do
    start_fe
    sleep 2
  done
) &
FE_WATCH=$!

log "watchers be=$BE_WATCH fe=$FE_WATCH — sleeping"
# Keep screen session alive
while true; do
  sleep 3600
done
