#!/bin/bash
# Run on search-api as root. Clones do-search-runpod (not main) so Railway
# GitHub-on-main cannot pick up this stack. Never railway up.
set -euo pipefail

DFI_BRANCH="${DFI_BRANCH:-do-search-runpod}"

if [[ ! -f /swapfile ]]; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon /swapfile 2>/dev/null || true

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

mkdir -p /opt/dfi
cd /opt/dfi

if [[ ! -d video-image-search-indexer-quality/.git ]]; then
  git clone --depth 1 --branch "$DFI_BRANCH" https://github.com/Codenuclei/video-image-search-indexer-quality.git
else
  git -C video-image-search-indexer-quality fetch origin "$DFI_BRANCH"
  git -C video-image-search-indexer-quality checkout "$DFI_BRANCH"
  git -C video-image-search-indexer-quality pull --ff-only origin "$DFI_BRANCH"
fi

if [[ ! -d google-drive-connector/.git ]]; then
  git clone --depth 1 --branch main https://github.com/Codenuclei/google-drive-connector.git
else
  git -C google-drive-connector fetch origin main
  git -C google-drive-connector checkout main
  git -C google-drive-connector pull --ff-only origin main
fi

STAGE=/opt/dfi/stage
DEST=/opt/dfi/video-image-search-indexer-quality/deploy/search
mkdir -p "$DEST"
cp -f "$STAGE/Caddyfile" "$DEST/Caddyfile"
cp -f "$STAGE/docker-compose.yml" "$DEST/docker-compose.yml"
cp -f "$STAGE/backend.env" "$DEST/backend.env"
cp -f "$STAGE/connector.env" "$DEST/connector.env"
cp -f "$STAGE/compose.env" "$DEST/.env"
chmod 600 "$DEST/backend.env" "$DEST/connector.env" "$DEST/.env"

# Overlay SSL connect_args (not yet on GitHub main). Railway-safe if DATABASE_SSL unset.
cp -f "$STAGE/session.py" /opt/dfi/video-image-search-indexer-quality/backend/app/db/session.py

cd "$DEST"
docker compose --env-file .env build
docker compose --env-file .env up -d
docker compose ps
