"""Sequences (enrollments) and tasks: see where each contact is in their outreach, pause/resume/stop it,
and work the tasks the sequences and agents create (calls, LinkedIn messages, follow-ups)."""
from datetime import datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Approval, Contact, Enrollment, Task
from ..services import sequences
from ..services.approvals import audit
from .workspace import caller

router = APIRouter(prefix="/api", tags=["sequences"])


def _actor(request: Request) -> str:
    user = caller(request).get("user")
    return (user.get("name") or user.get("email")) if isinstance(user, dict) else str(user or "anonymous")


def enrollment_to_dict(db: Session, e: Enrollment) -> dict:
    steps = []
    for step, approvals, tasks in sequences._steps(db, e):
        for a in approvals:
            steps.append({"step": step, "type": a.kind, "approval_id": a.id, "status": a.status,
                          "subject": a.subject, "variant": a.variant, "send_after_days": a.send_after_days,
                          "scheduled_for": a.scheduled_for, "sent_at": a.sent_at})
        for t in tasks:
            steps.append({"step": step, "type": t.type, "task_id": t.id, "status": t.status, "title": t.title,
                          "send_after_days": t.offset_days, "scheduled_for": t.due_at, "sent_at": t.completed_at})
    c = e.contact
    return {"id": e.id, "status": e.status, "paused_reason": e.paused_reason, "resume_at": e.resume_at,
            "started_at": e.started_at, "next_due_at": e.next_due_at, "finished_at": e.finished_at,
            "created_at": e.created_at, "created_by_agent": e.created_by_agent,
            "contact": {"id": c.id, "name": c.full_name, "account": c.account.name, "timezone": c.timezone},
            "steps": steps}


@router.get("/enrollments")
def list_enrollments(contact_id: int | None = None, status: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Enrollment)
    if contact_id:
        stmt = stmt.where(Enrollment.contact_id == contact_id)
    if status:
        stmt = stmt.where(Enrollment.status == status)
    return [enrollment_to_dict(db, e) for e in db.scalars(stmt.order_by(Enrollment.id.desc()).limit(200))]


class EnrollmentAction(BaseModel):
    reason: str | None = None
    resume_at: datetime | None = None


@router.post("/enrollments/{enrollment_id}/{action}")
def enrollment_action(enrollment_id: int, action: str, body: EnrollmentAction, request: Request,
                      db: Session = Depends(get_db)):
    e = db.get(Enrollment, enrollment_id)
    if not e:
        raise HTTPException(404, "Enrollment not found")
    actor = _actor(request)
    if action == "pause":
        if e.status != "active":
            raise HTTPException(409, f"Sequence is {e.status}.")
        e.status, e.paused_reason, e.next_due_at = "paused", body.reason or f"Paused by {actor}", None
    elif action == "resume":
        if e.status != "paused":
            raise HTTPException(409, f"Sequence is {e.status}.")
        e.status, e.paused_reason, e.resume_at = "active", None, body.resume_at
    elif action == "stop":
        if e.status not in sequences.OPEN:
            raise HTTPException(409, f"Sequence is already {e.status}.")
        sequences.close(db, e, "stopped", body.reason or f"Stopped by {actor}")
    else:
        raise HTTPException(404, "Unknown action")
    audit(db, actor, f"enrollment.{action}", "enrollment", e.id, reason=body.reason)
    db.commit()
    return enrollment_to_dict(db, e)


class ContactTimezone(BaseModel):
    timezone: str | None


@router.put("/leads/{contact_id}/timezone")
def set_timezone(contact_id: int, body: ContactTimezone, db: Session = Depends(get_db)):
    c = db.get(Contact, contact_id)
    if not c:
        raise HTTPException(404, "Contact not found")
    if body.timezone:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(body.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, f"Unknown time zone '{body.timezone}'. Use an IANA name like Europe/Berlin.")
    c.timezone = body.timezone or None
    db.commit()
    return {"contact_id": c.id, "timezone": c.timezone}


# ------------------------------------------------------------------ tasks

def task_to_dict(t: Task) -> dict:
    c = t.contact
    return {"id": t.id, "type": t.type, "title": t.title, "notes": t.notes, "status": t.status, "owner": t.owner,
            "due_at": t.due_at, "created_by": t.created_by, "created_at": t.created_at,
            "completed_at": t.completed_at, "outcome": t.outcome, "enrollment_id": t.enrollment_id,
            "approval_id": t.approval_id, "opportunity_id": t.opportunity_id,
            "contact": {"id": c.id, "name": c.full_name, "account": c.account.name, "email": c.email,
                        "linkedin_url": c.linkedin_url} if c else None}


@router.get("/tasks")
def list_tasks(status: str = "open", due: str | None = None, owner: str | None = None,
               contact_id: int | None = None, db: Session = Depends(get_db)):
    """due=today: due by the end of today (UTC) including overdue; due=scheduled: has a due date at all.
    Sequence tasks get a due date only when the sequence reaches them."""
    stmt = select(Task).where(Task.status == status)
    if owner:
        stmt = stmt.where(Task.owner == owner)
    if contact_id:
        stmt = stmt.where(Task.contact_id == contact_id)
    if due == "today":
        end = datetime.combine(datetime.now(timezone.utc).date() + timedelta(days=1), time.min, tzinfo=timezone.utc)
        stmt = stmt.where(Task.due_at.is_not(None), Task.due_at < end)
    elif due == "scheduled":
        stmt = stmt.where(Task.due_at.is_not(None))
    return [task_to_dict(t) for t in db.scalars(stmt.order_by(Task.due_at.is_(None), Task.due_at, Task.id).limit(500))]


class TaskIn(BaseModel):
    title: str
    type: str = "other"
    notes: str | None = None
    contact_id: int | None = None
    opportunity_id: int | None = None
    due_at: datetime | None = None
    owner: str | None = None


@router.post("/tasks")
def create_task(body: TaskIn, request: Request, db: Session = Depends(get_db)):
    t = Task(**body.model_dump(), created_by=_actor(request))
    db.add(t)
    db.commit()
    return task_to_dict(t)


class TaskUpdate(BaseModel):
    status: str | None = None  # open|done|skipped
    outcome: str | None = None
    due_at: datetime | None = None
    owner: str | None = None


@router.patch("/tasks/{task_id}")
def update_task(task_id: int, body: TaskUpdate, request: Request, db: Session = Depends(get_db)):
    t = db.get(Task, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    if body.status and body.status not in ("open", "done", "skipped"):
        raise HTTPException(422, "status must be open, done or skipped")
    for field in ("outcome", "due_at", "owner"):
        if getattr(body, field) is not None:
            setattr(t, field, getattr(body, field))
    if body.status:
        t.status = body.status
        t.completed_at = datetime.now(timezone.utc) if body.status in ("done", "skipped") else None
        if body.status == "done" and t.approval_id:
            # A LinkedIn step the rep has now sent by hand: record it on the draft too.
            a = db.get(Approval, t.approval_id)
            if a and a.status == "approved":
                a.sent_at = t.completed_at
    audit(db, _actor(request), "task.update", "task", t.id, status=body.status)
    db.commit()
    return task_to_dict(t)
