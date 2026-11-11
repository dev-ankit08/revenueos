"""Website tracking (public), intent data, events and webinars, customers and renewals, social publishing, ad spend."""
import csv
import io
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Account, Approval, Campaign, CampaignSpend, Contact, Contract, Event, EventRegistration
from ..services import growth
from ..services.approvals import audit
from .leads import LeadIn, upsert_lead
from .webhooks import verify_secret
from .workspace import caller

router = APIRouter(prefix="/api", tags=["growth"])


def _actor(request: Request) -> str:
    user = caller(request).get("user")
    return (user.get("name") or user.get("email")) if isinstance(user, dict) else str(user or "anonymous")


# ------------------------------------------------------------------ website tracking (public)

@router.get("/public/t.js")
def tracker(request: Request, ws: str | None = None):
    """<script src="{PUBLIC_BASE_URL}/api/public/t.js?ws=<slug>" async></script> on every page of your site."""
    endpoint = f"{settings.public_base_url.rstrip('/')}/api/public/track" + (f"?ws={ws}" if ws else "")
    return Response(growth.tracker_script(endpoint), media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.post("/public/track")
async def track(request: Request, db: Session = Depends(get_db)):
    """Page view beacon (sent as text/plain so browsers send it without a CORS preflight)."""
    try:
        data = json.loads((await request.body())[:8000] or b"{}")
    except ValueError:
        return Response(status_code=204)
    url = str(data.get("u") or "")
    if not url.startswith("http"):
        return Response(status_code=204)
    growth.record_visit(db, visitor_id=str(data.get("v") or ""), url=url, referrer=data.get("r"),
                        ip=request.client.host if request.client else None)
    db.commit()
    return Response(status_code=204)


@router.get("/visits")
def visits(contact_id: int | None = None, account_id: int | None = None, db: Session = Depends(get_db)):
    from ..models import WebVisit

    stmt = select(WebVisit).order_by(WebVisit.occurred_at.desc()).limit(200)
    if contact_id:
        stmt = stmt.where(WebVisit.contact_id == contact_id)
    if account_id:
        stmt = stmt.where(WebVisit.account_id == account_id)
    return [{"path": v.path, "url": v.url, "utm_source": v.utm_source, "utm_campaign": v.utm_campaign,
             "contact_id": v.contact_id, "at": v.occurred_at} for v in db.scalars(stmt)]


# ------------------------------------------------------------------ intent data

class IntentIn(BaseModel):
    provider: str = "intent"
    topic: str
    score: float = Field(ge=0, le=100)
    domain: str | None = None
    company: str | None = None
    detail: str | None = None
    url: str | None = None


@router.post("/webhooks/intent", dependencies=[Depends(verify_secret)])
def intent_webhook(items: list[IntentIn] | IntentIn, db: Session = Depends(get_db)):
    """Topic surges from Bombora, G2, 6sense, etc. (one item or a list). X-Webhook-Secret header."""
    items = items if isinstance(items, list) else [items]
    created = sum(1 for i in items if growth.record_intent(db, **i.model_dump()))
    db.commit()
    return {"received": len(items), "signals": created}


@router.post("/intel/intent/import")
async def intent_import(file: UploadFile = File(...), provider: str = "csv", db: Session = Depends(get_db)):
    """CSV with columns domain or company, topic, score (0-100); optional detail, url."""
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    created = rows = 0
    for row in csv.DictReader(io.StringIO(text)):
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        try:
            score = float(r.get("score") or r.get("intent score") or 0)
        except ValueError:
            continue
        rows += 1
        if growth.record_intent(db, provider=provider, topic=r.get("topic") or "unspecified topic", score=score,
                                domain=r.get("domain") or r.get("website"), company=r.get("company"),
                                detail=r.get("detail"), url=r.get("url")):
            created += 1
    db.commit()
    return {"rows": rows, "signals": created}


# ------------------------------------------------------------------ events

class EventIn(BaseModel):
    name: str
    kind: str = "webinar"
    starts_at: datetime | None = None
    url: str | None = None
    external_id: str | None = None
    campaign_id: int | None = None
    attended_flow_id: int | None = None
    noshow_flow_id: int | None = None


def _event(e: Event) -> dict:
    counts: dict[str, int] = {}
    for r in e.registrations:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {"id": e.id, "name": e.name, "kind": e.kind, "starts_at": e.starts_at, "url": e.url, "status": e.status,
            "external_id": e.external_id, "campaign_id": e.campaign_id, "attended_flow_id": e.attended_flow_id,
            "noshow_flow_id": e.noshow_flow_id, "registrations": counts, "total": len(e.registrations)}


@router.get("/events")
def list_events(db: Session = Depends(get_db)):
    return [_event(e) for e in db.scalars(select(Event).order_by(Event.starts_at.desc().nullslast(), Event.id.desc()))]


@router.post("/events")
def create_event(body: EventIn, db: Session = Depends(get_db)):
    e = Event(**body.model_dump())
    db.add(e)
    db.commit()
    return _event(e)


@router.put("/events/{event_id}")
def update_event(event_id: int, body: EventIn, db: Session = Depends(get_db)):
    e = db.get(Event, event_id)
    if not e:
        raise HTTPException(404, "Event not found")
    for k, v in body.model_dump().items():
        setattr(e, k, v)
    db.commit()
    return _event(e)


class RegistrationIn(BaseModel):
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    title: str | None = None
    event_id: int | None = None
    external_event_id: str | None = None
    questions: str | None = None


def _register(db: Session, body: RegistrationIn, event: Event) -> EventRegistration:
    c = db.scalar(select(Contact).where(Contact.email.ilike(body.email)))
    if not c:
        c = upsert_lead(db, LeadIn(first_name=body.first_name or body.email.split("@")[0], last_name=body.last_name,
                                   company=body.company or body.email.split("@")[-1], title=body.title,
                                   email=body.email, source="event"))
    reg = growth.register(db, event, c)
    if body.questions:
        reg.questions = body.questions
    return reg


@router.post("/events/{event_id}/registrations")
def add_registration(event_id: int, body: RegistrationIn, db: Session = Depends(get_db)):
    e = db.get(Event, event_id)
    if not e:
        raise HTTPException(404, "Event not found")
    reg = _register(db, body, e)
    db.commit()
    return {"registration_id": reg.id, "contact_id": reg.contact_id, "event": _event(e)}


@router.post("/webhooks/event-registration", dependencies=[Depends(verify_secret)])
def registration_webhook(body: RegistrationIn, db: Session = Depends(get_db)):
    """From a webinar platform or form: event_id or external_event_id, plus the registrant's details."""
    e = db.get(Event, body.event_id) if body.event_id else (
        db.scalar(select(Event).where(Event.external_id == body.external_event_id)) if body.external_event_id else None)
    if not e:
        raise HTTPException(404, "Event not found")
    reg = _register(db, body, e)
    db.commit()
    return {"registration_id": reg.id, "contact_id": reg.contact_id}


class AttendanceIn(BaseModel):
    attendees: list[dict] = Field(description='[{"email": "...", "minutes": 42, "questions": "..."}]')


@router.post("/events/{event_id}/attendance")
def attendance(event_id: int, body: AttendanceIn, db: Session = Depends(get_db)):
    e = db.get(Event, event_id)
    if not e:
        raise HTTPException(404, "Event not found")
    by_email = {str(a.get("email", "")).lower(): a for a in body.attendees if a.get("email")}
    counts = growth.mark_attendance(db, e, {k: v.get("minutes") for k, v in by_email.items()})
    for r in e.registrations:
        q = by_email.get((r.contact.email or "").lower(), {}).get("questions")
        if q:
            r.questions = q
    db.commit()
    return {**counts, "event": _event(e)}


@router.post("/events/{event_id}/follow-up")
def event_follow_up(event_id: int, request: Request, db: Session = Depends(get_db)):
    e = db.get(Event, event_id)
    if not e:
        raise HTTPException(404, "Event not found")
    out = growth.follow_up(db, e)
    audit(db, _actor(request), "event.follow_up", "event", e.id, **out)
    db.commit()
    return out


# ------------------------------------------------------------------ customers and renewals

class ContractIn(BaseModel):
    account_id: int
    name: str
    amount: float | None = None
    currency: str = "USD"
    start_date: str | None = None
    end_date: str
    auto_renew: bool = False
    products: list[str] | None = None
    status: str = "active"


def _contract(k: Contract) -> dict:
    return {"id": k.id, "account_id": k.account_id, "account": k.account.name, "name": k.name, "amount": k.amount,
            "currency": k.currency, "start_date": k.start_date, "end_date": k.end_date, "auto_renew": k.auto_renew,
            "products": k.products or [], "status": k.status, "renewal_opportunity_id": k.renewal_opportunity_id}


@router.get("/customers")
def customers(db: Session = Depends(get_db)):
    """Every account with an active contract: health, churn risk, next renewal, upsell ideas."""
    out = []
    for acc_id in sorted({k.account_id for k in db.scalars(select(Contract).where(Contract.status == "active"))}):
        acc = db.get(Account, acc_id)
        ks = [k for k in db.scalars(select(Contract).where(Contract.account_id == acc_id, Contract.status == "active"))]
        out.append({"account_id": acc.id, "account": acc.name, "health": growth.health(db, acc),
                    "arr": round(sum(k.amount or 0 for k in ks), 2),
                    "next_renewal": min((k.end_date for k in ks if k.end_date), default=None),
                    "contracts": [_contract(k) for k in ks], "upsell_ideas": growth.upsell_ideas(db, acc)})
    return sorted(out, key=lambda r: r["health"]["score"])


@router.post("/contracts")
def create_contract(body: ContractIn, db: Session = Depends(get_db)):
    if not db.get(Account, body.account_id):
        raise HTTPException(404, "Account not found")
    k = Contract(**body.model_dump())
    db.add(k)
    db.commit()
    return _contract(k)


@router.put("/contracts/{contract_id}")
def update_contract(contract_id: int, body: ContractIn, db: Session = Depends(get_db)):
    k = db.get(Contract, contract_id)
    if not k:
        raise HTTPException(404, "Contract not found")
    for key, v in body.model_dump().items():
        setattr(k, key, v)
    db.commit()
    return _contract(k)


@router.post("/customers/renewals/run")
def run_renewals(db: Session = Depends(get_db)):
    n = growth.create_due_renewals(db)
    h = growth.refresh_health(db)
    db.commit()
    return {"renewal_deals_created": n, "churn_risk_signals": h}


# ------------------------------------------------------------------ social publishing and ad spend

@router.post("/approvals/{approval_id}/publish")
def publish(approval_id: int, request: Request, db: Session = Depends(get_db)):
    """Publish an approved LinkedIn/social post to the company page. A person clicks this; nothing auto-posts."""
    from ..tools import social

    a = db.get(Approval, approval_id)
    if not a or a.kind != "content" or a.content_type not in ("linkedin_post", "social_post"):
        raise HTTPException(404, "No approved social post with that id")
    if a.status != "approved":
        raise HTTPException(409, "Approve the post first.")
    if a.published_at:
        raise HTTPException(409, f"Already published: {a.published_url}")
    if not social.linkedin_configured():
        raise HTTPException(409, "LinkedIn publishing isn't set up (Settings: LinkedIn access token and author).")
    a.published_url = social.publish_linkedin(a.body)
    a.published_at = datetime.now(timezone.utc)
    audit(db, _actor(request), "content.publish", "approval", a.id, url=a.published_url)
    db.commit()
    return {"approval_id": a.id, "published_url": a.published_url}


class SpendIn(BaseModel):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    amount: float = Field(ge=0)
    channel: str = "other"
    impressions: int | None = None
    clicks: int | None = None


@router.post("/campaigns/{campaign_id}/spend")
def add_spend(campaign_id: int, body: SpendIn, db: Session = Depends(get_db)):
    if not db.get(Campaign, campaign_id):
        raise HTTPException(404, "Campaign not found")
    row = db.scalar(select(CampaignSpend).where(CampaignSpend.campaign_id == campaign_id, CampaignSpend.day == body.day,
                                                CampaignSpend.channel == body.channel))
    if row is None:
        row = CampaignSpend(campaign_id=campaign_id, **body.model_dump())
        db.add(row)
    else:
        for k, v in body.model_dump().items():
            setattr(row, k, v)
    db.commit()
    return {"campaign_id": campaign_id, "day": row.day, "channel": row.channel, "amount": row.amount}


@router.post("/campaigns/{campaign_id}/spend/import")
async def import_spend(campaign_id: int, file: UploadFile = File(...), channel: str = "other",
                       db: Session = Depends(get_db)):
    """CSV export from LinkedIn Campaign Manager or Google Ads: columns day/date, amount/spend/cost,
    optional impressions, clicks."""
    if not db.get(Campaign, campaign_id):
        raise HTTPException(404, "Campaign not found")
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        r = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        day = (r.get("day") or r.get("date") or r.get("start date (in utc)") or "")[:10]
        amount = r.get("amount") or r.get("spend") or r.get("cost") or r.get("total spent") or r.get("amount spent")
        try:
            value = float((amount or "").replace(",", "").replace("$", ""))
        except ValueError:
            continue
        if len(day) != 10:
            continue
        add_spend(campaign_id, SpendIn(day=day, amount=value, channel=channel,
                                       impressions=int(float(r["impressions"])) if r.get("impressions") else None,
                                       clicks=int(float(r["clicks"])) if r.get("clicks") else None), db)
        n += 1
    return {"rows": n}


@router.get("/tracking/snippet")
def snippet(request: Request):
    """The script tag to put on your website (includes this workspace)."""
    from ..db import current_workspace

    ws = ""
    ctx = current_workspace.get()
    if ctx is not None:
        from ..db import control_scope
        from ..models import Workspace

        with control_scope() as cdb:
            w = cdb.get(Workspace, ctx)
            ws = f"?ws={w.slug}" if w else ""
    return {"snippet": f'<script src="{settings.public_base_url.rstrip("/")}/api/public/t.js{ws}" async></script>'}
