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

import sys
import time
import json
import threading
import webbrowser
import http.server
import socketserver
from pathlib import Path
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

OUTPUT_DIR = Path(__file__).parent
TRACK_FILE = OUTPUT_DIR / "track.json"
SESSION_FILE = OUTPUT_DIR / "session.json"
COURSE_FILE = (
    OUTPUT_DIR / "course.json"
)  # planned route ("loaded track"), if the session has one
SERVER_PORT = 8765

POLL_TIMEOUT_MS = 5000  # how often we nudge the event loop / check for stop
RELOAD_AFTER_IDLE_S = 30  # if the page's own polling seems to have stalled, reload


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
        ts = int(
            datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp() * 1000
        )
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


def _normalize_course(raw) -> dict:
    """
    Extract the planned route ("loaded track") from the /courses API
    response, so the viewer can draw it alongside the live trail.

    Garmin returns this as either a bare list of points or an object
    wrapping the list under any of several keys, and each point's lat/lon
    can be top-level, nested under "position", or a GeoJSON-style
    [lon, lat] "coordinates" pair. Tolerant on purpose -- the shape has
    varied across LiveTrack releases.
    """
    if isinstance(raw, dict):
        name = raw.get("name") or raw.get("courseName") or raw.get("title")
        raw_points = (
            raw.get("points")
            or raw.get("trackPoints")
            or raw.get("coursePoints")
            or raw.get("data")
            or raw.get("coordinates")
        )
    else:
        name = None
        raw_points = raw

    if isinstance(raw_points, dict):
        raw_points = raw_points.get("points") or raw_points.get("trackPoints") or []

    points = []
    for p in raw_points or []:
        if not isinstance(p, dict):
            continue
        pos = p.get("position") or {}
        lat = p.get("latitude", p.get("lat", pos.get("lat")))
        lon = p.get("longitude", p.get("lon", pos.get("lon", pos.get("lng"))))
        if lat is None and "coordinates" in p:
            coords = p["coordinates"]
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                lon, lat = coords[0], coords[1]  # GeoJSON order: [lon, lat]
        if lat is None or lon is None:
            continue
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            continue
        points.append({"latitude": lat, "longitude": lon})

    return {"name": name, "points": points}


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
    with open(tmp, "w") as f:
        json.dump(data, f)
    tmp.replace(path)


def _serve_output_dir(port: int = SERVER_PORT):
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
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

    state = {
        "track": [],
        "last_ts": None,
        "last_activity": 0.0,
        "session_info": None,
        "last_session_pos": None,
        "course": None,
    }

    def handle_response(response):
        req_url = response.url
        if "livetrack.garmin.com/api/sessions/" not in req_url:
            return

        try:
            data = response.json()
        except Exception:
            return

        # Log the top-level shape so we can discover the course endpoint
        if isinstance(data, dict):
            print(f"[API] keys: {list(data.keys())}")
        elif isinstance(data, list):
            print(f"[API] list: {len(data)} items")

        if "/track-points/" in req_url:
            raw_points = (
                data
                if isinstance(data, list)
                else (data.get("trackPoints") or data.get("points") or [])
            )
            points = [_normalize_point(p) for p in raw_points]
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
            else:
                print(
                    f"[debug] /track-points/ responded with {len(raw_points)} raw point(s), "
                    f"0 new after filtering -- raw sample: {str(raw_points[:1])[:300]}"
                )

        elif "/courses" in req_url:
            # Planned route the athlete loaded before the session ("loaded
            # track"). Drawn as a dashed line in the viewer, if present.
            course = _normalize_course(data)
            if course["points"]:
                state["course"] = course
                _write_json(COURSE_FILE, course)
                print(
                    f"[course] {course['name'] or 'unnamed route'} | "
                    f"{len(course['points'])} point(s) saved to {COURSE_FILE.name}"
                )

        elif isinstance(data, dict) and "sessionId" in data:
            data = dict(data)
            data["sessionStatus"] = "InProgress" if _is_live(data) else "Expired"
            state["session_info"] = data
            _write_json(SESSION_FILE, data)
            pos = data.get("position")
            if pos:
                print(
                    f"[session] {data.get('sessionName')} | {data.get('userDisplayName')} | "
                    f"{'LIVE' if _is_live(data) else 'expired'} | "
                    f"position={pos.get('lat')},{pos.get('lon')}"
                )

                # Fallback trail: build a breadcrumb from the session's own
                # 'position' field every time it changes, in case the
                # track-points endpoint never fires (or fires differently
                # than expected) -- this still gets you a live marker + trail.
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

        else:
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
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.on("response", handle_response)
        page.on("request", handle_response)
        page.on(
            "request",
            lambda request: (
                (print("POST:", request.url), print("Body:", request.post_data))
                if request.method == "POST"
                else None
            ),
        )

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
                page.wait_for_timeout(POLL_TIMEOUT_MS)

                if state["session_info"] and not _is_live(state["session_info"]):
                    print("\nSession is no longer live. Stopping.")
                    break
        except KeyboardInterrupt:
            print("\nStopped by user.")
        finally:
            browser.close()

    if state["track"]:
        _write_json(TRACK_FILE, state["track"])
    print(f"\nSaved {len(state['track'])} points to {TRACK_FILE}")


if __name__ == "__main__":
    main()
