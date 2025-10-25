import datetime
import email
import re
from time import sleep
from typing import Callable
from imaplib import IMAP4
from imapclient import IMAPClient
import imapclient as imapclient


from garmin_livetrack.logger import get_logger

logger = get_logger(__name__)


class GarminLinkListener:

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        # 10 minute default timeout as mentioned here:
        # https://imapclient.readthedocs.io/en/3.0.0/advanced.html#watching-a-mailbox-using-idle
        idle_timeout_s: int = 60 * 10,
        callback: Callable[[str], None] | None = None,
    ):
        """
        Optionally register a callback function that takes a single string argument.
        """
        self.host = host
        self.username = username
        self.password = password
        self.idle_timeout_s = idle_timeout_s
        self.callback = callback

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

    def __process_unseen_messages(self, server: IMAPClient):
        today = datetime.date.today().strftime("%d-%b-%Y")

        # search for garmin livetrack emails from today
        try:
            messages = server.search(
                [
                    "UNSEEN",
                    "SINCE",
                    today,
                    "FROM",
                    "noreply@garmin.com",
                ]
            )
        except (IMAP4.abort, IMAP4.error):
            delay_s = 60
            logger.warning(
                f"Failed to search for garmin messages. Retry in {delay_s}s."
            )
            sleep(delay_s)
            return self.__process_unseen_messages(server=server)

        for uid, message_data in reversed(server.fetch(messages, "RFC822").items()):
            msg = email.message_from_bytes(message_data[b"RFC822"])
            logger.info(
                f"Found email from {msg.get("From")} with subject {msg.get("Subject")}"
            )
            link = self.__extract_garmin_link(msg)

            # Add the \Seen flag so we won't accidentally process this message again
            server.add_flags(uid, [imapclient.SEEN])

            if link:
                if self.callback:
                    self.callback(link)
                break  # latest link found!

    def start(self):
        with IMAPClient(host=self.host) as server:
            server.login(self.username, self.password)
            logger.info(f"Successfully logged in to: {self.username}")
            server.select_folder("INBOX", readonly=False)
            server.idle()

            # Start IDLE mode
            logger.info(
                f"Listening for new garmin emails using idle timeout: {self.idle_timeout_s}s"
            )

            while True:
                # check if a new livetrack email has been received
                server.idle_done()
                self.__process_unseen_messages(server)
                server.idle()

                # Wait for an IDLE response
                responses = server.idle_check(timeout=self.idle_timeout_s)
                logger.info(f'Server sent: {responses if responses else "nothing"}')

                if responses:
                    for _, status in responses:
                        if status == b"EXISTS":
                            continue

        logger.info("\nIDLE mode done")
