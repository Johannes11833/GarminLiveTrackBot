from pathlib import Path
from time import sleep
import time
import qrcode
import requests


from garmin_livetrack.logger import get_logger

logger = get_logger(__name__)


class SignalBot:
    def __init__(self, api: str, sender: str, recipients: list[str], device_name: str):
        self.api = api
        self.sender = sender
        self.recipients = recipients
        self.device_name = device_name

    def ping(self) -> bool:
        timeout_s = 20
        logger.info(f"Pinging the signal-api (timeout={timeout_s}s)")
        for _ in range(timeout_s):
            try:
                response = requests.get(f"{self.api}/v1/about", timeout=2)
                if response.status_code == 200:
                    logger.info("Successfully connected to the signal-api")
                    return True
            except requests.exceptions.ConnectionError:
                pass

            sleep(1)

        logger.error(
            f"Failed to connect to signal-api on: {self.api}. Is the signal-api server running?"
        )
        return False

    def start(self) -> bool:
        setup_done = False
        link_printed = False

        if not self.ping():
            return False

        while not setup_done:
            try:
                response = requests.get(f"{self.api}/v1/accounts", timeout=300)

                if response.status_code != 200:
                    logger.error(
                        "Failed to connect to signal-api. Is the signal-api server running?"
                    )
                    return False
            except requests.exceptions.ConnectionError:
                logger.error("Could not connect to signal-api (connection error).")
                return False

            except requests.exceptions.Timeout:
                logger.error("Request to signal-api timed out.")
                return False

            except requests.exceptions.RequestException as e:
                logger.error(f"Unexpected issue connecting to signal-api: {e}")
                return False

            devices: list[str] = response.json()

            setup_done = self.sender in devices

            if not setup_done and not link_printed:
                logger.info(f'The configured sender "{self.sender}" is not yet setup.')

                if len(devices) > 0:
                    logger.info(f"Connected numbers are: {",".join(devices)}")

                response = requests.get(
                    f"{self.api}/v1/qrcodelink/raw?device_name={self.device_name}",
                    timeout=20,
                )

                if response.status_code != 200:
                    logger.error(
                        "Failed to generate the QR code. Is the signal-api server running?"
                    )
                    return False

                device_link_uri = response.json()["device_link_uri"]

                # Print the QR code to the terminal so it can be scanned directly
                logger.info("Scan the QR code to continue:")
                qr = qrcode.QRCode(
                    border=1,
                    box_size=1,
                )
                qr.add_data(device_link_uri)
                qr.make(fit=True)
                qr.print_tty()

                # Keep a file-based fallback in case the terminal output can't be
                # scanned (e.g. remote/headless setup)
                img = qr.make_image(fill_color="black", back_color="white")
                qr_file = Path("garmin-livetrack-data/qrcode.png")
                qr_file.parent.mkdir(parents=True, exist_ok=True)
                img.save(qr_file.as_posix())
                logger.info(f"QR code link saved to: {qr_file.absolute()}")

                link_printed = True

        if not setup_done:
            # failed to initialize
            return False

        logger.info(f"Logged in to signal as: {self.sender}")
        logger.info(f'Recipient(s): {", ".join(self.recipients)}')

        # Send the startup notice only to the sender
        self.send_message(
            f"{self.device_name} started 🤖\n\nThe following {len(self.recipients)} recipient(s) are configured: {', '.join(self.recipients)}",
            recipients=[self.sender],
        )

        return True

    def send_message(self, message: str, recipients: list[str] | None = None):
        json = {
            "recipients": recipients if recipients else self.recipients,
            "number": self.sender,
            "message": message,
        }
        logger.info(f"Sending message: {json}")
        response = None

        could_send = False
        for _ in range(10):
            try:
                response = requests.post(f"{self.api}/v2/send", json=json, timeout=180)

                if response.status_code == 201:
                    could_send = True
                    break
                else:
                    logger.warning(
                        f"Failed to send message. Status Code: {response.status_code}, message: {response.text}"
                    )
            except:
                logger.warning(f"Timeout occurred while trying to send the message.")

            # retry to send after a delay
            time.sleep(5)

        if could_send:
            logger.info("SignalBot: successfully sent message")
        else:
            logger.error("Failed to send message!")
