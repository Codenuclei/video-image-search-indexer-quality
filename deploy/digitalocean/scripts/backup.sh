#!/usr/bin/env bash
# Backup Carousel Studio PostgreSQL + all Qdrant collections (including transcripts).
# Run on the data Droplet with docker compose healthy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck disable=SC1091
if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

BACKUP_DIR="${BACKUP_DIR:-/var/backups/dfi-carousel}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${BACKUP_DIR}/${STAMP}"
COMPOSE=(docker compose -f "${ROOT_DIR}/docker-compose.yml" --env-file "${ROOT_DIR}/.env")

COLLECTIONS=(
  "${QDRANT_COLLECTION:-dfi_video_frames}"
  "${QDRANT_IMAGES_COLLECTION:-dfi_images}"
  "${QDRANT_IMAGE_CAPTIONS_COLLECTION:-dfi_image_captions}"
  "${QDRANT_VIDEO_TRANSCRIPTS_COLLECTION:-dfi_video_transcripts}"
)

QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333}"
POSTGRES_USER="${POSTGRES_USER:-carousel}"
POSTGRES_DB="${POSTGRES_DB:-carousel}"

mkdir -p "${OUT}/postgres" "${OUT}/qdrant"
echo "==> Backup ${STAMP} → ${OUT}"

echo "==> PostgreSQL pg_dump (custom format)"
"${COMPOSE[@]}" exec -T postgres \
  pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --format=custom \
  > "${OUT}/postgres/carousel.dump"

echo "==> Qdrant snapshots"
manifest_items=()
for coll in "${COLLECTIONS[@]}"; do
  [[ -n "${coll}" ]] || continue
  echo "    snapshot ${coll}"
  resp="$(curl -fsS -X POST "${QDRANT_URL}/collections/${coll}/snapshots")"
  snap_name="$(
    python3 -c 'import json,sys; r=json.load(sys.stdin); print((r.get("result") or {}).get("name") or "")' \
      <<<"${resp}"
  )"
  if [[ -z "${snap_name}" ]]; then
    echo "ERROR: failed to create snapshot for ${coll}: ${resp}" >&2
    exit 1
  fi
  curl -fsS \
    "${QDRANT_URL}/collections/${coll}/snapshots/${snap_name}" \
    -o "${OUT}/qdrant/${coll}__${snap_name}"
  manifest_items+=("$(python3 -c 'import json,sys; print(json.dumps({"collection":sys.argv[1],"snapshot":sys.argv[2],"file":sys.argv[1]+"__"+sys.argv[2]}))' "${coll}" "${snap_name}")")
done

python3 - "${STAMP}" "${OUT}/qdrant/manifest.json" "${manifest_items[@]}" <<'PY'
import json, sys
stamp, path, *items = sys.argv[1:]
payload = {"created_at": stamp, "snapshots": [json.loads(i) for i in items]}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)
    fh.write("\n")
PY

echo "==> Pack archive"
tar -C "${BACKUP_DIR}" -czf "${BACKUP_DIR}/carousel-backup-${STAMP}.tar.gz" "${STAMP}"
ARCHIVE="${BACKUP_DIR}/carousel-backup-${STAMP}.tar.gz"

if [[ -n "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ]]; then
  echo "==> Encrypt archive (openssl AES-256-CBC)"
  openssl enc -aes-256-cbc -pbkdf2 -salt \
    -in "${ARCHIVE}" \
    -out "${ARCHIVE}.enc" \
    -pass "env:BACKUP_ENCRYPTION_PASSPHRASE"
  UPLOAD_FILE="${ARCHIVE}.enc"
else
  UPLOAD_FILE="${ARCHIVE}"
  echo "WARN: BACKUP_ENCRYPTION_PASSPHRASE unset — archive left unencrypted" >&2
fi

if [[ -n "${SPACES_ACCESS_KEY_ID:-}" && -n "${SPACES_SECRET_ACCESS_KEY:-}" && -n "${SPACES_BUCKET:-}" ]]; then
  echo "==> Upload to Spaces ${SPACES_BUCKET}"
  export AWS_ACCESS_KEY_ID="${SPACES_ACCESS_KEY_ID}"
  export AWS_SECRET_ACCESS_KEY="${SPACES_SECRET_ACCESS_KEY}"
  export AWS_DEFAULT_REGION="${SPACES_REGION:-nyc3}"
  endpoint="${SPACES_ENDPOINT:-https://nyc3.digitaloceanspaces.com}"
  dest="s3://${SPACES_BUCKET}/carousel/$(basename "${UPLOAD_FILE}")"
  if command -v aws >/dev/null 2>&1; then
    aws --endpoint-url "${endpoint}" s3 cp "${UPLOAD_FILE}" "${dest}"
  else
    echo "ERROR: aws CLI required for Spaces upload (or clear SPACES_* to skip)" >&2
    exit 1
  fi
else
  echo "==> Skipping Spaces upload (SPACES_* unset)"
fi

echo "OK backup complete: ${UPLOAD_FILE}"
