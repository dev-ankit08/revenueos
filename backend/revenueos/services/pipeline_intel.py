"""Phase B: zero admin and fewer surprises.

- Activity capture: emails reps send themselves and customer meetings are logged to the CRM automatically, and
  colleagues the customer copies in are suggested as new contacts (one click to add; never auto-created).
- Forecast calibration: a weekly snapshot of the forecast; at quarter end, how close it was, by rep and for the AI.
- Slip prediction: plain rules (no AI cost) flag deals likely to slip, with a task for the owner.
- Meeting-to-pipeline: every customer meeting gets an outcome (held, no-show, qualified...), a reminder email the day
  before (from an approved template), and a funnel from meeting to won deal.
- Relationships and job changes: who on the team actually knows whom (emails and meetings), and champions who
  moved company become a signal at the old account and a lead suggestion at the new one.
"""
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (Account, Activity, Approval, CalendarEvent, Connection, Contact, ContactSuggestion, ContextEvent,
                      ForecastSnapshot, Opportunity, Task)

UTC = timezone.utc
OPEN = ("discovery", "qualification", "solution", "proposal", "negotiation")
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com", "aol.com", "proton.me"}


def _aware(dt):
    return None if dt is None else (dt if dt.tzinfo else dt.replace(tzinfo=UTC))


def touch_deal(db: Session, account_id: int, when: datetime | None = None) -> None:
    when = when or datetime.now(UTC)
    for o in db.scalars(select(Opportunity).where(Opportunity.account_id == account_id, Opportunity.stage.in_(OPEN))):
        if not o.last_activity_at or _aware(o.last_activity_at) < _aware(when):
            o.last_activity_at = when


# ------------------------------------------------------------------ 5. activity capture

def capture_rep_email(db: Session, contact: Contact, subject: str, text: str, mailbox: str | None,
                      recipients: list[str], when: datetime | None = None) -> int:
    """A rep emailed a customer from their own mailbox: log it to the CRM, refresh the deal, suggest new people."""
    from ..tools.crm import get_crm

    db.add(Activity(contact_id=contact.id, type="rep_email", detail=f"{mailbox or 'Rep'}: {subject}"[:2000]))
    get_crm().log_activity(contact, f"Email: {subject}", (text or "")[:4000])
    touch_deal(db, contact.account_id, when)
    return suggest_contacts(db, contact, recipients)


def capture_meeting(db: Session, ev: CalendarEvent) -> None:
    from ..tools.crm import get_crm

    if ev.crm_logged or not ev.contact_id:
        return
    c = db.get(Contact, ev.contact_id)
    get_crm().log_activity(c, f"Meeting: {ev.title}", f"{_aware(ev.starts_at):%Y-%m-%d %H:%M} UTC with "
                                                      + ", ".join(ev.attendees or []))
    db.add(Activity(contact_id=c.id, type="meeting", detail=f"{ev.title} on {_aware(ev.starts_at):%Y-%m-%d}"))
    ev.opportunity_id = ev.opportunity_id or db.scalar(select(Opportunity.id).where(
        Opportunity.account_id == c.account_id, Opportunity.stage.in_(OPEN)).order_by(Opportunity.id.desc()))
    touch_deal(db, c.account_id, ev.starts_at)
    ev.crm_logged = True


def suggest_contacts(db: Session, contact: Contact, emails: list[str]) -> int:
    """People on the thread at the customer's company that we don't know yet."""
    domain = (contact.email or "").split("@")[-1].lower()
    if not domain or domain in FREE_MAIL:
        return 0
    n = 0
    for e in {x.strip().lower() for x in emails if x}:
        if e.split("@")[-1] != domain or e == (contact.email or "").lower():
            continue
        if db.scalar(select(Contact.id).where(func.lower(Contact.email) == e)):
            continue
        if db.scalar(select(ContactSuggestion.id).where(ContactSuggestion.email == e)):
            continue
        local = e.split("@")[0].replace(".", " ").replace("_", " ").title()
        db.add(ContactSuggestion(email=e, name=local, account_id=contact.account_id, seen_with_contact_id=contact.id,
                                 source="email thread", reason=f"On an email thread with {contact.full_name}"))
        n += 1
    return n


