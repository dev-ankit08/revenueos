"""Two-factor sign-in (TOTP, works with Google Authenticator, Microsoft Authenticator, 1Password...),
login lockout after repeated failures, and single sign-on with any OpenID Connect provider
(Microsoft Entra ID, Google Workspace, Okta...)."""
import base64
import hashlib
import hmac
import json
import secrets as pysecrets
import struct
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from ..config import settings

# ------------------------------------------------------------------ TOTP (RFC 6238)

def new_totp_secret() -> str:
    return base64.b32encode(pysecrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((at or time.time()) // 30))


def verify_totp(secret: str, code: str, at: float | None = None, window: int = 1) -> bool:
    """Accepts the current code and one step either side (clock drift)."""
    code = (code or "").strip().replace(" ", "")
    if not (len(code) == 6 and code.isdigit()):
        return False
    step = int((at or time.time()) // 30)
    return any(hmac.compare_digest(_hotp(secret, step + d), code) for d in range(-window, window + 1))


def otpauth_uri(secret: str, email: str) -> str:
    issuer = urllib.parse.quote(f"RevenueOS ({settings.company_name})")
    return f"otpauth://totp/{issuer}:{urllib.parse.quote(email)}?secret={secret}&issuer={issuer}&digits=6&period=30"


# ------------------------------------------------------------------ lockout

MAX_FAILED = 5
LOCK_MINUTES = 15


def locked(user) -> bool:
    until = user.locked_until
    if until is None:
        return False
    until = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
    return until > datetime.now(timezone.utc)


def record_failure(user) -> None:
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= MAX_FAILED:
        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCK_MINUTES)
        user.failed_logins = 0


def record_success(user) -> None:
    user.failed_logins, user.locked_until = 0, None


# ------------------------------------------------------------------ OpenID Connect SSO

def oidc_enabled() -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id and settings.oidc_client_secret)


def _discovery() -> dict:
    from ..integrations import http

    return http.request("GET", settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration")


def oidc_redirect_uri() -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/auth/oidc/callback"


def oidc_start_url(state: str, nonce: str) -> str:
    d = _discovery()
    q = urllib.parse.urlencode({"client_id": settings.oidc_client_id, "response_type": "code",
                                "scope": "openid email profile", "redirect_uri": oidc_redirect_uri(),
                                "state": state, "nonce": nonce})
    return f"{d['authorization_endpoint']}?{q}"


def sign_state(payload: dict) -> str:
    from . import secrets

    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{body}.{secrets.sign(body)}"


def read_state(state: str, max_age: int = 600) -> dict | None:
    from . import secrets

    try:
        body, sig = state.rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(secrets.sign(body), sig):
        return None
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if time.time() - payload.get("t", 0) > max_age:
        return None
    return payload


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def verify_id_token(id_token: str, jwks: dict, nonce: str, now: float | None = None) -> dict:
    """Validate an RS256/ES256 ID token: signature (against the provider's published keys), issuer,
    audience, expiry and nonce. Returns the claims or raises ValueError."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    try:
        header_b64, payload_b64, sig_b64 = id_token.split(".")
    except ValueError:
        raise ValueError("Malformed ID token") from None
    header, claims = json.loads(_b64(header_b64)), json.loads(_b64(payload_b64))
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
    if not key:
        raise ValueError("Signing key not found")
    signed, sig = f"{header_b64}.{payload_b64}".encode(), _b64(sig_b64)
    alg = header.get("alg")
    if alg == "RS256" and key.get("kty") == "RSA":
        pub = rsa.RSAPublicNumbers(int.from_bytes(_b64(key["e"]), "big"), int.from_bytes(_b64(key["n"]), "big")).public_key()
        try:
            pub.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
        except Exception:
            raise ValueError("Bad ID token signature") from None
    elif alg == "ES256" and key.get("kty") == "EC":
        pub = ec.EllipticCurvePublicNumbers(int.from_bytes(_b64(key["x"]), "big"), int.from_bytes(_b64(key["y"]), "big"),
                                            ec.SECP256R1()).public_key()
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        try:
            pub.verify(der, signed, ec.ECDSA(hashes.SHA256()))
        except Exception:
            raise ValueError("Bad ID token signature") from None
    else:
        raise ValueError(f"Unsupported ID token algorithm {alg}")
    now = now or time.time()
    aud = claims.get("aud")
    if claims.get("iss", "").rstrip("/") != settings.oidc_issuer.rstrip("/"):
        raise ValueError("Wrong issuer")
    if settings.oidc_client_id not in (aud if isinstance(aud, list) else [aud]):
        raise ValueError("Wrong audience")
    if claims.get("exp", 0) < now - 60:
        raise ValueError("ID token expired")
    if claims.get("nonce") != nonce:
        raise ValueError("Bad nonce")
    return claims


def oidc_exchange(code: str, nonce: str) -> dict:
    """Code -> tokens -> verified claims (email, name)."""
    from ..integrations import http

    d = _discovery()
    tokens = http.request("POST", d["token_endpoint"], form={
        "grant_type": "authorization_code", "code": code, "redirect_uri": oidc_redirect_uri(),
        "client_id": settings.oidc_client_id, "client_secret": settings.oidc_client_secret})
    jwks = http.request("GET", d["jwks_uri"])
    return verify_id_token(tokens["id_token"], jwks, nonce)
