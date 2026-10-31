"""Deal-stage agents: Meeting Prep (before the call) and Call Analyst (after it)."""
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Activity, Contact, Opportunity, SalesDocument
from ..services import approvals, memory
from ..tools.crm import get_crm
from .base import WEB_TOOLS, run_agent
from .shared_tools import brief_to_dict, get_account, get_contact, kb_search

OPEN_STAGES = ("discovery", "qualification", "solution", "proposal", "negotiation")

# --------------------------------------------------------------------------- Meeting Prep

PREP_ROLE = """You are Meeting Prep. A rep has a call coming up; give them a one-page brief they can read in
three minutes and walk in confident.

Use get_contact and get_account first (the latest_brief is your main source). Use web_search only to fill
gaps or check for news from the last few weeks. Use kb_search for the most relevant case studies and
objection handling.

Return Markdown with exactly these sections:
## Objective  (what a great outcome for this call looks like, in one or two lines)
## Who they are  (3-4 bullets: company context that matters for this conversation)
## People in the room  (each attendee: role, what they likely care about, how to engage them)
## Agenda  (a suggested 30-minute flow)
## Discovery questions  (8-10 open questions, ordered; cover pain, impact, process, decision makers, timing)
## Proof points to use  (2-3, each with the result and when to bring it up; knowledge-base facts only)
## Likely objections  (each with a short response)
## Ask at the end  (the specific next step to propose)

Only state facts you found. Mark guesses as guesses."""


async def prep_meeting(
    contact_id: int, goal: str | None = None, attendees: str | None = None,
    when: str | None = None, parent_run_id: int | None = None,
) -> int:
    """Writes a meeting prep brief. Returns the SalesDocument id."""
    with session_scope() as db:
        c = db.get(Contact, contact_id)
        if not c:
            raise ValueError(f"No contact {contact_id}")
        account_id, label = c.account_id, f"{c.full_name} ({c.title or 'title unknown'}) at {c.account.name}"

    prompt = f"Prepare me for a call with {label}. contact_id={contact_id}, account_id={account_id}."
    if history := memory.context_block(account_id, contact_id):
        prompt += (f"\n\n{history}\n\n"
                   "Build on this history: what was promised, what they worried about, who is involved.")
    if when:
        prompt += f"\nWhen: {when}"
    if attendees:
        prompt += f"\nAttendees: {attendees}"
    if goal:
        prompt += f"\nMy goal for the call: {goal}"

    result = await run_agent(
        agent="meeting_prep",
        model=settings.model_worker,
        role_prompt=PREP_ROLE,
        prompt=prompt,
        tools=[*WEB_TOOLS, get_contact, get_account, kb_search],
        effort="medium",
        parent_run_id=parent_run_id,
    )
    with session_scope() as db:
        doc = SalesDocument(
            kind="meeting_prep", title=f"Meeting prep: {label}", content=result.text,
            data={"goal": goal, "attendees": attendees, "when": when},
            account_id=account_id, contact_id=contact_id, run_id=result.run_id,
        )
        db.add(doc)
        db.add(Activity(contact_id=contact_id, type="meeting_prep", detail=f"Prep brief for {when or 'upcoming call'}"))
        db.flush()
        return doc.id


# --------------------------------------------------------------------------- Call Analyst

ANALYST_ROLE = """You are the Call Analyst. Turn a sales call transcript (or rough notes) into a structured
deal record and a follow-up email.

- MEDDICC: fill each field only with what was actually said. Use "Unknown" when the call didn't cover it;
  those gaps are valuable, because they tell the rep what to ask next.
- stage: discovery (exploring the problem), qualification (pain + budget/authority confirmed),
  solution (shaping the approach), proposal (they asked for a proposal or pricing), negotiation.
- amount_estimate / close_date_estimate: only if numbers or timing were discussed; otherwise null.
- health: green (clear pain, engaged champion, next step booked), amber (gaps), red (no pain, no next step,
  or a blocker raised).
- follow_up_email: thank them, recap what you heard in their words (3 bullets), confirm agreed next steps
  with owners and dates, and attach nothing. Under 170 words. Follow the BRAND VOICE. No placeholders:
  if a date wasn't agreed, propose one."""


class Meddicc(BaseModel):
    metrics: str = Field(description="Quantified impact they care about.")
    economic_buyer: str
    decision_criteria: str
    decision_process: str
    identify_pain: str
    champion: str
    competition: str


class FollowUp(BaseModel):
    subject: str
    body: str


class CallAnalysis(BaseModel):
    summary: str = Field(description="4-6 sentence summary of the call.")
    meddicc: Meddicc
    stage: Literal["discovery", "qualification", "solution", "proposal", "negotiation"]
    amount_estimate: float | None = None
    close_date_estimate: str | None = Field(default=None, description="ISO date, only if discussed.")
    buying_signals: list[str]
    risks: list[str]
    next_steps: list[str] = Field(description="Agreed next steps, each with owner and date if known.")
    questions_for_next_call: list[str]
    health: Literal["green", "amber", "red"]
    follow_up_email: FollowUp


