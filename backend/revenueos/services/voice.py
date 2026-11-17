"""Rep Voice: drafts that sound like the rep who will send them.

With the rep's consent, a fast model reads 10-50 emails the rep wrote themselves (from their connected mailbox, and
drafts they rewrote before sending) and writes a short style guide: typical length, greeting, sign-off, formality,
phrases they use and avoid. The writing assistants then follow it for that rep's leads.

Only the style is kept, not the emails. A rep can switch it off or rebuild it at any time.
"""
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import Approval, ContextEvent, RepVoice

MIN_SAMPLES = 10
MAX_SAMPLES = 50

ROLE = """You study how one salesperson writes emails and describe their style so another writer can imitate it.
Describe only HOW they write (length, structure, greeting, sign-off, tone, sentence style, recurring phrases), never
WHAT the emails were about, and never include names, companies, figures or anything confidential from the samples."""


class VoiceProfile(BaseModel):
    summary: str = Field(description="Two or three sentences on how this person writes.")
    typical_length_words: int
    greeting: str = Field(description='How they usually open, e.g. "Hi {first name}," or none.')
    sign_off: str = Field(description='How they usually close, e.g. "Cheers, Ankit".')
    formality: str = Field(description="casual | friendly-professional | formal")
    sentence_style: str = Field(description="e.g. short and direct; uses questions; bullet points")
    phrases_they_use: list[str] = Field(description="Up to 6 characteristic phrases (generic wording only).")
    phrases_they_avoid: list[str] = Field(description="Up to 6 things they never do, e.g. exclamation marks, jargon.")


def samples(db: Session, owner: str, limit: int = MAX_SAMPLES) -> list[str]:
    """The rep's own words: emails they sent from their mailbox, and drafts they rewrote substantially."""
    texts = [e.raw_text for e in db.scalars(
        select(ContextEvent).where(ContextEvent.source == "email", ContextEvent.author.ilike(owner),
                                   ContextEvent.raw_text.is_not(None)).order_by(ContextEvent.occurred_at.desc()).limit(limit))]
    rewritten = db.scalars(select(Approval).join(Approval.contact).where(
        Approval.status == "sent", Approval.edit_pct >= 30, Approval.reviewed_by.ilike(f"%{owner}%"))
        .order_by(Approval.sent_at.desc()).limit(limit))
    texts += [a.body for a in rewritten]
    return [t[:2500] for t in texts if t and len(t.split()) >= 15][:limit]


def to_instructions(p: dict, owner: str) -> str:
    return (f"WRITE IN {owner.upper()}'S OWN VOICE (they will send this):\n- {p['summary']}\n"
            f"- About {p['typical_length_words']} words; {p['sentence_style']}; {p['formality']}.\n"
            f"- Greeting: {p['greeting']}. Sign-off: {p['sign_off']}.\n"
            + (f"- Phrases they use: {', '.join(p['phrases_they_use'])}.\n" if p.get("phrases_they_use") else "")
            + (f"- Never: {', '.join(p['phrases_they_avoid'])}.\n" if p.get("phrases_they_avoid") else "")
            + "Keep every other rule (facts, sources, brand rules) exactly as before.")


async def build(owner: str, consented_by: str) -> dict:
    from ..agents.base import run_agent

    owner = owner.strip().lower()
    with session_scope() as db:
        texts = samples(db, owner)
    if len(texts) < MIN_SAMPLES:
        raise ValueError(f"Need at least {MIN_SAMPLES} emails {owner} wrote (found {len(texts)}). Connect their mailbox "
                         "on Integrations and let it sync, or try again after they have sent more emails.")
    result = await run_agent(
        agent="voice_profiler", model=settings.model_fast, role_prompt=ROLE, effort="low", max_tokens=1500,
        prompt="Emails written by this salesperson:\n\n" + "\n\n---\n\n".join(texts), output_format=VoiceProfile)
    profile = result.parsed.model_dump()
    with session_scope() as db:
        v = db.scalar(select(RepVoice).where(RepVoice.owner == owner)) or RepVoice(owner=owner)
        v.profile, v.sample_count, v.enabled = profile, len(texts), True
        v.consented_by, v.updated_at = consented_by, datetime.now(timezone.utc)
        db.add(v)
    return {"owner": owner, "samples": len(texts), "profile": profile}


def instructions_for(db: Session, owner: str | None) -> str | None:
    """The style instructions for this rep's drafts, or None when Rep Voice is off or not built."""
    if not owner:
        return None
    v = db.scalar(select(RepVoice).where(RepVoice.owner == owner.strip().lower(), RepVoice.enabled.is_(True)))
    return to_instructions(v.profile, v.owner) if v and v.profile else None
