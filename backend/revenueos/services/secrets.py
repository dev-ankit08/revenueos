"""Encryption for stored credentials (OAuth tokens). Keyed by SECRET_KEY; refuses to work without it."""
import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings


class SecretKeyMissing(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not settings.secret_key:
        raise SecretKeyMissing("Set SECRET_KEY before connecting mailboxes, calendars or call tools.")
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def encrypt(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None


def decrypt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise SecretKeyMissing("Stored token can't be decrypted: SECRET_KEY changed? Reconnect the account.") from exc


def sign(message: str) -> str:
    if not settings.secret_key:
        raise SecretKeyMissing("Set SECRET_KEY first.")
    return hmac.new(settings.secret_key.encode(), message.encode(), hashlib.sha256).hexdigest()[:32]
