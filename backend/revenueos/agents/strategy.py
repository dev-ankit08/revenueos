"""Revenue intelligence agents (Opus): Deal Strategist, Forecast Analyst, Learning loop."""
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Activity, Approval, Contact, KBDocument, Opportunity, SalesDocument, Signal
from ..services import approvals, insights, memory
from ..tools import kb
from ..tools.crm import get_crm
from .base import run_agent
from .shared_tools import brief_to_dict, kb_search

# --------------------------------------------------------------------------- Deal Strategist

DEAL_ROLE = """You are the Deal Strategist, a seasoned enterprise sales leader reviewing one open deal.
Judge it like a skeptical VP of Sales in a pipeline review: evidence over optimism.

- win_probability: your honest estimate from the evidence (MEDDICC completeness, engagement, stage history,
  momentum, competition, risks), not the stage default.
- forecast_category: commit (would bet on it this period), best_case, pipeline, or omitted (not real).
- Stakeholders: map the people we know and the roles we are missing. Single-threaded deals are a risk;
  say who to engage next by title and how.
- next_best_actions: 3-5 specific actions with owner and timing, most impactful first.
- If the single most useful next step is an email to a known contact, write it as draft_email (it will go to
  the rep's approval queue). Use kb_search for any proof point. Otherwise leave draft_email null."""


class Stakeholder(BaseModel):
    name_or_title: str
    role_in_deal: Literal["economic_buyer", "champion", "influencer", "technical_evaluator", "user", "blocker", "unknown"]
    engagement: Literal["engaged", "aware", "not_engaged"]
    action: str


class NextAction(BaseModel):
    action: str
    owner: Literal["rep", "manager", "marketing", "executive_sponsor"]
    due_in_days: int
    why: str


class DraftEmail(BaseModel):
    contact_id: int
    subject: str
    body: str


class DealStrategy(BaseModel):
    health: Literal["green", "amber", "red"]
    win_probability: int = Field(ge=0, le=100)
    forecast_category: Literal["commit", "best_case", "pipeline", "omitted"]
    assessment: str = Field(description="3-5 sentences: where the deal really stands and why.")
    stakeholders: list[Stakeholder]
    missing_roles: list[str]
    risks: list[str]
    next_best_actions: list[NextAction]
    talk_track: str = Field(description="What the rep should say in the next conversation, 3-5 sentences.")
    draft_email: DraftEmail | None = None


def _deal_context(db, o: Opportunity) -> dict:
    now = datetime.now(timezone.utc)
    acc = o.account
    contacts = list(acc.contacts)
    contact_ids = [c.id for c in contacts]
    acts = list(db.scalars(select(Activity).where(Activity.contact_id.in_(contact_ids))
                           .order_by(Activity.created_at.desc()).limit(25))) if contact_ids else []
    calls = db.scalars(select(SalesDocument).where(SalesDocument.account_id == acc.id,
                                                   SalesDocument.kind == "call_summary")
                       .order_by(SalesDocument.id.desc()).limit(3))
    has_proposal = db.scalar(select(SalesDocument.id).where(SalesDocument.account_id == acc.id,
                                                            SalesDocument.kind == "proposal")) is not None
    signals = db.scalars(select(Signal).where(Signal.account_id == acc.id).order_by(Signal.id.desc()).limit(5))
    last = o.last_activity_at if o.last_activity_at.tzinfo else o.last_activity_at.replace(tzinfo=timezone.utc)
    return {
        "opportunity": {"id": o.id, "name": o.name, "stage": o.stage, "amount": o.amount, "close_date": o.close_date,
                        "meddicc": o.qualification, "next_steps": o.next_steps, "risks": o.risks,
                        "days_since_activity": (now - last).days, "proposal_sent": has_proposal,
                        "stage_history": [{"to": c.to_stage, "at": c.changed_at.date().isoformat()}
                                          for c in o.stage_changes]},
        "account": {"name": acc.name, "industry": acc.industry, "employees": acc.employees,
                    "brief": brief_to_dict(acc.briefs[0] if acc.briefs else None)},
        "contacts": [{"contact_id": c.id, "name": c.full_name, "title": c.title, "status": c.status} for c in contacts],
        "recent_activity": [{"type": a.type, "detail": a.detail[:300], "at": a.created_at.date().isoformat()}
                            for a in acts],
        "call_summaries": [d.content for d in calls],
        "signals": [{"type": s.type, "headline": s.headline, "date": s.event_date} for s in signals],
        "relationship_history": memory.context_block(acc.id),
    }


def _strategy_md(o_name: str, s: DealStrategy) -> str:
    lines = [f"**Health:** {s.health} · **Win probability:** {s.win_probability}% · **Forecast:** {s.forecast_category}",
             "", s.assessment, "", "## Next best actions",
             *[f"{i}. {a.action} (owner: {a.owner}, within {a.due_in_days} days): {a.why}"
               for i, a in enumerate(s.next_best_actions, 1)],
             "", "## Stakeholders",
             *[f"- {p.name_or_title}: {p.role_in_deal.replace('_', ' ')}, {p.engagement.replace('_', ' ')}. {p.action}"
               for p in s.stakeholders]]
    if s.missing_roles:
        lines += ["", "## Missing roles", *[f"- {r}" for r in s.missing_roles]]
    if s.risks:
        lines += ["", "## Risks", *[f"- {r}" for r in s.risks]]
    lines += ["", "## Talk track", s.talk_track]
    return "\n".join(lines)


