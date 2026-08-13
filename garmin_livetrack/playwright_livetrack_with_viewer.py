"""
Garmin LiveTrack extractor -- Playwright edition.

Garmin's LiveTrack API sits behind bot detection that blocks plain HTTP
clients, even with a spoofed browser User-Agent and TLS fingerprint
(curl_cffi). Rather than fight that further, this script drives a real
headless browser to load the actual LiveTrack page, and eavesdrops on the
network responses the page's own JavaScript receives -- the same JSON
that the earlier requests-based approach was trying to fetch directly.

Setup (one-time):
    pip install playwright
    playwright install chromium

Usage:
    python playwright_livetrack.py "https://livetrack.garmin.com/session/<id>/token/<token>"
"""

import re
import sys
import time
import json
import threading
import webbrowser
import http.server
import socketserver
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

from playwright.sync_api import sync_playwright

OUTPUT_DIR = Path(__file__).parent
TRACK_FILE = OUTPUT_DIR / "track.json"
SESSION_FILE = OUTPUT_DIR / "session.json"
COURSE_FILE = OUTPUT_DIR / "course.json"
SERVER_PORT = 8765

POLL_TIMEOUT_MS = 5000  # how often we nudge the event loop / check for stop

URL_RE = re.compile(
    r"session/(?P<session_id>[0-9a-fA-F-]+)/token/(?P<token>[0-9A-Za-z]+)"
)


def parse_livetrack_url(url: str):
    match = URL_RE.search(url)
    if not match:
        raise ValueError(f"Could not find session/token in URL: {url}")
    return match.group("session_id"), match.group("token")


def _normalize_point(raw: dict) -> dict:
    """
    Handles the confirmed current shape from
    /api/sessions/{id}/track-points/common:
        {
          "dateTime": "2026-08-09T20:58:04.000Z",
          "position": {"lat": ..., "lon": ...},
          "speedMetersPerSec": ..., "altitude": ...,
          "totalDistanceMeters": ..., "totalDurationSecs": ...,
          "activityType": "CYCLING", ...
        }
    while staying tolerant of the older top-level lat/lon/latitude/longitude
    shape too, in case Garmin changes it again.
    """
    pos = raw.get("position") or {}
    lat = raw.get("latitude", raw.get("lat", pos.get("lat")))
    lon = raw.get("longitude", raw.get("lon", raw.get("lng", pos.get("lon"))))

    ts_raw = raw.get("timestamp", raw.get("dateTime", raw.get("time")))
    if isinstance(ts_raw, str):
        try:
            ts = int(
                datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                * 1000
            )
        except ValueError:
            ts = None
    else:
        ts = ts_raw

    meta_data = raw.get("metaData") or raw.get("metadata") or {}
    if not meta_data:
        # Promote the real (2026) field names into the old-style metaData
        # shape the map viewer already expects (SPEED in m/s, distance in m).
        meta_data = {
            "SPEED": raw.get("speedMetersPerSec", raw.get("speed")),
            "ELEVATION": raw.get("altitude"),
            "TOTAL_DISTANCE": raw.get("totalDistanceMeters"),
            "TOTAL_DURATION": raw.get("totalDurationSecs"),
            "ACTIVITY_TYPE": raw.get("activityType"),
        }

    return {
        "latitude": lat,
        "longitude": lon,
        "timestamp": ts,
        "metaData": meta_data,
        "events": raw.get("events", []),
    }


def _normalize_course(raw: dict) -> list:
    """
    Normalize a /api/sessions/{id}/courses response into a flat list of
    {latitude, longitude} points describing the planned route.

    Actual confirmed shape:
        {
          "courses": [
            {
              "coursePoints": [
                {"position": {"lat": 48.690533, "lon": 9.326042}},
                {"position": {"lat": 48.689967, "lon": 9.325795}},
                ...
              ]
            },
            ...
          ]
        }
    A course can have multiple sub-courses; this flattens all of their
    coursePoints into a single ordered list. Points missing a usable
    position are skipped rather than inserted as None/None.
    """
    points = []
    for course in raw.get("courses") or []:
        for cp in course.get("coursePoints") or []:
            pos = cp.get("position") or {}
            lat, lon = pos.get("lat"), pos.get("lon")
            if lat is not None and lon is not None:
                points.append({"latitude": lat, "longitude": lon})
    return points


def _is_live(info: dict) -> bool:
    if not info.get("viewable", True):
        return False
    end = info.get("end")
    if not end:
        return True
    try:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < end_dt
    except ValueError:
        return True


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    tmp.replace(path)


