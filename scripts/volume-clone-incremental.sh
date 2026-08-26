#!/bin/bash
set -uo pipefail
export PATH="$HOME/miniconda3/bin:$HOME/miniconda3/envs/dfi/bin:$HOME/.local/bin:$PATH"
ROOT=/Users/mu-mac_3/Projects/video-image-search-indexer-quality
LOG=$ROOT/data/imports/volume-clone.log
IMP=$ROOT/data/imports
LOCAL_THUMBS=$ROOT/backend/data/thumbnails
LOCAL_VIDS=$ROOT/backend/data/videos
SSH=(ssh -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=12 -o StrictHostKeyChecking=accept-new railway-dfi-backend)
SCP=(scp -q -o BatchMode=yes -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new)

mkdir -p "$IMP" "$LOCAL_THUMBS" "$LOCAL_VIDS"
exec >>"$LOG" 2>&1
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] INCREMENTAL clone start pid=$$"

"$HOME/miniconda3/envs/dfi/bin/pg_isready" -h 127.0.0.1 -p 5432 >/dev/null 2>&1 \
  || "$HOME/miniconda3/envs/dfi/bin/pg_ctl" -D "$HOME/pgdata/dfi" -l "$HOME/pgdata/dfi.log" -o "-p 5432" start || true

copy_missing_dir() {
  local remote_root="$1"   # e.g. /app/data/thumbnails
  local local_root="$2"
  local label="$3"
  local parallel="${4:-1}"

  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $label: listing remote"
  "${SSH[@]}" "find '$remote_root' -type f -printf '%P\n'" | sort > "$IMP/remote-$label.txt"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $label: listing local"
  (cd "$local_root" && find . -type f -printf '%P\n' 2>/dev/null || find . -type f | sed 's|^\./||') | sort > "$IMP/local-$label.txt"
  # Missing = in remote not in local
  comm -23 "$IMP/remote-$label.txt" "$IMP/local-$label.txt" > "$IMP/missing-$label.txt"
  local remote_n local_n missing_n
  remote_n=$(wc -l <"$IMP/remote-$label.txt" | tr -d ' ')
  local_n=$(wc -l <"$IMP/local-$label.txt" | tr -d ' ')
  missing_n=$(wc -l <"$IMP/missing-$label.txt" | tr -d ' ')
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $label: remote=$remote_n local=$local_n missing=$missing_n"

  if [ "${missing_n:-0}" -eq 0 ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $label: nothing missing"
    return 0
  fi

  # For many small files: tar only missing paths in batches of 5000
  if [ "$label" = "thumbs" ]; then
    local batch=0
    local total_batches=$(( (missing_n + 4999) / 5000 ))
    split -l 5000 "$IMP/missing-$label.txt" "$IMP/missing-$label-batch-"
    for batchfile in "$IMP"/missing-$label-batch-*; do
      batch=$((batch+1))
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $label: tar batch $batch/$total_batches ($(wc -l <"$batchfile" | tr -d ' ') files)"
      # Upload list and stream tar of only those relative paths
      "${SCP[@]}" "$batchfile" "railway-dfi-backend:/tmp/missing-$label.txt"
      "${SSH[@]}" "cd '$remote_root' && tar cf - -T /tmp/missing-$label.txt" \
        | tar xf - -C "$local_root"
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $label: batch $batch exit=$? local_now=$(find "$local_root" -type f | wc -l | tr -d ' ') size=$(du -sh "$local_root" | awk '{print $1}')"
      rm -f "$batchfile"
    done
    rm -f /tmp/missing-$label.txt 2>/dev/null || true
    return 0
  fi

  # Videos: parallel scp of missing only
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $label: parallel scp workers=$parallel"
  copied=0; failed=0
  export ROOT SSH_HOST=railway-dfi-backend remote_root local_root LOG
  # Use xargs -P for parallelism
  export PATH
  cat "$IMP/missing-$label.txt" | xargs -P "$parallel" -I{} bash -c '
    rel="$1"
    remote="'"$remote_root"'/$rel"
    localf="'"$local_root"'/$rel"
    mkdir -p "$(dirname "$localf")"
    if [ -s "$localf" ]; then exit 0; fi
    if scp -q -o BatchMode=yes -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new \
         "railway-dfi-backend:$remote" "$localf.tmp" && mv -f "$localf.tmp" "$localf"; then
      echo OK
    else
      rm -f "$localf.tmp"
      echo FAIL "$rel" >> "'"$LOG"'"
      exit 1
    fi
  ' _ {} | while read -r line; do
    if [ "$line" = OK ]; then copied=$((copied+1)); else failed=$((failed+1)); fi
    if [ $(((copied+failed) % 5)) -eq 0 ]; then
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $label progress copied=$copied failed=$failed / $missing_n"
    fi
  done
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $label scp phase done size=$(du -sh "$local_root" | awk '{print $1}') files=$(find "$local_root" -type f | wc -l | tr -d ' ')"
}

# macOS find lacks -printf; use python for local listing consistency
python3 - <<'PY'
import os
from pathlib import Path
root = Path(os.environ.get("ROOT", "/Users/mu-mac_3/Projects/video-image-search-indexer-quality"))
# placeholder - listing done in bash with sed fallback
PY

# Fix local listing without GNU -printf
list_local() {
  local dir="$1" out="$2"
  (cd "$dir" && find . -type f | sed 's|^\./||' | sort) > "$out"
}

# Override thumbs/videos with correct local listing helpers inline
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs: listing remote"
"${SSH[@]}" 'find /app/data/thumbnails -type f | sed "s|^/app/data/thumbnails/||"' | sort > "$IMP/remote-thumbs.txt"
list_local "$LOCAL_THUMBS" "$IMP/local-thumbs.txt"
comm -23 "$IMP/remote-thumbs.txt" "$IMP/local-thumbs.txt" > "$IMP/missing-thumbs.txt"
remote_n=$(wc -l <"$IMP/remote-thumbs.txt" | tr -d ' ')
local_n=$(wc -l <"$IMP/local-thumbs.txt" | tr -d ' ')
missing_n=$(wc -l <"$IMP/missing-thumbs.txt" | tr -d ' ')
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs: remote=$remote_n local=$local_n missing=$missing_n"

if [ "${missing_n:-0}" -gt 0 ]; then
  rm -f "$IMP"/missing-thumbs-batch-*
  split -l 4000 "$IMP/missing-thumbs.txt" "$IMP/missing-thumbs-batch-"
  batches=( "$IMP"/missing-thumbs-batch-* )
  total_batches=${#batches[@]}
  i=0
  for batchfile in "${batches[@]}"; do
    i=$((i+1))
    bn=$(wc -l <"$batchfile" | tr -d ' ')
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs: tar-missing batch $i/$total_batches files=$bn"
    "${SCP[@]}" "$batchfile" "railway-dfi-backend:/tmp/missing-thumbs.txt"
    "${SSH[@]}" 'cd /app/data/thumbnails && tar cf - -T /tmp/missing-thumbs.txt' \
      | tar xf - -C "$LOCAL_THUMBS"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs: batch $i done local_files=$(find "$LOCAL_THUMBS" -type f | wc -l | tr -d ' ') size=$(du -sh "$LOCAL_THUMBS" | awk '{print $1}')"
  done
  rm -f "$IMP"/missing-thumbs-batch-*
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] thumbs: complete"
fi

# Videos go to case-sensitive sparsebundle (macOS APFS default is case-insensitive;
# Railway has many .mov/.MOV and .mp4/.MP4 pairs that collide otherwise).
LOCAL_VIDS_CS=$ROOT/backend/data/videos-cs
BUNDLE=$ROOT/data/dfi-videos.sparsebundle
mkdir -p "$LOCAL_VIDS_CS"
if ! mount | grep -q " on $LOCAL_VIDS_CS "; then
  hdiutil attach "$BUNDLE" -mountpoint "$LOCAL_VIDS_CS" || {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] videos: CS mount FAILED — falling back to handoff script"
  }
