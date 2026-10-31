"""Phase 3 endpoints: campaigns, content, ABM, competitor intel, signals, lead sourcing."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import jobs
from ..agents import committee, intel, marketing
from ..db import get_db
from ..models import Account, Approval, Campaign, CampaignMember, Contact, SalesDocument, Signal

router = APIRouter(prefix="/api", tags=["marketing"])


# ------------------------------------------------------------------ campaigns

class CampaignIn(BaseModel):
    goal: str = Field(min_length=10)
    budget: float | None = None
    start_date: str | None = None
    end_date: str | None = None


@router.post("/campaigns")
def create_campaign(body: CampaignIn):
    async def work(run_id: int) -> str:
        r = await marketing.plan_campaign(body.goal, body.budget, body.start_date, body.end_date, parent_run_id=run_id)
        return f"campaign:{r['campaign_id']} {r['name']} ({r['members']} contacts, {r['content_items']} content items)"
    return {"run_id": jobs.start("job:campaign_plan", body.goal[:200], work)}


def campaign_to_dict(db: Session, c: Campaign, full: bool = False) -> dict:
    content = dict(db.execute(select(Approval.status, func.count()).where(Approval.campaign_id == c.id)
                              .group_by(Approval.status)).all())
    members = db.scalar(select(func.count()).select_from(CampaignMember).where(CampaignMember.campaign_id == c.id))
    out = {"id": c.id, "name": c.name, "goal": c.goal, "status": c.status, "budget": c.budget,
           "start_date": c.start_date, "end_date": c.end_date, "sf_id": c.sf_id, "crm_id": c.crm_id, "members": members,
           "content": content, "created_at": c.created_at}
    if full:
        out["plan"] = c.plan
        out["member_list"] = [
            {"contact_id": ct.id, "name": ct.full_name, "title": ct.title, "account": ct.account.name,
             "status": ct.status, "tier": ct.score_tier}
            for ct in db.scalars(select(Contact).join(CampaignMember, CampaignMember.contact_id == Contact.id)
                                 .where(CampaignMember.campaign_id == c.id).limit(200))
        ]
    return out


@router.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db)):
    return [campaign_to_dict(db, c) for c in db.scalars(select(Campaign).order_by(Campaign.id.desc()))]


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    return campaign_to_dict(db, c, full=True)


class CampaignPatch(BaseModel):
    status: Literal["draft", "active", "completed"]


@router.patch("/campaigns/{campaign_id}")
def update_campaign(campaign_id: int, body: CampaignPatch, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    c.status = body.status
    db.commit()
    return campaign_to_dict(db, c)


class ContentGenIn(BaseModel):
    item_indexes: list[int] | None = None


@router.post("/campaigns/{campaign_id}/content")
def generate_campaign_content(campaign_id: int, body: ContentGenIn, db: Session = Depends(get_db)):
    if not db.get(Campaign, campaign_id):
        raise HTTPException(404, "Campaign not found")

    async def work(run_id: int) -> str:
        return await marketing.generate_campaign_content(campaign_id, body.item_indexes, parent_run_id=run_id)
    return {"run_id": jobs.start("job:campaign_content", f"Content for campaign {campaign_id}", work)}


# ------------------------------------------------------------------ content & ABM

class ContentIn(BaseModel):
    content_type: str
    brief: str = Field(min_length=5)
    campaign_id: int | None = None
    account_id: int | None = None


@router.post("/content")
def create_content(body: ContentIn):
    if body.content_type not in marketing.CONTENT_GUIDE:
        raise HTTPException(422, f"content_type must be one of {list(marketing.CONTENT_GUIDE)}")

    async def work(run_id: int) -> str:
        return await marketing.create_content(body.content_type, body.brief, body.campaign_id, body.account_id,
                                              parent_run_id=run_id)
    return {"run_id": jobs.start("job:content", f"{body.content_type}: {body.brief[:150]}", work)}


class AbmIn(BaseModel):
    instructions: str | None = None


@router.post("/accounts/{account_id}/abm")
def abm(account_id: int, body: AbmIn, db: Session = Depends(get_db)):
    if not db.get(Account, account_id):
        raise HTTPException(404, "Account not found")

    async def work(run_id: int) -> str:
        return await marketing.abm_pack(account_id, body.instructions, parent_run_id=run_id)
    return {"run_id": jobs.start("job:abm", f"ABM pack for account {account_id}", work)}


# ------------------------------------------------------------------ intel

class BattlecardIn(BaseModel):
    competitor: str = Field(min_length=2)


@router.post("/intel/battlecard")
def battlecard(body: BattlecardIn):
    async def work(run_id: int) -> str:
        r = await intel.competitor_battlecard(body.competitor, parent_run_id=run_id)
        return f"document:{r['document_id']} {r['competitor']}"
    return {"run_id": jobs.start("job:battlecard", body.competitor, work)}


@router.get("/intel/battlecards")
def battlecards(db: Session = Depends(get_db)):
    docs = db.scalars(select(SalesDocument).where(SalesDocument.kind == "battlecard").order_by(SalesDocument.id.desc()))
    latest: dict[str, SalesDocument] = {}
    for d in docs:  # newest per competitor
        latest.setdefault(d.title, d)
    return [{"id": d.id, "title": d.title, "content": d.content, "created_at": d.created_at} for d in latest.values()]


class SignalScanIn(BaseModel):
    account_ids: list[int] | None = None
    days: int = Field(default=90, ge=7, le=365)
    limit: int = Field(default=10, ge=1, le=25)


@router.post("/intel/signals")
def scan_signals(body: SignalScanIn):
    async def work(run_id: int) -> str:
        r = await intel.detect_signals(body.account_ids, body.days, body.limit, parent_run_id=run_id)
        return f"{r.get('signals', 0)} new signals across {r.get('accounts_scanned', 0)} accounts"
    return {"run_id": jobs.start("job:signals", "Signal scan", work)}


@router.get("/signals")
def list_signals(status: str = "new", db: Session = Depends(get_db)):
    order = {"high": 0, "medium": 1, "low": 2}
    rows = list(db.scalars(select(Signal).where(Signal.status == status).order_by(Signal.id.desc()).limit(300)))
    rows.sort(key=lambda s: order.get(s.strength, 3))
    out = []
    for s in rows:
        contacts = [{"contact_id": c.id, "name": c.full_name, "title": c.title} for c in s.account.contacts][:5]
        out.append({"id": s.id, "account_id": s.account_id, "account": s.account.name, "type": s.type,
                    "headline": s.headline, "detail": s.detail, "url": s.url, "event_date": s.event_date,
                    "strength": s.strength, "recommended_action": s.recommended_action, "status": s.status,
                    "contacts": contacts})
    return out


class SignalPatch(BaseModel):
    status: Literal["new", "actioned", "dismissed"]


@router.patch("/signals/{signal_id}")
def update_signal(signal_id: int, body: SignalPatch, db: Session = Depends(get_db)):
    s = db.get(Signal, signal_id)
    if not s:
        raise HTTPException(404, "Signal not found")
    s.status = body.status
    db.commit()
    return {"id": s.id, "status": s.status}


class SourceIn(BaseModel):
    request: str = Field(min_length=5)
    count: int = Field(default=10, ge=1, le=25)


@router.post("/intel/source")
def source(body: SourceIn):
    async def work(run_id: int) -> str:
        r = await intel.source_accounts(body.request, body.count, parent_run_id=run_id)
        return f"Added {len(r['added'])} accounts ({r['skipped_duplicates']} duplicates skipped): {', '.join(r['added'])}"
    return {"run_id": jobs.start("job:source", body.request[:200], work)}


@router.get("/accounts")
def list_accounts(source: str | None = None, db: Session = Depends(get_db)):
    stmt = select(Account).order_by(Account.id.desc()).limit(500)
    if source:
        stmt = stmt.where(Account.source == source)
    return [{"id": a.id, "name": a.name, "domain": a.domain, "industry": a.industry, "employees": a.employees,
             "country": a.country, "source": a.source, "fit_reason": a.fit_reason, "notes": a.notes,
             "contacts": len(a.contacts), "has_brief": bool(a.briefs)} for a in db.scalars(stmt)]


class CommitteeIn(BaseModel):
    instructions: str | None = None
    research_people: bool = True


@router.post("/accounts/{account_id}/committee")
def committee_outreach(account_id: int, body: CommitteeIn, db: Session = Depends(get_db)):
    if not db.get(Account, account_id):
        raise HTTPException(404, "Account not found")

    async def work(run_id: int) -> str:
        r = await committee.committee_outreach(account_id, body.instructions, body.research_people, parent_run_id=run_id)
        missing = f" Roles still to find: {', '.join(r['missing_roles'])}." if r["missing_roles"] else ""
        return f"Sequenced {', '.join(r['sequenced'])} (document:{r['document_id']}).{missing}"
    return {"run_id": jobs.start("job:committee", f"Committee outreach for account {account_id}", work)}
