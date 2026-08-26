#!/bin/bash
# Start CS resume once incremental is done. Sequential may still write to
# case-insensitive videos/; CS uses videos-cs so they can overlap safely.
set -uo pipefail
export PATH="$HOME/miniconda3/bin:$HOME/miniconda3/envs/dfi/bin:$HOME/.local/bin:$PATH"
ROOT=/Users/mu-mac_3/Projects/video-image-search-indexer-quality
LOG=$ROOT/data/imports/volume-clone.log
CS=$ROOT/backend/data/videos-cs
BUNDLE=$ROOT/data/dfi-videos.sparsebundle

exec >>"$LOG" 2>&1
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CS handoff watcher start pid=$$"

mkdir -p "$CS"
if ! mount | grep -q " on $CS "; then
  hdiutil attach "$BUNDLE" -mountpoint "$CS" -nobrowse || {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CS handoff: mount failed"
    exit 1
  }
fi

# Wait until incremental is gone. Do not block on sequential (different target dir).
for i in $(seq 1 720); do
  incr=$(pgrep -f 'volume-clone-incremental\.sh' | wc -l | tr -d ' ')
  scp_n=$(ps -ax -o command= | grep -E 'scp .*railway-dfi-backend:/app/data/videos/.*backend/data/videos/' | grep -v grep | wc -l | tr -d ' ')
  seqn=$(pgrep -f 'volume-clone-sequential\.sh' | wc -l | tr -d ' ')
  if pgrep -f 'volume-clone-videos-cs\.sh' >/dev/null 2>&1; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CS handoff: videos-cs.sh already running — waiting"
    while pgrep -f 'volume-clone-videos-cs\.sh' >/dev/null 2>&1; do sleep 15; done
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CS handoff: peer CS finished"
    exec sleep 7200
  fi
  if [ "${incr:-0}" -eq 0 ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CS handoff: incremental idle (scp_old=$scp_n seq=$seqn) — launching CS"
    break
  fi
  if [ $((i % 12)) -eq 0 ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CS handoff waiting: incr=$incr scp_old=$scp_n seq=$seqn (tick $i)"
  fi
  sleep 10
done

bash "$ROOT/scripts/volume-clone-videos-cs.sh"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CS handoff: done exit=$?"
exec sleep 7200