def _markdown(a: CallAnalysis) -> str:
    m = a.meddicc
    lines = [
        f"**Stage:** {a.stage} · **Health:** {a.health}"
        + (f" · **Amount:** {a.amount_estimate:,.0f}" if a.amount_estimate else "")
        + (f" · **Close:** {a.close_date_estimate}" if a.close_date_estimate else ""),
        "", "## Summary", a.summary, "", "## MEDDICC",
        f"- **Metrics:** {m.metrics}", f"- **Economic buyer:** {m.economic_buyer}",
        f"- **Decision criteria:** {m.decision_criteria}", f"- **Decision process:** {m.decision_process}",
        f"- **Pain:** {m.identify_pain}", f"- **Champion:** {m.champion}", f"- **Competition:** {m.competition}",
    ]
    for title, items in (("Buying signals", a.buying_signals), ("Risks", a.risks),
                         ("Next steps", a.next_steps), ("Ask next time", a.questions_for_next_call)):
        if items:
            lines += ["", f"## {title}", *[f"- {x}" for x in items]]
    return "\n".join(lines)


async def analyze_call(contact_id: int, transcript: str, parent_run_id: int | None = None,
                       source_ref: str | None = None, occurred_at: datetime | None = None,
                       title: str | None = None) -> dict:
    """Analyse a call, update/create the opportunity, sync CRM, queue the follow-up, add it to the timeline.

    `source_ref` identifies a recording from an integration (prevents analysing it twice).
    """
    with session_scope() as db:
        c = db.get(Contact, contact_id)
        if not c:
            raise ValueError(f"No contact {contact_id}")
        context = {"contact": c.full_name, "title": c.title, "company": c.account.name,
                   "account_brief": brief_to_dict(c.account.briefs[0] if c.account.briefs else None),
                   "history": memory.context_block(c.account_id, c.id)}

    result = await run_agent(
        agent="call_analyst",
        model=settings.model_worker,
        role_prompt=ANALYST_ROLE,
        prompt=(f"Context: {json.dumps(context, default=str)}\n\n"
                f"<transcript>\n{transcript[:200_000]}\n</transcript>"),
        effort="medium",
        max_tokens=8000,
        output_format=CallAnalysis,
        parent_run_id=parent_run_id,
    )
    a: CallAnalysis = result.parsed

    with session_scope() as db:
        c = db.get(Contact, contact_id)
        account = c.account
        opp = db.scalar(select(Opportunity).where(
            Opportunity.account_id == account.id, Opportunity.stage.in_(OPEN_STAGES)
        ).order_by(Opportunity.id.desc()))
        if not opp:
            opp = Opportunity(account_id=account.id, primary_contact_id=c.id,
                              name=f"{account.name} - {datetime.now(timezone.utc):%Y-%m}")
            db.add(opp)
        opp.stage = a.stage
        opp.qualification = a.meddicc.model_dump()
        opp.next_steps, opp.risks, opp.health = a.next_steps, a.risks, a.health
        if a.amount_estimate:
            opp.amount = a.amount_estimate
        if a.close_date_estimate:
            opp.close_date = a.close_date_estimate
        opp.last_activity_at = datetime.now(timezone.utc)
        db.flush()

        doc = SalesDocument(kind="call_summary", title=f"Call summary: {c.full_name}, {account.name}",
                            content=_markdown(a), data=a.model_dump(), account_id=account.id,
                            contact_id=c.id, opportunity_id=opp.id, run_id=result.run_id)
        db.add(doc)
        if c.status not in ("disqualified",):
            c.status = "meeting"
        db.add(Activity(contact_id=c.id, type="call_analyzed", detail=f"{a.stage}, {a.health}: {a.summary[:300]}"))

        approval = approvals.queue(
            db, kind="email", contact=c, subject=a.follow_up_email.subject, body=a.follow_up_email.body,
            created_by_agent="call_analyst", run_id=result.run_id,
            rationale="Post-call follow-up recapping what the prospect said and the agreed next steps.",
            grounding_extra=transcript,
        )
        crm = get_crm()
        crm.upsert_opportunity(opp)
        crm.log_activity(c, "Call summary", doc.content)
        crm.update_lead_status(c, c.status, c.score)
        memory.record(
            db, source="call", source_ref=source_ref or f"call:manual:{result.run_id}", contact_id=c.id,
            account_id=account.id, occurred_at=occurred_at, title=title or f"Call with {c.full_name}",
            raw_text=transcript[:200_000], summary=a.summary, processed=True,
            facts={"commitments": a.next_steps, "risks": a.risks, "buying_signals": a.buying_signals,
                   "objections": [r for r in a.risks if "object" in r.lower()]},
        )
        db.flush()
        return {"opportunity_id": opp.id, "document_id": doc.id, "approval_id": approval.id,
                "stage": a.stage, "health": a.health, "summary": a.summary}
