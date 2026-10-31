from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Approval
from ..services import approvals as svc

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


def approval_to_dict(a: Approval) -> dict:
    c = a.contact
    return {
        "id": a.id, "kind": a.kind, "status": a.status, "subject": a.subject, "body": a.body,
        "content_type": a.content_type, "title": a.title, "campaign_id": a.campaign_id, "account_id": a.account_id,
        "sequence_step": a.sequence_step, "send_after_days": a.send_after_days, "variant": a.variant,
        "rationale": a.rationale, "guardrail_flags": a.guardrail_flags,
        "personalisation_score": a.personalisation_score, "claims": a.claims, "personalisation_notes": a.personalisation_notes, "created_by_agent": a.created_by_agent,
        "run_id": a.run_id, "reviewed_by": a.reviewed_by, "review_note": a.review_note,
        "created_at": a.created_at, "decided_at": a.decided_at, "sent_at": a.sent_at,
        "contact": {"id": c.id, "name": c.full_name, "title": c.title, "email": c.email,
                    "account": c.account.name, "score": c.score, "tier": c.score_tier} if c else None,
    }


@router.get("")
def list_approvals(status: str = "pending", kind: str | None = None, campaign_id: int | None = None,
                   db: Session = Depends(get_db)):
    stmt = select(Approval).where(Approval.status == status)
    if kind:
        stmt = stmt.where(Approval.kind == kind)
    if campaign_id:
        stmt = stmt.where(Approval.campaign_id == campaign_id)
    stmt = stmt.order_by(
        Approval.contact_id, Approval.sequence_step, Approval.variant
    ).limit(500)
    return [approval_to_dict(a) for a in db.scalars(stmt)]


class Decision(BaseModel):
    reviewer: str
    subject: str | None = None
    body: str | None = None
    note: str | None = None


def _get(db: Session, approval_id: int) -> Approval:
    a = db.get(Approval, approval_id)
    if not a:
        raise HTTPException(404, "Approval not found")
    return a


@router.post("/{approval_id}/approve")
def approve(approval_id: int, d: Decision, db: Session = Depends(get_db)):
    a = _get(db, approval_id)
    try:
        svc.approve_and_send(db, a, d.reviewer, d.subject, d.body)
    except svc.ApprovalError as exc:
        db.commit()  # keep refreshed guardrail flags
        raise HTTPException(409, str(exc))
    db.commit()
    return approval_to_dict(a)


@router.post("/{approval_id}/reject")
def reject(approval_id: int, d: Decision, db: Session = Depends(get_db)):
    a = _get(db, approval_id)
    try:
        svc.reject(db, a, d.reviewer, d.note)
    except svc.ApprovalError as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return approval_to_dict(a)
