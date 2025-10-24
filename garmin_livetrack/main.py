import os
from dotenv import load_dotenv

from garmin_livetrack.mail_listener import GarminLinkListener
from garmin_livetrack.signal_bot import SignalBot


bot: SignalBot = None


def send_message(link: str):
    bot.send_message(f"Schau dir meine Fahrradfahrt live an🚲: {link}")


def cli():
    # Load environment variables
    load_dotenv()
    HOST = os.getenv("LIVETRACK_EMAIL_HOST")
    USERNAME = os.getenv("LIVETRACK_EMAIL_USERNAME")
    PASSWORD = os.getenv("LIVETRACK_EMAIL_PASSWORD")

    SIGNAL_API = os.getenv("LIVETRACK_SIGNAL_API")
    SENDER = os.getenv("LIVETRACK_SENDER_PHONE_NUMBER")
    RECIPIENTS = os.getenv("LIVETRACK_RECIPIENT_PHONE_NUMBERS").split(",")

    # Setup the bot
    global bot
    bot = SignalBot(api=SIGNAL_API, sender=SENDER, recipients=RECIPIENTS)
    if not bot.start():
        return

    # Setup the garmin livetrack email listener
    listener = GarminLinkListener(
        host=HOST, username=USERNAME, password=PASSWORD, callback=send_message
    )
    listener.start()


if __name__ == "__main__":
    cli()