async def review_deal(opportunity_id: int, parent_run_id: int | None = None) -> dict:
    with session_scope() as db:
        o = db.get(Opportunity, opportunity_id)
        if not o:
            raise ValueError(f"No opportunity {opportunity_id}")
        context = _deal_context(db, o)
        valid_contacts = {c["contact_id"] for c in context["contacts"]}

    result = await run_agent(
        agent="deal_strategist",
        model=settings.model_orchestrator,
        role_prompt=DEAL_ROLE,
        prompt=f"Review this deal. Today is {datetime.now(timezone.utc).date()}.\n{json.dumps(context, default=str)}",
        tools=[kb_search],
        effort="high",
        max_tokens=16000,
        output_format=DealStrategy,
        parent_run_id=parent_run_id,
    )
    s: DealStrategy = result.parsed

    with session_scope() as db:
        o = db.get(Opportunity, opportunity_id)
        o.health, o.win_probability, o.forecast_category = s.health, s.win_probability, s.forecast_category
        o.risks = s.risks
        o.next_steps = [a.action for a in s.next_best_actions]
        doc = SalesDocument(kind="deal_strategy", title=f"Deal review: {o.name}", content=_strategy_md(o.name, s),
                            data=s.model_dump(), account_id=o.account_id, opportunity_id=o.id,
                            contact_id=o.primary_contact_id, run_id=result.run_id)
        db.add(doc)
        approval_id = None
        if s.draft_email and s.draft_email.contact_id in valid_contacts:
            a = approvals.queue(db, kind="email", contact=db.get(Contact, s.draft_email.contact_id),
                                subject=s.draft_email.subject, body=s.draft_email.body,
                                created_by_agent="deal_strategist", run_id=result.run_id,
                                rationale=f"Next best action for {o.name}: {s.next_best_actions[0].action}"
                                if s.next_best_actions else "Deal strategist recommendation",
                                grounding_extra=json.dumps(context, default=str))
            approval_id = a.id
        get_crm().upsert_opportunity(o)
        db.flush()
        return {"opportunity_id": o.id, "document_id": doc.id, "health": s.health,
                "win_probability": s.win_probability, "approval_id": approval_id}


async def review_pipeline(limit: int = 20, parent_run_id: int | None = None) -> str:
    with session_scope() as db:
        ids = list(db.scalars(select(Opportunity.id).where(Opportunity.stage.in_(insights.OPEN_STAGES))
                              .order_by(Opportunity.amount.desc().nullslast()).limit(limit)))
    results = [await review_deal(i, parent_run_id=parent_run_id) for i in ids]
    red = sum(r["health"] == "red" for r in results)
    return f"Reviewed {len(results)} deals: {red} red, {sum(r['health'] == 'amber' for r in results)} amber."


# --------------------------------------------------------------------------- Forecast Analyst

FORECAST_ROLE = """You are the Forecast Analyst presenting to the CRO. All numbers are computed for you; do not
recalculate or invent figures. Interpret them: is the quarter on track, where is the forecast fragile
(slipped dates, missing amounts, red deals in commit, thin coverage), which deals decide the quarter, and what
leadership should do this week. Be direct and specific; name deals."""


class FocusDeal(BaseModel):
    opportunity_id: int
    why: str
    action: str


class ForecastReport(BaseModel):
    headline: str = Field(description="One sentence: the forecast call.")
    confidence: Literal["high", "medium", "low"]
    narrative: str = Field(description="One short paragraph.")
    key_risks: list[str]
    deals_to_focus: list[FocusDeal]
    pipeline_gaps: list[str]
    recommendations: list[str]


def _money(x) -> str:
    return f"{x:,.0f}" if x else "0"


def _forecast_md(n: dict, r: ForecastReport) -> str:
    lines = [f"# Forecast {n['quarter']}", "", f"**{r.headline}** (confidence: {r.confidence})", "",
             f"- Closed won this quarter: {_money(n['closed_won_this_quarter'])}",
             f"- Commit: {_money(n['commit'])} · Best case: {_money(n['best_case'])} · Weighted: {_money(n['weighted'])}",
             f"- Quarter pipeline: {_money(n['quarter_pipeline'])} · Open pipeline: {_money(n['open_pipeline'])}"]
    if n["target"]:
        lines.append(f"- Target: {_money(n['target'])} · Projected attainment: {n['projected_attainment']}% · "
                     f"Coverage: {n['coverage_ratio'] or '-'}x")
    lines += ["", r.narrative, "", "## Deals that decide the quarter",
              *[f"- #{d.opportunity_id}: {d.why} **Do:** {d.action}" for d in r.deals_to_focus],
              "", "## Risks", *[f"- {x}" for x in r.key_risks],
              "", "## Pipeline gaps", *[f"- {x}" for x in r.pipeline_gaps],
              "", "## Recommendations", *[f"- {x}" for x in r.recommendations]]
    return "\n".join(lines)


