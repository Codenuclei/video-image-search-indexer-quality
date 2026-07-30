# Local stack, public HTTPS tunnels, and Google OAuth

How the Mac Mini serves this app to the outside world, and why the two Google
Cloud Console values no longer change.

## TL;DR

The two values you paste into Google Cloud Console are now **localhost URLs**:

| Google Cloud Console field    | Value                                        |
| ----------------------------- | -------------------------------------------- |
| Authorized JavaScript origin  | `http://localhost:3001`                      |
| Authorized redirect URI       | `http://localhost:8000/auth/google/callback` |

Google exempts `localhost` from its "HTTPS only" rule, so these are legal, and
they are **stable forever** — restarting tunnels, rebooting the Mac Mini, or
Cloudflare handing out brand-new hostnames cannot change them.

Print them any time:

```bash
scripts/sync-tunnel-env.sh --print
```

## Why OAuth is pinned to localhost

Cloudflare **quick** tunnels (`cloudflared tunnel --url`) mint a new random
`*.trycloudflare.com` hostname on every restart. Google won't accept a LAN IP,
so previously every tunnel restart meant re-pasting two values into the Console.

The app stores a **single shared Google Drive account** (the backend reads
`DriveUser` with `limit(1)`), so connecting Drive is a one-time *admin* action,
not something each visitor does. That means OAuth never has to happen over the
public URL:

- **Connecting Drive / picking a folder** → do it in a browser **on this Mac
  Mini** at <http://localhost:3001/folders>. Uses the stable localhost values.
- **Everything else** (search, carousel, indexer status) → works fine over the
  public tunnel URL for anyone, because it needs no Google sign-in. The stored
  refresh token keeps Drive access alive for all users.

So tunnel churn no longer touches OAuth at all.

## Where the URLs live — one source of truth

```
scripts/tunnel-config.sh        ← config you edit by hand (mode, hostnames, OAuth policy)
data/imports/tunnel-urls.env    ← current public URLs, written by the tunnel starter
        │
        └── scripts/sync-tunnel-env.sh  regenerates BOTH env files:
                frontend/.env.local   NEXT_PUBLIC_API_URL, NEXT_PUBLIC_GOOGLE_CLIENT_ID, …
                backend/.env          FRONTEND_URL, ALLOWED_ORIGINS,
                                      PUBLIC_BASE_URL, GOOGLE_REDIRECT_URI
```

**Never hand-edit `frontend/.env.local` or those four `backend/.env` keys** —
they are derived and will be overwritten. Change `scripts/tunnel-config.sh`
instead and re-run the sync.

`ALLOWED_ORIGINS` is rebuilt from scratch on every sync (localhost + 127.0.0.1 +
LAN IP + the current tunnel hostnames). That is what stops stale
`trycloudflare.com` origins from piling up, which is how the two env files
drifted apart before.

`backend/.env` is patched in place: only those four keys change, every other
line and secret is left byte-identical, and the previous copy is saved to
`backend/.env.bak-tunnel-sync`.

## Start / stop

Two `screen` sessions, deliberately independent so tunnels can be restarted
without disturbing anyone testing the app:

| Session       | What it runs                                    | Script                        |
| ------------- | ----------------------------------------------- | ----------------------------- |
| `dfi-stack`   | Postgres check, video volume, uvicorn :8000, next dev :3001 | `scripts/keep-local-stack.sh` |
| `dfi-tunnels` | cloudflared                                     | `scripts/start-https-tunnels.sh` |

```bash
screen -ls                          # what's running
screen -r dfi-tunnels               # attach (detach again with Ctrl-A then D)

scripts/tunnels-restart.sh          # restart ONLY tunnels + re-sync env
                                    # leaves :8000 and :3001 running

scripts/boot-local-stack.sh         # bring everything up (idempotent)
scripts/install-launch-agent.sh     # optional: also do that after every reboot
scripts/install-launch-agent.sh --uninstall
```

After a tunnel restart the new URLs are picked up automatically. One caveat: the
backend reads `.env` **at startup**, so a changed `ALLOWED_ORIGINS` only applies
once uvicorn restarts. `dfi-stack` respawns it automatically if it exits; to
force it:

```bash
pkill -f 'uvicorn app.main:app'     # dfi-stack restarts it within ~2s
```

Useful logs, all under `data/imports/`: `cloudflared-backend.log`,
`cloudflared-frontend.log`, `cloudflared-named.log`, `keep-local-stack.log`,
`backend-uvicorn.log`, `frontend-next.log`.

## Optional upgrade: stable *public* hostnames (named tunnel)

Pinning OAuth to localhost already solves the Console-churn problem, but the
public URL people visit still changes on every restart. If you want a permanent
public hostname such as `https://dfi.yourdomain.com`, use a Cloudflare **named**
tunnel.