def _serve_output_dir(port: int = SERVER_PORT):
    class QuietRequestHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

    handler = lambda *args, **kwargs: QuietRequestHandler(
        *args, directory=str(OUTPUT_DIR), **kwargs
    )
    # Try the preferred port first, then fall back to an OS-assigned free one
    # if it's already in use (e.g. a previous run's server never shut down).
    try:
        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    except OSError:
        httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        print(f"Port {port} was in use, using {httpd.server_address[1]} instead.")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, httpd.server_address[1]


def main():
    if len(sys.argv) < 2:
        print("Usage: python playwright_livetrack.py <livetrack_url>")
        sys.exit(1)
    url = sys.argv[1]
    session_id, token = parse_livetrack_url(url)
    session_api_url = f"https://livetrack.garmin.com/api/sessions/{session_id}"
    track_api_url = f"https://livetrack.garmin.com/api/sessions/{session_id}/track-points/common"
    course_api_url = f"https://livetrack.garmin.com/api/sessions/{session_id}/courses"

    state = {
        "track": [],
        "last_ts": None,
        "last_activity": 0.0,
        "session_info": None,
        "last_session_pos": None,
        "course": None,
        "csrf_token": None,
        "track_begin": None,
    }

    def _process_session_data(data: dict) -> bool:
        """
        Update state/session.json from a session-info payload, regardless of
        whether it came from the passive response listener or an active poll.
        Returns whether the session is still live.
        """
        data = dict(data)
        live = _is_live(data)
        data["sessionStatus"] = "InProgress" if live else "Expired"
        state["session_info"] = data
        if data.get("start"):
            state["track_begin"] = data["start"]
        _write_json(SESSION_FILE, data)

        pos = data.get("position")
        print(
            f"[session] {data.get('sessionName')} | {data.get('userDisplayName')} | "
            f"{'LIVE' if live else 'EXPIRED'}"
            + (f" | position={pos.get('lat')},{pos.get('lon')}" if pos else "")
        )

        if pos:
            lat, lon = pos.get("lat"), pos.get("lon")
            if lat is not None and lon is not None:
                last = state.get("last_session_pos")
                if last != (lat, lon):
                    state["last_session_pos"] = (lat, lon)
                    state["track"].append(
                        {
                            "latitude": lat,
                            "longitude": lon,
                            "timestamp": int(time.time() * 1000),
                            "metaData": {},
                            "events": [],
                        }
                    )
                    _write_json(TRACK_FILE, state["track"])
                    print(
                        f"[session-trail] position changed, appended point "
                        f"(total={len(state['track'])})"
                    )
        return live

    def _process_track_data(data: Any) -> None:
        raw_points = (
            data
            if isinstance(data, list)
            else (data.get("trackPoints") or data.get("points") or [])
            if isinstance(data, dict)
            else []
        )
        points = [_normalize_point(p) for p in raw_points if isinstance(p, dict)]
        geo_points = [
            p
            for p in points
            if p["latitude"] is not None and p["longitude"] is not None
        ]
        event_only = len(points) - len(geo_points)

        new_points = [
            p
            for p in geo_points
            if p["timestamp"]
            and (state["last_ts"] is None or p["timestamp"] > state["last_ts"])
        ]
        if new_points:
            state["track"].extend(new_points)
            state["last_ts"] = new_points[-1]["timestamp"]
            _write_json(TRACK_FILE, state["track"])
            pos = new_points[-1]
            print(
                f"[track] +{len(new_points)} point(s) | lat={pos['latitude']:.5f} "
                f"lon={pos['longitude']:.5f} | total={len(state['track'])}"
                + (f" ({event_only} event marker(s) skipped)" if event_only else "")
            )

    def _process_course_data(data: Any) -> None:
        if not isinstance(data, dict):
            return
        course_points = _normalize_course(data)
        if course_points and course_points != state["course"]:
            state["course"] = course_points
            _write_json(COURSE_FILE, course_points)
            print(f"[course] updated planned route: {len(course_points)} point(s)")

    def _fetch_json(page, api_url: str, params: Optional[dict] = None) -> Optional[Any]:
        """Fetch through the loaded Garmin page to retain its browser identity/cookies."""
        csrf_token = state["csrf_token"]
        if not csrf_token:
            print("[debug] Waiting for Garmin's CSRF token before polling its API.")
            return None
        result = page.evaluate(
            """async ({url, params, csrfToken}) => {
                try {
                    const requestUrl = new URL(url);
                    for (const [key, value] of Object.entries(params || {})) {
                        requestUrl.searchParams.set(key, value);
                    }
                    const response = await fetch(requestUrl, {
                        cache: 'no-store',
                        headers: {'livetrack-csrf-token': csrfToken},
                    });
                    const text = await response.text();
                    return {
                        ok: response.ok,
                        status: response.status,
                        data: text ? JSON.parse(text) : null,
                    };
                } catch (error) {
                    return {ok: false, status: 0, error: String(error)};
                }
            }""",
            {"url": api_url, "params": params, "csrfToken": csrf_token},
        )
        if result.get("ok"):
            return result.get("data")
        print(
            f"[debug] API poll failed for {api_url.split('?')[0]} "
            f"-- status={result.get('status')} error={result.get('error', '')}"
        )
        return None

    def handle_response(response):
        req_url = response.url
        if "livetrack.garmin.com/api/sessions/" not in req_url:
            return

        try:
            data = response.json()
        except Exception:
            return

        if "/track-points/" in req_url:
            _process_track_data(data)

        elif isinstance(data, dict) and "sessionId" in data:
            _process_session_data(data)

        else:
            if "/courses" in req_url:
                _process_course_data(data)
                return
            preview = (
                json.dumps(data)[:300]
                if isinstance(data, (dict, list))
                else str(data)[:300]
            )
            print(
                f"[debug] unrecognized /api/sessions/ response from {req_url}\n         {preview}"
            )

    _httpd, actual_port = _serve_output_dir(SERVER_PORT)
    viewer_url = f"http://127.0.0.1:{actual_port}/viewer.html"
    print(f"Map viewer: {viewer_url}")
    webbrowser.open(viewer_url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("response", handle_response)

        def capture_csrf_token(request):
            if "livetrack.garmin.com/api/" not in request.url:
                return
            csrf_token = request.headers.get("livetrack-csrf-token")
            if csrf_token and csrf_token != state["csrf_token"]:
                state["csrf_token"] = csrf_token
                print("[debug] Captured Garmin CSRF token from page request.")

        page.on("request", capture_csrf_token)

        # Debug: log every garmin.com response so we can see the *actual*
        # request pattern if our known endpoints don't match anything.
        def debug_log_response(response):
            u = response.url
            if "garmin.com" not in u or "/api/sessions/" in u:
                return
            if "/_next/static/" in u or "trustarc.com" in u or "/cdn-cgi/" in u:
                return  # build assets / consent-manager noise, not useful signal
            print(f"[debug] {response.status} {u}")

        page.on("response", debug_log_response)

        # Debug: some "real-time" trackers push live updates over a
        # WebSocket instead of polling HTTP -- log any socket + frames so we
        # can see if that's what's happening here.
        def handle_websocket(ws):
            print(f"[ws] connected: {ws.url}")
            ws.on(
                "framereceived",
                lambda payload: print(f"[ws frame] {str(payload)[:300]}"),
            )

        page.on("websocket", handle_websocket)

        print(f"Loading {url} ...")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print(
            "Page loaded (DOM ready). Watch the browser window --\n"
            "if it shows a cookie/consent banner or a challenge page instead\n"
            "of the live map, that's the actual problem.\n"
        )
        print("Listening for live updates (Ctrl+C to stop)...\n")

        try:
            while True:
                try:
                    session_data = _fetch_json(page, session_api_url, {"token": token})
                    if isinstance(session_data, dict):
                        live = _process_session_data(session_data)
                    else:
                        live = None
                    track_data = _fetch_json(
                        page,
                        track_api_url,
                        {
                            "token": token,
                            # Garmin's frontend requires a begin value. Use the
                            # session start so the first poll gets its full path.
                            "begin": state["track_begin"]
                            or datetime.now(timezone.utc)
                            .replace(microsecond=0)
                            .isoformat()
                            .replace("+00:00", "Z"),
                        },
                    )
                    course_data = _fetch_json(page, course_api_url, {"token": token})
                    if course_data is not None:
                        _process_course_data(course_data)
                except Exception as e:
                    session_data = None
                    track_data = None
                    print(f"[debug] browser API poll threw: {e}")

                if isinstance(session_data, dict):
                    if track_data is not None:
                        _process_track_data(track_data)
                    if not live:
                        print(
                            "\nSession is no longer live (share window ended). Stopping."
                        )
                        break
                elif track_data is not None:
                    _process_track_data(track_data)

                page.wait_for_timeout(POLL_TIMEOUT_MS)
        except KeyboardInterrupt:
            print("\nStopped by user.")
        finally:
            browser.close()

    if state["track"]:
        _write_json(TRACK_FILE, state["track"])
    print(f"\nSaved {len(state['track'])} points to {TRACK_FILE}")


if __name__ == "__main__":
    main()
