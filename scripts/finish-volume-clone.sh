#!/bin/bash
set -uo pipefail
export PATH="$HOME/miniconda3/bin:$HOME/miniconda3/envs/dfi/bin:$HOME/.local/bin:$PATH"
ROOT="$HOME/Projects/video-image-search-indexer-quality"
LOG="$ROOT/data/imports/volume-clone.log"
mkdir -p "$ROOT/data/imports" "$ROOT/backend/data/videos" "$ROOT/backend/data/thumbnails"
exec >>"$LOG" 2>&1
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] finish-volume-clone start pid=$$"

wait_for_thumbs_tar() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] waiting for existing thumbnail tar/ssh"
  while pgrep -f 'ssh railway-dfi-backend cd /app/data && tar cf - thumbnails' >/dev/null 2>&1 \
     || pgrep -f 'tar xf - -C .*backend/data' >/dev/null 2>&1; do
    sz=$(du -sh "$ROOT/backend/data/thumbnails" 2>/dev/null | awk '{print $1}')
    n=$(find "$ROOT/backend/data/thumbnails" -type f 2>/dev/null | wc -l | tr -d ' ')
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs running: $sz / $n files"
    sleep 60
  done
}

ensure_thumbs() {
  n=$(find "$ROOT/backend/data/thumbnails" -type f 2>/dev/null | wc -l | tr -d ' ')
  szk=$(du -sk "$ROOT/backend/data/thumbnails" 2>/dev/null | awk '{print $1}')
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs status files=$n kb=$szk"
  # Target ~90k files / ~5.2G (~5.2*1024*1024 ≈ 5452595 KB). Accept >=80k or >=4G
  if [ "-e" -ge 80000 ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs sufficient"
    return 0
  fi
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs incomplete; starting tar resume"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new railway-dfi-backend \
    'cd /app/data && tar cf - thumbnails' | tar xf - -C "$ROOT/backend/data"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs resume exit=$?"
  du -sh "$ROOT/backend/data/thumbnails"
  find "$ROOT/backend/data/thumbnails" -type f | wc -l
}

copy_videos() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] listing remote videos"
  ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new railway-dfi-backend \
    'find /app/data/videos -type f -print' > "$ROOT/data/imports/remote-videos.txt"
  total=$(wc -l < "$ROOT/data/imports/remote-videos.txt" | tr -d ' ')
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] remote videos=$total"
  copied=0; skipped=0; failed=0
  while IFS= read -r remote; do
    [ -z "$remote" ] && continue
    rel="${remote#/app/data/videos/}"
    localf="$ROOT/backend/data/videos/$rel"
    mkdir -p "$(dirname "$localf")"
    if [ -s "$localf" ]; then
      skipped=$((skipped+1))
      continue
    fi
    if scp -q -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
         "railway-dfi-backend:$remote" "$localf.tmp" && mv -f "$localf.tmp" "$localf"; then
      copied=$((copied+1))
    else
      failed=$((failed+1))
      rm -f "$localf.tmp"
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FAIL $remote"
    fi
    done_n=$((copied+skipped))
    if [ $((done_n % 5)) -eq 0 ]; then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] videos progress copied=$copied skipped=$skipped failed=$failed / $total"
    fi
  done < "$ROOT/data/imports/remote-videos.txt"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] videos finished copied=$copied skipped=$skipped failed=$failed"
  du -sh "$ROOT/backend/data/videos"
  find "$ROOT/backend/data/videos" -type f | wc -l
}

# Keep postgres alive if needed
"$HOME/miniconda3/envs/dfi/bin/pg_isready" -h 127.0.0.1 -p 5432 >/dev/null 2>&1 \
  || "$HOME/miniconda3/envs/dfi/bin/pg_ctl" -D "$HOME/pgdata/dfi" -l "$HOME/pgdata/dfi.log" -o "-p 5432" start || true

wait_for_thumbs_tar
ensure_thumbs
copy_videos

be=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/health || echo 000)
fe=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3001/ || echo 000)
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] health backend=$be frontend=$fe"
{
  echo "DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "thumbnails=$(du -sh "$ROOT/backend/data/thumbnails" | awk '{print $1}') files=$(find "$ROOT/backend/data/thumbnails" -type f | wc -l | tr -d ' ')"
  echo "videos=$(du -sh "$ROOT/backend/data/videos" | awk '{print $1}') files=$(find "$ROOT/backend/data/videos" -type f | wc -l | tr -d ' ')"
  echo "backend=$be frontend=$fe"
} > "$ROOT/data/imports/VOLUME_CLONE_DONE"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] VOLUME_CLONE_DONE written"
