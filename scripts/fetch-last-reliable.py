#!/usr/bin/env python3
"""Fetch the last missing Railway video (colon in name breaks scp). Prefer tar|tar, else chunked dd."""
from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path("/Users/mu-mac_3/Projects/video-image-search-indexer-quality")
CS = ROOT / "backend/data/videos-cs"
IMP = ROOT / "data/imports"
LOG = IMP / "volume-clone.log"
LOCK = IMP / "fetch-last.lock"
REL = "yt:2CL-u9PlMNs.webm"
DST = CS / REL
REMOTE_SIZE = 710867717
REMOTE_MD5 = "eaa388292b5151cd5f88f698905121f2"
CHUNK = 8 * 1024 * 1024
SSH_BASE = [
    "ssh",
    "-o",
    "BatchMode=yes",
    "-o",
    "Compression=no",
    "-o",
    "ServerAliveInterval=10",
    "-o",
    "ServerAliveCountMax=30",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "TCPKeepAlive=yes",
    "railway-dfi-backend",
]


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def acquire_lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("fetch-reliable: lock busy — exit")
        fh.close()
        sys.exit(0)
    return fh


def ensure_mount() -> None:
    m = subprocess.run(["mount"], capture_output=True, text=True)
    if f" on {CS} " not in m.stdout:
        subprocess.run(
            [
                "hdiutil",
                "attach",
                str(ROOT / "data/dfi-videos.sparsebundle"),
                "-mountpoint",
                str(CS),
                "-nobrowse",
            ],
            check=False,
        )


def verify_dst() -> bool:
    if not DST.exists() or DST.stat().st_size != REMOTE_SIZE:
        return False
    h = hashlib.md5(DST.read_bytes()).hexdigest()
    if h != REMOTE_MD5:
        log(f"md5 mismatch {h}; removing")
        DST.unlink()
        return False
    return True


