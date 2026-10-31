"""Sign-in: password hashing (scrypt), bearer session tokens (stored hashed, revocable, 14-day expiry)."""
import hashlib
import hmac
import secrets as pysecrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from ..db import control_scope
from ..models import AuthSession, User, Workspace

SESSION_DAYS = 14
_users_exist = False


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Passwords need at least 10 characters.")
    salt = pysecrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(check.hex(), digest)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def users_exist() -> bool:
    """Once any user exists, every /api call must be signed in (or use the service API_TOKEN)."""
    global _users_exist
    if not _users_exist:
        with control_scope() as db:
            _users_exist = bool(db.scalar(select(func.count()).select_from(User)))
    return _users_exist


def reset_cache() -> None:
    global _users_exist
    _users_exist = False


def create_user(email: str, password: str, workspace_id: int, role: str = "member", name: str | None = None) -> User:
    if role not in ("owner", "admin", "member"):
        raise ValueError("Role must be owner, admin or member.")
    with control_scope() as db:
        if db.scalar(select(User).where(func.lower(User.email) == email.lower())):
            raise ValueError("A user with that email already exists.")
        u = User(email=email.lower(), name=name, password_hash=hash_password(password), workspace_id=workspace_id, role=role)
        db.add(u)
        db.flush()
        db.expunge(u)
    reset_cache()
    return u


def login(email: str, password: str) -> tuple[str, User] | None:
    with control_scope() as db:
        u = db.scalar(select(User).where(func.lower(User.email) == email.lower(), User.active.is_(True)))
        if not u or not verify_password(password, u.password_hash):
            return None
        token = pysecrets.token_urlsafe(32)
        db.add(AuthSession(token_hash=_sha(token), user_id=u.id,
                           expires_at=datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)))
        db.flush()
        db.expunge(u)
        return token, u


def resolve(token: str) -> tuple[User, Workspace] | None:
    with control_scope() as db:
        s = db.scalar(select(AuthSession).where(AuthSession.token_hash == _sha(token)))
        if not s:
            return None
        expires = s.expires_at if s.expires_at.tzinfo else s.expires_at.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            return None
        u = db.get(User, s.user_id)
        if not u or not u.active:
            return None
        ws = db.get(Workspace, u.workspace_id)
        db.expunge(u)
        db.expunge(ws)
        return u, ws


def logout(token: str) -> None:
    with control_scope() as db:
        s = db.scalar(select(AuthSession).where(AuthSession.token_hash == _sha(token)))
        if s:
            db.delete(s)
