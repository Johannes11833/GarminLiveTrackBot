"""Web Push (VAPID) support for the LiveTrack REST API.

Generates and persists a VAPID key pair on first use, stores approved browser
push subscriptions, and delivers notifications through a single background
worker thread. Registration requires the shared token from
LIVETRACK_PUSH_TOKEN; the viewer passes it via `?token=<token>` in the app
URL, so only people who received the link can subscribe.
"""

import base64
import binascii
import hmac
import json
import logging
import os
import queue
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from py_vapid import b64urlencode
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)

load_dotenv()

DATA_DIR = Path(__file__).resolve().parent.parent / "garmin-livetrack-data"
VAPID_KEYS_FILE = DATA_DIR / "vapid_keys.json"
SUBSCRIPTIONS_FILE = DATA_DIR / "push_subscriptions.json"
VAPID_CONTACT_EMAIL = os.getenv("LIVETRACK_VAPID_CONTACT_EMAIL", "mailto:livetrack@example.com")
# Required: shared registration token. Only requests carrying this token
# (via ?token=... in the viewer URL) may register a push subscription.
REGISTRATION_TOKEN = os.getenv("LIVETRACK_PUSH_TOKEN", "")
# Override both keys to keep stable keys across hosts (e.g. multiple replicas).
VAPID_PUBLIC_KEY_OVERRIDE = os.getenv("LIVETRACK_VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY_OVERRIDE = os.getenv("LIVETRACK_VAPID_PRIVATE_KEY")

_lock = threading.Lock()
_subscriptions: List[Dict[str, Any]] = []
_queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
_public_key: str = ""
_private_key: str = ""


def token_valid(token: str) -> bool:
    """Constant-time comparison against the shared registration token."""
    if not REGISTRATION_TOKEN or not token:
        return False
    return hmac.compare_digest(token, REGISTRATION_TOKEN)


def _load_or_create_keys() -> None:
    global _public_key, _private_key
    if VAPID_PUBLIC_KEY_OVERRIDE and VAPID_PRIVATE_KEY_OVERRIDE:
        _public_key, _private_key = VAPID_PUBLIC_KEY_OVERRIDE, VAPID_PRIVATE_KEY_OVERRIDE
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if VAPID_KEYS_FILE.exists():
        keys = json.loads(VAPID_KEYS_FILE.read_text(encoding="utf-8"))
        try:
            # pywebpush accepts the private key as base64url-encoded DER or a
            # raw 32-byte scalar; PEM is rejected, so validate on load.
            from py_vapid import Vapid

            Vapid.from_string(keys["private_key"])
            _public_key, _private_key = keys["public_key"], keys["private_key"]
            return
        except Exception:
            logger.warning("Stored VAPID keys are unreadable, generating new ones.")
    from cryptography.hazmat.primitives.asymmetric.ec import (
        SECP256R1,
        generate_private_key,
    )
    from cryptography.hazmat.primitives import serialization

    key = generate_private_key(SECP256R1())
    public_key = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    # DER (PKCS8), base64url-encoded -- the format pywebpush expects.
    private_key = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _public_key, _private_key = b64urlencode(public_key), b64urlencode(private_key)
    VAPID_KEYS_FILE.write_text(
        json.dumps({"public_key": _public_key, "private_key": _private_key}),
        encoding="utf-8",
    )
    logger.info("Generated new VAPID key pair in %s", VAPID_KEYS_FILE)


def public_key() -> str:
    return _public_key


def _keys_valid(subscription: Dict[str, Any]) -> bool:
    """p256dh/auth must be base64url strings that decode to valid key material."""
    keys = subscription.get("keys")
    if not isinstance(keys, dict) or not isinstance(subscription.get("endpoint"), str):
        return False
    for name in ("p256dh", "auth"):
        value = keys.get(name)
        if not isinstance(value, str):
            return False
        try:
            base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))
        except Exception:
            return False
    return True


def _load_subscriptions() -> None:
    global _subscriptions
    if not SUBSCRIPTIONS_FILE.exists():
        return
    try:
        data = json.loads(SUBSCRIPTIONS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            _subscriptions = [sub for sub in data if _keys_valid(sub)]
    except Exception:
        logger.warning("Stored push subscriptions are unreadable, ignoring them.")


def _persist_subscriptions() -> None:
    """Write the subscription list atomically (temp file + rename)."""
    tmp = SUBSCRIPTIONS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_subscriptions), encoding="utf-8")
    tmp.replace(SUBSCRIPTIONS_FILE)


def subscribe(subscription: Dict[str, Any]) -> None:
    if not _keys_valid(subscription):
        raise ValueError("subscription must contain endpoint and valid keys.")
    with _lock:
        for existing in _subscriptions:
            if existing.get("endpoint") == subscription.get("endpoint"):
                existing.update(subscription)
                _persist_subscriptions()
                return
        _subscriptions.append(subscription)
        _persist_subscriptions()


def unsubscribe(endpoint: str) -> None:
    with _lock:
        _subscriptions[:] = [
            sub for sub in _subscriptions if sub.get("endpoint") != endpoint
        ]
        _persist_subscriptions()


def notify(session_id: str, title: str, body: str) -> None:
    """Queue a notification for every registered subscription."""
    with _lock:
        subscribers = list(_subscriptions)
    if not subscribers:
        return
    _queue.put({"session_id": session_id, "title": title, "body": body, "subscribers": subscribers})


def _send(session_id: str, subscription: Dict[str, Any], title: str, body: str) -> Optional[bool]:
    """Returns False when the subscription is dead and should be removed."""
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body, "sessionId": session_id}),
            vapid_private_key=_private_key,
            vapid_claims={"sub": VAPID_CONTACT_EMAIL},
            timeout=10,
        )
        return True
    except WebPushException as error:
        if error.response is not None and error.response.status_code == 410:
            logger.warning("Push subscription gone (410), removing it.")
            return False
        logger.warning("Push delivery failed: %s", error)
        return None
    except (binascii.Error, ValueError, KeyError, TypeError) as error:
        logger.warning("Invalid push subscription (%s), removing it.", error)
        return False
    except Exception as error:
        logger.warning("Push delivery failed: %s", error)
        return None


def _worker() -> None:
    while True:
        item = _queue.get()
        if item is None:
            return
        removed: List[str] = []
        for subscription in item["subscribers"]:
            try:
                result = _send(item["session_id"], subscription, item["title"], item["body"])
            except Exception as error:
                # Never let an unexpected error kill the worker: deliveries
                # must continue for the remaining subscriptions.
                logger.error("Unexpected error in push worker: %s", error)
                result = None
            if result is False:
                removed.append(subscription.get("endpoint", ""))
        if removed:
            with _lock:
                _subscriptions[:] = [
                    sub for sub in _subscriptions if sub.get("endpoint") not in removed
                ]
                _persist_subscriptions()


def start() -> None:
    _load_or_create_keys()
    _load_subscriptions()
    logger.info("Loaded %d push subscription(s).", len(_subscriptions))
    if not REGISTRATION_TOKEN:
        logger.warning(
            "LIVETRACK_PUSH_TOKEN is not set; push subscription requests will be rejected."
        )
    thread = threading.Thread(target=_worker, daemon=True, name="push-worker")
    thread.start()


def stop() -> None:
    _queue.put(None)
