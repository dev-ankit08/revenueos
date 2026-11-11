"""Inbound events: website form fills, replies, unsubscribes.

Point your website form, email provider or Salesforce Flow at these endpoints.
"""
import hmac
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..agents import workflows
from ..config import settings
from ..db import get_db
from ..models import Activity, Contact
from ..services import email_events as email_events_svc
from ..services import inbound as inbound_replies
from .leads import LeadIn, upsert_lead

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def verify_secret(x_webhook_secret: str | None = Header(default=None)) -> None:
    if settings.webhook_secret and not hmac.compare_digest(x_webhook_secret or "", settings.webhook_secret):
        raise HTTPException(401, "Bad webhook secret")


class InboundForm(BaseModel):
    first_name: str
    last_name: str
    email: str
    company: str
    title: str | None = None
    website: str | None = None
    message: str | None = None
    country: str | None = None


@router.post("/inbound", dependencies=[Depends(verify_secret)])
def inbound(form: InboundForm, db: Session = Depends(get_db)):
    """Form fill -> lead -> score -> research -> drafted reply in the approval queue, in minutes."""
    domain = (form.website or form.email.split("@")[-1]).replace("https://", "").replace("http://", "").strip("/")
    contact = upsert_lead(db, LeadIn(
        first_name=form.first_name, last_name=form.last_name, company=form.company, title=form.title,
        email=form.email, domain=domain, country=form.country, source="inbound",
        inbound_message=form.message,
    ))
    db.add(Activity(contact_id=contact.id, type="inbound", detail=form.message or "Form submitted"))
    db.commit()
    contact_id = contact.id

    async def work(run_id: int) -> str:
        return await workflows.inbound_lead(contact_id, parent_run_id=run_id)
    return {"contact_id": contact_id, "run_id": jobs.start("job:inbound", f"Inbound lead {contact_id}", work)}


class ReplyIn(BaseModel):
    from_email: str
    text: str
    subject: str | None = None
    triage: bool = True  # set false to skip the AI step (e.g. in bulk back-fills)


@router.post("/reply", dependencies=[Depends(verify_secret)])
def reply(r: ReplyIn, db: Session = Depends(get_db)):
    """A prospect replied: pause their sequence at once, then let Reply Triage decide what happens next.

    Explicit opt-outs are honoured immediately without waiting for the AI.
    """
    contact = db.scalar(select(Contact).where(Contact.email.ilike(r.from_email)))
    if not contact:
        return {"matched": False}
    return inbound_replies.handle_reply(db, contact, r.text, r.subject, triage=r.triage)


class UnsubscribeIn(BaseModel):
    email: str


@router.post("/unsubscribe", dependencies=[Depends(verify_secret)])
def unsubscribe(u: UnsubscribeIn, db: Session = Depends(get_db)):
    inbound_replies.suppress(db, u.email, "unsubscribe")
    db.commit()
    return {"suppressed": u.email.lower()}


# ------------------------------------------------------------------ sending-service delivery events

def _secret_ok(header: str | None, token: str | None) -> bool:
    """Shared secret in the X-Webhook-Secret header or ?token= (for providers that cannot set headers)."""
    if not settings.webhook_secret:
        return True
    return any(v and hmac.compare_digest(v, settings.webhook_secret) for v in (header, token))


@router.post("/email/{provider}")
async def email_events(provider: str, request: Request, db: Session = Depends(get_db),
                       token: str | None = None, x_webhook_secret: str | None = Header(default=None)):
    """Bounces, complaints, unsubscribes, opens and clicks from SendGrid or Postmark.

    SendGrid: Settings > Mail Settings > Event Webhook, URL .../api/webhooks/email/sendgrid?ws=<slug>; enable
    the signed webhook and set SENDGRID_WEBHOOK_PUBLIC_KEY (or append &token=<WEBHOOK_SECRET>).
    Postmark: Servers > Webhooks, URL .../api/webhooks/email/postmark?ws=<slug>, custom header
    X-Webhook-Secret: <WEBHOOK_SECRET>.
    """
    raw = await request.body()
    if provider == "sendgrid":
        key = settings.sendgrid_webhook_public_key
        if key:
            sig = request.headers.get("X-Twilio-Email-Event-Webhook-Signature", "")
            ts = request.headers.get("X-Twilio-Email-Event-Webhook-Timestamp", "")
            if not email_events_svc.verify_sendgrid(key, raw, sig, ts):
                raise HTTPException(401, "Bad SendGrid signature")
        elif not _secret_ok(x_webhook_secret, token):
            raise HTTPException(401, "Bad webhook secret")
        events = email_events_svc.normalise_sendgrid(json.loads(raw or b"[]"))
    elif provider == "postmark":
        if not _secret_ok(x_webhook_secret, token):
            raise HTTPException(401, "Bad webhook secret")
        events = email_events_svc.normalise_postmark(json.loads(raw or b"{}"))
    else:
        raise HTTPException(404, "Unknown email provider")
    counts = email_events_svc.record(db, provider, events)
    db.commit()
    return {"received": len(events), "processed": counts}
