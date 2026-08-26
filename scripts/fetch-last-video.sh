#!/bin/bash
set -uo pipefail
export PATH="$HOME/miniconda3/bin:$HOME/.local/bin:$PATH"
ROOT=/Users/mu-mac_3/Projects/video-image-search-indexer-quality
CS=$ROOT/backend/data/videos-cs
IMP=$ROOT/data/imports
LOG=$IMP/volume-clone.log
REL='yt:2CL-u9PlMNs.webm'
DST="$CS/$REL"
REMOTE_SIZE=710867717
exec >>"$LOG" 2>&1
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] fetch-last start pid=$$"

mount | grep -q " on $CS " || hdiutil attach "$ROOT/data/dfi-videos.sparsebundle" -mountpoint "$CS" -nobrowse

if [ -s "$DST" ] && [ "$(stat -f%z "$DST")" -eq "$REMOTE_SIZE" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] already complete"
else
  for attempt in 1 2 3 4 5; do
    TMP="$CS/.partial.last.${REL}.$$.${attempt}.tmp"
    rm -f "$TMP"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] attempt $attempt scp $REL"
    if scp -o BatchMode=yes -o ServerAliveInterval=10 -o ServerAliveCountMax=30 -o StrictHostKeyChecking=accept-new \
         "railway-dfi-backend:/app/data/videos/$REL" "$TMP"; then
      sz=$(stat -f%z "$TMP")
      if [ "$sz" -eq "$REMOTE_SIZE" ]; then
        mv -f "$TMP" "$DST"
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] scp success size=$sz"
        break
      fi
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] bad size $sz want $REMOTE_SIZE"
      rm -f "$TMP"
    else
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] scp fail attempt $attempt; trying ssh pipe"
      rm -f "$TMP"
      if ssh -o BatchMode=yes -o ServerAliveInterval=10 -o ServerAliveCountMax=30 railway-dfi-backend \
           "dd if='/app/data/videos/$REL' bs=4M status=none" > "$TMP"; then
        sz=$(stat -f%z "$TMP")
        if [ "$sz" -eq "$REMOTE_SIZE" ]; then
          mv -f "$TMP" "$DST"
          echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] dd pipe success size=$sz"
          break
        fi
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] dd bad size $sz"
        rm -f "$TMP"
      else
        rm -f "$TMP"
      fi
    fi
    sleep 5
  done
fi

python3 - <<'PY'
import time, urllib.request
from pathlib import Path
from collections import defaultdict
root = Path("/Users/mu-mac_3/Projects/video-image-search-indexer-quality")
imp, cs = root/"data/imports", root/"backend/data/videos-cs"
thumbs = root/"backend/data/thumbnails"
remote = [l for l in (imp/"remote-videos-rel.txt").read_text().splitlines() if l.strip()]
exact = sum(1 for r in remote if (cs/r).exists() and (cs/r).stat().st_size > 0)
missing = [r for r in remote if not ((cs/r).exists() and (cs/r).stat().st_size > 0)]
groups = defaultdict(list)
for r in remote: groups[r.casefold()].append(r)
mapped = sum(1 for names in groups.values() if any((cs/n).exists() and (cs/n).stat().st_size>0 for n in names))
tn = sum(1 for _ in thumbs.rglob("*") if _.is_file())
try:
    be = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).status
except Exception:
    be = 0
vn = sum(1 for p in cs.iterdir() if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".tmp"))
import subprocess
size = subprocess.check_output(["du","-sh",str(cs)], text=True).split()[0]
tsize = subprocess.check_output(["du","-sh",str(thumbs)], text=True).split()[0]
print(f"cs_exact={exact}/{len(remote)} groups={mapped}/{len(groups)} missing={missing} be={be}")
(imp/"video-collision-map.txt").write_text(
    "\n".join(f"{k}\t"+"|".join(v) for k,v in sorted(groups.items()) if len(v)>1)+"\n"
)
if exact >= len(remote) and be == 200:
    body = (
        f"DONE {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"status=COMPLETE\n"
        f"thumbnails=done files={tn} size={tsize}\n"
        f"videos_cs={exact}/{len(remote)} files_on_volume={vn} size={size}\n"
        f"VIDEO_CACHE_DIR=./data/videos-cs\n"
        f"backend={be}\n"
        f"collision_handling=case-sensitive-apfs-sparsebundle+sibling-copy\n"
        f"collision_groups=127\n"
    )
    (imp/"VOLUME_CLONE_DONE").write_text(body)
    (imp/"missing-videos-cs.txt").unlink(missing_ok=True)
    print("WROTE_COMPLETE_MARKER")
    print(body)
else:
    (imp/"missing-videos-cs.txt").write_text("\n".join(missing)+"\n")
    print("STILL_INCOMPLETE")
PY
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] fetch-last exit=$?"
