#!/bin/bash
# Rewire every public-URL setting from one source of truth.
#
#   scripts/tunnel-config.sh      → mode + OAuth policy (committed, hand-edited)
#   data/imports/tunnel-urls.env  → current tunnel hostnames (written by
#                                   start-https-tunnels.sh)
#
# and regenerates, in one shot:
#   frontend/.env.local  NEXT_PUBLIC_API_URL, NEXT_PUBLIC_GOOGLE_CLIENT_ID, …
#   backend/.env         FRONTEND_URL, ALLOWED_ORIGINS, PUBLIC_BASE_URL,
#                        GOOGLE_REDIRECT_URI
#
# Idempotent: safe to run repeatedly. ALLOWED_ORIGINS is rebuilt from scratch
# every run so stale trycloudflare hostnames cannot accumulate.
#
# Usage:
#   scripts/sync-tunnel-env.sh            # rewire, then print Google Console values
#   scripts/sync-tunnel-env.sh --print    # print only, change nothing
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/tunnel-config.sh"

PRINT_ONLY=0
[ "${1:-}" = "--print" ] && PRINT_ONLY=1

BE_ENV=$ROOT/backend/.env
FE_ENV=$ROOT/frontend/.env.local

# ── Load current tunnel hostnames (may be absent on a cold boot) ──────────────
# tunnel-config.sh is authoritative for the mode; the URL file also records a
# TUNNEL_MODE (for debugging) which must not win when the config has moved on.
CONFIGURED_MODE=$TUNNEL_MODE
BACKEND_TUNNEL_URL=""
FRONTEND_TUNNEL_URL=""
if [ -f "$URL_FILE" ]; then
  # shellcheck disable=SC1090
  . "$URL_FILE"
fi
TUNNEL_MODE=$CONFIGURED_MODE

# In named mode the hostnames are known up front and never change.
if [ "$TUNNEL_MODE" = "named" ]; then
  [ -n "$NAMED_BE_HOSTNAME" ] && BACKEND_TUNNEL_URL="https://$NAMED_BE_HOSTNAME"
  [ -n "$NAMED_FE_HOSTNAME" ] && FRONTEND_TUNNEL_URL="https://$NAMED_FE_HOSTNAME"
fi

# ── Derive every public URL ───────────────────────────────────────────────────
# NEXT_PUBLIC_API_URL is the *public* backend URL, used by browsers that are not
# on this machine. The frontend picks between it and loopback at runtime from
# window.location (see resolveApiBase in frontend/src/lib/api.ts), so local use
# stays on loopback without any flag. API_URL_MODE can force either side.
LOOPBACK_API="http://127.0.0.1:$BE_PORT"
case $API_URL_MODE in
  loopback)
    API_URL="$LOOPBACK_API"
    ;;
  tunnel)
    API_URL="${BACKEND_TUNNEL_URL:-$LOOPBACK_API}"
    ;;
  *)
    # auto: advertise the tunnel for remote visitors; the frontend still uses
    # loopback whenever the page itself was served from localhost.
    API_URL="${BACKEND_TUNNEL_URL:-$LOOPBACK_API}"
    ;;
esac

if [ "$OAUTH_MODE" = "tunnel" ] && [ -n "$BACKEND_TUNNEL_URL" ] && [ -n "$FRONTEND_TUNNEL_URL" ]; then
  OAUTH_ORIGIN="$FRONTEND_TUNNEL_URL"
  OAUTH_REDIRECT="$BACKEND_TUNNEL_URL/auth/google/callback"
  FRONTEND_URL="$FRONTEND_TUNNEL_URL"
else
  # localhost mode (default): stable forever, exempt from Google's HTTPS rule.
  OAUTH_ORIGIN="http://localhost:$FE_PORT"
  OAUTH_REDIRECT="http://localhost:$BE_PORT/auth/google/callback"
  FRONTEND_URL="http://localhost:$FE_PORT"
fi

# Google must be able to fetch /faces/{id}/thumbnail for reverse image search,
# so this always tracks the public backend URL even when the browser bundle is
# pointed at loopback.
PUBLIC_BASE_URL="${BACKEND_TUNNEL_URL:-http://127.0.0.1:$BE_PORT}"

# Rebuilt from scratch each run — this is what stops origin drift.
ORIGINS="http://localhost:$FE_PORT,http://127.0.0.1:$FE_PORT,http://$LAN_IP:$FE_PORT"
[ -n "$FRONTEND_TUNNEL_URL" ] && ORIGINS="$ORIGINS,$FRONTEND_TUNNEL_URL"
[ -n "$BACKEND_TUNNEL_URL" ] && ORIGINS="$ORIGINS,$BACKEND_TUNNEL_URL"

