"""Unified customer context: Slack, call transcripts, calendar, rep notes, and the account timeline.

The four inbound endpoints verify their own signatures (Slack signing secret, Zoom webhook
secret, X-Webhook-Secret) and are therefore exempt from the API token (see main.PUBLIC_PATHS).
"""
import asyncio
import json
import urllib.parse
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..agents import deals
from ..agents.orchestrator import run_copilot
from ..config import settings
from ..db import current_workspace, get_db, session_scope, use_workspace
from ..integrations import calendar, calls, oauth, slack, slack_bot, sync
from ..models import Account, Approval, CalendarEvent, Connection, Contact, ContextEvent, SlackChannel
from ..services import approvals as approvals_svc
from ..services import memory, secrets
from .webhooks import verify_secret

router = APIRouter(prefix="/api", tags=["integrations"])


# ------------------------------------------------------------------ Slack

@router.post("/integrations/slack/events")
async def slack_events(request: Request, db: Session = Depends(get_db)):
    if not settings.slack_signing_secret:
        raise HTTPException(503, "Slack is not configured (SLACK_SIGNING_SECRET).")
    body = await request.body()
    if not slack.verify_signature(settings.slack_signing_secret, request.headers.get("x-slack-request-timestamp"),
                                  body, request.headers.get("x-slack-signature")):
        raise HTTPException(401, "Bad Slack signature")
    payload = json.loads(body)
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    if payload.get("type") == "event_callback":
        result = slack.ingest_message(db, payload.get("event", {}))
        db.commit()
        return {"ok": True, "result": result}
    return {"ok": True}


async def _verified_slack_body(request: Request) -> bytes:
    if not settings.slack_signing_secret:
        raise HTTPException(503, "Slack is not configured (SLACK_SIGNING_SECRET).")
    body = await request.body()
    if not slack.verify_signature(settings.slack_signing_secret, request.headers.get("x-slack-request-timestamp"),
                                  body, request.headers.get("x-slack-signature")):
        raise HTTPException(401, "Bad Slack signature")
    return body


@router.post("/integrations/slack/interactions")
async def slack_interactions(request: Request, db: Session = Depends(get_db)):
    """Approve / Reject buttons on drafts posted to the approvals channel."""
    body = await _verified_slack_body(request)
    payload = json.loads(urllib.parse.parse_qs(body.decode())["payload"][0])
    action = (payload.get("actions") or [{}])[0]
    if action.get("action_id") not in ("approve", "reject"):
        return {"ok": True}
    user = payload.get("user", {})
    reviewer = f"{user.get('name') or user.get('username') or user.get('id')} (Slack)"
    a = db.get(Approval, int(action["value"]))
    if not a:
        text = "That draft no longer exists."
    else:
        try:
            if action["action_id"] == "approve":
                approvals_svc.approve_and_send(db, a, reviewer)
                text = f"✅ {reviewer} approved: {a.subject or a.title} ({a.status})"
            else:
                approvals_svc.reject(db, a, reviewer, "Rejected in Slack")
                text = f"❌ {reviewer} rejected: {a.subject or a.title}"
            db.commit()
        except approvals_svc.ApprovalError as exc:
            db.commit()  # keep refreshed guardrail flags
            text = f"Couldn't do that: {exc} Open RevenueOS to edit the draft."
    if payload.get("response_url"):
        await asyncio.to_thread(slack_bot.respond, payload["response_url"], text, True)
    return {"ok": True}


@router.post("/integrations/slack/commands")
async def slack_command(request: Request):
    """/revenueos <request>: the Copilot answers in Slack (privately to the person who asked)."""
    body = await _verified_slack_body(request)
    form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode()).items()}
    text, response_url = form.get("text", "").strip(), form.get("response_url")
    if not text:
        return {"response_type": "ephemeral",
                "text": "Ask me anything about your pipeline, e.g. `/revenueos brief me on Siemens before my 3pm`."}

    async def work(run_id: int) -> str:
        answer = await run_copilot(f"(Asked in Slack by {form.get('user_name', 'a rep')}) {text}", run_id)
        if response_url:
            await asyncio.to_thread(slack_bot.respond, response_url, answer or "Done.")
        return answer
    jobs.start("orchestrator", text, work, model=settings.model_orchestrator)
    return {"response_type": "ephemeral", "text": f"On it: _{text}_. I'll reply here when done."}


