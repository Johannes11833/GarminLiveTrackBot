# Garmin Livetrack Signal Bot

## Table of Contents
- [Garmin Livetrack Signal Bot](#garmin-livetrack-signal-bot)
  - [Table of Contents](#table-of-contents)
  - [Docker Setup](#docker-setup)
    - [Docker compose setup](#docker-compose-setup)
    - [Run livetrack bot w/o a container](#run-livetrack-bot-wo-a-container)
    - [Example .env file](#example-env-file)

## Docker Setup
### Docker compose setup

First setup your .env file as described below.
Then run this command to startup the compose project and display only the logs for the garmin-livetrack bot:

```bash
docker compose up garmin-livetrack
```

On the first run, open the qr-code generated in the data folder of the bot and scan it with the signal app.

### Run livetrack bot w/o a container
```bash
docker run --rm --name signal-api -p 8080:8080 -v "./signal-api-data:/home/.local/share/signal-cli" -e 'MODE=normal' bbernhard/signal-cli-rest-api
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

Start tracking explicitly by posting a LiveTrack URL. Each session has its own
Playwright worker and can run alongside other sessions:

```bash
curl -X POST http://127.0.0.1:8000/trackings -H "Content-Type: application/json" -d '{"url":"https://livetrack.garmin.com/session/<id>/token/<token>"}'
```

- `GET /trackings` lists all tracking sessions.
- `GET /trackings/{session_id}` returns a session's status and counts.
- `GET /trackings/{session_id}/track` returns accumulated track points.
- `GET /trackings/{session_id}/course` returns the current planned course.
- `DELETE /trackings/{session_id}` requests that session stop.

Interactive OpenAPI documentation is available at `http://127.0.0.1:8000/docs`.

### Web viewer & push notifications

The viewer in `garmin_livetrack_viewer/` is a Flutter web app that shows a
tracking session and can receive push notifications when a session starts or
ends. It uses the standard Web Push API (VAPID), no Firebase or other
third-party push service.

Build the viewer (the `API_BASE_URL` dart-define is optional; when empty the
app talks to the same origin as the page):

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
https://livetrack.example.com/?id=<session id>&token=<the token>
```

The bell icon is only shown when a token is present in the URL. All registered
devices receive a notification for every session that starts or ends
("LiveTrack started" / "LiveTrack ended", with the session name as body).
Tapping the notification opens the viewer at that session. Registered
subscriptions can be listed (with the token):

```bash
curl "https://livetrack.example.com/push/subscriptions?token=<the token>"
```

Deploy with HTTPS: service workers and Web Push require a secure context. The
included `Caddyfile` serves the built viewer and proxies the API
(`/trackings`, `/push`) on the same origin. Point the Caddyfile domain at your
server and run:

```bash
docker compose up -d
```

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

LIVETRACK_SIGNAL_API = "http://signal-api:8080"
LIVETRACK_SENDER_PHONE_NUMBER = "+49123456789"
LIVETRACK_RECIPIENT_PHONE_NUMBERS = "+49123456789,+49987654321"

LIVETRACK_PUSH_TOKEN = "change-me"
```
