"""Phase B endpoints: quick updates, contact suggestions, forecast accuracy and slip risk, meeting outcomes,
reminders and the meeting funnel, relationships."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..db import get_db
from ..models import Approval, CalendarEvent, Contact, ContactSuggestion, Opportunity
from ..services import approvals as approvals_svc
from ..services import pipeline_intel as svc
from .workspace import caller

router = APIRouter(prefix="/api", tags=["pipeline"])


def _actor(request: Request) -> str:
    user = caller(request).get("user")
    return (user.get("name") or user.get("email")) if isinstance(user, dict) else str(user or "anonymous")


# ------------------------------------------------------------------ quick update (speak or type two lines)

class QuickUpdate(BaseModel):
    note: str = Field(min_length=5, max_length=5000)


@router.post("/leads/{contact_id}/quick-update")
def quick_update(contact_id: int, body: QuickUpdate, db: Session = Depends(get_db)):
    """After a meeting or call: two lines of notes update the deal (stage, amount, next steps, tasks) and the CRM.
    No follow-up email is drafted (use Analyse a call for that)."""
    from ..agents import deals

    if not db.get(Contact, contact_id):
        raise HTTPException(404, "Contact not found")

    async def work(run_id: int) -> str:
        r = await deals.analyze_call(contact_id, f"Rep's quick update after a meeting or call:\n{body.note}",
                                     parent_run_id=run_id, title="Quick update", follow_up=False)
        return f"Deal updated: {r['stage']}, {r['health']}. {r['summary'][:200]}"
    return {"run_id": jobs.start("job:quick_update", f"Quick update for contact {contact_id}", work)}


# ------------------------------------------------------------------ contact suggestions

@router.get("/suggestions")
def suggestions(status: str = "pending", db: Session = Depends(get_db)):
    from ..models import Account

    out = []
    for s in db.scalars(select(ContactSuggestion).where(ContactSuggestion.status == status)
                        .order_by(ContactSuggestion.id.desc()).limit(200)):
        acc = db.get(Account, s.account_id) if s.account_id else None
        seen = db.get(Contact, s.seen_with_contact_id) if s.seen_with_contact_id else None
        out.append({"id": s.id, "email": s.email, "name": s.name, "account": acc.name if acc else None,
                    "new_company_domain": s.new_company_domain, "source": s.source, "reason": s.reason,
                    "seen_with": seen.full_name if seen else None, "created_at": s.created_at})
    return out


@router.post("/suggestions/{sid}/{action}")
def suggestion_action(sid: int, action: str, request: Request, db: Session = Depends(get_db)):
    s = db.get(ContactSuggestion, sid)
    if not s or s.status != "pending":
        raise HTTPException(404, "Suggestion not found")
    if action == "dismiss":
        s.status = "dismissed"
        db.commit()
        return {"id": sid, "status": "dismissed"}
    if action != "add":
        raise HTTPException(404, "Unknown action")
    if not s.account_id or not s.email:
        raise HTTPException(409, "This person changed company: add them as a lead at their new company from Pipeline.")
    c = svc.accept_suggestion(db, s)
    approvals_svc.audit(db, _actor(request), "suggestion.add", "contact", c.id, email=c.email)
    db.commit()
    return {"id": sid, "status": "added", "contact_id": c.id}


# ------------------------------------------------------------------ forecasting

@router.get("/forecast/accuracy")
def forecast_accuracy(period: str, db: Session = Depends(get_db)):
    r = svc.accuracy(db, period)
    if not r:
        raise HTTPException(404, f"No forecast snapshots for {period} yet (one is taken every week).")
    return r


@router.get("/forecast/snapshots")
def snapshots(db: Session = Depends(get_db)):
    from ..models import ForecastSnapshot

    rows = db.scalars(select(ForecastSnapshot).order_by(ForecastSnapshot.taken_at.desc()).limit(60))
    return [{"id": s.id, "period": s.period, "taken_at": s.taken_at, "commit": s.commit, "best_case": s.best_case,
             "weighted": s.weighted, "won_to_date": s.won_to_date} for s in rows]


@router.post("/forecast/snapshot")
def snapshot_now(db: Session = Depends(get_db)):
    s = svc.take_snapshot(db)
    db.commit()
    return {"id": s.id, "period": s.period}


@router.get("/deals/slip-risk")
def slip_risks(db: Session = Depends(get_db)):
    svc.flag_slips(db)
    db.commit()
    return [{"opportunity_id": o.id, "name": o.name, "owner": o.owner, "amount": o.amount, "close_date": o.close_date,
             "risk": o.slip_risk, "reasons": o.slip_reasons or []}
            for o in db.scalars(select(Opportunity).where(Opportunity.slip_risk.in_(["high", "medium"]))
                                .order_by(Opportunity.amount.desc().nullslast()))]


class RepCall(BaseModel):
    category: str = Field(pattern="^(commit|best_case|pipeline|omitted)$")


@router.put("/deals/{opp_id}/rep-forecast")
def rep_forecast(opp_id: int, body: RepCall, db: Session = Depends(get_db)):
    o = db.get(Opportunity, opp_id)
    if not o:
        raise HTTPException(404, "Deal not found")
    o.rep_forecast_category = body.category
    db.commit()
    return {"opportunity_id": o.id, "rep_forecast_category": o.rep_forecast_category}


# ------------------------------------------------------------------ meetings

class OutcomeIn(BaseModel):
    outcome: str = Field(pattern="^(held|no_show|qualified|next_step_booked|cancelled|rescheduled)$")


@router.put("/meetings/{event_id}/outcome")
def meeting_outcome(event_id: int, body: OutcomeIn, db: Session = Depends(get_db)):
    ev = db.get(CalendarEvent, event_id)
    if not ev:
        raise HTTPException(404, "Meeting not found")
    ev.outcome = body.outcome
    if body.outcome == "no_show" and ev.contact_id:
        from ..models import Activity

        db.add(Activity(contact_id=ev.contact_id, type="meeting_no_show", detail=ev.title))
    db.commit()
    return {"id": ev.id, "outcome": ev.outcome}


@router.get("/insights/meetings")
def meetings_funnel(days: int = 180, db: Session = Depends(get_db)):
    return svc.meeting_funnel(db, days)


class TemplateIn(BaseModel):
    subject: str
    body: str = Field(description="Merge fields: {{first_name}}, {{company}}, {{meeting_time}}, {{meeting_title}}")


@router.get("/meetings/reminder-template")
def get_template(db: Session = Depends(get_db)):
    live = svc.reminder_template(db)
    pending = db.scalar(select(Approval).where(Approval.kind == "meeting_reminder", Approval.status == "pending")
                        .order_by(Approval.id.desc()))
    pick = lambda a: {"id": a.id, "subject": a.subject, "body": a.body, "status": a.status} if a else None
    return {"live": pick(live), "pending": pick(pending)}


@router.post("/meetings/reminder-template")
def set_template(body: TemplateIn, request: Request, db: Session = Depends(get_db)):
    """A new reminder template waits in Approvals (filter: meeting reminders) until a person approves it."""
    a = approvals_svc.queue(db, kind="meeting_reminder", subject=body.subject, body=body.body,
                            created_by_agent=_actor(request), title="Meeting reminder template",
                            rationale="Sent to the customer the day before each meeting.")
    db.commit()
    return {"approval_id": a.id, "flags": a.guardrail_flags}


# ------------------------------------------------------------------ relationships

@router.get("/accounts/{account_id}/relationships")
def account_relationships(account_id: int, db: Session = Depends(get_db)):
    return svc.relationships(db, account_id)
