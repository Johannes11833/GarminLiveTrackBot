import datetime
import email
import os
import re
import time
from typing import Callable
from imapclient import IMAPClient
import imapclient as imapclient
import requests
from dotenv import load_dotenv

from garmin_livetrack.logger import configure_logs, get_logger

logger = get_logger(__name__)

# Load .env before reading configuration so values from the file take effect.
load_dotenv()

# Base URL of the LiveTrack REST API. Override when the listener runs in a
# different container than the API, e.g. LIVETRACK_API_URL=http://garmin-livetrack-api:8000
API_URL = os.getenv("LIVETRACK_API_URL", "http://127.0.0.1:8000")
# Shared secret proving this is the trusted email listener; must match the
# API's LIVETRACK_API_TOKEN, otherwise the API rejects /trackings requests.
API_TOKEN = os.getenv("LIVETRACK_API_TOKEN", "")


def start_session(url: str):
    """Feed the LiveTrack URL from the email listener to the API, which then
    starts tracking the session and notifies registered devices."""
    for attempt in range(3):
        try:
            response = requests.post(
                f"{API_URL}/trackings",
                json={"url": url},
                headers={"Authorization": f"Bearer {API_TOKEN}"},
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


class GarminLinkListener:

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        # 10 minute default timeout as mentioned here:
        # https://imapclient.readthedocs.io/en/3.0.0/advanced.html#watching-a-mailbox-using-idle
        idle_timeout_s: int = 10 * 60,
        resync_interval_s: int = 20 * 60,
        error_retry_s: int = 5 * 60,
        callback: Callable[[str], None] | None = None,
    ):
        """
        Optionally register a callback function that takes a single string argument.
        """
        self.host = host
        self.username = username
        self.password = password
        self.idle_timeout_s = idle_timeout_s
        self.resync_interval_s = resync_interval_s
        self.error_retry_s = error_retry_s
        self.callback = callback

        self.client: IMAPClient | None = None

    def __extract_garmin_link(self, msg) -> str | None:
        """
        Extracts a Garmin LiveTrack link from an HTML email.
        Returns the link as a string or None if not found.
        """
        html_content = ""

        # Get HTML part of the email
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    charset = part.get_content_charset() or "utf-8"
                    html_content += part.get_payload(decode=True).decode(
                        charset, errors="ignore"
                    )
                    break
        else:
            if msg.get_content_type() == "text/html":
                charset = msg.get_content_charset() or "utf-8"
                html_content = msg.get_payload(decode=True).decode(
                    charset, errors="ignore"
                )

        if not html_content:
            return None

        # Regex to match Garmin LiveTrack links
        pattern = r"https://livetrack\.garmin\.com/[\w\-/\?\=&%]+"
        match = re.search(pattern, html_content)

        if match:
            return match.group(0)

        return None

    def __process_unseen_garmin_messages(self) -> bool:
        logger.info("Searching for new Gamin messages.")
        today = datetime.date.today().strftime("%d-%b-%Y")
        response = dict()

        # search for garmin livetrack emails from today
        try:
            messages = self.client.search(
                [
                    "UNSEEN",
                    "SINCE",
                    today,
                    "FROM",
                    "noreply@garmin.com",
                ]
            )
            if messages:
                response = self.client.fetch(messages, "RFC822")
            else:
                # no messages to fetch
                logger.info("No new Garmin messages found.")
                return False

        except Exception as e:
            # wait, then re-login
            logger.warning(f"Failed to search for garmin messages: {e}")
            self.connect()
            return False

        # iterate over the found emails, latest first
        for uid, message_data in reversed(response.items()):
            msg = email.message_from_bytes(message_data[b"RFC822"])
            logger.info(
                f"Found email from {msg.get("From")} with subject {msg.get("Subject")}"
            )
            link = self.__extract_garmin_link(msg)

            try:
                # Add the \Seen flag so we won't accidentally process this message again
                self.client.add_flags(uid, [imapclient.SEEN])
            except:
                logger.warning("Failed to mark Garmin email as seen!")

            if link:
                if self.callback:
                    self.callback(link)
                return True  # latest link found!

        logger.info("No new Garmin email found")
        return False

    def connect(self):
        if self.client:
            try:
                self.client.logout()
                logger.info("Logged out.")
            except:
                logger.warning("Failed to logout!")

        time.sleep(2.5)

        self.client = IMAPClient(host=self.host)
        self.client.login(self.username, self.password)
        logger.info(f"Successfully logged in to: {self.username}")
        self.client.select_folder("INBOX", readonly=False)

    def start(self):
        last_sync = 0
        self.connect()

        # Start IDLE mode
        logger.info(
            f"Listening for new garmin emails using idle timeout: {self.idle_timeout_s}s"
        )

        while True:
            should_check = False

            try:
                # Wait for an IDLE response
                self.client.idle()
                responses = self.client.idle_check(timeout=self.idle_timeout_s)
                logger.info(f'Server sent: {responses if responses else "nothing"}')
                self.client.idle_done()

                if self.__check_responses(responses=responses):
                    should_check = True
            except Exception as e:
                # an error occurred
                logger.warning(f"An error occurred during idling: {e}")
                logger.info(
                    f"Waiting {self.error_retry_s}s before manually checking for new messages."
                )
                # wait a bit, then re-login & check emails
                time.sleep(self.error_retry_s)
                self.connect()
                should_check = True

            # Periodic NOOP (to check connection and sync)
            if (not should_check) and (
                time.time() - last_sync >= self.resync_interval_s
            ):
                logger.info("Performing periodic NOOP to stay logged in.")

                try:
                    _, responses = self.client.noop()
                    last_sync = time.time()

                    if self.__check_responses(responses=responses):
                        should_check = True
                except Exception:
                    self.connect()
                    should_check = True

            if should_check:
                # wait a little so the email can be fetched
                time.sleep(10)

                # check if a new livetrack email has been received
                for _ in range(0, 10):
                    if self.__process_unseen_garmin_messages():
                        break
                    else:
                        # retry in case the email cannot be fetched right away
                        delay_s = 15
                        logger.info(f"Waiting {delay_s}s before searching again.")
                        time.sleep(delay_s)

    def __check_responses(self, responses) -> bool:
        if responses:
            for _, status in responses:
                if status == b"EXISTS":
                    return True
        return False


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
