"""Privacy and compliance: lawful basis per contact, one-click unsubscribe, data-subject export and erasure,
and retention of AI run logs and old drafts.

- Lawful basis: in countries listed in CONSENT_REQUIRED_COUNTRIES, an email is blocked until the contact has a
  recorded basis (consent, legitimate_interest or contract). Elsewhere it is recorded but not required.
- Unsubscribe: every email carries a personal one-click link (and List-Unsubscribe headers); the link is signed,
  so it cannot be used to unsubscribe someone else.
- Export / erase: an admin can export everything held about an email address, or erase it. Erasure removes the
  person's records and keeps only a suppression entry (so they are never contacted again).
"""
import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (Activity, AgentRun, Approval, CampaignMember, Contact, ContextEvent, EmailEvent, Enrollment,
                      NurtureMember, PersonProfile, Task)

BASES = ("consent", "legitimate_interest", "contract")


# ------------------------------------------------------------------ lawful basis

def required_countries() -> set[str]:
    return {c.strip().lower() for c in (settings.consent_required_countries or "").split(",") if c.strip()}


def basis_problem(contact: Contact | None) -> str | None:
    """A reason this contact may not be emailed yet, or None."""
    if not contact or not required_countries():
        return None
    country = (contact.account.country or "").strip().lower() if contact.account else ""
    if country in required_countries() and contact.lawful_basis not in BASES:
        return (f"No lawful basis recorded for a contact in {contact.account.country}. "
                "Record consent or legitimate interest on the lead first.")
    return None


# ------------------------------------------------------------------ unsubscribe links

def _key() -> bytes:
    return (settings.secret_key or "revenueos-unsubscribe").encode()


def unsubscribe_token(email: str) -> str:
    e = email.strip().lower()
    sig = hmac.new(_key(), f"unsub:{e}".encode(), hashlib.sha256).hexdigest()[:24]
    return base64.urlsafe_b64encode(f"{e}|{sig}".encode()).decode().rstrip("=")


def read_unsubscribe_token(token: str) -> str | None:
    try:
        e, sig = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode().rsplit("|", 1)
    except Exception:
        return None
    good = hmac.new(_key(), f"unsub:{e}".encode(), hashlib.sha256).hexdigest()[:24]
    return e if hmac.compare_digest(good, sig) else None


def unsubscribe_url(email: str) -> str | None:
    if not settings.public_base_url:
        return None
    from ..db import current_workspace

    ws = ""
    ctx = current_workspace.get()
    if ctx is not None:
        from ..db import control_scope
        from ..models import Workspace

        with control_scope() as db:
            w = db.get(Workspace, ctx)
            ws = f"&ws={w.slug}" if w else ""
    return f"{settings.public_base_url.rstrip('/')}/api/public/unsubscribe?t={unsubscribe_token(email)}{ws}"


# ------------------------------------------------------------------ export / erase

def _row(obj, *fields) -> dict:
    return {f: getattr(obj, f) for f in fields}


def export(db: Session, email: str) -> dict:
    email = email.strip().lower()
    contacts = list(db.scalars(select(Contact).where(Contact.email.ilike(email))))
    ids = [c.id for c in contacts]
    out = {"email": email, "exported_at": datetime.now(timezone.utc).isoformat(), "contacts": []}
    for c in contacts:
        out["contacts"].append({
            "contact": _row(c, "id", "first_name", "last_name", "title", "email", "linkedin_url", "source", "status",
                            "score", "score_reason", "owner", "lifecycle", "lawful_basis", "consent_source",
                            "consent_at", "created_at") | {"company": c.account.name},
            "activities": [_row(a, "type", "detail", "created_at") for a in
                           db.scalars(select(Activity).where(Activity.contact_id == c.id))],
            "messages": [_row(a, "kind", "status", "subject", "body", "sent_at", "created_at") for a in
                         db.scalars(select(Approval).where(Approval.contact_id == c.id))],
            "email_events": [_row(e, "event", "url", "occurred_at") for e in
                             db.scalars(select(EmailEvent).where(EmailEvent.contact_id == c.id))],
            "timeline": [_row(e, "source", "title", "summary", "occurred_at") for e in
                         db.scalars(select(ContextEvent).where(ContextEvent.contact_id == c.id))],
            "profile": (lambda p: _row(p, "summary", "public_statements", "sources", "updated_at") if p else None)(
                db.scalar(select(PersonProfile).where(PersonProfile.contact_id == c.id))),
        })
    out["found"] = bool(ids)
    return out


def erase(db: Session, email: str) -> dict:
    """Delete everything about this person; keep only a suppression entry so they are never contacted again."""
    from .inbound import suppress

    email = email.strip().lower()
    contacts = list(db.scalars(select(Contact).where(Contact.email.ilike(email))))
    ids = [c.id for c in contacts]
    suppress(db, email, "erasure request")
    if not ids:
        return {"erased_contacts": 0}
    approval_ids = list(db.scalars(select(Approval.id).where(Approval.contact_id.in_(ids))))
    db.execute(update(Task).where(Task.approval_id.in_(approval_ids)).values(approval_id=None))
    db.execute(delete(EmailEvent).where(or_(EmailEvent.contact_id.in_(ids), EmailEvent.approval_id.in_(approval_ids))))
    for model in (Activity, Task, CampaignMember, NurtureMember, PersonProfile, ContextEvent):
        db.execute(delete(model).where(model.contact_id.in_(ids)))
    db.execute(update(Approval).where(Approval.id.in_(approval_ids)).values(enrollment_id=None))
    db.execute(delete(Approval).where(Approval.id.in_(approval_ids)))
    db.execute(delete(Enrollment).where(Enrollment.contact_id.in_(ids)))
    from ..models import CalendarEvent, Opportunity

    db.execute(update(Opportunity).where(Opportunity.primary_contact_id.in_(ids)).values(primary_contact_id=None))
    db.execute(update(CalendarEvent).where(CalendarEvent.contact_id.in_(ids)).values(contact_id=None))
    for c in contacts:
        db.delete(c)
    return {"erased_contacts": len(ids), "erased_messages": len(approval_ids)}


# ------------------------------------------------------------------ retention

def purge_old(db: Session, now: datetime | None = None) -> dict:
    """AI run inputs/outputs (they contain prompts and personal data) and the text of rejected or failed drafts
    are cleared after RUN_RETENTION_DAYS / DRAFT_RETENTION_DAYS. Costs and counts are kept for reporting."""
    now = now or datetime.now(timezone.utc)
    run_cut = now - timedelta(days=int(settings.run_retention_days))
    draft_cut = now - timedelta(days=int(settings.draft_retention_days))
    runs = db.execute(update(AgentRun).where(AgentRun.started_at < run_cut, AgentRun.input != "[purged]")
                      .values(input="[purged]", output=None, tool_calls=[], error=None)
                      .execution_options(synchronize_session=False)).rowcount
    drafts = db.execute(update(Approval).where(Approval.status.in_(["rejected", "failed"]),
                                               Approval.created_at < draft_cut, Approval.body != "[purged]")
                        .values(body="[purged]", ai_body=None, grounding_extra=None)
                        .execution_options(synchronize_session=False)).rowcount
    return {"runs_purged": runs, "drafts_purged": drafts}