async def forecast(target: float | None = None, parent_run_id: int | None = None) -> dict:
    target = target or settings.quarterly_target or None
    with session_scope() as db:
        numbers = insights.forecast_numbers(db, target)
    result = await run_agent(
        agent="forecast_analyst",
        model=settings.model_orchestrator,
        role_prompt=FORECAST_ROLE,
        prompt=f"Forecast data (computed):\n{json.dumps(numbers, default=str)}",
        effort="high",
        max_tokens=8000,
        output_format=ForecastReport,
        parent_run_id=parent_run_id,
    )
    r: ForecastReport = result.parsed
    with session_scope() as db:
        doc = SalesDocument(kind="forecast", title=f"Forecast {numbers['quarter']}", content=_forecast_md(numbers, r),
                            data={"numbers": numbers, "report": r.model_dump()}, run_id=result.run_id)
        db.add(doc)
        db.flush()
        return {"document_id": doc.id, "headline": r.headline}


# --------------------------------------------------------------------------- Learning loop

LEARN_ROLE = """You are the Learning agent. Study our sent outreach and what happened to it, and distil a short,
practical playbook the writing agents will follow. Base every point on patterns in THIS data (cite counts),
weigh the A/B results (only significant winners are proof), and be honest about uncertainty: with a small
sample, say so and keep conclusions tentative. Do not repeat generic best practice that the data doesn't show."""


class PersonaAngle(BaseModel):
    persona: str
    angle: str
    evidence: str


class Playbook(BaseModel):
    summary: str
    what_works: list[str]
    what_to_avoid: list[str]
    subject_lines: list[str] = Field(description="Patterns that earned replies, with examples.")
    by_persona: list[PersonaAngle]
    confidence: Literal["low", "medium", "high"]
    sample_note: str


PLAYBOOK_TITLE = "Outreach playbook (learned)"
MIN_SENT, MIN_REPLIES = 20, 3


def _outcome(reply_type: str | None) -> str:
    if reply_type in insights.POSITIVE_REPLIES:
        return "positive_reply"
    if reply_type in insights.NEGATIVE_REPLIES:
        return "negative_reply"
    return "other_reply" if reply_type else "no_reply"


async def learn_from_results(parent_run_id: int | None = None) -> dict:
    with session_scope() as db:
        credited = insights.credited_replies(db)
        sent = list(db.scalars(select(Approval).where(Approval.status == "sent", Approval.kind == "email")
                               .order_by(Approval.sent_at.desc()).limit(200)))
        replies = sum(1 for a in sent if a.id in credited)
        if len(sent) < MIN_SENT or replies < MIN_REPLIES:
            return {"updated": False, "reason": f"Need at least {MIN_SENT} sent emails and {MIN_REPLIES} replies "
                                                f"to learn reliably (have {len(sent)} and {replies})."}
        samples = [{
            "subject": a.subject, "body": a.body[:700], "step": a.sequence_step, "variant": a.variant,
            "agent": a.created_by_agent, "title": a.contact.title if a.contact else None,
            "industry": a.contact.account.industry if a.contact else None, "outcome": _outcome(credited.get(a.id)),
        } for a in sent]
        stats = {"attribution": insights.attribution(db), "ab_tests": insights.ab_results(db)}

    result = await run_agent(
        agent="learning",
        model=settings.model_orchestrator,
        role_prompt=LEARN_ROLE,
        prompt=f"Results summary:\n{json.dumps(stats, default=str)}\n\nSent emails with outcomes:\n{json.dumps(samples)}",
        effort="high",
        max_tokens=8000,
        output_format=Playbook,
        parent_run_id=parent_run_id,
    )
    p: Playbook = result.parsed
    md = "\n".join([
        f"_{p.summary}_ (confidence: {p.confidence}; {p.sample_note})", "", "What works:",
        *[f"- {x}" for x in p.what_works], "", "Avoid:", *[f"- {x}" for x in p.what_to_avoid],
        "", "Subject lines:", *[f"- {x}" for x in p.subject_lines],
        "", "By persona:", *[f"- {x.persona}: {x.angle} ({x.evidence})" for x in p.by_persona],
    ])
    with session_scope() as db:
        for old in db.scalars(select(KBDocument).where(KBDocument.title == PLAYBOOK_TITLE)):
            db.delete(old)
        db.flush()
        kb.add_document(db, PLAYBOOK_TITLE, "playbook", md)
        doc = SalesDocument(kind="playbook", title=f"Playbook update {datetime.now(timezone.utc):%Y-%m-%d}",
                            content=md, data=p.model_dump(), run_id=result.run_id)
        db.add(doc)
        db.flush()
        return {"updated": True, "document_id": doc.id, "confidence": p.confidence, "emails_analysed": len(samples)}
