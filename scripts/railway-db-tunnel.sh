#!/usr/bin/env bash
# Keep the Railway Postgres tunnel up on 127.0.0.1:15432.
#
# A plain `ssh -L` here has failed repeatedly in two different ways: the ssh
# process outlives a dead forward (so the port still listens while every
# connection is reset), and Railway's SSH gateway intermittently refuses key
# verification. Both look identical to the backend — connection reset by peer
# on connect — so supervise the tunnel and reconnect instead of one-shotting it.
set -uo pipefail

RAILWAY_SSH_TARGET="${RAILWAY_SSH_TARGET:-70d18de8-4d92-4a95-bde4-68707f473800@ssh.railway.com}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
LOCAL_PORT="${LOCAL_PORT:-15432}"
REMOTE_HOST="${REMOTE_HOST:-127.0.0.1}"
REMOTE_PORT="${REMOTE_PORT:-5432}"
RETRY_DELAY="${RETRY_DELAY:-10}"

echo "tunnel supervisor: 127.0.0.1:${LOCAL_PORT} -> ${REMOTE_HOST}:${REMOTE_PORT} via ${RAILWAY_SSH_TARGET}"

while true; do
  # Reclaim the port from a previous forward that died without exiting.
  for pid in $(lsof -nP -tiTCP:"${LOCAL_PORT}" -sTCP:LISTEN 2>/dev/null); do
    echo "tunnel supervisor: reclaiming port ${LOCAL_PORT} from pid ${pid}"
    kill "${pid}" 2>/dev/null
  done
  sleep 1

  ssh -i "${SSH_KEY}" -N \
    -o StrictHostKeyChecking=accept-new \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 \
    -o ConnectTimeout=15 \
    -L "127.0.0.1:${LOCAL_PORT}:${REMOTE_HOST}:${REMOTE_PORT}" \
    "${RAILWAY_SSH_TARGET}"
  code=$?

  echo "tunnel supervisor: ssh exited (code=${code}); reconnecting in ${RETRY_DELAY}s"
  sleep "${RETRY_DELAY}"
done