print_summary() {
  cat <<SUMMARY

  tunnel mode   : $TUNNEL_MODE
  oauth mode    : $OAUTH_MODE
  frontend URL  : ${FRONTEND_TUNNEL_URL:-(no tunnel)}
  backend  URL  : ${BACKEND_TUNNEL_URL:-(no tunnel)}
  api url mode  : $API_URL_MODE
  FE → API      : $LOOPBACK_API        (browsing from this Mac Mini)
                  $API_URL        (browsing from anywhere else)

  ── Paste these two into Google Cloud Console ──────────────────────────────
  APIs & Services → Credentials → your OAuth 2.0 Client ID

    Authorized JavaScript origin : $OAUTH_ORIGIN
    Authorized redirect URI      : $OAUTH_REDIRECT

SUMMARY
  if [ "$OAUTH_MODE" = "localhost" ]; then
    echo "  These are localhost URLs — they never change, so you should never"
    echo "  need to touch the Google Console again."
  elif [ "$TUNNEL_MODE" = "named" ]; then
    echo "  Named-tunnel hostnames — stable across restarts and reboots."
  else
    echo "  WARNING: OAUTH_MODE=tunnel with TUNNEL_MODE=quick means these change"
    echo "  on every restart. Switch to OAUTH_MODE=localhost or run"
    echo "  scripts/tunnel-named-setup.sh."
  fi
  echo
}

if [ "$PRINT_ONLY" = "1" ]; then
  print_summary
  exit 0
fi

# ── frontend/.env.local ───────────────────────────────────────────────────────
CLIENT_ID="$(grep -E '^GOOGLE_CLIENT_ID=' "$BE_ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]')"
SUPPORT_EMAIL="${SUPPORT_EMAIL:-abhishek.ghosh1@mastersunion.org}"
{
  echo "# Generated by scripts/sync-tunnel-env.sh — do not hand-edit."
  echo "# Public backend URL, for browsers that are not on this machine."
  echo "NEXT_PUBLIC_API_URL=$API_URL"
  echo "# Loopback backend, used whenever the page is served from localhost."
  echo "NEXT_PUBLIC_API_URL_LOCAL=$LOOPBACK_API"
  echo "# Backend port, for deriving a LAN URL from window.location."
  echo "NEXT_PUBLIC_API_PORT=$BE_PORT"
  echo "NEXT_PUBLIC_SUPPORT_EMAIL=$SUPPORT_EMAIL"
  [ -n "$CLIENT_ID" ] && echo "NEXT_PUBLIC_GOOGLE_CLIENT_ID=$CLIENT_ID"
} >"$FE_ENV"

# ── backend/.env (in-place key upsert; every other line untouched) ────────────
if [ ! -f "$BE_ENV" ]; then
  echo "sync-tunnel-env: missing $BE_ENV" >&2
  exit 1
fi

BE_CHANGED=$(
  FRONTEND_URL="$FRONTEND_URL" \
  ALLOWED_ORIGINS="$ORIGINS" \
  PUBLIC_BASE_URL="$PUBLIC_BASE_URL" \
  GOOGLE_REDIRECT_URI="$OAUTH_REDIRECT" \
  BE_ENV="$BE_ENV" \
  python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["BE_ENV"])
desired = {
    key: os.environ[key]
    for key in ("FRONTEND_URL", "ALLOWED_ORIGINS", "PUBLIC_BASE_URL", "GOOGLE_REDIRECT_URI")
}

original = path.read_text()
lines = original.splitlines()
seen = set()
out = []
for line in lines:
    stripped = line.lstrip()
    key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
    if key in desired:
        # Keep the first occurrence, drop later duplicates.
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{key}={desired[key]}")
    else:
        out.append(line)

for key, value in desired.items():
    if key not in seen:
        out.append(f"{key}={value}")

updated = "\n".join(out) + "\n"
if updated != original:
    (path.parent / f"{path.name}.bak-tunnel-sync").write_text(original)
    tmp = path.parent / f"{path.name}.tmp-tunnel-sync"
    tmp.write_text(updated)
    tmp.replace(path)
    print("changed")
PY
)

echo "sync-tunnel-env: frontend/.env.local written (NEXT_PUBLIC_API_URL=$API_URL)"
if [ "$BE_CHANGED" = "changed" ]; then
  echo "sync-tunnel-env: backend/.env updated (backup: backend/.env.bak-tunnel-sync)"
  echo "sync-tunnel-env: NOTE backend reads these at startup — restart uvicorn to apply."
else
  echo "sync-tunnel-env: backend/.env already correct"
fi
print_summary