fi
if grep -q '^VIDEO_CACHE_DIR=./data/videos$' "$ROOT/backend/.env" 2>/dev/null; then
  perl -i -pe 's|^VIDEO_CACHE_DIR=./data/videos$|VIDEO_CACHE_DIR=./data/videos-cs|' "$ROOT/backend/.env"
fi

# Prefer dedicated CS resume (seed + one download per casefold group + sibling copy)
if mount | grep -q " on $LOCAL_VIDS_CS "; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] videos: delegating to volume-clone-videos-cs.sh"
  bash "$ROOT/scripts/volume-clone-videos-cs.sh"
else
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] videos: listing (legacy case-insensitive path)"
  "${SSH[@]}" 'find /app/data/videos -type f | sed "s|^/app/data/videos/||"' | sort > "$IMP/remote-videos-rel.txt"
  list_local "$LOCAL_VIDS" "$IMP/local-videos.txt"
  # Deduplicate missing by casefold so .mov/.MOV don't race the same APFS inode
  python3 - <<'PY'
from pathlib import Path
imp = Path("/Users/mu-mac_3/Projects/video-image-search-indexer-quality/data/imports")
remote = (imp / "remote-videos-rel.txt").read_text().splitlines()
local = set((imp / "local-videos.txt").read_text().splitlines())
seen = set()
out = []
for r in remote:
    if not r or r in local:
        continue
    k = r.casefold()
    if k in seen:
        continue
    # skip if any case variant already local
    if any(l.casefold() == k for l in local):
        continue
    seen.add(k)
    out.append(r)
