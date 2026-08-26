#!/bin/bash
# Copy Railway videos onto a case-sensitive APFS sparsebundle so .mov/.MOV
# (and similar) pairs can coexist. Seeds from any existing case-insensitive
# downloads, downloads each casefold group once, then clones siblings locally.
set -uo pipefail
export PATH="$HOME/miniconda3/bin:$HOME/miniconda3/envs/dfi/bin:$HOME/.local/bin:$PATH"
ROOT=/Users/mu-mac_3/Projects/video-image-search-indexer-quality
CS=$ROOT/backend/data/videos-cs
OLD=$ROOT/backend/data/videos
IMP=$ROOT/data/imports
LOG=$IMP/volume-clone.log
BUNDLE=$ROOT/data/dfi-videos.sparsebundle

mkdir -p "$ROOT/backend/data" "$IMP"
exec >>"$LOG" 2>&1
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CS-video clone start pid=$$"

if ! mount | grep -q " on $CS "; then
  mkdir -p "$CS"
  hdiutil attach "$BUNDLE" -mountpoint "$CS" || {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CS mount FAILED"
    exit 1
  }
fi

# Point backend at the case-sensitive volume (old dir left intact)
if grep -q '^VIDEO_CACHE_DIR=./data/videos$' "$ROOT/backend/.env" 2>/dev/null; then
  perl -i -pe 's|^VIDEO_CACHE_DIR=./data/videos$|VIDEO_CACHE_DIR=./data/videos-cs|' "$ROOT/backend/.env"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] VIDEO_CACHE_DIR -> ./data/videos-cs"
fi

ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new railway-dfi-backend \
  'find /app/data/videos -type f | sed "s|^/app/data/videos/||"' | sort > "$IMP/remote-videos-rel.txt"

python3 <<'PY'
import shutil
import subprocess
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

root = Path("/Users/mu-mac_3/Projects/video-image-search-indexer-quality")
cs = root / "backend/data/videos-cs"
old = root / "backend/data/videos"
imp = root / "data/imports"
log_path = imp / "volume-clone.log"
remote = [l for l in (imp / "remote-videos-rel.txt").read_text().splitlines() if l.strip()]


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}"
    print(line, flush=True)
    with log_path.open("a") as f:
        f.write(line + "\n")


groups: dict[str, list[str]] = defaultdict(list)
for r in remote:
    groups[r.casefold()].append(r)

# Seed CS from old case-insensitive dir (one physical file can fill all casings)
seeded = 0
if old.exists():
    donors: dict[str, Path] = {}
    for p in old.iterdir():
        if not p.is_file() or p.name.endswith(".tmp") or p.stat().st_size <= 0:
            continue
        donors.setdefault(p.name.casefold(), p)
    for key, names in groups.items():
        src = donors.get(key)
        if not src:
            continue
        for name in names:
            dst = cs / name
            if dst.exists() and dst.stat().st_size > 0:
                continue
            try:
                shutil.copy2(src, dst)
                seeded += 1
            except Exception as e:
                log(f"CS seed fail {name}: {e}")
log(f"CS seeded {seeded} names from old videos dir")

# Prefer local sibling copy before any download
repaired = 0
for key, names in groups.items():
    donor = next((s for s in names if (cs / s).exists() and (cs / s).stat().st_size > 0), None)
    if not donor:
        continue
    for s in names:
        dst = cs / s
        if dst.exists() and dst.stat().st_size > 0:
            continue
        shutil.copy2(cs / donor, dst)
        repaired += 1
log(f"CS sibling pre-repair={repaired}")

need_download: list[str] = []
for key, names in groups.items():
    if any((cs / s).exists() and (cs / s).stat().st_size > 0 for s in names):
        continue
    need_download.append(names[0])

log(f"CS need scp downloads: {len(need_download)} unique files (of {len(remote)} remote names / {len(groups)} casefold groups)")


def scp_one(rel: str) -> bool:
    import os

    dst = cs / rel
    # Unique temp per exact remote name (safe on case-sensitive volume)
    safe = rel.replace("/", "_")
    tmp = cs / f".partial.{safe}.{os.getpid()}.{time.time_ns()}.tmp"
    if dst.exists() and dst.stat().st_size > 0:
        return True
    cmd = [
        "scp",
        "-q",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"railway-dfi-backend:/app/data/videos/{rel}",
        str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True)
        tmp.replace(dst)
        return True
    except Exception as e:
        log(f"CS FAIL {rel}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


ok = fail = 0
with ThreadPoolExecutor(max_workers=3) as ex:
    futs = {ex.submit(scp_one, rel): rel for rel in need_download}
    total = len(futs)
    for i, fut in enumerate(as_completed(futs), 1):
        rel = futs[fut]
        if fut.result():
            ok += 1
            for s in groups[rel.casefold()]:
                dst = cs / s
                if dst.exists() and dst.stat().st_size > 0:
                    continue
                try:
                    shutil.copy2(cs / rel, dst)
                except Exception as e:
                    log(f"CS sibling copy fail {s}: {e}")
        else:
            fail += 1
        if i % 5 == 0 or i == total:
            on_cs = sum(1 for p in cs.iterdir() if p.is_file() and not p.name.endswith(".tmp"))
            log(f"CS download progress {i}/{total} ok={ok} fail={fail} files_on_cs={on_cs}")

repaired2 = 0
for key, names in groups.items():
    donor = next((s for s in names if (cs / s).exists() and (cs / s).stat().st_size > 0), None)
    if not donor:
        continue
    for s in names:
        dst = cs / s
        if dst.exists() and dst.stat().st_size > 0:
            continue
        shutil.copy2(cs / donor, dst)
        repaired2 += 1
log(f"CS final sibling repair={repaired2}")

exact = sum(1 for r in remote if (cs / r).exists() and (cs / r).stat().st_size > 0)
missing = [r for r in remote if not ((cs / r).exists() and (cs / r).stat().st_size > 0)]
(imp / "video-collision-map.txt").write_text(
    "\n".join(
        f"{key}\t" + "|".join(names) + ("\tPRESENT" if any((cs / n).exists() for n in names) else "\tMISSING")
        for key, names in sorted(groups.items())
        if len(names) > 1
    )
    + "\n"
)
log(f"CS exact present {exact}/{len(remote)} missing={len(missing)}")

try:
    be = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).status
except Exception:
    be = 0

thumbs = root / "backend/data/thumbnails"
tn = sum(1 for _ in thumbs.rglob("*") if _.is_file())
vn = sum(1 for p in cs.iterdir() if p.is_file() and not p.name.endswith(".tmp"))
(imp / "VOLUME_CLONE_DONE").write_text(
    f"DONE {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
    f"thumbnails=done files={tn}\n"
    f"videos_cs={exact}/{len(remote)} files_on_volume={vn}\n"
    f"VIDEO_CACHE_DIR=./data/videos-cs\n"
    f"backend={be}\n"
    f"collision_handling=case-sensitive-apfs-sparsebundle+sibling-copy\n"
    f"collision_map={imp / 'video-collision-map.txt'}\n"
)
log(f"VOLUME_CLONE_DONE written exact={exact}/{len(remote)} be={be}")
if missing:
    (imp / "missing-videos-cs.txt").write_text("\n".join(missing) + "\n")
    log(f"CS still missing listed in missing-videos-cs.txt")
    raise SystemExit(2)
PY
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] CS-video clone exit=$?"
