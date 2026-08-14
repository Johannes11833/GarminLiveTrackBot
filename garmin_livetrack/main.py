import os
import time

import requests
from dotenv import load_dotenv

from garmin_livetrack.logger import configure_logs
from garmin_livetrack.mail_listener import GarminLinkListener

# Load .env before reading configuration so values from the file take effect.
load_dotenv()

# Base URL of the LiveTrack REST API. Override when the listener runs in a
# different container than the API, e.g. LIVETRACK_API_URL=http://garmin-livetrack-api:8000
API_URL = os.getenv("LIVETRACK_API_URL", "http://127.0.0.1:8000")


def start_session(url: str):
    """Feed the LiveTrack URL from the email listener to the API, which then
    starts tracking the session and notifies registered devices."""
    for attempt in range(3):
        try:
            response = requests.post(
                f"{API_URL}/trackings",
                json={"url": url},
                timeout=15,
            )
            if response.status_code == 201:
                print(f"Session started via API: {url}")
                return
            print(
                f"Failed to start session (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )
        except Exception as error:
            print(f"Failed to reach API at {API_URL}: {error}")
        time.sleep(5 * (attempt + 1))


def cli():
    # email secrets
    HOST = os.getenv("LIVETRACK_EMAIL_HOST")
    USERNAME = os.getenv("LIVETRACK_EMAIL_USERNAME")
    PASSWORD = os.getenv("LIVETRACK_EMAIL_PASSWORD")

    configure_logs()

    # Setup the garmin livetrack email listener
    listener = GarminLinkListener(
        host=HOST, username=USERNAME, password=PASSWORD, callback=start_session
    )
    listener.start()


if __name__ == "__main__":
    cli()
