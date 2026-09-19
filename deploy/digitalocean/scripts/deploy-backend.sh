#!/usr/bin/env bash
# Pull and restart the Droplet Carousel backend using an immutable DOCR tag.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}; copy .env.example and add production secrets." >&2
  exit 1
fi

tag="${1:-}"
if [[ -z "${tag}" ]]; then
  echo "Usage: $0 <immutable-image-tag>" >&2
  exit 1
fi
if [[ ! "${tag}" =~ ^[a-zA-Z0-9._-]+$ ]]; then
  echo "Invalid image tag: ${tag}" >&2
  exit 1
fi

image="registry.digitalocean.com/mu-pitch-studio/dfi-carousel-backend:${tag}"
export CAROUSEL_BACKEND_IMAGE="${image}"

mkdir -p /opt/dfi-carousel/backend-data/{videos,media_cache,tmp,thumbnails,fennec-media,backups,frame_thumbs}
chmod 750 /opt/dfi-carousel/backend-data

docker compose --env-file "${ENV_FILE}" -f "${ROOT}/docker-compose.yml" config >/dev/null
docker pull "${image}"
docker compose --env-file "${ENV_FILE}" -f "${ROOT}/docker-compose.yml" up -d

for _ in $(seq 1 40); do
  if docker compose --env-file "${ENV_FILE}" -f "${ROOT}/docker-compose.yml" \
    exec -T carousel-backend \
    python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)" \
    >/dev/null 2>&1; then
    echo "Carousel backend ${tag} is healthy."
    exit 0
  fi
  sleep 3
done

docker compose --env-file "${ENV_FILE}" -f "${ROOT}/docker-compose.yml" logs --tail=120 carousel-backend
echo "Carousel backend failed its health check." >&2
exit 1
