"""Sequence engine: moves each contact through their outreach steps at the right time.

An Enrollment ties together a contact's steps: Approvals (email, LinkedIn) and Tasks (calls, manual steps),
each with a `sequence_step` and a delay in days after the FIRST touch was sent. Every scheduler tick:

- a step waits until the previous one is done (sent, or its task scheduled) and its approval is given;
- it is due at first-touch time + delay, never before the previous step, never before an out-of-office
  return date, and always inside business hours in the contact's time zone (SEND_WINDOW_*);
- approved emails are then claimed and sent (services/approvals.py is still the only sender);
- LinkedIn steps become a task for the rep on their due date (LinkedIn is always sent by a person);
- a reply pauses the enrollment at once; Reply Triage resumes it (out of office) or stops it.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Approval, Contact, Enrollment, Task

OPEN = ("active", "paused")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _flag(value) -> bool:
    return value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")


def contact_tz(contact: Contact | None) -> ZoneInfo:
    for name in ((contact.timezone if contact else None), settings.default_timezone, "UTC"):
        if name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


def next_send_time(earliest: datetime, tz: ZoneInfo) -> datetime:
    """The first moment at or after `earliest` inside the send window, in the contact's time zone (UTC out)."""
    start_h, end_h = int(settings.send_window_start_hour), int(settings.send_window_end_hour)
    weekdays_only = _flag(settings.send_weekdays_only)
    local = _aware(earliest).astimezone(tz)
    for _ in range(14):
        if weekdays_only and local.weekday() >= 5:
            local = (local + timedelta(days=1)).replace(hour=start_h, minute=0, second=0, microsecond=0)
            continue
        if local.hour < start_h:
            local = local.replace(hour=start_h, minute=0, second=0, microsecond=0)
        elif local.hour >= end_h:
            local = (local + timedelta(days=1)).replace(hour=start_h, minute=0, second=0, microsecond=0)
            continue
        return local.astimezone(timezone.utc)
    return _aware(earliest)


# ------------------------------------------------------------------ enrolment

def open_enrollment(db: Session, contact_id: int) -> Enrollment | None:
    return db.scalar(select(Enrollment).where(Enrollment.contact_id == contact_id, Enrollment.status.in_(OPEN))
                     .order_by(Enrollment.id.desc()))


def attach(db: Session, approval: Approval, created_by: str | None = None) -> Enrollment:
    """Put a sequence step into the contact's open enrollment, starting one if needed.

    A new step 1 for someone whose current sequence has already started means a fresh sequence:
    the old one is closed as completed and a new enrollment begins.
    """
    e = open_enrollment(db, approval.contact_id)
    if e and approval.sequence_step == 1 and e.started_at is not None:
        close(db, e, "completed", "Replaced by a new sequence.")
        e = None
    if e is None:
        e = Enrollment(contact_id=approval.contact_id, status="active", created_by_agent=created_by)
        db.add(e)
        db.flush()
    approval.enrollment_id = e.id
    return e


def add_task_step(db: Session, contact: Contact, *, sequence_step: int, offset_days: int, title: str,
                  notes: str | None = None, task_type: str = "call", created_by: str = "sequence") -> Task:
    """A call or manual step inside a sequence. Its due date is set when the sequence reaches it."""
    e = open_enrollment(db, contact.id)
    if e is None:
        e = Enrollment(contact_id=contact.id, status="active", created_by_agent=created_by)
        db.add(e)
        db.flush()
    task = Task(type=task_type, title=title, notes=notes, contact_id=contact.id, enrollment_id=e.id,
                sequence_step=sequence_step, offset_days=offset_days, owner=contact.owner, created_by=created_by)
    db.add(task)
    db.flush()
    return task


def pause_contact(db: Session, contact_id: int, reason: str) -> int:
    n = 0
    for e in db.scalars(select(Enrollment).where(Enrollment.contact_id == contact_id, Enrollment.status == "active")):
        e.status, e.paused_reason, e.next_due_at = "paused", reason, None
        n += 1
    return n


def resume_contact(db: Session, contact_id: int, resume_at: datetime | None = None, note: str | None = None) -> int:
    n = 0
    for e in db.scalars(select(Enrollment).where(Enrollment.contact_id == contact_id, Enrollment.status == "paused")):
        e.status, e.paused_reason = "active", note
        e.resume_at = _aware(resume_at) if resume_at else None
        n += 1
    return n


def stop_contact(db: Session, contact_id: int, reason: str) -> int:
    n = 0
    for e in db.scalars(select(Enrollment).where(Enrollment.contact_id == contact_id, Enrollment.status.in_(OPEN))):
        close(db, e, "stopped", reason)
        n += 1
    return n


