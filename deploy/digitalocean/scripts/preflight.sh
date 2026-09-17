#!/usr/bin/env bash
# Preflight checks for Carousel DigitalOcean artifacts (no cloud mutations).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DO_DIR="${ROOT}/deploy/digitalocean"
BACKEND_SPEC="${ROOT}/.do/backend.yaml"
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
need_file "${BACKEND_SPEC}"
need_file "${FRONTEND_SPEC}"
if [[ -f "${ROOT}/.do/app.yaml" ]]; then
  fail "obsolete .do/app.yaml must be removed (use backend.yaml + frontend.yaml)"
else
  ok "no obsolete .do/app.yaml"
fi
need_file "${DO_DIR}/docker-compose.yml"
need_file "${DO_DIR}/.env.example"
need_file "${DO_DIR}/README.md"
need_file "${DO_DIR}/scripts/backup.sh"
need_file "${DO_DIR}/scripts/restore.sh"
need_file "${DO_DIR}/scripts/preflight.sh"

echo "==> Script syntax"
for s in backup.sh restore.sh preflight.sh; do
  if bash -n "${DO_DIR}/scripts/${s}"; then
    ok "bash -n scripts/${s}"
  else
    fail "bash -n scripts/${s}"
  fi
done
need_exec "${DO_DIR}/scripts/backup.sh"
need_exec "${DO_DIR}/scripts/restore.sh"
need_exec "${DO_DIR}/scripts/preflight.sh"

echo "==> Backend app spec invariants"
if grep -q 'branch: pruned-craousel' "${BACKEND_SPEC}"; then
  ok "backend branch pruned-craousel"
else
  fail "backend.yaml missing branch pruned-craousel"
fi
if grep -q 'name: carousel-backend' "${BACKEND_SPEC}"; then
  ok "carousel-backend service"
else
  fail "missing carousel-backend service"
fi
# Exactly one service block name under services (heuristic: count "name: carousel-")
backend_svc_count="$(grep -cE '^\s+-\s+name:\s+carousel-' "${BACKEND_SPEC}" || true)"
if [[ "${backend_svc_count}" -eq 1 ]]; then
  ok "backend app has exactly one carousel service"
else
  fail "backend app must have exactly one service (found ${backend_svc_count})"
fi
if grep -q 'name: carousel-frontend' "${BACKEND_SPEC}"; then
  fail "frontend service must not appear in backend.yaml"
else
  ok "backend.yaml has no frontend service"
fi
if grep -q 'source_dir: backend' "${BACKEND_SPEC}" && grep -q 'http_port: 8000' "${BACKEND_SPEC}"; then
  ok "backend source_dir + port 8000"
else
  fail "backend source_dir/port incorrect"
fi
if grep -q 'http_path: /health' "${BACKEND_SPEC}"; then
  ok "backend health /health"
else
  fail "backend health_check missing"
fi
if grep -q 'WEB_CONCURRENCY' "${BACKEND_SPEC}" && grep -q 'value: "1"' "${BACKEND_SPEC}"; then
  ok "WEB_CONCURRENCY present (expect 1)"
else
  fail "WEB_CONCURRENCY not set"
fi
if grep -q 'dfi_video_transcripts' "${BACKEND_SPEC}"; then
  ok "transcripts collection configured"
else
  fail "missing dfi_video_transcripts"
fi
if grep -q 'REPLACE_WITH_VPC_UUID' "${BACKEND_SPEC}"; then
  ok "backend VPC placeholder"
else
  fail "backend VPC guidance missing"
fi
if grep -Eq 'type:\s+(GENERAL|SECRET)' "${BACKEND_SPEC}" && ! grep -Eq 'type:\s+(general|secret)\b' "${BACKEND_SPEC}"; then
  ok "backend env types uppercase GENERAL/SECRET"
else
  fail "backend env types must be uppercase GENERAL/SECRET only"
fi
if grep -Eq 'name: (dfi-backend|dfi-frontend|dfi-face-worker)' "${BACKEND_SPEC}"; then
  fail "search services must not appear in backend.yaml"
else
  ok "no search services in backend.yaml"
fi

echo "==> Frontend app spec invariants"
if grep -q 'branch: pruned-craousel' "${FRONTEND_SPEC}"; then
  ok "frontend branch pruned-craousel"
else
  fail "frontend.yaml missing branch pruned-craousel"
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
if grep -q 'source_dir: carousel-frontend' "${FRONTEND_SPEC}" && grep -q 'http_port: 3002' "${FRONTEND_SPEC}"; then
  ok "frontend source_dir + port 3002"
else
  fail "frontend source_dir/port incorrect"
fi
if grep -q 'http_path: /' "${FRONTEND_SPEC}"; then
  ok "frontend health /"
else
  fail "frontend health_check missing"
fi
if grep -q 'REPLACE_BACKEND_PUBLIC_URL' "${FRONTEND_SPEC}"; then
  ok "frontend uses public backend URL placeholder"
else
  fail "frontend missing REPLACE_BACKEND_PUBLIC_URL"
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

echo "==> Shared VPC placeholder (same token in both specs)"
if grep -q 'REPLACE_WITH_VPC_UUID' "${BACKEND_SPEC}" && grep -q 'REPLACE_WITH_VPC_UUID' "${FRONTEND_SPEC}"; then
  ok "both specs document REPLACE_WITH_VPC_UUID"
else
  fail "VPC UUID placeholder must match across both specs"
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
  if docker compose -f "${COMPOSE}" --env-file "${envfile}" config >/dev/null; then
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
