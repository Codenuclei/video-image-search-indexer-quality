#!/usr/bin/env bash
# Railway → DigitalOcean search data copy. Railway stays online.
# Does not railway up, drop Railway DBs, freeze auto-index, or copy /app/data.
#
# Run only when asked to actually move data:
#   deploy/search/data-copy-execute.sh
set -euo pipefail

STAMP="${DFI_COPY_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
SSH_OPTS=(-o ConnectTimeout=25 -o ConnectionAttempts=6 -o ServerAliveInterval=15 -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -i "$SSH_KEY")
DO_API="root@165.245.170.117"
DO_QDRANT="root@165.245.173.59"
RAILWAY_QDRANT="${RAILWAY_QDRANT_URL:-https://qdrant-production-e623.up.railway.app}"
COLLECTIONS=(dfi_images dfi_image_captions dfi_video_frames dfi_video_transcripts dfi_folder_contexts)
REMOTE_DIR="/opt/dfi/data-copy/$STAMP"
COMPOSE_DIR="/opt/dfi/video-image-search-indexer-quality/deploy/search"

ssh_retry() {
  local n=0
  until ssh "${SSH_OPTS[@]}" "$@"; do
    n=$((n + 1))
    [[ $n -ge 8 ]] && return 1
    sleep 2
  done
}

scp_retry() {
  local n=0
  until scp "${SSH_OPTS[@]}" "$@"; do
    n=$((n + 1))
    [[ $n -ge 8 ]] && return 1
    sleep 2
  done
}

echo "execute copy stamp=$STAMP"
echo "Railway remains serving. Postgres dump is read-only; Qdrant snapshot create adds I/O."

python3 - <<'PY'
import json, subprocess
from pathlib import Path
from urllib.parse import urlparse, unquote
r = subprocess.run(["railway","variables","--service","Postgres","--json"], capture_output=True, text=True, check=True)
src = urlparse(json.loads(r.stdout)["DATABASE_PUBLIC_URL"])
pw = json.loads(Path.home().joinpath(".config/dfi-search/doadmin-reset.json").read_text())
do_pw = pw[0]["password"] if isinstance(pw, list) else pw["password"]
out = Path("/tmp/dfi-copy-pg.env")
out.write_text(
    "\n".join([
        f"SRC_PGHOST={src.hostname}",
        f"SRC_PGPORT={src.port or 5432}",
        f"SRC_PGUSER={unquote(src.username or 'postgres')}",
        f"SRC_PGPASSWORD={unquote(src.password or '')}",
        f"SRC_PGDATABASE={(src.path or '/railway').lstrip('/') or 'railway'}",
        "DST_PGHOST=private-search-pg-do-user-43506388-0.f.db.ondigitalocean.com",
        "DST_PGPORT=25060",
        "DST_PGUSER=doadmin",
        f"DST_PGPASSWORD={do_pw}",
        "DST_PGDATABASE=defaultdb",
        "PGSSLMODE=require",
    ]) + "\n"
)
out.chmod(0o600)
print("wrote_env")
PY

ssh_retry "$DO_API" "mkdir -p $REMOTE_DIR && chmod 700 /opt/dfi/data-copy $REMOTE_DIR"
scp_retry /tmp/dfi-copy-pg.env "$DO_API:$REMOTE_DIR/pg.env"
rm -f /tmp/dfi-copy-pg.env

echo "== pg_dump Railway search Postgres (Postgres service, not Postgres-WEBK) =="
ssh_retry "$DO_API" "set -euo pipefail
set -a
source $REMOTE_DIR/pg.env
set +a
docker run --rm --entrypoint pg_dump \
  -e PGSSLMODE=require \
  -e PGPASSWORD=\"\$SRC_PGPASSWORD\" \
  -v $REMOTE_DIR:/out \
  search-backend \
  --format=custom --no-owner --no-acl \
  -h \"\$SRC_PGHOST\" -p \"\$SRC_PGPORT\" -U \"\$SRC_PGUSER\" -d \"\$SRC_PGDATABASE\" \
  -f /out/railway-search.dump
ls -lh $REMOTE_DIR/railway-search.dump
"

echo "== stop DO backend only =="
ssh_retry "$DO_API" "cd $COMPOSE_DIR && docker compose stop backend"

echo "== pg_restore onto DigitalOcean managed Postgres =="
ssh_retry "$DO_API" "set -euo pipefail
set -a
source $REMOTE_DIR/pg.env
set +a
docker run --rm --entrypoint pg_restore \
  -e PGSSLMODE=require \
  -e PGPASSWORD=\"\$DST_PGPASSWORD\" \
  -v $REMOTE_DIR:/out \
  search-backend \
  --no-owner --no-acl --clean --if-exists \
  -h \"\$DST_PGHOST\" -p \"\$DST_PGPORT\" -U \"\$DST_PGUSER\" -d \"\$DST_PGDATABASE\" \
  /out/railway-search.dump
"

echo "== Qdrant fresh snapshots (Railway stays up) =="
ssh_retry "$DO_QDRANT" "mkdir -p $REMOTE_DIR/qdrant && chmod 700 /opt/dfi/data-copy $REMOTE_DIR $REMOTE_DIR/qdrant"
for col in "${COLLECTIONS[@]}"; do
  echo "snapshot $col"
  snap_json="$(curl -sS -m 900 -X POST "$RAILWAY_QDRANT/collections/${col}/snapshots")"
  snap_name="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print((d.get("result") or {}).get("name") or "")' "$snap_json")"
  if [[ -z "$snap_name" ]]; then
    echo "failed to create snapshot for $col: $snap_json" >&2
    exit 1
  fi
  echo "  $snap_name"
  ssh_retry "$DO_QDRANT" "curl -fL --retry 5 -o $REMOTE_DIR/qdrant/${col}.snapshot '$RAILWAY_QDRANT/collections/${col}/snapshots/${snap_name}'"
  ssh_retry "$DO_QDRANT" "docker cp $REMOTE_DIR/qdrant/${col}.snapshot qdrant:/qdrant/snapshots/${col}.snapshot"
  ssh_retry "$DO_QDRANT" "curl -sfS -m 900 -X PUT http://127.0.0.1:6333/collections/${col}/snapshots/recover -H 'Content-Type: application/json' -d '{\"location\":\"file:///qdrant/snapshots/${col}.snapshot\"}' || curl -sfS -m 900 -X PUT http://10.130.0.2:6333/collections/${col}/snapshots/recover -H 'Content-Type: application/json' -d '{\"location\":\"file:///qdrant/snapshots/${col}.snapshot\"}'"
done

echo "== start DO backend, indexer still false =="
ssh_retry "$DO_API" "cd $COMPOSE_DIR && docker compose --env-file .env up -d
sleep 8
docker exec search-backend-1 python -c 'import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:8000/health\", timeout=20).read().decode())'
docker exec search-backend-1 printenv RUN_INDEXER FACE_JOBS_ENABLED AUTO_INDEX_ENABLED
"

echo "copy finished. Railway was not dropped or redeployed."