class ChannelIn(BaseModel):
    channel_id: str = Field(min_length=2)
    name: str | None = None
    account_id: int | None = None
    shared: bool = False


@router.get("/integrations/slack/channels")
def list_channels(db: Session = Depends(get_db)):
    return [{"id": c.id, "channel_id": c.channel_id, "name": c.name, "account_id": c.account_id,
             "account": db.get(Account, c.account_id).name if c.account_id else None,
             "shared": c.shared, "enabled": c.enabled} for c in db.scalars(select(SlackChannel))]


@router.post("/integrations/slack/channels")
def opt_in_channel(body: ChannelIn, db: Session = Depends(get_db)):
    ch = db.scalar(select(SlackChannel).where(SlackChannel.channel_id == body.channel_id)) or SlackChannel(
        channel_id=body.channel_id)
    ch.name, ch.account_id, ch.shared, ch.enabled = body.name, body.account_id, body.shared, True
    db.add(ch)
    db.commit()
    return {"channel_id": ch.channel_id, "enabled": True}


@router.delete("/integrations/slack/channels/{channel_id}")
def opt_out_channel(channel_id: str, db: Session = Depends(get_db)):
    ch = db.scalar(select(SlackChannel).where(SlackChannel.channel_id == channel_id))
    if not ch:
        raise HTTPException(404, "Channel not registered")
    ch.enabled = False
    db.commit()
    return {"channel_id": channel_id, "enabled": False}


# ------------------------------------------------------------------ call transcripts

@router.post("/integrations/zoom")
async def zoom_webhook(request: Request):
    if not settings.zoom_webhook_secret:
        raise HTTPException(503, "Zoom is not configured (ZOOM_WEBHOOK_SECRET).")
    body = await request.body()
    payload = json.loads(body)
    if payload.get("event") == "endpoint.url_validation":  # Zoom's ownership check, unsigned by design
        return calls.zoom_url_validation(settings.zoom_webhook_secret, payload["payload"]["plainToken"])
    if not calls.zoom_signature_ok(settings.zoom_webhook_secret, request.headers.get("x-zm-request-timestamp"),
                                   body, request.headers.get("x-zm-signature")):
        raise HTTPException(401, "Bad Zoom signature")
    if payload.get("event") != "recording.transcript_completed":
        return {"ok": True, "ignored": payload.get("event")}

    async def work(run_id: int) -> str:
        return json.dumps(await calls.process_zoom_event(payload, parent_run_id=run_id), default=str)
    return {"ok": True, "run_id": jobs.start("job:zoom_transcript", "Zoom transcript", work)}


class TranscriptIn(BaseModel):
    source: str = Field(description="teams | meet | gong | upload | ...")
    external_id: str
    transcript: str = Field(min_length=50)
    title: str | None = None
    attendees: list[str] = []
    meeting_url: str | None = None
    started_at: str | None = None


@router.post("/integrations/transcripts", dependencies=[Depends(verify_secret)])
def transcript(body: TranscriptIn):
    """Generic: Teams (Graph / Power Automate), Google Meet, Gong, Chorus or manual upload."""
    async def work(run_id: int) -> str:
        r = await calls.process_transcript(
            source=body.source, external_id=f"{body.source}:{body.external_id}", transcript=body.transcript,
            title=body.title, attendees=body.attendees, meeting_ref=body.meeting_url,
            started_at=body.started_at, parent_run_id=run_id)
        return json.dumps(r, default=str)
    return {"ok": True, "run_id": jobs.start("job:transcript", f"{body.source} transcript", work)}


# ------------------------------------------------------------------ calendar

class CalendarEventIn(BaseModel):
    id: str
    title: str
    start: str
    end: str | None = None
    attendees: list[str] = []
    meeting_url: str | None = None


@router.post("/integrations/calendar-events", dependencies=[Depends(verify_secret)])
def calendar_events(events: list[CalendarEventIn], db: Session = Depends(get_db)):
    r = calendar.upsert_events(db, [e.model_dump() for e in events])
    db.commit()
    return r


