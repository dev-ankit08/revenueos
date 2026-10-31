"""Person Researcher (Sonnet + web): a sourced dossier of one person's PUBLIC, PROFESSIONAL footprint.

This is what lets outreach speak to the person, not just the company: what they have said in
talks, articles, interviews and company posts, and what they are trying to achieve in their role.
"""
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from ..config import settings
from ..db import session_scope
from ..models import Activity, Contact, PersonProfile
from .base import WEB_TOOLS, run_agent, submit_tool

ROLE = """You are the Person Researcher. Build a short dossier on ONE professional so a salesperson can write to them
about what they actually care about.

Look for their PUBLIC, PROFESSIONAL footprint: current role and recent role history, conference talks,
podcasts, interviews, articles or posts they wrote, press quotes, company announcements that name them,
and published priorities for their function.

Rules (strict):
- Professional and public information only. Never include family, health, home address or location,
  politics, religion, finances, or anything from private accounts.
- Do not scrape LinkedIn. Use pages that are publicly reachable on the web.
- Every public_statement needs a source URL and, where available, a date. Paraphrase faithfully.
- Same-name confusion is common: only use pages you are confident are about THIS person at THIS company.
  If you can't find anything reliable, say so in the summary and return empty lists. Never invent.
- Finish by calling submit_dossier once."""


class Statement(BaseModel):
    statement: str = Field(description="What they said or wrote, faithfully paraphrased or briefly quoted.")
    source: str = Field(description="URL")
    date: str | None = None


class Dossier(BaseModel):
    summary: str = Field(description="3-4 sentences: who they are professionally and what they focus on now.")
    role_history: list[str]
    public_statements: list[Statement]
    priorities: list[str] = Field(description="What they are trying to achieve, inferred only from the statements.")
    talking_points: list[str] = Field(description="3-5 specific, respectful openers grounded in the statements.")
    communication_style: str | None = Field(default=None, description="E.g. 'data-driven, short and direct'.")
    sources: list[str]


def dossier_dict(p: PersonProfile | None) -> dict | None:
    if not p:
        return None
    return {"summary": p.summary, "role_history": p.role_history, "public_statements": p.public_statements,
            "priorities": p.priorities, "talking_points": p.talking_points,
            "communication_style": p.communication_style, "sources": p.sources, "updated_at": p.updated_at}


def get_dossier(contact_id: int) -> dict | None:
    with session_scope() as db:
        return dossier_dict(db.query(PersonProfile).filter_by(contact_id=contact_id).first())


async def research_person(contact_id: int, parent_run_id: int | None = None) -> dict:
    with session_scope() as db:
        c = db.get(Contact, contact_id)
        if not c:
            raise ValueError(f"No contact {contact_id}")
        who = {"name": c.full_name, "title": c.title, "company": c.account.name, "domain": c.account.domain,
               "notes_from_rep": c.inbound_message}

    submit, get_result = submit_tool(Dossier, "submit_dossier", "Submit the finished dossier (call once).")
    await run_agent(
        agent="person_researcher",
        model=settings.model_worker,
        role_prompt=ROLE,
        prompt=f"Research this person: {who}",
        tools=[{**WEB_TOOLS[0], "max_uses": 10}, WEB_TOOLS[1], submit],
        effort="medium",
        max_tokens=16000,
        parent_run_id=parent_run_id,
    )
    d: Dossier = get_result()
    with session_scope() as db:
        p = db.query(PersonProfile).filter_by(contact_id=contact_id).first() or PersonProfile(contact_id=contact_id)
        p.summary, p.role_history, p.priorities = d.summary, d.role_history, d.priorities
        p.public_statements = [s.model_dump() for s in d.public_statements]
        p.talking_points, p.communication_style, p.sources = d.talking_points, d.communication_style, d.sources
        p.updated_at = datetime.now(timezone.utc)
        db.add(p)
        db.add(Activity(contact_id=contact_id, type="dossier",
                        detail=f"Person dossier: {len(d.public_statements)} sourced public statements"))
        db.flush()
        return dossier_dict(p)