(imp / "missing-videos.txt").write_text("\n".join(out) + ("\n" if out else ""))
print(f"legacy missing unique casefold={len(out)}")
PY
  vm=$(wc -l <"$IMP/missing-videos.txt" | tr -d ' ')
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] videos: missing=$vm / $(wc -l <"$IMP/remote-videos-rel.txt" | tr -d ' ')"
  if [ "${vm:-0}" -gt 0 ]; then
    while IFS= read -r rel; do
      [ -z "$rel" ] && continue
      while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge 3 ]; do
        wait -n 2>/dev/null || sleep 1
      done
      (
        # Hash-based temp: case variants of the same basename must not share a temp on APFS
        localf="$LOCAL_VIDS/$rel"
        tmp="$LOCAL_VIDS/.partial.$(printf '%s' "$rel" | shasum -a 256 | awk '{print $1}').$$.tmp"
        mkdir -p "$(dirname "$localf")"
        if [ -s "$localf" ]; then exit 0; fi
        if scp -q -o BatchMode=yes -o ServerAliveInterval=30 -o StrictHostKeyChecking=accept-new \
             "railway-dfi-backend:/app/data/videos/$rel" "$tmp" && mv -f "$tmp" "$localf"; then
          echo OK
        else
          rm -f "$tmp"; echo FAIL
        fi
      ) &
    done < "$IMP/missing-videos.txt"
    wait
  fi
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] videos: finished size=$(du -sh "$LOCAL_VIDS" | awk '{print $1}') files=$(find "$LOCAL_VIDS" -type f | wc -l | tr -d ' ')"
  n=$(find "$LOCAL_THUMBS" -type f | wc -l | tr -d ' ')
  v=$(find "$LOCAL_VIDS" -type f | wc -l | tr -d ' ')
  be=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health || echo 000)
  {
    echo "DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "thumbnails=$(du -sh "$LOCAL_THUMBS" | awk '{print $1}') files=$n"
    echo "videos=$(du -sh "$LOCAL_VIDS" | awk '{print $1}') files=$v"
    echo "backend=$be"
    echo "collision_handling=legacy-casefold-dedupe (CS mount unavailable)"
  } > "$IMP/VOLUME_CLONE_DONE"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] VOLUME_CLONE_DONE files_thumbs=$n files_videos=$v be=$be"
fi