@router.get("/integrations/meetings")
def upcoming_meetings(db: Session = Depends(get_db)):
    rows = db.scalars(select(CalendarEvent).where(CalendarEvent.contact_id.isnot(None))
                      .order_by(CalendarEvent.starts_at.desc()).limit(50))
    return [{"id": e.id, "title": e.title, "starts_at": e.starts_at, "contact_id": e.contact_id,
             "prep_status": e.prep_status, "prep_document_id": e.prep_document_id} for e in rows]


# ------------------------------------------------------------------ timeline, notes, unmatched

def event_to_dict(e: ContextEvent) -> dict:
    return {"id": e.id, "source": e.source, "occurred_at": e.occurred_at, "author": e.author, "title": e.title,
            "summary": e.summary, "facts": e.facts, "internal": e.internal, "account_id": e.account_id,
            "contact_id": e.contact_id, "processed": e.processed}


@router.get("/accounts/{account_id}/timeline")
def account_timeline(account_id: int, db: Session = Depends(get_db)):
    if not db.get(Account, account_id):
        raise HTTPException(404, "Account not found")
    return [event_to_dict(e) for e in memory.timeline(db, account_id, limit=100)]


class NoteIn(BaseModel):
    text: str = Field(min_length=3)
    author: str | None = None
    contact_id: int | None = None
    internal: bool = True


@router.post("/accounts/{account_id}/notes")
def add_note(account_id: int, body: NoteIn, db: Session = Depends(get_db)):
    if not db.get(Account, account_id):
        raise HTTPException(404, "Account not found")
    ev = memory.record(db, source="note", source_ref=f"note:{uuid.uuid4().hex}", account_id=account_id,
                       contact_id=body.contact_id, author=body.author, title="Rep note", raw_text=body.text,
                       summary=body.text[:1000], internal=body.internal, processed=True)
    db.commit()
    return event_to_dict(ev)


@router.get("/integrations/unmatched")
def unmatched(db: Session = Depends(get_db)):
    rows = db.scalars(select(ContextEvent).where(ContextEvent.account_id.is_(None))
                      .order_by(ContextEvent.occurred_at.desc()).limit(100))
    return [event_to_dict(e) | {"preview": (e.raw_text or "")[:300]} for e in rows]


class AssignIn(BaseModel):
    account_id: int
    contact_id: int | None = None


@router.post("/integrations/events/{event_id}/assign")
def assign(event_id: int, body: AssignIn, db: Session = Depends(get_db)):
    """A person links an unmatched message or call to its account; unmatched calls are then analysed."""
    e = db.get(ContextEvent, event_id)
    if not e:
        raise HTTPException(404, "Event not found")
    if not db.get(Account, body.account_id):
        raise HTTPException(404, "Account not found")
    if e.source == "call":
        if not body.contact_id or not db.get(Contact, body.contact_id):
            raise HTTPException(422, "A call needs the contact it was with.")
        transcript_text, ref, title, occurred = e.raw_text or "", e.source_ref, e.title, e.occurred_at
        db.delete(e)  # replaced by the analysed call event
        db.commit()
        contact_id = body.contact_id

        async def work(run_id: int) -> str:
            r = await deals.analyze_call(contact_id, transcript_text, parent_run_id=run_id, source_ref=ref,
                                         occurred_at=occurred, title=title)
            return f"{r['stage']} / {r['health']}: {r['summary']}"
        return {"run_id": jobs.start("job:call_analysis", f"Assigned call {event_id}", work)}
    e.account_id, e.contact_id = body.account_id, body.contact_id
    db.commit()
    return event_to_dict(e)


