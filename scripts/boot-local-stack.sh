#!/bin/bash
# Bring the whole local stack up idempotently: app stack + tunnels.
# Safe to run when things are already running — existing screen sessions are
# left exactly as they are.
#
# Used by the LaunchAgent (scripts/install-launch-agent.sh) so the stack and the
# public URLs come back by themselves after a reboot.
set -uo pipefail
export PATH="$HOME/miniconda3/bin:$HOME/miniconda3/envs/dfi/bin:$HOME/.local/bin:$HOME/.bun/bin:$PATH"

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/tunnel-config.sh"

BOOT_LOG=$LOGDIR/boot-local-stack.log
mkdir -p "$LOGDIR"
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$BOOT_LOG"; }

has_screen() { screen -ls 2>/dev/null | grep -q "[.]$1[[:space:]]"; }

if has_screen dfi-stack; then
  log "dfi-stack already running — leaving it alone"
else
  log "starting dfi-stack"
  screen -dmS dfi-stack bash "$SCRIPT_DIR/keep-local-stack.sh"
fi

if has_screen dfi-tunnels; then
  log "dfi-tunnels already running — leaving it alone"
else
  log "starting dfi-tunnels (mode=$TUNNEL_MODE)"
  screen -dmS dfi-tunnels bash "$SCRIPT_DIR/start-https-tunnels.sh"
fi

# Tunnel URLs land a few seconds later; re-sync so env matches reality.
sleep 20
bash "$SCRIPT_DIR/sync-tunnel-env.sh" 2>&1 | tee -a "$BOOT_LOG"
