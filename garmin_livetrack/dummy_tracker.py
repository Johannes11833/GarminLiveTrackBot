"""Simulated LiveTrack session for exercising the viewer UI without a real
Garmin LiveTrack link.
"""

import math
import random
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from garmin_livetrack.tracker import Tracker


class DummyTracker(Tracker):
    """Simulates a LiveTrack session with synthetic movement and vitals, so
    the viewer UI can be exercised without a real Garmin session."""

    DURATION_SECONDS = 300
    POLL_SECONDS = 2
    START_LAT, START_LON = 48.7758, 9.1829  # Stuttgart

    def __init__(self):
        session_id = str(uuid.uuid4())
        token = secrets.token_hex(16)
        super().__init__(
            f"https://livetrack.garmin.com/session/{session_id}/token/{token}"
        )
        self._start_time = datetime.now(timezone.utc)

    def _session_payload(self, *, live: bool) -> Dict[str, Any]:
        # No "end" field: is_live() already treats a missing one as "still
        # live", so `viewable` alone is the live/ended switch here.
        return {
            "sessionId": self.session_id,
            "userDisplayName": "Dummy Rider",
            "sessionName": "Dummy LiveTrack Session",
            "start": self._start_time.isoformat().replace("+00:00", "Z"),
            "viewable": live,
            "unitId": 0,
            "publisher": {"connectUserProfileId": 0, "garminGuid": "dummy"},
        }

    def _send_message(self, page, sender: str, content: str) -> None:
        # No real Garmin session to post to; just accept it.
        print(f"[{self.session_id}] (dummy) message from {sender}: {content}")

    def _run(self) -> None:
        with self.lock:
            self._mark_state("waiting_for_garmin")
        self.stop_requested.wait(1)
        if self.stop_requested.is_set():
            with self.lock:
                self._mark_state("stopped")
            return

        self._save_session(self._session_payload(live=True))
        with self.lock:
            self._mark_state("running")

        lat, lon = self.START_LAT, self.START_LON
        heading = random.uniform(0, 360)
        distance_m = 0.0
        elapsed = 0
        while not self.stop_requested.is_set() and elapsed < self.DURATION_SECONDS:
            self._drain_outbox(None)
            heading += random.uniform(-20, 20)
            step_m = random.uniform(3, 6) * self.POLL_SECONDS
            lat += (step_m * math.cos(math.radians(heading))) / 111_111
            lon += (step_m * math.sin(math.radians(heading))) / (
                111_111 * math.cos(math.radians(lat))
            )
            distance_m += step_m
            elapsed += self.POLL_SECONDS
            self._save_track(
                [
                    {
                        "dateTime": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "position": {"lat": lat, "lon": lon},
                        "speedMetersPerSec": step_m / self.POLL_SECONDS,
                        "altitude": 300 + 40 * math.sin(elapsed / 90),
                        "totalDistanceMeters": distance_m,
                        "totalDurationSecs": elapsed,
                        "heartRateBeatsPerMin": 120
                        + 20 * math.sin(elapsed / 45)
                        + random.uniform(-4, 4),
                    }
                ]
            )
            self._wake.wait(self.POLL_SECONDS)
            self._wake.clear()

        self._drain_outbox(None)
        with self.lock:
            if self.stop_requested.is_set():
                self._mark_state("stopped")
                return
        self._save_session(self._session_payload(live=False))
        with self.lock:
            self._mark_state("ended")
