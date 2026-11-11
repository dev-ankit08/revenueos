"""Phase 2 endpoints: meeting prep, call analysis, proposals, documents, deals, CRM hygiene."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..agents import deals as deal_agents
from ..agents import hygiene as hygiene_agent
from ..agents import proposals
from ..config import settings
from ..db import get_db
from ..models import Contact, Opportunity, SalesDocument
from ..services import hygiene
from ..tools.crm import get_crm

router = APIRouter(prefix="/api", tags=["deals"])


def _require_contact(db: Session, contact_id: int) -> None:
    if not db.get(Contact, contact_id):
        raise HTTPException(404, "Lead not found")


# ------------------------------------------------------------------ agent actions on a lead

class PrepIn(BaseModel):
    goal: str | None = None
    attendees: str | None = None
    when: str | None = None


@router.post("/leads/{contact_id}/meeting-prep")
def meeting_prep(contact_id: int, body: PrepIn, db: Session = Depends(get_db)):
    _require_contact(db, contact_id)

    async def work(run_id: int) -> str:
        doc_id = await deal_agents.prep_meeting(contact_id, body.goal, body.attendees, body.when, parent_run_id=run_id)
        return f"document:{doc_id}"
    return {"run_id": jobs.start("job:meeting_prep", f"Meeting prep for contact {contact_id}", work)}


class CallIn(BaseModel):
    transcript: str = Field(min_length=50, description="Call transcript or detailed notes.")


@router.post("/leads/{contact_id}/call-analysis")
def call_analysis(contact_id: int, body: CallIn, db: Session = Depends(get_db)):
    _require_contact(db, contact_id)

    async def work(run_id: int) -> str:
        r = await deal_agents.analyze_call(contact_id, body.transcript, parent_run_id=run_id)
        return f"{r['stage']} / {r['health']}: {r['summary']} (document:{r['document_id']}, approval:{r['approval_id']})"
    return {"run_id": jobs.start("job:call_analysis", f"Analyse call with contact {contact_id}", work)}


class ProposalIn(BaseModel):
    instructions: str | None = None


@router.post("/leads/{contact_id}/proposal")
def proposal(contact_id: int, body: ProposalIn, db: Session = Depends(get_db)):
    _require_contact(db, contact_id)

    async def work(run_id: int) -> str:
        r = await proposals.build_proposal(contact_id, body.instructions, parent_run_id=run_id)
        return f"{r['title']} (document:{r['proposal_document_id']}, document:{r['deck_document_id']})"
    return {"run_id": jobs.start("job:proposal", f"Proposal for contact {contact_id}", work)}


# ------------------------------------------------------------------ documents

def doc_to_dict(d: SalesDocument, full: bool = True) -> dict:
    out = {"id": d.id, "kind": d.kind, "title": d.title, "file_name": d.file_name,
           "account_id": d.account_id, "contact_id": d.contact_id, "opportunity_id": d.opportunity_id,
           "created_at": d.created_at}
    if full:
        out |= {"content": d.content, "data": d.data}
    return out


@router.get("/documents")
def list_documents(contact_id: int | None = None, account_id: int | None = None, kind: str | None = None,
                   limit: int = 50, db: Session = Depends(get_db)):
    stmt = select(SalesDocument).order_by(SalesDocument.id.desc()).limit(min(limit, 200))
    if contact_id:
        c = db.get(Contact, contact_id)
        # Include account-level docs (e.g. proposals) alongside the contact's own.
        stmt = stmt.where((SalesDocument.contact_id == contact_id)
                          | (SalesDocument.account_id == (c.account_id if c else -1)))
    if account_id:
        stmt = stmt.where(SalesDocument.account_id == account_id)
    if kind:
        stmt = stmt.where(SalesDocument.kind == kind)
    return [doc_to_dict(d, full=False) for d in db.scalars(stmt)]


@router.get("/documents/{doc_id}")
def get_document(doc_id: int, db: Session = Depends(get_db)):
    d = db.get(SalesDocument, doc_id)
    if not d:
        raise HTTPException(404, "Document not found")
    return doc_to_dict(d)


@router.get("/documents/{doc_id}/file")
def download(doc_id: int, db: Session = Depends(get_db)):
    d = db.get(SalesDocument, doc_id)
    if not d or not d.file_name:
        raise HTTPException(404, "No file for this document")
    path = (settings.generated_dir / d.file_name).resolve()
    if settings.generated_dir.resolve() not in path.parents or not path.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(path, filename=d.file_name)


# ------------------------------------------------------------------ deals

def opp_to_dict(o: Opportunity) -> dict:
    return {"id": o.id, "name": o.name, "stage": o.stage, "amount": o.amount, "close_date": o.close_date,
            "health": o.health, "win_probability": o.win_probability, "forecast_category": o.forecast_category,
            "qualification": o.qualification, "next_steps": o.next_steps,
            "risks": o.risks, "account": o.account.name, "account_id": o.account_id,
            "primary_contact_id": o.primary_contact_id, "sf_id": o.sf_id, "crm_id": o.crm_id,
            "last_activity_at": o.last_activity_at, "owner": o.owner, "close_reason": o.close_reason,
            "close_reason_detail": o.close_reason_detail, "competitors": o.competitors or []}


@router.get("/deals")
def list_deals(db: Session = Depends(get_db)):
    return [opp_to_dict(o) for o in db.scalars(select(Opportunity).order_by(Opportunity.last_activity_at.desc()))]


class DealPatch(BaseModel):
    stage: Literal["discovery", "qualification", "solution", "proposal", "negotiation",
                   "closed_won", "closed_lost"] | None = None
    amount: float | None = None
    close_date: str | None = None
    owner: str | None = None
    close_reason: str | None = None  # required when closing; see GET /api/deals/close-reasons
    close_reason_detail: str | None = None
    competitors: list[str] | None = None


@router.get("/deals/close-reasons")
def close_reasons():
    from ..services.salesmgmt import CLOSE_REASONS

    return CLOSE_REASONS


@router.patch("/deals/{opp_id}")
def update_deal(opp_id: int, body: DealPatch, db: Session = Depends(get_db)):
    from ..services import salesmgmt

    o = db.get(Opportunity, opp_id)
    if not o:
        raise HTTPException(404, "Deal not found")
    if body.stage in ("closed_won", "closed_lost"):
        reason = body.close_reason or o.close_reason
        valid = salesmgmt.CLOSE_REASONS["won" if body.stage == "closed_won" else "lost"]
        if reason not in valid:
            raise HTTPException(422, f"Say why it was {'won' if body.stage == 'closed_won' else 'lost'}: "
                                     f"close_reason must be one of {', '.join(valid)}.")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(o, field, value)
    if body.stage == "closed_won":
        from ..services import growth

        db.flush()
        growth.contract_from_won_deal(db, o)  # recurring line items -> a contract with a renewal date
        if o.deal_type == "renewal":
            from ..models import Contract

            for k in db.scalars(select(Contract).where(Contract.renewal_opportunity_id == o.id)):
                k.status = "renewed"
    get_crm().upsert_opportunity(o)
    db.commit()
    return opp_to_dict(o)


# ------------------------------------------------------------------ hygiene

class HygieneIn(BaseModel):
    enrich: bool = True


@router.post("/hygiene/run")
def run_hygiene(body: HygieneIn):
    async def work(run_id: int) -> str:
        r = await hygiene_agent.run_hygiene(body.enrich, parent_run_id=run_id)
        return f"document:{r['document_id']} {r['issue_counts']}"
    return {"run_id": jobs.start("job:crm_hygiene", "CRM hygiene", work)}


@router.get("/hygiene/latest")
def latest_hygiene(db: Session = Depends(get_db)):
    d = db.scalar(select(SalesDocument).where(SalesDocument.kind == "hygiene_report")
                  .order_by(SalesDocument.id.desc()))
    if not d:  # nothing yet: compute live (free, rule-based)
        return {"id": None, "title": "Live check", "data": hygiene.find_issues(db),
                "content": hygiene.to_markdown(hygiene.find_issues(db))}
    return doc_to_dict(d)


class MergeIn(BaseModel):
    keep_id: int
    merge_id: int


@router.post("/hygiene/merge-contacts")
def merge(body: MergeIn, db: Session = Depends(get_db)):
    try:
        keep = hygiene.merge_contacts(db, body.keep_id, body.merge_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    db.commit()
    return {"kept": keep.id, "merged": body.merge_id}
