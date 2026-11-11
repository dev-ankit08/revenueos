"""Lifecycle stages and the MQL response-time SLA.

Stages only move forward: lead -> mql -> sql -> opportunity -> customer (a disqualified contact keeps its
last stage; its status says it is out). Rules, recomputed every scheduler tick:

- customer: the account has a won deal
- opportunity: the account has an open deal
- sql: the contact replied with interest or a meeting happened (status replied/meeting with a positive reply)
- mql: score >= MQL_SCORE and they engaged (inbound form, a reply, or a click on one of our emails)
- lead: everyone else

SLA: from becoming an MQL to the first human touch (a sent email, a completed task, or a logged call)
must take no more than MQL_SLA_HOURS.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Activity, Approval, Contact, Opportunity, Task

STAGES = ("lead", "mql", "sql", "opportunity", "customer")
OPEN_DEAL = ("closed_won", "closed_lost")
POSITIVE_REPLIES = ("reply_interested", "reply_question", "reply_referral", "call_analyzed")
ENGAGED = ("reply", "email_click", "reply_interested", "reply_question", "reply_objection", "reply_referral",
           "reply_not_now", "web_visit_high", "event_registered", "event_attended")


def _aware(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def stage_for(contact: Contact, activity_types: set[str], deals: list[Opportunity]) -> str:
    if any(o.stage == "closed_won" for o in deals):
        return "customer"
    if any(o.stage not in OPEN_DEAL for o in deals):
        return "opportunity"
    if contact.status == "meeting" or activity_types & set(POSITIVE_REPLIES):
        return "sql"
    engaged = contact.source == "inbound" or bool(activity_types & set(ENGAGED))
    if engaged and (contact.score or 0) >= int(settings.mql_score):
        return "mql"
    return "lead"


def refresh_all(db: Session) -> int:
    """Recompute every contact's stage; stamp MQL time and first touch. Returns how many changed."""
    now = datetime.now(timezone.utc)
    acts: dict[int, set[str]] = {}
    for contact_id, kind in db.execute(select(Activity.contact_id, Activity.type)):
        acts.setdefault(contact_id, set()).add(kind)
    deals: dict[int, list[Opportunity]] = {}
    for o in db.scalars(select(Opportunity)):
        deals.setdefault(o.account_id, []).append(o)
    changed = 0
    for c in db.scalars(select(Contact)):
        new = stage_for(c, acts.get(c.id, set()), deals.get(c.account_id, []))
        old = c.lifecycle or "lead"
        if c.lifecycle is None or STAGES.index(new) > STAGES.index(old):
            if new != c.lifecycle:
                changed += 1
            c.lifecycle, c.lifecycle_changed_at = new, now
            if new != "lead" and c.mql_at is None:
                c.mql_at = now
        if c.mql_at is not None and c.first_touch_at is None:
            c.first_touch_at = first_touch_after(db, c.id, _aware(c.mql_at))
    return changed


def first_touch_after(db: Session, contact_id: int, since: datetime) -> datetime | None:
    times = []
    sent = db.scalar(select(Approval.sent_at).where(Approval.contact_id == contact_id, Approval.sent_at.is_not(None),
                                                     Approval.sent_at >= since).order_by(Approval.sent_at))
    if sent:
        times.append(_aware(sent))
    done = db.scalar(select(Task.completed_at).where(Task.contact_id == contact_id, Task.status == "done",
                                                      Task.completed_at >= since).order_by(Task.completed_at))
    if done:
        times.append(_aware(done))
    call = db.scalar(select(Activity.created_at).where(Activity.contact_id == contact_id, Activity.type == "call_analyzed",
                                                Activity.created_at >= since).order_by(Activity.created_at))
    if call:
        times.append(_aware(call))
    return min(times) if times else None


def sla_breaches(db: Session, now: datetime | None = None) -> list[dict]:
    """MQLs (and later stages) still waiting for a first human touch past the SLA."""
    now = now or datetime.now(timezone.utc)
    limit = timedelta(hours=int(settings.mql_sla_hours))
    out = []
    for c in db.scalars(select(Contact).where(Contact.mql_at.is_not(None), Contact.first_touch_at.is_(None),
                                              Contact.status != "disqualified")):
        waited = now - _aware(c.mql_at)
        if waited > limit:
            out.append({"contact_id": c.id, "name": c.full_name, "account": c.account.name, "owner": c.owner,
                        "lifecycle": c.lifecycle, "mql_at": c.mql_at, "hours_waiting": round(waited.total_seconds() / 3600, 1)})
    return sorted(out, key=lambda r: -r["hours_waiting"])


def funnel(db: Session) -> dict:
    counts = {s: 0 for s in STAGES}
    for (stage,) in db.execute(select(Contact.lifecycle)):
        counts[stage or "lead"] = counts.get(stage or "lead", 0) + 1
    limit = timedelta(hours=int(settings.mql_sla_hours))
    touched = within = 0
    for c in db.scalars(select(Contact).where(Contact.mql_at.is_not(None), Contact.first_touch_at.is_not(None))):
        touched += 1
        within += (_aware(c.first_touch_at) - _aware(c.mql_at)) <= limit
    breaches = sla_breaches(db)
    return {"stages": counts, "sla_hours": int(settings.mql_sla_hours), "mql_score": int(settings.mql_score),
            "sla_met_pct": round(within / touched * 100, 1) if touched else None,
            "touched": touched, "waiting_past_sla": len(breaches), "breaches": breaches[:20]}