@router.get("/integrations/status")
def status(request: Request, db: Session = Depends(get_db)):
    base = str(request.base_url).rstrip("/")
    ws = current_workspace.get()
    slug = None
    if ws is not None:
        from ..db import control_scope
        from ..models import Workspace

        with control_scope() as cdb:
            slug = cdb.get(Workspace, ws).slug
    q = f"?ws={slug}" if slug else ""  # webhooks name their workspace
    return {
        "google": {"configured": bool(settings.google_client_id and settings.google_client_secret),
                   "redirect_uri": oauth.redirect_uri("google")},
        "microsoft": {"configured": bool(settings.ms_client_id and settings.ms_client_secret),
                      "redirect_uri": oauth.redirect_uri("microsoft")},
        "gong": {"configured": bool(settings.gong_access_key and settings.gong_access_secret)},
        "slack_bot": {"configured": slack_bot.enabled(), "channel": settings.slack_approvals_channel,
                      "interactions_url": f"{base}/api/integrations/slack/interactions{q}",
                      "commands_url": f"{base}/api/integrations/slack/commands{q}"},
        "secret_key_set": bool(settings.secret_key),
        "crm": {"provider": settings.crm,
                "last_pull": next((c.cursors.get("pull") for c in db.scalars(select(Connection).where(
                    Connection.provider == f"crm:{settings.crm}")) if c.cursors), None)},
        "slack": {"configured": bool(settings.slack_signing_secret), "events_url": f"{base}/api/integrations/slack/events{q}",
                  "channels": len(list(db.scalars(select(SlackChannel.id).where(SlackChannel.enabled.is_(True)))))},
        "zoom": {"configured": bool(settings.zoom_webhook_secret), "webhook_url": f"{base}/api/integrations/zoom{q}"},
        "transcripts": {"url": f"{base}/api/integrations/transcripts{q}",
                        "auth": "X-Webhook-Secret header" if settings.webhook_secret else "none (set WEBHOOK_SECRET)"},
        "calendar": {"url": f"{base}/api/integrations/calendar-events{q}",
                     "auth": "X-Webhook-Secret header" if settings.webhook_secret else "none (set WEBHOOK_SECRET)"},
        "retention_days": settings.context_retention_days,
        "personalisation_threshold": settings.personalisation_threshold,
    }


# ------------------------------------------------------------------ connected accounts (Google, Microsoft, Gong)

@router.get("/integrations/oauth/{provider}/start")
def oauth_start(provider: str):
    """Returns the consent URL; the browser goes there and Google/Microsoft redirect back to the callback."""
    if provider not in oauth.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    try:
        return {"url": oauth.authorize_url(provider)}
    except (RuntimeError, secrets.SecretKeyMissing) as exc:
        raise HTTPException(503, str(exc))


@router.get("/integrations/oauth/{provider}/callback")
def oauth_callback(provider: str, code: str | None = None, state: str = "", error: str | None = None):
    target = f"{settings.app_base_url}/integrations"
    if error or not code:
        return RedirectResponse(f"{target}?error={urllib.parse.quote(error or 'cancelled')}")
    ws = oauth.check_state(provider, state) if provider in oauth.PROVIDERS else False
    if ws is False:
        raise HTTPException(400, "Invalid or expired sign-in state. Start the connection again.")
    with use_workspace(ws), session_scope() as db:  # store the connection in the workspace that started it
        conn = oauth.exchange_code(db, provider, code)
        email = conn.account_email
    return RedirectResponse(f"{target}?connected={urllib.parse.quote(email or provider)}")


@router.get("/integrations/connections")
def list_connections(db: Session = Depends(get_db)):
    return [{"id": c.id, "provider": c.provider, "account_email": c.account_email, "status": c.status,
             "last_error": c.last_error, "cursors": c.cursors, "created_at": c.created_at}
            for c in db.scalars(select(Connection).where(Connection.status != "disabled"))]


@router.delete("/integrations/connections/{conn_id}")
def disconnect(conn_id: int, db: Session = Depends(get_db)):
    c = db.get(Connection, conn_id)
    if not c:
        raise HTTPException(404, "Connection not found")
    c.status, c.refresh_token_enc, c.access_token_enc = "disabled", None, None  # tokens wiped
    db.commit()
    return {"id": conn_id, "status": "disabled"}


@router.post("/integrations/sync")
def sync_now():
    async def work(run_id: int) -> str:
        return await sync.run_sync(parent_run_id=run_id)
    return {"run_id": jobs.start("job:sync", "Sync mail, calendars and call recordings", work)}
