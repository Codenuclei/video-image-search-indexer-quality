#!/bin/bash
# Restart ONLY the tunnels (screen session dfi-tunnels) and re-wire env.
# Deliberately leaves the app stack alone: uvicorn stays on :8000 and
# next dev stays on :3001, so anyone testing the app is not interrupted.
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/tunnel-config.sh"

echo "stopping screen session dfi-tunnels"
screen -S dfi-tunnels -X quit >/dev/null 2>&1 || true
# Quick tunnels are anonymous processes; make sure none are orphaned.
pkill -f 'cloudflared --no-autoupdate tunnel' >/dev/null 2>&1 || true
pkill -f 'cloudflared tunnel --url' >/dev/null 2>&1 || true
sleep 2

echo "starting screen session dfi-tunnels (mode=$TUNNEL_MODE)"
screen -dmS dfi-tunnels bash "$SCRIPT_DIR/start-https-tunnels.sh"

# Named hostnames are known instantly; quick tunnels need a few seconds.
for _ in $(seq 1 45); do
  if [ -f "$URL_FILE" ] && grep -q '^FRONTEND_TUNNEL_URL=http' "$URL_FILE"; then
    break
  fi
  sleep 1
done

bash "$SCRIPT_DIR/sync-tunnel-env.sh" --print
echo "app stack untouched — uvicorn :$BE_PORT and next dev :$FE_PORT still running"
