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


### Example .env file
```
LIVETRACK_EMAIL_HOST = "imap.gmx.net"
LIVETRACK_EMAIL_USERNAME = "email123@gmx.de"
LIVETRACK_EMAIL_PASSWORD = "ur-password"

LIVETRACK_SIGNAL_API = "http://signal-api:8080"
LIVETRACK_SENDER_PHONE_NUMBER = "+49123456789"
LIVETRACK_RECIPIENT_PHONE_NUMBERS = "+49123456789,+49987654321"
```
