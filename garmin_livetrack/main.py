import os
from dotenv import load_dotenv

from garmin_livetrack.logger import configure_logs
from garmin_livetrack.mail_listener import GarminLinkListener
from garmin_livetrack.signal_bot import SignalBot


bot: SignalBot = None


def send_link(link: str):
    bot.send_message(f"🚲 Schau dir meine Fahrradfahrt an: {link}")


def cli():
    # Load environment variables from .env file
    load_dotenv()

    # email secrets
    HOST = os.getenv("LIVETRACK_EMAIL_HOST")
    USERNAME = os.getenv("LIVETRACK_EMAIL_USERNAME")
    PASSWORD = os.getenv("LIVETRACK_EMAIL_PASSWORD")

    # signal secrets
    SIGNAL_API = os.getenv("LIVETRACK_SIGNAL_API")
    SENDER = os.getenv("LIVETRACK_SENDER_PHONE_NUMBER")
    RECIPIENTS = os.getenv("LIVETRACK_RECIPIENT_PHONE_NUMBERS").split(",")
    DEVICE_NAME = os.getenv(
        "LIVETRACK_SIGNAL_DEVICE_NAME", default="GarminLivetrackBot"
    )

    configure_logs()

    # Setup the bot
    global bot
    bot = SignalBot(
        api=SIGNAL_API, sender=SENDER, recipients=RECIPIENTS, device_name=DEVICE_NAME
    )
    if not bot.start():
        return

    # Setup the garmin livetrack email listener
    listener = GarminLinkListener(
        host=HOST, username=USERNAME, password=PASSWORD, callback=send_link
    )
    listener.start()


if __name__ == "__main__":
    cli()
