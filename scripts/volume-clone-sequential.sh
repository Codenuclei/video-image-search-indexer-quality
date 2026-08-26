#!/bin/bash
set -uo pipefail
export PATH="$HOME/miniconda3/bin:$HOME/.local/bin:$PATH"
ROOT=/Users/mu-mac_3/Projects/video-image-search-indexer-quality
LOG=$ROOT/data/imports/volume-clone.log
exec >>"$LOG" 2>&1
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] sequential clone start pid=$$"

# Keep PG alive
"$HOME/miniconda3/envs/dfi/bin/pg_isready" -h 127.0.0.1 -p 5432 >/dev/null 2>&1 \
  || "$HOME/miniconda3/envs/dfi/bin/pg_ctl" -D "$HOME/pgdata/dfi" -l "$HOME/pgdata/dfi.log" -o "-p 5432" start || true

# --- THUMBS: resume via tar until >=80000 files or 3 attempts ---
for attempt in 1 2 3; do
  n=$(find "$ROOT/backend/data/thumbnails" -type f 2>/dev/null | wc -l | tr -d ' ')
  sz=$(du -sh "$ROOT/backend/data/thumbnails" 2>/dev/null | awk '{print $1}')
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs before attempt=$attempt files=$n size=$sz"
  if [ "${n:-0}" -ge 80000 ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs complete enough"
    break
  fi
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs tar attempt=$attempt"
  ssh -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=12 \
      -o StrictHostKeyChecking=accept-new railway-dfi-backend \
      'cd /app/data && tar cf - thumbnails' \
    | tar xf - -C "$ROOT/backend/data"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs tar attempt=$attempt exit=$?"
done
n=$(find "$ROOT/backend/data/thumbnails" -type f 2>/dev/null | wc -l | tr -d ' ')
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs final files=$n size=$(du -sh "$ROOT/backend/data/thumbnails" | awk '{print $1}')"

# --- VIDEOS: resumable scp ---
[ -s "$ROOT/data/imports/remote-videos.txt" ] \
  || ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new railway-dfi-backend \
       'find /app/data/videos -type f -print' > "$ROOT/data/imports/remote-videos.txt"
total=$(wc -l <"$ROOT/data/imports/remote-videos.txt" | tr -d ' ')
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] videos total=$total"
copied=0; skipped=0; failed=0
while IFS= read -r remote; do
  [ -z "$remote" ] && continue
  rel=${remote#/app/data/videos/}
  localf=$ROOT/backend/data/videos/$rel
  mkdir -p "$(dirname "$localf")"
  if [ -s "$localf" ]; then skipped=$((skipped+1)); continue; fi
  if scp -q -o BatchMode=yes -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new \
       "railway-dfi-backend:$remote" "$localf.tmp" && mv -f "$localf.tmp" "$localf"; then
    copied=$((copied+1))
  else
    failed=$((failed+1)); rm -f "$localf.tmp"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FAIL $remote"
  fi
  if [ $(((copied+skipped) % 5)) -eq 0 ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] videos progress copied=$copied skipped=$skipped failed=$failed / $total"
  fi
done < "$ROOT/data/imports/remote-videos.txt"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] videos finished copied=$copied skipped=$skipped failed=$failed size=$(du -sh "$ROOT/backend/data/videos" | awk '{print $1}')"

n=$(find "$ROOT/backend/data/thumbnails" -type f | wc -l | tr -d ' ')
v=$(find "$ROOT/backend/data/videos" -type f | wc -l | tr -d ' ')
be=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health || echo 000)
{
  echo "DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "thumbnails=$(du -sh "$ROOT/backend/data/thumbnails" | awk '{print $1}') files=$n"
  echo "videos=$(du -sh "$ROOT/backend/data/videos" | awk '{print $1}') files=$v"
  echo "backend=$be"
} > "$ROOT/data/imports/VOLUME_CLONE_DONE"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] VOLUME_CLONE_DONE written"
