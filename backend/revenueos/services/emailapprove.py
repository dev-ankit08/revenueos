"""Approve drafts from email or Slack without opening RevenueOS.

Each link is signed for one draft, one action and one reviewer, and expires after LINK_HOURS. Opening a link shows a
confirmation page; nothing changes until the person clicks the button on that page. That matters because company
email security scanners open every link in an email automatically.
"""
import hmac
import time
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Approval
from . import secrets

LINK_HOURS = 72
ACTIONS = ("approve", "reject")


def _message(approval_id: int, action: str, reviewer: str, expires: int) -> str:
    return f"approval-link:{approval_id}:{action}:{reviewer.lower()}:{expires}"


def link(approval_id: int, action: str, reviewer: str, now: float | None = None) -> str | None:
    """The signed URL, or None when links can't be made (no SECRET_KEY or PUBLIC_BASE_URL)."""
    if action not in ACTIONS or not settings.secret_key or not settings.public_base_url:
        return None
    expires = int((now or time.time()) + LINK_HOURS * 3600)
    sig = secrets.sign(_message(approval_id, action, reviewer, expires))
    url = (f"{settings.public_base_url.rstrip('/')}/api/public/approvals/{approval_id}/{action}"
           f"?r={quote(reviewer.lower())}&e={expires}&s={sig}")
    from .privacy import unsubscribe_url  # reuse its workspace lookup for ?ws=

    ws_url = unsubscribe_url("x@x") or ""
    if "&ws=" in ws_url:
        url += "&ws=" + ws_url.split("&ws=", 1)[1]
    return url


def verify(approval_id: int, action: str, reviewer: str, expires: int, sig: str, now: float | None = None) -> str | None:
    """None if the link is good, else the reason it isn't."""
    if action not in ACTIONS:
        return "Unknown action."
    if int(expires) < (now or time.time()):
        return "This link has expired. Open Approvals in RevenueOS instead."
    try:
        good = secrets.sign(_message(approval_id, action, reviewer, int(expires)))
    except secrets.SecretKeyMissing:
        return "Links are not set up on this server."
    if not hmac.compare_digest(good, sig or ""):
        return "This link isn't valid."
    return None


def pending_for(db: Session, owner: str, limit: int = 5) -> list[Approval]:
    return [a for a in db.scalars(select(Approval).where(Approval.status == "pending").order_by(Approval.created_at))
            if a.contact and a.contact.owner == owner][:limit]


def digest_lines(db: Session, owner: str) -> list[str]:
    """Up to five drafts with their first lines and approve/reject links, for the morning digest."""
    lines = []
    for a in pending_for(db, owner):
        blocked = any(f.get("severity") == "block" for f in a.guardrail_flags or [])
        preview = " ".join((a.body or "").split())[:160]
        lines.append(f"\n* {a.contact.full_name} ({a.contact.account.name}): {a.subject or a.kind}")
        lines.append(f"  \"{preview}...\"")
        if blocked:
            lines.append("  Blocked by a safety check: open it in RevenueOS to fix.")
            continue
        ok, no = link(a.id, "approve", owner), link(a.id, "reject", owner)
        if ok and no:
            lines.append(f"  Approve as written: {ok}")
            lines.append(f"  Reject: {no}")
    return lines
