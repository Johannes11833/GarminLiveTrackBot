"""REST API for concurrent Garmin LiveTrack sessions.

Run with:
    uvicorn garmin_livetrack.livetrack_api:app --host 127.0.0.1 --port 8000
"""

import hmac
import os
import threading
from typing import Any, Dict

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from garmin_livetrack import push
from garmin_livetrack.tracker import Tracker, TrackerManager

# Shared secret the email listener must present to start/stop tracking
# sessions, so a random internet caller can't create or kill trackings.
API_TOKEN = os.getenv("LIVETRACK_API_TOKEN", "")
# Lets clients spin up a simulated tracking session for exercising the
# viewer UI without a real Garmin session. Off by default.
DUMMY_MODE_ENABLED = os.getenv("LIVETRACK_ENABLE_DUMMY_MODE", "").lower() in {
    "1",
    "true",
    "yes",
}


def require_api_token(authorization: str = Header(default="")) -> None:
    if not API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LIVETRACK_API_TOKEN is not configured on the server.",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(token, API_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing API token.",
        )


class StartTrackingRequest(BaseModel):
    url: str


class SendMessageRequest(BaseModel):
    sender: str
    content: str


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
manager.start_cleanup()


def _start_dummy_session() -> None:
    tracker = manager.start_dummy()
    print(
        f"[dummy] Started simulated session: id={tracker.session_id} "
        f"token={tracker.token}\n"
        f"[dummy] Open the viewer with: "
        f"?id={tracker.session_id}&sessionToken={tracker.token}"
    )


@app.on_event("startup")
def startup() -> None:
    if DUMMY_MODE_ENABLED:
        # Delayed so it doesn't hold up the API becoming available.
        threading.Timer(5.0, _start_dummy_session).start()


@app.on_event("shutdown")
def shutdown() -> None:
    manager.stop_cleanup()
    manager.stop_all()
    push.stop()


@app.post(
    "/trackings",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_token)],
)
def start_tracking(request: StartTrackingRequest):
    try:
        tracker = manager.start(request.url)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return tracker.snapshot()


@app.get("/trackings", dependencies=[Depends(require_api_token)])
def list_trackings():
    with manager.lock:
        trackers = list(manager.trackers.values())
    return [tracker.snapshot() for tracker in trackers]


def get_tracker_or_404(session_id: str) -> Tracker:
    try:
        return manager.get(session_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tracking not found.")


def get_tracker_with_token(session_id: str, token: str) -> Tracker:
    """Require the Garmin LiveTrack share token, not just the session id, so
    someone who only guesses/observes the id can't read track/course/photo."""
    tracker = get_tracker_or_404(session_id)
    if not token or not hmac.compare_digest(token, tracker.token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing session token.",
        )
    return tracker


@app.get("/trackings/{session_id}/token/{token}")
def get_tracking(session_id: str, token: str):
    return get_tracker_with_token(session_id, token).snapshot()


@app.get("/trackings/{session_id}/token/{token}/track")
def get_track(session_id: str, token: str):
    return get_tracker_with_token(session_id, token).get_track()


@app.get("/trackings/{session_id}/token/{token}/course")
def get_course(session_id: str, token: str):
    return get_tracker_with_token(session_id, token).get_course()


@app.get("/trackings/{session_id}/token/{token}/profile-image")
def get_profile_image(session_id: str, token: str):
    tracker = get_tracker_with_token(session_id, token)
    with tracker.lock:
        image = tracker.profile_image
        content_type = tracker.profile_image_content_type
    if not image:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No profile image available.",
        )
    return Response(content=image, media_type=content_type)


@app.post(
    "/trackings/{session_id}/token/{token}/message",
    status_code=status.HTTP_204_NO_CONTENT,
)
def send_message(session_id: str, token: str, request: SendMessageRequest):
    sender = request.sender.strip()
    content = request.content.strip()
    if not sender or not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sender and content must not be empty.",
        )
    tracker = get_tracker_with_token(session_id, token)
    try:
        tracker.send_message(sender, content)
    except RuntimeError as error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.delete(
    "/trackings/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_api_token)],
)
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


def cli() -> None:
    import uvicorn

    # Bind to 0.0.0.0 inside a container so other services can reach the API;
    # default to loopback-only for local development.
    host = os.getenv("LIVETRACK_API_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=8000)