def accept_suggestion(db: Session, s: ContactSuggestion, owner: str | None = None) -> Contact:
    first, _, last = (s.name or s.email.split("@")[0]).partition(" ")
    c = Contact(account_id=s.account_id, first_name=first, last_name=last, email=s.email, source="suggested",
                owner=owner or (db.get(Contact, s.seen_with_contact_id).owner if s.seen_with_contact_id else None))
    db.add(c)
    s.status = "added"
    db.flush()
    return c


# ------------------------------------------------------------------ 6. forecast calibration and slip prediction

def take_snapshot(db: Session, now: datetime | None = None) -> ForecastSnapshot:
    from .insights import deal_category, deal_probability, forecast_numbers

    n = forecast_numbers(db)
    deals = {}
    for o in db.scalars(select(Opportunity).where(Opportunity.stage.in_(OPEN))):
        deals[str(o.id)] = {"amount": o.amount, "close_date": o.close_date, "probability": deal_probability(o),
                            "ai_category": deal_category(o), "rep_category": o.rep_forecast_category, "owner": o.owner}
    snap = ForecastSnapshot(period=n["quarter"], taken_at=now or datetime.now(UTC), commit=n["commit"],
                            best_case=n["best_case"], weighted=n["weighted"], won_to_date=n["closed_won_this_quarter"],
                            deals=deals)
    db.add(snap)
    return snap


def _period_bounds(period: str):
    from .salesmgmt import _period_bounds as pb

    return pb(period)


