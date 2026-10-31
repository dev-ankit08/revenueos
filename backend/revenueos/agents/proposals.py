"""Pitch & Proposal Builder (Sonnet): tailored proposal + pitch deck from the brief, call notes and KB."""
import json
from datetime import date

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Activity, Contact, Opportunity, SalesDocument
from ..services import memory
from ..tools import docgen
from ..tools.crm import get_crm
from .base import run_agent
from .shared_tools import brief_to_dict, get_account, kb_search

ROLE = """You are the Pitch & Proposal Builder. Write a proposal and a matching pitch deck tailored to one account.

Inputs you get: the account brief, any call summaries (what the prospect actually said), and the rep's
instructions. Use kb_search to pull the right offering, case studies, pricing guidance and objection handling.

Rules:
- Lead with THEIR situation and goals in their own words (from call notes when available), then the approach.
- The approach must map to real offerings in the knowledge base. Phases need realistic durations.
- Proof points: only knowledge-base case studies, with their real results. Never invent customers or numbers.
- Investment: follow the knowledge-base pricing guidance. If it doesn't support a figure for this scope,
  say pricing will be confirmed after scoping. Never invent prices.
- Deck: 7-10 slides, one idea per slide, at most 5 bullets of at most 15 words each.
  Include speaker notes with what the rep should say.
- Write in the BRAND VOICE. Concrete, no filler."""


class Phase(BaseModel):
    name: str
    duration: str
    activities: list[str]
    deliverables: list[str]


class ProofPoint(BaseModel):
    title: str
    result: str
    why_relevant: str


class Slide(BaseModel):
    title: str
    bullets: list[str]
    speaker_notes: str


class ProposalContent(BaseModel):
    title: str
    executive_summary: str = Field(description="2 short paragraphs.")
    client_situation: list[str]
    objectives: list[str]
    approach: list[Phase]
    why_us: list[str]
    proof_points: list[ProofPoint]
    investment: str = Field(description="Commercial approach per KB pricing guidance; no invented figures.")
    assumptions: list[str]
    next_steps: list[str]
    deck: list[Slide]


def _sections(p: ProposalContent) -> list[dict]:
    return [
        {"heading": "Executive summary", "paragraphs": p.executive_summary.split("\n\n")},
        {"heading": "Your situation", "bullets": p.client_situation},
        {"heading": "Objectives", "bullets": p.objectives},
        *[{"heading": f"Phase {i}: {ph.name} ({ph.duration})",
           "bullets": [*ph.activities, *[f"Deliverable: {d}" for d in ph.deliverables]]}
          for i, ph in enumerate(p.approach, 1)],
        {"heading": "Why us", "bullets": p.why_us},
        {"heading": "Relevant results",
         "bullets": [f"{pp.title}: {pp.result} ({pp.why_relevant})" for pp in p.proof_points]},
        {"heading": "Investment", "paragraphs": [p.investment]},
        {"heading": "Assumptions", "bullets": p.assumptions},
        {"heading": "Next steps", "bullets": p.next_steps},
    ]


def _markdown(p: ProposalContent) -> str:
    out = [f"# {p.title}"]
    for s in _sections(p):
        out += ["", f"## {s['heading']}", *s.get("paragraphs", []), *[f"- {b}" for b in s.get("bullets", [])]]
    return "\n".join(out)


async def build_proposal(
    contact_id: int, instructions: str | None = None, parent_run_id: int | None = None
) -> dict:
    with session_scope() as db:
        c = db.get(Contact, contact_id)
        if not c:
            raise ValueError(f"No contact {contact_id}")
        account = c.account
        calls = db.scalars(select(SalesDocument).where(
            SalesDocument.account_id == account.id, SalesDocument.kind == "call_summary"
        ).order_by(SalesDocument.id.desc()).limit(3))
        context = {
            "prepared_for": f"{c.full_name}, {c.title or ''}".strip(", "),
            "account_id": account.id, "company": account.name, "industry": account.industry,
            "brief": brief_to_dict(account.briefs[0] if account.briefs else None),
            "call_summaries": [d.content for d in calls],
            "relationship_history": memory.context_block(account.id, c.id),
        }
        account_id, company = account.id, account.name

    prompt = f"Build a proposal and pitch deck.\n{json.dumps(context, default=str)}"
    if instructions:
        prompt += f"\n\nRep's instructions: {instructions}"

    result = await run_agent(
        agent="proposal_builder",
        model=settings.model_worker,
        role_prompt=ROLE,
        prompt=prompt,
        tools=[kb_search, get_account],
        effort="high",
        max_tokens=32000,
        output_format=ProposalContent,
        parent_run_id=parent_run_id,
    )
    p: ProposalContent = result.parsed

    stem = f"{docgen.safe_filename(company)}_{date.today():%Y%m%d}_{result.run_id}"
    subtitle = f"Prepared for {context['prepared_for']} · {settings.company_name} · {date.today():%B %Y}"
    deck_name, docx_name = f"{stem}_deck.pptx", f"{stem}_proposal.docx"
    docgen.build_deck(settings.generated_dir / deck_name, p.title, subtitle, [s.model_dump() for s in p.deck])
    docgen.build_proposal_docx(settings.generated_dir / docx_name, p.title, subtitle, _sections(p))

    with session_scope() as db:
        opp = db.scalar(select(Opportunity).where(Opportunity.account_id == account_id)
                        .order_by(Opportunity.id.desc()))
        common = dict(account_id=account_id, contact_id=contact_id, run_id=result.run_id,
                      opportunity_id=opp.id if opp else None)
        proposal = SalesDocument(kind="proposal", title=f"Proposal: {p.title}", content=_markdown(p),
                                 data=p.model_dump(), file_name=docx_name, **common)
        deck = SalesDocument(kind="pitch_deck", title=f"Pitch deck: {p.title}",
                             content="\n".join(f"### {s.title}\n" + "\n".join(f"- {b}" for b in s.bullets)
                                               for s in p.deck),
                             data={"slides": [s.model_dump() for s in p.deck]}, file_name=deck_name, **common)
        db.add_all([proposal, deck])
        if opp and opp.stage in ("discovery", "qualification", "solution"):
            opp.stage = "proposal"
            get_crm().upsert_opportunity(opp)
        db.add(Activity(contact_id=contact_id, type="proposal_built", detail=p.title))
        db.flush()
        return {"proposal_document_id": proposal.id, "deck_document_id": deck.id, "title": p.title}
