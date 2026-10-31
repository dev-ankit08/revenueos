"""Handling a prospect's reply, wherever it arrives from (reply webhook, Gmail sync, Outlook sync)."""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..models import Activity, Approval, Contact, Suppression

OPT_OUT_PHRASES = ("unsubscribe", "remove me", "stop emailing", "take me off")
_QUOTE_START = re.compile(r"^(On .+ wrote:|-{2,}\s*Original Message\s*-{2,}|From: .+|Sent from my )", re.I)


def strip_quoted(text: str) -> str:
    """Keep only the new part of an email reply (drop the quoted thread below it)."""
    kept = []
    for line in text.splitlines():
        if _QUOTE_START.match(line.strip()):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip() or text.strip()


def suppress(db: Session, email: str, reason: str) -> None:
    if not db.scalar(select(Suppression).where(Suppression.email_or_domain == email.lower())):
        db.add(Suppression(email_or_domain=email.lower(), reason=reason))


def handle_reply(db: Session, contact: Contact, text: str, subject: str | None = None, triage: bool = True) -> dict:
    """Pause the contact's sequence at once, honour opt-outs, then start Reply Triage in the background.

    Commits, because the triage job reads the paused state from its own session.
    """
    from ..agents import replies  # agents import services; import late to avoid a cycle

    text = strip_quoted(text)
    opted_out = any(w in text.lower() for w in OPT_OUT_PHRASES)
    if opted_out:
        if contact.email:
            suppress(db, contact.email, "replied: opt-out")
        contact.status = "disqualified"
    else:
        contact.status = "replied"
    cancelled: list[tuple[int, str]] = []
    for a in db.scalars(select(Approval).where(Approval.contact_id == contact.id,
                                               Approval.status.in_(["pending", "approved"]))):
        cancelled.append((a.id, a.status))
        a.status, a.review_note = "rejected", "Auto-cancelled: prospect replied."
    db.add(Activity(contact_id=contact.id, type="reply", detail=text[:2000]))
    db.commit()

    result = {"matched": True, "contact_id": contact.id, "status": contact.status,
              "cancelled_followups": len(cancelled)}
    if triage and not opted_out:
        contact_id = contact.id

        async def work(run_id: int) -> str:
            return await replies.triage_reply(contact_id, text, subject, cancelled, parent_run_id=run_id)
        result["run_id"] = jobs.start("job:reply_triage", f"Triage reply from contact {contact_id}", work)
    return result
