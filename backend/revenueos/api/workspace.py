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
    result = auth.login(body.email, body.password)
    if not result:
        raise HTTPException(401, "Wrong email or password.")
    token, user = result
    with control_scope() as db:
        ws = db.get(Workspace, user.workspace_id)
        ws_info = {"slug": ws.slug, "name": ws.name}
    return {"token": token, "user": {"email": user.email, "name": user.name, "role": user.role}, "workspace": ws_info}


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
    return {"user": c["user"], "role": c["role"], "workspace": info,
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
