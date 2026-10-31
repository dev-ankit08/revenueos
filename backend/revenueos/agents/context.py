"""Context Summariser (Haiku): turns raw Slack messages into per-account digests with extracted facts.

Raw messages are batched per account (and kept apart by internal vs customer-visible), so a
busy deal channel costs one cheap call per scheduler tick, not one per message.
"""
from collections import defaultdict
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import Account, ContextEvent
from ..services import memory
from .base import run_agent

DIGEST_ROLE = """You summarise a batch of Slack messages about ONE customer account for the sales team's memory.
Keep only what matters for winning and serving the deal: decisions, the customer's needs and concerns,
objections, who is involved (names and roles), commitments either side made (with dates), risks, and open
questions. Ignore chit-chat. Be factual; never invent. Attribute statements to who said them when it matters."""


class Digest(BaseModel):
    summary: str = Field(description="2-4 sentences: what was discussed and why it matters for the deal.")
    commitments: list[str] = Field(description="Promises made, each with who and when if stated.")
    objections: list[str]
    stakeholders: list[str] = Field(description="People mentioned with their role, e.g. 'Ravi (CFO), sceptical'.")
    risks: list[str]
    open_questions: list[str]


async def digest_slack(max_accounts: int = 20, parent_run_id: int | None = None) -> dict:
    """Summarise unprocessed Slack messages that are matched to an account."""
    with session_scope() as db:
        rows = list(db.scalars(select(ContextEvent).where(
            ContextEvent.source == "slack", ContextEvent.processed.is_(False),
            ContextEvent.account_id.isnot(None), ContextEvent.raw_text.isnot(None),
        ).order_by(ContextEvent.occurred_at)))
        batches: dict[tuple[int, bool], list[tuple[int, str]]] = defaultdict(list)
        for e in rows:
            batches[(e.account_id, e.internal)].append(
                (e.id, f"[{e.occurred_at:%Y-%m-%d %H:%M}] {e.author or '?'}: {e.raw_text}"))
        names = {a.id: a.name for a in db.scalars(select(Account).where(
            Account.id.in_({k[0] for k in batches})))} if batches else {}

    done = 0
    for (account_id, internal), msgs in list(batches.items())[:max_accounts]:
        ids = [i for i, _ in msgs]
        transcript = "\n".join(m for _, m in msgs)[-40_000:]
        result = await run_agent(
            agent="context_summariser", model=settings.model_fast, role_prompt=DIGEST_ROLE,
            prompt=(f"Account: {names.get(account_id)}. "
                    f"{'Internal team channel' if internal else 'Shared channel with the customer'}.\n"
                    f"<messages>\n{transcript}\n</messages>"),
            effort="low", max_tokens=3000, output_format=Digest, parent_run_id=parent_run_id,
        )
        d: Digest = result.parsed
        with session_scope() as db:
            memory.record(
                db, source="slack", source_ref=f"slack-digest:{account_id}:{ids[0]}-{ids[-1]}",
                account_id=account_id, occurred_at=datetime.now(timezone.utc),
                title=f"Slack digest ({len(ids)} messages)", summary=d.summary,
                facts=d.model_dump(exclude={"summary"}), internal=internal, processed=True,
            )
            for e in db.scalars(select(ContextEvent).where(ContextEvent.id.in_(ids))):
                e.processed = True
        done += 1
    return {"digests": done}
