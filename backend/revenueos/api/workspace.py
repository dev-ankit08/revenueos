"""Sign-in, users, workspaces (clients) and per-workspace settings."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..db import control_scope, current_workspace
from ..models import User, Workspace
from ..services import auth, secrets, workspaces

router = APIRouter(prefix="/api", tags=["workspace"])


# ------------------------------------------------------------------ who is calling

def caller(request: Request) -> dict:
    """{user, role, workspace} for the request (set by the middleware in main.py)."""
    return getattr(request.state, "caller", {"user": None, "role": "admin", "workspace": None})


def require_admin(request: Request) -> dict:
    c = caller(request)
    if c["role"] not in ("admin", "owner"):
        raise HTTPException(403, "Only workspace admins can do this.")
    return c


def require_owner(request: Request) -> dict:
    c = caller(request)
    if c["role"] != "owner":
        raise HTTPException(403, "Only the platform owner can manage client workspaces.")
    return c


# ------------------------------------------------------------------ sign-in

class Credentials(BaseModel):
    email: str
    password: str
    name: str | None = None
    code: str | None = None  # 6-digit two-factor code, when the account has it switched on


@router.post("/auth/bootstrap")
def bootstrap(body: Credentials):
    """Create the first (owner) account. Only works while no users exist."""
    if auth.users_exist():
        raise HTTPException(409, "Already set up. Sign in instead.")
    default = workspaces.ensure_default()
    try:
        auth.create_user(body.email, body.password, default.id, role="owner", name=body.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return login(body)


@router.post("/auth/login")
def login(body: Credentials):
    try:
        result = auth.login(body.email, body.password, body.code)
    except auth.MFARequired as exc:
        raise HTTPException(401, detail={"mfa_required": True, "message": str(exc)})
    except auth.AccountLocked as exc:
        raise HTTPException(423, str(exc))
    if not result:
        raise HTTPException(401, "Wrong email or password.")
    token, user = result
    with control_scope() as db:
        ws = db.get(Workspace, user.workspace_id)
        ws_info = {"slug": ws.slug, "name": ws.name}
    return {"token": token, "user": {"email": user.email, "name": user.name, "role": user.role}, "workspace": ws_info}


def _me_user(request: Request) -> User:
    c = caller(request)
    if not isinstance(c.get("user"), dict):
        raise HTTPException(401, "Sign in first.")
    with control_scope() as db:
        u = db.scalar(select(User).where(func.lower(User.email) == c["user"]["email"].lower()))
        if not u:
            raise HTTPException(404, "User not found")
        db.expunge(u)
        return u


@router.post("/auth/mfa/setup")
def mfa_setup(request: Request):
    """Start two-factor setup: returns a secret and an otpauth:// link to add to an authenticator app."""
    from ..services import security

    u = _me_user(request)
    secret = security.new_totp_secret()
    with control_scope() as db:
        row = db.get(User, u.id)
        row.totp_secret, row.mfa_enabled = secrets.encrypt(secret), False
    return {"secret": secret, "otpauth_uri": security.otpauth_uri(secret, u.email)}


class CodeIn(BaseModel):
    code: str


@router.post("/auth/mfa/enable")
def mfa_enable(body: CodeIn, request: Request):
    from ..services import security

    u = _me_user(request)
    if not u.totp_secret or not security.verify_totp(secrets.decrypt(u.totp_secret), body.code):
        raise HTTPException(422, "That code didn't match. Check the time on your phone and try again.")
    with control_scope() as db:
        db.get(User, u.id).mfa_enabled = True
    return {"mfa_enabled": True}


@router.post("/auth/mfa/disable")
def mfa_disable(body: CodeIn, request: Request):
    from ..services import security

    u = _me_user(request)
    if u.mfa_enabled and not security.verify_totp(secrets.decrypt(u.totp_secret), body.code):
        raise HTTPException(422, "Enter a current code to switch two-factor sign-in off.")
    with control_scope() as db:
        row = db.get(User, u.id)
        row.mfa_enabled, row.totp_secret = False, None
    return {"mfa_enabled": False}


class PasswordReset(BaseModel):
    new_password: str


@router.post("/workspace/users/{user_id}/password", dependencies=[Depends(require_admin)])
def reset_password(user_id: int, body: PasswordReset, request: Request):
    """An admin sets a new password for someone in their workspace (and clears any lockout)."""
    c = caller(request)
    with control_scope() as db:
        u = db.get(User, user_id)
        if not u or (c["role"] != "owner" and not _same_workspace(u)):
            raise HTTPException(404, "User not found")
        try:
            u.password_hash = auth.hash_password(body.new_password)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        u.failed_logins, u.locked_until = 0, None
    return {"ok": True}


def _same_workspace(u: User) -> bool:
    ctx = current_workspace.get()
    with control_scope() as db:
        ws = db.get(Workspace, u.workspace_id)
    return (ctx is None and ws is not None and ws.is_default) or u.workspace_id == ctx


@router.get("/auth/oidc/start")
def oidc_start():
    """Single sign-on: send the browser to the identity provider."""
    import secrets as pysecrets
    import time

    from fastapi.responses import RedirectResponse

    from ..services import security

    if not security.oidc_enabled():
        raise HTTPException(404, "Single sign-on is not configured.")
    nonce = pysecrets.token_urlsafe(16)
    state = security.sign_state({"n": nonce, "t": int(time.time())})
    return RedirectResponse(security.oidc_start_url(state, nonce))


