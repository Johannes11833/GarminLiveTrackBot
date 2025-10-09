from time import sleep
import requests


class SignalBot:
    def __init__(self, api: str, sender: str, recipients: list[str]):
        self.api = api
        self.sender = sender
        self.recipients = recipients

        setup_done = False
        link_printed = False

        while not setup_done:
            response = requests.get(f"{self.api}/v1/accounts")

            if response.status_code != 200:
                print(
                    "ERROR: failed to connect to signal-api. Is the signal-api server running?"
                )
                return

            devices: list[str] = response.json()

            setup_done = self.sender in devices

            if not setup_done and not link_printed:
                print(f'The configured sender "{self.sender}" is not yet setup.')

                if len(devices) > 0:
                    print(f"Connected number are: {",".join(devices)}")

                print(
                    f"Open this link to link this device to your signal account: {f"{api}/v1/qrcodelink?device_name=GarminLivetrackBot"}"
                )

                link_printed = True

            sleep(1)

        print(f"SignalBot: sender: {self.sender}")
        print(f'SignalBot: recipient(s): {", ".join(self.recipients)}')

    def send_message(self, message: str):
        response = requests.post(
            f"{self.api}/v2/send",
            json={
                "recipients": self.recipients,
                "number": self.sender,
                "message": message,
            },
        )

        if response.status_code == 201:
            print(f'SignalBot: successfully sent message: "{message}"')
        else:
            print(
                f'SignalBot: failed to send message! Response: <Code: "{response.status_code}, text: {response.text}">'
            )
