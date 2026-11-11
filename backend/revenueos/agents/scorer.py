"""Lead Scorer (Haiku): fit + intent scoring with an explanation and a routing tier."""
import json
from typing import Literal

from pydantic import BaseModel, Field

from ..config import settings
from ..db import session_scope
from ..models import Activity, Contact
from ..tools.crm import get_crm
from .base import run_agent

ROLE = """You are the Lead Scorer. Score a lead against the Ideal Customer Profile in the company context.

Scoring:
- fit_score (0-100): how well the company (industry, size, geography) and the person (seniority, function,
  buying power for what we sell) match the ICP and personas.
- intent_score (0-100): evidence they are in-market: an inbound message describing a need, a relevant
  trigger in the notes, the lead source. With no evidence, use 20-40; do not assume intent.
- total_score = round(0.6 * fit_score + 0.4 * intent_score).
- tier: A >= 75, B 55-74, C 35-54, D < 35.
- disqualify only for clear reasons: competitor, student/job seeker, a person with no plausible role in buying,
  or a company far outside the ICP.

Be specific in reasons: cite the actual attributes that drove the score. If data is missing, say what is missing."""


class LeadScore(BaseModel):
    fit_score: int = Field(ge=0, le=100)
    intent_score: int = Field(ge=0, le=100)
    total_score: int = Field(ge=0, le=100)
    tier: Literal["A", "B", "C", "D"]
    persona: str = Field(description="Which buyer persona this person maps to, or 'unknown'.")
    reasons: list[str] = Field(description="2-5 short, specific reasons behind the score.")
    missing_data: list[str]
    disqualify: bool
    recommended_action: Literal[
        "research_and_sequence_now", "nurture_campaign", "route_to_ae_immediately", "disqualify"
    ]


async def score_contact(contact_id: int, parent_run_id: int | None = None) -> LeadScore:
    with session_scope() as db:
        c = db.get(Contact, contact_id)
        if not c:
            raise ValueError(f"No contact {contact_id}")
        lead = {
            "name": c.full_name, "title": c.title, "email_domain": (c.email or "").split("@")[-1] or None,
            "source": c.source, "inbound_message": c.inbound_message,
            "company": c.account.name, "industry": c.account.industry,
            "employees": c.account.employees, "country": c.account.country, "notes": c.account.notes,
            "recent_buying_signals": _signals(db, c.account_id),
        }

    result = await run_agent(
        agent="lead_scorer",
        model=settings.model_fast,
        role_prompt=ROLE,
        prompt=f"Score this lead:\n{json.dumps(lead, indent=2)}",
        effort="low",
        max_tokens=2000,
        output_format=LeadScore,
        parent_run_id=parent_run_id,
    )
    score: LeadScore = result.parsed

    with session_scope() as db:
        c = db.get(Contact, contact_id)
        c.score = score.total_score
        c.score_tier = score.tier
        c.score_reason = "; ".join(score.reasons)
        if score.disqualify:
            c.status = "disqualified"
        db.add(Activity(contact_id=c.id, type="scored",
                        detail=f"{score.total_score} ({score.tier}) -> {score.recommended_action}"))
        get_crm().update_lead_status(c, c.status, c.score)
    return score


def _signals(db, account_id: int) -> list[dict]:
    """Intent surges, high-intent website visits, news triggers and churn risk from the last 30 days."""
    from ..services import growth

    return growth.recent_signals(db, account_id)
