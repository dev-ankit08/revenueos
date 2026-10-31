"""Calendar → automatic meeting prep.

Events arrive at /api/integrations/calendar-events (from a Google Apps Script, Power Automate
flow, or a native sync later). Meetings with a known customer attendee are matched to the
contact, appear on the account timeline, and get a Meeting Prep brief before they start.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import deals
from ..config import settings
from ..db import session_scope
from ..models import CalendarEvent
from ..services import memory


def _dt(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def detect_platform(url: str | None) -> str | None:
    u = (url or "").lower()
    return ("zoom" if "zoom.us" in u else "teams" if "teams.microsoft.com" in u
            else "meet" if "meet.google.com" in u else ("other" if u else None))


def upsert_events(db: Session, events: list[dict]) -> dict:
    """events: [{id, title, start, end?, attendees: [emails], meeting_url?}]"""
    matched = 0
    for e in events:
        attendees = [a.lower() for a in e.get("attendees", []) if a]
        ev = db.scalar(select(CalendarEvent).where(CalendarEvent.external_id == str(e["id"])))
        if not ev:
            ev = CalendarEvent(external_id=str(e["id"]))
            db.add(ev)
        ev.title, ev.attendees, ev.meeting_url = e.get("title") or "Meeting", attendees, e.get("meeting_url")
        ev.platform = e.get("platform") or detect_platform(ev.meeting_url)
        ev.connection_id = e.get("connection_id") or ev.connection_id
        new_start = _dt(e["start"])
        if ev.starts_at and _dt(ev.starts_at) != new_start and ev.prep_status == "done":
            ev.prep_status = "pending"  # rescheduled: prep again closer to the new time
        ev.starts_at, ev.ends_at = new_start, _dt(e.get("end"))
        contacts = memory.contacts_by_emails(db, attendees)
        if contacts:
            ev.contact_id, ev.account_id = contacts[0].id, contacts[0].account_id
            if ev.platform in ("teams", "meet") and ev.transcript_status is None:
                ev.transcript_status = "pending"  # fetched from Teams/Meet after the meeting ends
            matched += 1
            db.flush()
            memory.record(db, source="calendar", source_ref=f"calendar:{ev.external_id}",
                          account_id=ev.account_id, contact_id=ev.contact_id, occurred_at=ev.starts_at,
                          title=ev.title, processed=True,
                          summary=f"Meeting scheduled: {ev.title} with {', '.join(c.full_name for c in contacts)}")
        elif ev.prep_status == "pending":
            ev.prep_status = "skipped"  # internal meeting: no customer attendee
    return {"received": len(events), "customer_meetings": matched}


def due_for_prep(db: Session, now: datetime | None = None) -> list[int]:
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(hours=settings.meeting_prep_hours_before)
    return list(db.scalars(select(CalendarEvent.id).where(
        CalendarEvent.contact_id.isnot(None), CalendarEvent.prep_status == "pending",
        CalendarEvent.starts_at > now, CalendarEvent.starts_at <= horizon)))


async def run_due_preps(parent_run_id: int | None = None) -> str:
    with session_scope() as db:
        ids = due_for_prep(db)
        for ev in db.scalars(select(CalendarEvent).where(CalendarEvent.id.in_(ids))):
            ev.prep_status = "running"  # so the next scheduler tick doesn't start it again
    done = 0
    for event_id in ids:
        with session_scope() as db:
            ev = db.get(CalendarEvent, event_id)
            args = (ev.contact_id, ev.title, ", ".join(ev.attendees), f"{_dt(ev.starts_at):%A %d %B %H:%M} UTC")
        try:
            doc_id = await deals.prep_meeting(*args, parent_run_id=parent_run_id)
            status = "done"
        except Exception:
            doc_id, status = None, "failed"
        with session_scope() as db:
            ev = db.get(CalendarEvent, event_id)
            ev.prep_status, ev.prep_document_id = status, doc_id
        done += status == "done"
    return f"Prepared {done} of {len(ids)} upcoming customer meetings."
