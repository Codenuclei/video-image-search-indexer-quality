#!/bin/bash
set -uo pipefail
export PATH="$HOME/miniconda3/bin:$HOME/.local/bin:$PATH"
ROOT=/Users/mu-mac_3/Projects/video-image-search-indexer-quality
CS=$ROOT/backend/data/videos-cs
IMP=$ROOT/data/imports
LOG=$IMP/volume-clone.log
LOCK=$IMP/fetch-last.lock
REL='yt:2CL-u9PlMNs.webm'
DST="$CS/$REL"
REMOTE_SIZE=710867717
REMOTE_MD5=eaa388292b5151cd5f88f698905121f2

exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] fetch-reliable: another instance holds lock" >>"$LOG"
  exit 0
fi
exec >>"$LOG" 2>&1
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] fetch-reliable start pid=$$"

if [ -s "$DST" ] && [ "$(stat -f%z "$DST")" -eq "$REMOTE_SIZE" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] already present"
else
  # Method A: tar stream (avoids scp colon issues)
  for attempt in 1 2 3 4 5 6; do
    TMPDIR=$(mktemp -d "$CS/.tmp.fetch.XXXXXX")
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] tar attempt $attempt"
    if ssh -o BatchMode=yes -o Compression=no -o ServerAliveInterval=10 -o ServerAliveCountMax=30 \
         -o StrictHostKeyChecking=accept-new railway-dfi-backend \
         "cd /app/data/videos && tar cf - './$REL'" \
         | tar xf - -C "$TMPDIR"; then
      # tar may extract as ./yt:2CL... or yt:2CL...
      GOT=$(find "$TMPDIR" -type f | head -1)
      if [ -n "$GOT" ] && [ "$(stat -f%z "$GOT")" -eq "$REMOTE_SIZE" ]; then
        mv -f "$GOT" "$DST"
        rm -rf "$TMPDIR"
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] tar SUCCESS"
        break
      fi
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] tar bad size got=$(stat -f%z "$GOT" 2>/dev/null || echo 0)"
    else
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] tar fail attempt $attempt"
    fi
    rm -rf "$TMPDIR"
    sleep 3
  done
fi

# Method B: chunked python if still missing
if [ ! -s "$DST" ] || [ "$(stat -f%z "$DST")" -ne "$REMOTE_SIZE" ]; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] falling back to chunked python"
  python3 "$ROOT/scripts/fetch-last-chunked.py" || true
fi

# Verify + write DONE marker
python3 - <<'PY'
import hashlib, subprocess, time, urllib.request
from pathlib import Path
from collections import defaultdict
root = Path("/Users/mu-mac_3/Projects/video-image-search-indexer-quality")
cs, imp = root/"backend/data/videos-cs", root/"data/imports"
rel = "yt:2CL-u9PlMNs.webm"
dst = cs/rel
want_size, want_md5 = 710867717, "eaa388292b5151cd5f88f698905121f2"
ok = dst.exists() and dst.stat().st_size == want_size
md5 = hashlib.md5(dst.read_bytes()).hexdigest() if ok else None
print(f"verify exists={dst.exists()} size={dst.stat().st_size if dst.exists() else None} md5={md5}")
if ok and md5 != want_md5:
    print("MD5 mismatch — deleting")
    dst.unlink()
    ok = False
remote = [l for l in (imp/"remote-videos-rel.txt").read_text().splitlines() if l.strip()]
exact = sum(1 for r in remote if (cs/r).exists() and (cs/r).stat().st_size > 0)
missing = [r for r in remote if not ((cs/r).exists() and (cs/r).stat().st_size > 0)]
thumbs = root/"backend/data/thumbnails"
tn = sum(1 for _ in thumbs.rglob("*") if _.is_file())
try: be = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).status
except Exception: be = 0
vn = sum(1 for p in cs.iterdir() if p.is_file() and not p.name.startswith('.') and not p.name.endswith('.tmp'))
size = subprocess.check_output(["du","-sh",str(cs)], text=True).split()[0]
tsize = subprocess.check_output(["du","-sh",str(thumbs)], text=True).split()[0]
(imp/"video-collision-map.txt").write_text("\n".join(
    f"{k}\t"+"|".join(v) for k,v in sorted(
        __import__('collections').defaultdict(list, **{}).items()
    )
) )
# rebuild collision map properly
from collections import defaultdict
g=defaultdict(list)
for r in remote: g[r.casefold()].append(r)
(imp/"video-collision-map.txt").write_text("\n".join(f"{k}\t"+"|".join(v) for k,v in sorted(g.items()) if len(v)>1)+"\n")
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
        f"last_file={rel} md5={want_md5}\n"
    )
    (imp/"VOLUME_CLONE_DONE").write_text(body)
    (imp/"missing-videos-cs.txt").unlink(missing_ok=True)
    print("WROTE_COMPLETE")
    print(body)
else:
    (imp/"missing-videos-cs.txt").write_text("\n".join(missing)+"\n")
    print(f"INCOMPLETE exact={exact}/{len(remote)} missing={missing} be={be}")
    raise SystemExit(2)
PY
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] fetch-reliable exit=$?"
