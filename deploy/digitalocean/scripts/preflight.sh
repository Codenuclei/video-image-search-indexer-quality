#!/usr/bin/env bash
# Preflight checks for Carousel DigitalOcean artifacts (no cloud mutations).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DO_DIR="${ROOT}/deploy/digitalocean"
FRONTEND_SPEC="${ROOT}/.do/frontend.yaml"
errors=0

ok() { printf 'OK  %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*" >&2; errors=$((errors + 1)); }

need_file() {
  if [[ -f "$1" ]]; then ok "file $1"; else fail "missing $1"; fi
}

need_exec() {
  if [[ -x "$1" ]]; then ok "executable $1"; else fail "not executable $1"; fi
}

echo "==> Artifact presence"
need_file "${FRONTEND_SPEC}"
if [[ -f "${ROOT}/.do/app.yaml" ]]; then
  fail "obsolete .do/app.yaml must be removed (use backend.yaml + frontend.yaml)"
else
  ok "no obsolete .do/app.yaml"
fi
need_file "${DO_DIR}/docker-compose.yml"
need_file "${DO_DIR}/Caddyfile"
need_file "${DO_DIR}/.env.example"
need_file "${DO_DIR}/README.md"
need_file "${DO_DIR}/scripts/backup.sh"
need_file "${DO_DIR}/scripts/restore.sh"
need_file "${DO_DIR}/scripts/preflight.sh"
need_file "${DO_DIR}/scripts/deploy-backend.sh"

echo "==> Script syntax"
for s in backup.sh restore.sh preflight.sh deploy-backend.sh; do
  if bash -n "${DO_DIR}/scripts/${s}"; then
    ok "bash -n scripts/${s}"
  else
    fail "bash -n scripts/${s}"
  fi
done
need_exec "${DO_DIR}/scripts/backup.sh"
need_exec "${DO_DIR}/scripts/restore.sh"
need_exec "${DO_DIR}/scripts/preflight.sh"
need_exec "${DO_DIR}/scripts/deploy-backend.sh"

echo "==> Frontend app spec invariants"
if grep -q 'registry_type: DOCR' "${FRONTEND_SPEC}" && grep -q 'repository: dfi-carousel-frontend' "${FRONTEND_SPEC}"; then
  ok "frontend DOCR image source"
else
  fail "frontend.yaml missing DOCR image source"
fi
if grep -q 'name: carousel-frontend' "${FRONTEND_SPEC}"; then
  ok "carousel-frontend service"
else
  fail "missing carousel-frontend service"
fi
frontend_svc_count="$(grep -cE '^\s+-\s+name:\s+carousel-' "${FRONTEND_SPEC}" || true)"
if [[ "${frontend_svc_count}" -eq 1 ]]; then
  ok "frontend app has exactly one carousel service"
else
  fail "frontend app must have exactly one service (found ${frontend_svc_count})"
fi
if grep -q 'name: carousel-backend' "${FRONTEND_SPEC}"; then
  fail "backend service must not appear in frontend.yaml"
else
  ok "frontend.yaml has no backend service"
fi
if grep -q 'tag: REPLACE_IMAGE_TAG' "${FRONTEND_SPEC}" && grep -q 'http_port: 3002' "${FRONTEND_SPEC}"; then
  ok "frontend immutable image tag placeholder + port 3002"
else
  fail "frontend image tag/port incorrect"
fi
if grep -q 'http_path: /' "${FRONTEND_SPEC}"; then
  ok "frontend health /"
else
  fail "frontend health_check missing"
fi
if grep -q 'https://api-carousel.139-59-35-242.sslip.io' "${FRONTEND_SPEC}"; then
  ok "frontend targets Droplet backend domain"
else
  fail "frontend missing Droplet backend domain"
fi
if grep -Eq '\$\{[^}]*PRIVATE_URL\}' "${FRONTEND_SPEC}"; then
  fail "frontend must not use cross-app PRIVATE_URL bindables"
else
  ok "frontend has no PRIVATE_URL bindable"
fi
if grep -q 'REPLACE_WITH_VPC_UUID' "${FRONTEND_SPEC}"; then
  ok "frontend VPC placeholder"
else
  fail "frontend VPC guidance missing"
fi
if grep -Eq 'type:\s+(GENERAL|SECRET)' "${FRONTEND_SPEC}" && ! grep -Eq 'type:\s+(general|secret)\b' "${FRONTEND_SPEC}"; then
  ok "frontend env types uppercase GENERAL/SECRET"
else
  fail "frontend env types must be uppercase GENERAL/SECRET only"
fi
if grep -Eq 'name: (dfi-backend|dfi-frontend|dfi-face-worker)' "${FRONTEND_SPEC}"; then
  fail "search services must not appear in frontend.yaml"
else
  ok "no search services in frontend.yaml"
fi

echo "==> Compose invariants"
COMPOSE="${DO_DIR}/docker-compose.yml"
if grep -q 'pgvector/pgvector:pg17' "${COMPOSE}"; then
  ok "Postgres pgvector:pg17"
else
  fail "expected pgvector/pgvector:pg17"
fi
if grep -Eq 'qdrant/qdrant:v[0-9]' "${COMPOSE}"; then
  ok "Qdrant image pinned"
else
  fail "Qdrant image not pinned"
fi
if grep -q 'DROPLET_BIND_ADDR' "${COMPOSE}"; then
  ok "private bind via DROPLET_BIND_ADDR"
else
  fail "missing DROPLET_BIND_ADDR bind"
fi
if grep -q 'restart: unless-stopped' "${COMPOSE}"; then
  ok "restart unless-stopped"
else
  fail "missing restart policy"
fi
if grep -q 'healthcheck:' "${COMPOSE}"; then
  ok "healthchecks present"
else
  fail "missing healthchecks"
fi
for token in 'carousel-backend:' 'caddy:' 'CAROUSEL_BACKEND_IMAGE' 'backend-data' '/app/data'; do
  if grep -q "${token}" "${COMPOSE}"; then
    ok "compose contains ${token}"
  else
    fail "compose missing ${token}"
  fi
done
if grep -q 'api-carousel.139-59-35-242.sslip.io' "${DO_DIR}/Caddyfile"; then
  ok "Caddy public backend domain"
else
  fail "Caddy backend domain missing"
fi

echo "==> Backup script covers all Carousel collections"
for coll in dfi_video_frames dfi_images dfi_image_captions dfi_video_transcripts; do
  if grep -q "${coll}" "${DO_DIR}/scripts/backup.sh"; then
    ok "backup mentions ${coll}"
  else
    fail "backup missing ${coll}"
  fi
done

echo "==> Env example has no live secrets"
ok ".env.example secret fields empty or placeholder"
for key in POSTGRES_PASSWORD SPACES_ACCESS_KEY_ID SPACES_SECRET_ACCESS_KEY BACKUP_ENCRYPTION_PASSPHRASE; do
  val="$(grep -E "^${key}=" "${DO_DIR}/.env.example" | head -1 | cut -d= -f2- || true)"
  if [[ -n "${val}" ]]; then
    fail "${key} must be empty in .env.example (got non-empty)"
  else
    ok "${key} empty in .env.example"
  fi
done

echo "==> Optional: docker compose config"
if command -v docker >/dev/null 2>&1; then
  if [[ -f "${DO_DIR}/.env" ]]; then
    envfile="${DO_DIR}/.env"
  else
    envfile="${DO_DIR}/.env.preflight.tmp"
    cp "${DO_DIR}/.env.example" "${envfile}"
    echo "POSTGRES_PASSWORD=preflight-only-not-for-prod" >> "${envfile}"
  fi
  if BACKEND_ENV_FILE="$(basename "${envfile}")" \
    docker compose -f "${COMPOSE}" --env-file "${envfile}" config >/dev/null; then
    ok "docker compose config"
  else
    fail "docker compose config"
  fi
  if [[ "${envfile}" == *.preflight.tmp ]]; then
    rm -f "${envfile}"
  fi
else
  echo "SKIP docker compose (docker not available)"
fi

if [[ "${errors}" -ne 0 ]]; then
  echo "Preflight failed with ${errors} error(s)" >&2
  exit 1
fi
echo "Preflight passed"
