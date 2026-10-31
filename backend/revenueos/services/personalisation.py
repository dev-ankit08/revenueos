"""Personalisation grader (Haiku): how specific is this draft to this person, at this company, right now?

Used as a gate by the writing tools: a draft under the threshold goes back to the writer once
with concrete feedback; if it is still under, it is queued with a warning for the reviewer.
"""
import json

from pydantic import BaseModel, Field

from ..config import settings
from ..agents.base import run_agent

ROLE = """You grade sales emails and messages for personalisation. A great message could only have been sent to
THIS person at THIS company now: it uses specific, true facts from the evidence (their priorities, recent
news, what they said on a call or in a reply, their role's concerns) and connects them to a relevant point.

Score 0-100:
- 80-100: two or more specific facts from the evidence, woven into the reason for writing; no filler.
- 60-79: one clear specific fact used meaningfully; mostly free of filler.
- 30-59: only surface details (name, company, title, industry) or a specific fact bolted on.
- 0-29: a template anyone could receive.
Only count facts that appear in the evidence. A fact not in the evidence counts against the score.
The feedback must name the specific evidence the writer should have used."""


class Grade(BaseModel):
    score: int = Field(ge=0, le=100)
    specific_facts_used: list[str]
    unsupported_claims: list[str] = Field(description="Personal or account facts in the draft not found in the evidence.")
    generic_phrases: list[str]
    feedback: str = Field(description="One or two sentences: how to make it more personal, citing evidence to use.")


async def grade(*, subject: str | None, body: str, recipient: dict, evidence: str,
                parent_run_id: int | None = None) -> Grade:
    result = await run_agent(
        agent="personalisation_grader",
        model=settings.model_fast,
        role_prompt=ROLE,
        prompt=(f"Recipient: {json.dumps(recipient, default=str)}\n\n<evidence>\n{evidence[:30_000]}\n</evidence>\n\n"
                f"<draft>\nSubject: {subject or '(none)'}\n\n{body}\n</draft>"),
        effort="low",
        max_tokens=1500,
        output_format=Grade,
        parent_run_id=parent_run_id,
    )
    return result.parsed
