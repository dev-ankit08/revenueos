"""Reply Triage (Haiku) + reply drafting (Sonnet).

Every inbound reply is classified, the pipeline is updated accordingly, and when the
reply deserves an answer a draft goes to the approval queue.
"""
import json
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Activity, Approval, Contact, SalesDocument, Suppression
from ..services import memory, sequences
from ..tools.crm import get_crm
from .base import new_run, run_agent
from .shared_tools import brief_to_dict, get_account, get_contact, kb_search
from .writer import make_queue_tool

TRIAGE_ROLE = """You are Reply Triage. Classify a prospect's email reply to our outreach so the right thing
happens next. Be precise: an auto-reply is out_of_office, not interested. "Not right now, try in Q3" is
not_now. "Talk to Jane (jane@x.com) instead" is referral. "Remove me" / "stop" is unsubscribe.
Only extract a referral email or return date if it is written in the reply."""


class ReplyTriage(BaseModel):
    category: Literal[
        "interested", "question", "objection", "referral", "not_now",
        "out_of_office", "unsubscribe", "wrong_person", "other",
    ]
    sentiment: Literal["positive", "neutral", "negative"]
    summary: str = Field(description="One sentence: what they said.")
    urgency: Literal["high", "medium", "low"]
    needs_response: bool
    referral_name: str | None = None
    referral_email: str | None = None
    referral_title: str | None = None
    return_date: str | None = Field(default=None, description="ISO date they are back, if out of office.")
    follow_up_after: str | None = Field(default=None, description="When to re-engage, if not_now (as written).")
    suggested_next_step: str


DRAFT_ROLE = """You draft replies to prospects who answered our outreach. A human approves every reply.

- interested: thank them briefly, confirm the value in one line, propose two concrete time windows this
  week or next for a 30-minute call, and ask who else should join.
- question: answer it directly and specifically using kb_search; if the knowledge base lacks the answer,
  say you'll confirm and propose a short call.
- objection: acknowledge it, respond with the approved handling from kb_search (category "objection"),
  offer the low-risk next step, and don't push.
- referral: thank them and confirm you'll reach out to the person they named.
- not_now: thank them, confirm when you'll check back, offer one useful resource. No pressure.

Keep replies under 120 words, in the BRAND VOICE. Subject: "Re: " + their subject if known.
Queue exactly one email with queue_message (sequence_step=1, send_after_days=0), then stop."""


async def triage_reply(
    contact_id: int, text: str, subject: str | None = None,
    cancelled: list[tuple[int, str]] | None = None, parent_run_id: int | None = None,
) -> str:
    """Classify a reply and act on it. `cancelled` = follow-ups the webhook paused, as (approval_id, previous_status)."""
    t, run_id = await classify_reply(contact_id, text, subject, parent_run_id)
    outcome = _apply_triage(contact_id, t, cancelled or [], run_id)

    if t.needs_response and t.category in ("interested", "question", "objection", "referral", "not_now"):
        await draft_reply(contact_id, text, subject, t, parent_run_id)
        outcome += " Drafted a reply for approval."
    return f"{t.category} ({t.sentiment}): {t.summary} {outcome}"


async def classify_reply(contact_id: int, text: str, subject: str | None = None,
                         parent_run_id: int | None = None) -> tuple[ReplyTriage, int]:
    """The classification step alone (no side effects)."""
    with session_scope() as db:
        c = db.get(Contact, contact_id)
        context = {"contact": c.full_name, "title": c.title, "company": c.account.name,
                   "our_last_subject": subject}

    result = await run_agent(
        agent="reply_triage",
        model=settings.model_fast,
        role_prompt=TRIAGE_ROLE,
        prompt=f"Context: {json.dumps(context)}\n\nTheir reply:\n<reply>\n{text[:8000]}\n</reply>",
        effort="low",
        max_tokens=2000,
        output_format=ReplyTriage,
        parent_run_id=parent_run_id,
    )
    return result.parsed, result.run_id


