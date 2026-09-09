#!/usr/bin/env bash
# Copy search Postgres + Qdrant from Railway → DigitalOcean.
#
# Default is inventory only (read-only). Does not change Railway env, does not
# freeze auto-index, does not drop anything, does not railway up.
#
#   ./data-copy.sh inventory
#   ./data-copy.sh copy --execute
#
# copy --execute still leaves Railway running. It will:
#   - pg_dump the search Postgres (Postgres, not Postgres-WEBK) via the public proxy
#   - create+download Qdrant snapshots for all five collections (I/O on Railway Qdrant)
#   - restore onto DigitalOcean only
#   - keep DO RUN_INDEXER=false
set -euo pipefail

MODE="${1:-inventory}"
EXECUTE=0
if [[ "${2:-}" == "--execute" ]]; then
  EXECUTE=1
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL_DIR="${DFI_COPY_DIR:-$HOME/.config/dfi-search/copy}"
INVENTORY="$HOME/.config/dfi-search/inventory.json"
RAILWAY_QDRANT="${RAILWAY_QDRANT_URL:-https://qdrant-production-e623.up.railway.app}"
DO_API_SSH="${DO_API_SSH:-root@165.245.170.117}"
DO_QDRANT_SSH="${DO_QDRANT_SSH:-root@165.245.173.59}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
SSH_OPTS=(-o ConnectTimeout=25 -o ConnectionAttempts=4 -o ServerAliveInterval=15 -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -i "$SSH_KEY")
COLLECTIONS=(dfi_images dfi_image_captions dfi_video_frames dfi_video_transcripts dfi_folder_contexts)

ssh_retry() {
  local n=0
  until ssh "${SSH_OPTS[@]}" "$@"; do
    n=$((n + 1))
    if [[ $n -ge 6 ]]; then
      return 1
    fi
    sleep 2
  done
}

inventory() {
  python3 - <<'PY'
import json, os, subprocess, urllib.request
from pathlib import Path
from urllib.parse import urlparse, unquote
import psycopg2

base = os.environ.get("RAILWAY_QDRANT", "https://qdrant-production-e623.up.railway.app").rstrip("/")
cols = [
    "dfi_images",
    "dfi_image_captions",
    "dfi_video_frames",
    "dfi_video_transcripts",
    "dfi_folder_contexts",
]
qdrant = {}
for name in cols:
    with urllib.request.urlopen(f"{base}/collections/{name}", timeout=30) as r:
        d = json.load(r)
    res = d.get("result") or {}
    with urllib.request.urlopen(f"{base}/collections/{name}/snapshots", timeout=60) as r:
        snaps = json.load(r)
    items = snaps.get("result") or []
    latest = None
    if items:
        latest = sorted(items, key=lambda s: s.get("creation_time") or "", reverse=True)[0]
    qdrant[name] = {
        "points": res.get("points_count"),
        "indexed": res.get("indexed_vectors_count"),
        "status": res.get("status"),
        "snapshot_count": len(items),
        "latest_snapshot": None if not latest else {
            "name": latest.get("name"),
            "creation_time": latest.get("creation_time"),
            "size_bytes": latest.get("size"),
        },
    }

r = subprocess.run(
    ["railway", "variables", "--service", "Postgres", "--json"],
    capture_output=True, text=True, check=True,
)
url = json.loads(r.stdout)["DATABASE_PUBLIC_URL"]
p = urlparse(url)
conn = psycopg2.connect(
    host=p.hostname,
    port=p.port or 5432,
    user=unquote(p.username or "postgres"),
    password=unquote(p.password or ""),
    dbname=(p.path or "/railway").lstrip("/") or "railway",
    connect_timeout=15,
    sslmode="require",
    options="-c statement_timeout=20000 -c default_transaction_read_only=on",
)
conn.set_session(readonly=True, autocommit=True)
cur = conn.cursor()
cur.execute("SELECT pg_database_size(current_database())")
db_bytes = int(cur.fetchone()[0])
cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY 1")
exts = {n: v for n, v in cur.fetchall()}
cur.execute(
    "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC NULLS LAST"
)
tables = {n: int(t or 0) for n, t in cur.fetchall()}
cur.execute("SELECT COUNT(*) FROM drive_files")
drive_files = int(cur.fetchone()[0])
cur.close()
conn.close()

out = {
    "railway_untouched": True,
    "postgres": {
        "service": "Postgres",
        "not": "Postgres-WEBK",
        "db_bytes": db_bytes,
        "extensions": exts,
        "tables": tables,
        "drive_files": drive_files,
    },
    "qdrant": qdrant,
    "notes": [
        "Existing dfi_images snapshots are older than live points; copy must create fresh snapshots.",
        "dfi_video_transcripts and dfi_folder_contexts have no snapshots yet.",
        "Faces live in Postgres (pgvector), not Qdrant.",
        "Do not copy dfi-backend-volume /app/data.",
        "Do not freeze AUTO_INDEX unless asked; copy will then have small live drift.",
    ],
}
path = Path.home() / ".config/dfi-search/inventory.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(out, indent=2) + "\n")
print(json.dumps(out, indent=2))
print("wrote", path)
PY
}

assert_not_railway_up() {
  echo "guard: will not run railway up; Railway stays serving"
}

copy_execute() {
  assert_not_railway_up
  mkdir -p "$LOCAL_DIR/$STAMP"
  echo "copy execute is a separate step; refusing to start dump from this wrapper until dump helpers are invoked explicitly"
  echo "Use: DFI_COPY_STAMP=$STAMP $ROOT/data-copy-execute.sh"
  echo "Not running dump now."
  exit 2
}

case "$MODE" in
  inventory)
    RAILWAY_QDRANT="$RAILWAY_QDRANT" inventory
    ;;
  copy)
    if [[ "$EXECUTE" != 1 ]]; then
      echo "Refusing copy without --execute. Default is inventory-only so Railway is not loaded."
      echo "When you want the dump: $0 copy --execute"
      exit 2
    fi
    copy_execute
    ;;
  *)
    echo "usage: $0 [inventory|copy --execute]" >&2
    exit 2
    ;;
esac
