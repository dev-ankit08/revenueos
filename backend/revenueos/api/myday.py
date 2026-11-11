"""My day: one screen with what a rep should do today, most urgent first."""
from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Activity, Approval, CalendarEvent, Contact, Task
from ..services import lifecycle
from .sequences import task_to_dict
from .workspace import caller

router = APIRouter(prefix="/api", tags=["my-day"])

HOT_REPLIES = ("reply_interested", "reply_question", "reply_referral", "reply_objection")


def _me(request: Request) -> set[str]:
    """Names a contact's or task's `owner` may carry for the signed-in user."""
    user = caller(request).get("user")
    if not isinstance(user, dict):
        return set()
    return {v for v in (user.get("name"), user.get("email")) if v}


@router.get("/my-day")
def my_day(request: Request, mine: bool = False, db: Session = Depends(get_db)):
    """mine=true limits to contacts and tasks owned by the signed-in user (unowned items are always shown)."""
    me = _me(request) if mine else set()
    now = datetime.now(timezone.utc)
    end_of_day = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=timezone.utc)

    def owned(owner: str | None) -> bool:
        return not me or not owner or owner in me

    lifecycle.refresh_all(db)
    db.commit()

    tasks = [t for t in db.scalars(select(Task).where(Task.status == "open", Task.due_at.is_not(None),
                                                      Task.due_at < end_of_day).order_by(Task.due_at))
             if owned(t.owner)]
    pending = [a for a in db.scalars(select(Approval).where(Approval.status == "pending").order_by(Approval.created_at))
               if owned(a.contact.owner if a.contact else None)]
    meetings = [e for e in db.scalars(select(CalendarEvent).where(
        CalendarEvent.starts_at >= now, CalendarEvent.starts_at < now + timedelta(hours=48),
        CalendarEvent.contact_id.is_not(None)).order_by(CalendarEvent.starts_at))]
    since = now - timedelta(days=7)
    hot = db.execute(select(Activity, Contact).join(Contact, Contact.id == Activity.contact_id).where(
        Activity.type.in_(HOT_REPLIES), Activity.created_at >= since).order_by(Activity.created_at.desc())).all()
    seen, hot_rows = set(), []
    for act, c in hot:
        if c.id in seen or not owned(c.owner) or c.status in ("meeting", "disqualified"):
            continue
        seen.add(c.id)
        hot_rows.append({"contact_id": c.id, "name": c.full_name, "account": c.account.name, "type": act.type,
                         "detail": act.detail[:300], "at": act.created_at})
    breaches = [b for b in lifecycle.sla_breaches(db, now) if owned(b["owner"])]
    failed = db.scalars(select(Approval).where(Approval.status == "failed").order_by(Approval.decided_at.desc())).all()

    def age_hours(dt) -> float:
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return round((now - dt).total_seconds() / 3600, 1)

    return {
        "summary": {"tasks_due": len(tasks), "approvals_waiting": len(pending), "meetings_48h": len(meetings),
                    "hot_replies": len(hot_rows), "sla_breaches": len(breaches), "failed_sends": len(failed)},
        "sla_breaches": breaches[:20],
        "hot_replies": hot_rows[:20],
        "tasks": [task_to_dict(t) for t in tasks[:50]],
        "approvals": [{"id": a.id, "kind": a.kind, "subject": a.subject or a.title,
                       "contact": a.contact.full_name if a.contact else None,
                       "account": a.contact.account.name if a.contact else None,
                       "waiting_hours": age_hours(a.created_at),
                       "blocked": any(f.get("severity") == "block" for f in a.guardrail_flags or [])}
                      for a in pending[:50]],
        "meetings": [{"id": e.id, "title": e.title, "starts_at": e.starts_at, "contact_id": e.contact_id,
                      "prep_status": e.prep_status, "prep_document_id": e.prep_document_id} for e in meetings[:20]],
        "failed_sends": [{"id": a.id, "subject": a.subject, "contact": a.contact.full_name if a.contact else None,
                          "note": a.review_note} for a in failed[:10]],
    }
