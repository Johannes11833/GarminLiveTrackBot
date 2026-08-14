"""REST API for concurrent Garmin LiveTrack sessions.

Run with:
    uvicorn garmin_livetrack.livetrack_api:app --host 127.0.0.1 --port 8000
"""

import copy
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

from garmin_livetrack import push

POLL_SECONDS = 5
TERMINAL_STATES = {"stopped", "ended", "error"}
URL_RE = re.compile(
    r"session/(?P<session_id>[0-9a-fA-F-]+)/token/(?P<token>[0-9A-Za-z]+)"
)


def parse_livetrack_url(url: str):
    match = URL_RE.search(url)
    if not match:
        raise ValueError("URL must contain /session/<id>/token/<token>.")
    return match.group("session_id"), match.group("token")


def normalize_point(raw: Dict[str, Any]) -> Dict[str, Any]:
    position = raw.get("position") or {}
    timestamp = raw.get("timestamp", raw.get("dateTime", raw.get("time")))
    if isinstance(timestamp, str):
        try:
            timestamp = int(
                datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
                * 1000
            )
        except ValueError:
            timestamp = None
    metadata = raw.get("metaData") or raw.get("metadata") or {
        "SPEED": raw.get("speedMetersPerSec", raw.get("speed")),
        "ELEVATION": raw.get("altitude"),
        "TOTAL_DISTANCE": raw.get("totalDistanceMeters"),
        "TOTAL_DURATION": raw.get("totalDurationSecs"),
        "ACTIVITY_TYPE": raw.get("activityType"),
    }
    return {
        "latitude": raw.get("latitude", raw.get("lat", position.get("lat"))),
        "longitude": raw.get(
            "longitude", raw.get("lon", raw.get("lng", position.get("lon")))
        ),
        "timestamp": timestamp,
        "metaData": metadata,
        "events": raw.get("events", []),
    }


def normalize_course(data: Dict[str, Any]) -> List[Dict[str, float]]:
    points = []
    for course in data.get("courses") or []:
        for point in course.get("coursePoints") or []:
            position = point.get("position") or {}
            latitude, longitude = position.get("lat"), position.get("lon")
            if latitude is not None and longitude is not None:
                points.append({"latitude": latitude, "longitude": longitude})
    return points


def is_live(session: Dict[str, Any]) -> bool:
    if not session.get("viewable", True):
        return False
    end = session.get("end")
    if not end:
        return True
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(
            end.replace("Z", "+00:00")
        )
    except ValueError:
        return True


class StartTrackingRequest(BaseModel):
    url: str