@router.get("/auth/oidc/callback")
def oidc_callback(code: str, state: str):
    from fastapi.responses import RedirectResponse

    from ..config import settings
    from ..services import security

    payload = security.read_state(state)
    if not payload:
        raise HTTPException(400, "Sign-in link expired. Start again.")
    try:
        claims = security.oidc_exchange(code, payload["n"])
    except ValueError as exc:
        raise HTTPException(401, f"Single sign-on failed: {exc}")
    email = (claims.get("email") or claims.get("preferred_username") or "").lower()
    if not email or claims.get("email_verified") is False:
        raise HTTPException(401, "Your identity provider didn't share a verified email address.")
    with control_scope() as db:
        u = db.scalar(select(User).where(func.lower(User.email) == email, User.active.is_(True)))
        user_id = u.id if u else None
    if user_id is None:
        if not settings.oidc_auto_provision:
            raise HTTPException(403, "No RevenueOS account for this email. Ask an admin to add you.")
        import secrets as pysecrets

        u = auth.create_user(email, pysecrets.token_urlsafe(24), workspaces.ensure_default().id, role="member",
                             name=claims.get("name"))
        user_id = u.id
    token = auth.session_for(user_id)
    return RedirectResponse(f"{settings.app_base_url.rstrip('/')}/login#sso_token={token}")


@router.post("/auth/logout")
def logout(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if token:
        auth.logout(token)
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request):
    c = caller(request)
    ws_ctx = current_workspace.get()
    with control_scope() as db:
        ws = (db.get(Workspace, ws_ctx) if ws_ctx is not None
              else db.scalar(select(Workspace).where(Workspace.is_default.is_(True))))
        info = {"slug": ws.slug, "name": ws.name} if ws else {"slug": "default", "name": "Default"}
    from ..services import security

    mfa = False
    if isinstance(c.get("user"), dict):
        with control_scope() as db:
            u = db.scalar(select(User).where(func.lower(User.email) == c["user"]["email"].lower()))
            mfa = bool(u and u.mfa_enabled)
    return {"user": c["user"], "role": c["role"], "workspace": info, "mfa_enabled": mfa,
            "sso_available": security.oidc_enabled(),
            "auth_required": auth.users_exist(), "setup_needed": not auth.users_exist()}


# ------------------------------------------------------------------ workspaces (platform owner)

class WorkspaceIn(BaseModel):
    name: str = Field(min_length=2)
    slug: str
    admin_email: str
    admin_password: str
    admin_name: str | None = None


@router.get("/workspaces", dependencies=[Depends(require_owner)])
def list_workspaces():
    with control_scope() as db:
        counts = dict(db.execute(select(User.workspace_id, func.count()).group_by(User.workspace_id)).all())
        return [{"id": w.id, "slug": w.slug, "name": w.name, "is_default": w.is_default, "users": counts.get(w.id, 0),
                 "created_at": w.created_at} for w in db.scalars(select(Workspace).order_by(Workspace.id))]


@router.post("/workspaces", dependencies=[Depends(require_owner)])
def create_workspace(body: WorkspaceIn):
    """A new client: its own database, settings and admin account."""
    try:
        ws = workspaces.create_workspace(body.name, body.slug)
        auth.create_user(body.admin_email, body.admin_password, ws.id, role="admin", name=body.admin_name)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"id": ws.id, "slug": ws.slug, "name": ws.name}


# ------------------------------------------------------------------ this workspace: settings and users

@router.get("/workspace/settings", dependencies=[Depends(require_admin)])
def get_settings():
    return workspaces.public_settings(current_workspace.get())


@router.put("/workspace/settings", dependencies=[Depends(require_admin)])
def put_settings(values: dict):
    workspaces.ensure_default()
    try:
        workspaces.update_settings(current_workspace.get(), values)
    except (ValueError, secrets.SecretKeyMissing) as exc:
        raise HTTPException(422, str(exc))
    return workspaces.public_settings(current_workspace.get())


def _ws_row_id() -> int:
    ctx = current_workspace.get()
    return ctx if ctx is not None else workspaces.ensure_default().id


class UserIn(BaseModel):
    email: str
    password: str
    name: str | None = None
    role: str = "member"


@router.get("/workspace/users", dependencies=[Depends(require_admin)])
def list_users():
    with control_scope() as db:
        return [{"id": u.id, "email": u.email, "name": u.name, "role": u.role, "active": u.active}
                for u in db.scalars(select(User).where(User.workspace_id == _ws_row_id()))]


@router.post("/workspace/users")
def add_user(body: UserIn, c: dict = Depends(require_admin)):
    if body.role == "owner" and c["role"] != "owner":
        raise HTTPException(403, "Only the platform owner can add owners.")
    try:
        u = auth.create_user(body.email, body.password, _ws_row_id(), role=body.role, name=body.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"id": u.id, "email": u.email, "role": u.role}


@router.delete("/workspace/users/{user_id}", dependencies=[Depends(require_admin)])
def deactivate_user(user_id: int):
    with control_scope() as db:
        u = db.get(User, user_id)
        if not u or u.workspace_id != _ws_row_id():
            raise HTTPException(404, "User not found")
        u.active = False
    return {"id": user_id, "active": False}
