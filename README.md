### Docker Setup
#### Startup compose

Run this command to startup the compose project and display only the logs for the garmin-livetrack bot:

```bash
docker compose up garmin-livetrack
```

On the first run, open the qr-code generated in the data folder of the bot and scan it with the signal app.

#### Startup signal-api manually
```bash
docker run --rm --name signal-api -p 8080:8080 -v "./signal-api-data:/home/.local/share/signal-cli" -e 'MODE=normal' bbernhard/signal-cli-rest-api
```


### Example .env file
```
LIVETRACK_EMAIL_HOST = "imap.gmx.net"
LIVETRACK_EMAIL_USERNAME = "email123@gmx.de"
LIVETRACK_EMAIL_PASSWORD = "ur-password"

LIVETRACK_SIGNAL_API = "http://signal-api:8080"
LIVETRACK_SENDER_PHONE_NUMBER = "+49123456789"
LIVETRACK_RECIPIENT_PHONE_NUMBERS = "+49123456789,+49987654321"
```