"""Read-only tools several agents share."""
import json

from anthropic import beta_async_tool
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import settings
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
        "unverified_sources_removed": brief.unverified_sources or [],
        "stale": _stale(brief.created_at),
    }


def _stale(created_at) -> bool:
    from datetime import datetime, timedelta, timezone

    from ..config import settings

    if created_at is None:
        return False
    created = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created > timedelta(days=int(settings.brief_max_age_days))


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
        hits = kb.search(db, query, k=15, category=category)
    if not hits:
        return "No matching knowledge base entries. Do not invent facts; say the proof point is not available."
    hits = await rerank(query, hits, k=5)
    return json.dumps([{key: h[key] for key in ("doc_id", "chunk_id", "title", "category", "text")} for h in hits])


class _Ranking(BaseModel):
    best_first: list[int] = Field(description="Indexes of the passages that answer the query, most useful first. "
                                              "Leave out passages that do not help.")


async def rerank(query: str, hits: list[dict], k: int = 5) -> list[dict]:
    """Keyword search finds candidates; a fast model orders them by how well they answer the query.
    Falls back to keyword order if reranking is off or fails."""
    if not settings.kb_rerank or len(hits) <= k:
        return hits[:k]
    from .base import run_agent

    listing = "\n\n".join(f"[{i}] {h['title']} ({h['category']}): {h['text'][:700]}" for i, h in enumerate(hits))
    try:
        r = await run_agent(agent="kb_reranker", model=settings.model_fast, effort="low", max_tokens=400,
                            role_prompt="You rank knowledge-base passages by how directly they answer a query.",
                            prompt=f"Query: {query}\n\nPassages:\n{listing}", output_format=_Ranking)
        order = [i for i in r.parsed.best_first if 0 <= i < len(hits)]
    except Exception:
        return hits[:k]
    ranked = [hits[i] for i in dict.fromkeys(order)]
    return (ranked or hits)[:k]


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
