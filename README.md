<h1>Garmin Livetrack Bot</h1>

<h2>Table of Contents</h2>

- [Docker Setup](#docker-setup)
  - [Docker compose setup](#docker-compose-setup)
  - [Run livetrack bot w/o a container](#run-livetrack-bot-wo-a-container)
  - [Extract LiveTrack data](#extract-livetrack-data)
  - [LiveTrack REST API](#livetrack-rest-api)
  - [Web viewer \& push notifications](#web-viewer--push-notifications)
  - [Registration token](#registration-token)
  - [Example .env file](#example-env-file)

## Docker Setup
### Docker compose setup

First setup your .env file as described below. Then start the compose project
(use `--build` to build the image from the local source instead of pulling it):

```bash
docker compose up -d --build
```

The email listener watches the configured mailbox and automatically starts a
tracking session through the API for every received Garmin LiveTrack link.
Registered devices then receive push notifications (see "Web viewer & push
notifications" below).

### Run livetrack bot w/o a container

```bash
# terminal 1: API
poetry run garmin-livetrack-api

# terminal 2: email listener (feeds URLs into the API)
poetry run garmin-livetrack-email-listener
```

### Extract LiveTrack data

Garmin blocks direct API clients, so the extractor uses Chromium to make
same-origin API requests. Install the dependency and browser once, then pass a
LiveTrack share URL:

```bash
poetry install
poetry run playwright install chromium
poetry run python garmin_livetrack/playwright_livetrack.py "https://livetrack.garmin.com/session/<id>/token/<token>"
```

The extractor writes `session.json`, `track.json`, and (when available)
`course.json` in `garmin_livetrack/`. Press `Ctrl+C` to stop polling.

### LiveTrack REST API

Run the independent multi-session API with:

```bash
poetry install
poetry run playwright install chromium
poetry run garmin-livetrack-api
```

Starting and stopping a tracking session requires the shared `LIVETRACK_API_TOKEN`
(see below):

```bash
curl -X POST http://127.0.0.1:8000/trackings \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <the api token>" \
  -d '{"url":"https://livetrack.garmin.com/session/<id>/token/<token>"}'
```

Each session has its own Playwright worker and can run alongside other sessions.

To try the viewer UI without a real Garmin LiveTrack link, enable dummy mode
(`LIVETRACK_ENABLE_DUMMY_MODE=1` in `.env`). A simulated session then starts
automatically 5 seconds after the API comes up -- it walks a synthetic route
with fake speed/elevation/heart-rate data for 5 minutes, then ends itself.
The API logs the session id and token it started, e.g.:

```
[dummy] Started simulated session: id=<id> token=<token>
[dummy] Open the viewer with: ?id=<id>&sessionToken=<token>
```

- `GET /trackings` lists all tracking sessions (also requires the API token).
- `GET /trackings/{session_id}/token/{token}` returns a session's status and counts.
- `GET /trackings/{session_id}/token/{token}/track` returns accumulated track points.
- `GET /trackings/{session_id}/token/{token}/course` returns the current planned course.
- `GET /trackings/{session_id}/token/{token}/profile-image` returns the user's profile photo.
- `POST /trackings/{session_id}/token/{token}/message` sends a spectator message.
- `DELETE /trackings/{session_id}` requests that session stop (also requires the API token).

Every per-session read/write endpoint above (except starting/stopping) requires
the Garmin session's own share token (the `<token>` from the original
`livetrack.garmin.com/session/<id>/token/<token>` URL) as a path segment,
mirroring Garmin's own URL shape, so knowing/guessing a session id alone isn't
enough to read or interact with someone's live location -- the viewer passes
this automatically from its `?sessionToken=` URL parameter (see below).

Interactive OpenAPI documentation is available at `http://127.0.0.1:8000/docs`.

### Web viewer & push notifications

The viewer in `garmin_livetrack_viewer/` is a Flutter web app that shows a
tracking session and can receive push notifications when a session starts or
ends. It uses the standard Web Push API (VAPID), no Firebase or other
third-party push service.

`docker compose up -d --build` builds and serves the viewer automatically
(`Dockerfile.viewer`, reverse-proxied by Caddy). To build it by hand instead
(the `API_BASE_URL` dart-define is optional; when empty the app talks to the
same origin as the page):

```bash
cd garmin_livetrack_viewer
flutter build web
```

VAPID keys are generated automatically on first start and stored in
`garmin-livetrack-data/vapid_keys.json`. You can override them (and set a
contact email for the push service) via the environment:

```
LIVETRACK_VAPID_PUBLIC_KEY = "..."
LIVETRACK_VAPID_PRIVATE_KEY = "..."
LIVETRACK_VAPID_CONTACT_EMAIL = "mailto:you@example.com"
```

### Registration token

Only people who know the shared registration token can subscribe. Set it via
the environment (generate one with e.g. `openssl rand -hex 32`):

```
LIVETRACK_PUSH_TOKEN = "...."
```

A visitor subscribes by opening the viewer with the token in the URL and
tapping the bell icon in the app bar:

```
https://livetrack.example.com/?id=<session id>&token=<the push token>&sessionToken=<the garmin token>
```

`token` is the push-registration token above; `sessionToken` is the Garmin
LiveTrack share token, required for the viewer to load anything for that
session at all (see "LiveTrack REST API") -- without it the app just shows
"Tracking not found." The bell icon is only shown when a push token is present
in the URL. All registered devices receive a notification for every session
that starts or ends ("LiveTrack started" / "LiveTrack ended", with the session
name as body); the notification carries the session's Garmin token too, so
tapping it opens the viewer with both parameters already set.

Deploy with HTTPS: service workers and Web Push require a secure context. The
included `Caddyfile` serves the built viewer and proxies the API
(`/trackings`, `/push`) on the same origin.

**Option A: Cloudflare Tunnel (recommended, no open inbound ports)**

1. In the Cloudflare Zero Trust dashboard, go to Networks > Tunnels > Create
   a tunnel (Cloudflared connector), then add a Public Hostname pointing at
   `http://caddy:80`.
2. Put the tunnel token in `.env`:
   ```
   CLOUDFLARE_TUNNEL_TOKEN = "..."
   ```
3. Run:
   ```bash
   docker compose up -d
   ```

Cloudflare terminates HTTPS at its edge and forwards plain HTTP to Caddy
through the tunnel, so no domain/certificate setup is needed on the server,
and ports 80/443 don't need to be open on your firewall at all.

**Option B: expose Caddy directly**

Replace the `:80 { import routes }` block in `Caddyfile` with your own
domain, so Caddy obtains its own Let's Encrypt certificate:

```
livetrack.example.com {
	import routes
}
```

Then point that domain's DNS at the server, open ports 80/443, and run
`docker compose up -d` (without `CLOUDFLARE_TUNNEL_TOKEN` set, the
`cloudflared` service will just fail to start and restart-loop harmlessly;
remove it from `compose.yml` if you don't need it).

Notes:
- On iOS (16.4+), push works only after the app is installed ("Add to Home
  Screen"); the permission prompt then comes from a user tap.
- Notifications require HTTPS on all supported browsers; the button shows a
  hint when the app is not in a secure context.

### Example .env file
```
LIVETRACK_EMAIL_HOST = "imap.gmx.net"
LIVETRACK_EMAIL_USERNAME = "email123@gmx.de"
LIVETRACK_EMAIL_PASSWORD = "ur-password"

LIVETRACK_PUSH_TOKEN = "change-me"

# shared secret required to start/stop tracking sessions (POST/DELETE
# /trackings); generate one with e.g. `openssl rand -hex 32`
LIVETRACK_API_TOKEN = "change-me-too"

# optional: where the email listener finds the API
# (default http://127.0.0.1:8000; compose sets it to the api service)
LIVETRACK_API_URL = "http://127.0.0.1:8000"

# optional: set to "1" to auto-start a simulated tracking session 5s after
# the API boots, for UI testing without a real Garmin LiveTrack link
# (off by default)
LIVETRACK_ENABLE_DUMMY_MODE = "1"

# optional: only needed if using the cloudflared service to expose the app
# via a Cloudflare Tunnel instead of exposing Caddy directly (see README)
CLOUDFLARE_TUNNEL_TOKEN = "..."
```