def close(db: Session, e: Enrollment, status: str, reason: str) -> None:
    """Finish an enrollment; its unsent drafts are cancelled and its open tasks skipped."""
    e.status, e.paused_reason = status, reason
    e.finished_at, e.next_due_at = datetime.now(timezone.utc), None
    for a in db.scalars(select(Approval).where(Approval.enrollment_id == e.id,
                                               Approval.status.in_(["pending", "approved"]))):
        a.status, a.review_note = "rejected", f"Sequence {status}: {reason}"
    for t in db.scalars(select(Task).where(Task.enrollment_id == e.id, Task.status == "open")):
        t.status, t.outcome = "skipped", f"Sequence {status}: {reason}"


# ------------------------------------------------------------------ engine

def advance_all(db: Session, now: datetime | None = None) -> int:
    """Move every active enrollment forward. Returns the number of emails sent."""
    now = _aware(now) or datetime.now(timezone.utc)
    sent = 0
    for e in db.scalars(select(Enrollment).where(Enrollment.status == "active").order_by(Enrollment.id)).all():
        sent += advance(db, e, now)
    return sent


def _steps(db: Session, e: Enrollment) -> list[tuple[int, list[Approval], list[Task]]]:
    grouped: dict[int, tuple[list, list]] = defaultdict(lambda: ([], []))
    for a in db.scalars(select(Approval).where(Approval.enrollment_id == e.id)):
        grouped[a.sequence_step][0].append(a)
    for t in db.scalars(select(Task).where(Task.enrollment_id == e.id, Task.approval_id.is_(None))):
        grouped[t.sequence_step or 0][1].append(t)
    return [(step, *grouped[step]) for step in sorted(grouped)]


def advance(db: Session, e: Enrollment, now: datetime) -> int:
    from . import approvals as approvals_svc  # approvals imports this module

    contact = e.contact
    tz = contact_tz(contact)
    start = _aware(e.started_at)  # when the first touch went out
    prev = start
    floor = _aware(e.resume_at)  # out of office: nothing before they are back
    sent = 0

    def due_after(offset_days: int) -> datetime:
        base = now if start is None else max(start + timedelta(days=offset_days or 0), prev)
        if floor and base < floor:
            base = floor
        return next_send_time(base, tz)

    for step, approvals, tasks in _steps(db, e):
        live = [a for a in approvals if a.status != "rejected"]
        if not live and not tasks:
            continue  # every draft for this step was rejected: the step is skipped
        if not live:  # a call or manual step
            task = tasks[0]
            if task.due_at is None:
                if start is None:
                    e.next_due_at = None
                    return sent  # waits for the first touch
                task.due_at = due_after(task.offset_days)
            prev = max(prev, _aware(task.due_at)) if prev else _aware(task.due_at)
            continue

        if any(a.status == "pending" for a in live):
            e.next_due_at = None
            return sent  # waiting for a person to approve this step
        if any(a.status in ("failed", "sending") for a in live):
            e.next_due_at = None
            return sent  # in flight, or a failed send a person should look at
        done = [a for a in live if a.status == "sent"]
        if done:
            t = _aware(done[0].sent_at)
            start = start or t
            prev = t
            continue

        a = live[0]  # approved, waiting for its moment
        due = due_after(a.send_after_days)
        if a.kind != "email":
            # LinkedIn messages are sent by the rep: give them a task on the due date.
            task = db.scalar(select(Task).where(Task.approval_id == a.id))
            if task is None:
                task = Task(type="linkedin", title=f"Send LinkedIn message to {contact.full_name}", notes=a.body,
                            contact_id=contact.id, enrollment_id=e.id, approval_id=a.id, sequence_step=step,
                            due_at=due, owner=contact.owner, created_by="sequence")
                db.add(task)
            prev = _aware(task.due_at)
            start = start or prev
            continue

        a.scheduled_for = due
        if now < due:
            e.next_due_at = due
            return sent
        try:
            approvals_svc.claim_and_send(db, a, from_status="approved", commit=True)
        except approvals_svc.ApprovalError as exc:  # daily cap etc.: try again next tick
            a.review_note = str(exc)
            e.next_due_at = None
            return sent
        if a.status != "sent":
            e.next_due_at = None
            return sent
        sent += 1
        t = _aware(a.sent_at)
        start = start or t
        prev = t

    e.status, e.finished_at, e.next_due_at = "completed", now, None
    return sent