def _apply_triage(contact_id: int, t: ReplyTriage, cancelled: list[tuple[int, str]], run_id: int) -> str:
    with session_scope() as db:
        c = db.get(Contact, contact_id)
        crm = get_crm()
        outcome = ""

        if t.category == "out_of_office":
            # Auto-reply: resume the sequence the webhook paused.
            for approval_id, prev in cancelled:
                a = db.get(Approval, approval_id)
                if a and a.status == "rejected":
                    a.status, a.review_note = prev, f"Resumed after out-of-office (back {t.return_date or 'unknown'})."
            c.status = "contacted"
            back = _parse_date(t.return_date)
            sequences.resume_contact(db, c.id, resume_at=back,
                                     note=f"Out of office until {t.return_date}" if back else "Out of office")
            outcome = "Out of office; sequence resumed" + (f" from {t.return_date}." if back else ".")
        elif t.category == "unsubscribe":
            if c.email and not db.scalar(select(Suppression).where(Suppression.email_or_domain == c.email.lower())):
                db.add(Suppression(email_or_domain=c.email.lower(), reason="replied: unsubscribe"))
            c.status = "disqualified"
            outcome = "Suppressed; no further contact."
        elif t.category == "wrong_person" and not t.referral_email:
            c.status = "disqualified"
            outcome = "Wrong person; marked disqualified."
        else:
            c.status = "replied"
        if t.category != "out_of_office":
            # A real answer: a person takes the conversation from here.
            sequences.stop_contact(db, c.id, f"Prospect replied ({t.category}).")
        if t.category in ("interested", "question", "referral", "objection"):
            from ..services import notify

            notify.hot_reply(c, t.category, t.summary)

        if t.referral_email and not db.scalar(select(Contact).where(Contact.email.ilike(t.referral_email))):
            first, _, last = (t.referral_name or t.referral_email.split("@")[0]).partition(" ")
            db.add(Contact(account_id=c.account_id, first_name=first, last_name=last or "",
                           title=t.referral_title, email=t.referral_email, source="referral",
                           inbound_message=f"Referred by {c.full_name}: {t.summary}"))
            outcome += f" Added referral {t.referral_email} as a new lead."

        db.add(Activity(contact_id=c.id, type=f"reply_{t.category}",
                        detail=f"[{t.urgency} urgency] {t.summary} Next: {t.suggested_next_step}"))
        memory.record(db, source="email", source_ref=f"email-reply:{c.id}:{run_id}", contact_id=c.id,
                      account_id=c.account_id, author=c.full_name, title=f"Reply ({t.category})",
                      summary=f"{c.full_name} replied ({t.category}, {t.sentiment}): {t.summary}",
                      facts={"stakeholders": [f"{t.referral_name} ({t.referral_title})"] if t.referral_name else [],
                             "open_questions": [t.summary] if t.category == "question" else [],
                             "objections": [t.summary] if t.category == "objection" else []},
                      processed=True)
        db.add(SalesDocument(kind="reply_triage", title=f"Reply from {c.full_name}: {t.category}",
                             content=t.summary, data=t.model_dump(), account_id=c.account_id,
                             contact_id=c.id, run_id=run_id))
        crm.log_activity(c, f"Reply ({t.category})", t.summary)
        crm.update_lead_status(c, c.status, c.score)
    return outcome.strip()


async def draft_reply(
    contact_id: int, their_reply: str, subject: str | None, t: ReplyTriage, parent_run_id: int | None
) -> str:
    with session_scope() as db:
        c = db.get(Contact, contact_id)
        brief = brief_to_dict(c.account.briefs[0] if c.account.briefs else None)
        grounding = str(brief or "")
        account_id = c.account_id

    prompt = (
        f"contact_id={contact_id}, account_id={account_id}. Category: {t.category}. "
        f"Triage summary: {t.summary}. Suggested next step: {t.suggested_next_step}.\n"
        f"Our subject: {subject or 'unknown'}\n\nTheir reply:\n<reply>\n{their_reply[:8000]}\n</reply>"
    )
    history = memory.context_block(account_id, contact_id)
    if history:
        prompt += "\n\n" + history
    if settings.booking_url and t.category in ("interested", "question", "objection", "not_now"):
        prompt += (f"\n\nMEETING BOOKING LINK: {settings.booking_url}\nIf they are open to a conversation, offer this "
                   "link as the easy way to pick a time (copy it exactly). Do not propose specific times yourself.")
        grounding += f"\n{settings.booking_url}"
    run_id = new_run("reply_writer", settings.model_worker, prompt, parent_run_id)
    result = await run_agent(
        agent="reply_writer",
        model=settings.model_worker,
        role_prompt=DRAFT_ROLE,
        prompt=prompt,
        tools=[get_contact, get_account, kb_search,
               make_queue_tool({contact_id}, run_id, grounding, agent_name="reply_writer")],
        effort="medium",
        run_id=run_id,
    )
    return result.text


def _parse_date(value: str | None):
    """ISO date from the triage (return from out of office) -> 09:00 UTC that day, or None."""
    from datetime import datetime, timezone

    try:
        d = datetime.fromisoformat((value or "").strip()[:10])
    except ValueError:
        return None
    return d.replace(hour=9, tzinfo=timezone.utc)
