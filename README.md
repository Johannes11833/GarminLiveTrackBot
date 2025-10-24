### Docker Setup
#### Startup signal-api manually
```bash
docker run --rm --name signal-api -p 8080:8080 -v "./signal-api-data:/home/.local/share/signal-cli" -e 'MODE=normal' bbernhard/signal-cli-rest-api
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