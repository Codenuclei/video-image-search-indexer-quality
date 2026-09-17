#!/usr/bin/env bash
# Restore Carousel Studio PostgreSQL + Qdrant collections from a backup.sh archive.
# DESTRUCTIVE: replaces database contents and collection snapshot state.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  restore.sh <backup-dir-or-tar.gz[.enc]>

Examples:
  ./scripts/restore.sh /var/backups/dfi-carousel/20260101T120000Z
  ./scripts/restore.sh /var/backups/dfi-carousel/carousel-backup-….tar.gz
  BACKUP_ENCRYPTION_PASSPHRASE=… ./scripts/restore.sh ….tar.gz.enc

Requires docker compose stack healthy and .env present.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck disable=SC1091
if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

COMPOSE=(docker compose -f "${ROOT_DIR}/docker-compose.yml" --env-file "${ROOT_DIR}/.env")
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
POSTGRES_USER="${POSTGRES_USER:-carousel}"
POSTGRES_DB="${POSTGRES_DB:-carousel}"
SRC="$1"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

mkdir -p "${WORK}/unpack" "${WORK}/restore"

if [[ -d "${SRC}" ]]; then
  cp -a "${SRC}/." "${WORK}/restore/"
else
  archive="${SRC}"
  if [[ "${SRC}" == *.enc ]]; then
    if [[ -z "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ]]; then
      echo "ERROR: BACKUP_ENCRYPTION_PASSPHRASE required for .enc archives" >&2
      exit 1
    fi
    archive="${WORK}/decrypted.tar.gz"
    openssl enc -d -aes-256-cbc -pbkdf2 \
      -in "${SRC}" \
      -out "${archive}" \
      -pass "env:BACKUP_ENCRYPTION_PASSPHRASE"
  fi
  tar -xzf "${archive}" -C "${WORK}/unpack"
  stamped="$(find "${WORK}/unpack" -mindepth 1 -maxdepth 1 -type d | head -1)"
  if [[ -z "${stamped}" ]]; then
    echo "ERROR: no stamped directory inside archive" >&2
    exit 1
  fi
  cp -a "${stamped}/." "${WORK}/restore/"
fi

DUMP="${WORK}/restore/postgres/carousel.dump"
MANIFEST="${WORK}/restore/qdrant/manifest.json"
if [[ ! -f "${DUMP}" ]]; then
  echo "ERROR: missing postgres dump at ${DUMP}" >&2
  exit 1
fi
if [[ ! -f "${MANIFEST}" ]]; then
  echo "ERROR: missing qdrant manifest at ${MANIFEST}" >&2
  exit 1
fi

echo "==> Restoring PostgreSQL (DROP SCHEMA public CASCADE)"
"${COMPOSE[@]}" exec -T postgres \
  psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  -v ON_ERROR_STOP=1 \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO \"${POSTGRES_USER}\";"
set +e
"${COMPOSE[@]}" exec -T postgres \
  pg_restore -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --no-owner --no-acl \
  < "${DUMP}"
pg_rc=$?
set -e
if [[ "${pg_rc}" -gt 1 ]]; then
  echo "ERROR: pg_restore failed with exit ${pg_rc}" >&2
  exit "${pg_rc}"
fi
"${COMPOSE[@]}" exec -T postgres \
  psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"

echo "==> Restoring Qdrant collections from snapshots"
# Copy each snapshot into the container snapshots dir, then recover via file:// URI.
python3 - "${MANIFEST}" <<'PY' > "${WORK}/pairs.tsv"
import json, sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
for item in manifest.get("snapshots", []):
    coll = item.get("collection") or ""
    snap = item.get("snapshot") or ""
    if coll and snap:
        print(f"{coll}\t{snap}")
PY

while IFS=$'\t' read -r coll snap; do
  [[ -n "${coll}" && -n "${snap}" ]] || continue
  file="${WORK}/restore/qdrant/${coll}__${snap}"
  if [[ ! -f "${file}" ]]; then
    file="$(find "${WORK}/restore/qdrant" -maxdepth 1 -type f -name "${coll}__*" | head -1 || true)"
  fi
  if [[ -z "${file}" || ! -f "${file}" ]]; then
    echo "ERROR: snapshot file missing for ${coll} (${snap})" >&2
    exit 1
  fi
  echo "    restore ${coll} from $(basename "${file}")"
  "${COMPOSE[@]}" exec -T qdrant mkdir -p "/qdrant/snapshots/${coll}"
  "${COMPOSE[@]}" cp "${file}" "qdrant:/qdrant/snapshots/${coll}/${snap}"
  curl -fsS -X PUT \
    "${QDRANT_URL}/collections/${coll}/snapshots/recover" \
    -H "Content-Type: application/json" \
    -d "{\"location\":\"file:///qdrant/snapshots/${coll}/${snap}\"}" >/dev/null
done < "${WORK}/pairs.tsv"

echo "OK restore finished from ${SRC}"
echo "Next: hit backend /health/detail and compare row/point counts with the Railway export."
