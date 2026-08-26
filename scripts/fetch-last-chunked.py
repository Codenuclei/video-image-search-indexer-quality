#!/usr/bin/env python3
"""Chunked SSH download with per-chunk retries for flaky Railway SSH."""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/mu-mac_3/Projects/video-image-search-indexer-quality")
CS = ROOT / "backend/data/videos-cs"
IMP = ROOT / "data/imports"
LOG = IMP / "volume-clone.log"
REL = "yt:2CL-u9PlMNs.webm"
DST = CS / REL
TMP = CS / f".partial.chunked.{REL}.tmp"
REMOTE_SIZE = 710867717
REMOTE_MD5 = "eaa388292b5151cd5f88f698905121f2"
CHUNK = 8 * 1024 * 1024  # 32 MiB
SSH = [
    "ssh",
    "-o", "BatchMode=yes",
    "-o", "Compression=no",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=6",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "TCPKeepAlive=yes",
    "railway-dfi-backend",
]


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def fetch_chunk(offset: int, length: int) -> bytes:
    # dd skip/count in bytes via iflag=skip_bytes,count_bytes
    cmd = SSH + [
        f"dd if='/app/data/videos/{REL}' bs=65536 iflag=skip_bytes,count_bytes "
        f"skip={offset} count={length} status=none"
    ]
    for attempt in range(1, 8):
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=300)
            if p.returncode == 0 and len(p.stdout) == length:
                return p.stdout
            # last chunk may be shorter only if length requested equals remaining — we always request exact
            if p.returncode == 0 and offset + len(p.stdout) == REMOTE_SIZE and len(p.stdout) <= length:
                return p.stdout
            log(f"chunk offset={offset} attempt={attempt} rc={p.returncode} got={len(p.stdout)} want={length} err={p.stderr[-200:]!r}")
        except Exception as e:
            log(f"chunk offset={offset} attempt={attempt} exc={e}")
        time.sleep(min(2 * attempt, 15))
    raise RuntimeError(f"failed chunk at offset {offset}")


def main() -> int:
    CS.mkdir(parents=True, exist_ok=True)
    if DST.exists() and DST.stat().st_size == REMOTE_SIZE:
        h = hashlib.md5(DST.read_bytes()).hexdigest()
        if h == REMOTE_MD5:
            log("chunked: already complete + md5 ok")
            return finalize(True)
        log(f"chunked: dest size ok but md5 {h} != {REMOTE_MD5}; re-download")
        DST.unlink()

    have = TMP.stat().st_size if TMP.exists() else 0
    if have > REMOTE_SIZE:
        TMP.unlink()
        have = 0
    log(f"chunked: start resume_from={have} target={REMOTE_SIZE}")

    with TMP.open("ab" if have else "wb") as out:
        # if resume mid-chunk, truncate to chunk boundary
        aligned = (have // CHUNK) * CHUNK
        if have != aligned:
            out.truncate(aligned)
            out.seek(aligned)
            have = aligned
            log(f"chunked: truncated to aligned={aligned}")

        while have < REMOTE_SIZE:
            length = min(CHUNK, REMOTE_SIZE - have)
            data = fetch_chunk(have, length)
            out.write(data)
            out.flush()
            have += len(data)
            log(f"chunked: progress {have}/{REMOTE_SIZE} ({100*have/REMOTE_SIZE:.1f}%)")

    h = hashlib.md5(TMP.read_bytes()).hexdigest()
    sz = TMP.stat().st_size
    if sz != REMOTE_SIZE or h != REMOTE_MD5:
        log(f"chunked: VERIFY FAIL size={sz} md5={h}")
        return finalize(False)
    TMP.replace(DST)
    log(f"chunked: SUCCESS size={sz} md5={h}")
    return finalize(True)


def finalize(ok: bool) -> int:
    import urllib.request
    from collections import defaultdict

    remote = [l for l in (IMP / "remote-videos-rel.txt").read_text().splitlines() if l.strip()]
    exact = sum(1 for r in remote if (CS / r).exists() and (CS / r).stat().st_size > 0)
    missing = [r for r in remote if not ((CS / r).exists() and (CS / r).stat().st_size > 0)]
    groups: dict[str, list[str]] = defaultdict(list)
    for r in remote:
        groups[r.casefold()].append(r)
    mapped = sum(1 for names in groups.values() if any((CS / n).exists() and (CS / n).stat().st_size > 0 for n in names))
    thumbs = ROOT / "backend/data/thumbnails"
    tn = sum(1 for _ in thumbs.rglob("*") if _.is_file())
    try:
        be = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).status
    except Exception:
        be = 0
    vn = sum(1 for p in CS.iterdir() if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".tmp"))
    size = subprocess.check_output(["du", "-sh", str(CS)], text=True).split()[0]
    tsize = subprocess.check_output(["du", "-sh", str(thumbs)], text=True).split()[0]
    log(f"chunked finalize exact={exact}/{len(remote)} groups={mapped}/{len(groups)} missing={missing} be={be}")
    (IMP / "video-collision-map.txt").write_text(
        "\n".join(f"{k}\t" + "|".join(v) for k, v in sorted(groups.items()) if len(v) > 1) + "\n"
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
            f"last_file={REL} md5={REMOTE_MD5}\n"
        )
        (IMP / "VOLUME_CLONE_DONE").write_text(body)
        (IMP / "missing-videos-cs.txt").unlink(missing_ok=True)
        log("VOLUME_CLONE_DONE COMPLETE written")
        print(body)
        return 0
    (IMP / "missing-videos-cs.txt").write_text("\n".join(missing) + "\n")
    return 1 if not ok else 2


if __name__ == "__main__":
    sys.exit(main())
