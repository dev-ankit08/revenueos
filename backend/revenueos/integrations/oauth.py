"""OAuth for Google (Gmail, Calendar, Meet) and Microsoft 365 (Outlook mail, calendar, Teams transcripts).

Each rep connects their own account once; access is READ-ONLY. Refresh tokens are encrypted at rest.
"""
import secrets as pysecrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Connection
from ..services import secrets
from . import http

PROVIDERS = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "scopes": ["openid", "email",
                   "https://www.googleapis.com/auth/gmail.readonly",
                   "https://www.googleapis.com/auth/calendar.readonly",
                   "https://www.googleapis.com/auth/meetings.space.readonly"],
        "extra": {"access_type": "offline", "prompt": "consent"},
    },
    "microsoft": {
        "auth_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "scopes": ["offline_access", "User.Read", "Mail.Read", "Calendars.Read",
                   "OnlineMeetings.Read", "OnlineMeetingTranscript.Read.All"],
        "extra": {},
    },
}


def client_credentials(provider: str) -> tuple[str, str]:
    cid, secret = ((settings.google_client_id, settings.google_client_secret) if provider == "google"
                   else (settings.ms_client_id, settings.ms_client_secret))
    if not (cid and secret):
        raise RuntimeError(f"{provider} OAuth is not configured (client id/secret).")
    return cid, secret


def redirect_uri(provider: str) -> str:
    return f"{settings.public_base_url}/api/integrations/oauth/{provider}/callback"


def _url(provider: str, key: str) -> str:
    return PROVIDERS[provider][key].replace("{tenant}", settings.ms_tenant)


def make_state(provider: str) -> str:
    """CSRF protection: a signed, 10-minute state token that also carries the workspace."""
    from ..db import current_workspace

    ws = current_workspace.get()
    body = f"{provider}.{ws if ws is not None else 'd'}.{pysecrets.token_urlsafe(12).replace('.', '')}.{int(time.time())}"
    return f"{body}.{secrets.sign(body)}"


def check_state(provider: str, state: str):
    """False if invalid/expired; otherwise the workspace id it was issued for (None = default)."""
    try:
        prov, ws, nonce, ts, sig = state.split(".")
    except ValueError:
        return False
    body = f"{prov}.{ws}.{nonce}.{ts}"
    if prov != provider or secrets.sign(body) != sig or time.time() - int(ts) >= 600:
        return False
    return None if ws == "d" else int(ws)


def authorize_url(provider: str) -> str:
    cid, _ = client_credentials(provider)
    p = PROVIDERS[provider]
    params = {"client_id": cid, "redirect_uri": redirect_uri(provider), "response_type": "code",
              "scope": " ".join(p["scopes"]), "state": make_state(provider), **p["extra"]}
    return _url(provider, "auth_url") + "?" + urllib.parse.urlencode(params)


def _store_tokens(conn: Connection, tok: dict) -> None:
    conn.access_token_enc = secrets.encrypt(tok["access_token"])
    if tok.get("refresh_token"):
        conn.refresh_token_enc = secrets.encrypt(tok["refresh_token"])
    conn.expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)


def exchange_code(db: Session, provider: str, code: str) -> Connection:
    cid, secret = client_credentials(provider)
    tok = http.request("POST", _url(provider, "token_url"), form={
        "client_id": cid, "client_secret": secret, "code": code, "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(provider)})
    auth = {"Authorization": f"Bearer {tok['access_token']}"}
    if provider == "google":
        email = http.request("GET", "https://openidconnect.googleapis.com/v1/userinfo", headers=auth).get("email")
    else:
        me = http.request("GET", "https://graph.microsoft.com/v1.0/me", headers=auth)
        email = me.get("mail") or me.get("userPrincipalName")
    conn = db.query(Connection).filter_by(provider=provider, account_email=email).first() or Connection(
        provider=provider, account_email=email, cursors={})
    conn.scopes, conn.status, conn.last_error = tok.get("scope"), "active", None
    _store_tokens(conn, tok)
    db.add(conn)
    db.flush()
    return conn


def access_token(db: Session, conn: Connection) -> str:
    """A valid access token, refreshing it when it has expired."""
    expires = conn.expires_at if not conn.expires_at or conn.expires_at.tzinfo else conn.expires_at.replace(tzinfo=timezone.utc)
    if conn.access_token_enc and expires and expires > datetime.now(timezone.utc):
        return secrets.decrypt(conn.access_token_enc)
    cid, secret = client_credentials(conn.provider)
    tok = http.request("POST", _url(conn.provider, "token_url"), form={
        "client_id": cid, "client_secret": secret, "grant_type": "refresh_token",
        "refresh_token": secrets.decrypt(conn.refresh_token_enc)})
    _store_tokens(conn, tok)
    return tok["access_token"]
