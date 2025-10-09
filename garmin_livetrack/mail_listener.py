import datetime
import email
import re
from typing import Callable
from imapclient import IMAPClient
import imapclient as imapclient


class GarminLinkListener:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        callback: Callable[[str], None] | None = None,
    ):
        """
        Optionally register a callback function that takes a single string argument.
        """
        self.host = host
        self.username = username
        self.password = password
        self.callback = callback

    def extract_garmin_link(self, msg) -> str | None:
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

    def process_unseen_messages(self, server: IMAPClient):
        today = datetime.date.today().strftime("%d-%b-%Y")

        # search for garmin livetrack emails from today
        messages = server.search(
            [
                "UNSEEN",
                "SINCE",
                today,
                #   "FROM", "noreply@garmin.com"
            ]
        )
        for uid, message_data in reversed(server.fetch(messages, "RFC822").items()):
            msg = email.message_from_bytes(message_data[b"RFC822"])
            print(uid, msg.get("From"), msg.get("Subject"))
            link = self.extract_garmin_link(msg)

            # Add the \Seen flag
            server.add_flags(uid, [imapclient.SEEN])

            if link:
                if self.callback:
                    self.callback(link)
                return  # latest link found!

    def start(self):
        with IMAPClient(host=self.host) as server:
            server.login(self.username, self.password)
            print(f"Successfully logged in to: {self.username}")
            server.select_folder("INBOX", readonly=False)

            # Start IDLE mode
            server.idle()
            print("Listening for new emails")

            while True:
                # Wait for an IDLE response
                responses = server.idle_check(timeout=60)
                print("Server sent:", responses if responses else "nothing")

                if responses:
                    for _, status in responses:
                        if status == b"EXISTS":
                            server.idle_done()
                            self.process_unseen_messages(server)
                            server.idle()
                            continue

        print("\nIDLE mode done")
