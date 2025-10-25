from pathlib import Path
from time import sleep
import requests


class SignalBot:
    def __init__(self, api: str, sender: str, recipients: list[str], device_name: str):
        self.api = api
        self.sender = sender
        self.recipients = recipients
        self.device_name = device_name

    def ping(self) -> bool:
        print("Pinging the signal-api")
        for _ in range(20):
            try:
                response = requests.get(f"{self.api}/v1/about", timeout=2)
                if response.status_code == 200:
                    return True
            except requests.exceptions.ConnectionError:
                pass

            sleep(1)

        print(
            f"ERROR: failed to connect to signal-api on: {self.api}. Is the signal-api server running?"
        )
        return False

    def start(self) -> bool:
        setup_done = False
        link_printed = False

        if not self.ping():
            return False

        while not setup_done:
            try:
                response = requests.get(f"{self.api}/v1/accounts")

                if response.status_code != 200:
                    print(
                        "ERROR: failed to connect to signal-api. Is the signal-api server running?"
                    )
                    return False
            except requests.exceptions.ConnectionError:
                print("ERROR: Could not connect to signal-api (connection error).")
                return False

            except requests.exceptions.Timeout:
                print("ERROR: Request to signal-api timed out.")
                return False

            except requests.exceptions.RequestException as e:
                print(f"ERROR: Unexpected issue connecting to signal-api: {e}")
                return False

            devices: list[str] = response.json()

            setup_done = self.sender in devices

            if not setup_done and not link_printed:
                print(f'The configured sender "{self.sender}" is not yet setup.')

                if len(devices) > 0:
                    print(f"Connected number are: {",".join(devices)}")

                response = requests.get(
                    f"{self.api}/v1/qrcodelink?device_name={self.device_name}"
                )

                if response.status_code != 200:
                    print(
                        "ERROR: failed generate tge QR code. Is the signal-api server running?"
                    )
                    return False

                qr_code_file = Path("garmin-livetrack-data/signal_qr_code.png")
                qr_code_file.parent.mkdir(parents=True, exist_ok=True)

                with open(qr_code_file, "wb") as f:
                    f.write(response.content)

                print(f"Scan the QR code to continue: {qr_code_file.absolute()}")

                link_printed = True

        if not setup_done:
            # failed to initialize
            return False

        print(f"SignalBot: sender: {self.sender}")
        print(f'SignalBot: recipient(s): {", ".join(self.recipients)}')

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
        print(f"SignalBot: sending message: {json}")

        response = requests.post(f"{self.api}/v2/send", json=json)

        if response.status_code == 201:
            print(f"SignalBot: successfully sent message")
        else:
            print(
                f'SignalBot: failed to send message! Response: <Code: "{response.status_code}, text: {response.text}">'
            )