class Tracker:
    """One browser worker and isolated state for one LiveTrack share URL."""

    def __init__(self, url: str):
        self.url = url
        self.session_id, self.token = parse_livetrack_url(url)
        self.lock = threading.Lock()
        self.stop_requested = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.state = "starting"
        self.error: Optional[str] = None
        self.session: Optional[Dict[str, Any]] = None
        self.track: List[Dict[str, Any]] = []
        self.course: List[Dict[str, float]] = []
        self.last_timestamp: Optional[int] = None
        self.track_begin: Optional[str] = None
        self.csrf_token: Optional[str] = None
        self._start_notified = False
        self._end_notified = False

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_requested.set()

    def request_stop(self) -> bool:
        with self.lock:
            if self.state in TERMINAL_STATES | {"stopping"}:
                return False
            self.state = "stopping"
        self.stop()
        return True

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return copy.deepcopy(
                {
                    "id": self.session_id,
                    "url": self.url,
                    "state": self.state,
                    "error": self.error,
                    "session": self.session,
                    "pointCount": len(self.track),
                    "coursePointCount": len(self.course),
                }
            )

    def get_track(self) -> List[Dict[str, Any]]:
        with self.lock:
            return copy.deepcopy(self.track)

    def get_course(self) -> List[Dict[str, float]]:
        with self.lock:
            return copy.deepcopy(self.course)

    def _capture_csrf_token(self, request) -> None:
        if "livetrack.garmin.com/api/" not in request.url:
            return
        token = request.headers.get("livetrack-csrf-token")
        if token:
            with self.lock:
                changed = token != self.csrf_token
                self.csrf_token = token
            if changed:
                print(f"[{self.session_id}] Garmin CSRF token captured.")

    def _fetch_json(self, page, url: str, params: Dict[str, str]) -> Optional[Any]:
        with self.lock:
            csrf_token = self.csrf_token
        if not csrf_token:
            return None
        result = page.evaluate(
            """async ({url, params, csrfToken}) => {
                try {
                    const requestUrl = new URL(url);
                    for (const [key, value] of Object.entries(params)) {
                        requestUrl.searchParams.set(key, value);
                    }
                    const response = await fetch(requestUrl, {
                        cache: 'no-store',
                        headers: {'livetrack-csrf-token': csrfToken},
                    });
                    const text = await response.text();
                    return {ok: response.ok, status: response.status,
                            data: text ? JSON.parse(text) : null};
                } catch (error) {
                    return {ok: false, status: 0, error: String(error)};
                }
            }""",
            {"url": url, "params": params, "csrfToken": csrf_token},
        )
        if result.get("ok"):
            return result.get("data")
        raise RuntimeError(f"Garmin API returned HTTP {result.get('status')}.")

    def _save_session(self, data: Dict[str, Any]) -> bool:
        session = dict(data)
        live = is_live(session)
        session["sessionStatus"] = "InProgress" if live else "Expired"
        with self.lock:
            self.session = session
            self.track_begin = session.get("start") or self.track_begin
            started = self._start_notified
            self._start_notified = self._start_notified or live
            ended = self._end_notified
            self._end_notified = self._end_notified or not live
        print(
            f"[{self.session_id}] session: {session.get('sessionName')} | "
            f"{session.get('userDisplayName')} | {session['sessionStatus']}"
        )
        name = str(session.get("sessionName") or self.session_id)
        if live and not started:
            push.notify(self.session_id, "LiveTrack started", name)
        elif not live and not ended:
            push.notify(self.session_id, "LiveTrack ended", name)
        return live

    def _save_track(self, data: Any) -> None:
        raw_points = data if isinstance(data, list) else data.get("trackPoints", [])
        points = [normalize_point(point) for point in raw_points if isinstance(point, dict)]
        progress_point = None
        with self.lock:
            new_points = [
                point
                for point in points
                if point["latitude"] is not None
                and point["longitude"] is not None
                and point["timestamp"]
                and (
                    self.last_timestamp is None
                    or point["timestamp"] > self.last_timestamp
                )
            ]
            if new_points:
                self.track.extend(new_points)
                self.last_timestamp = new_points[-1]["timestamp"]
                point = new_points[-1]
                print(
                    f"[{self.session_id}] track: +{len(new_points)} point(s) | "
                    f"lat={point['latitude']:.5f} lon={point['longitude']:.5f} | "
                    f"total={len(self.track)}"
                )

    def _save_course(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        course = normalize_course(data)
        if course:
            with self.lock:
                changed = course != self.course
                self.course = course
            if changed:
                print(f"[{self.session_id}] course: {len(course)} point(s)")

    def _run(self) -> None:
        session_url = f"https://livetrack.garmin.com/api/sessions/{self.session_id}"
        track_url = (
            f"https://livetrack.garmin.com/api/sessions/{self.session_id}/track-points/common"
        )
        course_url = f"https://livetrack.garmin.com/api/sessions/{self.session_id}/courses"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.on("request", self._capture_csrf_token)
                    page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
                    with self.lock:
                        self.state = "waiting_for_garmin"
                    while not self.stop_requested.is_set():
                        try:
                            session = self._fetch_json(page, session_url, {"token": self.token})
                            if not isinstance(session, dict):
                                # Let Playwright process Garmin's initial request,
                                # which supplies the CSRF header we must reuse.
                                page.wait_for_timeout(1000)
                                continue
                            live = self._save_session(session)
                            with self.lock:
                                begin = self.track_begin
                            track = self._fetch_json(
                                page,
                                track_url,
                                {"token": self.token, "begin": begin or session.get("start", "")},
                            )
                            if track is not None:
                                self._save_track(track)
                            course = self._fetch_json(page, course_url, {"token": self.token})
                            if course is not None:
                                self._save_course(course)
                            if not live:
                                with self.lock:
                                    self.state = "ended"
                                return
                            with self.lock:
                                self.state = "running"
                        except RuntimeError as error:
                            with self.lock:
                                self.error = str(error)
                                self.state = "error"
                            return
                        self.stop_requested.wait(POLL_SECONDS)
                finally:
                    browser.close()
        except Exception as error:
            with self.lock:
                self.error = str(error)
                self.state = "error"
            return
        with self.lock:
            self.state = "stopped"


class TrackerManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.trackers: Dict[str, Tracker] = {}

    def start(self, url: str) -> Tracker:
        tracker = Tracker(url)
        with self.lock:
            existing = self.trackers.get(tracker.session_id)
            if existing and existing.snapshot()["state"] not in TERMINAL_STATES:
                raise ValueError("This LiveTrack session is already being tracked.")
            self.trackers[tracker.session_id] = tracker
        tracker.start()
        return tracker

    def get(self, session_id: str) -> Tracker:
        with self.lock:
            tracker = self.trackers.get(session_id)
        if not tracker:
            raise KeyError(session_id)
        return tracker

    def stop(self, session_id: str) -> Tracker:
        tracker = self.get(session_id)
        if not tracker.request_stop():
            raise ValueError("This LiveTrack session is already stopped.")
        return tracker

    def stop_all(self) -> None:
        with self.lock:
            trackers = list(self.trackers.values())
        for tracker in trackers:
            tracker.stop()


manager = TrackerManager()
app = FastAPI(title="Garmin LiveTrack API")
app.add_middleware(
    CORSMiddleware,
    # Local dev viewer; Flutter's web server uses a random port, so allow any.
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

push.start()


@app.on_event("shutdown")
def shutdown() -> None:
    manager.stop_all()
    push.stop()


@app.post("/trackings", status_code=status.HTTP_201_CREATED)
def start_tracking(request: StartTrackingRequest):
    try:
        tracker = manager.start(request.url)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return tracker.snapshot()


@app.get("/trackings")
def list_trackings():
    with manager.lock:
        trackers = list(manager.trackers.values())
    return [tracker.snapshot() for tracker in trackers]


def get_tracker_or_404(session_id: str) -> Tracker:
    try:
        return manager.get(session_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tracking not found.")


@app.get("/trackings/{session_id}")
def get_tracking(session_id: str):
    return get_tracker_or_404(session_id).snapshot()


@app.get("/trackings/{session_id}/track")
def get_track(session_id: str):
    return get_tracker_or_404(session_id).get_track()


@app.get("/trackings/{session_id}/course")
def get_course(session_id: str):
    return get_tracker_or_404(session_id).get_course()


@app.delete("/trackings/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def stop_tracking(session_id: str):
    try:
        manager.stop(session_id)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class SubscribeRequest(BaseModel):
    token: str
    subscription: Dict[str, Any]


class UnsubscribeRequest(BaseModel):
    token: str
    endpoint: str


@app.get("/push/public-key")
def get_public_key():
    return {"publicKey": push.public_key()}


@app.post("/push/subscribe", status_code=status.HTTP_201_CREATED)
def subscribe(request: SubscribeRequest):
    if not push.token_valid(request.token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid registration token.",
        )
    subscription = request.subscription
    try:
        push.subscribe(subscription)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        )
    return {"status": "subscribed"}


@app.delete("/push/subscribe", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe(request: UnsubscribeRequest):
    if not push.token_valid(request.token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid registration token.",
        )
    push.unsubscribe(request.endpoint)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/push/subscriptions")
def list_subscriptions(token: str):
    if not push.token_valid(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid registration token.",
        )
    return push.subscriptions()


def cli() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