def accuracy(db: Session, period: str) -> dict | None:
    """How good the forecast was for a finished quarter: each weekly snapshot against what actually closed, and
    how often deals called "commit" (by the rep and by the AI) really closed."""
    snaps = list(db.scalars(select(ForecastSnapshot).where(ForecastSnapshot.period == period).order_by(ForecastSnapshot.taken_at)))
    if not snaps:
        return None
    start, end = _period_bounds(period)
    won = [o for o in db.scalars(select(Opportunity).where(Opportunity.stage == "closed_won"))
           if _aware(o.closed_at) and start <= _aware(o.closed_at) < end]
    actual = sum(o.amount or 0 for o in won)
    won_ids = {str(o.id) for o in won}

    def acc(forecast: float) -> float | None:
        return round(max(0.0, 1 - abs(forecast - actual) / actual) * 100, 1) if actual else None

    weekly = [{"taken_at": s.taken_at, "commit_plus_won": s.commit + s.won_to_date, "weighted_plus_won": s.weighted + s.won_to_date,
               "commit_accuracy": acc(s.commit + s.won_to_date), "weighted_accuracy": acc(s.weighted + s.won_to_date)}
              for s in snaps]
    mid = snaps[len(snaps) // 2]  # the call made mid-quarter is the one leaders plan on

    def hit_rate(key: str, owner: str | None = None) -> dict:
        calls = [(i, d) for i, d in mid.deals.items() if d.get(key) == "commit" and (owner is None or d.get("owner") == owner)]
        hits = sum(1 for i, _ in calls if i in won_ids)
        return {"commit_calls": len(calls), "closed": hits, "hit_rate": round(hits / len(calls) * 100, 1) if calls else None}

    owners = sorted({d.get("owner") for d in mid.deals.values() if d.get("owner")})
    return {"period": period, "actual_won": actual, "snapshots": weekly,
            "mid_quarter_accuracy": {"commit": acc(mid.commit + mid.won_to_date), "weighted": acc(mid.weighted + mid.won_to_date)},
            "ai_commit": hit_rate("ai_category"), "rep_commit": hit_rate("rep_category"),
            "by_rep": {o: {"rep": hit_rate("rep_category", o), "ai": hit_rate("ai_category", o)} for o in owners}}


def slip_risk(db: Session, o: Opportunity, now: datetime | None = None) -> tuple[str, list[str]]:
    now = now or datetime.now(UTC)
    reasons = []
    if (o.close_date_pushes or 0) >= 2:
        reasons.append(f"close date pushed {o.close_date_pushes} times")
    idle = (now - _aware(o.last_activity_at)).days if o.last_activity_at else 999
    if idle >= 14:
        reasons.append(f"no activity for {idle} days")
    if not o.next_steps:
        reasons.append("no agreed next step")
    engaged = db.scalar(select(func.count(func.distinct(Activity.contact_id))).join(Contact, Contact.id == Activity.contact_id)
                        .where(Contact.account_id == o.account_id,
                               Activity.type.in_(["reply", "reply_interested", "reply_question", "meeting",
                                                  "call_analyzed", "event_attended"]))) or 0
    if engaged <= 1:
        reasons.append("single-threaded: only one person engaged")
    if o.close_date and o.close_date < now.date().isoformat():
        reasons.append("close date is in the past")
    level = "high" if len(reasons) >= 3 else "medium" if len(reasons) == 2 else "low"
    return level, reasons


def flag_slips(db: Session, now: datetime | None = None) -> int:
    """Daily: score every open deal; a newly high-risk deal gets a task for its owner (once a fortnight)."""
    now = now or datetime.now(UTC)
    n = 0
    for o in db.scalars(select(Opportunity).where(Opportunity.stage.in_(OPEN))):
        level, reasons = slip_risk(db, o, now)
        o.slip_risk, o.slip_reasons = level, reasons
        recently = _aware(o.slip_flagged_at) and now - _aware(o.slip_flagged_at) < timedelta(days=14)
        if level == "high" and not recently:
            o.slip_flagged_at = now
            db.add(Task(type="follow_up", title=f"{o.name} is likely to slip"[:255], opportunity_id=o.id,
                        contact_id=o.primary_contact_id, owner=o.owner, created_by="slip_predictor",
                        due_at=now + timedelta(days=1), notes="Why: " + "; ".join(reasons)))
            n += 1
    return n


# ------------------------------------------------------------------ 7. meeting-to-pipeline

OUTCOMES = ("held", "no_show", "qualified", "next_step_booked", "cancelled", "rescheduled")


def infer_outcomes(db: Session, now: datetime | None = None) -> int:
    """Meetings that ended with a call analysis (or a deal update) that day are marked held."""
    now = now or datetime.now(UTC)
    n = 0
    for ev in db.scalars(select(CalendarEvent).where(CalendarEvent.outcome.is_(None), CalendarEvent.contact_id.is_not(None),
                                                     CalendarEvent.starts_at < now)):
        start = _aware(ev.starts_at)
        analysed = db.scalar(select(Activity.id).where(Activity.contact_id == ev.contact_id,
                                                       Activity.type == "call_analyzed",
                                                       Activity.created_at >= start - timedelta(hours=2),
                                                       Activity.created_at <= start + timedelta(days=1)))
        if analysed:
            ev.outcome = "held"
            n += 1
    return n


def meeting_funnel(db: Session, days: int = 180) -> dict:
    since = datetime.now(UTC) - timedelta(days=days)
    events = list(db.scalars(select(CalendarEvent).where(CalendarEvent.contact_id.is_not(None),
                                                         CalendarEvent.starts_at >= since,
                                                         CalendarEvent.starts_at <= datetime.now(UTC))))
    first_agent: dict[int, str] = {}
    for a in db.scalars(select(Approval).where(Approval.status == "sent").order_by(Approval.sent_at)):
        if a.contact_id:
            first_agent.setdefault(a.contact_id, a.created_by_agent)

    def bucket() -> dict:
        return {"meetings": 0, "held": 0, "no_show": 0, "qualified": 0, "opportunity": 0, "won": 0}

    total, by_source, by_assistant = bucket(), defaultdict(bucket), defaultdict(bucket)
    for ev in events:
        c = db.get(Contact, ev.contact_id)
        opp = db.scalar(select(Opportunity).where(Opportunity.account_id == ev.account_id,
                                                  Opportunity.created_at >= _aware(ev.starts_at) - timedelta(days=1),
                                                  Opportunity.created_at <= _aware(ev.starts_at) + timedelta(days=45)))
        opp = opp or (db.get(Opportunity, ev.opportunity_id) if ev.opportunity_id else None)
        for b in (total, by_source[c.source or "unknown"], by_assistant[first_agent.get(c.id, "no AI email")]):
            b["meetings"] += 1
            b["held"] += ev.outcome in ("held", "qualified", "next_step_booked")
            b["no_show"] += ev.outcome == "no_show"
            b["qualified"] += ev.outcome in ("qualified", "next_step_booked") or bool(opp)
            b["opportunity"] += bool(opp)
            b["won"] += bool(opp and opp.stage == "closed_won")

    def rates(b: dict) -> dict:
        m = b["meetings"]
        return {**b, "show_rate": round(b["held"] / m * 100, 1) if m and (b["held"] + b["no_show"]) else None,
                "meeting_to_opportunity": round(b["opportunity"] / m * 100, 1) if m else None}
    return {"days": days, "total": rates(total), "by_source": {k: rates(v) for k, v in by_source.items()},
            "by_assistant": {k: rates(v) for k, v in by_assistant.items()},
            "unrecorded_outcomes": sum(1 for e in events if not e.outcome)}


def reminder_template(db: Session) -> Approval | None:
    return db.scalar(select(Approval).where(Approval.kind == "meeting_reminder", Approval.status == "approved")
                     .order_by(Approval.id.desc()))


def send_reminders(db: Session, now: datetime | None = None) -> int:
    """Day-before reminder to the customer, from the approved template, through every guardrail."""
    from . import approvals as approvals_svc
    from .nurture import merge

    tpl = reminder_template(db)
    if not tpl:
        return 0
    now = now or datetime.now(UTC)
    hours = int(settings.meeting_reminder_hours)
    sent = 0
    for ev in db.scalars(select(CalendarEvent).where(CalendarEvent.contact_id.is_not(None),
                                                     CalendarEvent.reminder_sent_at.is_(None),
                                                     CalendarEvent.starts_at > now,
                                                     CalendarEvent.starts_at <= now + timedelta(hours=hours))):
        c = db.get(Contact, ev.contact_id)
        when = f"{_aware(ev.starts_at):%A %d %B, %H:%M} UTC"
        fill = lambda t: merge(t, c).replace("{{meeting_time}}", when).replace("{{meeting_title}}", ev.title or "our meeting")
        draft = approvals_svc.queue(db, kind="email", contact=c, subject=fill(tpl.subject or ""), body=fill(tpl.body),
                                    created_by_agent="meeting_reminder", rationale=f"Reminder for {ev.title}")
        ev.reminder_sent_at = now
        if any(f["severity"] == "block" for f in draft.guardrail_flags):
            draft.status, draft.review_note = "rejected", "Reminder blocked by guardrails."
            continue
        draft.status, draft.reviewed_by, draft.decided_at = "approved", tpl.reviewed_by, now
        db.flush()
        try:
            approvals_svc.claim_and_send(db, draft, from_status="approved", commit=True)
            sent += draft.status == "sent"
        except approvals_svc.ApprovalError as exc:
            draft.status, draft.review_note = "rejected", f"Not sent: {exc}"
    return sent


# ------------------------------------------------------------------ 8. relationships and job changes

def relationships(db: Session, account_id: int) -> list[dict]:
    """Who on our team knows whom at this account, strongest first (emails and meetings, recent counts more)."""
    now = datetime.now(UTC)
    contacts = {c.id: c for c in db.scalars(select(Contact).where(Contact.account_id == account_id))}
    pairs: dict[tuple[str, int], dict] = {}

    def add(rep: str | None, cid: int | None, when, kind: str):
        if not rep or cid not in contacts:
            return
        p = pairs.setdefault((rep.lower(), cid), {"emails": 0, "meetings": 0, "last": None, "score": 0.0})
        p["emails" if kind == "email" else "meetings"] += 1
        when = _aware(when)
        age_days = (now - when).days if when else 365
        p["score"] += (3 if kind == "meeting" else 1) * (1.0 if age_days <= 90 else 0.5 if age_days <= 365 else 0.2)
        if when and (p["last"] is None or when > p["last"]):
            p["last"] = when

    for e in db.scalars(select(ContextEvent).where(ContextEvent.account_id == account_id, ContextEvent.source == "email",
                                                   ContextEvent.author.like("%@%"))):
        add(e.author, e.contact_id, e.occurred_at, "email")
    for a in db.scalars(select(Approval).where(Approval.status == "sent", Approval.contact_id.in_(list(contacts) or [0]))):
        add(contacts[a.contact_id].owner, a.contact_id, a.sent_at, "email")
    for ev in db.scalars(select(CalendarEvent).where(CalendarEvent.account_id == account_id)):
        conn = db.get(Connection, ev.connection_id) if ev.connection_id else None
        rep = conn.account_email if conn else None
        for att in ev.attendees or []:
            c = next((c for c in contacts.values() if (c.email or "").lower() == att.lower()), None)
            if c:
                add(rep, c.id, ev.starts_at, "meeting")
    out = [{"rep": rep, "contact_id": cid, "contact": contacts[cid].full_name, "title": contacts[cid].title,
            "emails": p["emails"], "meetings": p["meetings"], "last_contact": p["last"], "strength": round(p["score"], 1),
            "level": "strong" if p["score"] >= 6 else "warm" if p["score"] >= 2 else "light"}
           for (rep, cid), p in pairs.items()]
    return sorted(out, key=lambda r: -r["strength"])


def key_contacts(db: Session) -> list[Contact]:
    """People worth watching for job changes: on an open deal's account or a customer account."""
    from ..models import Contract

    accounts = {o.account_id for o in db.scalars(select(Opportunity).where(Opportunity.stage.in_(OPEN + ("closed_won",))))}
    accounts |= {k.account_id for k in db.scalars(select(Contract).where(Contract.status == "active"))}
    return list(db.scalars(select(Contact).where(Contact.account_id.in_(accounts or {0}), Contact.status != "disqualified")))


def check_job_changes(db: Session, limit: int = 25, now: datetime | None = None) -> int:
    """Quarterly re-check of key contacts through the enrichment provider; a new company means they moved."""
    from ..tools import dataproviders as dp
    from .growth import _signal_once

    if not dp.enrichment_provider():
        return 0
    now = now or datetime.now(UTC)
    due = [c for c in key_contacts(db)
           if not c.job_checked_at or now - _aware(c.job_checked_at) > timedelta(days=90)][:limit]
    moved = 0
    for c in due:
        c.job_checked_at = now
        acc = c.account
        try:
            e = dp.enrich_person(email=c.email, first_name=c.first_name, last_name=c.last_name, company=acc.name,
                                 domain=acc.domain)
        except Exception:
            continue
        new_domain = (e.company_domain or "").lower().removeprefix("www.") if e else ""
        old_domain = (acc.domain or (c.email or "").split("@")[-1]).lower().removeprefix("www.")
        if not new_domain or not old_domain or new_domain == old_domain:
            continue
        moved += 1
        _signal_once(db, acc.id, "champion_left", f"{c.full_name} has moved to {new_domain}",
                     detail=f"{c.full_name} ({c.title or 'contact'}) now appears at {new_domain}.", strength="high",
                     recommended_action=f"Find who replaces {c.first_name} at {acc.name}, and reconnect with "
                                        f"{c.first_name} at {new_domain}: a past contact is a warm door.")
        db.add(Activity(contact_id=c.id, type="job_change", detail=f"Now at {new_domain}"))
        if not db.scalar(select(ContactSuggestion.id).where(ContactSuggestion.name == c.full_name,
                                                            ContactSuggestion.new_company_domain == new_domain)):
            db.add(ContactSuggestion(name=c.full_name, new_company_domain=new_domain, source="job change",
                                     seen_with_contact_id=c.id,
                                     reason=f"Former contact at {acc.name}; knows us already. Title: {e.title or 'unknown'}"))
    return moved


def bounce_means_left(db: Session, contact: Contact) -> None:
    """A hard bounce at an account with an open deal often means the person left: raise it."""
    from .growth import _signal_once

    if db.scalar(select(Opportunity.id).where(Opportunity.account_id == contact.account_id, Opportunity.stage.in_(OPEN))):
        _signal_once(db, contact.account_id, "champion_left", f"{contact.full_name}'s email bounced: they may have left",
                     detail="Hard bounce on an account with an open deal.", strength="high",
                     recommended_action="Confirm with another contact and find their replacement.")