**Requirements** (Cloudflare's, not ours):

1. A Cloudflare account — free plan is fine.
2. A domain whose **nameservers are delegated to Cloudflare**. Named tunnels map
   a hostname *you own*; there is no way to reserve a fixed
   `trycloudflare.com` name. Where you bought the domain doesn't matter, but DNS
   must be on Cloudflare.
3. One interactive browser login (below). Everything else is scripted.

If you don't have such a domain, stay on quick tunnels with
`OAUTH_MODE=localhost` — the Console values are already stable, and this costs
nothing.

### The one-time command

```bash
cloudflared tunnel login
```

What to click:

1. A browser tab opens at `dash.cloudflare.com`.
2. Log in / choose the account that owns your domain.
3. Cloudflare lists the zones (domains) on the account. **Click your domain**,
   then click the blue **Authorize** button.
4. The page says "You have successfully logged in" — close the tab.
5. `~/.cloudflared/cert.pem` now exists.

Then, with your domain:

```bash
scripts/tunnel-named-setup.sh yourdomain.com
# → https://dfi.yourdomain.com      (frontend :3001)
# → https://dfi-api.yourdomain.com  (backend  :8000)

# or choose both hostnames explicitly:
scripts/tunnel-named-setup.sh app.yourdomain.com api.yourdomain.com
```

That script is idempotent and does everything else: creates the named tunnel,
writes `~/.cloudflared/config-dfi.yml` with ingress for both hostnames, adds the
two DNS CNAMEs, and flips `scripts/tunnel-config.sh` to `TUNNEL_MODE=named`.
Finally:

```bash
scripts/tunnels-restart.sh
```

Keep hostnames to a **single** subdomain level. Cloudflare's free Universal TLS
cert covers `yourdomain.com` and `*.yourdomain.com`, but not
`a.b.yourdomain.com`, which would need a paid Advanced Certificate.

### Moving OAuth onto the public hostnames

Optional. Once on a named tunnel the public hostnames are stable too, so OAuth
can use them (this is what lets people connect Drive from *off* the Mac Mini):

1. Set `OAUTH_MODE=tunnel` in `scripts/tunnel-config.sh`.
2. Run `scripts/sync-tunnel-env.sh` and paste the two printed values into the
   Google Console — once. They won't change again.

The sync script warns if you combine `OAUTH_MODE=tunnel` with
`TUNNEL_MODE=quick`, since that is exactly the churn we're escaping.

## Which API URL the browser uses

No single build-time URL can be right for every visitor: loopback is fast but
only works on this machine, while an HTTPS tunnel page **cannot** call
`http://127.0.0.1:8000` at all (the browser blocks it as mixed content). Picking
one statically means the other case breaks.

So the frontend decides at runtime, from the origin the page was served from.
`resolveApiBase()` in `frontend/src/lib/api.ts`:

| Page served from        | Backend it calls              | Why                         |
| ----------------------- | ----------------------------- | --------------------------- |
| `localhost` / `127.0.0.1` | `http://127.0.0.1:8000`     | fast; never leaves the machine |
| LAN IP, e.g. `192.168.42.23:3001` | `http://192.168.42.23:8000` | direct on the LAN, no tunnel hop |
| Cloudflare tunnel (https) | the public HTTPS backend URL | only protocol that isn't blocked |

The sync script writes all three inputs into `frontend/.env.local`
(`NEXT_PUBLIC_API_URL` for public access, `NEXT_PUBLIC_API_URL_LOCAL` for
loopback, `NEXT_PUBLIC_API_PORT` for the LAN case). **Local work is on loopback
by default and remote access still works, with no flag to flip.**

`API_URL_MODE` in `scripts/tunnel-config.sh` only exists to override that:

- `auto` (default) — runtime resolution as above.
- `loopback` — force loopback for everyone. Fastest locally; breaks remote access.
- `tunnel` — force the public URL for everyone, including this machine. Slower
  locally and long requests can stall. Avoid.

`PUBLIC_BASE_URL` always tracks the public backend URL regardless of this
setting, because Google must be able to reach face thumbnails.

## Long requests do not survive a Cloudflare tunnel

Carousel "Extract hooks & topics" is genuinely slow — measured **137 seconds**
for a single theme on `How to Get Your First 10 Customers`. That matters:

- On loopback it completes fine. There is no client-side `fetch` timeout in
  `api()` and uvicorn imposes no request timeout, so nothing cuts it off.
- Through a Cloudflare tunnel it does **not** complete. The identical request
  returned **HTTP 524 "A timeout occurred"** after 126 seconds — Cloudflare's
  edge gave up before the backend answered. In the UI that surfaces as the
  "Lost connection to DFI / Socket disconnected" overlay.

Practical consequence: **run extract/generate on the Mac Mini itself.** Remote
tunnel access is fine for browsing, search and reviewing saved results, but the
long AI steps should be driven locally. Making them work remotely needs the
endpoint to become a job you poll rather than one long request — a backend
change, not a tunnel setting.

## Other notes

- Quick tunnels are capped at 200 concurrent requests and do not support
  Server-Sent Events. The app doesn't use SSE on any active route, so this
  currently costs nothing — but it's another reason to prefer a named tunnel.
- `PUBLIC_BASE_URL` is set to the **public backend** URL, not loopback, because
  Google has to fetch `/faces/{id}/thumbnail` for reverse image search. The sync
  script keeps this correct automatically; it used to be wrong (loopback).
- `backend/.env*` is gitignored, so the sync backup never lands in git.
