"""Encryption for stored credentials (OAuth tokens). Keyed by SECRET_KEY; refuses to work without it."""
import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from ..config import settings


class SecretKeyMissing(RuntimeError):
    pass


def _fernet_for(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _fernet() -> MultiFernet:
    """Encrypts with SECRET_KEY; also decrypts values written with SECRET_KEY_OLD during a key rotation."""
    if not settings.secret_key:
        raise SecretKeyMissing("Set SECRET_KEY before connecting mailboxes, calendars or call tools.")
    keys = [_fernet_for(settings.secret_key)]
    if settings.secret_key_old:
        keys.append(_fernet_for(settings.secret_key_old))
    return MultiFernet(keys)


def rotate(value: str | None) -> str | None:
    """Re-encrypt a stored value with the current key."""
    return _fernet().rotate(value.encode()).decode() if value else None


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
