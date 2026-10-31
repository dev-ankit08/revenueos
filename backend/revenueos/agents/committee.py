"""Buying-committee outreach: one account narrative, a role-specific sequence for every stakeholder.

Enterprise deals are decided by several people. The Committee Planner (Opus) assigns each known
contact a role and an angle consistent with one story; the Outreach Writer then drafts each
person's sequence with that brief, staggered so the account isn't hit by everyone on one day.
"""
import json
from typing import Literal

from pydantic import BaseModel, Field

from ..config import settings
from ..db import session_scope
from ..models import Account, SalesDocument
from ..services import memory
from . import researcher, writer
from .base import run_agent
from .person_researcher import get_dossier, research_person
from .shared_tools import brief_to_dict, kb_search

ROLE = """You are the Committee Planner, a strategic account executive. Plan coordinated outreach to the buying
committee of ONE account. Different people care about different things; the story must stay the same.

- account_narrative: the one-paragraph story every message must be consistent with (why change, why now,
  why us), grounded in the brief, the relationship history and the knowledge base (use kb_search for proof).
- For each contact given, decide their likely role in the decision and the angle that matters to THEM
  (an economic buyer hears risk and return; a champion hears how they will look good; a technical evaluator
  hears fit and effort; finance hears cost and payback). Use their dossier when present.
- Stagger the starts: usually the likely champion first, then the technical evaluator, then the economic
  buyer once there is something to point to. start_after_days is 0 for the first person.
- Name important roles nobody at the account covers yet (missing_roles), so the rep can find them.
Only plan for the contact ids you were given."""


class StakeholderPlan(BaseModel):
    contact_id: int
    role_in_deal: Literal["economic_buyer", "champion", "technical_evaluator", "finance", "user", "influencer"]
    angle: str = Field(description="The one thing to talk to this person about, in a sentence.")
    call_to_action: str
    start_after_days: int = Field(ge=0, le=30)
    steps: int = Field(ge=1, le=4)


class CommitteePlan(BaseModel):
    account_narrative: str
    stakeholders: list[StakeholderPlan]
    sequencing_rationale: str
    missing_roles: list[str]


def _plan_md(name: str, p: CommitteePlan, names: dict[int, str]) -> str:
    lines = [f"**Narrative:** {p.account_narrative}", "", "## Who hears what",
             *[f"- **{names.get(s.contact_id, s.contact_id)}** ({s.role_in_deal.replace('_', ' ')}, day {s.start_after_days}, "
               f"{s.steps} steps): {s.angle} → {s.call_to_action}" for s in p.stakeholders],
             "", f"**Order:** {p.sequencing_rationale}"]
    if p.missing_roles:
        lines += ["", "## Roles still to find", *[f"- {r}" for r in p.missing_roles]]
    return "\n".join(lines)


async def committee_outreach(account_id: int, instructions: str | None = None, research_people: bool = True,
                             parent_run_id: int | None = None) -> dict:
    with session_scope() as db:
        account = db.get(Account, account_id)
        if not account:
            raise ValueError(f"No account {account_id}")
        contacts = [(c.id, c.full_name, c.title) for c in account.contacts if c.status not in ("disqualified",)]
        has_brief, name = bool(account.briefs), account.name
    if not contacts:
        raise ValueError(f"{name} has no active contacts to plan for.")
    if not has_brief:
        await researcher.research_account(account_id, parent_run_id=parent_run_id)
    if research_people:
        for cid, _, _ in contacts:
            if not get_dossier(cid):
                await research_person(cid, parent_run_id=parent_run_id)

    with session_scope() as db:
        account = db.get(Account, account_id)
        context = {
            "account": name, "industry": account.industry, "brief": brief_to_dict(account.briefs[0] if account.briefs else None),
            "contacts": [{"contact_id": cid, "name": n, "title": t, "dossier": get_dossier(cid)} for cid, n, t in contacts],
            "relationship_history": memory.context_block(account_id),
        }
    prompt = f"Plan buying-committee outreach.\n{json.dumps(context, default=str)}"
    if instructions:
        prompt += f"\nRep's instructions: {instructions}"
    result = await run_agent(
        agent="committee_planner", model=settings.model_orchestrator, role_prompt=ROLE, prompt=prompt,
        tools=[kb_search], effort="high", max_tokens=16000, output_format=CommitteePlan, parent_run_id=parent_run_id,
    )
    plan: CommitteePlan = result.parsed
    valid = {cid for cid, _, _ in contacts}
    names = {cid: n for cid, n, _ in contacts}
    others = "; ".join(f"{names[s.contact_id]} ({s.role_in_deal.replace('_', ' ')})"
                       for s in plan.stakeholders if s.contact_id in valid)

    written = []
    for s in plan.stakeholders:
        if s.contact_id not in valid:
            continue
        brief = (f"COMMITTEE OUTREACH. Account narrative (every message must be consistent with it): "
                 f"{plan.account_narrative}\nThis person's role: {s.role_in_deal.replace('_', ' ')}. "
                 f"Angle: {s.angle}. Call to action: {s.call_to_action}. "
                 f"Add {s.start_after_days} days to every send_after_days (their sequence starts on day {s.start_after_days}). "
                 f"Colleagues also being contacted: {others}. Don't mention them by name unless natural; never "
                 f"send the same message twice in one company.")
        if instructions:
            brief += f"\nRep's instructions: {instructions}"
        await writer.write_sequence(s.contact_id, steps=s.steps, instructions=brief, parent_run_id=parent_run_id)
        written.append(names[s.contact_id])

    with session_scope() as db:
        doc = SalesDocument(kind="committee_plan", title=f"Committee plan: {name}", content=_plan_md(name, plan, names),
                            data=plan.model_dump(), account_id=account_id, run_id=result.run_id)
        db.add(doc)
        db.flush()
        return {"document_id": doc.id, "sequenced": written, "missing_roles": plan.missing_roles}