def fetch_via_tar() -> bool:
    for attempt in range(1, 7):
        tmp = Path(tempfile.mkdtemp(prefix=".tmp.fetch.", dir=CS))
        log(f"tar attempt {attempt} -> {tmp}")
        try:
            # Quote path so colon is not special to local tools; stream tar of one file
            q = "./" + REL.replace("'", "'\\''")
            remote_cmd = f"cd /app/data/videos && tar cf - '{q}'"
            p1 = subprocess.Popen(SSH_BASE + [remote_cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p2 = subprocess.Popen(["tar", "xf", "-", "-C", str(tmp)], stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert p1.stdout is not None
            p1.stdout.close()
            _, err2 = p2.communicate(timeout=900)
            err1 = p1.stderr.read() if p1.stderr else b""
            rc1 = p1.wait(timeout=30)
            if p2.returncode != 0 or rc1 != 0:
                log(f"tar fail rc_ssh={rc1} rc_tar={p2.returncode} err={err1[-300]!r} {err2[-300]!r}")
                continue
            files = [p for p in tmp.rglob("*") if p.is_file()]
            if not files:
                log("tar extracted nothing")
                continue
            got = files[0]
            sz = got.stat().st_size
            if sz != REMOTE_SIZE:
                log(f"tar bad size {sz}")
                continue
            got.replace(DST)
            log("tar SUCCESS")
            return True
        except Exception as e:
            log(f"tar exception attempt={attempt}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        time.sleep(3)
    return False


def fetch_chunk(offset: int, length: int) -> bytes:
    cmd = SSH_BASE + [
        "dd",
        f"if=/app/data/videos/{REL}",
        "bs=65536",
        "iflag=skip_bytes,count_bytes",
        f"skip={offset}",
        f"count={length}",
        "status=none",
    ]
    # Prefer shell form — remote dd flags need to be one remote command string
    remote = (
        f"dd if='/app/data/videos/{REL}' bs=65536 iflag=skip_bytes,count_bytes "
        f"skip={offset} count={length} status=none"
    )
    for attempt in range(1, 10):
        try:
            p = subprocess.run(SSH_BASE + [remote], capture_output=True, timeout=300)
            if p.returncode == 0 and len(p.stdout) == length:
                return p.stdout
            if p.returncode == 0 and offset + len(p.stdout) == REMOTE_SIZE:
                return p.stdout
            log(
                f"chunk offset={offset} attempt={attempt} rc={p.returncode} "
                f"got={len(p.stdout)} want={length}"
            )
        except Exception as e:
            log(f"chunk offset={offset} attempt={attempt} exc={e}")
        time.sleep(min(2 * attempt, 12))
    raise RuntimeError(f"chunk failed at {offset}")


def fetch_via_chunks() -> bool:
    tmp = CS / f".partial.chunked.{REL}.tmp"
    have = tmp.stat().st_size if tmp.exists() else 0
    if have > REMOTE_SIZE:
        tmp.unlink()
        have = 0
    aligned = (have // CHUNK) * CHUNK
    if have != aligned:
        with tmp.open("r+b") as f:
            f.truncate(aligned)
        have = aligned
    log(f"chunked resume_from={have}")
    with tmp.open("ab" if have else "wb") as out:
        while have < REMOTE_SIZE:
            length = min(CHUNK, REMOTE_SIZE - have)
            data = fetch_chunk(have, length)
            out.write(data)
            out.flush()
            have += len(data)
            if have % (CHUNK * 4) == 0 or have >= REMOTE_SIZE:
                log(f"chunked progress {have}/{REMOTE_SIZE} ({100 * have / REMOTE_SIZE:.1f}%)")
    h = hashlib.md5(tmp.read_bytes()).hexdigest()
    if tmp.stat().st_size != REMOTE_SIZE or h != REMOTE_MD5:
        log(f"chunked verify fail size={tmp.stat().st_size} md5={h}")
        return False
    tmp.replace(DST)
    log(f"chunked SUCCESS md5={h}")
    return True


def write_done_marker() -> int:
    remote = [l for l in (IMP / "remote-videos-rel.txt").read_text().splitlines() if l.strip()]
    exact = sum(1 for r in remote if (CS / r).exists() and (CS / r).stat().st_size > 0)
    missing = [r for r in remote if not ((CS / r).exists() and (CS / r).stat().st_size > 0)]
    groups: dict[str, list[str]] = defaultdict(list)
    for r in remote:
        groups[r.casefold()].append(r)
    thumbs = ROOT / "backend/data/thumbnails"
    tn = sum(1 for _ in thumbs.rglob("*") if _.is_file())
    try:
        be = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5).status
    except Exception:
        be = 0
    vn = sum(1 for p in CS.iterdir() if p.is_file() and not p.name.startswith(".") and not p.name.endswith(".tmp"))
    size = subprocess.check_output(["du", "-sh", str(CS)], text=True).split()[0]
    tsize = subprocess.check_output(["du", "-sh", str(thumbs)], text=True).split()[0]
    (IMP / "video-collision-map.txt").write_text(
        "\n".join(f"{k}\t" + "|".join(v) for k, v in sorted(groups.items()) if len(v) > 1) + "\n"
    )
    log(f"finalize exact={exact}/{len(remote)} missing={missing} be={be}")
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
    return 2


def main() -> int:
    lockfh = acquire_lock()
    try:
        ensure_mount()
        log(f"fetch-reliable.py start pid={os.getpid()}")
        if verify_dst():
            log("already complete")
            return write_done_marker()
        if fetch_via_tar() and verify_dst():
            return write_done_marker()
        if fetch_via_chunks() and verify_dst():
            return write_done_marker()
        log("ALL METHODS FAILED")
        return write_done_marker()
    finally:
        try:
            fcntl.flock(lockfh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lockfh.close()


if __name__ == "__main__":
    sys.exit(main())
