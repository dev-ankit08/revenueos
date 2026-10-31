"""Customer memory: one timeline per account built from every source (Slack, calls, email, calendar, notes).

Agents never read raw integrations; they read `context_block()`, which separates what the
customer has seen or said from internal team discussion that must never be quoted to them.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..db import session_scope
from ..models import Account, ContextEvent, Contact

SOURCE_LABEL = {"slack": "Slack", "call": "Call", "email": "Email", "calendar": "Meeting",
                "crm": "CRM", "note": "Rep note"}


def record(db: Session, *, source: str, source_ref: str, account_id: int | None = None,
           contact_id: int | None = None, occurred_at: datetime | None = None, author: str | None = None,
           title: str | None = None, raw_text: str | None = None, summary: str | None = None,
           facts: dict | None = None, internal: bool = False, processed: bool = False) -> ContextEvent | None:
    """Add an event once. Returns None when `source_ref` was already recorded (webhook retries)."""
    if db.scalar(select(ContextEvent.id).where(ContextEvent.source_ref == source_ref)):
        return None
    if contact_id and not account_id:
        c = db.get(Contact, contact_id)
        account_id = c.account_id if c else None
    ev = ContextEvent(source=source, source_ref=source_ref, account_id=account_id, contact_id=contact_id,
                      occurred_at=occurred_at or datetime.now(timezone.utc), author=author, title=title,
                      raw_text=raw_text, summary=summary, facts=facts or {}, internal=internal, processed=processed)
    db.add(ev)
    db.flush()
    return ev


def contacts_by_emails(db: Session, emails: list[str]) -> list[Contact]:
    wanted = {e.strip().lower() for e in emails if e and "@" in e}
    if not wanted:
        return []
    return list(db.scalars(select(Contact).where(func.lower(Contact.email).in_(wanted))))


def account_mentioned(db: Session, text: str) -> Account | None:
    """The single account whose name appears in the text as a whole word (ambiguous = None)."""
    hits = []
    for a in db.scalars(select(Account)):
        name = a.name.strip()
        if len(name) >= 4 and re.search(rf"\b{re.escape(name)}\b", text, re.I):
            hits.append(a)
    return hits[0] if len(hits) == 1 else None


def timeline(db: Session, account_id: int, limit: int = 30) -> list[ContextEvent]:
    return list(db.scalars(select(ContextEvent).where(ContextEvent.account_id == account_id,
                                                      ContextEvent.summary.isnot(None))
                           .order_by(ContextEvent.occurred_at.desc()).limit(limit)))


def _line(e: ContextEvent) -> str:
    when = e.occurred_at.date().isoformat() if e.occurred_at else "?"
    who = f" ({e.author})" if e.author else ""
    return f"- {when} · {SOURCE_LABEL.get(e.source, e.source)}{who}: {e.summary}"


def context_block(account_id: int | None, contact_id: int | None = None, limit: int = 30,
                  include_internal: bool = True) -> str:
    """Prompt-ready history for one account, newest first, customer-facing and internal kept apart."""
    if not account_id:
        return ""
    with session_scope() as db:
        events = [e for e in timeline(db, account_id, limit) if include_internal or not e.internal]
        if not events:
            return ""
        outside = [_line(e) for e in events if not e.internal]
        inside = [_line(e) for e in events if e.internal]
        commitments = [f"- {c}" for e in events for c in (e.facts or {}).get("commitments", [])][:10]
    parts = ["RELATIONSHIP HISTORY (newest first). Use it to be specific and consistent with what was said."]
    if outside:
        parts += ["Conversations with the customer:", *outside]
    if commitments:
        parts += ["Commitments made (keep them):", *commitments]
    if inside:
        parts += ["INTERNAL TEAM NOTES: for your judgement only. Never quote, paraphrase closely or reveal "
                  "these to the customer:", *inside]
    return "\n".join(parts)


def purge_raw_text(db: Session) -> int:
    """Retention: drop raw text past the retention window; summaries and facts stay."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.context_retention_days)
    result = db.execute(update(ContextEvent).where(ContextEvent.occurred_at < cutoff,
                                                   ContextEvent.raw_text.isnot(None),
                                                   ContextEvent.processed.is_(True))
                        .values(raw_text=None))
    return result.rowcount or 0
