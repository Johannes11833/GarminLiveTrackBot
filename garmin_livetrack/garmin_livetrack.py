"""
Garmin LiveTrack extractor.

Given a LiveTrack share URL (e.g. from an email), pulls:
  - session metadata (user, live/expired status, current position)
  - the full accumulated track (track points)

Usage:
    python garmin_livetrack.py "https://livetrack.garmin.com/session/<id>/token/<token>"

Or import and use programmatically:
    from garmin_livetrack import LiveTrackSession
    lt = LiveTrackSession(url)
    lt.get_session_info()
    lt.get_new_points()
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
from dataclasses import dataclass, field
from typing import Optional

try:
    from curl_cffi import requests
    _HAS_CURL_CFFI = True
except ImportError:
    import requests
    _HAS_CURL_CFFI = False

OUTPUT_DIR = Path(__file__).parent
TRACK_FILE = OUTPUT_DIR / "track.json"
SESSION_FILE = OUTPUT_DIR / "session.json"
SERVER_PORT = 8765

# Current (2026) Garmin LiveTrack API, confirmed via browser DevTools.
SESSION_URL_TMPL = "https://livetrack.garmin.com/api/sessions/{session_id}"
TRACKLOG_URL_TMPL = "https://livetrack.garmin.com/api/sessions/{session_id}/track-points/common"

# Matches /session/<id>/token/<token> anywhere in a URL/string
URL_RE = re.compile(
    r"session/(?P<session_id>[0-9a-fA-F-]+)/token/(?P<token>[0-9A-Za-z]+)"
)


def parse_livetrack_url(url: str):
    """Extract (session_id, token) from any Garmin LiveTrack URL."""
    match = URL_RE.search(url)
    if not match:
        raise ValueError(f"Could not find session/token in URL: {url}")
    return match.group("session_id"), match.group("token")


def _normalize_point(raw: dict) -> dict:
    """
    Normalize a track point to a consistent shape. Handles the confirmed
    current API shape (coords nested under "position": {lat, lon}, ISO
    "dateTime" timestamp, and real-world speed/altitude/distance field
    names) while staying tolerant of older top-level lat/lon shapes too.
    """
    pos = raw.get("position") or {}
    lat = raw.get("latitude", raw.get("lat", pos.get("lat")))
    lon = raw.get("longitude", raw.get("lon", raw.get("lng", pos.get("lon"))))

    ts_raw = raw.get("timestamp", raw.get("dateTime", raw.get("time")))
    if isinstance(ts_raw, str):
        # ISO 8601 -> epoch millis
        ts = int(datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp() * 1000)
    else:
        ts = ts_raw  # already epoch millis (or None)

    meta_data = raw.get("metaData") or raw.get("metadata") or {}
    if not meta_data:
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
        "_raw": raw,  # keep original around in case you need other fields later
    }


@dataclass
class LiveTrackSession:
    url: str
    session_id: str = field(init=False)
    token: str = field(init=False)
    track: list = field(default_factory=list)
    _last_timestamp: Optional[int] = field(default=None, init=False)

    def __post_init__(self):
        self.session_id, self.token = parse_livetrack_url(self.url)
        self.session = (
            requests.Session(impersonate="chrome124") if _HAS_CURL_CFFI
            else requests.Session()
        )
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.url,
            "Origin": "https://livetrack.garmin.com",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        })
        self._warm_up()

    def _warm_up(self):
        """
        Load the human-facing session page first, like a real browser would,
        so any session/CSRF cookies Garmin sets get attached to later API
        calls. Failures here are non-fatal -- we still try the API calls.
        """
        try:
            self.session.get(self.url, timeout=10)
        except requests.RequestException:
            pass

    def get_session_info(self) -> dict:
        """Fetch session metadata: user, name, start/end, live position, etc."""
        resp = self.session.get(
            SESSION_URL_TMPL.format(session_id=self.session_id),
            params={"token": self.token},
            timeout=10,
        )
        if resp.status_code == 403:
            hint = (
                "curl_cffi is active but still blocked -- Garmin's protection\n"
                "     may be doing a JS challenge, not just TLS fingerprinting.\n"
                "     Next step: automate a real headless browser (playwright)\n"
                "     against the LiveTrack page and read the XHR responses\n"
                "     directly instead of replaying the URLs standalone."
                if _HAS_CURL_CFFI else
                "curl_cffi is NOT installed/active -- this 403 is likely plain\n"
                "     TLS-fingerprint bot detection. Run:\n"
                "       pip install curl_cffi\n"
                "     then re-run this script (no code changes needed -- it\n"
                "     auto-detects curl_cffi and uses it if present)."
            )
            raise RuntimeError(f"Garmin returned 403 Forbidden for the API call.\n     {hint}")
        resp.raise_for_status()
        return resp.json()

    def is_live(self, info: dict) -> bool:
        """
        The new API has no explicit 'InProgress'/'Expired' status field.
        Infer liveness from 'viewable' plus the session's end time.
        """
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

    def get_new_points(self) -> list:
        """
        Fetch track points newer than the last one we've seen.
        On first call, fetches the whole available track.
        """
        resp = self.session.get(
            TRACKLOG_URL_TMPL.format(session_id=self.session_id),
            params={"token": self.token},
            timeout=10,
        )
        if resp.status_code == 403:
            raise RuntimeError(
                "Garmin returned 403 Forbidden for the track-points call "
                "(same bot-detection issue as get_session_info -- see that "
                "error message for fixes)."
            )
        resp.raise_for_status()
        data = resp.json()

        # Handle either a bare list, or an object wrapping the list
        if isinstance(data, dict):
            raw_points = data.get("trackPoints") or data.get("points") or []
        else:
            raw_points = data

        points = [_normalize_point(p) for p in raw_points]

        if self._last_timestamp is not None:
            points = [p for p in points if p["timestamp"] and p["timestamp"] > self._last_timestamp]

        if points:
            self.track.extend(points)
            self._last_timestamp = points[-1]["timestamp"]

        return points

    def get_live_position(self, session_info: Optional[dict] = None) -> Optional[dict]:
        """
        Best available current position: prefer the 'position' field from the
        session endpoint (most up to date), fall back to the last track point.
        """
        if session_info and session_info.get("position"):
            pos = session_info["position"]
            return {
                "latitude": pos.get("lat"),
                "longitude": pos.get("lon"),
                "timestamp": self.track[-1]["timestamp"] if self.track else None,
                "metaData": self.track[-1]["metaData"] if self.track else {},
            }
        if self.track:
            return self.track[-1]
        return None

    def poll(self, interval_seconds: float = 4.0, stop_on_expired: bool = True):
        """
        Generator: yields (session_info, new_points) each poll cycle.
        Stops automatically once the session is no longer live (if stop_on_expired).
        """
        while True:
            info = self.get_session_info()
            live = self.is_live(info)

            new_points = self.get_new_points()
            yield info, new_points

            if stop_on_expired and not live:
                break

            time.sleep(interval_seconds)


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    tmp.replace(path)  # avoids the viewer reading a half-written file


def _serve_output_dir(port: int = SERVER_PORT):
    """Serve OUTPUT_DIR (where viewer.html + track.json live) in a background thread."""
    handler = lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
        *args, directory=str(OUTPUT_DIR), **kwargs
    )
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
        print("Usage: python garmin_livetrack.py <livetrack_url>")
        sys.exit(1)

    url = sys.argv[1]
    lt = LiveTrackSession(url)

    info = lt.get_session_info()
    live = lt.is_live(info)
    print(f"Session: {info.get('sessionName')} | user: {info.get('userDisplayName')} | "
          f"{'LIVE' if live else 'expired/ended'}")
    _write_json(SESSION_FILE, info)

    # Serve viewer.html + the json files so the browser can fetch them
    _httpd, actual_port = _serve_output_dir(SERVER_PORT)
    viewer_url = f"http://127.0.0.1:{actual_port}/viewer.html"
    print(f"\nOpening map viewer at {viewer_url}")
    webbrowser.open(viewer_url)

    print("\nPolling for live position (Ctrl+C to stop)...\n")
    try:
        for info, new_points in lt.poll(interval_seconds=4.0):
            info["sessionStatus"] = "InProgress" if lt.is_live(info) else "Expired"  # for viewer.html
            _write_json(SESSION_FILE, info)

            if new_points:
                _write_json(TRACK_FILE, lt.track)

            pos = lt.get_live_position(info)
            if pos and pos["latitude"] is not None:
                print(f"lat={pos['latitude']:.5f} lon={pos['longitude']:.5f} "
                      f"({len(new_points)} new point(s), {len(lt.track)} total)")
    except KeyboardInterrupt:
        print("\nStopped by user.")

    _write_json(TRACK_FILE, lt.track)
    print(f"\nSaved {len(lt.track)} points to {TRACK_FILE}")


if __name__ == "__main__":
    main()