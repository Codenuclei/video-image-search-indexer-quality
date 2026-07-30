#!/bin/bash
# Tunnel + public-URL configuration for the Mac Mini local stack.
# Sourced by start-https-tunnels.sh, sync-tunnel-env.sh, tunnel-named-setup.sh
# and keep-local-stack.sh. Edit here only — never hand-edit backend/.env or
# frontend/.env.local, they are regenerated from this file plus the runtime
# URL file (data/imports/tunnel-urls.env).
#
# See docs/local-tunnels.md for the full story.

ROOT=${ROOT:-/Users/mu-mac_3/Projects/video-image-search-indexer-quality}
LOGDIR=${LOGDIR:-$ROOT/data/imports}
URL_FILE=${URL_FILE:-$LOGDIR/tunnel-urls.env}

# Local services. The DFI API always binds :8000 (8002 is Fennec only).
BE_PORT=8000
FE_PORT=3001
LAN_IP=192.168.42.23

# ── Which tunnel flavour to run ───────────────────────────────────────────────
# quick — cloudflared quick tunnels. No account needed, but Cloudflare mints a
#         NEW random *.trycloudflare.com hostname on every restart.
# named — cloudflared named tunnel with hostnames you own. Stable across
#         restarts/reboots. Requires a one-time `cloudflared tunnel login` and a
#         domain whose DNS is managed by Cloudflare.
#         Run scripts/tunnel-named-setup.sh to switch over.
TUNNEL_MODE=${TUNNEL_MODE:-quick}

# Only used when TUNNEL_MODE=named. Filled in by tunnel-named-setup.sh.
NAMED_TUNNEL=${NAMED_TUNNEL:-dfi-local}
NAMED_BE_HOSTNAME=${NAMED_BE_HOSTNAME:-}
NAMED_FE_HOSTNAME=${NAMED_FE_HOSTNAME:-}

# ── Which URLs Google OAuth uses ──────────────────────────────────────────────
# localhost — pin OAuth to http://localhost:$FE_PORT + http://localhost:$BE_PORT.
#             Google exempts localhost from its HTTPS rule, so these two values
#             are stable FOREVER and survive every tunnel restart. Drive connect
#             and folder picking must then be done in a browser on this Mac Mini
#             (they are one-time admin actions — the app stores a single shared
#             Drive account, so remote users never sign in themselves).
# tunnel    — use the public tunnel hostnames for OAuth. Only pick this with
#             TUNNEL_MODE=named, otherwise the Google Console values churn on
#             every restart.
OAUTH_MODE=${OAUTH_MODE:-localhost}

# ── Which API URL the browser uses ────────────────────────────────────────────
# auto     — (default) the frontend decides per-visitor, at runtime, from the
#            origin the page was served from:
#              localhost/127.0.0.1 → http://127.0.0.1:$BE_PORT  (fast, no tunnel)
#              LAN IP              → http://<that IP>:$BE_PORT
#              tunnel hostname     → the public HTTPS backend URL
#            Local work stays on loopback and remote access still works, with no
#            flag to flip. See resolveApiBase() in frontend/src/lib/api.ts.
# loopback — force loopback for everyone. Fastest locally, but the app breaks for
#            remote/tunnel visitors. Only for local-only debugging.
# tunnel    — force the public backend URL for everyone, including this machine.
#            Slower locally and long requests can stall on a quick tunnel.
API_URL_MODE=${API_URL_MODE:-auto}
