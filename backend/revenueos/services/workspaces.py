"""Client workspaces: provisioning, per-workspace settings (secrets encrypted), lookup by slug."""
import re

from sqlalchemy import select

from ..config import SECRET_KEYS, WORKSPACE_KEYS, settings
from ..db import _factory_for, control_scope, current_workspace, workspace_db_location
from ..models import Workspace
from . import secrets

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}$")
_cache: dict[int | None, dict] = {}
INT_KEYS = {"smtp_port", "daily_send_cap", "per_domain_daily_cap", "personalisation_threshold",
            "signal_scan_interval_days", "review_interval_days", "send_window_start_hour", "send_window_end_hour",
            "mql_score", "mql_sla_hours", "brief_max_age_days", "dossier_max_age_days", "digest_hour",
            "event_engaged_minutes", "renewal_lead_days"}
FLOAT_KEYS = {"holdout_pct", "quarterly_target"}
BOOL_KEYS = {"email_tracking", "send_weekdays_only", "utm_enabled", "intent_create_accounts"}


def ensure_default() -> Workspace:
    with control_scope() as db:
        ws = db.scalar(select(Workspace).where(Workspace.is_default.is_(True)))
        if not ws:
            ws = Workspace(slug="default", name=settings._base.company_name, is_default=True, settings={})
            db.add(ws)
            db.flush()
        db.expunge(ws)
        return ws


def context_id(ws: Workspace) -> int | None:
    """The value of current_workspace for this workspace (None = default database)."""
    return None if ws.is_default else ws.id


def _row(db, ctx: int | None) -> Workspace | None:
    if ctx is None:
        return db.scalar(select(Workspace).where(Workspace.is_default.is_(True)))
    return db.get(Workspace, ctx)


def overrides(ctx: int | None) -> dict:
    """Decrypted settings for a workspace (cached; invalidated on update)."""
    if ctx not in _cache:
        with control_scope() as db:
            ws = _row(db, ctx)
            raw = dict(ws.settings or {}) if ws else {}
        _cache[ctx] = {k: (secrets.decrypt(v) if k in SECRET_KEYS and v else v) for k, v in raw.items()}
    return _cache[ctx]


def install() -> None:
    """Make `settings` workspace-aware."""
    object.__setattr__(settings, "_overrides", lambda: overrides(current_workspace.get()))


def public_settings(ctx: int | None) -> dict:
    """Effective settings for the Settings page; secrets shown only as set / not set."""
    own = overrides(ctx)
    out = {}
    for k in sorted(WORKSPACE_KEYS):
        if k in SECRET_KEYS:
            out[k] = {"secret": True, "set": bool(own.get(k) or getattr(settings._base, k, None)),
                      "own": bool(own.get(k))}
        else:
            out[k] = {"value": own.get(k, getattr(settings._base, k, None)), "own": k in own}
    return out


def update_settings(ctx: int | None, values: dict) -> None:
    unknown = set(values) - WORKSPACE_KEYS
    if unknown:
        raise ValueError(f"Not a workspace setting: {', '.join(sorted(unknown))}")
    with control_scope() as db:
        ws = _row(db, ctx)
        current = dict(ws.settings or {})
        for k, v in values.items():
            if v is None or v == "":
                current.pop(k, None)  # back to the platform default
                continue
            if k in INT_KEYS:
                v = int(v)
            elif k in FLOAT_KEYS:
                v = float(v)
            elif k in BOOL_KEYS:
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            current[k] = secrets.encrypt(str(v)) if k in SECRET_KEYS else v
        ws.settings = current
    _cache.pop(ctx, None)
    from ..tools import crm

    crm.forget(ctx)  # CRM choice or credentials may have changed


def create_workspace(name: str, slug: str) -> Workspace:
    if not _SLUG.match(slug):
        raise ValueError("Slug: 2-39 lowercase letters, digits or dashes.")
    with control_scope() as db:
        if db.scalar(select(Workspace).where(Workspace.slug == slug)):
            raise ValueError(f"A workspace called {slug!r} already exists.")
        url, schema = workspace_db_location(slug)
        ws = Workspace(slug=slug, name=name, db_url=url, db_schema=schema,
                       settings={"company_name": name})
        db.add(ws)
        db.flush()
        ws_id = ws.id
        db.expunge(ws)
    _factory_for(ws_id)  # creates its database and tables now
    return ws


def by_slug(slug: str | None) -> Workspace | None:
    with control_scope() as db:
        ws = db.scalar(select(Workspace).where(Workspace.slug == (slug or "default")))
        if ws:
            db.expunge(ws)
        return ws


def all_context_ids() -> list[int | None]:
    with control_scope() as db:
        others = list(db.scalars(select(Workspace.id).where(Workspace.is_default.is_(False))))
    return [None, *others]
