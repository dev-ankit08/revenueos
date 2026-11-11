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


CONTENT_ROLE = """You review marketing content (blog posts, LinkedIn posts, ads, landing pages, newsletters,
case studies, one-pagers) before publication. Great content is specific to the stated audience, uses concrete,
true facts from the evidence (our knowledge base: offerings, approved case studies, brand voice), makes one clear
point, and has a clear call to action. It avoids generic filler that any company could publish.

Score 0-100:
- 80-100: specific to the audience, two or more concrete facts from the evidence, clear point and CTA, on brand.
- 60-79: relevant and mostly specific; one concrete fact; minor filler.
- 30-59: generic, vague claims, or weak fit for the audience.
- 0-29: boilerplate.
Any claim not supported by the evidence counts against the score and goes in unsupported_claims.
The feedback must say what to change, citing evidence to use."""


class Grade(BaseModel):
    score: int = Field(ge=0, le=100)
    specific_facts_used: list[str]
    unsupported_claims: list[str] = Field(description="Personal or account facts in the draft not found in the evidence.")
    generic_phrases: list[str]
    feedback: str = Field(description="One or two sentences: how to make it more personal, citing evidence to use.")


async def grade(*, subject: str | None, body: str, recipient: dict, evidence: str,
                parent_run_id: int | None = None, mode: str = "message") -> Grade:
    """mode="message" grades 1:1 outreach for personalisation; mode="content" grades marketing content."""
    result = await run_agent(
        agent="personalisation_grader",
        model=settings.model_fast,
        role_prompt=CONTENT_ROLE if mode == "content" else ROLE,
        prompt=(f"Recipient: {json.dumps(recipient, default=str)}\n\n<evidence>\n{evidence[:30_000]}\n</evidence>\n\n"
                f"<draft>\nSubject: {subject or '(none)'}\n\n{body}\n</draft>"),
        effort="low",
        max_tokens=1500,
        output_format=Grade,
        parent_run_id=parent_run_id,
    )
    return result.parsed


async def grade_approval(approval_id: int, evidence: str, parent_run_id: int | None = None) -> int | None:
    """Grade a draft that was queued without the writer's rewrite loop (call follow-ups, deal-strategy emails,
    marketing content) and attach the score and warnings for the reviewer. Never blocks: if grading fails,
    the draft gets a "not graded" warning instead. Returns the score (None when not graded)."""
    from ..db import session_scope
    from ..models import Approval

    with session_scope() as db:
        a = db.get(Approval, approval_id)
        if not a:
            return None
        c = a.contact
        mode = "content" if a.kind == "content" else "message"
        recipient = ({"name": c.full_name, "title": c.title, "company": c.account.name} if c
                     else {"audience": a.content_type or "marketing audience", "title": a.title})
        subject, body = a.subject, a.body
    flags: list[dict] = []
    try:
        g = await grade(subject=subject, body=body, recipient=recipient, evidence=evidence,
                        parent_run_id=parent_run_id, mode=mode)
    except Exception as exc:  # grading is advisory: never lose the draft over it
        g = None
        flags.append({"severity": "warn", "code": "not_graded",
                      "message": f"Couldn't check this draft for specificity ({type(exc).__name__}). Review it closely."})
    with session_scope() as db:
        a = db.get(Approval, approval_id)
        if g is not None:
            a.personalisation_score = g.score
            a.personalisation_notes = g.model_dump(exclude={"score"})
            flags += review_flags(g)
        if flags:
            have = {f.get("code") for f in a.guardrail_flags or []}
            a.guardrail_flags = [*(a.guardrail_flags or []), *[f for f in flags if f["code"] not in have]]
    return g.score if g else None


def review_flags(g: Grade) -> list[dict]:
    """Warnings for the reviewer from a grade: too generic, or facts the grader couldn't find in the evidence."""
    flags = []
    if g.score < int(settings.personalisation_threshold):
        flags.append({"severity": "warn", "code": "low_personalisation",
                      "message": f"Personalisation {g.score}/100. {g.feedback}"})
    if g.unsupported_claims:
        flags.append({"severity": "warn", "code": "grader_unsupported",
                      "message": "Not found in the research or knowledge base: " + "; ".join(g.unsupported_claims[:5])})
    return flags
