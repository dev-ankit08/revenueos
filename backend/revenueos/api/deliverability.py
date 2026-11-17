"""Deliverability: the sending pool (mailboxes, caps, warm-up, health), DNS readiness and inbox placement tests."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Connection, PlacementTest, SendingMailbox
from ..services import deliverability as svc
from ..services.approvals import audit
from .workspace import caller, require_admin

router = APIRouter(prefix="/api/deliverability", tags=["deliverability"])


def _actor(request: Request) -> str:
    user = caller(request).get("user")
    return (user.get("name") or user.get("email")) if isinstance(user, dict) else str(user or "anonymous")


@router.get("")
def overview(db: Session = Depends(get_db)):
    return svc.overview(db)


class MailboxIn(BaseModel):
    address: str
    provider: str = Field(default="smtp", pattern="^(smtp|sendgrid|postmark|google|microsoft)$")
    connection_id: int | None = None  # for google/microsoft: the rep's connected mailbox
    owner: str | None = None
    daily_cap: int | None = Field(default=None, ge=1, le=200)
    warmup_days: int = Field(default=21, ge=0, le=60)  # 0 = mailbox is already warm


@router.post("/mailboxes", dependencies=[Depends(require_admin)])
def add_mailbox(body: MailboxIn, request: Request, db: Session = Depends(get_db)):
    address = body.address.strip().lower()
    if "@" not in address:
        raise HTTPException(422, "Enter a full email address.")
    if db.scalar(select(SendingMailbox).where(SendingMailbox.address == address, SendingMailbox.status != "removed")):
        raise HTTPException(409, "That mailbox is already in the pool.")
    conn_id = body.connection_id
    if body.provider in ("google", "microsoft"):
        conn = db.get(Connection, conn_id) if conn_id else db.scalar(
            select(Connection).where(Connection.provider == body.provider, Connection.account_email.ilike(address),
                                     Connection.status == "active"))
        if not conn:
            raise HTTPException(422, f"Connect {address} on Integrations first (Connect {body.provider.title()}).")
        conn_id = conn.id
    old = db.scalar(select(SendingMailbox).where(SendingMailbox.address == address))
    mb = old or SendingMailbox(address=address)
    if not old:
        db.add(mb)
    mb.provider, mb.connection_id, mb.owner = body.provider, conn_id, body.owner
    mb.daily_cap = body.daily_cap or int(settings.mailbox_daily_cap)
    mb.warmup_days = body.warmup_days
    mb.warmup_started_at = datetime.now(timezone.utc) if body.warmup_days else None
    mb.status = "warming" if body.warmup_days else "active"
    mb.paused_reason = None
    db.flush()
    audit(db, _actor(request), "mailbox.add", "mailbox", mb.id, address=address, provider=body.provider)
    db.commit()
    return svc.mailbox_dict(db, mb)


class MailboxPatch(BaseModel):
    daily_cap: int | None = Field(default=None, ge=1, le=200)
    owner: str | None = None


@router.patch("/mailboxes/{mailbox_id}", dependencies=[Depends(require_admin)])
def update_mailbox(mailbox_id: int, body: MailboxPatch, db: Session = Depends(get_db)):
    mb = db.get(SendingMailbox, mailbox_id)
    if not mb:
        raise HTTPException(404, "Mailbox not found")
    if body.daily_cap:
        mb.daily_cap = body.daily_cap
    if body.owner is not None:
        mb.owner = body.owner or None
    db.commit()
    return svc.mailbox_dict(db, mb)


@router.post("/mailboxes/{mailbox_id}/{action}", dependencies=[Depends(require_admin)])
def mailbox_action(mailbox_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    mb = db.get(SendingMailbox, mailbox_id)
    if not mb or mb.status == "removed":
        raise HTTPException(404, "Mailbox not found")
    if action == "pause":
        mb.status, mb.paused_reason, mb.paused_at = "paused", f"Paused by {_actor(request)}.", datetime.now(timezone.utc)
    elif action == "resume":
        warming = mb.warmup_started_at and svc.warmup_cap(mb) < mb.daily_cap
        mb.status, mb.paused_reason = ("warming" if warming else "active"), None
    elif action == "remove":
        mb.status = "removed"
    else:
        raise HTTPException(404, "Unknown action")
    audit(db, _actor(request), f"mailbox.{action}", "mailbox", mb.id)
    db.commit()
    return svc.mailbox_dict(db, mb)


@router.get("/dns")
def dns(domain: str):
    """SPF, DKIM, DMARC and MX for a sending domain, with a 0-100 score and plain-English fixes."""
    try:
        return svc.dns_check(domain)
    except RuntimeError as exc:
        raise HTTPException(503, f"Couldn't look up DNS right now: {exc}")


@router.post("/placement-tests")
def start_placement(db: Session = Depends(get_db)):
    try:
        test = svc.start_placement_test(db)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return _test(test)


@router.get("/placement-tests/{test_id}")
def get_placement(test_id: int, db: Session = Depends(get_db)):
    """Checks where each test email landed. Give it 3-5 minutes after starting."""
    test = db.get(PlacementTest, test_id)
    if not test:
        raise HTTPException(404, "Test not found")
    svc.check_placement_test(db, test)
    db.commit()
    return _test(test)


def _test(t: PlacementTest) -> dict:
    return {"id": t.id, "token": t.token, "status": t.status, "placement_pct": t.placement_pct,
            "results": t.results or [], "created_at": t.created_at, "checked_at": t.checked_at}
