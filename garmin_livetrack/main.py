import os
from dotenv import load_dotenv

from garmin_livetrack.mail_listener import GarminLinkListener


def link_received(link: str):
    print(f"New link: {link}")


if __name__ == "__main__":
    # Load environment variables
    load_dotenv()
    HOST = os.getenv("LIVETRACK_EMAIL_HOST")
    USERNAME = os.getenv("LIVETRACK_EMAIL_USERNAME")
    PASSWORD = os.getenv("LIVETRACK_EMAIL_PASSWORD")

    listener = GarminLinkListener(
        host=HOST, username=USERNAME, password=PASSWORD, callback=link_received
    )
    listener.start()
