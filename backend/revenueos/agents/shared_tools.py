"""Read-only tools several agents share."""
import json

from anthropic import beta_async_tool
from sqlalchemy import select

from ..db import session_scope
from ..models import Account, AccountBrief, Contact
from ..tools import kb


def brief_to_dict(brief: AccountBrief | None) -> dict | None:
    if not brief:
        return None
    return {
        "brief_id": brief.id,
        "created_at": brief.created_at.isoformat(),
        "summary": brief.summary,
        "priorities": brief.priorities,
        "pain_points": brief.pain_points,
        "recent_news": brief.recent_news,
        "recommended_angle": brief.recommended_angle,
        "relevant_proof_points": brief.relevant_proof_points,
        "likely_objections": brief.likely_objections,
        "sources": brief.sources,
    }


def contact_to_dict(c: Contact) -> dict:
    return {
        "contact_id": c.id, "name": c.full_name, "title": c.title, "email": c.email,
        "status": c.status, "score": c.score, "tier": c.score_tier, "source": c.source,
        "account_id": c.account_id, "account": c.account.name,
    }


def account_snapshot(db, account_id: int) -> dict | None:
    account = db.get(Account, account_id)
    if not account:
        return None
    return {
        "account_id": account.id, "name": account.name, "domain": account.domain,
        "industry": account.industry, "employees": account.employees, "country": account.country,
        "notes": account.notes,
        "contacts": [contact_to_dict(c) for c in account.contacts],
        "latest_brief": brief_to_dict(account.briefs[0] if account.briefs else None),
    }


@beta_async_tool
async def kb_search(query: str, category: str | None = None) -> str:
    """Search the company knowledge base (offerings, case studies, pricing, competitors, objection handling).

    Use it to ground every claim about what we sell or results we have delivered.

    Args:
        query: Keywords describing what you need, e.g. "manufacturing cost reduction case study".
        category: Optional filter: offering, case_study, icp, persona, brand_voice, competitor, pricing, objection.
    """
    with session_scope() as db:
        hits = kb.search(db, query, k=5, category=category)
    if not hits:
        return "No matching knowledge base entries. Do not invent facts; say the proof point is not available."
    return json.dumps(hits)


@beta_async_tool
async def get_account(account_id: int) -> str:
    """Get an account with its contacts and the latest research brief.

    Args:
        account_id: The account's id.
    """
    with session_scope() as db:
        snap = account_snapshot(db, account_id)
    return json.dumps(snap, default=str) if snap else f"No account with id {account_id}."


@beta_async_tool
async def get_contact(contact_id: int) -> str:
    """Get one contact (lead) with its score and pipeline status.

    Args:
        contact_id: The contact's id.
    """
    with session_scope() as db:
        c = db.get(Contact, contact_id)
        if not c:
            return f"No contact with id {contact_id}."
        data = contact_to_dict(c) | {"score_reason": c.score_reason, "inbound_message": c.inbound_message}
    return json.dumps(data, default=str)


@beta_async_tool
async def find_leads(
    status: str | None = None, min_score: float | None = None, search: str | None = None, limit: int = 20
) -> str:
    """List leads in the pipeline, best scored first.

    Args:
        status: Optional pipeline status: new, researched, sequenced, contacted, replied, meeting, disqualified.
        min_score: Optional minimum lead score (0-100).
        search: Optional text matched against contact name, title or company name.
        limit: Maximum number of rows (default 20).
    """
    with session_scope() as db:
        stmt = select(Contact).join(Account)
        if status:
            stmt = stmt.where(Contact.status == status)
        if min_score is not None:
            stmt = stmt.where(Contact.score >= min_score)
        if search:
            like = f"%{search}%"
            stmt = stmt.where(
                Contact.first_name.ilike(like) | Contact.last_name.ilike(like)
                | Contact.title.ilike(like) | Account.name.ilike(like)
            )
        stmt = stmt.order_by(Contact.score.desc().nullslast(), Contact.id.desc()).limit(min(limit, 100))
        rows = [contact_to_dict(c) for c in db.scalars(stmt)]
    return json.dumps(rows) if rows else "No leads match."
