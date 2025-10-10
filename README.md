### Docker Setup
#### Startup signal-api manually
```bash
docker run --rm --name signal-api -p 8080:8080 -v signal-api:/home/.local/share/signal-cli -e 'MODE=normal' bbernhard/signal-cli-rest-api
```

#### Docker compose setup
```yml
version: "3"
services:
  garmin-livetrack:
    image: garmin-livetrack
    env_file:
      - .env
    environment:
      - LIVETRACK_SIGNAL_API: http://signal-api:8080

  signal-api:
    image: bbernhard/signal-cli-rest-api:latest
    environment:
      - MODE=normal #supported modes: json-rpc, native, normal
      - AUTO_RECEIVE_SCHEDULE=0 22 * * *
    ports:
      - "8080:8080" #map docker port 8080 to host port 8080.
    volumes:
      - "./signal-cli-config:/home/.local/share/signal-cli" #map "signal-cli-config" folder on host system into docker container. the folder contains the password and cryptographic keys when a new number is registered
```

### Example .env file
```
LIVETRACK_EMAIL_HOST = "imap.gmx.net"
LIVETRACK_EMAIL_USERNAME = "email123@gmx.de"
LIVETRACK_EMAIL_PASSWORD = "ur-password"

LIVETRACK_SIGNAL_API = "http://localhost:8080"
LIVETRACK_SENDER_PHONE_NUMBER = "+49123456789"
LIVETRACK_RECIPIENT_PHONE_NUMBERS = "+49123456789,+49987654321"
```