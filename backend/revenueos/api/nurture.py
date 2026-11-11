"""Nurture flows, contact enrichment and email verification endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CampaignMember, Contact, NurtureFlow
from ..services import datahealth, nurture
from ..services.approvals import audit
from ..tools import dataproviders
from .workspace import caller

router = APIRouter(prefix="/api", tags=["nurture & data"])


def _actor(request: Request) -> str:
    user = caller(request).get("user")
    return (user.get("name") or user.get("email")) if isinstance(user, dict) else str(user or "anonymous")


class FlowIn(BaseModel):
    name: str
    campaign_id: int | None = None


@router.get("/nurture")
def list_flows(db: Session = Depends(get_db)):
    return [nurture.flow_stats(db, f) for f in db.scalars(select(NurtureFlow).order_by(NurtureFlow.id.desc()))]


@router.post("/nurture")
def create_flow(body: FlowIn, request: Request, db: Session = Depends(get_db)):
    f = NurtureFlow(name=body.name, campaign_id=body.campaign_id)
    db.add(f)
    db.flush()
    audit(db, _actor(request), "nurture.create", "nurture", f.id, name=f.name)
    db.commit()
    return nurture.flow_stats(db, f)


def _flow(db: Session, flow_id: int) -> NurtureFlow:
    f = db.get(NurtureFlow, flow_id)
    if not f:
        raise HTTPException(404, "Nurture flow not found")
    return f


class StepIn(BaseModel):
    subject: str
    body: str = Field(description="May use {{first_name}}, {{last_name}}, {{company}}, {{title}}.")
    delay_days: int = Field(default=7, ge=0, le=365)


@router.post("/nurture/{flow_id}/steps")
def add_step(flow_id: int, body: StepIn, request: Request, db: Session = Depends(get_db)):
    """Adds a step; its template waits in Approvals (filter: nurture emails) until a person approves it."""
    f = _flow(db, flow_id)
    step = nurture.add_step(db, f, subject=body.subject, body=body.body, delay_days=body.delay_days,
                            created_by=_actor(request))
    db.commit()
    return {"step": step.position, "approval_id": step.approval_id, "flow": nurture.flow_stats(db, f)}


class EnrollIn(BaseModel):
    contact_ids: list[int] = Field(default_factory=list)
    from_campaign: bool = False  # enrol every member of the flow's campaign


@router.post("/nurture/{flow_id}/enroll")
def enroll(flow_id: int, body: EnrollIn, db: Session = Depends(get_db)):
    f = _flow(db, flow_id)
    ids = list(body.contact_ids)
    if body.from_campaign and f.campaign_id:
        ids += [m.contact_id for m in db.scalars(select(CampaignMember).where(CampaignMember.campaign_id == f.campaign_id))]
    out = nurture.enroll(db, f, ids)
    db.commit()
    return {**out, "flow": nurture.flow_stats(db, f)}


@router.post("/nurture/{flow_id}/{action}")
def flow_action(flow_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    f = _flow(db, flow_id)
    if action not in ("pause", "resume"):
        raise HTTPException(404, "Unknown action")
    f.status = "paused" if action == "pause" else "active"
    audit(db, _actor(request), f"nurture.{action}", "nurture", f.id)
    db.commit()
    return nurture.flow_stats(db, f)


# ------------------------------------------------------------------ data providers

@router.get("/data/providers")
def providers():
    return {"enrichment": dataproviders.enrichment_provider(), "verification": dataproviders.verification_provider()}


@router.post("/leads/{contact_id}/enrich")
def enrich(contact_id: int, db: Session = Depends(get_db)):
    c = db.get(Contact, contact_id)
    if not c:
        raise HTTPException(404, "Contact not found")
    if not dataproviders.enrichment_provider():
        raise HTTPException(409, "No enrichment provider configured (Settings: enrichment provider + API key).")
    filled = datahealth.enrich_contact(db, c)
    db.commit()
    return {"contact_id": c.id, "filled": filled}


@router.post("/leads/{contact_id}/verify-email")
def verify(contact_id: int, db: Session = Depends(get_db)):
    c = db.get(Contact, contact_id)
    if not c:
        raise HTTPException(404, "Contact not found")
    c.email_checked_at = None  # force a fresh check
    status, detail = datahealth.email_check(db, c)
    db.commit()
    return {"contact_id": c.id, "email": c.email, "status": status, "detail": detail}
