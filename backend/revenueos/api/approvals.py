from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Approval
from ..services import approvals as svc
from .workspace import caller

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


def _hours_since(dt) -> float | None:
    from datetime import datetime, timezone

    if dt is None:
        return None
    dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1)


def approval_to_dict(a: Approval) -> dict:
    c = a.contact
    return {
        "id": a.id, "kind": a.kind, "status": a.status, "subject": a.subject, "body": a.body,
        "content_type": a.content_type, "title": a.title, "campaign_id": a.campaign_id, "account_id": a.account_id,
        "sequence_step": a.sequence_step, "send_after_days": a.send_after_days, "variant": a.variant,
        "rationale": a.rationale, "guardrail_flags": a.guardrail_flags,
        "personalisation_score": a.personalisation_score, "claims": a.claims, "personalisation_notes": a.personalisation_notes, "created_by_agent": a.created_by_agent,
        "run_id": a.run_id, "reviewed_by": a.reviewed_by, "review_note": a.review_note,
        "reject_reason": a.reject_reason, "edit_pct": a.edit_pct,
        "published_url": a.published_url, "published_at": a.published_at,
        "ai_subject": a.ai_subject, "ai_body": a.ai_body,
        "created_at": a.created_at, "decided_at": a.decided_at, "sent_at": a.sent_at,
        "waiting_hours": _hours_since(a.created_at) if a.status == "pending" else None,
        "contact": {"id": c.id, "name": c.full_name, "title": c.title, "email": c.email,
                    "account": c.account.name, "score": c.score, "tier": c.score_tier} if c else None,
    }


@router.get("")
def list_approvals(status: str = "pending", kind: str | None = None, campaign_id: int | None = None,
                   order: str = "contact", owner: str | None = None, limit: int = 200, offset: int = 0,
                   db: Session = Depends(get_db)):
    """order: contact (grouped by person and step) | oldest (longest-waiting first) | newest."""
    from ..models import Contact

    stmt = select(Approval).where(Approval.status == status)
    if kind:
        stmt = stmt.where(Approval.kind == kind)
    if campaign_id:
        stmt = stmt.where(Approval.campaign_id == campaign_id)
    if owner:
        stmt = stmt.join(Contact, Contact.id == Approval.contact_id).where(Contact.owner == owner)
    if order == "oldest":
        stmt = stmt.order_by(Approval.created_at, Approval.id)
    elif order == "newest":
        stmt = stmt.order_by(Approval.created_at.desc(), Approval.id.desc())
    else:
        stmt = stmt.order_by(Approval.contact_id, Approval.sequence_step, Approval.variant)
    return [approval_to_dict(a) for a in db.scalars(stmt.offset(max(0, offset)).limit(min(max(1, limit), 500)))]


class Decision(BaseModel):
    # Ignored when someone is signed in: the reviewer is always the signed-in user.
    reviewer: str | None = None
    subject: str | None = None
    body: str | None = None
    note: str | None = None
    reason: str | None = None  # reject only; one of svc.REJECT_REASONS


def reviewer_name(request: Request, d: Decision) -> str:
    """Who is deciding. Taken from the session so it cannot be typed in as someone else."""
    user = caller(request).get("user")
    if isinstance(user, dict):
        return user.get("name") or user.get("email") or "unknown"
    if user == "service":
        return f"service ({d.reviewer})" if d.reviewer else "service"
    # Open single-company install (no accounts yet): fall back to the typed name.
    return (d.reviewer or "").strip() or "anonymous"


class Bulk(BaseModel):
    ids: list[int]
    action: str  # approve | reject
    reason: str | None = None
    note: str | None = None
    reviewer: str | None = None


@router.post("/bulk")
def bulk(body: Bulk, request: Request, db: Session = Depends(get_db)):
    """Approve or reject many drafts at once, unedited. Blocked drafts are skipped and reported."""
    if body.action not in ("approve", "reject"):
        raise HTTPException(422, "action must be approve or reject")
    if len(body.ids) > 200:
        raise HTTPException(422, "At most 200 drafts at a time.")
    who = reviewer_name(request, Decision(reviewer=body.reviewer))
    done, skipped = [], []
    for aid in body.ids:
        a = db.get(Approval, aid)
        if not a:
            skipped.append({"id": aid, "why": "not found"})
            continue
        try:
            if body.action == "approve":
                svc.approve_and_send(db, a, who)
            else:
                svc.reject(db, a, who, body.note, body.reason)
            db.commit()
            done.append({"id": aid, "status": a.status})
        except svc.ApprovalError as exc:
            db.commit()  # keep refreshed guardrail flags
            skipped.append({"id": aid, "why": str(exc)})
    return {"done": done, "skipped": skipped}


@router.get("/reject-reasons")
def reject_reasons():
    return [{"code": k, "label": v} for k, v in svc.REJECT_REASONS.items()]


def _get(db: Session, approval_id: int) -> Approval:
    a = db.get(Approval, approval_id)
    if not a:
        raise HTTPException(404, "Approval not found")
    return a


@router.post("/{approval_id}/approve")
def approve(approval_id: int, d: Decision, request: Request, db: Session = Depends(get_db)):
    a = _get(db, approval_id)
    try:
        svc.approve_and_send(db, a, reviewer_name(request, d), d.subject, d.body)
    except svc.ApprovalError as exc:
        db.commit()  # keep refreshed guardrail flags
        raise HTTPException(409, str(exc))
    db.commit()
    return approval_to_dict(a)


@router.post("/{approval_id}/reject")
def reject(approval_id: int, d: Decision, request: Request, db: Session = Depends(get_db)):
    a = _get(db, approval_id)
    try:
        svc.reject(db, a, reviewer_name(request, d), d.note, d.reason)
    except svc.ApprovalError as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return approval_to_dict(a)
