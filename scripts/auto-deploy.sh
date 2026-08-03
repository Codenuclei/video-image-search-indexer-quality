#!/usr/bin/env bash
# Unix companion to scripts/auto-deploy.ps1 (Windows deploy-mirror watcher).
# Same contract: clean tree, ff-only to origin/main, then railway up for
# backend/, frontend/, and/or carousel-frontend/ when those paths changed.
#
# Usage:
#   scripts/auto-deploy.sh           # only when behind origin/main
#   scripts/auto-deploy.sh --force   # deploy all three app services for current HEAD

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BRANCH=main
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/auto-deploy.log"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

mkdir -p "$LOG_DIR"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a "$LOG"; }

cd "$REPO"
git fetch origin "$BRANCH" --quiet

local_rev=$(git rev-parse HEAD)
remote_rev=$(git rev-parse "origin/$BRANCH")

if [[ "$FORCE" -eq 0 && "$local_rev" == "$remote_rev" ]]; then
  log "No changes (HEAD=${local_rev:0:7}). Skip."
  exit 0
fi

if [[ -n "$(git status --porcelain)" ]]; then
  # Allow untracked-only noise (volume-clone helpers, etc.) but never dirty tracked files.
  if [[ -n "$(git status --porcelain -uno)" ]]; then
    log "Working tree dirty - skipping auto-deploy to avoid clobbering local edits."
    exit 1
  fi
fi

if [[ "$FORCE" -eq 1 ]]; then
  # Redeploy all app services for the commit already on origin/main.
  deploy_backend=1
  deploy_frontend=1
  deploy_carousel=1
  log "Force deploy at ${local_rev:0:7} | backend=1 frontend=1 carousel=1"
else
  changed=$(git diff --name-only "$local_rev" "$remote_rev" || true)
  deploy_backend=0
  deploy_frontend=0
  deploy_carousel=0
  echo "$changed" | grep -q '^backend/' && deploy_backend=1 || true
  echo "$changed" | grep -q '^frontend/' && deploy_frontend=1 || true
  echo "$changed" | grep -q '^carousel-frontend/' && deploy_carousel=1 || true
  git merge --ff-only "origin/$BRANCH" >/dev/null
  log "Updated ${local_rev:0:7} -> ${remote_rev:0:7} | backend=$deploy_backend frontend=$deploy_frontend carousel=$deploy_carousel"
fi

if [[ "$deploy_backend" -eq 1 ]]; then
  log "Deploying backend (railway up dfi-backend)..."
  (cd "$REPO/backend" && railway up --service dfi-backend --detach -y) | tee -a "$LOG"
fi
if [[ "$deploy_frontend" -eq 1 ]]; then
  log "Deploying frontend (railway up dfi-frontend)..."
  (cd "$REPO/frontend" && railway up --service dfi-frontend --detach -y) | tee -a "$LOG"
fi
if [[ "$deploy_carousel" -eq 1 ]]; then
  # Service Root Directory = carousel-frontend; upload from repo (not --path-as-root)
  # so /carousel-frontend/railway.json resolves. GitHub source is also connected.
  log "Deploying carousel (railway up dfi-carousel)..."
  (cd "$REPO" && railway up --service dfi-carousel --detach -y) | tee -a "$LOG"
fi
if [[ "$deploy_backend" -eq 0 && "$deploy_frontend" -eq 0 && "$deploy_carousel" -eq 0 ]]; then
  log "Changes were outside backend/, frontend/, and carousel-frontend/ - nothing to deploy."
fi
log "Done."
